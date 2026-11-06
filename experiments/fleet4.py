"""Experiment 3 with 4 robots: does slotting or the station layout matter
once orders arrive over time?

Factors (4 robots, Poisson arrivals, paired replications):
    layout + slotting: aisle station + ABC, aisle station + spread:3, bay station + ABC
    release:           none, pool
    rate:              180-420 orders/hr

Each finished run is appended to results/fleet4_raw.csv straight away, so an
interrupted run keeps its work; rerunning resumes where it stopped.

Run from the project folder:
    python -m experiments.fleet4
    python -m experiments.fleet4 --seeds 10 --hours 4     # bigger, slower
"""
import argparse, csv, os, statistics
from multiprocessing import Pool

from warehouse.twin import run_stream
from experiments.slotting import ci
from experiments.streaming import load_raw

LAYOUTS = [("aisle", "abc"), ("aisle", "spread:3"), ("bay", "abc")]
RELEASES = ["none", "pool"]
RATES = [180, 240, 300, 360, 420]
PATH = "results/fleet4_raw.csv"
FIELDS = ["station", "slotting", "release", "rate_per_hr", "seed", "resp_mean_min", "resp_p95_min",
          "stable", "aborted", "trips_per_order", "throughput_per_hr", "wait_s_per_trip"]


def job(a):
    station, slot, rel, rate, seed, hours = a
    r = run_stream(rel, rate, seed, 4, slot, measure_s=hours * 3600, station=station)
    r["slotting"] = slot
    return {k: r[k] for k in FIELDS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--hours", type=float, default=2)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    seeds = range(1, args.seeds + 1)
    os.makedirs("results", exist_ok=True)

    done = load_raw(PATH)
    have = {(r["station"], r["slotting"], r["release"], r["rate_per_hr"], r["seed"]) for r in done}
    jobs = [(st, sl, rel, rate, s, args.hours) for st, sl in LAYOUTS for rel in RELEASES for rate in RATES for s in seeds
            if (st, sl, rel, rate, s) not in have]
    jobs.sort(key=lambda j: (j[2] != "pool", -j[3]))   # heavy pooled high-rate runs first
    new_file = not os.path.exists(PATH)
    with open(PATH, "a", newline="") as f, Pool(args.workers) as pool:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for row in pool.imap_unordered(job, jobs):
            w.writerow(row)
            f.flush()
    rows = load_raw(PATH)

    def cell(st, sl, rel, rate):
        return {r["seed"]: r for r in rows if (r["station"], r["slotting"], r["release"], r["rate_per_hr"]) == (st, sl, rel, rate)
                and r["seed"] in seeds}

    print(f"\n4 robots, streaming orders, {args.seeds} paired replications, {args.hours:g} h after 30 min warm-up\n")
    summary = []
    for rel in RELEASES:
        for st, sl in LAYOUTS:
            stable_to = sla_to = 0
            broken = False
            line = []
            for rate in RATES:
                rs = list(cell(st, sl, rel, rate).values())
                ok = sum(r["stable"] for r in rs) >= 0.8 * len(rs)
                mean = statistics.mean(r["resp_mean_min"] for r in rs) if ok else float("nan")
                p95 = statistics.mean(r["resp_p95_min"] for r in rs) if ok else float("nan")
                if ok and not broken:
                    stable_to = rate
                    if p95 <= 5:
                        sla_to = rate
                else:
                    broken = True
                line.append(f"{rate}:{mean:5.2f}" if ok else f"{rate}:  -- ")
                summary.append({"station": st, "slotting": sl, "release": rel, "rate_per_hr": rate, "stable": ok,
                                "resp_mean_min": mean, "resp_p95_min": p95})
            for row in summary:
                if (row["station"], row["slotting"], row["release"]) == (st, sl, rel):
                    row["stable_cap"], row["sla_cap"] = stable_to, sla_to
            print(f"  {rel:<5} {st:<5} {sl:<9} stable to {stable_to}/hr, p95<=5 min to {sla_to}/hr | mean response: "
                  + " ".join(line))

    print("\nPaired difference in mean response time vs aisle + ABC (negative = faster), 95% CI:")
    for rel in RELEASES:
        for st, sl in LAYOUTS[1:]:
            parts = []
            for rate in RATES:
                a, b = cell("aisle", "abc", rel, rate), cell(st, sl, rel, rate)
                if not a or a.keys() != b.keys() or not all(x["stable"] for x in list(a.values()) + list(b.values())):
                    continue
                d = ci([b[s]["resp_mean_min"] - a[s]["resp_mean_min"] for s in a])
                parts.append(f"{rate}/hr {d[0]:+.2f}±{d[1]:.2f}")
                for row in summary:
                    if (row["station"], row["slotting"], row["release"], row["rate_per_hr"]) == (st, sl, rel, rate):
                        row["diff_vs_base"], row["diff_ci"] = d
            print(f"  {rel:<5} {st:<5} {sl:<9} " + ", ".join(parts))
    keys = sorted({k for r in summary for k in r}, key=lambda k: list(summary[0]).index(k) if k in summary[0] else 99)
    with open("results/fleet4_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(summary)
    print("\nSaved results/fleet4_raw.csv and results/fleet4_summary.csv\n")


if __name__ == "__main__":
    main()
