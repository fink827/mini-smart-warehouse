"""Audit the original (ideal) twin for physically impossible events.

sim.py doesn't model robots as physical objects, so two robots can occupy the
same tape node, both "hold" the same pod, or both sit on the packing point.
This replays sim.py's exact logic with a full space-time log and counts those
events. It also exports trajectories for the side-by-side replay.

Run from the project folder:
    python -m experiments.audit
    python -m experiments.audit --robots 4
"""
import argparse
from collections import defaultdict

import simpy

from warehouse import config as C
from warehouse.layout import PODS, PICK, shortest_path, travel_seconds, coords
from warehouse.orders import generate_orders, get_slotting
from warehouse.sim import run as ideal_run


def timed_nodes(path, t0):
    """[(node, t_arrive, t_depart)] using sim.py's timing (turn time charged at the turning node)."""
    out, t, dirs = [], t0, []
    for a, b in zip(path, path[1:]):
        (ax, ay), (bx, by) = coords(a), coords(b)
        dirs.append((round(bx - ax, 3) != 0, round(by - ay, 3) != 0))
    for i, node in enumerate(path):
        dwell = C.TURN_SECONDS if 0 < i < len(path) - 1 and dirs[i - 1] != dirs[i] else 0.0
        out.append((node, t, t + dwell))
        if i < len(path) - 1:
            t += dwell + (0.5 if PICK in (node, path[i + 1]) else 1.0) * C.SECONDS_PER_CELL
    return out


def replay(policy, seed, n_orders=C.N_ORDERS, n_robots=C.N_ROBOTS, batch_size=1):
    """Re-run sim.py's model, logging where every robot is and which pod it holds."""
    env = simpy.Environment()
    station = simpy.Resource(env, capacity=1)
    tasks = simpy.Store(env)
    orders = generate_orders(n_orders, seed)
    slotting = get_slotting(policy, seed)
    remaining = {oid: 0 for oid in range(len(orders))}
    done = {}
    all_done = env.event()
    for b0 in range(0, len(orders), batch_size):
        by_pod = {}
        for oid in range(b0, min(b0 + batch_size, len(orders))):
            for sku, units in orders[oid]:
                by_pod.setdefault(slotting[sku], []).append((oid, sku, units))
        for pod, plines in by_pod.items():
            served = sorted({oid for oid, _, _ in plines})
            for oid in served:
                remaining[oid] += 1
            tasks.put({"orders": served, "pod": pod, "lines": [(s, u) for _, s, u in plines]})

    occ = defaultdict(list)    # robot -> [(node, t0, t1)] stationary intervals
    keys = defaultdict(list)   # robot -> [(t, node, pod_or_None)] keyframes
    holds = defaultdict(list)  # pod -> [(robot, t0, t1)]
    station_log = []

    def robot(rid):
        here = C.DOCKS[rid % len(C.DOCKS)]
        keys[rid].append((0.0, here, None))
        last = 0.0
        while True:
            task = yield tasks.get()
            occ[rid].append((here, last, env.now))
            pod = task["pod"]
            pod_node = PODS[pod]
            p1 = shortest_path(here, pod_node, loaded=False)
            p2 = shortest_path(pod_node, PICK, loaded=True)
            p3 = shortest_path(PICK, pod_node, loaded=True)
            for node, ta, td in timed_nodes(p1, env.now):
                keys[rid] += [(ta, node, None), (td, node, None)]
            occ[rid] += [(n, a, d) for n, a, d in timed_nodes(p1, env.now)[:-1]]
            yield env.timeout(travel_seconds(p1))
            lift0 = env.now
            yield env.timeout(C.LIFT_SECONDS)
            occ[rid].append((pod_node, lift0, env.now))
            keys[rid].append((env.now, pod_node, pod))
            for node, ta, td in timed_nodes(p2, env.now):
                keys[rid] += [(ta, node, pod), (td, node, pod)]
            occ[rid] += [(n, a, d) for n, a, d in timed_nodes(p2, env.now)[1:-1]]
            yield env.timeout(travel_seconds(p2))
            arrive = env.now
            with station.request() as req:
                yield req
                units = sum(u for _, u in task["lines"])
                station_log.append((rid, arrive, env.now))
                yield env.timeout(C.PICK_SECONDS_BASE + C.PICK_SECONDS_PER_UNIT * units)
            occ[rid].append((PICK, arrive, env.now))
            keys[rid].append((env.now, PICK, pod))
            for node, ta, td in timed_nodes(p3, env.now):
                keys[rid] += [(ta, node, pod), (td, node, pod)]
            occ[rid] += [(n, a, d) for n, a, d in timed_nodes(p3, env.now)[1:-1]]
            yield env.timeout(travel_seconds(p3))
            yield env.timeout(C.LOWER_SECONDS)
            keys[rid].append((env.now, pod_node, None))
            holds[pod].append((rid, lift0, env.now))
            here, last = pod_node, env.now
            for oid in task["orders"]:
                remaining[oid] -= 1
                if remaining[oid] == 0:
                    done[oid] = env.now
            if len(done) == len(orders) and not all_done.triggered:
                all_done.succeed()

    for rid in range(n_robots):
        env.process(robot(rid))
    env.run(until=all_done)
    for rid in range(n_robots):
        occ[rid].append((keys[rid][-1][1], keys[rid][-1][0], env.now))
    return {"makespan": env.now, "occ": occ, "keys": keys, "holds": holds,
            "station": station_log, "orders": len(orders), "done": dict(done)}


