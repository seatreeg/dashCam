"""
4Runner Car Monitor - Combined OBD-II + Arduino Trailer-Port Logger w/ Live GUI
===============================================================================

Replaces OBDwiz with the open-source `python-OBD` library and merges the OBD
data (RPM / speed / throttle) with the Arduino trailer-port data (yellow & green
wire activation) into ONE growing CSV, while showing a live dashboard window.

Two background reader threads keep the most recent value from each device.
A single logging tick (the GUI's refresh loop, or a plain loop in headless mode)
samples both at a fixed rate and writes one aligned row per sample.

Runs on Windows (laptop) and Raspberry Pi 5 (Linux) unchanged - only the serial
port names differ, and those auto-detect (override below if needed).

    pip install -r requirements.txt
    python car_monitor.py

Press Ctrl+C in the terminal, or close the window, to stop.
"""

import csv
import sys
import threading
import time
from datetime import datetime

import serial
import serial.tools.list_ports

# python-OBD is optional at runtime: if it isn't installed or no OBD adapter is
# connected, the program still runs and logs the Arduino data (OBD columns blank).
try:
    import obd
    obd.logger.setLevel(obd.logging.WARNING)  # quiet the library's chatter
    HAVE_OBD = True
except ImportError:
    HAVE_OBD = False


# ============================ CONFIGURATION ============================
# Set explicit ports if auto-detection picks the wrong device.
# Windows examples: 'COM3', 'COM4'.  Linux/Pi examples: '/dev/ttyACM0', '/dev/ttyUSB0'.
ARDUINO_PORT = None      # None = auto-detect the Arduino
ARDUINO_BAUD = 115200

OBD_PORT     = None      # None = let python-OBD scan for the ELM327 adapter
ENABLE_OBD   = True      # set False to skip OBD entirely (Arduino-only logging)

OUTPUT_FILE  = 'car_telemetry.csv'
SAMPLE_HZ    = 10        # rows written per second (10 Hz = one row every 100 ms)

ENABLE_GUI   = True      # set False for headless logging (e.g. Pi with no screen)

# Speed comes off the OBD port in km/h; convert to mph for display + logging.
KPH_TO_MPH   = 0.621371
# ======================================================================


class SharedState:
    """Thread-safe snapshot of the latest reading from every device."""

    def __init__(self):
        self._lock = threading.Lock()
        # OBD values (None = no fresh reading yet)
        self.rpm = None
        self.speed_mph = None
        self.throttle = None
        self.obd_connected = False
        # Arduino values
        self.yellow = None
        self.green = None
        self.arduino_millis = None
        self.arduino_connected = False

    def update(self, **kwargs):
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def snapshot(self):
        with self._lock:
            return dict(
                rpm=self.rpm,
                speed_mph=self.speed_mph,
                throttle=self.throttle,
                obd_connected=self.obd_connected,
                yellow=self.yellow,
                green=self.green,
                arduino_millis=self.arduino_millis,
                arduino_connected=self.arduino_connected,
            )


def find_arduino_port():
    """Return the COM/tty port of the first connected Arduino, or None."""
    for port in serial.tools.list_ports.comports():
        haystack = f"{port.description} {port.manufacturer or ''}".lower()
        # 0x2341 = Arduino LLC vendor ID. CH340/CH341 clones report 0x1A86.
        if 'arduino' in haystack or port.vid in (0x2341, 0x1A86):
            return port.device
    return None


# ----------------------------- Reader threads -----------------------------

