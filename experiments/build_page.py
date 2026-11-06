"""Build docs/twin-lab.html (the interactive results page) from results/.

Run after the experiments:
    python -m experiments.fidelity
    python -m experiments.spread
    python -m experiments.streaming
    python -m experiments.fleet4
    python -m experiments.station
    python -m experiments.export_replay
    python -m experiments.build_page
"""
import csv, datetime, json, os, re, statistics

from warehouse import fleet as F
from warehouse.twin import run_static

NAMES = {"none": "No batching", "fixed:3": "Batch of 3", "fixed:5": "Batch of 5", "window:30": "Every 30 s",
         "window:60": "Every 60 s", "window:120": "Every 120 s", "pool": "Pool at the pod"}
ORDER = ["none", "fixed:3", "fixed:5", "window:30", "window:60", "window:120", "pool"]
CHART = ["none", "fixed:5", "window:60", "pool"]          # policies drawn in the response-time chart
CHART_COLORS = ["--r3", "--r0", "--r2", "--tape"]


def rows(path):
    with open(path) as f:
        out = []
        for r in csv.DictReader(f):
            for k, v in r.items():
                try:
                    r[k] = int(v)
                except (ValueError, TypeError):
                    try:
                        r[k] = float(v)
                    except (ValueError, TypeError):
                        r[k] = {"True": True, "False": False}.get(v, v)
            out.append(r)
        return out


def time_budget(robots=4, seeds=range(1, 11)):
    """Share of robot-time by activity, traffic-aware twin, ABC, 50 orders."""
    tot = dict(robot=0, move=0, lift=0, pick=0, wait=0, blocked=0, empty=0)
    for s in seeds:
        r = run_static("abc", s, 50, robots, 1, record=True)
        tw = r["_twin"]
        tot["robot"] += robots * r["makespan_min"] * 60
        tot["blocked"] += tw.idle_blocked_s
        tot["empty"] += tw.idle_empty_s
        for trips in tw.plans.values():
            for tr in trips:
                for st in tr["steps"]:
                    d = (st[4] - st[3]) * F.TICK
                    key = {"move": "move", "pick": "pick", "lift": "lift", "lower": "lift", "wait": "wait"}[st[0]]
                    tot[key] += d
    pct = lambda v: 100 * v / tot["robot"]
    return [{"label": "driving", "v": pct(tot["move"])},
            {"label": "lifting / lowering", "v": pct(tot["lift"])},
            {"label": "at packing", "v": pct(tot["pick"])},
            {"label": "yielding in traffic", "v": pct(tot["wait"]), "bad": True},
            {"label": "idle, pod taken", "v": pct(tot["blocked"]), "bad": True},
            {"label": "idle, no work left", "v": pct(tot["empty"])}]


def slotting_step(spread, fleet4):
    rs = [r for r in spread if r["model"] == "traffic" and r["robots"] == 4 and r["batch"] == 1]
    best = max(rs, key=lambda r: r["throughput"])
    txt = (f"<li><b>Keep plain ABC slotting if you batch or pool.</b> Spreading the top sellers across a few pods "
           f"only pays off when trips are never combined ({best['gain_vs_abc_pct']:+.1f}% throughput at 4 robots, over "
           f"{best['width']} pods). With batches of 5, plain ABC wins at every fleet size")
    p = [r for r in fleet4 if r["release"] == "pool" and r["slotting"] == "spread:3" and r.get("diff_vs_base") not in (None, "")]
    if p:
        lo = min(p, key=lambda r: r["rate_per_hr"])
        txt += (f", and with pooling and 4 robots, spreading was slightly slower "
                f"({lo['diff_vs_base']:+.2f} ± {lo['diff_ci']:.2f} min at {lo['rate_per_hr']}/hr)")
    return txt + ".</li>"


def station_step(st):
    if not st:
        return ""
    g = {(r["robots"], r["batch"]): r for r in st}
    def first_win(b):
        return min((k for k in sorted({r["robots"] for r in st}) if g[(k, b)]["gain_pct"] - g[(k, b)]["gain_ci"] > 0),
                   default=None)
    flip, flip5 = first_win(1), first_win(5)
    if flip is None:
        return ""
    lose = g[(2, 1)]
    win = g[(max(r["robots"] for r in st), 1)]
    return (f"<li><b>Leave room for a packing bay.</b> With 2 robots the station on the aisle is better "
            f"(the bay costs {abs(lose['gain_pct']):.1f}% throughput from the longer detour). From {flip} robots on, "
            f"the bay wins without batching (+{win['gain_pct']:.1f}% at {win['robots']} robots) because column 6 stays open; "
            f"with batches of 5 it wins only from {flip5} robots, and with pooling it was the fastest layout at 4 robots "
            f"(the only fleet size tested with pooling). Build the station so it can slide one cell east when robot "
            f"{flip} (no batching) or robot {flip5} (batching or pooling) arrives.</li>")


