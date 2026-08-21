import re
import sys

import serial
from gpiozero import DigitalInputDevice

import carpark
import grbl
import inventory
import positions
import slots

BAUD = 9600           # carpark UART baud

# Per-carriage hardware: GRBL port, its own carpark board UART, and that carpark's
# alignment sensor GPIO (driven HIGH on trigger, Pi pull-down).
# c1/c2 in the debug shell swaps this whole bundle.
# GRBL boards use /dev/serial/by-id paths - ACM0/ACM1 enumeration can swap on reboot.
BY_ID = "/dev/serial/by-id/"
# column0: whether this carriage can physically reach column 0 - staging (0,0)
# and the output lane (0,1). Only carriage 1 can, so it is the only one that can
# run store or get. That also settles the scanner: the label is read at column 0
# on the way home, so a carriage that cannot get there never needs one, which is
# why carriage 2 has no scanner fitted.
CARRIAGES = {
    # carpark1: Pi uart4 (GPIO 8/9, header 24/21) <-> its Teensy Serial1 (pins 0/1)
    1: {"cnc": BY_ID + "usb-Arduino__www.arduino.cc__0043_0353638323635140D2A1-if00",
        "uart": "/dev/ttyAMA4", "pin": 5, "column0": True},
    # carpark2: Pi uart5 (GPIO 12/13, header 32/33) <-> its Teensy Serial1
    2: {"cnc": BY_ID + "usb-Arduino__www.arduino.cc__0043_03536373332351812222-if00",
        "uart": "/dev/ttyAMA5", "pin": 6, "column0": False},
}

MIN_SEPARATION = 140  # mm - carriages collide closer than this (compared in the
                      # shared reference frame, since each has its own machine zero)


HELP = """commands:
  m C R         go to the saved position for col C row R (never aligns)
  save C R      teach the CURRENT position as slot C,R (col 0 is taught, not aligned)
  positions     print all saved positions
  offset [X Y]  show or set this carriage's offset from carriage 1's frame
  car <cmd>     forward a debug command to the active carriage's car board
  car <cmd> MS  same, but run it for MS milliseconds then brake and release
                (e.g. 'car AF250 800'). Timed on carpark, so it measures the
                same burst driveToDepth uses - this is the calibration tool
  u             lifts up   (carpark runs it, shows live mA)
  d             lifts down (carpark runs it, shows live mA)
  cur           spot current reading (mA) from carpark's INA219
  store [C R D] put the bin at staging (0,0) away - locate, lift, scan, shelve.
                With no args it picks the next free slot, deepest first.
  get BARCODE   fetch that stored bin and drop it at the output lane 0,1.
                Depth 2+ needs carriage 2: it lifts the bin in front out and
                holds it while carriage 1 takes the target, then puts it back
  slots         print barcode->slot assignments and the next free slot
  depth N       drive the car to taught depth N (1=closest, 2, 3=deepest).
                Never scans - use 'read' for that
  read          send the car home AND scan the label on the way - the bin
                passes the scanner anyway, so the scan is free. Replaces
                gohome in the store/get sequences; no extra move at all
  place         creep in until the carried bin meets whatever is already in the
                lane, and stop there - for dropping at an unknown depth
  dist          car depth in the lane (mm) from carpark's rangefinder
  scan          trigger the barcode scanner and report what it reads
  laser <hex>   hex bytes to the rangefinder, reply shown as hex
                (e.g. 'laser aa 80 00 22 a2' reads, 'laser aa 00 01 be 00 01 00 01 c1' = dot on)
  f             drive forward (runs until stopped)
  r             drive reverse (runs until stopped)
  s             stop all motors
  ret           retrieve a bin - carpark drives in, grabs it and returns home
  locate        drive in until the bin sensor finds a bin, then stop (no lift)
  gohome        send the car back to its home position
  x+ x- y+ y-   jog one mm per sign (x++ = 2mm, y----- = -5mm); prints pos + sensor
  pos           print current (x, y)
  home          car-home sensor: trigger + raw value
  bin           bin-placement sensor: trigger + value
  align         alignment sensor: trigger + raw value (carpark's own read)
  sensor        print live alignment sensor value
  $H            run the GRBL homing cycle (blocks until done)
  sleep         $SLP - de-energize steppers
  wake          soft reset + unlock out of sleep
  <raw gcode>   anything else is sent straight to GRBL
  c1 / c2       switch active carriage (prompt shows which)
  help          show this list
  quit          exit

carpark protocol letters - the single chars the shell commands above send to
the carpark board. Type these raw in the web DevPage crane console or in the
Teensy's USB serial monitor; the shell equivalent is in brackets.
  f b s   drive forward / back / stop all motors        [f, r, s]
  u d     lifts up / down                               [u, d]
  t       retrieve: in, grab bin, lift, back out home   [ret]
  l       locate: in, stop at the bin, no lift/return   [locate]
  g       go home: reverse until docked                 [gohome]
  1 2 3   drive to taught depth 1/2/3 (never scans)     [depth N]
  R       go home AND read the label -> "BC <code|->"    [read]
  P       creep in until the carried bin is stopped     [place]
  m       one rangefinder reading -> "DIST <mm>"        [dist]
  Q       fresh scan, waits for it -> "BC <code|->"     [scan]
  q       last barcode already seen -> "BC <code>"      [-]
  h       home sensor    -> "HOME <0|1> <raw>"          [home]
  p       bin sensor     -> "BIN <0|1> <raw>"           [bin]
  a       align sensor   -> "ALIGN <0|1> <raw>"         [align]
  c       lift current   -> "I <mA>"                    [cur]
  >...    forward the rest of the line to the car board [car <cmd>]
  <...    hex bytes straight to the rangefinder         [laser <hex>]

Sequences (t l g 1 2 3 R) stream progress lines and end in DONE or FAIL.
Depth moves print "d <mm>" while running in, then "c <mm>" while creeping."""


