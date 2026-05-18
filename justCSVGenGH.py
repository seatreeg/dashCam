"""
REQUIRED DIRS

  Raw video sources:
    D:\\claudeDrive\\drive cam\\all raw\\gopro\\DCIM\\100GOPRO\\GX*.MP4
    D:\\claudeDrive\\drive cam\\all raw\\garmin\\DCIM\\105UNSVD\\*.MP4
    D:\\claudeDrive\\drive cam\\all raw\\garmin\\DCIM\\102SAVED\\*.MP4
    D:\\claudeDrive\\drive cam\\all raw\\RedTiger\\CARDV\\Movie_F\\*.MP4
    D:\\claudeDrive\\drive cam\\all raw\\Rove\\Video\\Front\\*.mp4

  OBD-II log:
    D:\\claudeDrive\\drive cam\\all raw\\OBD\\JTENU5JR8P6211096\\CSVLog_20260426_010317.csv

  External binaries:
    ffmpeg.exe  
    ffprobe.exe  
    exiftool.exe 

"""

###
# STARTS
###

import argparse
import csv
import math
import re
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

from PIL import Image, ImageDraw, ImageFont
from staticmap import StaticMap, CircleMarker


# input paths

RAW_BASE = Path(r"D:\camdriveproj 5 10 2026\claudeDrive\drive cam\all raw")
GOPRO_DIR    = RAW_BASE / "gopro" / "DCIM" / "100GOPRO"
GARMIN_DIR  = RAW_BASE / "garmin" / "DCIM" / "105UNSVD"
REDTIGER_DIR = RAW_BASE / "RedTiger" / "CARDV" / "Movie_F"
ROVE_DIR     = RAW_BASE / "Rove" / "Video" / "Front"
OBD_CSV      = RAW_BASE / "OBD" / "JTENU5JR8P6211096" / "CSVLog_20260426_010317.csv"



# output paths
OUT_BASE = Path(r"D:\camdriveproj 5 10 2026\claudeDrive\drive cam\just_csvs")
WORK             = OUT_BASE / "_work"
GPS_DIR          = WORK / "gps"

GARMIN_GPS_CSV   = GPS_DIR / "garmin_gps.csv"
REDTIGER_GPS_CSV = GPS_DIR / "redtiger_gps.csv"
ROVE_GPS_CSV     = GPS_DIR / "rove_gps.csv"
GOPRO_PER_SEC_CSV = GPS_DIR / "gopro_per_sec.csv"
SYNC_CSV         = WORK / "trip_synced_1hz.csv"


# external binaries
FFMPEG   = r"C:\Users\coeno\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
FFPROBE  = r"C:\Users\coeno\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffprobe.exe"
EXIFTOOL = r"C:\Users\coeno\AppData\Local\Programs\ExifTool\ExifTool.exe"


# time and offset
UTC = timezone.utc
TRIP_START = datetime(2026, 4, 26,  5, 50, 0, tzinfo=UTC) 
TRIP_END   = datetime(2026, 4, 26,  7,  0, 0, tzinfo=UTC)
GOPRO_CLOCK_FAST_SEC = 9   # amount of time, in secs, that the gopro is ahead
OBD_LATE_BY_SEC      = 13  # amount of time, in secs, that the obd csv is ahead


MIN_FILE_SIZE = 1024  # any output smaller than this is treated as failed/empty. 
# this is an arbitrary threshold to prevent issues later on in case a csv write is partial


###
# magic numbers and video format parsing
###


"""

the uuid is obtained from
from https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/QuickTime.pm
            Name => 'GarminGPS',
            Condition => q{
                $$valPt=~/^\x9b\x63\x0f\x8d\x63\x74\x40\xec\x82\x04\xbc\x5f\xf5\x09\x17\x28/ and
                $$self{OPTIONS}{ExtractEmbedded}
            },
            # you can also see it in the hex code if you search uuid

also at the uuid in the hex, you see the nofix pattern at the begining of the trip after each index
before the values start showning. 

the way the no fix pattern is by finding the first reported coords, 
garmins is -2147483648 and roves is 0x41efffffffe00000

redtiger does not have a nofix magic number because it has a separate fix flag
(active[A] vs void [V])
https://gpsd.gitlab.io/gpsd/NMEA.html


"""


