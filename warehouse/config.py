"""All tunable numbers in one place.

Timing values are placeholders. Once a real robot drives, measure them
(stopwatch + the overhead camera) and replace these. That calibration step
is what turns this model into a real digital twin.
"""

# ---- Floor layout (matches the 4 x 3 ft board in the plan) ----
GRID_COLS = 7          # tape columns, numbered 0..6 left to right
GRID_ROWS = 6          # tape rows, numbered 0..5 top to bottom

# Four 2x2 blocks of shelf pods. Key = block letter, value = top-left node.
POD_BLOCKS = {"A": (1, 1), "B": (4, 1), "C": (1, 3), "D": (4, 3)}

# Robot home docks (one per robot), bottom-left of the board
DOCKS = [(1, 5), (0, 5)]

# The packing station's pick point sits on column 6, halfway between rows 2 and 3
PICK_BETWEEN = ((6, 2), (6, 3))

# ---- Robot timing (seconds) — CALIBRATE THESE ----
SECONDS_PER_CELL = 1.6     # one grid cell (~16 cm) at ~10 cm/s line following
TURN_SECONDS = 1.2         # extra time for each 90-degree turn
LIFT_SECONDS = 2.0         # servo raises a pod
LOWER_SECONDS = 2.0        # servo lowers a pod
PICK_SECONDS_BASE = 4.0    # picker reaches into the pod
PICK_SECONDS_PER_UNIT = 2.0

# ---- Demand ----
SKUS_PER_POD = 3           # bins per pod
ZIPF_S = 1.1               # demand skew: higher = a few items get most orders
LINES_PER_ORDER = {1: 0.50, 2: 0.35, 3: 0.15}   # probability of 1, 2, 3 distinct items
UNITS_PER_LINE = {1: 0.7, 2: 0.3}

# ---- Experiment defaults ----
N_ORDERS = 50
N_ROBOTS = 2
SEEDS = [1, 2, 3, 4, 5]
