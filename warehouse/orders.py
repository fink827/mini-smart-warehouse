"""Items (SKUs), customer orders, and slotting policies (which pod holds which item)."""
import random
from . import config as C
from .layout import PODS, PICK, shortest_path, travel_seconds


def make_skus():
    """One SKU per bin. Demand follows a Zipf (Pareto-like) curve: the top few
    items get most of the orders, like real retail."""
    n = len(PODS) * C.SKUS_PER_POD
    skus = [f"S{i + 1:02d}" for i in range(n)]
    weights = {s: 1.0 / ((i + 1) ** C.ZIPF_S) for i, s in enumerate(skus)}
    return skus, weights


def _pick(rng, dist):
    keys, probs = zip(*dist.items())
    return rng.choices(keys, weights=probs, k=1)[0]


def generate_orders(n, seed):
    """Return a list of orders. Each order is a list of (sku, units)."""
    rng = random.Random(seed)
    skus, weights = make_skus()
    orders = []
    for _ in range(n):
        n_lines = _pick(rng, C.LINES_PER_ORDER)
        chosen = set()
        while len(chosen) < n_lines:
            chosen.add(rng.choices(skus, weights=[weights[s] for s in skus], k=1)[0])
        orders.append([(s, _pick(rng, C.UNITS_PER_LINE)) for s in sorted(chosen)])
    return orders


def pods_by_distance():
    """Pods sorted from closest to farthest from packing (loaded round trip)."""
    def cost(pod):
        node = PODS[pod]
        return travel_seconds(shortest_path(node, PICK, loaded=True)) + \
               travel_seconds(shortest_path(PICK, node, loaded=True))
    return sorted(PODS, key=lambda p: (cost(p), p))


def slot_random(seed):
    """Items placed in pods at random."""
    rng = random.Random(seed + 10_000)
    skus, _ = make_skus()
    bins = [p for p in PODS for _ in range(C.SKUS_PER_POD)]
    rng.shuffle(bins)
    return dict(zip(skus, bins))


def slot_abc(seed=None):
    """ABC slotting: the most-ordered items go in the pods closest to packing."""
    skus, weights = make_skus()
    ranked = sorted(skus, key=lambda s: -weights[s])
    bins = [p for p in pods_by_distance() for _ in range(C.SKUS_PER_POD)]
    return dict(zip(ranked, bins))


def slot_spread(width):
    """Congestion-aware ABC: like ABC, but the fastest movers are dealt out
    round-robin across the `width` closest pods instead of filling the closest
    pod first. width=1 is plain ABC; width=16 spreads over every pod.

    Why: only one robot can hold a pod at a time. ABC puts the top 3 items in
    one pod, so that pod becomes a single-server bottleneck once you have
    several robots. Spreading trades a little travel for parallelism."""
    def policy(seed=None):
        skus, weights = make_skus()
        ranked = sorted(skus, key=lambda s: -weights[s])
        pods = pods_by_distance()
        bins = []
        for g in range(0, len(pods), width):
            group = pods[g:g + width]
            for _level in range(C.SKUS_PER_POD):
                bins.extend(group)
        return dict(zip(ranked, bins))
    return policy


POLICIES = {"random": slot_random, "abc": slot_abc}


def get_slotting(name, seed):
    """'random', 'abc', or 'spread:W' (congestion-aware ABC over the W closest pods)."""
    if name.startswith("spread:"):
        return slot_spread(int(name.split(":")[1]))(seed)
    return POLICIES[name](seed)
