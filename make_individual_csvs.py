"""
Step 2 - per-camera individual CSVs
===================================

One CSV per camera, all four in an identical schema (empty cells where a camera
lacks a sensor), plus a pass-through of the Pi's OBD / four-way log.

Times here are each device's RAW clock. No offsets are applied -- that is
make_combined_csv.py's job. Keeping the individual CSVs raw means they stay a
faithful record of what each device actually reported, and re-tuning an offset
later never requires re-extracting from 47 GB of video.

Output: results\\individual\\<camera>\\<key>.csv
        results\\individual\\OBD II and Four Way Port\\obd_four_way.csv
"""

import csv
import re
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

from tqdm import tqdm

import config as C
import parsers as P

# Camera clock -> trip window, in each camera's own raw UTC.
WIN_START = C.TRIP_START.astimezone(C.UTC)
WIN_END = C.TRIP_END.astimezone(C.UTC)

# Filenames that carry a local-time stamp, used to skip pre-drive clips without
# opening them. Falls back to parsing when a name doesn't match.
_NAME_TIME_RE = {
    "REDTIGER F7N Touch": re.compile(r"^(\d{14})_"),          # 20260713052715_000198F
    "ROVE R2 4K Dual":    re.compile(r"^REC(\d{8}-\d{6})-"),  # REC20260713-052715-187
}
_NAME_TIME_FMT = {
    "REDTIGER F7N Touch": "%Y%m%d%H%M%S",
    "ROVE R2 4K Dual":    "%Y%m%d-%H%M%S",
}

# A clip whose name says it starts slightly before the trip may still straddle
# the start, so the prefilter is deliberately generous on the near side.
_PREFILTER_PAD = timedelta(minutes=5)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * asin(sqrt(h)) * 6371000.0


def clip_prefilter(name: str, mp4: Path) -> bool:
    """
    Cheap 'could this clip overlap the trip?' test from the filename alone.
    True = worth parsing. Only ever used to skip obviously-pre-drive clips;
    the authoritative gate is the per-row timestamp filter below.
    """
    rx = _NAME_TIME_RE.get(name)
    if not rx:
        return True
    m = rx.match(mp4.name)
    if not m:
        return True                        # unknown pattern -> don't risk it
    try:
        start_local = datetime.strptime(m.group(1), _NAME_TIME_FMT[name]).replace(tzinfo=C.LOCAL)
    except ValueError:
        return True
    start_utc = start_local.astimezone(C.UTC)
    return (start_utc + _PREFILTER_PAD) >= WIN_START and start_utc <= WIN_END


def add_calculated_speed(rows):
    """
    Fill speed_mph_calculated from consecutive fixed positions.

    This is a cross-check against the camera's own reported speed, not a
    replacement: every camera here reports speed directly, so speed_source stays
    'from_camera' throughout. Gaps larger than MAX_DT (clip boundaries, dropouts)
    are left blank rather than smeared into a bogus average.
    """
    MAX_DT = 10.0
    prev = None
    for r in rows:
        if r["lat_deg"] == "" or r["lon_deg"] == "" or not r["ts_utc_raw"]:
            prev = None
            continue
        t = datetime.fromisoformat(r["ts_utc_raw"])
        if prev is not None:
            dt = (t - prev[0]).total_seconds()
            if 0 < dt <= MAX_DT:
                d = haversine_m(prev[1], prev[2], r["lat_deg"], r["lon_deg"])
                r["speed_mph_calculated"] = round(d / dt * 2.236936, 2)  # m/s -> mph
        prev = (t, r["lat_deg"], r["lon_deg"])
    return rows


