import time

# Protocol for the carpark Teensy's UART link. carpark owns the sequencing and
# the sensor thresholds; the Pi asks for reports ('h'/'p'/'a'/'c'), outcomes
# ('u'/'d'/'t'/'g'), or forwards a debug line to the car board with '>'.


def send_car(uart, cmd, ms=None, report=print):
    """Forward a debug command to the car board and report its ack.

    ms runs it as a timed burst: carpark drives for that long, then brakes and
    releases, and answers "BURST <ms>ms" when it is done. The timing is done on
    carpark rather than here on purpose - it is the calibration tool for
    driveToDepth, so it walks the same path without the Pi link in the middle.
    """
    uart.reset_input_buffer()
    uart.write((">" + cmd + (" %d" % ms if ms else "") + "\n").encode())
    if not ms:
        for _ in range(30):
            reply = uart.readline().decode(errors="replace").strip()
            if reply:
                report(reply)
                break
        return
    # burst: echo progress until carpark confirms the burst finished
    deadline = time.time() + ms / 1000.0 + 5.0
    while time.time() < deadline:
        reply = uart.readline().decode(errors="replace").strip()
        if not reply:
            continue
        report(reply)
        if reply.startswith("BURST "):
            return
    report("no BURST confirmation - is carpark on the current firmware?")


def drive(uart, cmd):
    # f/r run until stopped; s stops all motors
    uart.write({"f": b"f", "r": b"b", "s": b"s"}[cmd])


def read_trigger(uart, cmd, tag, timeout=2.0):
    # carpark owns these triggers and reports state: "h" -> "HOME 0|1", "p" -> "BIN 0|1"
    uart.reset_input_buffer()
    uart.write(cmd.encode())
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = uart.readline().decode(errors="replace").strip()
        if line.startswith(tag + " "):
            p = line.split()          # "<TAG> <trig> <value> [extra diagnostics]"
            return (p[1] == "1",
                    int(p[2]) if len(p) > 2 else None,
                    p[3:])
    raise RuntimeError("no '%s' reply from carpark for command '%s' - is carpark "
                       "running the current firmware?" % (tag, cmd))


def read_home(uart):
    return read_trigger(uart, "h", "HOME")


def read_bin(uart):
    return read_trigger(uart, "p", "BIN")


def read_align(uart):
    return read_trigger(uart, "a", "ALIGN")


def read_distance(uart, timeout=5.0):
    # car depth in the lane (mm) from carpark's laser rangefinder; None = no
    # reading. A read takes up to RANGE_WAIT_MS (1.5s) on the firmware side.
    uart.reset_input_buffer()
    uart.write(b"m")
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = uart.readline().decode(errors="replace").strip()
        if line.startswith("DIST "):
            mm = int(line[5:])
            return None if mm < 0 else mm
    return None


def read_barcode(uart, timeout=8.0, fresh=True):
    """Barcode from the scanner; None = nothing seen.

    fresh=True ('Q') triggers a new scan and waits SCAN_WAIT_MS for it.
    fresh=False ('q') just reports what carpark already has - which is what a
    depth-1 move leaves behind, since that arms the scanner on the way in and
    waits out its window before finishing. No second scan needed.
    """
    uart.reset_input_buffer()
    uart.write(b"Q" if fresh else b"q")
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = uart.readline().decode(errors="replace").strip()
        if line.startswith("BC "):
            code = line[3:]
            return None if code == "-" else code
    return None


def send_laser(uart, cmd, timeout=2.0, report=print):
    # raw passthrough to the rangefinder for protocol bring-up; report what it says
    uart.reset_input_buffer()
    uart.write(("<" + cmd + "\n").encode())
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = uart.readline().decode(errors="replace").strip()
        if line:
            report(line)


def read_current(uart, timeout=2.0):
    # spot current reading (mA) from carpark's INA219
    uart.reset_input_buffer()
    uart.write(b"c")
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = uart.readline().decode(errors="replace").strip()
        if line.startswith("I "):
            return float(line[2:])
    return None


# How long to wait for each sequence. These MUST exceed carpark's own timeout
# for that work, or the Pi gives up while the machine is still moving - and
# carpark's late DONE then turns up in the next command's replies. Derived from
# carpark1.ino: LIFT_TIMEOUT_MS 7s (x2 motors), DRIVE_TIMEOUT_MS 10s,
# DEPTH_TIMEOUT_MS 15s, SCAN_WAIT_MS 3s.
TIMEOUTS = {
    "u": 20.0, "d": 20.0,              # two lift motors at 7s each + kick/settle
    "l": 15.0,                         # driveToBin, 10s
    "g": 15.0,                         # driveToHome, 10s
    "t": 45.0,                         # driveToBin + both lifts + driveToHome
    "1": 20.0, "2": 20.0, "3": 20.0,   # driveToDepth, 15s
}
DEFAULT_TIMEOUT = 30.0


def run_sequence(uart, cmd, timeout=None, report=None):
    """Ask carpark for a high-level action and echo its progress until it ends.

    carpark owns the sequencing; we just report. Terminal lines are DONE / FAIL.
    timeout: None picks the per-command value from TIMEOUTS.
    report: callback for each progress line (default: print to the shell).
    """
    if timeout is None:
        timeout = TIMEOUTS.get(cmd, DEFAULT_TIMEOUT)
    say = report or (lambda l: print("  " + l))
    uart.reset_input_buffer()
    uart.write(cmd.encode())
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = uart.readline().decode(errors="replace").strip()
        if not line:
            continue
        say(line)
        if "DONE" in line or "FAIL" in line:
            return "DONE" in line
    say("timed out waiting for carpark after %gs (command '%s')" % (timeout, cmd))
    return False