def arduino_reader(state, stop_event):
    """
    Continuously read the Arduino CSV stream ("millis,yellow,green") and push
    the latest values into shared state. Reconnects automatically if unplugged.
    """
    while not stop_event.is_set():
        port = ARDUINO_PORT or find_arduino_port()
        if port is None:
            state.update(arduino_connected=False)
            print("[arduino] not found, retrying in 2s...")
            stop_event.wait(2)
            continue

        try:
            ser = serial.Serial(port, ARDUINO_BAUD, timeout=1)
            print(f"[arduino] connected on {port}")
            state.update(arduino_connected=True)

            while not stop_event.is_set():
                raw = ser.readline().decode('utf-8', errors='ignore').strip()
                if not raw or raw.startswith('Timestamp'):
                    continue
                parts = raw.split(',')
                if len(parts) == 3:
                    try:
                        millis = int(parts[0])
                        yellow = int(parts[1])
                        green = int(parts[2])
                    except ValueError:
                        continue  # skip malformed line
                    state.update(arduino_millis=millis, yellow=yellow, green=green)
            ser.close()
        except serial.SerialException as exc:
            print(f"[arduino] serial error: {exc}; reconnecting in 2s...")
            state.update(arduino_connected=False)
            stop_event.wait(2)


def obd_reader(state, stop_event):
    """
    Continuously query RPM / speed / throttle from the OBD adapter and push the
    latest values into shared state. Reconnects automatically.
    """
    if not (ENABLE_OBD and HAVE_OBD):
        if ENABLE_OBD and not HAVE_OBD:
            print("[obd] python-OBD not installed; run 'pip install obd'. "
                  "Continuing with Arduino-only logging.")
        return

    while not stop_event.is_set():
        try:
            connection = obd.OBD(OBD_PORT) if OBD_PORT else obd.OBD()
            if not connection.is_connected():
                state.update(obd_connected=False)
                print("[obd] adapter not found, retrying in 3s...")
                stop_event.wait(3)
                continue

            print(f"[obd] connected on {connection.port_name()}")
            state.update(obd_connected=True)

            while not stop_event.is_set() and connection.is_connected():
                rpm_r = connection.query(obd.commands.RPM)
                spd_r = connection.query(obd.commands.SPEED)
                thr_r = connection.query(obd.commands.THROTTLE_POS)

                rpm = None if rpm_r.is_null() else round(rpm_r.value.magnitude)
                # SPEED comes back in km/h; convert to mph.
                speed_mph = (None if spd_r.is_null()
                             else round(spd_r.value.magnitude * KPH_TO_MPH, 1))
                throttle = (None if thr_r.is_null()
                            else round(thr_r.value.magnitude, 1))

                state.update(rpm=rpm, speed_mph=speed_mph, throttle=throttle)

            connection.close()
            state.update(obd_connected=False)
        except Exception as exc:  # noqa: BLE001 - keep the thread alive no matter what
            print(f"[obd] error: {exc}; reconnecting in 3s...")
            state.update(obd_connected=False)
            stop_event.wait(3)


# ----------------------------- Logging core -----------------------------

def make_writer():
    """Open the CSV in append mode, writing the header only if the file is new."""
    file = open(OUTPUT_FILE, mode='a', newline='')
    writer = csv.writer(file)
    if file.tell() == 0:
        writer.writerow([
            'Computer_DateTime', 'Seconds_Elapsed',
            'RPM', 'Speed_MPH', 'Throttle_Pct',
            'Yellow_Wire', 'Green_Wire', 'Arduino_Millis',
            'OBD_Connected', 'Arduino_Connected',
        ])
    return file, writer


def blank(value):
    """Render None as an empty CSV cell."""
    return '' if value is None else value


def log_row(writer, start_time, snap):
    now = datetime.now()
    seconds_elapsed = (now - start_time).total_seconds()
    date_time_str = now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    writer.writerow([
        date_time_str, f"{seconds_elapsed:.3f}",
        blank(snap['rpm']), blank(snap['speed_mph']), blank(snap['throttle']),
        blank(snap['yellow']), blank(snap['green']), blank(snap['arduino_millis']),
        int(snap['obd_connected']), int(snap['arduino_connected']),
    ])


# ----------------------------- GUI -----------------------------

