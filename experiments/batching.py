"""Experiment 2: order batching.

Question: if several orders need the same pod, should one robot trip serve
all of them? Fewer trips should raise throughput, but an order in a batch is
only done when every pod its batch needs has been picked, so individual
orders may finish later.

Design: ABC slotting (the winner of Experiment 1) for every run. Batch sizes
1 (no batching), 2, 3 and 5. Each seed is one replication with the same
order set for every batch size, so each batch size is compared with batch
size 1 using a paired test.

Run from the project folder:
    python -m experiments.batching
    python -m experiments.batching --robots 1 --sizes 1 2 4 8
"""
import argparse, csv, os, statistics

from warehouse import config as C
from warehouse.sim import run
from experiments.slotting import ci

METRICS = [("throughput_per_hr", "Throughput", "orders/hr"),
           ("trips_per_order", "Pod trips per order", "trips"),
           ("cells_per_order", "Travel per order", "grid cells"),
           ("avg_completion_min", "Avg order completion", "min")]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robots", type=int, default=C.N_ROBOTS)
    ap.add_argument("--orders", type=int, default=C.N_ORDERS)
    ap.add_argument("--seeds", type=int, default=len(C.SEEDS))
    ap.add_argument("--sizes", type=int, nargs="+", default=[1, 2, 3, 5])
    ap.add_argument("--policy", default="abc", choices=["abc", "random"])
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))
    sizes = sorted(set(args.sizes))
    if sizes[0] != 1: sizes = [1] + sizes

    rows = [run(args.policy, s, args.orders, args.robots, b) for b in sizes for s in seeds]
    by = {b: [r for r in rows if r["batch_size"] == b] for b in sizes}

    print(f"\nBatching experiment: {args.orders} orders, {args.robots} robot(s), "
          f"{args.policy.upper()} slotting, {len(seeds)} paired replications\n")
    for key, name, unit in METRICS:
        print(f"{name} ({unit})")
        base = [r[key] for r in by[1]]
        for b in sizes:
            vals = [r[key] for r in by[b]]
            m, h = ci(vals)
            line = f"  batch {b}:  {m:7.1f} ± {h:4.1f}"
            if b != 1:
                md, hd = ci([y - x for x, y in zip(base, vals)])
                pct = 100 * md / statistics.mean(base)
                sig = "significant" if abs(md) > hd else "not significant"
                line += f"   vs batch 1: {md:+6.1f} ± {hd:4.1f}  ({pct:+.0f}%, {sig})"
            print(line)
        print()
    print("± values are 95% confidence intervals. A change is significant when its\n"
          "interval does not include zero (a paired t-test at the 5% level).")

    os.makedirs("results", exist_ok=True)
    path = os.path.join("results", f"batching_{args.policy}_{args.robots}robots_{args.orders}orders.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\nRaw results saved to {path}\n")

if __name__ == "__main__":
    main()
