"""
Per-camera telemetry parsers
============================

Four cameras, four completely different embedded formats. Ported from the
previous trip's `justCSVGenGH.py`, which did the hard reverse-engineering work.

Every parser returns rows in ONE shared schema (see UNIFIED_FIELDS), with empty
strings where a camera lacks a sensor. Times are the camera's RAW clock -- no
offsets are applied here.

    Garmin X310    1 Hz GPS in a private uuid atom. No IMU.
    REDTIGER F7N   1 Hz GPS in Novatek 'freeGPS' chunks + coarse 2-axis G-sensor.
    ROVE R2 4K     1 Hz GPS + ~16.6 Hz 3-axis accel, in SStarMeta ('ssmd') tracks.
    GoPro Hero 13  10 Hz GPS + ~200 Hz 3-axis accel AND gyro, in GPMF. Only gyro.

ON CLOCKS: all four cameras timestamp from their GPS receivers, so every
ts_utc_raw here is true satellite UTC and needs no skew correction. This is why
the GoPro is read via GPSDateTime rather than the container CreateDate -- the
latter is the camera's own RTC (it was 9 s fast on the April trip) and would
reintroduce a per-camera offset we otherwise don't have to model.

ON ACCEL UNITS: the three accelerometers are NOT mutually comparable.
    GoPro    - m/s^2, includes gravity (~9.81 on one axis at rest)
    Rove     - g, includes gravity (rest magnitude ~1.1 g; sensor uncalibrated)
    REDTIGER - coarse integer, gravity-COMPENSATED (~0 at rest), units uncertain
               (~tenths of g per count), Z axis unused and always 0.
They are kept in native units rather than force-fitted to a common scale, which
would fabricate precision the sensors don't have. The accel_units column records
which is which.
"""

import re
import struct
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

from config import EXIFTOOL, FFMPEG, FFPROBE, KMH_TO_MPH, UTC

# ============================ SCHEMA ============================

UNIFIED_FIELDS = [
    "camera", "clip", "sample_idx", "ts_utc_raw",
    "lat_deg", "lon_deg",
    "speed_mph_from_camera", "speed_mph_calculated", "speed_source",
    "fix_status", "fix_ok",
    "accel_x", "accel_y", "accel_z", "accel_units",
    "gyro_x", "gyro_y", "gyro_z", "gyro_units",
]


def _blank_row():
    return {k: "" for k in UNIFIED_FIELDS}


# ============================ MAGIC NUMBERS ============================
# Sourced from exiftool's QuickTime.pm and from empirical work on the April trip.
# See justCSVGenGH.py for the original derivation notes.

# Garmin X310 keeps 1 Hz GPS in a private uuid atom under this key.
GARMIN_GPS_UUID = bytes.fromhex("9b630f8d637440ec8204bc5ff5091728")
MP4_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)   # QuickTime epoch
NO_FIX_I32 = -2147483648                       # Garmin no-fix sentinel (0x80000000)

# Rove no-fix sentinel, as raw double bits.
ROVE_NO_FIX_DOUBLE_BITS = 0x41EFFFFFFFE00000

# REDTIGER G-sensor: three int32 LE inside the freeGPS payload. Found empirically
# on the April trip -- offset 128 (longitudinal) tracks the camera's own d(speed)
# and reads ~0 when parked. The vertical axis at 132 is unused: it is 0 in every
# sample ever observed, which is why this camera is effectively 2-axis.
REDTIGER_GFORCE_OFFS = (124, 128, 132)   # gx (lateral), gy (longitudinal), gz (unused)

# REDTIGER speed: float32 LE, mph. justCSVGenGH.py never parsed this field, so it
# was located by scanning every plausible float offset in the chunk and
# correlating against Garmin speed at matched UTC seconds. Offset 96 is
# unambiguous (r = 0.970, mean abs error 1.3 mph over 46 matched samples); the
# next best candidate scored r = 0.25. The residual error is the same brake-lag
# artifact seen between all these receivers, not a unit mismatch.
REDTIGER_SPEED_OFF = 96

ROVE_ACCEL_REC_SIZE = 12   # 3x float32 LE
ROVE_GPS_REC_SIZE = 32

# GoPro IMU: pull every number out of exiftool's text dump and group in threes.
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d+")


# ============================ SHARED HELPERS ============================

