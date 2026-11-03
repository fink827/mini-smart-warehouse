"""The warehouse floor as a graph: nodes are tape intersections, edges are tape.

Rules (same as the physical robots will follow):
  * An empty robot can drive under pods, so it may pass through any node.
  * A robot carrying a pod must stay in the aisles: it may not enter a node
    where another pod is parked (except its own pod's home spot).
"""
import heapq
from . import config as C

PICK = "P"   # special node for the packing station's pick point


def pod_nodes():
    """Return {pod_name: (col, row)}, e.g. {'A1': (1, 1), ...}."""
    pods = {}
    for letter, (c0, r0) in C.POD_BLOCKS.items():
        k = 1
        for dr in (0, 1):
            for dc in (0, 1):
                pods[f"{letter}{k}"] = (c0 + dc, r0 + dr)
                k += 1
    return pods


PODS = pod_nodes()
POD_SPOTS = set(PODS.values())


def coords(node):
    """(x, y) position in grid units, used for drawing and turn counting."""
    if node == PICK:
        (c1, r1), (c2, r2) = C.PICK_BETWEEN
        return ((c1 + c2) / 2, (r1 + r2) / 2)
    return node


def neighbors(node):
    """Yield (neighbor, cost_in_cells)."""
    if node == PICK:
        for n in C.PICK_BETWEEN:
            yield n, 0.5
        return
    c, r = node
    for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nc, nr = c + dc, r + dr
        if 0 <= nc < C.GRID_COLS and 0 <= nr < C.GRID_ROWS:
            yield (nc, nr), 1.0
    if node in C.PICK_BETWEEN:
        yield PICK, 0.5


def shortest_path(start, goal, loaded=False):
    """Dijkstra on the tape grid. Returns a list of nodes from start to goal."""
    allowed_extra = {start, goal}
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, 0, start)]
    tie = 0
    while pq:
        d, _, node = heapq.heappop(pq)
        if node == goal:
            break
        if d > dist.get(node, float("inf")):
            continue
        for nb, w in neighbors(node):
            if loaded and nb in POD_SPOTS and nb not in allowed_extra:
                continue
            nd = d + w
            if nd < dist.get(nb, float("inf")):
                dist[nb] = nd
                prev[nb] = node
                tie += 1
                heapq.heappush(pq, (nd, tie, nb))
    if goal not in dist:
        raise ValueError(f"No route from {start} to {goal} (loaded={loaded})")
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    return path[::-1]


def path_cells(path):
    total = 0.0
    for a, b in zip(path, path[1:]):
        total += 0.5 if PICK in (a, b) else 1.0
    return total


def path_turns(path):
    turns = 0
    dirs = []
    for a, b in zip(path, path[1:]):
        (ax, ay), (bx, by) = coords(a), coords(b)
        dirs.append((round(bx - ax, 3) != 0, round(by - ay, 3) != 0))
    for d1, d2 in zip(dirs, dirs[1:]):
        if d1 != d2:
            turns += 1
    return turns


def travel_seconds(path):
    return path_cells(path) * C.SECONDS_PER_CELL + path_turns(path) * C.TURN_SECONDS


def render_map(route=None):
    """ASCII picture of the floor. Pods show their name, the route shows as *."""
    route_nodes = set(route or [])
    label = {node: name for name, node in PODS.items()}
    for i, d in enumerate(C.DOCKS):
        label.setdefault(d, f"H{i + 1}")
    lines = []
    for r in range(C.GRID_ROWS):
        row = []
        for c in range(C.GRID_COLS):
            n = (c, r)
            if n in label:
                cell = label[n]
            elif n in route_nodes:
                cell = "**"
            else:
                cell = "+ "
            if n in route_nodes and n in label:
                cell = cell.lower()
            row.append(f"{cell:>3}")
        tail = ""
        if (c, r) == C.PICK_BETWEEN[0]:
            tail = "  ]"
        if (c, r) == C.PICK_BETWEEN[1]:
            tail = "  ] PACK" + (" *" if PICK in route_nodes else "")
        lines.append(" ".join(row) + tail)
    return "\n".join(lines)


if __name__ == "__main__":
    print("Mini Smart Warehouse floor (7 x 6 tape grid)\n")
    print(render_map())
    start, pod = C.DOCKS[0], PODS["B3"]
    to_pod = shortest_path(start, pod, loaded=False)
    to_pack = shortest_path(pod, PICK, loaded=True)
    print("\nExample: robot at dock 1 fetches pod B3 and brings it to packing\n")
    print(render_map(to_pod + to_pack))
    print(f"\n  To pod:     {path_cells(to_pod):4.1f} cells, {path_turns(to_pod)} turns, "
          f"{travel_seconds(to_pod):5.1f} s")
    print(f"  To packing: {path_cells(to_pack):4.1f} cells, {path_turns(to_pack)} turns, "
          f"{travel_seconds(to_pack):5.1f} s  (loaded, aisles only)")
    print("\n  Lowercase names are pods the route passes under or picks up.")
