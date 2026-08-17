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