# Garmin Dash Cam X310 stores 1Hz GPS in a private uuid atom keyed by this val
GARMIN_GPS_UUID = bytes.fromhex("9b630f8d637440ec8204bc5ff5091728")
MP4_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)  # epoch system in quiicktime uses this date
NO_FIX_I32 = -2147483648  # Garmin GPS no-fix sentinel, value sent when gps has "no fix" (no lat/long data from gps, gps issues)
# 80_000_000 in hex

# no fix sentinel for rove r2 4k dual
ROVE_NO_FIX_DOUBLE_BITS = 0x41efffffffe00000


# regex for gopro IMU triplet parsing
# eg, Accelerometer: 0.123 -0.456 9.81 and gyro at -2.3e-7 m/s ->  ['0.123', '-0.456', '9.81', '-2.3e-7']
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d+")

###
# helpers
###

def fmt_t(sec: float) -> str:
    """
    format duration in seconds as string
     mm:ss or h:mm:ss.
    """
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def cached(p: Path, min_size: int = MIN_FILE_SIZE) -> bool:
    """
    path is considered 'cached' if it exists AND is larger than min size stated earlier
    """
    return p.exists() and p.stat().st_size > min_size





def run_with_progress(cmd, total_seconds: float, label: str):
    """
    run an ffmpeg command and print a live percentage bar.
    """
    full = list(cmd) + ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(
        full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=1, text=True,
    )
    stderr_lines = []

    def _drain():
        for line in proc.stderr:
            stderr_lines.append(line)
    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    last_print = 0.0
    last_sec   = 0.0
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                # ffmpeg reports negative microseconds before the first frame; ignore.
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if us < 0:
                    continue
                last_sec = us / 1e6
                # Throttle output to twice per second so terminal isn't spammed.
                now = time.time()
                if now - last_print >= 0.5:
                    pct = min(100.0, last_sec / total_seconds * 100) if total_seconds > 0 else 0.0
                    print(f"\r  {label}: {fmt_t(last_sec)}/{fmt_t(total_seconds)} "
                          f"({pct:5.1f}%)   ", end="", flush=True)
                    last_print = now
            elif line == "progress=end":
                break
    finally:
        rc = proc.wait()
        drainer.join(timeout=2)

    pct_final = min(100.0, last_sec / total_seconds * 100) if total_seconds > 0 else 100.0
    print(f"\r  {label}: {fmt_t(last_sec)}/{fmt_t(total_seconds)} "
          f"({pct_final:5.1f}%) done" + " " * 20)
    if rc != 0:
        err = "".join(stderr_lines)
        if err:
            print(err, file=sys.stderr)
        raise subprocess.CalledProcessError(rc, full, output="", stderr=err)

def loop_progress(label: str, i: int, n: int, every: int = 100):
    """
    print a one-line progress update every 'every' iterations and at the end
    """
    if i == n - 1 or i % every == 0:
        pct = ((i + 1) / n * 100) if n else 100.0
        print(f"\r  {label}: {i + 1}/{n} ({pct:5.1f}%)   ", end="", flush=True)
    if i == n - 1:
        print()  # newline at the end

