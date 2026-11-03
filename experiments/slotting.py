"""Experiment 1: random slotting vs ABC slotting.

Design: each seed is one replication. Both policies see the SAME order set for
a given seed (common random numbers), so we compare them with a paired test.

Run from the project folder:
    python -m experiments.slotting
    python -m experiments.slotting --robots 1 --orders 100
"""
import argparse, csv, math, os, statistics

from warehouse import config as C
from warehouse.sim import run

# Two-sided 95% t critical values by degrees of freedom
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
       9: 2.262, 10: 2.228, 14: 2.145, 19: 2.093, 29: 2.045}

def t_crit(df):
    return T95.get(df) or T95[min(T95, key=lambda k: abs(k - df))]

def ci(xs):
    m = statistics.mean(xs)
    if len(xs) < 2: return m, 0.0
    return m, t_crit(len(xs) - 1) * statistics.stdev(xs) / math.sqrt(len(xs))

METRICS = [("throughput_per_hr", "Throughput", "orders/hr"),
           ("cells_per_order", "Travel per order", "grid cells"),
           ("avg_completion_min", "Avg order completion", "min"),
           ("utilization_pct", "Robot utilization", "%"),
           ("station_wait_s_per_trip", "Wait at packing", "s/trip")]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robots", type=int, default=C.N_ROBOTS)
    ap.add_argument("--orders", type=int, default=C.N_ORDERS)
    ap.add_argument("--seeds", type=int, default=len(C.SEEDS))
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))

    rows = [run(p, s, args.orders, args.robots) for s in seeds for p in ("random", "abc")]
    res = {p: [r for r in rows if r["policy"] == p] for p in ("random", "abc")}

    print(f"\nSlotting experiment: {args.orders} orders, {args.robots} robot(s), {len(seeds)} paired replications\n")
    print(f"{'Metric':<32}{'Random':>18}{'ABC':>18}{'Difference (ABC - Random)':>42}")
    print("-" * 110)
    for key, name, unit in METRICS:
        a, b = [r[key] for r in res["random"]], [r[key] for r in res["abc"]]
        (ma, ha), (mb, hb) = ci(a), ci(b)
        diffs = [y - x for x, y in zip(a, b)]
        md, hd = ci(diffs)
        pct = 100 * md / ma if ma else 0.0
        sig = "significant" if abs(md) > hd else "not significant"
        print(f"{name + ' (' + unit + ')':<32}{f'{ma:.1f} ± {ha:.1f}':>18}{f'{mb:.1f} ± {hb:.1f}':>18}"
              f"{f'{md:+.1f} ± {hd:.1f}  ({pct:+.0f}%, {sig})':>42}")
    print("\n± values are 95% confidence intervals. A difference is significant when its\n"
          "interval does not include zero (a paired t-test at the 5% level).")

    os.makedirs("results", exist_ok=True)
    path = os.path.join("results", f"slotting_{args.robots}robots_{args.orders}orders.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\nRaw results saved to {path}\n")

if __name__ == "__main__":
    main()
