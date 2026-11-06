# Mini Smart Warehouse

**A tabletop autonomous fulfillment center: small robots carry shelf pods to a packing station, and a digital twin finds the best way to run it.**

![Robot R1 fetching a pod and delivering it to the packing station](docs/demo.gif)

*Concept animation of the planned system. The physical build is in progress.*

---

## The idea

Large fulfillment centers don't send people walking to shelves. Robots bring the shelves to the people. This project rebuilds that system at 4 × 3 ft scale, then uses industrial engineering to make it faster:

- **Robots** (ESP32, line sensor, lift servo) follow a taped grid, drive under a shelf pod, lift it and carry it to packing.
- **A fleet manager** assigns each order to a robot and plans its route, keeping loaded robots in the aisles.
- **An overhead camera** tracks every robot with ArUco tags, and every event is logged.
- **A SimPy digital twin** predicts throughput before each physical test, then gets checked against what the real floor does. A traffic-aware version plans every robot's route in space and time, so simulated robots can't collide or share a pod, just like real ones.

## First result: slotting

Where you store items matters. **ABC slotting** puts the most-ordered items in the pods closest to packing; the alternative is random placement.

Simulated on the original twin with 50 orders, 2 robots and 5 paired replications (re-checked on the traffic-aware twin in the third result: at 2 robots the gain holds at +47%):

| Metric | Random slotting | ABC slotting | Change |
| --- | ---: | ---: | ---: |
| Throughput (orders/hr) | 95.7 ± 2.2 | 150.9 ± 9.3 | **+58%** |
| Robot travel per order (grid cells) | 29.1 ± 1.3 | 14.6 ± 1.3 | **−50%** |
| Average order completion (min) | 16.3 ± 1.4 | 10.5 ± 1.6 | **−35%** |

± values are 95% confidence intervals. All three differences are statistically significant (paired t-test, 5% level). Robot timings are estimates until the first robot is measured; the physical runs will test whether the predicted gain holds.

## Second result: order batching

If several orders need the same pod, one trip can serve all of them. With ABC slotting, 50 orders, 2 robots and 5 paired replications:

| Batch size | Throughput (orders/hr) | Pod trips per order | Avg order completion (min) |
| ---: | ---: | ---: | ---: |
| 1 (no batching) | 150.9 | 1.5 | 10.5 |
| 2 | 166.7 (+10%) | 1.3 | 9.5 |
| 3 | 179.0 (+19%) | 1.2 | 8.9 |
| 5 | 200.3 (+33%) | 0.9 | 7.9 |

Every change from batch size 1 is statistically significant. Combined with ABC slotting, batches of 5 more than double throughput over the random, unbatched baseline (95.7 → 200.3 orders/hr).

One limit of this test: all 50 orders are released at once, so no order ever waits for batch-mates. Experiment 3 below tests batching with orders arriving over time.

## Third result: the first twin was too optimistic, and it gets worse with more robots

The original twin (`warehouse/sim.py`) treats robots as timers, not objects. An audit (`python -m experiments.audit`) that replays its exact logic found, over 5 runs with 2 robots and ABC slotting:

- 31 times a pod was "held" by two robots at once (equal to 8% of the trip count)
- 35 times two robots occupied the same tape intersection
- 77 times two robots sat on the packing point together

With 4 robots, shared-pod events number 29% of the trip count. None of that can happen on the real floor.

The **traffic-aware twin** (`warehouse/twin.py` + `warehouse/fleet.py`) fixes this. A fleet manager plans each trip through space *and* time with a reservation table:

- one robot per tape intersection per 0.4 s tick
- a robot crossing a tape segment books both ends, so robots can't swap places or tailgate
- one robot per pod
- the pick point sits on the column-6 tape, so traffic along column 6 passes through it, and no robot may stand half a cell from a robot that is picking