def fleet4_meta():
    raw = rows("results/fleet4_raw.csv") if os.path.exists("results/fleet4_raw.csv") else []
    return len({r["seed"] for r in raw}), 2


def fleet4_table(rows_, n_seeds=None, hours=None):
    """4 robots, streaming orders: capacity and response by layout, slotting and release."""
    if not rows_:
        return ""
    label = {("aisle", "abc"): "aisle · ABC", ("aisle", "spread:3"): "aisle · spread 3", ("bay", "abc"): "bay · ABC"}
    rates = sorted({r["rate_per_hr"] for r in rows_})
    show = 300 if 300 in rates else rates[len(rates) // 2]
    out = []
    for rel in ("none", "pool"):
        group = []
        for key, name in label.items():
            rs = [r for r in rows_ if (r["station"], r["slotting"], r["release"]) == (*key, rel)]
            at = next(r for r in rs if r["rate_per_hr"] == show)
            group.append((name, rs[0]["sla_cap"], at["resp_mean_min"] if at["stable"] else None, max(rates)))
        best_cap = max(g[1] for g in group)
        best_t = min((g[2] for g in group if g[2] is not None), default=None)
        for name, cap, t, top in group:
            cap_txt = (f"{cap}/hr" + ("+" if cap == top else "")) if cap else "no tested rate"
            t_txt = f"{t:.2f}" if t is not None else "—"
            out.append(f'<tr><td>{"None" if rel == "none" else "Pool"} · {name}</td>'
                       f'<td class="{"best" if cap == best_cap and len({g[1] for g in group}) > 1 else ""}">{cap_txt}</td>'
                       f'<td class="{"best" if t is not None and t == best_t else ""}">{t_txt}</td></tr>')
    return ('<figure><h3>4 robots, orders arriving over time</h3><div class="tablewrap"><table><thead><tr>'
            f'<th>Batching · layout</th><th>95% in 5 min</th><th>Avg min, {show}/hr</th></tr></thead><tbody>'
            + "".join(out) + '</tbody></table></div><figcaption>None = no batching; Pool = pool at the pod. “—” means unstable at that rate. Poisson arrivals, '
            f'{len({r["rate_per_hr"] for r in rows_})} rates from {min(rates)} to {max(rates)}/hr, {n_seeds} paired replications '
            f'of a {hours:g}-hour shift each. “+” means it still met the target at the highest rate tested.</figcaption></figure>')


def main():
    fid = rows("results/fidelity_summary.csv")
    spread = rows("results/spread_summary.csv")
    stream = rows("results/streaming_policies_summary.csv")
    fleet4 = rows("results/fleet4_summary.csv") if os.path.exists("results/fleet4_summary.csv") else []
    raw_fid = rows("results/fidelity_raw.csv")
    replay = json.load(open("results/replay.json"))
    station = rows("results/station_static_summary.csv") if os.path.exists("results/station_static_summary.csv") else []

    def over(k):
        i = [r for r in raw_fid if r["model"] == "ideal" and r["policy"] == "abc" and r["batch"] == 1 and r["robots"] == k]
        t = {r["seed"]: r for r in raw_fid if r["model"] == "traffic" and r["policy"] == "abc" and r["batch"] == 1 and r["robots"] == k}
        return statistics.mean(100 * (r["throughput"] - t[r["seed"]]["throughput"]) / t[r["seed"]]["throughput"] for r in i)

    # spread callouts
    callouts = []
    for k in (2, 3, 4):
        rs = [r for r in spread if r["model"] == "traffic" and r["robots"] == k and r["batch"] == 1]
        best = max(rs, key=lambda r: r["throughput"])
        if best["width"] == 1:
            txt = "Plain ABC is best. Keep the top sellers in the closest pod."
            head = "W = 1"
        else:
            txt = f"Spreading over {best['width']} pods: {best['gain_vs_abc_pct']:+.1f}% throughput (±{best['gain_ci']:.1f})."
            head = f"W = {best['width']}"
        callouts.append(f'<div class="callout"><span class="eyebrow">{k} robots</span><b>{head}</b><p class="muted">{txt}</p></div>')
    best4 = max((r for r in spread if r["model"] == "traffic" and r["robots"] == 4 and r["batch"] == 1), key=lambda r: r["throughput"])
    ideal4 = [r for r in spread if r["model"] == "ideal" and r["robots"] == 4 and r["batch"] == 1 and r["width"] == best4["width"]][0]
    callouts.append(f'<div class="callout"><span class="eyebrow">original twin, 4 robots</span><b>{ideal4["gain_vs_abc_pct"]:+.1f}% at W = {best4["width"]}</b>'
                    f'<p class="muted">It would have told you spreading hurts, the opposite of what solid robots do.</p></div>')

    fleet_html = ""

    pool = next(r for r in stream if r["release"] == "pool")
    none = next(r for r in stream if r["release"] == "none")
    steps = [
        f"<li><b>Build traffic control into the fleet manager from day one.</b> With 2 robots the old twin was about {over(2):.0f}% optimistic; by robot 4 it was {over(4):.0f}% optimistic. The booking-table planner in <code>warehouse/fleet.py</code> is small enough to run on the laptop that sends MQTT commands, and its plans are exactly what the robots should execute.</li>",
        f"<li><b>Implement pool batching, not fixed batches.</b> On 2 robots it raises the rate at which 95% of orders finish within 5 minutes from {none['sla_cap']}/hr to {pool['sla_cap']}/hr, it is never slower than no batching, and nobody waits for batch-mates. It is a few lines in the fleet manager: before dispatching a trip, add any new order lines for the same pod.</li>",
        slotting_step(spread, fleet4),
        station_step(station),
        "<li><b>Calibrate before trusting any of these numbers.</b> Time robot 1 per cell, per turn, and per lift with the overhead camera, put the values in <code>config.py</code>, and rerun. The pod-bottleneck effect depends on pick and lift times, so its size will move.</li>",
        "<li><b>Run the physical test that separates the models.</b> 4 robots, ABC, 50 orders: the original twin predicts about "
        + f"{next(r for r in fid if r['robots'] == 4 and r['model'] == 'ideal')['abc']:.0f}"
        + " orders/hr and the traffic-aware twin about "
        + f"{next(r for r in fid if r['robots'] == 4 and r['model'] == 'traffic')['abc']:.0f}"
        + ". That gap is large enough to see on a tabletop.</li>",
    ]

    prow = sorted((r for r in stream if r["release"] == "pool"), key=lambda r: r["rate_per_hr"])
    ptop = prow[-1]
    pool_note = (f"Pooling never runs out of robots: a busier floor just means fuller trips. That doesn't make it free. "
                 f"At {ptop['rate_per_hr']} orders/hr each pod trip serves about {1 / ptop['trips_per_order']:.0f} orders and the "
                 f"average order waits {ptop['resp_mean_min']:.1f} minutes. The useful limit is the service level: 95% of orders "
                 f"within 5 minutes holds up to {pool['sla_cap']}/hr with pooling versus {none['sla_cap']}/hr without batching. "
                 f"The twin also assumes a bin never runs out of stock, so very large trips are optimistic.")
    rates = sorted({r["rate_per_hr"] for r in stream})
    # compare waits at the busiest rate where every policy is still stable
    mid = max(r for r in rates if all(any(x["release"] == p and x["rate_per_hr"] == r and x["stable_frac"] >= 0.8 for x in stream)
                                       for p in ORDER))
    data = {
        "replay": replay, "fidelity": fid, "spread": spread, "streamPolicies": stream,
        "policyOrder": ORDER, "policyNames": NAMES, "chartPolicies": CHART, "chartColors": CHART_COLORS, "midRate": mid,
        "timeBudget": time_budget(), "station": station,
    }
    seeds_fid = len({r["seed"] for r in raw_fid})
    s_raw = rows("results/streaming_policies_raw.csv")
    html = open("docs/twin_lab_template.html").read()
    rep = {
        "/*DATA*/": json.dumps(data, separators=(",", ":")),
        "/*DATE*/": datetime.date.today().strftime("%B %Y"),
        "/*F2*/": f"{over(2):.0f}", "/*F4*/": f"{over(4):.0f}",
        "/*SEEDS*/": str(seeds_fid), "/*S_SEEDS*/": str(len({r["seed"] for r in s_raw})),
        "/*SPREAD_CALLOUTS*/": "".join(callouts), "/*FLEET_BLOCK*/": fleet_html,
        "/*BUILD_STEPS*/": "".join(steps), "/*POOL_NOTE*/": pool_note,
        "/*STATION_TABLE*/": fleet4_table(fleet4, *fleet4_meta()), "/*VAL_TRIPS*/": os.environ.get("VAL_TRIPS", "2,605"),
    }
    for k, v in rep.items():
        html = html.replace(k, v)
    left = re.findall(r"/\*[A-Z_0-9]+\*/", html)
    assert not left, f"unfilled placeholders: {left}"
    with open("docs/twin-lab.html", "w") as f:
        f.write(html)
    print(f"wrote docs/twin-lab.html ({os.path.getsize('docs/twin-lab.html') // 1024} KB)")


if __name__ == "__main__":
    main()
