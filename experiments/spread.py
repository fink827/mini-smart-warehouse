"""Congestion-aware slotting: how widely should fast movers be spread?

ABC slotting packs the top items into the single closest pod. With several
robots, that pod becomes a bottleneck (only one robot can hold it). The
'spread:W' policy deals the fastest movers round-robin over the W closest pods.
W=1 is plain ABC.

Runs every width on both twins. The ideal twin has no congestion, so it can't
see the benefit of spreading; the traffic-aware twin can.

Run from the project folder:
    python -m experiments.spread
    python -m experiments.spread --robots 2 3 4 --widths 1 2 3 4 6 8 16 --seeds 20
"""
import argparse, csv, os, statistics
from multiprocessing import Pool

from warehouse import sim, twin
from experiments.slotting import ci


def one(args):
    model, width, seed, robots, batch = args
    pol = f"spread:{width}"
    r = sim.run(pol, seed, 50, robots, batch) if model == "ideal" else twin.run_static(pol, seed, 50, robots, batch)
    return {"model": model, "width": width, "seed": seed, "robots": robots, "batch": batch,
            "throughput": r["throughput_per_hr"], "cells": r["cells_per_order"],
            "completion": r["avg_completion_min"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--robots", type=int, nargs="+", default=[2, 3, 4])
    ap.add_argument("--widths", type=int, nargs="+", default=[1, 2, 3, 4, 6, 8, 16])
    ap.add_argument("--batches", type=int, nargs="+", default=[1, 5])
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    seeds = range(1, args.seeds + 1)
    jobs = [(m, w, s, k, b) for m in ("ideal", "traffic") for k in args.robots for b in args.batches
            for w in args.widths for s in seeds]
    with Pool(args.workers) as pool:
        rows = pool.map(one, jobs, chunksize=8)
    get = {(r["model"], r["width"], r["robots"], r["batch"], r["seed"]): r for r in rows}

    summary = []
    for b in args.batches:
        for k in args.robots:
            print(f"\n{k} robots, batch size {b}: throughput (orders/hr) vs ABC (W=1), paired 95% CI")
            print(f"  {'W':>3} | {'ideal twin':>26} | {'traffic-aware twin':>26} | travel/order")
            best = {}
            for w in args.widths:
                cells = []
                line = f"  {w:>3} |"
                for m in ("ideal", "traffic"):
                    base = [get[(m, 1, k, b, s)]["throughput"] for s in seeds]
                    vals = [get[(m, w, k, b, s)]["throughput"] for s in seeds]
                    d = ci([100 * (y - x) / x for x, y in zip(base, vals)])
                    mean = statistics.mean(vals)
                    sig = "*" if abs(d[0]) > d[1] else " "
                    line += f" {mean:6.1f} ({d[0]:+5.1f}% ±{d[1]:4.1f}){sig}  |"
                    summary.append({"model": m, "robots": k, "batch": b, "width": w, "throughput": mean,
                                    "gain_vs_abc_pct": d[0], "gain_ci": d[1],
                                    "cells": statistics.mean(get[(m, w, k, b, s)]["cells"] for s in seeds),
                                    "completion": statistics.mean(get[(m, w, k, b, s)]["completion"] for s in seeds)})
                    if m == "traffic":
                        best[w] = mean
                line += f" {statistics.mean(get[('traffic', w, k, b, s)]['cells'] for s in seeds):5.1f}"
                print(line)
            wbest = max(best, key=best.get)
            print(f"  -> traffic-aware best width: W={wbest}")
    print("\n* = significantly different from ABC (W=1) at the 5% level")
    os.makedirs("results", exist_ok=True)
    with open("results/spread_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0])); w.writeheader(); w.writerows(summary)
    with open("results/spread_raw.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print("Saved results/spread_summary.csv and results/spread_raw.csv\n")


if __name__ == "__main__":
    main()
