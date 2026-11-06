"""Fleet manager: collision-free multi-robot route planning on the tape grid.

The original twin (sim.py) lets robots pass through each other, lets two robots
hold the same pod, and lets robots stack on the packing point. Real robots
can't. This module plans every trip in *space and time* so none of that happens.

How it works (prioritized planning with a reservation table)
------------------------------------------------------------
Time is cut into short ticks. Every node on the grid has a calendar of which
robot occupies it at which tick. When a robot gets a trip, it searches for the
fastest route through (node, heading, time) space that never uses a node another
robot has already booked, then books its own route. Waiting in place is a legal
move, so congestion and queueing at the packing station emerge on their own.

Rules enforced by the booking calendar
  * one robot per node per tick
  * a robot crossing an edge books BOTH end nodes for the whole crossing, so
    two robots can never swap places or tailgate closer than one cell
  * a robot that has finished a trip keeps its node booked (it's parked there)
    until it gets its next trip
  * loaded robots stay in the aisles (same rule as layout.py)
  * the pick point sits ON the tape between (6,2) and (6,3), so a robot driving
    along column 6 must pass through it (no shortcut past a robot that is
    picking), and while a robot is at the pick point nobody may stand half a
    cell away at (6,2) or (6,3): robot bodies are about one cell wide

Timing uses the same model as sim.py: SECONDS_PER_CELL per cell, half a cell
to and from the pick point, TURN_SECONDS for each change between horizontal
and vertical travel. Turns at the start of a leg are free, as in sim.py.
"""
import heapq
from collections import defaultdict
from functools import lru_cache

from . import config as C
from .layout import PICK, POD_SPOTS, neighbors, coords


# ---------------------------------------------------------------- time base
def _pick_tick():
    """Largest tick that divides every timing constant exactly."""
    consts = [C.SECONDS_PER_CELL, C.SECONDS_PER_CELL / 2, C.TURN_SECONDS, C.LIFT_SECONDS,
              C.LOWER_SECONDS, C.PICK_SECONDS_BASE, C.PICK_SECONDS_PER_UNIT]
    for tick in (0.4, 0.2, 0.1, 0.05):
        if all(abs(round(c / tick) * tick - c) < 1e-9 for c in consts):
            return tick
    raise ValueError("Timing constants need to be multiples of 0.05 s")


TICK = _pick_tick()


def to_ticks(seconds):
    return int(round(seconds / TICK))


CELL = to_ticks(C.SECONDS_PER_CELL)
HALF = to_ticks(C.SECONDS_PER_CELL / 2)
TURN = to_ticks(C.TURN_SECONDS)
LIFT = to_ticks(C.LIFT_SECONDS)
LOWER = to_ticks(C.LOWER_SECONDS)


def pick_ticks(units):
    return to_ticks(C.PICK_SECONDS_BASE + C.PICK_SECONDS_PER_UNIT * units)


_PAIR = frozenset(C.PICK_BETWEEN)

# Where the packing station's pick point is:
#   "aisle" (the current design): ON the column-6 tape, halfway between (6,2) and
#           (6,3). Traffic along column 6 must pass through it, and nobody may stand
#           half a cell away while a robot is picking (robot bodies are ~1 cell wide).
#   "bay":  in a side bay one cell east of column 6, reached from (6,2) or (6,3).
#           Column 6 stays open while a robot is picking.
STATION = "aisle"
CLEARANCE = {}          # node -> nodes too close for a second robot at the same time


def set_station(kind):
    """Switch the pick-point layout ('aisle' or 'bay') for all planning that follows."""
    global STATION
    if kind not in ("aisle", "bay"):
        raise ValueError(f"unknown station layout {kind!r}")
    STATION = kind
    CLEARANCE.clear()
    if kind == "aisle":
        CLEARANCE[PICK] = tuple(C.PICK_BETWEEN)
        for n in C.PICK_BETWEEN:
            CLEARANCE[n] = (PICK,)
    lower_bound.cache_clear()


def pick_xy():
    (c1, r1), (c2, r2) = C.PICK_BETWEEN
    x, y = (c1 + c2) / 2, (r1 + r2) / 2
    return (x + 1, y) if STATION == "bay" else (x, y)