def _run(cmd, binary=False):
    """Run a command, returning stdout. Raises on non-zero exit."""
    r = subprocess.run(cmd, capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8", errors="replace")


def ddmm_to_decimal(v: float) -> float:
    """NMEA DDDMM.MMMM -> decimal degrees."""
    sign = -1.0 if v < 0 else 1.0
    v = abs(v)
    deg = int(v // 100)
    minutes = v - deg * 100
    return sign * (deg + minutes / 60.0)


def find_atom(fp, start, end, target):
    """
    First atom named `target` within [start, end). Recurses into the standard
    container atoms. Returns (offset, size) or None.
    """
    pos = start
    while pos < end - 8:
        fp.seek(pos)
        hdr = fp.read(8)
        if len(hdr) < 8:
            return None
        size = struct.unpack(">I", hdr[:4])[0]
        name = hdr[4:8]
        if size == 1:                                   # 64-bit extended size
            size = struct.unpack(">Q", fp.read(8))[0]
        if size == 0:
            size = end - pos
        if name == target:
            return pos, size
        if name in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"):
            r = find_atom(fp, pos + 8, pos + size, target)
            if r:
                return r
        if size <= 0:
            return None
        pos += size
    return None


def find_uuid_atom(fp, start, end, target_uuid):
    """
    First 'uuid' atom whose 16-byte body key matches target_uuid.
    Returns (atom_offset, atom_size, body_offset_after_uuid) or None.
    """
    pos = start
    while pos < end - 8:
        fp.seek(pos)
        hdr = fp.read(8)
        if len(hdr) < 8:
            return None
        size = struct.unpack(">I", hdr[:4])[0]
        name = hdr[4:8]
        body_start = pos + 8
        if size == 1:
            size = struct.unpack(">Q", fp.read(8))[0]
            body_start = pos + 16
        if size == 0:
            size = end - pos
        if name == b"uuid":
            fp.seek(body_start)
            if fp.read(16) == target_uuid:
                return pos, size, body_start + 16
        if name in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"):
            r = find_uuid_atom(fp, body_start, pos + size, target_uuid)
            if r:
                return r
        if size <= 0:
            return None
        pos += size
    return None


# ============================ GARMIN X310 ============================

def parse_garmin(mp4: Path):
    """1 Hz GPS from the private uuid atom. This camera has no accel or gyro."""
    with open(mp4, "rb") as fp:
        fp.seek(0, 2)
        filesize = fp.tell()
        found = find_uuid_atom(fp, 0, filesize, GARMIN_GPS_UUID)
        if not found:
            return []
        atom_off, atom_size, body_off = found
        fp.seek(body_off)
        body = fp.read(atom_off + atom_size - body_off)

    if len(body) < 17:
        return []
    # Fixed header: 4x LE int32, observed (0, 20, 0, 60) = (?, rec_size, ?, capacity).
    _h0, rec_size, _h2, _h3 = struct.unpack("<4I", body[:16])
    if rec_size != 20:
        return []
    actual = body[16]
    payload = body[17:]
    actual = min(actual, len(payload) // rec_size)

    SCALE = 360.0 / (1 << 32)     # int32 -> degrees
    rows = []
    for i in range(actual):
        rec = payload[i * rec_size:(i + 1) * rec_size]
        ts, pad1, _pad2, lat_i, lon_i = struct.unpack(">IIIii", rec)
        speed_mph = (pad1 >> 16) & 0xFF        # second byte of the BE pad word
        no_fix = (lat_i == NO_FIX_I32) or (lon_i == NO_FIX_I32)

        row = _blank_row()
        row.update(
            camera="Garmin X310", clip=mp4.name, sample_idx=i,
            ts_utc_raw=(MP4_EPOCH + timedelta(seconds=ts)).isoformat(),
            lat_deg="" if no_fix else round(lat_i * SCALE, 7),
            lon_deg="" if no_fix else round(lon_i * SCALE, 7),
            speed_mph_from_camera=speed_mph,
            speed_source="from_camera",
            fix_status="nofix" if no_fix else "fix",
            fix_ok=0 if no_fix else 1,
        )
        rows.append(row)
    return rows


# ============================ REDTIGER F7N ============================

def _parse_redtiger_chunk(buf: bytes):
    """
    One freeGPS chunk -> dict, or None if it isn't one.

    NOTE: exiftool reports "GPSLatitude encryption is not yet known" for this
    camera and emits garbage. Nothing is actually encrypted -- the coordinates
    are plain float32 LE at offsets 40/44 in NMEA ddmm.mmmm form. Verified
    against the Garmin and Rove at matched UTC seconds (agreement within ~6 m).
    """
    if len(buf) < 100 or buf[4:11] != b"freeGPS":
        return None
    lat_raw, = struct.unpack_from("<f", buf, 40)
    lon_raw, = struct.unpack_from("<f", buf, 44)
    hour, minute, second, year, month, day = struct.unpack_from("<6I", buf, 48)
    active = chr(buf[72]) if buf[72] else "?"
    lat_hem = chr(buf[73]) if buf[73] else "?"
    lon_hem = chr(buf[74]) if buf[74] else "?"
    speed_mph, = struct.unpack_from("<f", buf, REDTIGER_SPEED_OFF)

    if len(buf) >= REDTIGER_GFORCE_OFFS[2] + 4:
        gx, gy, gz = (struct.unpack_from("<i", buf, o)[0] for o in REDTIGER_GFORCE_OFFS)
    else:
        gx = gy = gz = ""

    out = {
        "active": active, "hour": hour, "minute": minute, "second": second,
        "year": year, "month": month, "day": day, "speed_mph": speed_mph,
        "gx": gx, "gy": gy, "gz": gz, "lat": "", "lon": "",
    }
    # The G-sensor is valid regardless of GPS fix, so it is filled either way.
    if active == "A":
        lat = ddmm_to_decimal(lat_raw)
        lon = ddmm_to_decimal(lon_raw)
        out["lat"] = round(-lat if lat_hem == "S" else lat, 7)
        out["lon"] = round(-lon if lon_hem == "W" else lon, 7)
    return out


def parse_redtiger(mp4: Path):
    """1 Hz GPS + coarse 2-axis G-sensor from Novatek freeGPS chunks."""
    rows = []
    with open(mp4, "rb") as fp:
        fp.seek(0, 2)
        filesize = fp.tell()
        found = find_atom(fp, 0, filesize, b"gps ")
        if not found:
            return []
        gps_off, gps_size = found
        # Body: 8-byte header (version, count), then count x 8-byte
        # (chunk_offset, chunk_size) pointers into mdat.
        fp.seek(gps_off + 8)
        body = fp.read(gps_size - 8)
        n_records = struct.unpack(">I", body[4:8])[0]

        for i in range(n_records):
            chunk_off, _chunk_len = struct.unpack(">II", body[8 + i * 8: 16 + i * 8])
            if chunk_off + 100 > filesize:
                continue
            fp.seek(chunk_off)
            p = _parse_redtiger_chunk(fp.read(256))
            if p is None:
                continue

            try:
                year = p["year"] + 2000 if p["year"] < 100 else p["year"]
                ts = datetime(year, p["month"], p["day"],
                              p["hour"], p["minute"], p["second"],
                              tzinfo=UTC).isoformat()
            except (ValueError, KeyError):
                ts = ""

            row = _blank_row()
            row.update(
                camera="REDTIGER F7N Touch", clip=mp4.name, sample_idx=i,
                ts_utc_raw=ts, lat_deg=p["lat"], lon_deg=p["lon"],
                speed_mph_from_camera=round(p["speed_mph"], 2),
                speed_source="from_camera",
                # This camera reports NMEA-style A(ctive)/V(oid) rather than a
                # fix sentinel, so its native vocabulary is preserved.
                fix_status="active" if p["active"] == "A" else "inactive",
                fix_ok=1 if p["active"] == "A" else 0,
                accel_x=p["gx"], accel_y=p["gy"], accel_z=p["gz"],
                accel_units="g-force (coarse int, gravity-compensated)",
            )
            rows.append(row)
    return rows


# ============================ ROVE R2 4K ============================

def _rove_streams(mp4: Path):
    """
    Return (gps_stream_idx, accel_stream_idx). The Rove writes three 'ssmd'
    tracks per clip: ~1 frame (a JPEG thumbnail), ~60 frames (1 Hz GPS), and
    ~1000 frames (~16.6 Hz accel). They are told apart by frame count.
    """
    out = _run([FFPROBE, "-v", "error", "-show_streams", "-of", "compact",
                "-show_entries", "stream=index,codec_type,codec_tag_string,nb_frames",
                str(mp4)])
    candidates = []
    for line in out.splitlines():
        f = dict(p.split("=", 1) for p in line.split("|") if "=" in p)
        if f.get("codec_type") == "data" and f.get("codec_tag_string") == "ssmd":
            try:
                nb = int(f.get("nb_frames", "0"))
            except ValueError:
                nb = 0
            candidates.append((int(f["index"]), nb))
    if not candidates:
        return None, None
    gps_idx = next((i for i, nb in candidates if 30 <= nb <= 120), None)
    accel_idx, accel_nb = max(candidates, key=lambda t: t[1])
    return gps_idx, (accel_idx if accel_nb >= 300 else None)


def _rove_stream_bytes(mp4: Path, idx: int) -> bytes:
    """Demux one data stream straight to stdout -- no temp files."""
    return _run([FFMPEG, "-v", "error", "-i", str(mp4),
                 "-map", f"0:{idx}", "-c", "copy", "-f", "data", "-"], binary=True)


def _parse_rove_gps_rec(rec: bytes):
    """
    One 32-byte SStarMeta GPS record.

    BUG FIX vs justCSVGenGH.py:606 -- that version read
        day = rec[22]; month = rec[23]; year2k = rec[24]
    but the true layout is year, month, day. It was invisible on the April trip
    (2026-04-26: year 26 == day 26, so swapping them was a no-op) and only
    surfaces when day != year. Left unfixed it stamps this trip's rows
    2013-07-26 instead of 2026-07-13 and breaks the whole time join.
    """
    if len(rec) < ROVE_GPS_REC_SIZE:
        return None
    lat_bits = struct.unpack_from("<Q", rec, 0)[0]
    lon_bits = struct.unpack_from("<Q", rec, 8)[0]
    lat_d = struct.unpack_from("<d", rec, 0)[0]
    lon_d = struct.unpack_from("<d", rec, 8)[0]
    no_fix = (
        lat_bits == ROVE_NO_FIX_DOUBLE_BITS or lon_bits == ROVE_NO_FIX_DOUBLE_BITS
        or not (1.0 <= abs(lat_d) < 9000.0)
        or not (1.0 <= abs(lon_d) < 18000.0)
    )
    speed_mph = rec[20]
    year2k, month, day = rec[22], rec[23], rec[24]
    hour, minute, second = rec[25], rec[26], rec[27]
    bearing = rec[28]

    try:
        ts = datetime(2000 + year2k, month, day, hour, minute, second,
                      tzinfo=UTC).isoformat()
    except ValueError:
        ts = ""
    return {
        "ts": ts, "fix": 0 if no_fix else 1,
        "lat": "" if no_fix else round(ddmm_to_decimal(lat_d), 7),
        "lon": "" if no_fix else round(ddmm_to_decimal(lon_d), 7),
        "speed_mph": speed_mph, "bearing": round(bearing * 360.0 / 256.0, 1),
    }


def parse_rove(mp4: Path):
    """1 Hz GPS, with the ~16.6 Hz accel averaged into each GPS second."""
    gps_idx, accel_idx = _rove_streams(mp4)
    if gps_idx is None:
        return []

    data = _rove_stream_bytes(mp4, gps_idx)
    gps = [_parse_rove_gps_rec(data[i * ROVE_GPS_REC_SIZE:(i + 1) * ROVE_GPS_REC_SIZE])
           for i in range(len(data) // ROVE_GPS_REC_SIZE)]
    gps = [g for g in gps if g]
    if not gps:
        return []

    accel = []
    if accel_idx is not None:
        ad = _rove_stream_bytes(mp4, accel_idx)
        n = len(ad) // ROVE_ACCEL_REC_SIZE
        accel = [struct.unpack_from("<3f", ad, i * ROVE_ACCEL_REC_SIZE) for i in range(n)]

    # Both tracks span the same clip, so accel samples map onto GPS seconds by
    # even distribution.
    rate = (len(accel) / len(gps)) if gps and accel else 0.0

    rows = []
    for i, g in enumerate(gps):
        row = _blank_row()
        row.update(
            camera="ROVE R2 4K Dual", clip=mp4.name, sample_idx=i,
            ts_utc_raw=g["ts"], lat_deg=g["lat"], lon_deg=g["lon"],
            speed_mph_from_camera=g["speed_mph"], speed_source="from_camera",
            fix_status="fix" if g["fix"] else "nofix", fix_ok=g["fix"],
        )
        if rate:
            seg = accel[int(i * rate):int((i + 1) * rate)]
            if seg:
                row.update(
                    accel_x=round(mean(a[0] for a in seg), 5),
                    accel_y=round(mean(a[1] for a in seg), 5),
                    accel_z=round(mean(a[2] for a in seg), 5),
                    accel_units="g",
                )
        rows.append(row)
    return rows


def parse_rove_accel_full(mp4: Path):
    """Full-rate (~16.6 Hz) accel triplets, for DUMP_FULL_RATE_IMU."""
    _gps_idx, accel_idx = _rove_streams(mp4)
    if accel_idx is None:
        return []
    ad = _rove_stream_bytes(mp4, accel_idx)
    n = len(ad) // ROVE_ACCEL_REC_SIZE
    return [struct.unpack_from("<3f", ad, i * ROVE_ACCEL_REC_SIZE) for i in range(n)]


# ============================ GOPRO HERO 13 ============================

def _parse_triplets(text: str, max_abs: float = 1000.0):
    """Group every number in an exiftool dump into (x, y, z). Outliers -> 0."""
    nums = []
    for t in _NUM_RE.findall(text):
        try:
            v = float(t)
        except ValueError:
            continue
        if v != v or abs(v) > max_abs:      # NaN or absurd
            v = 0.0
        nums.append(v)
    return [(nums[i], nums[i + 1], nums[i + 2]) for i in range(0, len(nums) - 2, 3)]


def _gopro_gps(mp4: Path):
    """10 Hz GPS samples straight from GPMF (satellite UTC, not the camera RTC)."""
    out = _run([EXIFTOOL, "-ee", "-n", "-api", "LargeFileSupport=1",
                "-p", "$GPSDateTime|$GPSLatitude|$GPSLongitude|$GPSSpeed"
                      "|$GPSMeasureMode|$GPSDOP",
                str(mp4)])
    samples = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 6 or not parts[0]:
            continue                     # warning lines / docs without GPS
        try:
            ts = datetime.strptime(parts[0], "%Y:%m:%d %H:%M:%S.%f").replace(tzinfo=UTC)
            lat, lon, spd = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        mode = parts[4].strip()          # "2" or "3" with -n
        samples.append({
            "ts": ts, "lat": lat, "lon": lon,
            # GPMF reports km/h; every other camera here reports mph.
            "speed_mph": spd * KMH_TO_MPH,
            "mode": mode, "dop": parts[5].strip(),
        })
    return samples


def _gopro_imu(mp4: Path, tag: str):
    txt = _run([EXIFTOOL, "-ee", "-api", "LargeFileSupport=1", "-b", f"-{tag}",
                str(mp4)])
    return _parse_triplets(txt)


def parse_gopro(mp4: Path):
    """
    10 Hz GPS with ~200 Hz accel and gyro averaged into each GPS sample window.

    Kept at native 10 Hz rather than downsampled to 1 Hz: it is the only camera
    with a gyro, and the extra resolution feeds the follow-map motion.
    """
    gps = _gopro_gps(mp4)
    if not gps:
        return []
    accel = _gopro_imu(mp4, "Accelerometer")
    gyro = _gopro_imu(mp4, "Gyroscope")

    a_rate = len(accel) / len(gps) if accel else 0.0
    g_rate = len(gyro) / len(gps) if gyro else 0.0

    rows = []
    for i, s in enumerate(gps):
        row = _blank_row()
        # GPSMeasureMode: 3 = 3D fix, 2 = 2D fix. A 2D fix has no reliable
        # altitude but lat/lon are still usable, so it counts as fix_ok.
        mode = s["mode"]
        row.update(
            camera="GOPRO Hero Black 13", clip=mp4.name, sample_idx=i,
            ts_utc_raw=s["ts"].isoformat(),
            lat_deg=round(s["lat"], 7), lon_deg=round(s["lon"], 7),
            speed_mph_from_camera=round(s["speed_mph"], 2),
            speed_source="from_camera",
            fix_status={"3": "3D", "2": "2D"}.get(mode, "nofix"),
            fix_ok=1 if mode in ("2", "3") else 0,
        )
        if a_rate:
            seg = accel[int(i * a_rate):int((i + 1) * a_rate)]
            if seg:
                row.update(accel_x=round(mean(a[0] for a in seg), 4),
                           accel_y=round(mean(a[1] for a in seg), 4),
                           accel_z=round(mean(a[2] for a in seg), 4),
                           accel_units="m/s^2")
        if g_rate:
            seg = gyro[int(i * g_rate):int((i + 1) * g_rate)]
            if seg:
                row.update(gyro_x=round(mean(a[0] for a in seg), 5),
                           gyro_y=round(mean(a[1] for a in seg), 5),
                           gyro_z=round(mean(a[2] for a in seg), 5),
                           gyro_units="rad/s")
        rows.append(row)
    return rows


# ============================ DISPATCH ============================

PARSERS = {
    "Garmin X310":         parse_garmin,
    "GOPRO Hero Black 13": parse_gopro,
    "REDTIGER F7N Touch":  parse_redtiger,
    "ROVE R2 4K Dual":     parse_rove,
}


def clip_start_utc(name: str, mp4: Path):
    """
    A clip's first valid RAW UTC timestamp, or None. Used to decide which clips
    fall inside the trip window without parsing every sample.
    """
    for row in PARSERS[name](mp4):
        if row["ts_utc_raw"]:
            return datetime.fromisoformat(row["ts_utc_raw"])
    return None
