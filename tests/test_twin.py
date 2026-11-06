"""Tests for the traffic-aware twin.   Run:  python -m unittest discover tests"""
import unittest

from warehouse import config as C, fleet as F
from warehouse.layout import PODS, PICK, shortest_path, travel_seconds
from warehouse.orders import get_slotting
from warehouse.sim import run as ideal_run
from warehouse.twin import run_static, run_stream
from warehouse.validate import check


class Planner(unittest.TestCase):
    def test_free_floor_matches_layout_timing(self):
        """Alone on the floor, a trip is never slower than layout.py's route and
        at most as fast as the turn-free lower bound."""
        for pod, node in PODS.items():
            res = F.Reservations()
            legs = [F.Leg(node, False, F.LIFT, "lift"), F.Leg(PICK, True, F.pick_ticks(1), "pick"),
                    F.Leg(node, True, F.LOWER, "lower")]
            steps = F.plan(res, 0, C.DOCKS[0], 0, legs)
            got = steps[-1][4] * F.TICK
            ref = (travel_seconds(shortest_path(C.DOCKS[0], node)) + C.LIFT_SECONDS
                   + travel_seconds(shortest_path(node, PICK, loaded=True))
                   + C.PICK_SECONDS_BASE + C.PICK_SECONDS_PER_UNIT
                   + travel_seconds(shortest_path(PICK, node, loaded=True)) + C.LOWER_SECONDS)
            self.assertLessEqual(got, ref + 1e-9, pod)
            self.assertFalse(any(s[0] == "wait" for s in steps), pod)

    def test_second_robot_yields(self):
        """Two robots sent to the packing station at once must not overlap there."""
        res = F.Reservations()
        a, b = PODS["B4"], PODS["D2"]
        for rid, (start, pod) in enumerate(((C.DOCKS[0], a), (C.DOCKS[1], b))):
            res.park_at(rid, start, 0)
        plans = []
        for rid, (start, pod) in enumerate(((C.DOCKS[0], a), (C.DOCKS[1], b))):
            res.release(rid)
            legs = [F.Leg(pod, False, F.LIFT, "lift"), F.Leg(PICK, True, F.pick_ticks(2), "pick"),
                    F.Leg(pod, True, F.LOWER, "lower")]
            s = F.plan(res, rid, start, 0, legs)
            F.book_plan(res, rid, s)
            plans.append(next(x for x in s if x[0] == "pick"))
        (a0, a1), (b0, b1) = plans[0][3:5], plans[1][3:5]
        self.assertTrue(a1 <= b0 or b1 <= a0)


class PickPoint(unittest.TestCase):
    """The pick point is on the tape between (6,2) and (6,3). Driving along
    column 6 must go through it, and nobody may sit half a cell from a robot
    that is picking. (An earlier version of the planner had a shortcut past it.)"""

    def test_column_six_passes_through_pick_point(self):
        a, b = C.PICK_BETWEEN
        self.assertNotIn(b, dict(F.fleet_neighbors(a)))
        res = F.Reservations()
        steps = F.plan(res, 0, (6, 1), 0, [F.Leg((6, 4), False, 1, "stop")])
        self.assertIn(PICK, [s[2] for s in steps if s[0] == "move"])

    def test_cannot_pass_a_robot_that_is_picking(self):
        res = F.Reservations()
        res.book(1, PICK, 0, 100)            # robot 1 picking for 40 s
        steps = F.plan(res, 0, (6, 0), 0, [F.Leg((6, 5), False, 1, "stop")])
        for node, t0, t1 in F.occupancy(steps):
            if node in (PICK,) + tuple(C.PICK_BETWEEN):
                self.assertGreaterEqual(t0, 100, f"entered {node} at tick {t0} while robot 1 was picking")

    def test_validator_flags_the_shortcut(self):
        from warehouse.layout import neighbors
        g = F.plan.__globals__
        saved_n, saved_c = g["fleet_neighbors"], dict(F.CLEARANCE)
        try:
            g["fleet_neighbors"] = neighbors       # old, buggy planner graph
            F.CLEARANCE.clear()
            F.lower_bound.cache_clear()
            tw = run_static("abc", 1, 50, 4, record=True)["_twin"]
        finally:
            g["fleet_neighbors"] = saved_n
            F.CLEARANCE.update(saved_c)
            F.lower_bound.cache_clear()
        rep = check(tw)
        self.assertGreater(rep["clearance_conflicts"] + sum("illegal move" in p for p in rep["problems"]), 0)


class Twin(unittest.TestCase):
    def test_one_robot_matches_original(self):
        for pol in ("random", "abc"):
            for seed in (1, 2):
                a = ideal_run(pol, seed, 50, 1)["throughput_per_hr"]
                b = run_static(pol, seed, 50, 1)["throughput_per_hr"]
                self.assertAlmostEqual(a, b, delta=0.01 * a)
                self.assertGreaterEqual(b, a - 1e-9)   # turn-aware routing never hurts

    def test_no_physical_violations(self):
        for robots in (2, 4):
            for pol in ("abc", "spread:4"):
                r = run_static(pol, 3, 50, robots, record=True)
                rep = check(r["_twin"])
                self.assertEqual((rep["node_conflicts"], rep["pod_conflicts"], rep["station_conflicts"],
                                  rep["clearance_conflicts"]), (0, 0, 0, 0))
                self.assertEqual(rep["problems"], [])
        r = run_stream("pool", 200, 2, 3, warmup_s=300, measure_s=1800, record=True)
        rep = check(r["_twin"])
        self.assertEqual(rep["node_conflicts"] + rep["pod_conflicts"] + rep["station_conflicts"]
                         + rep["clearance_conflicts"], 0)

    def test_bay_station_is_valid_and_keeps_column_open(self):
        try:
            r = run_static("abc", 2, 50, 4, record=True, station="bay")
            rep = check(r["_twin"])
            self.assertEqual(rep["node_conflicts"] + rep["pod_conflicts"] + rep["station_conflicts"], 0)
            self.assertEqual(rep["problems"], [])
            self.assertEqual(F.CLEARANCE, {})
            a, b = C.PICK_BETWEEN
            self.assertIn(b, dict(F.fleet_neighbors(a)))
        finally:
            F.set_station("aisle")

    def test_every_order_completes(self):
        r = run_static("abc", 4, 60, 3, batch_size=3)
        self.assertEqual(r["orders"], 60)
        r = run_stream("window:60", 120, 3, 2, warmup_s=300, measure_s=1800)
        self.assertEqual(r["orders_finished"], r["orders_measured"])

    def test_release_policies_see_same_orders(self):
        n = {rel: run_stream(rel, 100, 5, 2, warmup_s=300, measure_s=1800)["orders_measured"]
             for rel in ("none", "fixed:3", "window:30", "pool")}
        self.assertEqual(len(set(n.values())), 1)


class Slotting(unittest.TestCase):
    def test_spread_one_is_abc(self):
        self.assertEqual(get_slotting("spread:1", 1), get_slotting("abc", 1))

    def test_spread_uses_every_bin_once(self):
        for w in (2, 3, 4, 16):
            s = get_slotting(f"spread:{w}", 1)
            per_pod = {}
            for pod in s.values():
                per_pod[pod] = per_pod.get(pod, 0) + 1
            self.assertEqual(set(per_pod.values()), {C.SKUS_PER_POD})


if __name__ == "__main__":
    unittest.main()
