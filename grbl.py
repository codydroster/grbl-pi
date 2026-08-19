import re
import time

import serial

BAUD = 115200   # GRBL serial baud; the port itself comes from main.py's CARRIAGES

# GRBL default coordinate space: machine zero is at the greatest X/Y and the whole
# envelope runs negative. Carriage 1 homes at (-1140, -500), the most-negative
# corner; carriage 2 homes at 0 (greatest X). Each carriage's zero is its own max-X
# end, so the same number is a different physical spot on each - main.py's
# per-carriage offset reconciles them. Slot coordinates live in positions.json.


def connect(port):
    grbl = serial.Serial(port, BAUD, timeout=1)
    print("connected to", port)
    return grbl


def position(cnc):
    # query GRBL and return current (x, y); accepts MPos or WPos.
    # retry because a fresh '?' can land on a banner/ok line instead of a status report
    for _ in range(10):
        cnc.reset_input_buffer()
        cnc.write(b"?")
        line = cnc.readline().decode(errors="replace").strip()
        m = re.search(r"Pos:(-?\d+\.\d+),(-?\d+\.\d+)", line)
        if m:
            return float(m.group(1)), float(m.group(2))
        time.sleep(0.05)
    return None


def wait_idle(cnc, timeout=60):
    # wait until GRBL reports Idle twice (dodges the buffered-move race).
    # bail on Alarm, else a rejected move (soft limit, not homed) hangs here forever
    count = 0
    deadline = time.time() + timeout
    while count < 2:
        if time.time() > deadline:
            raise RuntimeError("GRBL not idle after %ds - is the board responding?" % timeout)
        cnc.reset_input_buffer()
        cnc.write(b"?")
        line = cnc.readline().decode(errors="replace").strip()
        if "Alarm" in line:
            raise RuntimeError("GRBL in Alarm - move rejected. Unlock with $X. (%s)" % line)
        count = count + 1 if "Idle" in line else 0
        time.sleep(0.1)


def home(cnc, timeout=90):
    # $H homing cycle - GRBL goes silent until the cycle finishes, then replies
    # ok (or error:). Much longer than the usual 1s readline, hence the loop.
    cnc.reset_input_buffer()
    cnc.write(b"$H\n")
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = cnc.readline().decode(errors="replace").strip()
        if line.startswith("ok") or line.startswith("error") or "ALARM" in line:
            return line
    raise RuntimeError("no reply from homing cycle after %ds" % timeout)


def sleep(cnc):
    # $SLP -> GRBL sleep state: de-energizes the steppers until woken
    cnc.write(b"$SLP\n")
    time.sleep(0.2)
    cnc.reset_input_buffer()


def wake(cnc):
    # soft reset out of sleep, clear the alarm; position survives (no re-home)
    cnc.write(b"\x18")            # soft reset
    cnc.flush()
    time.sleep(1)
    cnc.reset_input_buffer()
    cnc.write(b"$X\n")           # clear the post-reset alarm
    cnc.readline()
    cnc.write(b"G90\n")
    cnc.readline()


def start(cnc):
    # one-time startup: clean state, then hold steppers energized so position is kept
    wake(cnc)                    # soft reset, unlock, G90
    cnc.write(b"$1=255\n")       # step idle delay 255 = never de-energize on idle
    cnc.readline()


def jog(cnc, axis, dist, feed=1000):
    # $J is GRBL's jog command - relative, and it leaves the modal state alone
    cnc.write(("$J=G91 %s%.3f F%d\n" % (axis, dist, feed)).encode())
    cnc.readline()
    wait_idle(cnc)


def send_goto(cnc, x, y):
    # Start a rapid and return immediately. Used when two carriages have to run
    # at once; anything else should use goto(), which waits.
    cnc.write(("G90 G0 X%.3f Y%.3f\n" % (x, y)).encode())


def state(cnc, timeout=1.0):
    # Current state word from a status report - Idle / Run / Alarm / Jog / Hold.
    # None if it never answered. Unlike wait_idle this returns straight away, so
    # a move can be checked for rejection without waiting for it to finish.
    deadline = time.time() + timeout
    while time.time() < deadline:
        cnc.reset_input_buffer()
        cnc.write(b"?")
        line = cnc.readline().decode(errors="replace").strip()
        m = re.match(r"<([A-Za-z]+)", line)
        if m:
            return m.group(1)
        time.sleep(0.05)
    return None


def goto(cnc, x, y):
    # rapid straight to a known position (from positions.json)
    send_goto(cnc, x, y)
    wait_idle(cnc)
