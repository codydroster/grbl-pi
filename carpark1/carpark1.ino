#include <Wire.h>
#include <Adafruit_INA219.h>   // ASSUMES INA219 - swap this lib + readCurrent() for another chip

Adafruit_INA219 ina219;

const int SENSOR_PIN   = 22;  // alignment sensor (A8)
const int CAR_HOME_PIN = 23;  // car-home sensor (A9), same type
const int OUTPUT_PIN = 2;    // trigger output -> Pi GPIO (5 on carriage 1, 6 on carriage 2)
const int BAUD_RATE  = 115200;
const int SAMPLE_MS  = 20;   // idle loop period

// Lift control: drive each motor until its current spikes (hits its end stop).
const float    CURRENT_SPIKE   = 80.0;   // mA - stall threshold (TUNE on hardware)
const int      BREAKFREE_SPEED = 175;    // initial kick to break stiction
const int      RUN_SPEED       = 100;    // speed after the kick
const uint32_t BREAKFREE_MS    = 250;    // kick duration (also covers startup inrush)
const uint32_t SETTLE_MS       = 200;    // after the kick, let running current settle before watching
const uint32_t LIFT_TIMEOUT_MS = 7000;   // safety: give up on a lift move after this

// FIXED THRESHOLDS - no baselines, no drift. Both boards use the stock 10-bit
// ADC, so every reading and threshold here is on one 0-1023 scale.
// All three are pullup + phototransistor: no reflection floats HIGH, reflection
// pulls LOW. So MORE REFLECTION = LOWER reading and every threshold below reads
// "below this = seen".
// THE TARGET HAS TO REFLECT IR. A matte black surface absorbs it and the
// reading never drops, which looks exactly like a broken sensor or a wiring
// fault - it is neither. Light-coloured target or reflective tape.
const int ALIGN_LEVEL = 375;   // alignment below this = upright seen
const int BIN_LEVEL   = 650;   // bin sensor below this = bin present
const int HOME_LEVEL  = 100;   // home below this = docked
const int HOME_SLOW   = 200;   // home below this = close, ease off

const int      DRIVE_SPEED     = 250;    // fast approach
const int      CREEP_SPEED     = 110;    // lowest speed with enough torque to
                                         // actually move the car (50 stalled)
const uint32_t BIN_OVERRUN_MS  = 300;    // keep creeping this long after the bin trigger
const uint32_t DRIVE_TIMEOUT_MS = 10000; // safety: give up on a drive after this

