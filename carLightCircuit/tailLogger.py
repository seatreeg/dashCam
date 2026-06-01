import serial
import serial.tools.list_ports
import csv
from datetime import datetime

# VERY IMPORTANT::: MAKE SURE TO CLOSE THE SERIAL MONITOR/ARDUINO IDE

# --- CONFIGURATION ---
# Set a specific port (e.g., 'COM4' on Windows, '/dev/ttyACM0' on Linux/Mac),
# or leave as None to auto-detect a connected Arduino.
SERIAL_PORT = None
BAUD_RATE = 115200
OUTPUT_FILE = '4runner_telemetry.csv'


def find_arduino_port():
    """Return the COM port of the first connected Arduino, or None if not found."""
    for port in serial.tools.list_ports.comports():
        haystack = f"{port.description} {port.manufacturer or ''}".lower()
        if 'arduino' in haystack or (port.vid == 0x2341):
            return port.device
    return None


port = SERIAL_PORT or find_arduino_port()
if port is None:
    available = [p.device for p in serial.tools.list_ports.comports()]
    raise SystemExit(
        "No Arduino found. Set SERIAL_PORT manually. "
        f"Available ports: {available or 'none'}"
    )

print(f"Connecting to Arduino on {port}...")
ser = serial.Serial(port, BAUD_RATE, timeout=1)

# Open the CSV file to append data
with open(OUTPUT_FILE, mode='a', newline='') as file:
    writer = csv.writer(file)
    
    # Write headers only if the file is brand new/empty
    if file.tell() == 0:
        writer.writerow(['Computer_DateTime', 'Seconds_Elapsed', 'Arduino_Millis', 'Yellow_Wire', 'Green_Wire'])

    start_time = datetime.now()
    print(f"Logging started. Saving data to {OUTPUT_FILE}. Press Ctrl+C to stop.")

    try:
        while True:
            # Read a raw line from the USB serial bus
            raw_line = ser.readline().decode('utf-8', errors='ignore').strip()
            
            # Ensure it's a valid data row and ignore the setup header
            if raw_line and not raw_line.startswith('Timestamp'):
                # Split the Arduino data: [millis, yellow, green]
                data_parts = raw_line.split(',')
                
                if len(data_parts) == 3:
                    arduino_millis = data_parts[0]
                    yellow = data_parts[1]
                    green = data_parts[2]
                    
                    # Compute computer-side timestamps
                    now = datetime.now()
                    seconds_elapsed = (now - start_time).total_seconds()
                    date_time_str = now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] # Precise down to milliseconds
                    
                    # Construct the unified data row
                    row = [date_time_str, f"{seconds_elapsed:.3f}", arduino_millis, yellow, green]
                    
                    # Write straight to disk
                    writer.writerow(row)
                    
    except KeyboardInterrupt:
        print("\nLogging stopped cleanly via user request.")
    finally:
        ser.close()
