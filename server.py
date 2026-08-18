"""Web server for grbl-pi - one process owns the serial ports, any number of
browsers connect. Speaks the same contract as the old mq-brs node backend so
the React app works unchanged: {topic, payload} JSON in over the /ws WebSocket,
flat {barcode, status} broadcasts out, plus the bin/category REST API backed by
data/bins/*.json. Serves the built frontend from web/.

Run: python3 server.py     (then open http://<pi>:8000 from any device)
"""
import asyncio
import json
import re
import time
from pathlib import Path

from aiohttp import web, WSMsgType

import carpark
import grbl
import inventory
import main
import positions
import slots

PORT = 8000
ROOT = Path(__file__).parent
WEB = ROOT / "web"            # React build output (npm run build, copied here)
BINS_DIR = ROOT / "data" / "bins"
PARENTS_FILE = ROOT / "data" / "parents.json"

# A category name becomes a FILENAME, so this is a strict whitelist, not a
# blacklist. Letters, digits, spaces, underscore and hyphen; it must start with
# a letter or digit. No dot is allowed, so ".." can never form and there is no
# path traversal to defend against; no slash or backslash for the same reason.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")


def clean_name(value):
    """Trimmed category name if it is acceptable, else None.

    Surrounding whitespace is trimmed rather than rejected - "M5 Bolts " and
    "M5 Bolts" are the same folder to anyone typing it, and a filename with a
    trailing space is its own kind of misery.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if NAME_RE.match(value) else None


# ---------- inventory files (same layout the node backend used) ----------

def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def barcode_index(skip=None):
    """barcode -> category holding it, across every category file.

    The barcode is the bin's identity: slots.json maps it to a physical slot,
    status broadcasts key on it, and a retrieve looks a bin up by it. Two
    records sharing one barcode makes all three ambiguous, so writes are
    checked against this. `skip` leaves out the file being rewritten.
    """
    idx = {}
    for f in BINS_DIR.glob("*.json"):
        if skip is not None and f == skip:
            continue
        for b in read_json(f, []):
            code = b.get("barcode")
            if code and code not in idx:
                idx[code] = f.stem
    return idx


def set_bin_status(barcode, status):
    # persist a status change wherever that barcode lives
    for f in BINS_DIR.glob("*.json"):
        bins = read_json(f, [])
        hit = [b for b in bins if b.get("barcode") == barcode]
        if hit:
            for b in hit:
                b["status"] = status
            write_json(f, bins)
            return True
    return False


# ---------- broadcast to every connected page ----------

def broadcast(app, msg):
    data = json.dumps(msg)
    for ws in list(app["clients"]):
        asyncio.ensure_future(ws.send_str(data))


def make_emit(app):
    # thread-safe broadcast for callbacks running in the serial executor thread
    loop = asyncio.get_event_loop()

    def emit(msg):
        loop.call_soon_threadsafe(broadcast, app, msg)
    return emit


def crane_log(emit, line):
    # progress/status lines land in the DevPage crane console
    emit({"topic": "machine/crane/response", "payload": {"response": line}})


def update_status(app, emit, barcode, status):
    set_bin_status(barcode, status)
    emit({"barcode": barcode, "status": status})   # flat - what BinList expects


# ---------- machine commands (serialized - one thing at a time) ----------

def pick_carriage(app):
    return next((n for n, c in app["carriages"].items() if c), None)


def retrieve_blocking(app, emit, n, barcode):
    # runs in the executor thread; all serial I/O lives here. Depth-aware, so it
    # fetches the bin at the recorded slot rather than whatever is at the front.
    return main.retrieve_bin(app["carriages"], n,
                             lambda l: crane_log(emit, l), barcode)


def store_blocking(app, emit, n):
    # on_status only BROADCASTS. store_bin writes the record itself, keyed on
    # the barcode the scanner actually read, so a store from the shell persists
    # the same way and the two cannot disagree.
    return main.store_bin(app["carriages"], n, lambda l: crane_log(emit, l),
                          on_status=lambda code, status: emit({"barcode": code,
                                                               "status": status}))


async def do_retrieve(app, barcode):
    emit = make_emit(app)
    if slots.find(barcode) is None:
        crane_log(emit, "no slot assigned for barcode %s - store it first" % barcode)
        return
    n = pick_carriage(app)
    if n is None:
        crane_log(emit, "no carriage connected")
        return
    if app["lock"].locked():
        crane_log(emit, "machine busy - retrieve %s ignored" % barcode)
        return
    async with app["lock"]:
        update_status(app, emit, barcode, "out-pending")
        ok = await asyncio.get_event_loop().run_in_executor(
            None, retrieve_blocking, app, emit, n, barcode)
        update_status(app, emit, barcode, "out" if ok else "in")


async def do_store(app, barcode):
    # `barcode` is which card was tapped. It is deliberately NOT used to decide
    # anything: the bin actually sitting at staging is whatever it is, and only
    # the scanner can say. Kept in the signature because the page sends it.
    emit = make_emit(app)
    n = pick_carriage(app)
    if n is None:
        crane_log(emit, "no carriage connected")
        return
    if app["lock"].locked():
        crane_log(emit, "machine busy - store ignored")
        return
    async with app["lock"]:
        # NO OPTIMISTIC STATUS HERE. The page cannot know which bin is actually
        # sitting at staging - only the scanner does. Marking the tapped card
        # in-pending meant storing bin B while bin A claimed to be in stock.
        # store_bin sets it against the code it reads and broadcasts through
        # on_status, so the card that updates is the bin that moved.
        await asyncio.get_event_loop().run_in_executor(
            None, store_blocking, app, emit, n)


def cnc_blocking(app, n, command):
    cnc = app["carriages"][n]["cnc"]
    cnc.write((command + "\n").encode())
    return cnc.readline().decode(errors="replace").strip()


def carpark_blocking(app, n, command, window=1.5):
    # raw bytes to carpark, collect whatever comes back for a short window
    uart = app["carriages"][n]["uart"]
    uart.reset_input_buffer()
    uart.write((command + "\n").encode() if command[:1] in "><" else command.encode())
    lines, deadline = [], time.time() + window
    while time.time() < deadline:
        line = uart.readline().decode(errors="replace").strip()
        if line:
            lines.append(line)
    return lines


async def do_debug(app, command):
    # /debug page: same command language as the shell, one shared active carriage
    emit = make_emit(app)

    def say(line):
        emit({"topic": "debug/response", "payload": {"response": str(line)}})

    command = command.strip()
    if not command:
        return
    n = app["debug_active"] or pick_carriage(app)
    if n is None:
        say("no carriage connected")
        return
    if app["lock"].locked():
        say("machine busy")
        return
    async with app["lock"]:
        app["debug_active"] = await asyncio.get_event_loop().run_in_executor(
            None, main.dispatch, app["carriages"], n, command, say)


async def do_console(app, topic, command):
    # DevPage consoles: cnc = raw gcode, crane = raw carpark command
    emit = make_emit(app)
    n = pick_carriage(app)
    if n is None or not command:
        return
    if app["lock"].locked():
        emit({"topic": topic.replace("command", "response"),
              "payload": {"response": "machine busy"}})
        return
    async with app["lock"]:
        loop = asyncio.get_event_loop()
        if "cnc" in topic:
            reply = await loop.run_in_executor(None, cnc_blocking, app, n, command)
            emit({"topic": "machine/cnc/response", "payload": {"response": reply}})
        else:
            lines = await loop.run_in_executor(None, carpark_blocking, app, n, command)
            for l in lines:
                crane_log(emit, l)


# ---------- websocket ----------

async def ws_handler(request):
    app = request.app
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    app["clients"].add(ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
                topic, payload = d["topic"], d["payload"]
            except (ValueError, KeyError):
                continue
            if topic == "bins/command":
                kind = payload.get("type")
                barcode = str(payload.get("barcode", "")).strip()
                if kind == "retrieve":
                    asyncio.ensure_future(do_retrieve(app, barcode))
                elif kind == "store":
                    asyncio.ensure_future(do_store(app, barcode))
            elif topic == "debug/command":
                asyncio.ensure_future(do_debug(app, str(payload.get("command", ""))))
            elif topic in ("machine/cnc/command", "machine/crane/command"):
                asyncio.ensure_future(
                    do_console(app, topic, str(payload.get("command", ""))))
    finally:
        app["clients"].discard(ws)
    return ws


# ---------- REST (ported 1:1 from the node backend) ----------

def bad(msg, status=400):
    return web.json_response({"error": msg}, status=status)


async def get_parents(request):
    return web.json_response(read_json(PARENTS_FILE, {}))


async def post_parents(request):
    body = await request.json()
    category, parent = body.get("category"), body.get("parent")
    category = clean_name(category)
    if category is None:
        return bad("Invalid category name")
    parents = read_json(PARENTS_FILE, {})
    if parent:
        parents[category] = str(parent).strip()
    else:
        parents.pop(category, None)
    write_json(PARENTS_FILE, parents)
    return web.json_response({"success": True})


async def get_categories(request):
    return web.json_response(sorted(f.stem for f in BINS_DIR.glob("*.json")))


async def post_categories(request):
    name = (await request.json()).get("name")
    name = clean_name(name)
    if name is None:
        return bad("Invalid category name")
    f = BINS_DIR / (name + ".json")
    if f.exists():
        return bad("Category already exists", 409)
    write_json(f, [])
    return web.json_response({"success": True})


async def get_category(request):
    name = clean_name(request.match_info["name"])
    if name is None:
        return bad("Invalid category name")
    return web.json_response(read_json(BINS_DIR / (name + ".json"), []))


async def post_bin(request):
    name = clean_name(request.match_info["name"])
    if name is None:
        return bad("Invalid category name")
    f = BINS_DIR / (name + ".json")
    new_bin = await request.json()
    code = str(new_bin.get("barcode") or "").strip()
    if not code:
        return bad("A bin needs a barcode")
    owner = barcode_index().get(code)
    if owner:
        return bad('Barcode "%s" is already on a bin in "%s"' % (code, owner), 409)
    new_bin["barcode"] = code
    bins = read_json(f, [])
    bins.append(new_bin)
    write_json(f, bins)
    return web.json_response({"success": True})


async def put_category(request):
    name = clean_name(request.match_info["name"])
    if name is None:
        return bad("Invalid category name")
    bins = await request.json()
    if not isinstance(bins, list):
        return bad("Expected a list of bins")
    f = BINS_DIR / (name + ".json")
    # This rewrites the file wholesale, so it is where an edit could introduce
    # a clash - with another bin in the same category, or one in another.
    seen = set()
    for b in bins:
        code = str(b.get("barcode") or "").strip()
        if not code:
            return bad("A bin needs a barcode")
        if code in seen:
            return bad('Barcode "%s" is on two bins in "%s"' % (code, name), 409)
        seen.add(code)
        b["barcode"] = code
    elsewhere = barcode_index(skip=f)
    for code in seen:
        if code in elsewhere:
            return bad('Barcode "%s" is already on a bin in "%s"'
                       % (code, elsewhere[code]), 409)
    write_json(f, bins)
    return web.json_response({"success": True})


async def patch_category(request):
    name = clean_name(request.match_info["name"])
    if name is None:
        return bad("Invalid category name")
    body = await request.json()
    new_name, parent = body.get("newName"), body.get("parent")
    if new_name is not None:
        new_name = clean_name(new_name)
        if new_name is None:
            return bad("Invalid new name")
        new_path = BINS_DIR / (new_name + ".json")
        if new_path.exists():
            return bad("Category already exists", 409)
        (BINS_DIR / (name + ".json")).rename(new_path)
        parents = read_json(PARENTS_FILE, {})
        if name in parents:
            parents[new_name] = parents.pop(name)
            write_json(PARENTS_FILE, parents)
        name = new_name
    if parent is not None:
        parents = read_json(PARENTS_FILE, {})
        if parent:
            parents[name] = str(parent).strip()
        else:
            parents.pop(name, None)
        write_json(PARENTS_FILE, parents)
    return web.json_response({"success": True, "name": name})


async def delete_category(request):
    name = clean_name(request.match_info["name"])
    if name is None:
        return bad("Invalid category name")
    f = BINS_DIR / (name + ".json")
    if not f.exists():
        return bad("Category not found", 404)
    bins = read_json(f, [])
    if bins and request.query.get("force") != "1":
        # Not an error, just worth confirming: the bins are about to move house.
        # The caller says force=1 once it has told the user where they go.
        return web.json_response(
            {"error": "Category is not empty", "bins": len(bins)}, status=409)
    if bins:
        # The records outlive the category. A bin still physically exists (and
        # may still be shelved, tracked in slots.json) after someone tidies up
        # the folder it was filed under, so it lands in the unfiled bucket
        # rather than being dropped - same place inventory.ensure() puts a bin
        # nobody has categorised yet.
        if name == inventory.UNCATEGORIZED:
            return bad("The unfiled bucket holds bins with nowhere else to go - "
                       "it cannot be deleted while it still has any", 409)
        dest = BINS_DIR / (inventory.UNCATEGORIZED + ".json")
        keep = read_json(dest, [])
        known = {b.get("barcode") for b in keep}
        for b in bins:
            if b.get("barcode") in known:
                continue                     # already filed there; do not double up
            keep.append({**b, "subcategory": ""})   # that bucket has no subcategories
        write_json(dest, keep)
    f.unlink()
    parents = read_json(PARENTS_FILE, {})
    if parents.pop(name, None) is not None:
        write_json(PARENTS_FILE, parents)
    return web.json_response({"success": True})


async def patch_subcategory(request):
    name = clean_name(request.match_info["name"])
    if name is None:
        return bad("Invalid category name")
    body = await request.json()
    frm, to = body.get("from"), body.get("to")
    if not (isinstance(frm, str) and isinstance(to, str) and to.strip()):
        return bad("Invalid subcategory names")
    f = BINS_DIR / (name + ".json")
    if not f.exists():
        return bad("Category not found", 404)
    bins = read_json(f, [])
    for b in bins:
        if (b.get("subcategory") or "Uncategorized") == frm:
            b["subcategory"] = to.strip()
    write_json(f, bins)
    return web.json_response({"success": True})


# ---------- static frontend (React build in web/) ----------

async def static_handler(request):
    name = request.match_info.get("path", "")
    f = (WEB / name).resolve()
    if name and f.is_file() and WEB.resolve() in f.parents:
        return web.FileResponse(f)
    return web.FileResponse(WEB / "index.html")    # react-router fallback


def build_app(carriages):
    app = web.Application()
    app["carriages"] = carriages
    app["clients"] = set()
    app["lock"] = asyncio.Lock()
    app["debug_active"] = None
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/parents", get_parents)
    app.router.add_post("/parents", post_parents)
    app.router.add_get("/categories", get_categories)
    app.router.add_post("/categories", post_categories)
    app.router.add_get("/category/{name}", get_category)
    app.router.add_post("/category/{name}", post_bin)
    app.router.add_put("/category/{name}", put_category)
    app.router.add_patch("/category/{name}", patch_category)
    app.router.add_delete("/category/{name}", delete_category)
    app.router.add_patch("/category/{name}/subcategory", patch_subcategory)
    app.router.add_get("/{path:.*}", static_handler)
    return app


if __name__ == "__main__":
    BINS_DIR.mkdir(parents=True, exist_ok=True)
    carriages = {}
    for n, cfg in main.CARRIAGES.items():
        try:
            carriages[n] = main.open_carriage(cfg)
        except Exception as e:
            carriages[n] = None
            print("carriage %d not available: %s" % (n, e))
    web.run_app(build_app(carriages), port=PORT)