// Depth positions in a lane, as the rangefinder sees them (mm from carpark).
// Depth 1 is the bin closest to the carpark, 3 is the deepest. The carriage
// picks col,row; these pick how far the car drives into that lane.
// IMPORTANT: the rangefinder ranges off the BIN, not the car - an empty car
// gives no reading at all. So depth moves are only valid with a bin aboard
// (placing one). Going in empty to collect one must use driveToBin instead.
const int DEPTH_MM[3] = {230, 430, 630};
// DEPTH 1 IS ALSO THE SCANNER POSITION. The fixed scanner's usable window
// covers depth 1, so a bin being carried is presented for reading by driving to
// depth 1 - there is no separate read position and no 'r' command. Sequences
// that need a label read send '1' like any other depth move.
// TWO-PHASE POSITIONING: timed run in, laser-guided creep to finish.
//
// PHASE 1 covers the distance blind, at DRIVE_SPEED, on the clock. The drive is
// self-locking - cutting power stops the car dead, no coast - so distance
// really is speed x time. It aims at the near edge of the creep zone, not at
// the target, so it always hands over short.
// PHASE 2 creeps at DEPTH_CREEP_SPEED with the rangefinder closing the loop,
// and stops on the first reading that is inside tolerance OR past the target.
//
// Why not close the loop the whole way: a reading takes ~670ms round trip, and
// the car keeps moving throughout, so the finest a continuous loop can resolve
// is one reading's worth of travel - 86mm at DRIVE_SPEED, which cannot hit a
// +/-10mm window at all. Creeping shrinks that number instead of fighting it.
// THE CROSSING TEST IN PHASE 2 IS LOad-BEARING for the same reason: if the
// creep still covers more than the window between two readings, the car would
// step clean over it and hunt back and forth to the timeout. Stopping the
// moment a reading says "at or past" is what makes the phase terminate.
// Calibrated with the burst tool ("car AF250 <ms>", distance read before and
// after) at 800/200/100ms -> 112/23/11mm. Least squares over those three:
//
//     travel_mm = 146 * seconds - 4.8
//
// so 146mm/s with a ~5mm LOSS per burst while the car comes up to speed. The
// offset is negative, i.e. ramp-up, not brake overshoot - the gearing really
// does stop dead. That is the safe direction: short bursts fall short rather
// than jumping past, so small corrections cannot oscillate. Below ~33ms a burst
// moves nothing at all, which is the stiction floor DEPTH_MIN_BURST_MS clears.
// Load does not change these. Re-run the three bursts if gearing or speed do.
// (An earlier hand-timed ~620mm/~4.8s suggested 129mm/s; these instrument
// -measured bursts supersede it, and they measure the right thing - distance
// per COMMANDED burst, which is exactly what this loop asks for.)
const int      DRIVE_MM_PER_S = 146;
const int      DEPTH_RAMP_MM  = 5;       // add back what the ramp-up costs
// Phase 1 aims to hand over this far short of the target; phase 2 creeps the
// rest under laser guidance. Wide enough to absorb phase 1's timing error (the
// fit is good to a few mm, so 40 is generous), narrow enough that the slow
// phase stays short.
const int      DEPTH_CREEP_ZONE_MM = 40;
// UNMEASURED: 146mm/s is PWM 250; naive scaling puts PWM 80 near 47mm/s, but
// PWM-to-speed is very non-linear near stall - PWM 50 stalled outright and 110
// was the known-good creep elsewhere. This has to move the car AND cover less
// than 2 x DEPTH_TOL_MM per ~670ms reading (i.e. stay under ~30mm/s) for the
// window to be reachable; the crossing test keeps it terminating either way.
// Measure it: "car AF80 800" and read the distance before and after.
const int      DEPTH_CREEP_SPEED = 80;
const int      DEPTH_MAX_STILL = 3;      // creep readings with no movement = stalled
// A burst shorter than this may not break stiction at all. Its travel must stay
// under 2 x DEPTH_TOL_MM (here ~13mm vs a 20mm window), or a small correction
// would jump clean over the target and oscillate.
const uint32_t DEPTH_MIN_BURST_MS = 100;
const uint32_t DEPTH_BURST_BOOST_MS = 100;  // added each time a burst does not move the car
const uint32_t DEPTH_MAX_BOOST_MS = 400;    // give up escalating past this
const int      DEPTH_STALL_MM = 5;          // less movement than this = did not move
const int      DEPTH_TOL_MM  = 10;       // within this of target = arrived
const int      DEPTH_MAX_MISSES = 3;     // consecutive dropped reads before giving up
const uint32_t DEPTH_SETTLE_MS = 80;     // pause after braking, before measuring
// Budget for the whole move: the creep zone is wide (more cycles), and a miss
// costs a full RANGE_WAIT_MS retry, so 10s was tight enough to turn a couple of
// dropped reads into a timeout. carpark.TIMEOUTS on the Pi must stay above this.
const uint32_t DEPTH_TIMEOUT_MS = 15000; // safety: give up on a depth move

// DROPPING A BIN AT AN UNKNOWN DEPTH (the output lane). We cannot address a
// depth here: how many bins are already sitting there is whatever a human left,
// so the car drives in until the bin it is carrying MEETS something - the back
// of the lane, or a bin already parked - and drops it right there.
// Detection is the rangefinder: it ranges off the carried bin, so while the car
// advances the reading grows by roughly a creep-step each time, and the moment
// the bin is stopped the reading stops growing. DEPTH_STALL_MM / DEPTH_MAX_STILL
// are reused for that - "advanced less than 5mm, three readings running".
// THE WHOLE APPROACH IS AT ONE REDUCED SPEED, never DRIVE_SPEED: no distance
// into this lane can be declared safe to run in fast, because a full one can
// hold a bin as shallow as depth 1. 150 is the chosen middle ground - gentle
// enough to meet a bin with, quick enough that a deep lane does not take half a
// minute (at DEPTH_CREEP_SPEED's ~22mm/s the full lane was ~27s).
// UNMEASURED in mm/s: scaling off 146mm/s at PWM 250 suggests very roughly
// 90mm/s, i.e. ~60mm of travel per rangefinder reading, so the bin can lean on
// what it meets for up to DEPTH_MAX_STILL readings before this notices.
// Measure with "car AF150 800" and lower it if the contact looks too firm.
const int      DROP_SPEED = 150;
const uint32_t DROP_TIMEOUT_MS = 40000;

