"""Traffic-aware digital twin.

Same robots, timings, orders and slotting as sim.py, but physically valid:
  * every trip is planned by the fleet manager (fleet.py), so robots never
    collide, never pass through each other, and queue in the aisle for packing
  * a pod can be held by only one robot at a time (pod locking)
  * robots are dispatched by a central fleet manager, not a free-for-all queue

It also runs in two modes:
  * static:    all orders released at t=0 (the setup of Experiments 1 and 2)
  * streaming: orders arrive over time (Poisson process) — Experiment 3

Release policies decide when arrived orders become pod trips ("visits"):
  * "none"       each order is released the moment it arrives
  * "fixed:k"    hold orders until k have accumulated, release them together
  * "window:T"   every T seconds, release whatever has accumulated
  * "pool"       release on arrival, and if a trip to the same pod is already
                 waiting for a robot, add the new lines to that trip instead
                 of creating another one (no order is ever held back)

Within one release, all lines that need the same pod share one trip
(this is exactly the batching rule of sim.py).
"""
import math
import random
import statistics
from collections import defaultdict

import simpy

from . import config as C
from .layout import PODS, PICK
from .orders import generate_orders, get_slotting
from . import fleet as F

EXTRA_DOCKS = [(2, 5), (3, 5), (0, 4), (0, 3)]  # used only for robots beyond len(C.DOCKS)


def docks(n_robots):
    d = list(C.DOCKS) + [x for x in EXTRA_DOCKS if x not in C.DOCKS]
    if n_robots > len(d):
        raise ValueError(f"only {len(d)} dock spots defined")
    return d[:n_robots]


def parse_release(policy):
    if policy in ("none", "pool"):
        return policy, None
    kind, _, arg = policy.partition(":")
    if kind == "fixed":
        return kind, int(arg)
    if kind == "window":
        return kind, float(arg)
    raise ValueError(f"unknown release policy {policy!r}")