def extract_camera(name: str, force: bool = False) -> int:
    """Parse every in-window clip for one camera and write its CSV."""
    out = C.camera_csv(name)
    if C.cached(out) and not force:
        with out.open(encoding="utf-8") as fp:
            n = sum(1 for _ in fp) - 1
        tqdm.write(f"  [{name}] cached: {out.name} ({n} rows)")
        return n

    cam = C.CAMERAS[name]
    clips = sorted(cam["dir"].glob(cam["glob"]))
    candidates = [c for c in clips if clip_prefilter(name, c)]
    skipped = len(clips) - len(candidates)

    rows = []
    kept_clips = 0
    bar = tqdm(candidates, desc=f"  {name}", unit="clip", leave=False)
    for mp4 in bar:
        bar.set_postfix_str(mp4.name)
        try:
            clip_rows = P.PARSERS[name](mp4)
        except Exception as exc:                      # one bad clip must not kill the run
            tqdm.write(f"    ! {mp4.name}: {type(exc).__name__}: {exc}")
            continue
        # Authoritative filter: keep only samples actually inside the trip.
        clip_rows = [r for r in clip_rows if r["ts_utc_raw"]
                     and WIN_START <= datetime.fromisoformat(r["ts_utc_raw"]) <= WIN_END]
        if clip_rows:
            kept_clips += 1
            rows.extend(clip_rows)
    bar.close()

    rows.sort(key=lambda r: (r["ts_utc_raw"], r["clip"], r["sample_idx"]))
    add_calculated_speed(rows)

    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=P.UNIFIED_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    n_fix = sum(1 for r in rows if r["fix_ok"] == 1)
    tqdm.write(f"  [{name}] {len(rows)} rows from {kept_clips} clips "
               f"({skipped} pre-drive skipped, {len(candidates) - kept_clips} parsed but out of window), "
               f"fix={n_fix} -> {out.name}")
    return len(rows)


def extract_pi(force: bool = False) -> int:
    """
    Pass the Pi log through with its RAW clock intact, dropping pre-drive rows.

    The Pi's clock was wrong in both date and time (it logged 2026-03-02 for a
    2026-07-13 drive), so the cutoff is expressed in Pi time. The correction
    itself lives in make_combined_csv.py.
    """
    out = C.PI_CSV
    if C.cached(out) and not force:
        with out.open(encoding="utf-8") as fp:
            n = sum(1 for _ in fp) - 1
        tqdm.write(f"  [{C.PI_NAME}] cached: {out.name} ({n} rows)")
        return n

    with C.PI_RAW_CSV.open(newline="", encoding="utf-8") as fp:
        src = list(csv.DictReader(fp))

    kept = []
    for r in tqdm(src, desc=f"  {C.PI_NAME}", unit="row", leave=False):
        try:
            t = datetime.strptime(r["Computer_DateTime"], "%Y-%m-%d %H:%M:%S.%f")
        except (ValueError, KeyError):
            continue
        if t >= C.PI_CUTOFF_RAW:
            kept.append(r)

    fields = list(src[0].keys()) if src else []
    with out.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        w.writerows(kept)

    tqdm.write(f"  [{C.PI_NAME}] {len(kept)} rows kept of {len(src)} "
               f"(dropped {len(src) - len(kept)} before {C.PI_CUTOFF_RAW}) -> {out.name}")
    return len(kept)


def dump_full_rate_imu(force: bool = False):
    """Optional native-rate IMU dumps. Large; the 1 Hz join does not use them."""
    for name in ("GOPRO Hero Black 13", "ROVE R2 4K Dual"):
        out = C.camera_imu_csv(name)
        if C.cached(out) and not force:
            tqdm.write(f"  [{name} full-rate IMU] cached")
            continue
        cam = C.CAMERAS[name]
        clips = [c for c in sorted(cam["dir"].glob(cam["glob"])) if clip_prefilter(name, c)]
        with out.open("w", newline="", encoding="utf-8") as fp:
            w = csv.writer(fp)
            w.writerow(["clip", "sample_idx", "accel_x", "accel_y", "accel_z", "units"])
            for mp4 in tqdm(clips, desc=f"  {name} IMU", unit="clip", leave=False):
                try:
                    if name == "ROVE R2 4K Dual":
                        trips = P.parse_rove_accel_full(mp4)
                        units = "g"
                    else:
                        trips = P._gopro_imu(mp4, "Accelerometer")
                        units = "m/s^2"
                except Exception as exc:
                    tqdm.write(f"    ! {mp4.name}: {exc}")
                    continue
                for i, (x, y, z) in enumerate(trips):
                    w.writerow([mp4.name, i, x, y, z, units])
        tqdm.write(f"  [{name} full-rate IMU] -> {out.name}")


def run(force: bool = False):
    C.ensure_dirs()
    total = 0
    for name in tqdm(C.CAMERA_ORDER, desc="Cameras", unit="cam"):
        total += extract_camera(name, force)
    total += extract_pi(force)
    if C.DUMP_FULL_RATE_IMU:
        dump_full_rate_imu(force)
    print(f"  -- {total} rows written across {len(C.CAMERA_ORDER)} cameras + Pi")


if __name__ == "__main__":
    run()