def node_xy(node):
    return pick_xy() if node == PICK else coords(node)


def fleet_neighbors(node):
    """Grid neighbours for planning. In the aisle layout the direct (6,2)-(6,3)
    segment is replaced by the two half-segments through the pick point, because
    that is where the pick point physically is. In the bay layout the column
    stays intact and the pick point hangs off it."""
    for m, w in neighbors(node):
        if STATION == "aisle" and frozenset((node, m)) == _PAIR:
            continue
        yield m, w


def edge_ticks(a, b):
    if PICK in (a, b):
        return HALF if STATION == "aisle" else CELL
    return CELL


def axis(a, b):
    (ax, ay), (bx, by) = node_xy(a), node_xy(b)
    return "H" if abs(bx - ax) > 1e-9 else "V"


ALL_NODES = [(c, r) for c in range(C.GRID_COLS) for r in range(C.GRID_ROWS)] + [PICK]


@lru_cache(maxsize=None)
def lower_bound(goal, loaded):
    """Ticks from every node to `goal`, ignoring turns and other robots
    (an admissible heuristic for the search). Loaded robots may not pass
    through pod spots other than the goal."""
    dist = {goal: 0}
    pq = [(0, 0, goal)]
    seq = 0
    while pq:
        d, _, n = heapq.heappop(pq)
        if d > dist[n]:
            continue
        if loaded and n in POD_SPOTS and n != goal:
            continue  # a loaded robot may start here, but not pass through
        for m, _ in fleet_neighbors(n):
            nd = d + edge_ticks(n, m)
            if nd < dist.get(m, 1 << 30):
                dist[m] = nd
                seq += 1
                heapq.heappush(pq, (nd, seq, m))
    return dist


set_station("aisle")


# ---------------------------------------------------------------- bookings
class Reservations:
    """Who occupies which node at which tick."""

    def __init__(self):
        self.res = defaultdict(dict)   # node -> {tick: robot}
        self.park = {}                 # node -> (robot, from_tick)  (open-ended booking)
        self.mine = defaultdict(list)  # robot -> [(node, tick)]
        self.parked_at = {}            # robot -> node

    def free(self, node, t0, t1, rid):
        """True if `node` (and every node within clearance of it) is free for
        robot `rid` during ticks [t0, t1)."""
        if not self._free1(node, t0, t1, rid):
            return False
        for other in CLEARANCE.get(node, ()):
            if not self._free1(other, t0, t1, rid):
                return False
        return True

    def _free1(self, node, t0, t1, rid):
        p = self.park.get(node)
        if p is not None and p[0] != rid and t1 > p[1]:
            return False
        r = self.res.get(node)
        if r:
            for k in range(t0, t1):
                o = r.get(k)
                if o is not None and o != rid:
                    return False
        return True

    def free_forever(self, node, t0, rid):
        """True if nobody else has booked `node` (or a node within clearance) at any tick >= t0."""
        return all(self._free_forever1(n, t0, rid) for n in (node,) + tuple(CLEARANCE.get(node, ())))

    def _free_forever1(self, node, t0, rid):
        p = self.park.get(node)
        if p is not None and p[0] != rid:
            return False
        r = self.res.get(node)
        if r:
            for k, o in r.items():
                if k >= t0 and o != rid:
                    return False
        return True

    def release(self, rid):
        """Drop every booking robot `rid` holds (before it re-plans)."""
        for node, k in self.mine.pop(rid, []):
            r = self.res.get(node)
            if r is not None and r.get(k) == rid:
                del r[k]
        node = self.parked_at.pop(rid, None)
        if node is not None and self.park.get(node, (None,))[0] == rid:
            del self.park[node]

    def book(self, rid, node, t0, t1):
        r = self.res[node]
        for k in range(t0, t1):
            o = r.get(k)
            if o is not None and o != rid:
                raise RuntimeError(f"double booking at {node} tick {k}: {o} vs {rid}")
            r[k] = rid
            self.mine[rid].append((node, k))

    def park_at(self, rid, node, t0):
        p = self.park.get(node)
        if p is not None and p[0] != rid:
            raise RuntimeError(f"robot {rid} cannot park at {node}: robot {p[0]} is parked there")
        self.park[node] = (rid, t0)
        self.parked_at[rid] = node

    def prune(self, before):
        """Forget bookings in the past (keeps the calendar small)."""
        for rid, items in self.mine.items():
            keep = []
            for node, k in items:
                if k < before:
                    r = self.res.get(node)
                    if r is not None and r.get(k) == rid:
                        del r[k]
                else:
                    keep.append((node, k))
            self.mine[rid] = keep