# What a debug command actually needs held, so the web layer stops serialising
# things that never touch the same hardware.
#   none   - no machine at all (switching the active carriage, reading files)
#   active - only the carriage it is aimed at
#   both   - can move a carriage ALONG THE SHARED RAIL, so nothing else may move
#
# $H is deliberately 'active': the two carriages home to OPPOSITE ends of the
# rail, so homing only ever drives them apart and both can home at once.
# Jogs and raw gcode are 'both' - unlike 'm' they get no clear_path check, so
# they must not overlap with anything else that moves. Anything unrecognised
# falls through to GRBL as raw gcode, so the default is the cautious one.
_SCOPE_NONE = {"help", "c1", "c2", "positions", "offset", "slots", "quit"}
_SCOPE_ACTIVE = {"save", "car", "u", "d", "ret", "gohome", "locate", "cur",
                 "depth", "read", "place", "dist", "scan", "laser", "f", "r",
                 "s", "pos", "home", "bin", "align", "sensor", "$H", "sleep",
                 "wake"}


def command_scope(line):
    parts = line.split()
    if not parts:
        return "none"
    cmd = parts[0]
    if cmd in _SCOPE_NONE:
        return "none"
    if cmd in _SCOPE_ACTIVE:
        return "active"
    return "both"          # m/move/store/get, a jog, or raw gcode


