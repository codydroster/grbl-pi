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
const int DEPTH_MM[3] = {235, 430, 630};
// Where the car sits for the fixed scanner to read the label on a bin it is
// carrying. The scanner's usable window is wide enough to cover depth 1 too, so
// a bin just picked from the front of a lane can be read without moving at all.
const int      BARCODE_MM     = 200;     // ideal read distance
const int      BARCODE_TOL_MM = 20;      // readable band extends this far past
                                         // BARCODE_MM and past depth 1
// TIMED POSITIONING. The drive is self-locking - cutting power stops the car
// dead, no coast - so distance really is speed x time, and a computed burst
// positions far more finely than closed-loop laser control ever could. A laser
// reading takes ~670ms round trip, during which the car covers ~86mm at
// DRIVE_SPEED, so a pure read-decide-drive loop can only see the world in 86mm
// steps and cannot hit a +/-10mm window no matter how it is tuned.
// Measured by the user: ~620mm of rack in ~4.8s at DRIVE_SPEED, and the figure
// does NOT change with a loaded bin. Re-measure if the gearing or speed change.
const int      DRIVE_MM_PER_S = 129;
// Aim deliberately SHORT of the target on every pass, so the car always closes
// in from one side instead of overshooting and hunting back and forth.
const int      DEPTH_APPROACH_PCT = 85;
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

// Drive the car to a measured depth in the lane: measure stopped, drive a
// computed burst, stop, measure again. Two or three passes land inside the
// tolerance, each aiming DEPTH_APPROACH_PCT of the way so the car converges
// from one side rather than hunting.
//
// EVERY READING IS TAKEN WITH THE CAR STOPPED, which is the condition the
// rangefinder is reliable in - misses happen while it is moving. That is the
// real prize here, not just the finer positioning.
bool driveToDepth(int targetMm, int tolMm) {
  uint32_t t0 = millis();
  int misses = 0;
  int lastD = -1;                                // reading before the last burst
  uint32_t boost = 0;                            // extra burst time when nothing moved
  while (millis() - t0 < DEPTH_TIMEOUT_MS) {
    int d = readDistanceMM();                    // car is stopped here, always
    if (d < 0) {
      // A dropped frame is not a failure - ask again. The car is already
      // stopped, so a miss costs time but no travel. (No DONE/FAIL in this
      // text - the Pi stops reading at the first line containing either.)
      if (++misses >= DEPTH_MAX_MISSES) {
        say("no usable reading from the rangefinder");
        return false;
      }
      continue;
    }
    misses = 0;
    Serial1.print("d "); Serial1.println(d);     // progress, echoed by the Pi
    Serial.print("d ");  Serial.println(d);
    int err = targetMm - d;                      // + = deeper, - = too deep
    if (abs(err) <= tolMm) return true;          // already stopped - just done

    // Did the last burst actually move the car? If not, stiction won: lengthen
    // the next one rather than sitting here buzzing at it forever.
    if (lastD >= 0 && abs(d - lastD) < DEPTH_STALL_MM) {
      boost += DEPTH_BURST_BOOST_MS;
      if (boost > DEPTH_MAX_BOOST_MS) {
        say("car is not moving - stiction, an obstruction, or a jammed bin");
        return false;
      }
    } else {
      boost = 0;
    }
    lastD = d;

    // distance -> time. Aim short by DEPTH_APPROACH_PCT so we never overshoot.
    uint32_t ms = (uint32_t)abs(err) * DEPTH_APPROACH_PCT * 10 / DRIVE_MM_PER_S;
    if (ms < DEPTH_MIN_BURST_MS) ms = DEPTH_MIN_BURST_MS;
    ms += boost;

    carSpeed(err > 0 ? 'F' : 'R', DRIVE_SPEED);
    waitPumping(ms);
    car1cmd("AB");                               // brake, settle, then release
    waitPumping(DEPTH_SETTLE_MS);
    car1cmd("AS");
  }
  car1cmd("AB"); car1cmd("AS");
  return false;
}

// Put a carried bin where the scanner can read it. Anything from just short of
// BARCODE_MM out to just past depth 1 reads fine, so if the car is already in
// that band - which it is straight after locateBin - don't move at all. Saves a
// move, and avoids nudging a bin that is already sitting correctly on the forks.
bool driveToReadPos() {
  const int lo = BARCODE_MM - BARCODE_TOL_MM;    // whole band the scanner reads
  const int hi = DEPTH_MM[0] + BARCODE_TOL_MM;
  int d = readDistanceMM();
  // locateBin has just parked the car at a bin, so it is already about right -
  // a missing reading is no reason to go driving. Scan from where we are.
  if (d < 0) {
    say("no usable distance - scanning from where we are");
    return true;
  }
  if (d >= lo && d <= hi) {
    Serial1.print("already readable at "); Serial1.println(d);
    Serial.print("already readable at ");  Serial.println(d);
    return true;
  }
  // Aim at the middle of the band with the BAND's tolerance, not the tight
  // depth one. Anywhere in here reads, so there is nothing to be gained by
  // converging harder - and a narrow window is what makes the car hunt.
  return driveToDepth((lo + hi) / 2, (hi - lo) / 2);
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
//   1/2/3  drive the car to that taught depth in the lane (1 = closest)
//   r      drive to BARCODE_MM so the scanner can read a carried bin
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
  else if (c == 'r') {                  // present a carried bin to the scanner
    say("READPOS START");
    say(driveToReadPos() ? "READPOS DONE" : "READPOS FAIL");
  }
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
    say(driveToDepth(DEPTH_MM[depth - 1], DEPTH_TOL_MM) ? "DEPTH DONE"
                                                        : "DEPTH FAIL");
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