// Rangefinder: laser distance module on Serial4 (RX4 pin 16 / TX4 pin 17),
// pointed down the lane at the car - reads how deep the car has driven in.
// AA-frame hex protocol (seller doc): checksum = sum of bytes after the 0xAA
// head, mod 256. Measurement reply is a fixed 13-byte frame:
//   AA 80 00 22 | 00 04 | D1 D2 D3 D4 | S1 S2 | CS
// D1..D4 are BCD digits; read as a decimal number they ARE millimeters
// (doc: 0x12345678 = 12345.678 m). S1S2 = signal quality (bigger = better).
// Runs at 9600 (hardware-confirmed - the doc page with UART params is missing).
// 1500 is the value every working depth test ran on. Do NOT shorten it: a real
// reading round-trips in ~670ms (derived from the measured 129mm/s and the
// 86mm-per-cycle gap seen in a drive log), so the 600 it was once cut to would
// have timed out on perfectly good measurements. That cut came from an assumed
// ~400ms reading time, itself inferred from an assumed car speed - which is
// exactly how this went wrong. Only change it against a measurement.
// driveToDepth no longer drives while measuring, so this is no longer a
// distance budget - the car is stopped for every read.
const uint32_t RANGE_BAUD    = 9600;
const uint32_t RANGE_WAIT_MS = 1500;

// NO BLIND ZONE: this module reads down to ~9mm (measured). Do not add a
// minimum-range clamp - misses are not caused by the car being too close.

const uint8_t RANGE_MEASURE[] = {0xAA,0x00,0x00,0x20,0x00,0x01,0x00,0x00,0x21};

int  lastBin = -1;        // most recent value streamed from car1 (-1 = never heard from it)
bool binSeen = false;     // that value is below BIN_LEVEL

// Barcode scanner: DYscan DE2120 on Serial3, 3.3V TTL (power it from its own
// 3.3V regulator - it draws 190mA working, most of the Teensy's 3V3 budget).
// FFC pinout: 2=VCC 3=GND, 4=RXD <- TX3 pin 14, 5=TXD -> RX3 pin 15.
// Trigger is SERIAL ONLY: commands are "^_^<CMD>." and the scanner ACKs each
// with 0x06. "SCAN" starts decoding, "SLEEP" stops. Baud hardcoded to the
// DE2120 factory 115200; if scans return nothing with a code in view, try 9600.
// Codes arrive as text + CR/LF; 'Q' triggers fresh, 'q' reports the last one.
// NOTE: this unit never sends the 0x06 ACK (hardware-observed), so nothing
// waits on it - a received barcode is the real success signal.
const uint32_t SCAN_BAUD    = 115200;
const uint32_t SCAN_WAIT_MS = 3000;   // how long a triggered scan may hunt
// The scanner is triggered at the start of the run home ('R') and the carried
// bin sweeps past it en route, so there is no distance threshold to arm at.
uint32_t scanArmedAt = 0;             // millis when the scan was triggered
bool scanAck = false;                 // set by pump() if the scanner ever ACKs
char lastBarcode[32] = "";

// Report to the Pi and to the USB monitor at the same time.
void say(const char *s) {
  Serial1.println(s);
  Serial.println(s);
}

// Service the car1 link: relay its lines to the Pi and keep our copy of its
// sensor fresh. Called from inside every wait loop so sequences stay responsive.
void pump() {
  while (Serial2.available()) {
    String line = Serial2.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;
    if (line.startsWith("S ")) {        // car1's bin sensor - we own its threshold
      lastBin = line.substring(2).toInt();
      binSeen = lastBin < BIN_LEVEL;    // stream stops here; the Pi asks via 'p'
    } else {                            // acks go up to the Pi + USB monitor
      Serial1.println(line);
      Serial.print("<< car1: ");
      Serial.println(line);
    }
  }
  static char bcBuf[32];
  static int bcN = 0;
  while (Serial3.available()) {         // assemble scanner lines, non-blocking
    char ch = Serial3.read();
    if (ch == 0x06) {                   // command ACK
      scanAck = true;
    } else if (ch == '\r' || ch == '\n') {
      if (bcN) {
        bcBuf[bcN] = 0;
        strncpy(lastBarcode, bcBuf, sizeof(lastBarcode) - 1);
        Serial.print("<< scan: ");
        Serial.println(lastBarcode);
        bcN = 0;
      }
    } else if (ch >= ' ' && bcN < (int)sizeof(bcBuf) - 1) {
      bcBuf[bcN++] = ch;                // printable chars only - skip NACK etc.
    }
  }
}