def ensure_dirs():
    """
    create dirs if needed
    """
    for d in (WORK, GPS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def utc_key(dt: datetime) -> int:
    """
    truncate a datetime to whole-second UTC epoch
    """
    return int(dt.timestamp())


def key_to_iso(k: int) -> str:
    """
    convert whole-second UTC epoc back to iso string
    """
    return datetime.fromtimestamp(k, UTC).isoformat()


def in_trip(dt: datetime) -> bool:
    """
    determine if time is within our trip bounds
    """
    return TRIP_START <= dt <= TRIP_END



###
# prep parse cam
###


def find_atom(fp, start, end, target):
    """
    find the first atom with the given 4-byte name within [start, end).
    Recurses into the standard container atoms (moov / trak / mdia / minf /
    stbl / udta). Returns (offset, size) or None
    """
    pos = start
    while pos < end - 8:
        fp.seek(pos)
        hdr = fp.read(8)
        if len(hdr) < 8:
            return None
        size = struct.unpack(">I", hdr[:4])[0]
        name = hdr[4:8]
        # 64-bit extended size variant.
        if size == 1:
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
    """Find the first 'uuid' atom whose 16-byte UUID body matches target_uuid.
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
            u = fp.read(16)
            if u == target_uuid:
                return pos, size, body_start + 16
        if name in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"):
            r = find_uuid_atom(fp, body_start, pos + size, target_uuid)
            if r:
                return r
        if size <= 0:
            return None
        pos += size
    return None



###
# parse cams
###

### A-Garmin X310 ___===


def parse_garmin_gps(mp4_path: Path):
    """
    return list of dict rows with 1Hz GPS samples for one garmin clip
    """
    with open(mp4_path, "rb") as fp:
        fp.seek(0, 2); filesize = fp.tell()
        found = find_uuid_atom(fp, 0, filesize, GARMIN_GPS_UUID)
        if not found:
            return []
        atom_off, atom_size, body_off = found
        body_len = atom_off + atom_size - body_off
        fp.seek(body_off)
        body = fp.read(body_len)

    if len(body) < 17:
        return []
    # Fixed-width header: 4 LE int32. Values observed: (0, 20, 0, 60).
    _h0, rec_size, _h2, _h3 = struct.unpack("<4I", body[:16])
    actual = body[16]
    if rec_size != 20:
        return []
    payload = body[17:]
    n = len(payload) // rec_size
    actual = min(actual, n)
    SCALE = 360.0 / (1 << 32)
    rows = []
    for i in range(actual):
        rec = payload[i * rec_size : (i + 1) * rec_size]
        ts, pad1, _pad2, lat_i, lon_i = struct.unpack(">IIIii", rec)
        speed_mph = (pad1 >> 16) & 0xFF  # second byte of the pad1 BE word
        no_fix = (lat_i == NO_FIX_I32) or (lon_i == NO_FIX_I32)
        utc = (MP4_EPOCH + timedelta(seconds=ts)).isoformat()
        rows.append({
            "clip": mp4_path.name,
            "sample_idx": i,
            "ts_utc": utc,
            "lat_deg": "" if no_fix else round(lat_i * SCALE, 7),
            "lon_deg": "" if no_fix else round(lon_i * SCALE, 7),
            "speed_mph": speed_mph,
            "fix": 0 if no_fix else 1,
        })
    return rows


def extract_garmin_gps():
    """
    iterate through Garmin DCIM dirs and write GARMIN_GPS_CSV
    """
    print("  [Garmin] scanning DCIM dirs ...")
    all_rows = []
    files = []
    #for d in GARMIN_DIR:
    #    files.extend(sorted(d.glob("*.MP4")))
    files.extend(sorted(GARMIN_DIR.glob("*.MP4")))

    
    for i, f in enumerate(files):
        rows = parse_garmin_gps(f)
        n_fix = sum(1 for r in rows if r.get("fix") == 1)
        print(f"    {f.name:32s}  records={len(rows):3d}  fix={n_fix:3d}")
        all_rows.extend(rows)
    fields = ["clip", "sample_idx", "ts_utc", "lat_deg", "lon_deg", "speed_mph", "fix"]
    with GARMIN_GPS_CSV.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"  [Garmin] wrote {len(all_rows)} rows -> {GARMIN_GPS_CSV.name}")


### B-REDTIGER F7N extractor (Novatek freeGPS / YOUQINGGPS) ___===


def ddmm_to_decimal(v: float) -> float:
    """
    convert NMEA DDDMM.MMMM to decimal degrees
    """
    sign = -1.0 if v < 0 else 1.0
    v = abs(v)
    deg = int(v // 100)
    minutes = v - deg * 100
    return sign * (deg + minutes / 60.0)



def parse_redtiger_chunk(buf: bytes):
    """
    parse one 0x4000 byte freeGPS chunk into a row dict, or None if invalid
    """
    if len(buf) < 100 or buf[4:11] != b"freeGPS":
        return None
    lat_raw, = struct.unpack_from("<f", buf, 40)
    lon_raw, = struct.unpack_from("<f", buf, 44)
    hour, minute, second, year, month, day = struct.unpack_from("<6I", buf, 48)
    active  = chr(buf[72]) if buf[72] else "?"
    lat_hem = chr(buf[73]) if buf[73] else "?"
    lon_hem = chr(buf[74]) if buf[74] else "?"
    if active != "A":
        return {
            "active": active, "lat_hem": lat_hem, "lon_hem": lon_hem,
            "hour": hour, "minute": minute, "second": second,
            "year": year, "month": month, "day": day,
            "lat_deg": "", "lon_deg": "",
        }
    lat = ddmm_to_decimal(lat_raw)
    if lat_hem == "S":
        lat = -lat
    lon = ddmm_to_decimal(lon_raw)
    if lon_hem == "W":
        lon = -lon
    return {
        "active": active, "lat_hem": lat_hem, "lon_hem": lon_hem,
        "hour": hour, "minute": minute, "second": second,
        "year": year, "month": month, "day": day,
        "lat_deg": round(lat, 7), "lon_deg": round(lon, 7),
    }

def parse_redtiger_clip(mp4_path: Path):
    with open(mp4_path, "rb") as fp:
        fp.seek(0, 2); filesize = fp.tell()
        found = find_atom(fp, 0, filesize, b"gps ")
        if not found:
            return []
        gps_off, gps_size = found
        # The gps body is 8 byte header (version, count) then count x
        # 8 byte (chunk_offset, chunk_size) records pointing into mdat.
        fp.seek(gps_off + 8)
        body = fp.read(gps_size - 8)
        n_records = struct.unpack(">I", body[4:8])[0]
        rows = []
        for i in range(n_records):
            rec = body[8 + i * 8 : 8 + (i + 1) * 8]
            chunk_off, _chunk_len = struct.unpack(">II", rec)
            if chunk_off + 100 > filesize:
                continue
            fp.seek(chunk_off)
            chunk = fp.read(256)
            parsed = parse_redtiger_chunk(chunk)
            if parsed is None:
                continue
            try:
                year_full = parsed["year"]
                if year_full < 100:
                    year_full += 2000
                ts = datetime(year_full, parsed["month"], parsed["day"],
                              parsed["hour"], parsed["minute"], parsed["second"],
                              tzinfo=UTC).isoformat()
            except (ValueError, KeyError):
                ts = ""
            parsed["clip"] = mp4_path.name
            parsed["sample_idx"] = i
            parsed["ts_utc"] = ts
            rows.append(parsed)
    return rows



def extract_redtiger_gps():
    print("  [RedTiger] scanning Movie_F ...")
    all_rows = []
    for mp4 in sorted(REDTIGER_DIR.glob("*.MP4")):
        rows = parse_redtiger_clip(mp4)
        n_fix = sum(1 for r in rows if r.get("active") == "A")
        print(f"    {mp4.name:40s}  records={len(rows):3d}  fix={n_fix:3d}")
        all_rows.extend(rows)
    fields = ["clip", "sample_idx", "ts_utc", "active",
              "lat_hem", "lon_hem", "lat_deg", "lon_deg",
              "hour", "minute", "second", "year", "month", "day"]
    with REDTIGER_GPS_CSV.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"  [RedTiger] wrote {len(all_rows)} rows -> {REDTIGER_GPS_CSV.name}")




### C-Rove R2-4K Dual GPS extractor (SStarMeta)


def find_rove_meta_stream(mp4_path: Path):
    """
    ffprobe the file and return the index of the SStarMeta data stream
    that carries the 32 byte 1hz gps samples 
    
    rove writes 3 SStarMeta tracks;
    only one has ~60 frames per clip
    
    returns int or None.
    """
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-of", "compact",
         "-show_entries",
         "stream=index,codec_type,codec_tag_string,nb_frames:stream_tags=handler_name",
         str(mp4_path)],
        capture_output=True, text=True, errors="replace", check=True,
    )
    candidates = []
    for line in r.stdout.splitlines():
        fields = dict(p.split("=", 1) for p in line.split("|") if "=" in p)
        if fields.get("codec_type") == "data" and fields.get("codec_tag_string") == "ssmd":
            try:
                nb = int(fields.get("nb_frames", "0"))
            except ValueError:
                nb = 0
            candidates.append((int(fields["index"]), nb))
    for idx, nb in candidates:
        if 30 <= nb <= 120:
            return idx
    return candidates[0][0] if candidates else None


def parse_rove_sample(rec: bytes):
    """
    parse one 32n byte SStarMeta sample. Returns dict (with fix=0 / no
    lat/lon if the no fix sentinel is detected)
    
    """
    if len(rec) < 32:
        return {}
    lat_bits = struct.unpack_from("<Q", rec, 0)[0]
    lon_bits = struct.unpack_from("<Q", rec, 8)[0]
    lat_d    = struct.unpack_from("<d", rec, 0)[0]
    lon_d    = struct.unpack_from("<d", rec, 8)[0]
    no_fix = (
        lat_bits == ROVE_NO_FIX_DOUBLE_BITS or lon_bits == ROVE_NO_FIX_DOUBLE_BITS
        or not (1.0 <= abs(lat_d) < 9000.0)
        or not (1.0 <= abs(lon_d) < 18000.0)
    )
    speed_mph = rec[20]; day = rec[22]; month = rec[23]; year2k = rec[24]
    hour      = rec[25]; minute = rec[26]; second = rec[27]; bearing = rec[28]
    if no_fix:
        return {"fix": 0, "lat_deg": "", "lon_deg": "",
                "speed_mph": speed_mph, "bearing_raw": bearing,
                "year2k": year2k, "month": month, "day": day,
                "hour": hour, "minute": minute, "second": second}
    return {
        "fix": 1,
        "lat_deg": round(ddmm_to_decimal(lat_d), 7),
        "lon_deg": round(ddmm_to_decimal(lon_d), 7),
        "speed_mph": speed_mph, "bearing_raw": bearing,
        "year2k": year2k, "month": month, "day": day,
        "hour": hour, "minute": minute, "second": second,
    }


def parse_rove_clip(mp4_path: Path):
    """
    extract the SStarMeta data stream via ffmpeg (writes a temp .bin),
    then parse its 32 byte records into rows
    """
    stream_idx = find_rove_meta_stream(mp4_path)
    if stream_idx is None:
        return []
    out_bin = GPS_DIR / f"{mp4_path.stem}_meta.bin"
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(mp4_path),
         "-map", f"0:{stream_idx}", "-c", "copy", "-f", "data", str(out_bin)],
        check=True,
    )
    data = out_bin.read_bytes()
    out_bin.unlink()  # don't leave the temp .bin lying around
    rows = []
    for i in range(len(data) // 32):
        rec = data[i * 32:(i + 1) * 32]
        parsed = parse_rove_sample(rec)
        try:
            ts_utc = datetime(2000 + parsed["year2k"], parsed["month"], parsed["day"],
                              parsed["hour"], parsed["minute"], parsed["second"],
                              tzinfo=UTC).isoformat()
        except (ValueError, KeyError):
            ts_utc = ""
        parsed["clip"] = mp4_path.name
        parsed["sample_idx"] = i
        parsed["ts_utc"] = ts_utc
        rows.append(parsed)
    return rows

def extract_rove_gps():
    print("  [Rove] scanning Front ...")
    files = sorted(ROVE_DIR.glob("*.mp4"))
    all_rows = []
    for mp4 in files:
        rows = parse_rove_clip(mp4)
        n_fix = sum(1 for r in rows if r.get("fix") == 1)
        print(f"    {mp4.name:50s}  records={len(rows):3d}  fix={n_fix:3d}")
        all_rows.extend(rows)
    fields = ["clip", "sample_idx", "ts_utc", "fix", "lat_deg", "lon_deg",
              "speed_mph", "bearing_raw",
              "year2k", "month", "day", "hour", "minute", "second"]
    with ROVE_GPS_CSV.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"  [Rove] wrote {len(all_rows)} rows -> {ROVE_GPS_CSV.name}")



### gopro hero (imu)


def parse_triplets(text: str, max_abs: float = 1000.0):
    """
    pull all numbers out of the exiftool text output and group as (x,y,z)
    
    numbers above |max_abs| are set to 0 
    """
    nums = []
    for t in _NUM_RE.findall(text):
        try:
            v = float(t)
        except ValueError:
            continue
        if abs(v) > max_abs or v != v:
            v = 0.0
        nums.append(v)
    return [(nums[i], nums[i+1], nums[i+2]) for i in range(0, len(nums) - 2, 3)]


def gopro_create_utc_and_dur(mp4: Path) -> tuple[datetime | None, float]:
    """
    exiftool CreateDate - 9 sec +
    the clip duration in seconds
    """
    out = subprocess.run(
        [EXIFTOOL, "-s", "-s", "-s", "-CreateDate", "-Duration#",
         "-api", "QuickTimeUTC=1", str(mp4)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    if len(out) < 2:
        return None, 0.0
    cd = datetime.strptime(out[0], "%Y:%m:%d %H:%M:%S%z").astimezone(UTC)
    cd = cd - timedelta(seconds=GOPRO_CLOCK_FAST_SEC)
    dur = float(out[1]) if out[1] else 0.0
    return cd, dur


def gopro_clip_per_sec(mp4: Path):
    """
    return list of dict rows, one per UTC second 

    row schema: ts_utc, clip, t_sec, accel_x/y/z, gyro_x/y/z (averaged).
    
    """
    print(f"    {mp4.name:30s} ", end="", flush=True)
    create_utc, duration_s = gopro_create_utc_and_dur(mp4)
    if create_utc is None:
        print("(no CreateDate, skipping)")
        return []
    accel_txt = subprocess.run(
        [EXIFTOOL, "-ee", "-api", "LargeFileSupport=1", "-b", "-Accelerometer", str(mp4)],
        capture_output=True, check=True
    ).stdout.decode("utf-8", errors="replace")
    gyro_txt = subprocess.run(
        [EXIFTOOL, "-ee", "-api", "LargeFileSupport=1", "-b", "-Gyroscope", str(mp4)],
        capture_output=True, check=True
    ).stdout.decode("utf-8", errors="replace")
    accel = parse_triplets(accel_txt)
    gyro  = parse_triplets(gyro_txt)
    if duration_s <= 0:
        # Fallback: derive duration from sample counts at typical rates.
        duration_s = max(len(accel) / 200.0, len(gyro) / 1600.0)
    n_seconds = int(round(duration_s))
    accel_per_s = (len(accel) / duration_s) if duration_s > 0 else 0
    gyro_per_s  = (len(gyro)  / duration_s) if duration_s > 0 else 0
    print(f"accel={len(accel):>6d}  gyro={len(gyro):>6d}  dur={duration_s:>6.2f}s")
    rows = []
    for s in range(n_seconds):
        a0, a1 = int(s * accel_per_s), int((s + 1) * accel_per_s)
        g0, g1 = int(s * gyro_per_s ), int((s + 1) * gyro_per_s )
        accs = accel[a0:a1]
        gyrs = gyro[g0:g1]
        ts = (create_utc + timedelta(seconds=s)).isoformat()
        rec = {"ts_utc": ts, "clip": mp4.name, "t_sec": s,
               "accel_x": "", "accel_y": "", "accel_z": "",
               "gyro_x":  "", "gyro_y":  "", "gyro_z":  ""}
        if accs:
            rec["accel_x"] = round(mean(x for x, _, _ in accs), 4)
            rec["accel_y"] = round(mean(y for _, y, _ in accs), 4)
            rec["accel_z"] = round(mean(z for _, _, z in accs), 4)
        if gyrs:
            rec["gyro_x"] = round(mean(x for x, _, _ in gyrs), 5)
            rec["gyro_y"] = round(mean(y for _, y, _ in gyrs), 5)
            rec["gyro_z"] = round(mean(z for _, _, z in gyrs), 5)
        rows.append(rec)
    return rows


def extract_gopro_per_sec():
    """
    iterate gopro clips and write GOPRO_PER_SEC_CSV. 
    """
    print("  [GoPro] scanning 100GOPRO (slow: exiftool reads full GPMF stream) ...")
    files = sorted(GOPRO_DIR.glob("GX*.MP4"))
    all_rows = []
    t0 = time.time()
    for i, mp4 in enumerate(files):
        rows = gopro_clip_per_sec(mp4)
        all_rows.extend(rows)
        elapsed = time.time() - t0
        eta = elapsed / (i + 1) * (len(files) - i - 1)
        print(f"      ({i + 1}/{len(files)} clips, "
              f"elapsed {fmt_t(elapsed)}, eta {fmt_t(eta)})")
    fields = ["ts_utc", "clip", "t_sec",
              "accel_x", "accel_y", "accel_z",
              "gyro_x",  "gyro_y",  "gyro_z"]
    with GOPRO_PER_SEC_CSV.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"  [GoPro] wrote {len(all_rows)} rows -> {GOPRO_PER_SEC_CSV.name}")



###
# phase 1
###


def phase1_extract(force: bool):
    """
    extract GPS / IMU from each camera
    
    each of the
    four extractors only runs if its output CSV is missing or --force.
    """
    targets = [
        ("Garmin",   GARMIN_GPS_CSV,   extract_garmin_gps),
        ("RedTiger", REDTIGER_GPS_CSV, extract_redtiger_gps),
        ("Rove",     ROVE_GPS_CSV,     extract_rove_gps),
        ("GoPro",    GOPRO_PER_SEC_CSV, extract_gopro_per_sec),
    ]
    for name, csv_path, runner in targets:
        if force and csv_path.exists():
            csv_path.unlink()
        if cached(csv_path):
            n_rows = sum(1 for _ in csv.reader(open(csv_path))) - 1
            print(f"  [{name}] cached: {csv_path.name} ({n_rows} rows)")
            continue
        runner()


###
# main()
###

PHASE_TABLE = [
    ("extract",   "Extract GPS+IMU from raw videos",     phase1_extract),
    #("sync",      "Build trip_synced_1hz.csv",           phase2_build_sync),
    #("overlays",  "Render per-second PNG overlays",      phase3_overlays),
    #("tiles",     "Encode per-camera tile videos",       phase4_tiles),
    #("panels",    "Encode panel videos from PNG seqs",   phase5_panels),
    #("composite", "Final 1920x1080 composite",           phase6_composite),
]



def main():
    ap = argparse.ArgumentParser(
        description="Single-file dashcam composite pipeline (raw -> trip_composite.mp4).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--force", action="store_true",
                    help="Invalidate caches and re-run every phase.")
    ap.add_argument("--phase", type=int, default=0, metavar="N",
                    help="Run only phase N (1-6). Default 0 = all phases.")
    args = ap.parse_args()

    ensure_dirs()
    print(f"Output base: {OUT_BASE}")

    pipeline_t0 = time.time()
    for n, (label, desc, fn) in enumerate(PHASE_TABLE, start=1):
        if args.phase and args.phase != n:
            continue
        print(f"\n=== Phase {n}/6: {label} - {desc} ===")
        t0 = time.time()
        fn(args.force)
        print(f"  -- phase {n} done in {fmt_t(time.time() - t0)}")

    elapsed = time.time() - pipeline_t0
    print(f"\nPipeline complete in {fmt_t(elapsed)}")



if __name__ == "__main__":
    main()
