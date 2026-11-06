"""Where should the packing station go: on the aisle, or in a side bay?

The current layout puts the pick point ON the column-6 tape. While a robot is
picking there, column 6 is blocked, and robots can't wait right next to it.
The alternative puts it in a bay one cell east of column 6: a slightly longer
detour for every trip, but column 6 stays open.

Static setup (Experiments 1-2): throughput, 1-4 robots, 20 seeds.
For streaming orders with 4 robots, see experiments/fleet4.py.

Run from the project folder:
    python -m experiments.station
"""
import argparse, csv, os, statistics
from multiprocessing import Pool

from warehouse.twin import run_static
from experiments.slotting import ci


def static_job(a):
    station, robots, seed, batch = a
    r = run_static("abc", seed, 50, robots, batch, station=station)
    return {"station": station, "robots": robots, "seed": seed, "batch": batch,
            "throughput": r["throughput_per_hr"], "wait_s": r["wait_s_per_trip"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["static"], default="static")
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    os.makedirs("results", exist_ok=True)

    if True:
        seeds = range(1, args.seeds + 1)
        jobs = [(st, k, s, b) for st in ("aisle", "bay") for k in (1, 2, 3, 4) for b in (1, 5) for s in seeds]
        with Pool(args.workers) as p:
            rows = p.map(static_job, jobs, chunksize=4)
        with open("results/station_static_raw.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        get = {(r["station"], r["robots"], r["batch"], r["seed"]): r for r in rows}
        print(f"\nStation layout, static: 50 orders, ABC slotting, {args.seeds} paired replications")
        print(f"{'robots':>6} {'batch':>5} | {'aisle':>7} {'bay':>7} | {'bay vs aisle':>18}")
        summary = []
        for b in (1, 5):
            for k in (1, 2, 3, 4):
                a = [get[("aisle", k, b, s)]["throughput"] for s in seeds]
                y = [get[("bay", k, b, s)]["throughput"] for s in seeds]
                d = ci([100 * (yy - aa) / aa for aa, yy in zip(a, y)])
                sig = "*" if abs(d[0]) > d[1] else " "
                print(f"{k:>6} {b:>5} | {statistics.mean(a):7.1f} {statistics.mean(y):7.1f} | {d[0]:+7.1f}% ± {d[1]:4.1f}%{sig}")
                summary.append({"robots": k, "batch": b, "aisle": statistics.mean(a), "bay": statistics.mean(y),
                                "gain_pct": d[0], "gain_ci": d[1]})
        with open("results/station_static_summary.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summary[0])); w.writeheader(); w.writerows(summary)
        print("* = significant at the 5% level")

    print("\nSaved results/station_*.csv\n")


if __name__ == "__main__":
    main()