// send "^_^<CMD>." to the scanner; true if it ACKs within 200ms
bool scannerCmd(const char *cmd) {
  scanAck = false;
  Serial3.print("^_^");
  Serial3.print(cmd);
  Serial3.print(".");
  uint32_t t0 = millis();
  while (millis() - t0 < 200 && !scanAck) pump();
  return scanAck;
}

bool carHome() {
  return analogRead(CAR_HOME_PIN) < HOME_LEVEL;
}

// Wait for one valid measurement frame; -1 on timeout / bad checksum.
// Pumps the car relay while waiting so the sequences stay responsive.
long rangeFrame(uint32_t waitMs) {
  uint8_t f[13];
  int n = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < waitMs) {
    pump();
    if (!Serial4.available()) continue;
    uint8_t b = Serial4.read();
    if (n == 0 && b != 0xAA) continue;          // scan for the frame head
    f[n++] = b;
    if (n < 13) continue;
    uint8_t sum = 0;
    for (int i = 1; i < 12; i++) sum += f[i];
    // measure replies echo func 0x20 (seen on hardware); reads reply 0x22
    if (sum == f[12] && (f[3] == 0x20 || f[3] == 0x22)) {
      long mm = 0;
      for (int i = 6; i <= 9; i++)              // BCD digits -> decimal = mm
        mm = mm * 100 + (f[i] >> 4) * 10 + (f[i] & 0x0F);
      return mm;
    }
    n = 0;                                      // bad frame - resync from scratch
  }
  return -1;
}

// A failed read re-initialises the UART: Serial4.begin() doubles as a port
// reset after a desync or overrun, and that recovery has proven load-bearing.
int readDistanceMM() {
  while (Serial4.available()) Serial4.read();   // drop stale bytes
  Serial4.write(RANGE_MEASURE, sizeof(RANGE_MEASURE));
  long mm = rangeFrame(RANGE_WAIT_MS);
  if (mm < 0) Serial4.begin(RANGE_BAUD);
  return (int)mm;
}

float readCurrent() {
  return ina219.getCurrent_mA();  // change this one line for a different sensor
}

// send a command line to car1 (it parses whole '\n'-terminated lines)
void car1cmd(const char *s) {
  Serial2.print(s);
  Serial2.print('\n');
}

void carSpeed(char dir, int speed) {
  char cmd[8];
  sprintf(cmd, "A%c%d", dir, speed);
  car1cmd(cmd);
}

// pump() for a fixed duration instead of delay(), so the relay keeps running
void waitPumping(uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) pump();
}

// run one car1 motor (dir 'F'/'R') until its current spikes (end stop), then stop.
// kicks at BREAKFREE_SPEED for BREAKFREE_MS to break stiction, then settles to RUN_SPEED.
void runUntilSpike(char motor, char dir) {
  char cmd[8];
  sprintf(cmd, "%c%c%d", motor, dir, BREAKFREE_SPEED);  // breakfree kick
  car1cmd(cmd);
  waitPumping(BREAKFREE_MS);                            // kick + ride out the inrush
  sprintf(cmd, "%c%c%d", motor, dir, RUN_SPEED);        // settle to running speed
  car1cmd(cmd);
  waitPumping(SETTLE_MS);                               // let running current settle

  uint32_t t0 = millis();
  while (millis() - t0 < LIFT_TIMEOUT_MS) {
    pump();
    if (readCurrent() >= CURRENT_SPIKE) break;   // stall -> done
  }
  sprintf(cmd, "%cS", motor);                // stop this motor
  car1cmd(cmd);
}

void liftUp()   { runUntilSpike('B', 'R'); runUntilSpike('C', 'R'); say("UP DONE"); }
void liftDown() { runUntilSpike('B', 'F'); runUntilSpike('C', 'F'); say("DOWN DONE"); }

// Drive in until the bin sensor sees a bin, ease off, run a touch further, stop.
bool driveToBin() {
  carSpeed('F', DRIVE_SPEED);
  uint32_t t0 = millis();
  while (millis() - t0 < DRIVE_TIMEOUT_MS) {
    pump();
    if (binSeen) {
      carSpeed('F', CREEP_SPEED);        // ease off
      waitPumping(BIN_OVERRUN_MS);       // seat it
      car1cmd("AS");
      return true;
    }
  }
  car1cmd("AS");
  return false;
}

