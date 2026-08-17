#define RPR 14   // single reflectance sensor

#define SENSOR_INTERVAL_MS 50

bool sensorStreaming = true;  // stream reflectance to carpark (not to USB)

struct Motor {
  uint8_t pwm, bin1, bin2, stby;
  int speed;
};

Motor motorA = { 23, 21, 22, 20, 0 };  // drive
Motor motorB = {  0,  2,  1,  3, 0 };  // lift 1 (shares STBY pin 3 with motorC)
Motor motorC = {  6,  4,  5,  3, 0 };  // lift 2

void motorForward(Motor &m, int speed) {
  digitalWrite(m.bin1, HIGH);
  digitalWrite(m.bin2, LOW);
  analogWrite(m.pwm, speed);
  m.speed = speed;
}

void motorReverse(Motor &m, int speed) {
  digitalWrite(m.bin1, LOW);
  digitalWrite(m.bin2, HIGH);
  analogWrite(m.pwm, speed);
  m.speed = speed;
}

void motorBrake(Motor &m) {
  digitalWrite(m.bin1, HIGH);
  digitalWrite(m.bin2, HIGH);
  analogWrite(m.pwm, 255);
  m.speed = 0;
}

void motorStop(Motor &m) {
  analogWrite(m.pwm, 0);
  m.speed = 0;
}

void motorCoast(Motor &m, int stepDelay = 10) {
  for (int s = m.speed; s >= 0; s--) {
    analogWrite(m.pwm, s);
    delay(stepDelay);
  }
  m.speed = 0;
}

void initMotor(Motor &m) {
  pinMode(m.pwm,  OUTPUT);
  pinMode(m.bin1, OUTPUT);
  pinMode(m.bin2, OUTPUT);
  pinMode(m.stby, OUTPUT);
  digitalWrite(m.stby, HIGH);
}

void printHelp(Stream &port) {
  port.println("Commands:");
  port.println("  A/B/C + F<0-255>  Forward at speed");
  port.println("  A/B/C + R<0-255>  Reverse at speed");
  port.println("  A/B/C + B         Brake");
  port.println("  A/B/C + S         Stop");
  port.println("  A/B/C + C<ms>     Coast to stop (step delay ms)");
  port.println("  ?                 Show this help");
  port.println("  X                 Toggle sensor stream");
  port.println("  Q                 Report one reflectance reading");
}

void handleCommand(Stream &port) {
  // read one whole command line so back-to-back commands don't bleed together
  // (a value-less command like "AS" used to make parseInt eat the next command)
  char line[16];
  int n = port.readBytesUntil('\n', line, sizeof(line) - 1);
  line[n] = 0;
  if (n == 0) return;

  char which = line[0];
  Motor *m = nullptr;

  if      (which == '?')                 { printHelp(port); return; }
  else if (which == 'Q' || which == 'q') {   // one-shot reading, even when parked
    port.printf("S %d\n", analogRead(RPR));
    return;
  }
  else if (which == 'X' || which == 'x') {
    sensorStreaming = !sensorStreaming;
    port.printf("Sensor stream %s\n", sensorStreaming ? "on" : "off");
    return;
  }
  else if (which == 'A' || which == 'a') m = &motorA;
  else if (which == 'B' || which == 'b') m = &motorB;
  else if (which == 'C' || which == 'c') m = &motorC;
  else { port.print("car1: unknown '"); port.print(which); port.println("'"); return; }

  char cmd = (n > 1) ? line[1] : 0;
  int  val = (n > 2) ? atoi(line + 2) : 0;

  switch (cmd) {
    case 'F': case 'f':
      motorForward(*m, constrain(val, 0, 255));
      port.printf("Motor %c Forward %d\n", which, val);
      break;
    case 'R': case 'r':
      motorReverse(*m, constrain(val, 0, 255));
      port.printf("Motor %c Reverse %d\n", which, val);
      break;
    case 'B': case 'b':
      motorBrake(*m);
      port.printf("Motor %c Brake\n", which);
      break;
    case 'S': case 's':
      motorStop(*m);
      port.printf("Motor %c Stop\n", which);
      break;
    case 'C': case 'c': {
      int ms = val > 0 ? val : 10;
      port.printf("Motor %c Coasting (step delay %dms)\n", which, ms);
      motorCoast(*m, ms);
      port.printf("Motor %c Coasted to stop\n", which);
      break;
    }
    default:
      break;
  }
}

void setup() {
  initMotor(motorA);
  initMotor(motorB);
  initMotor(motorC);  // STBY already set by motorB init, harmless to repeat

  pinMode(RPR, INPUT_PULLUP);

  Serial.begin(115200);
  Serial.setTimeout(50);
  Serial.println("Commands: A/B/C + F<0-255>  R<0-255>  B  S  C<ms>");

  Serial2.begin(115200);  // carpark1 link on pins 7 (RX2) / 8 (TX2)
  Serial2.setTimeout(50);
  Serial2.println("car1 ready");  // announce over the link so the relay shows it
}

void loop() {
  if (Serial.available())  handleCommand(Serial);
  if (Serial2.available()) handleCommand(Serial2);
  

  if (sensorStreaming) {
    static uint32_t lastSensor = 0;
    if (millis() - lastSensor >= SENSOR_INTERVAL_MS) {
      lastSensor = millis();
      // carpark only - keep it off USB so the debug monitor stays readable
      Serial2.printf("S %d\n", analogRead(RPR));

    }
  }
}