class Twin:
    def __init__(self, slotting, n_robots, release="none", order_done="trip",
                 record=False, dispatch="nearest", station="aisle"):
        F.set_station(station)
        self.station = station
        self.env = simpy.Environment()
        self.res = F.Reservations()
        self.slotting = slotting
        self.n_robots = n_robots
        self.release_kind, self.release_arg = parse_release(release)
        self.order_done_rule = order_done   # "trip": pod returned (sim.py rule); "pick": last line picked
        self.dispatch_rule = dispatch
        self.record = record

        self.queue = []                 # visits waiting for a robot, FIFO
        self.pool = {}                  # pod -> waiting visit (pool policy)
        self.pod_busy = set()
        self.idle = {}                  # robot -> wake event
        self.here = {}                  # robot -> node
        self.held = []                  # orders waiting to be released (fixed/window)
        self.orders = {}                # oid -> dict
        self.trips = []
        self.busy_ticks = [0] * n_robots
        self.plan_failures = 0
        self.plans = defaultdict(list)  # robot -> [steps,...] (record mode)
        self.visit_seq = 0
        # robot idle time, split by cause (integrated between state changes)
        self.idle_blocked_s = 0.0      # idle while trips were waiting, but every waiting trip's pod was taken
        self.idle_empty_s = 0.0        # idle with no trips waiting at all
        self._acc_t = 0.0

        for rid, d in enumerate(docks(n_robots)):
            self.here[rid] = d
            self.res.park_at(rid, d, 0)
        for rid in range(n_robots):
            self.env.process(self._robot(rid))

    # ------------------------------------------------------------ orders in
    def add_order(self, oid, lines):
        self.orders[oid] = {"lines": lines, "arrive": self.env.now, "release": None,
                            "open": 0, "picked": None, "done": None}
        if self.release_kind in ("none", "pool"):
            self._release([oid])
        elif self.release_kind == "fixed":
            self.held.append(oid)
            if len(self.held) >= self.release_arg:
                self._release(self.held)
                self.held = []
        else:  # window: the window process releases
            self.held.append(oid)

    def flush(self):
        """Release anything still held (end of a static order list)."""
        if self.held:
            self._release(self.held)
            self.held = []

    def window_process(self):
        while True:
            yield self.env.timeout(self.release_arg)
            if self.held:
                self._release(self.held)
                self.held = []

    def _account(self):
        """Integrate idle robot-time since the last state change."""
        dt = self.env.now - self._acc_t
        if dt > 0 and self.idle:
            if self.queue:
                self.idle_blocked_s += dt * len(self.idle)
            else:
                self.idle_empty_s += dt * len(self.idle)
        self._acc_t = self.env.now

    def _release(self, oids):
        self._account()
        by_pod = {}
        for oid in oids:
            o = self.orders[oid]
            o["release"] = self.env.now
            for sku, units in o["lines"]:
                by_pod.setdefault(self.slotting[sku], []).append((oid, sku, units))
        for pod, plines in by_pod.items():
            if self.release_kind == "pool" and pod in self.pool:
                v = self.pool[pod]
                v["lines"] += plines
                new = {oid for oid, _, _ in plines} - v["orders"]
            else:
                v = {"id": self.visit_seq, "pod": pod, "lines": list(plines), "orders": set(),
                     "created": self.env.now}
                self.visit_seq += 1
                self.queue.append(v)
                if self.release_kind == "pool":
                    self.pool[pod] = v
                new = {oid for oid, _, _ in plines}
            v["orders"] |= new
            for oid in new:
                self.orders[oid]["open"] += 1
        self._dispatch()

    # ------------------------------------------------------------ dispatch
    def _dispatch(self):
        self._account()
        if not self.idle:
            return
        i = 0
        while i < len(self.queue) and self.idle:
            v = self.queue[i]
            if v["pod"] in self.pod_busy:
                i += 1
                continue
            node = PODS[v["pod"]]
            under = [r for r in self.idle if self.here[r] == node]
            if under:
                rid = under[0]
            elif self.dispatch_rule == "nearest":
                lb = F.lower_bound(node, False)
                rid = min(self.idle, key=lambda r: (lb[self.here[r]], r))
            else:
                rid = min(self.idle)
            # A pod spot can hold one robot: if an idle robot is parked under this
            # pod it must be the one to take it (handled above). Otherwise go.
            self.queue.pop(i)
            if self.pool.get(v["pod"]) is v:
                del self.pool[v["pod"]]
            self.pod_busy.add(v["pod"])
            ev = self.idle.pop(rid)
            ev.succeed(v)

    # ------------------------------------------------------------ robots
    def _robot(self, rid):
        env = self.env
        while True:
            ev = env.event()
            self._account()
            self.idle[rid] = ev
            self._dispatch()
            v = yield ev
            pod_node = PODS[v["pod"]]
            units = sum(u for _, _, u in v["lines"])
            legs = [F.Leg(pod_node, False, F.LIFT, "lift"),
                    F.Leg(PICK, True, F.pick_ticks(units), "pick"),
                    F.Leg(pod_node, True, F.LOWER, "lower")]
            while True:
                t0 = F.to_ticks(env.now)
                self.res.release(rid)
                steps = F.plan(self.res, rid, self.here[rid], t0, legs)
                if steps is not None:
                    break
                self.plan_failures += 1
                tries = getattr(self, "_tries", {})
                tries[rid] = tries.get(rid, 0) + 1
                self._tries = tries
                if tries[rid] > 60:
                    raise RuntimeError(f"robot {rid} has had no feasible route for {tries[rid]} tries "
                                       f"(deadlock) at t={env.now:.0f}s heading to pod {v['pod']}")
                self.res.park_at(rid, self.here[rid], t0)   # stay put, retry shortly
                yield env.timeout(5 * F.TICK)
            getattr(self, "_tries", {}).pop(rid, None)
            F.book_plan(self.res, rid, steps)
            if self.record:
                self.plans[rid].append({"steps": steps, "pod": v["pod"], "visit": v["id"]})
            self.res.prune(t0 - 50)

            lift_end = next(s[4] for s in steps if s[0] == "lift")
            pick = next(s for s in steps if s[0] == "pick")
            end = steps[-1][4]
            start = t0
            # pick finished
            yield env.timeout(max(0.0, pick[4] * F.TICK - env.now))
            picked_at = env.now
            # trip finished (pod lowered back home)
            yield env.timeout(max(0.0, end * F.TICK - env.now))
            self.here[rid] = pod_node
            self.pod_busy.discard(v["pod"])
            cells = sum((0.5 if PICK in (s[1], s[2]) else 1.0) for s in steps if s[0] == "move")
            waited = sum(s[4] - s[3] for s in steps if s[0] == "wait")
            self.busy_ticks[rid] += end - start - waited
            self.trips.append({"robot": rid, "pod": v["pod"], "cells": cells,
                               "wait_s": waited * F.TICK, "duration_s": (end - start) * F.TICK,
                               "units": units, "lines": len(v["lines"]), "orders": len(v["orders"]),
                               "pick_start": pick[3] * F.TICK, "pick_end": picked_at,
                               "lift_end": lift_end * F.TICK, "end": env.now})
            for oid in v["orders"]:
                o = self.orders[oid]
                o["open"] -= 1
                o["picked"] = max(o["picked"] or 0, picked_at)
                if o["open"] == 0:
                    o["done"] = env.now if self.order_done_rule == "trip" else o["picked"]
                    self.on_order_done(oid)

    def on_order_done(self, oid):
        pass


