import json

POSITIONS_FILE = "positions.json"  # taught (x, y) per "col,row" position (8x4 = 32)


def key(col, row):
    return "%d,%d" % (col, row)


def load():
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(positions):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


# Positions are stored once, in carriage 1's frame. Other carriages add their own
# (dx, dy) - the difference between that machine's origin and carriage 1's.
OFFSETS_FILE = "offsets.json"


def load_offsets():
    try:
        with open(OFFSETS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_offsets(offsets):
    with open(OFFSETS_FILE, "w") as f:
        json.dump(offsets, f, indent=2)