def clear_path(carriages, active, our_ref, target_ref, say=print):
    """Where the other carriage has to be for us to run our_ref -> target_ref.

    Returns None if it is already clear (or absent/unreadable), otherwise
    (number, cnc, x, y, gap): where to send it, and gap - how far apart the two
    are RIGHT NOW. Deciding the move and performing it are split so the caller
    can start both carriages together; see goto_slot.
    """
    other = 2 if active == 1 else 1
    oc = carriages.get(other)
    if not oc:
        return None
    off_o = positions.load_offsets().get(str(other), [0, 0])
    here = grbl.position(oc["cnc"])
    if here is None:
        say("cannot read carriage %d position - not moving it" % other)
        return None
    o_ref = here[0] - off_o[0]                     # into the shared frame

    # Which side is it on? Decide relative to OUR CURRENT position, not the target -
    # the carriages cannot pass each other, so the side it is on now is the side it
    # must stay on. Using the target instead can shove it into the path we traverse.
    side = 1 if o_ref >= our_ref else -1

    # it must clear both our destination and the whole path we sweep to get there
    limit = max(our_ref, target_ref) if side > 0 else min(our_ref, target_ref)
    safe = limit + MIN_SEPARATION * side
    if (o_ref - safe) * side >= 0:                 # already far enough on its side
        return None
    say("carriage %d at %.0f blocks the run %.0f -> %.0f - moving it to %.0f"
        % (other, o_ref, our_ref, target_ref, safe))
    return other, oc["cnc"], safe + off_o[0], here[1], abs(o_ref - our_ref)


def goto_slot(carriages, n, col, row, say=print):
    """Drive carriage n to the saved position for col,row, clearing the other
    carriage out of the way. False = missing position or unreadable GRBL.

    BOTH CARRIAGES RUN AT ONCE when it is safe to. They travel at the same
    speed, and the blocker is only ever asked to move to MIN_SEPARATION beyond
    where we are going - so in the direction that closes the gap its trip is
    always the shorter of the two. The gap therefore shrinks monotonically from
    whatever it is now to exactly MIN_SEPARATION, and never dips below.

    That argument leans on two things, so both are checked rather than assumed:

      * EQUAL SPEED. If the blocker is slower the gap undershoots - a quarter
        slower closes it to 75mm on a long run. $110/$111 (max rate) and
        $120/$121 (acceleration) must match on both boards.
      * ALREADY AT LEAST MIN_SEPARATION APART. Starting closer than that (after
        hand jogging, say) the blocker's trip is the longer one and the gap
        keeps shrinking. That case falls back to clearing it first, in full.
    """
    c = carriages[n]
    xy = positions.load().get(positions.key(col, row))
    if not xy:
        return False
    off = positions.load_offsets().get(str(n), [0, 0])
    ours = grbl.position(c["cnc"])
    if ours is None:
        return False
    x, y = xy[0] + off[0], xy[1] + off[1]

    move = clear_path(carriages, n, ours[0] - off[0], xy[0], say)
    if move is None:                               # nothing in the way
        grbl.goto(c["cnc"], x, y)
        return True

    other, ocnc, ox, oy, gap = move
    if gap < MIN_SEPARATION:
        say("carriages are only %.0fmm apart - clearing carriage %d first"
            % (gap, other))
        grbl.goto(ocnc, ox, oy)                    # in full, before we add motion
        grbl.goto(c["cnc"], x, y)
        return True

    # Start the blocker, then make sure it actually took the move before we
    # commit to ours. A rejected one (soft limit, not homed) used to leave us
    # standing still because clear_path blocked; now we would be rolling at it.
    grbl.send_goto(ocnc, ox, oy)
    if grbl.state(ocnc) == "Alarm":
        raise RuntimeError("carriage %d rejected its clearing move - staying put"
                           % other)
    grbl.send_goto(c["cnc"], x, y)
    grbl.wait_idle(ocnc)
    grbl.wait_idle(c["cnc"])
    return True


STAGING = (0, 0)   # bins to be put away arrive here; column 0 is not storage
OUTPUT  = (0, 1)   # retrieved bins are dropped here for a human to collect.
                   # Separate from STAGING so an arriving bin and a retrieved
                   # one never contend for the same lane. How deep it already
                   # is depends on what has been collected, so the drop is felt
                   # out with carpark's 'P' rather than addressed by depth.


