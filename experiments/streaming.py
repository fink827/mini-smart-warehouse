"""Experiment 3: batching when orders arrive over time.

Experiment 2 released all 50 orders at once, so no order ever waited for its
batch-mates. Here orders arrive as a Poisson stream and each release policy
has to trade fewer trips against holding orders back.

Policies (see warehouse/twin.py):
    none       release each order on arrival
    fixed:k    hold until k orders have arrived, release together
    window:T   release whatever has arrived every T seconds
    pool       release on arrival; lines for a pod that already has a trip
               waiting for a robot join that trip (nobody is held back)

For every (policy, arrival rate) we run paired replications (same arrivals,
same orders across policies), drop a 30-minute warm-up, measure 4 hours,
then drain. Two capacity numbers come out per policy:
    stable capacity  highest rate where the queue doesn't grow without bound
    SLA capacity     highest rate where 95% of orders finish within --sla minutes

Run from the project folder (takes a while; uses every CPU core):
    python -m experiments.streaming                     # 2 robots, all policies
    python -m experiments.streaming --seeds 5 --hours 2 # quicker look
    (4 robots, slotting and station layout: python -m experiments.fleet4)
"""
import argparse, csv, os, statistics
from multiprocessing import Pool

from warehouse.twin import run_stream
from experiments.slotting import ci

POLICIES_A = ["none", "fixed:3", "fixed:5", "window:30", "window:60", "window:120", "pool"]


def one(job):
    rel, rate, seed, robots, slot, hours = job
    r = run_stream(rel, rate, seed, robots, slot, warmup_s=1800, measure_s=hours * 3600)
    r["slotting"] = slot
    return r


def safe_ci(xs):
    """Mean and 95% half-width, or NaN when any run has no response times
    (a run stopped early as unstable)."""
    if any(x != x for x in xs):
        return float("nan"), float("nan")
    return ci(xs)


