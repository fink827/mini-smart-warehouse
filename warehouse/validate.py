"""Independent physics check for a recorded twin run.

Rebuilds every robot's path from its recorded plan steps (not from the booking
table) and checks that nothing physically impossible happened.

    python -m warehouse.validate            # checks a few runs, prints a report
"""
from collections import defaultdict

from . import config as C
from . import fleet as F
from .layout import PODS, PICK, POD_SPOTS


def check(tw):
    """Check a recorded run, using the station layout that run was planned with."""
    saved = F.STATION
    F.set_station(getattr(tw, "station", "aisle"))
    try:
        return _check(tw)
    finally:
        F.set_station(saved)


def _check(tw):
    from .twin import docks
    problems = []
    occ = defaultdict(list)          # node -> [(t0, t1, robot)]
    holds = defaultdict(list)        # pod -> [(t0, t1, robot)]
    station = []
    pos = {}
    for rid, trips in tw.plans.items():
        prev_end = None
        for trip in trips:
            steps = trip["steps"]
            pod_node = PODS[trip["pod"]]
            loaded = False
            t_lift = None
            for i, s in enumerate(steps):
                kind, a, b, ts, te = s[:5]
                # continuity in space and time
                if i > 0:
                    pk, pa, pb, pts, pte = steps[i - 1][:5]
                    if pb != a or pte != ts:
                        problems.append(f"r{rid}: discontinuous plan at step {i}")
                if te <= ts:
                    problems.append(f"r{rid}: zero/negative duration step {s}")
                if kind == "move":
                    if b not in dict(F.fleet_neighbors(a)):
                        problems.append(f"r{rid}: illegal move {a}->{b}")
                    turn = s[6]
                    if te - ts - turn != F.edge_ticks(a, b):
                        problems.append(f"r{rid}: wrong travel time {a}->{b}")
                    if loaded and b in POD_SPOTS and b != pod_node:
                        problems.append(f"r{rid}: carried a pod through pod spot {b}")
                elif kind == "lift":
                    if a != pod_node or te - ts != F.LIFT:
                        problems.append(f"r{rid}: bad lift {s}")
                    loaded, t_lift = True, ts
                elif kind == "pick":
                    if a != PICK or not loaded:
                        problems.append(f"r{rid}: pick away from station / without pod")
                    station.append((ts, te, rid))
                elif kind == "lower":
                    if a != pod_node or not loaded:
                        problems.append(f"r{rid}: bad lower {s}")
                    loaded = False
                    holds[trip["pod"]].append((t_lift, te, rid))
                elif kind != "wait":
                    problems.append(f"r{rid}: unknown step {kind}")
            for node, t0, t1 in F.occupancy(steps):
                occ[node].append((t0, t1, rid))
            # parked at the pod spot from trip end until the next trip starts
            prev_end = steps[-1][4]
            pos[rid] = (pod_node, prev_end)
        # gaps between trips: robot parked where it ended
    for rid, trips in tw.plans.items():
        for j in range(len(trips) - 1):
            end_node, end_t = trips[j]["steps"][-1][2], trips[j]["steps"][-1][4]
            start_node, start_t = trips[j + 1]["steps"][0][1], trips[j + 1]["steps"][0][3]
            if end_node != start_node:
                problems.append(f"r{rid}: teleported between trips")
            if start_t > end_t:
                occ[end_node].append((end_t, start_t, rid))
        if trips and trips[0]["steps"][0][3] > 0:
            occ[trips[0]["steps"][0][1]].append((0, trips[0]["steps"][0][3], rid))
    # parked after the last trip until the run ended, and robots that never got a trip
    run_end = F.to_ticks(tw.env.now) + 1
    for rid in range(tw.n_robots):
        trips = tw.plans.get(rid, [])
        if trips:
            last = trips[-1]["steps"][-1]
            if run_end > last[4]:
                occ[last[2]].append((last[4], run_end, rid))
        else:
            occ[docks(tw.n_robots)[rid]].append((0, run_end, rid))

    def overlaps(ivs, label):
        ivs = sorted(ivs)
        n = 0
        for i in range(len(ivs)):
            for j in range(i + 1, len(ivs)):
                if ivs[j][0] >= ivs[i][1]:
                    break
                if ivs[j][2] != ivs[i][2]:
                    n += 1
                    if n <= 3:
                        problems.append(f"{label}: robots {ivs[i][2]} and {ivs[j][2]} overlap "
                                        f"({ivs[i][0]*F.TICK:.1f}-{ivs[i][1]*F.TICK:.1f}s vs "
                                        f"{ivs[j][0]*F.TICK:.1f}-{ivs[j][1]*F.TICK:.1f}s)")
        return n

    n_node = sum(overlaps(v, f"node {k}") for k, v in occ.items())
    # clearance: a robot at the pick point while a different robot is half a cell away
    n_clear = 0
    for near in F.CLEARANCE.get(PICK, ()):
        for a0, a1, ra in occ.get(PICK, []):
            for b0, b1, rb in occ.get(near, []):
                if ra != rb and a0 < b1 and b0 < a1:
                    n_clear += 1
                    if n_clear <= 3:
                        problems.append(f"clearance: robot {ra} at pick point and robot {rb} at {near} "
                                        f"({max(a0, b0) * F.TICK:.1f}s)")
    n_pod = sum(overlaps(v, f"pod {k}") for k, v in holds.items())
    n_station = overlaps(station, "packing station")
    return {"node_conflicts": n_node, "pod_conflicts": n_pod, "station_conflicts": n_station,
            "clearance_conflicts": n_clear,
            "trips": sum(len(t) for t in tw.plans.values()), "problems": problems}


if __name__ == "__main__":
    from .twin import run_static, run_stream
    cases = []
    for robots in (2, 3, 4):
        for pol in ("random", "abc"):
            for seed in (1, 2, 3):
                cases.append(("static", robots, pol, seed, run_static(pol, seed, 50, robots, record=True)))
    for rel in ("none", "fixed:3", "window:60", "pool"):
        cases.append(("stream " + rel, 3, "abc", 1,
                      run_stream(rel, 220, 1, 3, warmup_s=600, measure_s=3600, record=True)))
    bad = 0
    total = 0
    for name, robots, pol, seed, r in cases:
        rep = check(r["_twin"])
        total += rep["trips"]
        flag = (rep["node_conflicts"] + rep["pod_conflicts"] + rep["station_conflicts"]
                + rep["clearance_conflicts"] + len(rep["problems"]))
        bad += flag > 0
        print(f"{name:<16} {robots} robots {pol:>6} seed {seed}: {rep['trips']:4d} trips, "
              f"node {rep['node_conflicts']}, pod {rep['pod_conflicts']}, station {rep['station_conflicts']}, "
              f"clearance {rep['clearance_conflicts']}"
              + ("" if not rep["problems"] else "  <-- " + "; ".join(rep["problems"][:3])))
    print(f"\n{len(cases)} runs, {total} trips checked, {bad} runs with any violation")