def rack_lanes():
    """Storage lanes in fill order - column 0 is the staging area, not a slot.

    ROW-MAJOR: fill all of row 0 across the columns, then row 1, and so on.
    Columns are X, rows are Y, and staging (0,0) sits at row 0's exact Y - so
    filling a row first means the carriage runs straight across at the height it
    is already at, instead of climbing a whole column and coming back down for
    every bin. Sorting by (col, row) - the obvious reading of the key - gives
    the column-major order, which is why this reverses them.
    """
    keys = [k for k in positions.load() if not k.startswith("0,")]

    def row_then_col(k):
        col, row = (int(v) for v in k.split(","))
        return (row, col)

    return sorted(keys, key=row_then_col)


def store_bin(carriages, active, say, target=None, on_status=None):
    """Take the bin waiting at the staging position and put it away.

    This is the one sequence that spans both axes - the carriage moves are the
    Pi's (GRBL), everything the car does is carpark's - so the interleaving has
    to live here. Each step is checked; a failure stops with the bin wherever it
    is rather than blindly carrying on.

    STATUS FOLLOWS THE SCANNER, NOTHING ELSE. A bin only becomes in-pending once
    its own label has been read off the forks, and only becomes in once it is
    actually shelved. Nothing upstream may set either optimistically: the UI
    cannot know which bin is really sitting at staging, and marking the tapped
    card meant storing bin B while bin A's card claimed to be in stock.
    on_status is for broadcasting the change; the file is written here either
    way, so a store from the shell records it too.
    """
    if not carriages[active].get("column0", True):
        say("carriage %d cannot reach column 0 - store is carriage 1 only" % active)
        return False
    uart = carriages[active]["uart"]

    def seq(ch):                         # per-command timeout, see carpark.TIMEOUTS
        return carpark.run_sequence(uart, ch, report=lambda l: say("  " + l))

    def status(code, value):
        inventory.set_status(code, value)
        if on_status:
            on_status(code, value)

    say("carriage -> staging %d,%d" % STAGING)
    if not goto_slot(carriages, active, *STAGING, say=say):
        say("cannot reach staging %d,%d - taught? GRBL readable?" % STAGING)
        return False

    say("lift down")                     # forks must be low before driving in
    if not seq("d"):
        return False
    say("locating bin")
    if not seq("l"):
        say("no bin at staging - nothing to store")
        return False
    say("lift up")
    if not seq("u"):
        return False

    say("car home, reading the label on the way")   # 'R' = 'g' plus the scan
    if not seq("R"):
        return False
    code = carpark.read_barcode(uart, fresh=False)
    if code is None:
        say("no barcode read - stopping with the bin still on the car")
        return False
    say("barcode %s" % code)
    inventory.ensure(code)               # a record to hang the status on
    status(code, "in-pending")           # now, and only now, is the bin known

    key = target or slots.next_free(rack_lanes())
    if key is None:
        say("rack is full - bin is on the car at staging")
        status(code, "out")
        return False
    col, row, depth = (int(v) for v in key.split(","))

    say("carriage -> %d,%d depth %d" % (col, row, depth))
    if not goto_slot(carriages, active, col, row, say=say):
        say("cannot reach %d,%d" % (col, row))
        status(code, "out")
        return False
    say("driving to depth %d" % depth)
    if not seq(str(depth)):
        status(code, "out")
        return False
    say("lift down")
    if not seq("d"):
        status(code, "out")
        return False
    say("car home")
    if not seq("g"):
        status(code, "out")
        return False

    store = slots.load()                 # only recorded once the bin is placed
    store[key] = code
    slots.save(store)
    status(code, "in")                   # shelved, and only now in stock
    say("stored %s at %s" % (code, key))
    return True