def load_raw(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            for k, v in r.items():
                if v in ("True", "False"):
                    r[k] = v == "True"
                    continue
                try:
                    r[k] = int(v)
                except ValueError:
                    try:
                        r[k] = float(v)
                    except ValueError:
                        pass
            out.append(r)
    return out


def capacity(rates, ok):
    """Highest rate on the grid with ok=True, provided every lower rate is ok too."""
    cap = 0
    for rate in rates:
        if ok[rate]:
            cap = rate
        else:
            break
    return cap


def ok_range(rates, ok):
    """(low, high) of the first unbroken run of passing rates, or (0, 0) if none pass.
    Fixed batches can miss a response-time target at LOW rates (orders wait for
    batch-mates) and pass at medium ones, so a single 'max rate' would mislead."""
    lo = hi = 0
    for rate in rates:
        if ok[rate]:
            lo = lo or rate
            hi = rate
        elif lo:
            break
    return lo, hi


def summarize(rows, configs, rates, seeds, sla):
    out = []
    for cfg in configs:
        rel, robots, slot = cfg
        stable_ok, sla_ok = {}, {}
        for rate in rates:
            rs = [r for r in rows if (r["release"], r["robots"], r["slotting"], r["rate_per_hr"]) == (rel, robots, slot, rate)]
            if not rs:
                stable_ok[rate] = sla_ok[rate] = stable_ok.get(max([x for x in stable_ok] or [0]), False)
                continue
            frac_stable = sum(r["stable"] for r in rs) / len(rs)
            m, h = safe_ci([r["resp_mean_min"] for r in rs])
            p95 = statistics.mean(r["resp_p95_min"] for r in rs)
            stable_ok[rate] = frac_stable >= 0.8
            sla_ok[rate] = stable_ok[rate] and p95 <= sla
            out.append({"release": rel, "robots": robots, "slotting": slot, "rate_per_hr": rate,
                        "stable_frac": frac_stable, "resp_mean_min": m, "resp_mean_ci": h, "resp_p95_min": p95,
                        "hold_mean_min": statistics.mean(r["hold_mean_min"] for r in rs),
                        "throughput_per_hr": statistics.mean(r["throughput_per_hr"] for r in rs),
                        "trips_per_order": statistics.mean(r["trips_per_order"] for r in rs),
                        "robot_util_pct": statistics.mean(r["robot_util_pct"] for r in rs),
                        "station_util_pct": statistics.mean(r["station_util_pct"] for r in rs),
                        "wip_mean": statistics.mean(r["wip_mean"] for r in rs),
                        "stable_cap": None, "sla_lo": None, "sla_cap": None})
        for row in out:
            if (row["release"], row["robots"], row["slotting"]) == cfg:
                row["stable_cap"] = capacity(rates, stable_ok)
                row["sla_lo"], row["sla_cap"] = ok_range(rates, sla_ok)
    return out


def print_table(summary, configs, rates, sla):
    for cfg in configs:
        rel, robots, slot = cfg
        rows = [r for r in summary if (r["release"], r["robots"], r["slotting"]) == cfg]
        sla_txt = (f"{rows[0]['sla_lo']}-{rows[0]['sla_cap']}/hr" if rows[0]["sla_cap"] else "at no tested rate")
        print(f"\n{rel:<10} | {robots} robots | {slot} slotting | stable to {rows[0]['stable_cap']}/hr, "
              f"p95 <= {sla:g} min at {sla_txt}")
        print(f"  {'rate':>5} {'mean resp (min)':>18} {'p95':>7} {'held':>6} {'trips/order':>11} {'robot util':>10} {'stable':>7}")
        for r in rows:
            mean = f"{r['resp_mean_min']:6.2f} ± {r['resp_mean_ci']:4.2f}" if r["stable_frac"] >= 0.8 else "  unstable    "
            print(f"  {r['rate_per_hr']:>5} {mean:>18} {r['resp_p95_min']:7.2f} {r['hold_mean_min']:6.2f} "
                  f"{r['trips_per_order']:11.2f} {r['robot_util_pct']:9.1f}% {100*r['stable_frac']:6.0f}%")


def paired_vs(rows, base, other, robots, slot, rates):
    """Paired difference in mean response time (other - base) at each rate both handle."""
    res = []
    for rate in rates:
        a = {r["seed"]: r for r in rows if (r["release"], r["robots"], r["slotting"], r["rate_per_hr"]) == (base, robots, slot, rate)}
        b = {r["seed"]: r for r in rows if (r["release"], r["robots"], r["slotting"], r["rate_per_hr"]) == (other, robots, slot, rate)}
        if not a or not b or a.keys() != b.keys():
            continue
        if not all(x["stable"] for x in list(a.values()) + list(b.values())):
            continue
        d = ci([b[s]["resp_mean_min"] - a[s]["resp_mean_min"] for s in a])
        res.append((rate, d))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["policies"], default="policies",
                    help="2-robot policy comparison (for 4 robots see experiments/fleet4.py)")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--hours", type=float, default=4)
    ap.add_argument("--sla", type=float, default=5.0, help="minutes; SLA capacity uses the 95th percentile")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--rates", type=int, nargs="+", help="override the rate grid")
    ap.add_argument("--only", nargs="+", help="run only these release policies")
    ap.add_argument("--append", action="store_true",
                    help="add to existing results/streaming_<part>_raw.csv instead of starting over")
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))

    rates = [40, 80, 120, 150, 180, 210, 240, 270, 300, 360, 420, 480, 540, 600]
    configs = [(p, 2, "abc") for p in POLICIES_A]

    if args.rates:
        rates = sorted(args.rates)
    if args.only:
        configs = [c for c in configs if c[0] in args.only]
    old = load_raw(f"results/streaming_{args.part}_raw.csv") if args.append else []
    have = {(r["release"], r["rate_per_hr"], r["seed"], r["robots"], r["slotting"]) for r in old}
    jobs = [(rel, rate, s, robots, slot, args.hours) for (rel, robots, slot) in configs for rate in rates for s in seeds
            if (rel, rate, s, robots, slot) not in have]
    jobs.sort(key=lambda j: -j[1])  # long (high-rate) runs first for better load balance
    with Pool(args.workers) as pool:
        rows = old + pool.map(one, jobs, chunksize=1)
    if args.append:
        configs = sorted({(r["release"], r["robots"], r["slotting"]) for r in rows},
                         key=lambda c: (c[1], c[2], POLICIES_A.index(c[0]) if c[0] in POLICIES_A else 99))
        rates = sorted({r["rate_per_hr"] for r in rows})

    os.makedirs("results", exist_ok=True)
    with open(f"results/streaming_{args.part}_raw.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    summary = summarize(rows, configs, rates, seeds, args.sla)
    print(f"\nExperiment 3 ({args.part}): Poisson arrivals, {len(seeds)} paired replications, "
          f"{args.hours:g} h measured after 30 min warm-up")
    print_table(summary, configs, rates, args.sla)

    print("\nPaired difference in mean response time vs 'none' (negative = faster), 95% CI:")
    for (rel, robots, slot) in configs:
        if rel == "none":
            continue
        diffs = paired_vs(rows, "none", rel, robots, slot, rates)
        txt = ", ".join(f"{rate}/hr {d[0]:+.2f}±{d[1]:.2f}" for rate, d in diffs)
        print(f"  {rel:<10} {robots}r {slot:<9} {txt}")

    tag = args.part
    with open(f"results/streaming_{tag}_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0])); w.writeheader(); w.writerows(summary)
    print(f"\nSaved results/streaming_{tag}_summary.csv and results/streaming_{tag}_raw.csv\n")


if __name__ == "__main__":
    main()
