"""
Step 3 - combined CSV
=====================

Joins all five devices onto ONE true-time axis at 1 Hz, so a single row shows
what every device reported at the same real-world instant.

This is the only place clock offsets are applied:

  Cameras - every one timestamps from its GPS receiver, so their raw stamps are
            already true satellite UTC. CAMERA_CLOCK_FAST_SEC exists as an
            escape hatch and defaults to 0 for all four.
  Pi      - its RTC was wrong in both date and time. PI_TIME_OFFSET (~133 days
            4:48:44) maps its wall clock onto real local time.

Each device keeps its own raw timestamp in a *_ts_raw column alongside the
shared true time, so what-was-reported-when stays auditable.

Output: results\\combined\\combined csv\\combined_drive.csv
"""

import csv
import json
from datetime import datetime, timedelta

from tqdm import tqdm

import config as C

STAMP = C.COMBINED_CSV.with_suffix(".stamp.json")


def _settings():
    """Everything that, if changed, invalidates the joined CSV."""
    return {
        "pi_offset_sec": C.PI_TIME_OFFSET.total_seconds(),
        "cam_fast": dict(C.CAMERA_CLOCK_FAST_SEC),
        "trip": [C.TRIP_START.isoformat(), C.TRIP_END.isoformat()],
    }


def _stamp_ok():
    """
    Caching on file existence alone is not enough here.

    The offsets live in config.py, not in any input file, so editing an offset
    leaves every timestamp on disk untouched and a plain existence check happily
    reuses a CSV built with the OLD offset. That silently defeats the entire
    point of re-running. (This is not hypothetical -- it bit during development,
    and check_sync.py duly reported the old number back.)
    """
    if not (C.cached(C.COMBINED_CSV) and STAMP.exists()):
        return False
    try:
        return json.loads(STAMP.read_text()) == json.loads(json.dumps(_settings()))
    except Exception:
        return False

GRID_HZ = 1
# A camera sample counts for a grid second if it lands within this much of it.
# Half a second at 1 Hz = nearest-sample matching with no double-counting.
MATCH_TOL = timedelta(seconds=0.5)