// Creep in until the carried bin stops advancing - it has met the back of the
// lane or a bin already parked there - and stop on the spot so it can be set
// down. For the output lane, where what is already in there is unknown.
// Never goes deeper than the deepest taught slot, so an empty lane still stops.
// Returns false only if it never got a usable reading or ran out of time; a
// blocked bin IS the success case here.
bool driveUntilBlocked() {
  uint32_t t0 = millis();
  int misses = 0, still = 0, lastD = -1;
  carSpeed('F', DROP_SPEED);
  while (millis() - t0 < DROP_TIMEOUT_MS) {
    int d = readDistanceMM();                  // car keeps creeping through this
    if (d < 0) {
      if (++misses >= DEPTH_MAX_MISSES) {
        car1cmd("AB"); waitPumping(DEPTH_SETTLE_MS); car1cmd("AS");
        say("lost the rangefinder while feeling for the drop point");
        return false;
      }
      continue;
    }
    misses = 0;
    Serial1.print("c "); Serial1.println(d);
    Serial.print("c ");  Serial.println(d);

    if (d >= DEPTH_MM[2]) {                    // empty lane - stop at the back
      car1cmd("AB"); waitPumping(DEPTH_SETTLE_MS); car1cmd("AS");
      say("reached the back of the lane");
      return true;
    }
    // Advancing by less than a creep-step means the bin has met something.
    if (lastD >= 0 && (d - lastD) < DEPTH_STALL_MM) {
      if (++still >= DEPTH_MAX_STILL) {
        car1cmd("AB"); waitPumping(DEPTH_SETTLE_MS); car1cmd("AS");
        Serial1.print("bin stopped at "); Serial1.println(d);
        Serial.print("bin stopped at ");  Serial.println(d);
        return true;
      }
    } else {
      still = 0;
    }
    lastD = d;
  }
  car1cmd("AB"); car1cmd("AS");
  say("timed out feeling for the drop point");
  return false;
}

// Drive the car to a measured depth in the lane. ONE timed burst at DRIVE_SPEED
// covers the bulk of the distance, aiming at the near edge of the creep zone,
// then the car creeps at DEPTH_CREEP_SPEED with the rangefinder closing the
// loop until it is inside tolerance or past the target.
//
// Deliberately a SINGLE burst, not a loop of them. The timing model overshoots
// its welcome on long runs - a measured 1103ms burst travelled 140mm where the
// model said 156 (127mm/s effective against the 146 the short bursts fit), so a
// looping phase 1 just fired a second little burst to make up the difference.
// The creep absorbs that error perfectly well and is the part that actually
// knows where the car is, so there is nothing for a second burst to add.
// Progress lines: "d <mm>" for the run in, "c <mm>" while creeping.
bool driveToDepth(int targetMm, int tolMm) {
  uint32_t t0 = millis();
  int misses = 0;
  int lastD = -1;
  int still = 0;
  int entryErr = 0;                              // error when the creep started
  bool creeping = false;
  bool ranIn = false;                            // the one timed burst is done

  while (millis() - t0 < DEPTH_TIMEOUT_MS) {
    int d = readDistanceMM();
    if (d < 0) {
      // A dropped frame is not a failure - ask again. (No DONE/FAIL in this
      // text - the Pi stops reading at the first line containing either.)
      if (++misses >= DEPTH_MAX_MISSES) {
        if (creeping) { car1cmd("AB"); waitPumping(DEPTH_SETTLE_MS); car1cmd("AS"); }
        say(creeping ? "lost the rangefinder during the creep"
                     : "no usable reading from the rangefinder");
        return false;
      }
      continue;
    }
    misses = 0;
    Serial1.print(creeping ? "c " : "d "); Serial1.println(d);
    Serial.print(creeping ? "c " : "d ");  Serial.println(d);
    int err = targetMm - d;                      // + = deeper, - = too deep


    if (!creeping) {
      if (abs(err) <= tolMm) return true;        // already there, still stopped
      if (!ranIn && abs(err) > DEPTH_CREEP_ZONE_MM) {
        ranIn = true;
        // distance -> time, adding back the ramp-up loss. Aim at the near edge
        // of the creep zone, so this always hands over short of the target.
        uint32_t want = (uint32_t)(abs(err) - DEPTH_CREEP_ZONE_MM);
        uint32_t ms = (want + DEPTH_RAMP_MM) * 1000 / DRIVE_MM_PER_S;
        if (ms < DEPTH_MIN_BURST_MS) ms = DEPTH_MIN_BURST_MS;
        carSpeed(err > 0 ? 'F' : 'R', DRIVE_SPEED);
        waitPumping(ms);
        car1cmd("AB");                           // brake, settle, then release
        waitPumping(DEPTH_SETTLE_MS);
        car1cmd("AS");
      }
      // Straight into the creep. entryErr keeps the sign from BEFORE the burst,
      // which is still right because the burst always aims short - and if the
      // model ever did overshoot, the crossing test below catches it on the
      // very first creep reading and stops.
      creeping = true;
      entryErr = err;
      lastD = -1;                                // the burst moved us; no stall compare
      carSpeed(err > 0 ? 'F' : 'R', DEPTH_CREEP_SPEED);
      continue;
    }
    // Inside tolerance, or stepped past the target. The crossing half matters:
    // the car moves for the whole of a reading, so it can pass clean over the
    // window between two of them - without this it would hunt to the timeout.
    if (abs(err) <= tolMm || (err > 0) != (entryErr > 0)) {
      car1cmd("AB"); waitPumping(DEPTH_SETTLE_MS); car1cmd("AS");
      return true;
    }

    // Creeping but not actually moving: DEPTH_CREEP_SPEED is below what this
    // car needs to break stiction. Say so rather than grinding to the timeout.
    if (lastD >= 0 && abs(d - lastD) < DEPTH_STALL_MM) {
      if (++still >= DEPTH_MAX_STILL) {
        car1cmd("AB"); waitPumping(DEPTH_SETTLE_MS); car1cmd("AS");
        say("creep speed too low to move the car - raise DEPTH_CREEP_SPEED");
        return false;
      }
    } else {
      still = 0;
    }
    lastD = d;
  }
  car1cmd("AB"); car1cmd("AS");
  say("timed out during the creep");
  return false;
}