def put_bin(carriages, active, say, seq, code, dest, old_key=None):
    """Shelve the bin currently on the car into dest ("col,row,depth").

    Driving to a measured depth only works with a bin aboard, which is exactly
    the case here - the rangefinder ranges off the bin, not the car.
    """
    col, row, depth = (int(v) for v in dest.split(","))
    say("re-shelving %s at %s" % (code, dest))
    if not goto_slot(carriages, active, col, row, say=say):
        say("cannot reach %d,%d" % (col, row))
        return False
    if not seq(str(depth)):
        return False
    if not seq("d"):                     # set it down
        return False
    if not seq("g"):
        return False
    store = slots.load()
    if old_key:
        store.pop(old_key, None)
    store[dest] = code
    slots.save(store)
    return True


def retrieve_bin(carriages, active, say, barcode, on_status=None):
    """Fetch a stored bin by barcode, digging out anything parked in front of it.

    The car enters a lane EMPTY, and the rangefinder ranges off the bin rather
    than the car, so there is no reading on the way in - going in is always
    sensor-driven (locate), which takes whichever bin is at the FRONT. Reaching
    a deeper bin therefore means pulling the ones ahead of it out first.

    THE SECOND CARRIAGE IS A PAIR OF HANDS, not a second retriever. For a target
    at depth D:

        depths 1 .. D-2   the active carriage clears, re-shelving each one
        depth  D-1        the helper lifts out and HOLDS, then puts it straight
                          back when the target is clear
        depth  D          the active carriage takes, and delivers to 0,1

    which is why depth 1 needs no help at all, depth 2 is just "helper holds one
    bin", and depth 3 is active, helper, active. Holding beats re-shelving
    because the bin never leaves its slot on paper - nothing in slots.json moves
    and it goes back where it came from. Only the earlier blockers, which have
    to be put down somewhere for the forks to be free again, actually move.

    They never share a lane: the column pitch is 115mm against a 140mm minimum
    separation, so two carriages cannot even stand at neighbouring columns. Each
    trip is take-turns, and goto_slot pushes the other one clear on its own.

    THE HELPER HAS NO SCANNER, so what it lifts is taken on trust from
    slots.json. Every bin the ACTIVE carriage picks is checked against its own
    slot: not the target is fine on a deep dig, not what that slot says is not.
    A slot that disagrees marks its recorded bin missing and sends whatever
    really came out to 0,1, rather than re-filing by a record just shown wrong.

    STATUS FOLLOWS THE SCANNER, as in store_bin: the target is only out-pending
    once its own label has come back off the forks. Bins that go back on a shelf
    are left alone - 'in' never stops being true for them.
    """
    if not carriages[active].get("column0", True):
        say("carriage %d cannot reach column 0 - get is carriage 1 only" % active)
        return False
    key = slots.find(barcode)
    if key is None:
        say("no slot assigned for barcode %s" % barcode)
        return False
    col, row, depth = (int(v) for v in key.split(","))
    lane = "%d,%d" % (col, row)
    uart = carriages[active]["uart"]

    other = 2 if active == 1 else 1
    helper = other if carriages.get(other) else None

    def seq(ch):                         # per-command timeout, see carpark.TIMEOUTS
        return carpark.run_sequence(uart, ch, report=lambda l: say("  " + l))

    def seq_h(ch):                       # ...on the helper, tagged so the two
        return carpark.run_sequence(     # machines are told apart in the log
            carriages[helper]["uart"], ch,
            report=lambda l: say("  c%d %s" % (helper, l)))

    def status(code, value):
        inventory.set_status(code, value)
        if on_status:
            on_status(code, value)

    def lift_out(n, runner):
        """Drive carriage n into the lane and come away with the front bin."""
        say("carriage %d -> %s" % (n, lane))
        if not goto_slot(carriages, n, col, row, say=say):
            say("cannot reach %s" % lane)
            return False
        return runner("d") and runner("l") and runner("u")

    def to_output(code):
        """Carry whatever is on the forks out to 0,1 and set it down there.

        Every exit leaves the bin out of the rack - it is on the car either way -
        so the only question is whether it reached the lane.
        """
        status(code, "out-pending")
        say("carriage -> output %d,%d" % OUTPUT)
        if not goto_slot(carriages, active, *OUTPUT, say=say):
            say("bin is on the car, but cannot reach output %d,%d" % OUTPUT)
            status(code, "out")
            return False
        if not (seq("P") and seq("d") and seq("g")):
            status(code, "out")
            return False
        status(code, "out")
        return True

    if depth > 1:
        if helper is None:
            say("%s is at depth %d and carriage %d is not connected - "
                "nothing can hold the bin in front of it" % (barcode, depth, other))
            return False
        say("%s is at depth %d - %d bin(s) in front of it come out first"
            % (barcode, depth, depth - 1))

    held_depth = None                    # depth the helper is holding a bin from
    try:
        # ---- blockers, front first ----
        for d in range(1, depth):
            if d == depth - 1:
                # The last one only has to be out of the way for a moment, so
                # the helper keeps it on the forks instead of shelving it.
                say("carriage %d lifting out depth %d to hold" % (helper, d))
                if not lift_out(helper, seq_h):
                    return False
                if not seq_h("g"):
                    return False
                held_depth = d
                continue

            # An earlier blocker has to be put down for good - the active
            # carriage needs empty forks again to keep digging.
            if not lift_out(active, seq):
                return False
            say("car home, reading the label on the way")
            if not seq("R"):
                return False
            got = carpark.read_barcode(uart, fresh=False)
            if got is None:
                say("could not read the label - stopping with the bin on the car")
                return False
            say("picked up %s" % got)
            if not _slot_agrees(say, status, to_output, col, row, d, got):
                return False
            dest = slots.next_free([l for l in rack_lanes() if l != lane])
            if dest is None:
                say("nowhere to put %s - rack is full" % got)
                return False
            if not put_bin(carriages, active, say, seq, got, dest, slots.find(got)):
                return False

        # ---- the target, now at the front ----
        if not lift_out(active, seq):
            return False
        say("car home, reading the label on the way")
        if not seq("R"):
            return False
        got = carpark.read_barcode(uart, fresh=False)
        if got is None:
            say("could not read the label - stopping with the bin on the car")
            return False
        say("picked up %s" % got)
        if not _slot_agrees(say, status, to_output, col, row, depth, got):
            return False

        got_key = slots.find(got)
        if not to_output(got):
            return False
        store = slots.load()             # cleared only once it is really out
        store.pop(got_key or key, None)
        slots.save(store)
        say("retrieved %s - dropped at output %d,%d" % ((barcode,) + OUTPUT))
        return True
    finally:
        # However this ended, the helper must not be left holding anything.
        if held_depth is not None:
            say("carriage %d putting its bin back at depth %d" % (helper, held_depth))
            if not (goto_slot(carriages, helper, col, row, say=say)
                    and seq_h(str(held_depth)) and seq_h("d") and seq_h("g")):
                say("CARRIAGE %d IS STILL HOLDING A BIN - it could not be put "
                    "back at %s depth %d" % (helper, lane, held_depth))


