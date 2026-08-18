import json
from pathlib import Path

# The web app's bin records, one JSON file per category. Separate from
# slots.json: slots says WHERE a bin is, this says what it is.
BINS_DIR = Path(__file__).parent / "data" / "bins"
UNCATEGORIZED = "uncategorized"   # where scans of unknown bins land


def _read(f):
    try:
        return json.loads(f.read_text())
    except (FileNotFoundError, ValueError):
        return []


def find(barcode):
    # which category holds this barcode, or None
    for f in sorted(BINS_DIR.glob("*.json")):
        if any(b.get("barcode") == barcode for b in _read(f)):
            return f.stem
    return None


def set_status(barcode, status):
    """Record a status against whichever category file holds this bin.

    Returns True if a record was found. Status belongs to the barcode the
    scanner actually read, never to whichever card someone happened to tap.
    """
    for f in sorted(BINS_DIR.glob("*.json")):
        bins = _read(f)
        hit = [b for b in bins if b.get("barcode") == barcode]
        if not hit:
            continue
        for b in hit:
            b["status"] = status
        f.write_text(json.dumps(bins, indent=2))
        return True
    return False


def ensure(barcode):
    """Give a scanned barcode a bin record if it does not already have one.

    A bin can be put away without ever being typed into the UI - the label is
    all the machine needs. Without this it would be stored, tracked in
    slots.json, and yet invisible on the main page. Returns the category it was
    added to, or None if it was already known.
    """
    if not barcode or find(barcode):
        return None
    BINS_DIR.mkdir(parents=True, exist_ok=True)
    f = BINS_DIR / (UNCATEGORIZED + ".json")
    bins = _read(f)
    bins.append({"name": barcode,      # no friendly name yet - rename in the UI
                 "barcode": barcode,
                 "subcategory": "",    # blank groups it under "Uncategorized"
                 "status": "in",
                 "request": "no",
                 "store": "no"})
    f.write_text(json.dumps(bins, indent=2))
    return UNCATEGORIZED