def run_gui(state, writer, file, start_time, stop_event):
    import tkinter as tk

    # --- Red & black theme palette ---
    BG        = "#0a0a0a"   # near-black background
    RED       = "#e10600"   # accent / live values / "on"
    RED_DIM   = "#3a0c0c"   # dark red for "off" indicators
    GREY      = "#777777"   # secondary label text
    GREY_DARK = "#2a2a2a"   # disconnected dot

    interval_ms = int(1000 / SAMPLE_HZ)
    root = tk.Tk()
    root.title("4Runner Car Monitor")
    root.configure(bg=BG)
    root.geometry("520x480")

    def big_label(parent, text):
        tk.Label(parent, text=text, fg=GREY, bg=BG,
                 font=("Segoe UI", 12)).pack()
        val = tk.Label(parent, text="--", fg=RED, bg=BG,
                       font=("Consolas", 40, "bold"))
        val.pack(pady=(0, 12))
        return val

    metrics = tk.Frame(root, bg=BG)
    metrics.pack(pady=10, fill="x")
    mph_val = big_label(metrics, "SPEED (MPH)")
    rpm_val = big_label(metrics, "RPM")
    thr_val = big_label(metrics, "THROTTLE (%)")

    def dot_row(parent):
        row = tk.Frame(parent, bg=BG)
        row.pack(pady=6)
        return row

    def indicator(parent, text):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(side="left", padx=18)
        dot = tk.Label(frame, text="●", font=("Segoe UI", 30), bg=BG, fg=GREY_DARK)
        dot.pack()
        tk.Label(frame, text=text, fg=GREY, bg=BG,
                 font=("Segoe UI", 10)).pack()
        return dot

    # Wire activation indicators
    wires = dot_row(root)
    yellow_dot = indicator(wires, "YELLOW WIRE")
    green_dot = indicator(wires, "GREEN WIRE")

    # Device connection indicators
    conns = dot_row(root)
    obd_dot = indicator(conns, "OBD")
    ard_dot = indicator(conns, "ARDUINO")

    def tick():
        if stop_event.is_set():
            root.destroy()
            return

        snap = state.snapshot()
        log_row(writer, start_time, snap)
        file.flush()

        mph_val.config(text="--" if snap['speed_mph'] is None else f"{snap['speed_mph']:.0f}")
        rpm_val.config(text="--" if snap['rpm'] is None else f"{snap['rpm']}")
        thr_val.config(text="--" if snap['throttle'] is None else f"{snap['throttle']:.0f}")

        # On = bright red, off = dark red (keeps the red/black theme consistent)
        yellow_dot.config(fg=RED if snap['yellow'] == 1 else RED_DIM)
        green_dot.config(fg=RED if snap['green'] == 1 else RED_DIM)

        # Connection: connected = bright red, disconnected = dim grey
        obd_dot.config(fg=RED if snap['obd_connected'] else GREY_DARK)
        ard_dot.config(fg=RED if snap['arduino_connected'] else GREY_DARK)

        root.after(interval_ms, tick)

    def on_close():
        stop_event.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(interval_ms, tick)
    root.mainloop()


def run_headless(state, writer, file, start_time, stop_event):
    interval = 1.0 / SAMPLE_HZ
    print(f"Headless logging at {SAMPLE_HZ} Hz -> {OUTPUT_FILE}. Press Ctrl+C to stop.")
    next_tick = time.monotonic()
    while not stop_event.is_set():
        log_row(writer, start_time, state.snapshot())
        file.flush()
        next_tick += interval
        time.sleep(max(0, next_tick - time.monotonic()))


# ----------------------------- main -----------------------------

def main():
    state = SharedState()
    stop_event = threading.Event()

    threads = [
        threading.Thread(target=arduino_reader, args=(state, stop_event), daemon=True),
        threading.Thread(target=obd_reader, args=(state, stop_event), daemon=True),
    ]
    for t in threads:
        t.start()

    file, writer = make_writer()
    start_time = datetime.now()

    try:
        if ENABLE_GUI:
            try:
                run_gui(state, writer, file, start_time, stop_event)
            except Exception as exc:  # no display available, etc.
                print(f"[gui] could not start ({exc}); falling back to headless.")
                run_headless(state, writer, file, start_time, stop_event)
        else:
            run_headless(state, writer, file, start_time, stop_event)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop_event.set()
        file.flush()
        file.close()
        print(f"Saved to {OUTPUT_FILE}.")


if __name__ == '__main__':
    main()
