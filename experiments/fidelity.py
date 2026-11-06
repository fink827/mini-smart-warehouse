"""Do Experiments 1 and 2 survive realistic robot physics?

The original twin (sim.py) lets robots drive through each other, lets two
robots hold the same pod, and stacks robots on the packing point. The
traffic-aware twin (twin.py) forbids all of that. This script reruns the
slotting and batching comparisons on BOTH twins with the same orders
(common random numbers) and reports how much each conclusion changes.

Run from the project folder:
    python -m experiments.fidelity
    python -m experiments.fidelity --seeds 30 --robots 1 2 3 4
"""
import argparse, csv, os, statistics
from multiprocessing import Pool

from warehouse import sim, twin
from experiments.slotting import ci


def one(args):
    model, policy, seed, robots, batch = args
    if model == "ideal":
        r = sim.run(policy, seed, 50, robots, batch)
    else:
        r = twin.run_static(policy, seed, 50, robots, batch)
    return {"model": model, "policy": policy, "seed": seed, "robots": robots, "batch": batch,
            "throughput": r["throughput_per_hr"], "cells": r["cells_per_order"],
            "completion": r["avg_completion_min"], "util": r["utilization_pct"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--robots", type=int, nargs="+", default=[1, 2, 3, 4])
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    seeds = range(1, args.seeds + 1)
    jobs = [(m, p, s, k, b) for m in ("ideal", "traffic") for k in args.robots for s in seeds
            for p, b in (("random", 1), ("abc", 1), ("abc", 5))]
    with Pool(args.workers) as pool:
        rows = pool.map(one, jobs)
    get = {(r["model"], r["policy"], r["batch"], r["robots"], r["seed"]): r for r in rows}

    def series(model, pol, b, k, key):
        return [get[(model, pol, b, k, s)][key] for s in seeds]

    print(f"\nFidelity check: {args.seeds} paired replications, 50 orders each\n")
    print(f"{'robots':>6} {'model':>8} | {'random':>7} {'ABC':>7} {'ABC+b5':>7} | "
          f"{'ABC gain vs random':>22} | {'batch-5 gain vs ABC':>22}")
    print("-" * 96)
    out = []
    for k in args.robots:
        for m in ("ideal", "traffic"):
            rnd, abc, b5 = (series(m, "random", 1, k, "throughput"), series(m, "abc", 1, k, "throughput"),
                            series(m, "abc", 5, k, "throughput"))
            g1 = ci([100 * (y - x) / x for x, y in zip(rnd, abc)])
            g2 = ci([100 * (y - x) / x for x, y in zip(abc, b5)])
            print(f"{k:>6} {m:>8} | {statistics.mean(rnd):7.1f} {statistics.mean(abc):7.1f} "
                  f"{statistics.mean(b5):7.1f} | {g1[0]:+8.1f}% ± {g1[1]:4.1f}%      | {g2[0]:+8.1f}% ± {g2[1]:4.1f}%")
            out.append({"robots": k, "model": m, "random": statistics.mean(rnd), "abc": statistics.mean(abc),
                        "abc_b5": statistics.mean(b5), "abc_gain_pct": g1[0], "abc_gain_ci": g1[1],
                        "b5_gain_pct": g2[0], "b5_gain_ci": g2[1]})
        # how far off was the ideal twin for each configuration?
        for pol, b, label in (("random", 1, "random"), ("abc", 1, "ABC"), ("abc", 5, "ABC+b5")):
            err = ci([100 * (i - t) / t for i, t in zip(series("ideal", pol, b, k, "throughput"),
                                                        series("traffic", pol, b, k, "throughput"))])
            print(f"{'':>6} {'':>8}   ideal twin over-predicts {label:<7} throughput by {err[0]:+5.1f}% ± {err[1]:.1f}%")
        print()
    print("Throughput in orders/hr. Gains are paired per seed; ± is the 95% CI.")
    os.makedirs("results", exist_ok=True)
    with open("results/fidelity_raw.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    with open("results/fidelity_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    print("Saved results/fidelity_raw.csv and results/fidelity_summary.csv\n")


if __name__ == "__main__":
    main()