def _slot_agrees(say, status, to_output, col, row, picked_depth, got):
    """True if `got` is what slots.json says is at that exact slot.

    Not the target is fine - pulling the front bins out of the way is normal on
    a deep dig. Not what THIS slot says is not: the record was the only reason
    to believe the recorded bin was anywhere, so it is marked missing, and
    whatever really came out goes to 0,1 rather than back on a shelf, since
    re-filing by a record just shown wrong would bury the problem.
    """
    store = slots.load()
    slot_key = slots.key(col, row, picked_depth)
    expected = store.get(slot_key)
    if got == expected:
        return True
    say("%s should hold %s but the car brought out %s"
        % (slot_key, expected or "nothing", got))
    if expected:
        status(expected, "missing")
        say("%s marked missing" % expected)
    store.pop(slot_key, None)            # that slot is empty now
    got_key = slots.find(got)
    if got_key:
        store.pop(got_key, None)         # and wherever this one was filed
    slots.save(store)
    say("sending %s to the output lane instead" % got)
    to_output(got)
    return False


def dispatch(carriages, active, line, say=print, on_status=None):
    # Run one debug command against the active carriage; returns the (possibly
    # switched) active carriage. say() gets every output line - the shell prints,
    # the web /debug page broadcasts. One implementation for both.
    # on_status is the same idea for bin status changes: the terminal has nobody
    # to tell, the web page has open browsers. Without it a store run from the
    # Debug page wrote the new status to disk and left every browser showing the
    # old one until someone reloaded.
    c = carriages[active]
    cnc, uart, sensor = c["cnc"], c["uart"], c["sensor"]
    off = positions.load_offsets().get(str(active), [0, 0])
    parts = line.split()
    cmd = parts[0]
    try:
        if cmd == "help":
            say(HELP)
        elif cmd in ("c1", "c2"):     # switch active carriage
            n = int(cmd[1])
            if carriages.get(n):
                active = n
                say("carriage %d active" % n)
            else:
                say("carriage %d not connected" % n)
        elif cmd in ("m", "move"):
            col, row = int(parts[1]), int(parts[2])
            if not positions.load().get(positions.key(col, row)):
                say("no saved position for %s - teach it with 'save C R'"
                    % positions.key(col, row))
            elif not goto_slot(carriages, active, col, row, say=say):
                say("cannot read our position - not moving")
        elif cmd == "save":       # teach current position as slot C,R (for col 0)
            col, row = int(parts[1]), int(parts[2])
            here = grbl.position(cnc)
            if here is None:      # never write a taught position we cannot read
                say("cannot read position from GRBL - nothing saved")
            else:
                pos = positions.load()
                pos[positions.key(col, row)] = [here[0] - off[0], here[1] - off[1]]
                positions.save(pos)
                say("saved %s %s"
                    % (positions.key(col, row), pos[positions.key(col, row)]))
        elif cmd == "positions":
            say(positions.load())
        elif cmd == "offset":     # show or set this carriage's (dx, dy)
            if len(parts) == 3:
                offs = positions.load_offsets()
                offs[str(active)] = [float(parts[1]), float(parts[2])]
                positions.save_offsets(offs)
            say("carriage %d offset %s"
                % (active, positions.load_offsets().get(str(active), [0, 0])))
        elif cmd == "car":
            # "car AF250 800" = run that for 800ms, then brake and release.
            # Only a COMPLETE motor command followed by digits is read that way,
            # so the old spaces-are-optional form ("car AF 250") still means AF250.
            args = parts[1:]
            ms = None
            if (len(args) == 2 and args[1].isdigit()
                    and re.match(r"^[A-Ca-c][FRfr]\d+$", args[0])):
                args, ms = args[:1], int(args[1])
            carpark.send_car(uart, "".join(args), ms=ms, report=say)
        elif cmd in ("u", "d", "ret", "gohome", "locate"):
            # high level: carpark owns the whole sequence, we just report
            carpark.run_sequence(uart, {"u": "u", "d": "d", "ret": "t",
                                        "gohome": "g", "locate": "l"}[cmd],
                                 report=lambda l: say("  " + l))
        elif cmd == "cur":        # spot current reading from carpark's INA219
            say("current %s mA" % carpark.read_current(uart))
        elif cmd == "depth":      # drive the car to taught depth 1/2/3
            depth = int(parts[1])
            if depth not in (1, 2, 3):
                say("depth must be 1, 2 or 3")
            else:
                carpark.run_sequence(uart, str(depth),
                                     report=lambda l: say("  " + l))
        elif cmd == "read":       # depth 1 + scan the carried bin's label
            carpark.run_sequence(uart, "R", report=lambda l: say("  " + l))
        elif cmd == "place":      # creep in until the carried bin is stopped
            carpark.run_sequence(uart, "P", report=lambda l: say("  " + l))
        elif cmd == "store":      # full put-away of the bin at staging
            target = (slots.key(int(parts[1]), int(parts[2]), int(parts[3]))
                      if len(parts) == 4 else None)
            store_bin(carriages, active, say, target, on_status=on_status)
        elif cmd == "get":        # fetch a stored bin by barcode
            retrieve_bin(carriages, active, say, parts[1], on_status=on_status)
        elif cmd == "slots":
            say(slots.load() or "no slots assigned yet")
            say("next free: %s" % slots.next_free(rack_lanes()))
        elif cmd == "dist":
            mm = carpark.read_distance(uart)
            say("no reading" if mm is None else "%d mm" % mm)
        elif cmd == "scan":
            code = carpark.read_barcode(uart)
            say("no code seen" if code is None else code)
        elif cmd == "laser":
            carpark.send_laser(uart, " ".join(parts[1:]), report=say)
        elif cmd in ("f", "r", "s"):
            carpark.drive(uart, cmd)
        elif len(cmd) > 1 and cmd[0] in "xy" and set(cmd[1:]) in ({"+"}, {"-"}):
            # one mm per sign: x+ = 1mm, x++ = 2mm, x+++ = 3mm ...
            dist = len(cmd) - 1
            grbl.jog(cnc, cmd[0].upper(), dist if cmd[1] == "+" else -dist)
            say("%s sensor %s" % (grbl.position(cnc), sensor.value))
        elif cmd == "pos":
            say(grbl.position(cnc))
        elif cmd in ("home", "bin", "align"):
            trig, val, extra = {"home": carpark.read_home, "bin": carpark.read_bin,
                                "align": carpark.read_align}[cmd](uart)
            say("%-5s trigger %-5s value %s%s"
                % (cmd, trig, val,
                   "   [watch,seen,samples = %s]" % ",".join(extra) if extra else ""))
        elif cmd == "sensor":
            say("sensor %s" % sensor.value)
        elif cmd == "$H":         # homing cycle: blocks until GRBL finishes
            say("homing carriage %d..." % active)
            say("homing: %s" % grbl.home(cnc))
            say("position %s" % (grbl.position(cnc),))
        elif cmd == "sleep":
            grbl.sleep(cnc)
        elif cmd == "wake":
            grbl.wake(cnc)
        else:
            cnc.write((line + "\n").encode())
            say(cnc.readline().decode(errors="replace").strip())
    except Exception as e:
        say("error: %s" % e)
    return active


