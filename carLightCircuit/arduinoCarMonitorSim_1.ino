/**
 * 4Runner Turn Signal & Brake Monitor - Maximum Rate Streamer
 * Pins:
 * Inputs (From Optocoupler): Pin 12 (Yellow Wire), Pin 13 (Green Wire)
 * LED Indicators:            Pin 8 (Yellow LED), Pin 9 (Green LED)
 * Outputs (Bench Sim Only):  Pin 2 (Yellow Out), Pin 3 (Green Out)
 */

// --- PIN DEFINITIONS ---
const int LOG_YELLOW_IN   = 12;  // Monitored Input (Left/Brake)
const int LOG_GREEN_IN    = 13;  // Monitored Input (Right/Brake)

const int LED_YELLOW_OUT  = 8;   // Live Status Display LED (Left)
const int LED_GREEN_OUT   = 9;   // Live Status Display LED (Right)

const int SIM_YELLOW_OUT  = 2;   // Bench Sim Only (Disconnect when in car)
const int SIM_GREEN_OUT   = 3;   // Bench Sim Only (Disconnect when in car)

void setup() {
  // Push the serial hardware to its maximum stable standard speed
  Serial.begin(115200);
  while (!Serial) { ; } 

  // Clean CSV Header
  Serial.println("Timestamp_ms,Yellow_Wire,Green_Wire");

  // Configure Monitoring Input Pins (Active LOW configuration)
  pinMode(LOG_YELLOW_IN, INPUT_PULLUP);
  pinMode(LOG_GREEN_IN, INPUT_PULLUP);

  // Configure LED Display Pins
  pinMode(LED_YELLOW_OUT, OUTPUT);
  pinMode(LED_GREEN_OUT, OUTPUT);

  // Configure Bench Sim Pins 
  pinMode(SIM_YELLOW_OUT, OUTPUT);
  pinMode(SIM_GREEN_OUT, OUTPUT);
  digitalWrite(SIM_YELLOW_OUT, HIGH);
  digitalWrite(SIM_GREEN_OUT, HIGH);
}

void loop() {
  // 1. RUN BENCH SIMULATOR (Generates test patterns onto Pins 2 & 3)
  runCarSimulation();

  // 2. READ PHYSICAL HARDWARE INPUTS (Zero software processing)
  bool yellowRaw = (digitalRead(LOG_YELLOW_IN) == LOW);
  bool greenRaw  = (digitalRead(LOG_GREEN_IN) == LOW);

  // 3. REFLECT REAL-TIME DATA TO HARDWARE LEDS
  digitalWrite(LED_YELLOW_OUT, yellowRaw ? HIGH : LOW);
  digitalWrite(LED_GREEN_OUT, greenRaw ? HIGH : LOW);

  // 4. MAXIMUM VELOCITY DATA STREAM
  // Grabs time, formats to binary string, and dumps directly to serial buffer
  Serial.print(millis());
  Serial.print(",");
  Serial.print(yellowRaw ? "1" : "0");
  Serial.print(",");
  Serial.println(greenRaw ? "1" : "0");
}

// Simulated Car Generator Engine (36-second loop sequence)
void runCarSimulation() {
  unsigned long simTime = millis() % 36000; 

  if (simTime < 6000) { // Scenario 1: Left Blink Only
    if ((simTime % 750) < 375) digitalWrite(SIM_YELLOW_OUT, LOW);
    else digitalWrite(SIM_YELLOW_OUT, HIGH);
    digitalWrite(SIM_GREEN_OUT, HIGH);
  } 
  else if (simTime >= 6000 && simTime < 12000) { // Scenario 2: Solid Brakes
    digitalWrite(SIM_YELLOW_OUT, LOW);
    digitalWrite(SIM_GREEN_OUT, LOW);
  } 
  else if (simTime >= 12000 && simTime < 18000) { // Scenario 3: Right Blink Only
    digitalWrite(SIM_YELLOW_OUT, HIGH);
    if ((simTime % 750) < 375) digitalWrite(SIM_GREEN_OUT, LOW);
    else digitalWrite(SIM_GREEN_OUT, HIGH);
  } 
  else if (simTime >= 18000 && simTime < 24000) { // Scenario 4: Brake + Left Blink
    digitalWrite(SIM_GREEN_OUT, LOW); 
    if ((simTime % 750) < 375) digitalWrite(SIM_YELLOW_OUT, LOW);
    else digitalWrite(SIM_YELLOW_OUT, HIGH);
  }
  else if (simTime >= 24000 && simTime < 30000) { // Scenario 5: Brake + Right Blink
    digitalWrite(SIM_YELLOW_OUT, LOW);
    if ((simTime % 750) < 375) digitalWrite(SIM_GREEN_OUT, LOW);
    else digitalWrite(SIM_GREEN_OUT, HIGH);
  }
  else { // Scenario 6: All Signals Off
    digitalWrite(SIM_YELLOW_OUT, HIGH);
    digitalWrite(SIM_GREEN_OUT, HIGH);
  }
}