Waiting is a legal move, so queueing at packing happens on its own. An independent checker (`python -m warehouse.validate`) rebuilds every path from the plan log and finds zero violations. Mutation tests show it catches robot collisions and the pick-point shortcut described next; a planner without pod locking stops with a deadlock error instead. (A code review caught that an early version of the planner let robots cut past a robot at the pick point. It's fixed, the checker now tests for it, and every number below comes from the fixed version.)

Same orders, same timings, 20 paired replications (`python -m experiments.fidelity`):

| Robots | ABC throughput, original twin | ABC throughput, traffic-aware twin | Original over-predicts by | ABC gain over random (traffic-aware) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 81.2 | 81.3 | −0.2% | +58% ± 5% |
| 2 | 157.3 | 143.6 | +9.5% | +47% ± 5% |
| 3 | 224.7 | 171.4 | +31% | +29% ± 4% |
| 4 | 278.6 | 177.9 | **+57%** | +20% ± 6% |

At 1 robot the two twins agree (the new one is 0.2% faster from turn-aware routing), which validates the timing model. At 2 robots the slotting result holds, at a somewhat smaller size. With more robots the original twin badly overstates both throughput and the value of ABC slotting: a 4th robot adds only about 6–7 orders/hr.

**Why:** two bottlenecks the original twin can't see. ABC puts the three best sellers in the single pod closest to packing (B4), which is needed on 42% of trips, and only one robot can hold it. And every pick blocks column 6. With 4 robots on ABC, robots spend 22% of their time yielding in traffic (mostly near packing) and 20% idle because the pod they need is out.

## Fourth result: with more robots, spread the fast movers out (unless you batch)

A new slotting policy, `spread:W`, deals the fast movers round-robin across the W pods closest to packing instead of filling the closest pod first (`spread:1` is plain ABC). Throughput change vs plain ABC, no batching, 20 paired replications (`python -m experiments.spread`):

| Robots | Best W (point estimate) | Gain at that W (traffic-aware twin) | Original twin's prediction at that W |
| ---: | ---: | ---: | ---: |
| 2 | 1 (plain ABC) | — | — |
| 3 | 2 | +2.8% ± 1.6% | −0.9% |
| 4 | 3 | **+4.9% ± 2.5%** | −2.4% |

The best width is only a point estimate: at 3 robots W = 2 to 4 and at 4 robots W = 3 to 6 are statistically tied, so any modest spread helps. The original twin points the wrong way: it says spreading always hurts, because it can't see the pod bottleneck. With batches of 5, plain ABC is best again at every fleet size, since batching already relieves the hot pod. With orders arriving over time and pooling (below), spreading is also slightly slower than plain ABC (4 robots: +0.05 ± 0.02 min average response at 180 orders/hr). So: pool, and keep ABC.

## Fifth result: batching with orders arriving over time (Experiment 3)

Orders arrive at random (Poisson) over a 4-hour shift; a 30-minute warm-up is dropped; 10 paired replications per point; 2 robots, ABC slotting (`python -m experiments.streaming`). Response time is arrival to last item picked. Seven release policies:

- **No batching**: release each order immediately
- **Batch of k**: hold orders until k have arrived
- **Every T s**: release whatever has arrived every T seconds
- **Pool at the pod**: release immediately; if a trip to the same pod is still waiting for a robot, add the new lines to it

| Policy | Stable up to | 95% of orders within 5 min | Mean response at 120 orders/hr |
| --- | ---: | ---: | ---: |
| No batching | 120/hr | up to 120/hr | 1.75 min |
| Batch of 3 | 150/hr | 80–120/hr | 1.64 min |
| Batch of 5 | 180/hr | 120–150/hr | 2.13 min |
| Every 30 s | 150/hr | up to 120/hr | 1.39 min |
| Every 60 s | 150/hr | up to 150/hr | 1.48 min |
| Every 120 s | 210/hr | up to 150/hr | 1.98 min |
| **Pool at the pod** | **600/hr+** | **up to 270/hr** | **0.91 min** |

Rates tested: 40 to 600 orders/hr. "Stable" means the queue stays bounded in at least 8 of 10 runs; "+" means still stable at the highest rate tested.

- **Pooling is never slower than no batching** (paired difference at 120/hr: −0.84 ± 0.36 min) and holds the 5-minute service level to 2.25× the order rate.
- **Fixed batches show the predicted trade-off.** At 40 orders/hr a batch of 5 makes the average order wait 3.9 min instead of 0.5, because it sits waiting for batch-mates. That's why fixed batches miss the 5-minute target at low volume.
- **Pooling batches automatically:** 1.49 trips per order at 40/hr, falling to 0.48 at 300/hr, as more new lines find a trip already waiting.
- **It is not free at extreme rates.** Pooling never runs out of robots, because a busier floor just means fuller trips, but at 600/hr a trip serves about 8 orders and the average order waits 8.8 min. The twin also assumes a bin never runs out of stock, so very large trips are optimistic.

### Experiment 3 with 4 robots

Same method, 4 robots, 5 paired replications of a 2-hour shift, rates 180 to 420 orders/hr (`python -m experiments.fleet4`):

| Release | Station · slotting | 95% within 5 min up to | Mean response at 300/hr |
| --- | --- | ---: | ---: |
| No batching | aisle · ABC | at no tested rate (unstable from 180/hr) | unstable |
| No batching | aisle · spread over 3 | at no tested rate (unstable from 180/hr) | unstable |
| No batching | bay · ABC | at no tested rate (stable at 180/hr, but 95th percentile 10.5 min) | unstable |
| **Pooling** | aisle · ABC | 420/hr+ | 1.70 min |
| **Pooling** | aisle · spread over 3 | 420/hr+ | 1.77 min |
| **Pooling** | **bay · ABC** | **420/hr+** | **1.53 min** |

Without batching, 4 robots on the aisle layout can't keep up with 180 orders/hr, and with the bay they keep up but miss the 5-minute target (rates below 180 weren't tested): extra robots mostly add congestion at packing. With pooling, 4 robots meet the 5-minute target at every rate tested. The bay station (next result) is the fastest layout with pooling, and its lead grows with load: 0.17 ± 0.05 min faster than the aisle station at 300/hr and 0.37 ± 0.07 min at 420/hr.

