import json

SLOTS_FILE = "slots.json"  # 8x4 positions, 3 deep = 96 slots, keyed "col,row,depth"


def key(col, row, depth):
    return "%d,%d,%d" % (col, row, depth)


def load():
    # barcodes per slot; missing file -> empty (slots get added when assigned)
    try:
        with open(SLOTS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(slots):
    with open(SLOTS_FILE, "w") as f:
        json.dump(slots, f, indent=2)


def find(barcode):
    # reverse lookup: barcode -> "col,row,depth" key, or None
    for k, v in load().items():
        if v == barcode:
            return k
    return None


DEPTHS = (1, 2, 3)


def deepest_open(lane, ignore=()):
    """Deepest depth in `lane` a bin can actually be driven to right now.

    Not simply the deepest empty slot: the car has to drive PAST everything
    shallower, so a free depth 3 behind an occupied depth 1 is unreachable.
    This walks out from the front and stops at the first bin in the way.

    `ignore` lists depths whose recorded bin is not physically there - the one
    on a carriage's forks mid-dig, which slots.json still has filed at its old
    slot. None means even depth 1 is blocked.
    """
    taken = load()
    best = None
    for d in DEPTHS:
        if d not in ignore and ("%s,%d" % (lane, d)) in taken:
            break                    # this one blocks anything behind it
        best = d
    return best


def next_free(lanes):
    # First empty slot, DEEPEST FIRST within each lane - the back has to fill
    # before the front or the front bin blocks the ones behind it.
    # lanes: "col,row" keys to consider, in fill order.
    taken = load()
    for lane in lanes:
        for depth in (3, 2, 1):
            k = "%s,%d" % (lane, depth)
            if k not in taken:
                return k
    return None