// Reverse until the car is docked, easing off as it comes into range.
bool driveToHome() {
  carSpeed('R', DRIVE_SPEED);
  bool crept = false;
  uint32_t t0 = millis();
  while (millis() - t0 < DRIVE_TIMEOUT_MS) {
    pump();
    int raw = analogRead(CAR_HOME_PIN);
    if (raw < HOME_LEVEL) { car1cmd("AS"); return true; }
    if (!crept && raw < HOME_SLOW) { carSpeed('R', CREEP_SPEED); crept = true; }
  }
  car1cmd("AS");
  return false;
}

// The whole retrieve: in, grab, out. One command from the Pi.
void retrieve() {
  say("RETRIEVE START");
  if (!driveToBin())  { say("RETRIEVE FAIL no bin");   return; }
  say("RETRIEVE bin reached");
  liftUp();
  if (!driveToHome()) { say("RETRIEVE FAIL not home"); return; }
  say("RETRIEVE DONE");
}

// Just the front half of retrieve: drive in until the bin sensor finds a bin,
// then stop there. No lift, no return trip. retrieve() calls driveToBin()
// directly rather than going through here, so a sequence only ever emits one
// terminal DONE/FAIL line - the Pi stops reading at the first one it sees.
void locateBin() {
  say("LOCATE START");
  say(driveToBin() ? "LOCATE DONE" : "LOCATE FAIL no bin");
}

