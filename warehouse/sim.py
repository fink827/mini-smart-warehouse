"""Discrete-event simulation of the warehouse (the digital twin), built on SimPy.

Each order becomes one "trip" per pod it needs. A robot takes the next trip,
drives (empty) to the pod, lifts it, carries it through the aisles to packing,
waits if another robot is already at the station, gets picked, returns the pod
and lowers it. The packing station serves one robot at a time.
"""
import statistics
import simpy

from . import config as C
from .layout import PODS, PICK, shortest_path, travel_seconds, path_cells
from .orders import generate_orders, POLICIES


def run(policy="abc", seed=1, n_orders=C.N_ORDERS, n_robots=C.N_ROBOTS, batch_size=1):
    env = simpy.Environment()
    station = simpy.Resource(env, capacity=1)
    tasks = simpy.Store(env)
    orders = generate_orders(n_orders, seed)
    slotting = POLICIES[policy](seed)

    log = {"trips": [], "busy": [0.0] * n_robots, "order_done": {}}
    remaining = {oid: 0 for oid in range(len(orders))}
    all_done = env.event()

    # Group orders into batches (in arrival order). Within a batch, every pod
    # that any order needs is fetched once and serves all of those orders.
    for b0 in range(0, len(orders), batch_size):
        by_pod = {}
        for oid in range(b0, min(b0 + batch_size, len(orders))):
            for sku, units in orders[oid]:
                by_pod.setdefault(slotting[sku], []).append((oid, sku, units))
        for pod, plines in by_pod.items():
            served = sorted({oid for oid, _, _ in plines})
            for oid in served:
                remaining[oid] += 1
            tasks.put({"orders": served, "pod": pod, "lines": [(s, u) for _, s, u in plines]})

    def robot(rid):
        here = C.DOCKS[rid % len(C.DOCKS)]
        while True:
            task = yield tasks.get()
            start = env.now
            pod_node = PODS[task["pod"]]
            p1 = shortest_path(here, pod_node, loaded=False)
            p2 = shortest_path(pod_node, PICK, loaded=True)
            p3 = shortest_path(PICK, pod_node, loaded=True)
            yield env.timeout(travel_seconds(p1))
            yield env.timeout(C.LIFT_SECONDS)
            yield env.timeout(travel_seconds(p2))
            arrive = env.now
            with station.request() as req:
                yield req
                wait = env.now - arrive
                units = sum(u for _, u in task["lines"])
                yield env.timeout(C.PICK_SECONDS_BASE + C.PICK_SECONDS_PER_UNIT * units)
            yield env.timeout(travel_seconds(p3))
            yield env.timeout(C.LOWER_SECONDS)
            here = pod_node
            log["busy"][rid] += env.now - start - wait
            log["trips"].append({"robot": rid, "cells": path_cells(p1) + path_cells(p2) + path_cells(p3),
                                 "wait": wait, "duration": env.now - start})
            for oid in task["orders"]:
                remaining[oid] -= 1
                if remaining[oid] == 0:
                    log["order_done"][oid] = env.now
            if len(log["order_done"]) == len(orders) and not all_done.triggered:
                all_done.succeed()

    for rid in range(n_robots):
        env.process(robot(rid))
    env.run(until=all_done)

    makespan = env.now
    trips = log["trips"]
    cells_per_order = sum(t["cells"] for t in trips) / len(orders)
    return {
        "policy": policy,
        "seed": seed,
        "batch_size": batch_size,
        "orders": len(orders),
        "trips": len(trips),
        "trips_per_order": len(trips) / len(orders),
        "makespan_min": makespan / 60,
        "throughput_per_hr": len(orders) / makespan * 3600,
        "avg_completion_min": statistics.mean(log["order_done"].values()) / 60,
        "cells_per_order": cells_per_order,
        "utilization_pct": 100 * statistics.mean(b / makespan for b in log["busy"]),
        "station_wait_s_per_trip": statistics.mean(t["wait"] for t in trips),
    }


if __name__ == "__main__":
    for pol in ("random", "abc"):
        r = run(pol, seed=1)
        print(f"{pol:>7}: {r['throughput_per_hr']:5.1f} orders/hr, "
              f"{r['cells_per_order']:5.1f} cells/order, {r['utilization_pct']:4.1f}% utilization, "
              f"{r['station_wait_s_per_trip']:4.1f} s wait/trip")