## Sixth result: from the third robot on, move packing off the aisle

The pick point sits on the column-6 tape. While a robot is picking, nobody else can use that stretch of column 6 or wait right beside it. The alternative is a **bay**: the pick point one cell east of column 6, reached from (6,2) or (6,3). Every trip takes a short detour, but column 6 stays open. Throughput, 50 orders, ABC slotting, 20 paired replications (`python -m experiments.station`):

| Robots | No batching: bay vs aisle | Batches of 5: bay vs aisle |
| ---: | ---: | ---: |
| 1 | −4.2% ± 0.3% | −5.9% ± 0.2% |
| 2 | −2.6% ± 0.7% | −4.7% ± 0.7% |
| 3 | **+4.8% ± 1.7%** | −1.9% ± 0.8% |
| 4 | **+5.7% ± 0.9%** | **+3.2% ± 1.2%** |

Every difference is statistically significant. With your current 2 robots, keep the station on the aisle. Without batching the bay wins from 3 robots on; with batches of 5, only at 4 robots. With pooling it was tested only at 4 robots, where it is the fastest layout (previous table). Practical takeaway for the build: mount the packing station so it can slide one cell (about 16 cm) east later, when robot 3 (no batching) or robot 4 (batching or pooling) arrives. The tape grid spans about 96 cm of the board's 122 cm width, so check the margin when you lay out the tape.



## Run the simulation

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m warehouse.layout         # floor map and an example route
python -m experiments.slotting     # the slotting experiment
python -m experiments.slotting --robots 3 --orders 200
python -m experiments.batching     # the batching experiment

python -m experiments.audit        # count impossible events in the original twin
python -m warehouse.validate       # physics check of the traffic-aware twin
python -m experiments.fidelity     # experiments 1-2 on both twins, 1-4 robots
python -m experiments.spread       # congestion-aware slotting sweep
python -m experiments.streaming    # experiment 3 (about 25 min on 2 cores)
python -m experiments.streaming --part fleet
python -m experiments.station    # aisle vs bay packing station
python -m experiments.export_replay && python -m experiments.build_page   # docs/twin-lab.html

python -m unittest discover tests  # tests
```

Open `docs/twin-lab.html` in a browser for a side-by-side replay of both twins and charts of every result.

## How the code is organized

| Path | Purpose |
| --- | --- |
| `warehouse/config.py` | Layout, robot timings and demand settings |
| `warehouse/layout.py` | The tape grid as a graph and aisle-aware route planning |
| `warehouse/orders.py` | Pareto-style demand, order generation, slotting policies |
| `warehouse/sim.py` | Original SimPy model (robots as timers; kept for comparison) |
| `warehouse/fleet.py` | Fleet manager: space-time route planning with a reservation table |
| `warehouse/twin.py` | Traffic-aware twin: pod locking, dispatching, static or streaming orders, release policies |
| `warehouse/validate.py` | Independent physics checker for traffic-aware runs |
| `experiments/slotting.py` | Random vs ABC slotting with confidence intervals |
| `experiments/batching.py` | Batch sizes 1, 2, 3 and 5, each compared with no batching |
| `experiments/audit.py` | Finds collisions and shared pods in the original twin |
| `experiments/fidelity.py` | Experiments 1 and 2 on both twins, 1 to 4 robots |
| `experiments/spread.py` | Congestion-aware slotting sweep |
| `experiments/streaming.py` | Experiment 3: release policies under Poisson arrivals |
| `experiments/station.py` | Packing station on the aisle vs in a side bay |
| `experiments/export_replay.py`, `build_page.py` | Build the interactive results page |
| `tests/test_twin.py` | Planner, twin and slotting tests |

## Roadmap

- [x] Floor layout, route planner and order generator
- [x] SimPy digital twin
- [x] Experiment 1: slotting
- [x] Experiment 2: order batching
- [x] Traffic-aware twin: collision-free route planning and pod locking
- [x] Experiment 3: batching with orders arriving over time
- [x] Congestion-aware slotting
- [ ] Build robot 1 and calibrate the twin with measured timings
- [ ] Overhead camera tracking and live dashboard
- [ ] Second robot, running the fleet manager's planned routes
- [ ] Packing bay layout (once a 3rd robot is planned)
- [ ] Physical experiments vs simulation predictions

## Tech

Python · SimPy · ESP32 (Arduino) · MQTT · OpenCV · Grafana

---

An Industrial & Systems Engineering project at the University of Florida.