def _overlap_events(ivs):
    """[(t0, t1, robot_a, robot_b)] for overlapping intervals of different robots."""
    ivs = sorted(ivs, key=lambda x: x[1])
    ev = []
    for i in range(len(ivs)):
        for j in range(i + 1, len(ivs)):
            if ivs[j][1] >= ivs[i][2] - 1e-9:
                break
            if ivs[j][0] != ivs[i][0]:
                ev.append((ivs[j][1], min(ivs[i][2], ivs[j][2]), ivs[i][0], ivs[j][0]))
    return ev


def violations(rep):
    by_node = defaultdict(list)
    for rid, ivs in rep["occ"].items():
        for node, t0, t1 in ivs:
            if node != PICK:
                by_node[node].append((rid, t0, max(t1, t0 + 1e-6)))
    node_ev = [(a, b, ra, rb, node) for node, v in by_node.items() for a, b, ra, rb in _overlap_events(v)]
    pod_ev = [(a, b, ra, rb, pod) for pod, v in rep["holds"].items() for a, b, ra, rb in _overlap_events(v)]
    stacked = [(rid, a, s) for rid, a, s in rep["station"] if s - a > 1e-9]
    return {"node": _episodes(node_ev), "pod": _episodes(pod_ev), "stacked_at_pick": stacked}


def _episodes(events, gap=0.5):
    """Merge overlaps of the same robot pair at the same place into one episode,
    so one incident logged across several intervals is counted once."""
    merged = {}
    out = []
    for a, b, ra, rb, where in sorted(events):
        key = (min(ra, rb), max(ra, rb), where)
        last = merged.get(key)
        if last is not None and a <= last[1] + gap:
            last[1] = max(last[1], b)
            continue
        cur = [a, b, ra, rb, where]
        merged[key] = cur
        out.append(cur)
    return [tuple(e) for e in out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robots", type=int, default=C.N_ROBOTS)
    ap.add_argument("--orders", type=int, default=C.N_ORDERS)
    ap.add_argument("--seeds", type=int, default=len(C.SEEDS))
    args = ap.parse_args()
    print(f"\nAudit of the ideal twin (sim.py): {args.orders} orders, {args.robots} robots, "
          f"{args.seeds} seeds\n")
    for batch in (1, 5):
        for pol in ("random", "abc"):
            tot = dict(trips=0, node=0, pod=0, stacked=0)
            for seed in range(1, args.seeds + 1):
                rep = replay(pol, seed, args.orders, args.robots, batch)
                ref = ideal_run(pol, seed, args.orders, args.robots, batch)
                assert abs(rep["orders"] / rep["makespan"] * 3600 - ref["throughput_per_hr"]) < 1e-9
                v = violations(rep)
                tot["trips"] += sum(len(h) for h in rep["holds"].values())
                tot["node"] += len(v["node"])
                tot["pod"] += len(v["pod"])
                tot["stacked"] += len(v["stacked_at_pick"])
            print(f"  batch {batch} {pol:>6}: {tot['trips']:4d} trips | pod held by two robots at once: "
                  f"{tot['pod']:3d} ({100 * tot['pod'] / tot['trips']:4.1f}% of trips) | robots on the same node: "
                  f"{tot['node']:3d} | robots stacked on the pick point: {tot['stacked']}")
    print("\nEvery one of these is impossible on the real floor. warehouse/twin.py removes all of them;"
          "\nwarehouse/validate.py checks that independently.\n")


if __name__ == "__main__":
    main()