# ---------------------------------------------------------------- planner
class Leg:
    """Drive to `goal` (loaded or empty), then stay there `dwell` ticks."""
    __slots__ = ("goal", "loaded", "dwell", "name")

    def __init__(self, goal, loaded, dwell, name):
        self.goal, self.loaded, self.dwell, self.name = goal, loaded, dwell, name


def plan(res, rid, start, t0, legs, horizon=1500):
    """Fastest collision-free plan through every leg, ending parked at the last goal.

    Returns a list of steps (kind, node_from, node_to, t_start, t_end, leg_index)
    where kind is 'wait', 'turn', 'move' or a leg name ('lift', 'pick', 'lower').
    Returns None if no plan exists within `horizon` ticks.
    """
    n_legs = len(legs)
    rest = [0] * (n_legs + 1)
    for i in range(n_legs - 1, -1, -1):
        rest[i] = legs[i].dwell + (rest[i + 1] if i + 1 < n_legs else 0)
        if i + 1 < n_legs:
            rest[i] += lower_bound(legs[i + 1].goal, legs[i + 1].loaded).get(legs[i].goal, 1 << 20)

    def h(leg, node):
        return lower_bound(legs[leg].goal, legs[leg].loaded).get(node, 1 << 20) + rest[leg]

    t_max = t0 + horizon
    start_state = (0, start, None, t0)
    seen = {start_state: None}
    pq = [(t0 + h(0, start), -t0, 0, start_state)]
    tie = 0
    while pq:
        _, _, _, state = heapq.heappop(pq)
        leg, n, ax, t = state
        L = legs[leg]
        succ = []

        if n == L.goal and res.free(n, t, t + L.dwell, rid):
            if leg == n_legs - 1:
                if res.free_forever(n, t + L.dwell, rid):
                    # done: rebuild the plan
                    steps = [(L.name, n, n, t, t + L.dwell, leg)]
                    s = state
                    while seen[s] is not None:
                        s, step = seen[s]
                        steps.append(step)
                    return steps[::-1]
            else:
                succ.append(((leg + 1, n, None, t + L.dwell), (L.name, n, n, t, t + L.dwell, leg)))

        if t + 1 <= t_max and res.free(n, t, t + 1, rid):
            succ.append(((leg, n, ax, t + 1), ("wait", n, n, t, t + 1, leg)))

        for m, _ in fleet_neighbors(n):
            if L.loaded and m in POD_SPOTS and m != L.goal:
                continue
            nax = axis(n, m)
            turn = TURN if (ax is not None and nax != ax) else 0
            c = edge_ticks(n, m)
            te = t + turn + c
            if te > t_max:
                continue
            if res.free(n, t, te, rid) and res.free(m, t + turn, te, rid):
                succ.append(((leg, m, nax, te), ("move", n, m, t, te, leg, turn)))

        for ns, step in succ:
            if ns in seen:
                continue
            seen[ns] = (state, step)
            tie += 1
            nleg, nn, _, nt = ns
            heapq.heappush(pq, (nt + h(nleg, nn), -nt, tie, ns))
    return None


def book_plan(res, rid, steps):
    """Commit a plan's bookings and park the robot at its final node."""
    for step in steps:
        kind, a, b, ts, te = step[:5]
        if kind == "move":
            turn = step[6]
            res.book(rid, a, ts, te)
            res.book(rid, b, ts + turn, te)
        else:
            res.book(rid, a, ts, te)
    last = steps[-1]
    res.park_at(rid, last[2], last[4])


def occupancy(steps):
    """(node, t0, t1) intervals a plan occupies — rebuilt straight from the steps,
    independent of the booking table, so validation can cross-check it."""
    out = []
    for step in steps:
        kind, a, b, ts, te = step[:5]
        if kind == "move":
            out.append((a, ts, te))
            out.append((b, ts + step[6], te))
        else:
            out.append((a, ts, te))
    return out