# ===================================================================== runs
def run_static(policy="abc", seed=1, n_orders=C.N_ORDERS, n_robots=C.N_ROBOTS, batch_size=1,
               record=False, slotting=None, station="aisle"):
    """Experiments 1/2 setup (all orders at t=0), on the traffic-aware twin.
    Returns the same metric names as sim.run()."""
    orders = generate_orders(n_orders, seed)
    slot = slotting if slotting is not None else get_slotting(policy, seed)
    tw = Twin(slot, n_robots, release=f"fixed:{batch_size}", order_done="trip", record=record, station=station)
    all_done = tw.env.event()
    done = [0]

    def hook(oid):
        done[0] += 1
        if done[0] == len(orders) and not all_done.triggered:
            all_done.succeed()
    tw.on_order_done = hook
    for oid, lines in enumerate(orders):
        tw.add_order(oid, lines)
    tw.flush()
    tw.env.run(until=all_done)
    tw._account()

    makespan = tw.env.now
    out = {
        "policy": policy, "seed": seed, "batch_size": batch_size, "robots": n_robots, "station": station,
        "orders": len(orders), "trips": len(tw.trips),
        "trips_per_order": len(tw.trips) / len(orders),
        "makespan_min": makespan / 60,
        "throughput_per_hr": len(orders) / makespan * 3600,
        "avg_completion_min": statistics.mean(o["done"] for o in tw.orders.values()) / 60,
        "cells_per_order": sum(t["cells"] for t in tw.trips) / len(orders),
        "utilization_pct": 100 * statistics.mean(b * F.TICK / makespan for b in tw.busy_ticks),
        "wait_s_per_trip": statistics.mean(t["wait_s"] for t in tw.trips),
        "plan_failures": tw.plan_failures,
        "idle_blocked_pct": 100 * tw.idle_blocked_s / (n_robots * makespan),
        "idle_empty_pct": 100 * tw.idle_empty_s / (n_robots * makespan),
    }
    if record:
        out["_twin"] = tw
    return out


def poisson_orders(rate_per_hr, until_s, seed):
    """Arrival times (s) and order contents. Contents come from the same
    generator as sim.py; arrival times from an independent stream, so every
    release policy sees identical orders at identical times (common random numbers)."""
    rng = random.Random(seed * 7919 + 13)
    times = []
    t = 0.0
    while True:
        t += rng.expovariate(rate_per_hr / 3600.0)
        if t > until_s:
            break
        times.append(t)
    contents = generate_orders(len(times), seed + 100_000)
    return list(zip(times, contents))