// Commands from the Pi (or USB serial for debugging). Actions are high level -
// carpark owns the sequencing, the Pi just asks for the outcome.
//   f/b/s  drive forward / back / stop all motors
//   u/d    lifts up / down
//   t      retrieve: in, detect bin, lift, reverse, home
//   l      locate bin: in, detect bin, stop (retrieve without the lift/return)
//   g      go home: reverse until docked
//   h/p/a/c  report home sensor / bin sensor / alignment sensor / current
//   1/2/3  drive the car to that taught depth in the lane (1 = closest).
//          Never scans - a depth-1 move is often just shelving a bin there.
//   R      go home while reading the carried bin's label - the bin passes the
//          scanner on the way, so this costs nothing over a plain 'g'.
//          -> "BC <code|->" then READ DONE/FAIL. Replaces g in a sequence.
//   P      place: creep in until the carried bin meets whatever is already in
//          the lane (or the back of it) and stop there, for an unknown depth
//   m      one rangefinder reading -> "DIST <mm>" (-1 = no reading)
//   q      last scanned barcode -> "BC <code>" ("-" = none yet)
//   Q      trigger a fresh scan, wait for it, -> "BC <code|->"
//   >...\n forward the rest of the line to car1 (debug passthrough).
//          An optional trailing ms runs it as a timed burst then brakes:
//          ">AF250 800" = forward at 250 for 800ms. Replies "BURST <ms>ms".
//   <...\n hex bytes to the rangefinder, echo its reply as hex (bring-up)
void handleCommand(Stream &port) {
  char c = port.read();
  if (c == '>') {                       // forward rest of line to car1
    char buf[40];
    int n = port.readBytesUntil('\n', buf, sizeof(buf) - 1);
    buf[n] = 0;
    while (n > 0 && (buf[n-1] == '\r' || buf[n-1] == ' ')) buf[--n] = 0;

    // Optional trailing duration: ">AF250 800" drives for 800ms, then brakes
    // and releases. TIMED HERE, not on the Pi: this is the calibration tool for
    // driveToDepth's bursts, so it has to walk the same path - same waitPumping,
    // same brake-settle-release - with no Pi link latency or host jitter in the
    // middle. A Python sleep would measure a different thing.
    uint32_t ms = 0;
    char *sp = strchr(buf, ' ');
    if (sp) { *sp = 0; ms = atoi(sp + 1); }

    Serial.print(">> sent: "); Serial.println(buf);
    Serial2.print(buf); Serial2.print('\n');

    if (ms) {
      waitPumping(ms);
      char tail[4];
      sprintf(tail, "%cB", buf[0]); car1cmd(tail);   // brake
      waitPumping(DEPTH_SETTLE_MS);                  // same settle as driveToDepth
      sprintf(tail, "%cS", buf[0]); car1cmd(tail);   // release
      Serial1.print("BURST "); Serial1.print(ms); Serial1.println("ms");
      Serial.print("BURST ");  Serial.print(ms);  Serial.println("ms");
    }
  }
  else if (c == '<') {                  // hex to rangefinder: <aa 80 00 22 a2
    char buf[48];
    int n = port.readBytesUntil('\n', buf, sizeof(buf));
    uint8_t out[16];
    int on = 0, hi = -1;
    for (int i = 0; i < n && on < (int)sizeof(out); i++) {
      int v = -1;
      if (buf[i] >= '0' && buf[i] <= '9') v = buf[i] - '0';
      else if (buf[i] >= 'a' && buf[i] <= 'f') v = buf[i] - 'a' + 10;
      else if (buf[i] >= 'A' && buf[i] <= 'F') v = buf[i] - 'A' + 10;
      else continue;                    // spaces etc. separate the pairs
      if (hi < 0) hi = v;
      else { out[on++] = (hi << 4) | v; hi = -1; }
    }
    Serial4.write(out, on);
    uint32_t t0 = millis();
    char hex[4];
    while (millis() - t0 < RANGE_WAIT_MS) {
      pump();
      while (Serial4.available()) {
        sprintf(hex, "%02X ", Serial4.read());
        Serial1.print(hex);
        Serial.print(hex);
      }
    }
    Serial1.println();
    Serial.println();
  }
  // --- actions ---
  else if (c == 'f') carSpeed('F', DRIVE_SPEED);   // standalone, runs until stopped
  else if (c == 'b') carSpeed('R', DRIVE_SPEED);
  else if (c == 's') { car1cmd("AS"); car1cmd("BS"); car1cmd("CS"); }
  else if (c == 'u') liftUp();
  else if (c == 'd') liftDown();
  else if (c == 't') retrieve();
  else if (c == 'l') locateBin();       // retrieve's front half, no lift
  else if (c == 'g') say(driveToHome() ? "HOME DONE" : "HOME FAIL");
  // --- reports ---
  else if (c == 'h') {
    port.print("HOME ");  port.print(carHome() ? 1 : 0);
    port.print(" ");      port.println(analogRead(CAR_HOME_PIN));
  } else if (c == 'p') {
    port.print("BIN ");   port.print(binSeen ? 1 : 0);
    port.print(" ");      port.println(lastBin);
  } else if (c == 'a') {
    int raw = analogRead(SENSOR_PIN);
    port.print("ALIGN "); port.print(raw < ALIGN_LEVEL ? 1 : 0);
    port.print(" ");      port.println(raw);
  } else if (c == 'c') {
    port.print("I "); port.println(readCurrent(), 1);
  } else if (c == 'm') {
    port.print("DIST "); port.println(readDistanceMM());
  } else if (c >= '1' && c <= '3') {    // drive the car to taught depth 1/2/3
    int depth = c - '0';
    say("DEPTH START");
    // NO SCANNING HERE. A depth-1 move is just as likely to be SHELVING a bin
    // at depth 1 as presenting one to be read, and firing the scanner then
    // would leave a stale or wrong code in lastBarcode. Reading is opt-in: the
    // caller asks for it with 'R'.
    say(driveToDepth(DEPTH_MM[depth - 1], DEPTH_TOL_MM) ? "DEPTH DONE"
                                                               : "DEPTH FAIL");
  } else if (c == 'P') {                // place: creep in until the bin is stopped
    say("PLACE START");
    say(driveUntilBlocked() ? "PLACE DONE" : "PLACE FAIL");
  } else if (c == 'R') {                // go home, reading the label en route
    say("READ START");
    // NO MOVE OF ITS OWN. The car has to return home anyway, and the bin it is
    // carrying sweeps straight past the fixed scanner on the way - so the scan
    // is free. Trigger before setting off and the code is usually decoded
    // before the car is even docked; the wait below then falls straight
    // through. Nothing stops, and the separate gohome step disappears.
    lastBarcode[0] = 0;
    scannerCmd("SCAN");
    scanArmedAt = millis();
    bool ok = driveToHome();
    // Anything left of the scan window, in case the run home was very short.
    while (millis() - scanArmedAt < SCAN_WAIT_MS && !lastBarcode[0]) pump();
    scannerCmd("SLEEP");
    // Before the terminal line: the Pi stops reading at the first DONE/FAIL.
    Serial1.print("BC "); Serial1.println(lastBarcode[0] ? lastBarcode : "-");
    Serial.print("BC ");  Serial.println(lastBarcode[0] ? lastBarcode : "-");
    say(ok ? "READ DONE" : "READ FAIL");
  } else if (c == 'q') {
    port.print("BC "); port.println(lastBarcode[0] ? lastBarcode : "-");
  } else if (c == 'Q') {                // fresh scan, triggered over serial
    lastBarcode[0] = 0;
    scannerCmd("SCAN");
    uint32_t t0 = millis();
    while (millis() - t0 < SCAN_WAIT_MS && !lastBarcode[0]) pump();
    scannerCmd("SLEEP");
    port.print("BC "); port.println(lastBarcode[0] ? lastBarcode : "-");
  }
  // anything else is ignored
}

