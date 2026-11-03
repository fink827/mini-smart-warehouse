# Mini Smart Warehouse

A tabletop autonomous warehouse: small robots follow a taped grid, lift shelf pods and bring them to a packing station, Amazon-style. This repo starts with the software side: a map of the floor, a route planner, an order generator and a SimPy digital twin that predicts how the warehouse will perform before the robots exist.

## What's here

| File | What it does |
| --- | --- |
| `warehouse/config.py` | Every tunable number: layout, robot speeds, demand. Timing values are placeholders until the real robot is measured. |
| `warehouse/layout.py` | The 7 x 6 tape grid as a graph, pod locations, docks, the packing pick point, and shortest-path routing. Loaded robots must stay in the aisles; empty robots can drive under pods. |
| `warehouse/orders.py` | 48 items (3 per pod) with Pareto-style demand, random order generation, and two slotting policies: random and ABC. |
| `warehouse/sim.py` | SimPy model: robots pull pod trips from a queue, travel, lift, carry, wait for the packing station, get picked, return the pod. Reports throughput, travel, completion time, utilization and station waiting. |
| `experiments/slotting.py` | Experiment 1: random vs ABC slotting, 5 paired replications, 95% confidence intervals and a paired t-test. |

## Setup (one time)

You need Python 3.10 or newer.

```bash
cd mini-smart-warehouse
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run it

```bash
python -m warehouse.layout        # print the floor map and an example route
python -m warehouse.sim           # one run of each slotting policy
python -m experiments.slotting    # the full experiment with statistics
python -m experiments.slotting --robots 1 --orders 100
```

## First results (simulated, placeholder timings)

50 orders, 2 robots, 5 paired replications:

| Metric | Random | ABC | Change |
| --- | --- | --- | --- |
| Throughput (orders/hr) | 95.7 | 150.9 | +58% |
| Travel per order (grid cells) | 29.1 | 14.6 | −50% |
| Avg order completion (min) | 16.3 | 10.5 | −35% |

These are predictions from a model with guessed timings. The point of the physical build is to measure the real numbers and compare.

## Next steps

- [ ] Calibrate `config.py` with stopwatch timings from the first real robot
- [ ] Experiment 2: pick routing / task assignment (nearest idle robot vs first-come)
- [ ] Experiment 3: batching (several orders per trip)
- [ ] Experiment 4: 1 vs 2 vs 3 robots, with traffic rules and congestion
- [ ] Log real runs in the same CSV format and plot predicted vs measured
