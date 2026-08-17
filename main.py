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
CARRIAGES = {
    # carpark1: Pi uart4 (GPIO 8/9, header 24/21) <-> its Teensy Serial1 (pins 0/1)
    1: {"cnc": BY_ID + "usb-Arduino__www.arduino.cc__0043_0353638323635140D2A1-if00",
        "uart": "/dev/ttyAMA4", "pin": 5},
    # carpark2: Pi uart5 (GPIO 12/13, header 32/33) <-> its Teensy Serial1
    2: {"cnc": BY_ID + "usb-Arduino__www.arduino.cc__0043_03536373332351812222-if00",
        "uart": "/dev/ttyAMA5", "pin": 6},
}

MIN_SEPARATION = 140  # mm - carriages collide closer than this (compared in the
                      # shared reference frame, since each has its own machine zero)


HELP = """commands:
  m C R         go to the saved position for col C row R (never aligns)
  save C R      teach the CURRENT position as slot C,R (col 0 is taught, not aligned)
  positions     print all saved positions
  offset [X Y]  show or set this carriage's offset from carriage 1's frame
  car <cmd>     forward a debug command to the active carriage's car board
  u             lifts up   (carpark runs it, shows live mA)
  d             lifts down (carpark runs it, shows live mA)
  cur           spot current reading (mA) from carpark's INA219
  store [C R D] put the bin at staging (0,0) away - locate, lift, scan, shelve.
                With no args it picks the next free slot, deepest first.
  get BARCODE   fetch that stored bin and bring it back to staging
  slots         print barcode->slot assignments and the next free slot
  depth N       drive the car to taught depth N (1=closest, 2, 3=deepest)
  readpos       drive the car to the barcode read distance
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
  quit          exit"""


def clear_path(carriages, active, our_ref, target_ref):
    # target_ref is our destination in the shared (carriage 1) frame. If the other
    # carriage is closer than MIN_SEPARATION to it, push it that far clear first,
    # staying on the side it is already on (the carriages cannot pass each other).
    other = 2 if active == 1 else 1
    oc = carriages.get(other)
    if not oc:
        return
    off_o = positions.load_offsets().get(str(other), [0, 0])
    here = grbl.position(oc["cnc"])
    if here is None:
        print("cannot read carriage %d position - not moving it" % other)
        return
    o_ref = here[0] - off_o[0]                     # into the shared frame

    # Which side is it on? Decide relative to OUR CURRENT position, not the target -
    # the carriages cannot pass each other, so the side it is on now is the side it
    # must stay on. Using the target instead can shove it into the path we traverse.
    side = 1 if o_ref >= our_ref else -1

    # it must clear both our destination and the whole path we sweep to get there
    limit = max(our_ref, target_ref) if side > 0 else min(our_ref, target_ref)
    safe = limit + MIN_SEPARATION * side
    if (o_ref - safe) * side >= 0:                 # already far enough on its side
        return
    print("carriage %d at %.0f blocks the run %.0f -> %.0f - moving it to %.0f"
          % (other, o_ref, our_ref, target_ref, safe))
    grbl.goto(oc["cnc"], safe + off_o[0], here[1])


def goto_slot(carriages, n, col, row):
    # drive carriage n to the saved position for col,row, clearing the other
    # carriage out of the way first. False = missing position or unreadable GRBL.
    c = carriages[n]
    xy = positions.load().get(positions.key(col, row))
    if not xy:
        return False
    off = positions.load_offsets().get(str(n), [0, 0])
    ours = grbl.position(c["cnc"])
    if ours is None:
        return False
    clear_path(carriages, n, ours[0] - off[0], xy[0])
    grbl.goto(c["cnc"], xy[0] + off[0], xy[1] + off[1])
    return True


STAGING = (0, 0)   # bins to be put away arrive here; column 0 is not storage


def rack_lanes():
    # storage lanes in fill order - column 0 is the staging area, not a slot
    keys = [k for k in positions.load() if not k.startswith("0,")]
    return sorted(keys, key=lambda k: tuple(int(v) for v in k.split(",")))


def store_bin(carriages, active, say, target=None):
    """Take the bin waiting at the staging position and put it away.

    This is the one sequence that spans both axes - the carriage moves are the
    Pi's (GRBL), everything the car does is carpark's - so the interleaving has
    to live here. Each step is checked; a failure stops with the bin wherever it
    is rather than blindly carrying on.
    """
    uart = carriages[active]["uart"]

    def seq(ch):                         # per-command timeout, see carpark.TIMEOUTS
        return carpark.run_sequence(uart, ch, report=lambda l: say("  " + l))

    say("carriage -> staging %d,%d" % STAGING)
    if not goto_slot(carriages, active, *STAGING):
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

    say("presenting to scanner")
    if not seq("r"):
        return False
    code = carpark.read_barcode(uart)
    if code is None:
        say("no barcode read - stopping with the bin still on the car")
        return False
    say("barcode %s" % code)

    say("car home")
    if not seq("g"):
        return False

    key = target or slots.next_free(rack_lanes())
    if key is None:
        say("rack is full - bin is on the car at staging")
        return False
    col, row, depth = (int(v) for v in key.split(","))

    say("carriage -> %d,%d depth %d" % (col, row, depth))
    if not goto_slot(carriages, active, col, row):
        say("cannot reach %d,%d" % (col, row))
        return False
    say("driving to depth %d" % depth)
    if not seq(str(depth)):
        return False
    say("lift down")
    if not seq("d"):
        return False
    say("car home")
    if not seq("g"):
        return False

    store = slots.load()                 # only recorded once the bin is placed
    store[key] = code
    slots.save(store)
    added = inventory.ensure(code)       # so a never-seen bin shows in the UI
    if added:
        say("new bin - added %s to the %s category" % (code, added))
    say("stored %s at %s" % (code, key))
    return True