void setup() {
  Serial.begin(BAUD_RATE);
  Serial1.begin(9600);    // Pi UART link (RX1 pin 0 / TX1 pin 1)
  Serial2.begin(115200);  // car1 link (RX2/TX2)
  Serial4.begin(RANGE_BAUD);  // laser rangefinder (RX4 pin 16 / TX4 pin 17)
  Serial3.begin(SCAN_BAUD);  // barcode scanner (RX3 pin 15 / TX3 pin 14)
  Serial.setTimeout(50);   // bound readBytesUntil for the > forward
  Serial1.setTimeout(50);
  Serial2.setTimeout(50);  // bound readStringUntil on the car1 relay
  // ADC left at the stock 10-bit (0-1023) so carpark and car1 share one scale

  if (!ina219.begin()) Serial.println("INA219 not found");  // current sensor for lift stalls

  // Each phototransistor needs a load resistor to turn its photocurrent into a
  // voltage. INPUT_PULLUP uses the Teensy's internal ~22k; INPUT_DISABLE means
  // an EXTERNAL resistor supplies it. These must match the actual wiring:
  //   external resistor fitted  -> INPUT_DISABLE  (internal pullup would sit in
  //                                parallel: 22k||100k = 18k, swamping it)
  //   no external resistor      -> INPUT_PULLUP   (INPUT_DISABLE floats the pin)
  pinMode(SENSOR_PIN, INPUT_PULLUP);      // alignment: internal pullup
  pinMode(CAR_HOME_PIN, INPUT_PULLUP);    // home: internal pullup (narrow band -
                                          // best candidate for an external 100k)
  pinMode(OUTPUT_PIN, OUTPUT);

  Serial.println("carpark ready");
}

void loop() {
  // drain the whole buffer each loop so multi-char commands forward in one burst
  while (Serial.available())  handleCommand(Serial);
  while (Serial1.available()) handleCommand(Serial1);

  pump();   // relay car1 and keep lastBin fresh

  // alignment sensor -> trigger output to the Pi
  digitalWrite(OUTPUT_PIN, analogRead(SENSOR_PIN) < ALIGN_LEVEL ? HIGH : LOW);

  delay(SAMPLE_MS);
}