def debug(carriages):
    print(HELP)
    active = next((n for n in carriages if carriages[n]), None)
    if active is None:
        print("no carriages connected")
        return
    while True:
        line = input("debug[%d]> " % active).strip()
        if not line:
            continue
        if line.split()[0] == "quit":
            break
        active = dispatch(carriages, active, line)


def open_carriage(cfg):
    cnc = grbl.connect(cfg["cnc"])
    grbl.start(cnc)                    # unlock + G90 + hold steppers energized ($1=255)
    uart = serial.Serial(cfg["uart"], BAUD, timeout=1)
    print("connected to", cfg["uart"])
    return {"cnc": cnc, "uart": uart,
            "sensor": DigitalInputDevice(cfg["pin"], pull_up=False),
            "column0": cfg.get("column0", True)}


def main():
    carriages = {}
    for n, cfg in CARRIAGES.items():
        try:
            carriages[n] = open_carriage(cfg)
        except Exception as e:
            carriages[n] = None
            print("carriage %d not available: %s" % (n, e))

    if "--debug" in sys.argv:
        debug(carriages)
    else:
        c = carriages[1]
        xy = positions.load().get(positions.key(1, 0))
        if xy:
            off = positions.load_offsets().get("1", [0, 0])
            grbl.goto(c["cnc"], xy[0] + off[0], xy[1] + off[1])
        else:
            print("no saved position for 1,0")


if __name__ == "__main__":
    main()
