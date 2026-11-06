"""Export robot trajectories from both twins for the side-by-side replay page.

    python -m experiments.export_replay        # writes results/replay.json
"""
import json, os

from warehouse import config as C
from warehouse import fleet as F
from warehouse.layout import PODS, PICK, coords
from warehouse.twin import run_static, docks
from experiments.audit import replay, violations

POD_NAMES = sorted(PODS)


def xy(node):
    x, y = coords(node)
    return round(x, 3), round(y, 3)


def traffic_tracks(tw, n_robots):
    tracks = {}
    for rid in range(n_robots):
        d = docks(n_robots)[rid]
        k = [[0.0, *xy(d), -1]]
        for trip in tw.plans.get(rid, []):
            pod = POD_NAMES.index(trip["pod"])
            loaded = False
            for s in trip["steps"]:
                kind, a, b, ts, te = s[:5]
                p = pod if loaded else -1
                if kind == "move":
                    turn = s[6]
                    k.append([round(ts * F.TICK, 2), *xy(a), p])
                    if turn:
                        k.append([round((ts + turn) * F.TICK, 2), *xy(a), p])
                    k.append([round(te * F.TICK, 2), *xy(b), p])
                else:
                    if kind == "lift":
                        loaded = True
                    k.append([round(ts * F.TICK, 2), *xy(a), p])
                    k.append([round(te * F.TICK, 2), *xy(a), pod if loaded else -1])
                    if kind == "lower":
                        loaded = False
                        k.append([round(te * F.TICK, 2) + 0.01, *xy(a), -1])
        tracks[rid] = k
    return [tracks[r] for r in range(n_robots)]


def ideal_tracks(rep, n_robots):
    out = []
    for rid in range(n_robots):
        k = []
        for t, node, pod in sorted(rep["keys"][rid], key=lambda x: x[0]):
            k.append([round(t, 2), *xy(node), POD_NAMES.index(pod) if pod else -1])
        out.append(k)
    return out


def scenario(label, policy, robots, seed=1, batch=1):
    tr = run_static(policy, seed, 50, robots, batch, record=True)
    tw = tr["_twin"]
    rep = replay(policy, seed, 50, robots, batch)
    v = violations(rep)
    events = []
    for a, b, ra, rb, node in v["node"]:
        events.append({"t": round(a, 2), "t1": round(b, 2), "kind": "collision", "x": xy(node)[0], "y": xy(node)[1],
                       "robots": [ra, rb]})
    for a, b, ra, rb, pod in v["pod"]:
        events.append({"t": round(a, 2), "t1": round(b, 2), "kind": "pod", "pod": POD_NAMES.index(pod), "robots": [ra, rb]})
    events.sort(key=lambda e: e["t"])
    done_traffic = sorted(round(o["done"], 2) for o in tw.orders.values())
    return {
        "label": label, "policy": policy, "robots": robots, "seed": seed, "batch": batch,
        "ideal": {"tracks": ideal_tracks(rep, robots), "done": sorted(rep_done(rep)), "end": round(rep["makespan"], 2),
                  "throughput": round(rep["orders"] / rep["makespan"] * 3600, 1), "events": events},
        "traffic": {"tracks": traffic_tracks(tw, robots), "done": done_traffic, "end": round(tw.env.now, 2),
                    "throughput": round(tr["throughput_per_hr"], 1),
                    "wait_s": round(sum(t["wait_s"] for t in tw.trips), 1),
                    "waits": sorted([round(st[3] * F.TICK, 2), round(st[4] * F.TICK, 2)]
                                    for trips in tw.plans.values() for tr in trips for st in tr["steps"] if st[0] == "wait")},
    }


def rep_done(rep):
    """Order completion times in the ideal model (when each order's last pod is back home)."""
    return [round(t, 2) for t in rep["done"].values()]


def main():
    data = {
        "grid": {"cols": C.GRID_COLS, "rows": C.GRID_ROWS},
        "pods": [{"name": n, "x": PODS[n][0], "y": PODS[n][1]} for n in POD_NAMES],
        "pick": list(xy(PICK)),
        "pickBetween": [list(p) for p in C.PICK_BETWEEN],
        "docks": [list(d) for d in docks(4)],
        "scenarios": [
            scenario("2 robots · ABC (your current setup)", "abc", 2),
            scenario("4 robots · ABC", "abc", 4),
            scenario("4 robots · ABC spread over 3 pods", "spread:3", 4),
        ],
    }
    os.makedirs("results", exist_ok=True)
    with open("results/replay.json", "w") as f:
        json.dump(data, f, separators=(",", ":"))
    for s in data["scenarios"]:
        n_col = sum(e["kind"] == "collision" for e in s["ideal"]["events"])
        n_pod = sum(e["kind"] == "pod" for e in s["ideal"]["events"])
        print(f"{s['label']:<40} ideal {s['ideal']['throughput']:6.1f}/hr ({n_col} collisions, {n_pod} shared pods) | "
              f"traffic-aware {s['traffic']['throughput']:6.1f}/hr")
    print(f"wrote results/replay.json ({os.path.getsize('results/replay.json') // 1024} KB)")


if __name__ == "__main__":
    main()