def run_stream(release="none", rate_per_hr=150, seed=1, n_robots=C.N_ROBOTS, slotting="abc",
               warmup_s=1800, measure_s=4 * 3600, drain_cap_s=1200, wip_abort=400, record=False,
               station="aisle"):
    """Experiment 3: orders arrive over time.

    Orders arriving in [warmup, warmup + measure] are the measured sample. Arrivals
    then stop and the system drains so every measured order has a completion time.
    Response time = arrival -> last line picked (what a customer waits for).
    A run whose backlog passes `wip_abort` orders is stopped early and marked
    unstable (it is not going to recover, and simulating it is slow).
    """
    slot = get_slotting(slotting, seed) if isinstance(slotting, str) else slotting
    tw = Twin(slot, n_robots, release=release, order_done="pick", record=record, station=station)
    t_end_arrivals = warmup_s + measure_s
    stream = poisson_orders(rate_per_hr, t_end_arrivals, seed)
    env = tw.env

    def source():
        for oid, (t, lines) in enumerate(stream):
            yield env.timeout(t - env.now)
            tw.add_order(oid, lines)
        tw.flush()
    env.process(source())
    if tw.release_kind == "window":
        env.process(tw.window_process())

    # sample work-in-progress every minute (stability diagnostic)
    wip = []

    def sampler():
        while True:
            yield env.timeout(60)
            wip.append((env.now, sum(1 for o in tw.orders.values() if o["done"] is None)))
    env.process(sampler())

    measured = [oid for oid, (t, _) in enumerate(stream) if warmup_s <= t <= t_end_arrivals]
    aborted = False
    while env.now < t_end_arrivals:
        env.run(until=min(t_end_arrivals, env.now + 300))
        if sum(1 for o in tw.orders.values() if o["done"] is None) > wip_abort:
            aborted = True
            break
    backlog_at_end = sum(1 for o in tw.orders.values() if o["done"] is None)
    deadline = t_end_arrivals + drain_cap_s
    while not aborted and env.now < deadline and any(tw.orders[o]["done"] is None for o in measured):
        env.run(until=min(deadline, env.now + 60))
    finished = [oid for oid in measured if tw.orders.get(oid, {}).get("done") is not None]
    resp = sorted(tw.orders[o]["done"] - tw.orders[o]["arrive"] for o in finished)
    hold = [tw.orders[o]["release"] - tw.orders[o]["arrive"] for o in finished]

    in_window = [t for t in tw.trips if warmup_s <= t["pick_end"] <= t_end_arrivals]
    done_in_window = sum(1 for o in tw.orders.values()
                         if o["done"] is not None and warmup_s <= o["done"] <= t_end_arrivals)
    station_busy = sum(t["pick_end"] - t["pick_start"] for t in in_window)
    wip_measure = [w for t, w in wip if warmup_s <= t <= t_end_arrivals]
    half = len(wip_measure) // 2
    stable = (not aborted and len(finished) == len(measured)
              and done_in_window >= 0.95 * len(measured)
              and (half == 0 or statistics.mean(wip_measure[half:]) < 2.0 * statistics.mean(wip_measure[:half]) + 3))

    def pct(xs, p):
        if not xs:
            return float("nan")
        k = (len(xs) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

    out = {
        "release": release, "rate_per_hr": rate_per_hr, "seed": seed, "robots": n_robots, "station": station,
        "orders_measured": len(measured), "orders_finished": len(finished),
        "throughput_per_hr": done_in_window / measure_s * 3600,
        "resp_mean_min": statistics.mean(resp) / 60 if resp else float("nan"),
        "resp_p50_min": pct(resp, 0.5) / 60, "resp_p95_min": pct(resp, 0.95) / 60,
        "hold_mean_min": statistics.mean(hold) / 60 if hold else float("nan"),
        "trips_per_order": len(in_window) / max(1, done_in_window),
        "units_per_trip": statistics.mean(t["units"] for t in in_window) if in_window else float("nan"),
        "robot_util_pct": 100 * sum(t["duration_s"] - t["wait_s"] for t in in_window) / (n_robots * measure_s),
        "station_util_pct": 100 * station_busy / measure_s,
        "wait_s_per_trip": statistics.mean(t["wait_s"] for t in in_window) if in_window else float("nan"),
        "wip_mean": statistics.mean(wip_measure) if wip_measure else float("nan"),
        "backlog_at_end": backlog_at_end,
        "stable": stable,
        "aborted": aborted,
        "plan_failures": tw.plan_failures,
    }
    if record:
        out["_twin"] = tw
    return out