def put_bin(carriages, active, say, seq, code, dest, old_key=None):
    """Shelve the bin currently on the car into dest ("col,row,depth").

    Driving to a measured depth only works with a bin aboard, which is exactly
    the case here - the rangefinder ranges off the bin, not the car.
    """
    col, row, depth = (int(v) for v in dest.split(","))
    say("re-shelving %s at %s" % (code, dest))
    if not goto_slot(carriages, active, col, row):
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


def retrieve_bin(carriages, active, say, barcode):
    """Fetch a stored bin by barcode, digging out anything parked in front of it.

    The car enters a lane EMPTY, and the rangefinder ranges off the bin rather
    than the car, so there is no distance reading on the way in - going in is
    always sensor-driven (locate), which takes whichever bin is at the front.
    Reaching a deeper bin therefore means pulling the ones ahead of it out first
    and re-shelving them elsewhere. Every pickup is scanned, so a lane whose
    real contents disagree with slots.json is caught rather than acted on.
    """
    key = slots.find(barcode)
    if key is None:
        say("no slot assigned for barcode %s" % barcode)
        return False
    col, row, depth = (int(v) for v in key.split(","))
    lane = "%d,%d" % (col, row)
    uart = carriages[active]["uart"]

    def seq(ch):                         # per-command timeout, see carpark.TIMEOUTS
        return carpark.run_sequence(uart, ch, report=lambda l: say("  " + l))

    if depth > 1:
        say("%s is at depth %d - bins in front of it come out first"
            % (barcode, depth))

    for _ in range(depth):               # at most depth pickups: blockers, then it
        say("carriage -> %s" % lane)
        if not goto_slot(carriages, active, col, row):
            say("cannot reach %s" % lane)
            return False

        say("lift down")                 # forks low to slide under
        if not seq("d"):
            return False
        say("locating front bin")        # car is empty: sensor, not distance
        if not seq("l"):
            say("no bin found in %s" % lane)
            return False
        say("lift up")
        if not seq("u"):
            return False
        say("reading label")             # bin aboard, so distance works again
        if not seq("r"):
            return False
        got = carpark.read_barcode(uart)
        if got is None:
            say("could not read the label - stopping with the bin on the car")
            return False
        say("picked up %s" % got)
        if not seq("g"):
            return False

        got_key = slots.find(got)
        if got == barcode:
            say("carriage -> staging %d,%d" % STAGING)
            if not goto_slot(carriages, active, *STAGING):
                say("bin is on the car, but cannot reach staging")
                return False
            store = slots.load()         # cleared only once it is really out
            store.pop(got_key or key, None)
            slots.save(store)
            say("retrieved %s - on the car at staging" % barcode)
            return True

        # a blocker: put it anywhere but this lane, then come back for the target
        dest = slots.next_free([l for l in rack_lanes() if l != lane])
        if dest is None:
            say("nowhere to put %s - rack is full" % got)
            return False
        if not put_bin(carriages, active, say, seq, got, dest, got_key):
            return False

    say("emptied %d bins from %s without finding %s - slots.json is out of sync"
        % (depth, lane, barcode))
    return False


def dispatch(carriages, active, line, say=print):
    # Run one debug command against the active carriage; returns the (possibly
    # switched) active carriage. say() gets every output line - the shell prints,
    # the web /debug page broadcasts. One implementation for both.
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
            elif not goto_slot(carriages, active, col, row):
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
            carpark.send_car(uart, "".join(parts[1:]), report=say)
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
        elif cmd == "readpos":    # drive to the barcode read distance
            carpark.run_sequence(uart, "r",
                                 report=lambda l: say("  " + l))
        elif cmd == "store":      # full put-away of the bin at staging
            target = (slots.key(int(parts[1]), int(parts[2]), int(parts[3]))
                      if len(parts) == 4 else None)
            store_bin(carriages, active, say, target)
        elif cmd == "get":        # fetch a stored bin by barcode
            retrieve_bin(carriages, active, say, parts[1])
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
            "sensor": DigitalInputDevice(cfg["pin"], pull_up=False)}


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