def _f(v):
    """CSV cell -> float, or None if blank/unparseable."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_camera(name: str):
    """
    Read one camera's individual CSV and bucket it by TRUE UTC second.

    Cameras sample at 1 Hz (Garmin/REDTIGER/Rove) or 10 Hz (GoPro). For the
    10 Hz case the sample nearest each whole second wins, so the joined row is a
    real observation rather than an interpolation.
    """
    path = C.camera_csv(name)
    if not C.cached(path):
        return {}
    fast = C.CAMERA_CLOCK_FAST_SEC[name]
    best = {}
    with path.open(newline="", encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            if not r["ts_utc_raw"]:
                continue
            raw = datetime.fromisoformat(r["ts_utc_raw"])
            true = raw - timedelta(seconds=fast)
            key = true.replace(microsecond=0)
            if true.microsecond >= 500_000:            # round to nearest second
                key += timedelta(seconds=1)
            delta = abs(true - key)
            if delta > MATCH_TOL:
                continue
            if key not in best or delta < best[key][0]:
                best[key] = (delta, r)
    return {k: v[1] for k, v in best.items()}


def load_pi():
    """
    Read the Pi log and bucket it by TRUE UTC second, applying PI_TIME_OFFSET.

    The Pi logs at 10 Hz; the sample nearest each whole second is kept.
    """
    path = C.PI_CSV
    if not C.cached(path):
        return {}
    best = {}
    with path.open(newline="", encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            try:
                raw = datetime.strptime(r["Computer_DateTime"], "%Y-%m-%d %H:%M:%S.%f")
            except (ValueError, KeyError):
                continue
            true_local = C.pi_raw_to_true(raw)
            true_utc = true_local.astimezone(C.UTC)
            key = true_utc.replace(microsecond=0)
            if true_utc.microsecond >= 500_000:
                key += timedelta(seconds=1)
            delta = abs(true_utc - key)
            if delta > MATCH_TOL:
                continue
            if key not in best or delta < best[key][0]:
                best[key] = (delta, r)
    return {k: v[1] for k, v in best.items()}


def build_fields():
    """Column order: true time first, then one namespaced block per device."""
    fields = ["true_time_utc", "true_time_local", "elapsed_sec"]
    for name in C.CAMERA_ORDER:
        cam = C.CAMERAS[name]
        k = cam["key"]
        fields += [f"{k}_ts_raw", f"{k}_clip", f"{k}_sample_idx",
                   f"{k}_lat", f"{k}_lon",
                   f"{k}_speed_mph", f"{k}_speed_mph_calculated",
                   f"{k}_fix_status", f"{k}_fix_ok"]
        if cam["has_accel"]:
            fields += [f"{k}_accel_x", f"{k}_accel_y", f"{k}_accel_z"]
        if cam["has_gyro"]:
            fields += [f"{k}_gyro_x", f"{k}_gyro_y", f"{k}_gyro_z"]
    fields += ["pi_ts_raw", "pi_rpm", "pi_speed_mph", "pi_throttle_pct",
               "pi_yellow_wire", "pi_green_wire",
               "pi_obd_connected", "pi_arduino_connected"]
    return fields


def run(force: bool = False):
    C.ensure_dirs()
    out = C.COMBINED_CSV
    if _stamp_ok() and not force:
        with out.open(encoding="utf-8") as fp:
            n = sum(1 for _ in fp) - 1
        print(f"  cached: {out.name} ({n} rows)")
        return
    if C.cached(out) and not force:
        print("  offsets changed since the cached CSV was built -- rebuilding")

    cams = {}
    for name in tqdm(C.CAMERA_ORDER, desc="  loading cameras", unit="cam"):
        cams[name] = load_camera(name)
        tqdm.write(f"    {name:22s} {len(cams[name]):5d} seconds")
    pi = load_pi()
    print(f"    {C.PI_NAME:22s} {len(pi):5d} seconds")

    # The grid spans every second any device reported, clamped to the trip.
    keys = set(pi)
    for d in cams.values():
        keys |= set(d)
    if not keys:
        raise SystemExit("No data loaded -- run the individual CSV step first.")
    lo = max(min(keys), C.TRIP_START.astimezone(C.UTC))
    hi = min(max(keys), C.TRIP_END.astimezone(C.UTC))
    n_sec = int((hi - lo).total_seconds()) + 1

    fields = build_fields()
    written = 0
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for i in tqdm(range(0, n_sec, GRID_HZ), desc="  joining", unit="sec"):
            t = lo + timedelta(seconds=i)
            row = {
                "true_time_utc": t.isoformat(),
                "true_time_local": t.astimezone(C.LOCAL).isoformat(),
                "elapsed_sec": i,
            }
            for name in C.CAMERA_ORDER:
                cam = C.CAMERAS[name]
                k = cam["key"]
                r = cams[name].get(t)
                if not r:
                    continue
                row.update({
                    f"{k}_ts_raw": r["ts_utc_raw"],
                    f"{k}_clip": r["clip"],
                    f"{k}_sample_idx": r["sample_idx"],
                    f"{k}_lat": r["lat_deg"],
                    f"{k}_lon": r["lon_deg"],
                    f"{k}_speed_mph": r["speed_mph_from_camera"],
                    f"{k}_speed_mph_calculated": r["speed_mph_calculated"],
                    f"{k}_fix_status": r["fix_status"],
                    f"{k}_fix_ok": r["fix_ok"],
                })
                if cam["has_accel"]:
                    row.update({f"{k}_accel_x": r["accel_x"],
                                f"{k}_accel_y": r["accel_y"],
                                f"{k}_accel_z": r["accel_z"]})
                if cam["has_gyro"]:
                    row.update({f"{k}_gyro_x": r["gyro_x"],
                                f"{k}_gyro_y": r["gyro_y"],
                                f"{k}_gyro_z": r["gyro_z"]})
            p = pi.get(t)
            if p:
                row.update({
                    "pi_ts_raw": p["Computer_DateTime"],
                    "pi_rpm": p["RPM"],
                    "pi_speed_mph": p["Speed_MPH"],
                    "pi_throttle_pct": p["Throttle_Pct"],
                    "pi_yellow_wire": p[C.LEFT_WIRE],
                    "pi_green_wire": p[C.RIGHT_WIRE],
                    "pi_obd_connected": p["OBD_Connected"],
                    "pi_arduino_connected": p["Arduino_Connected"],
                })
            w.writerow(row)
            written += 1

    STAMP.write_text(json.dumps(_settings(), sort_keys=True))
    print(f"  -- {written} rows  {lo.astimezone(C.LOCAL):%H:%M:%S} -> "
          f"{hi.astimezone(C.LOCAL):%H:%M:%S} local -> {out.name}")


if __name__ == "__main__":
    run()
