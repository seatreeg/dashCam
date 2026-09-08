"""
Drive Telemetry Pipeline - shared configuration
===============================================

Single source of truth for paths, clock offsets, and render settings.
Every other module imports from here; nothing else hardcodes a path.

Drive: 2026-07-13, 05:28 - ~06:34 local (CDT), Lincoln NE.
Devices: 4 dashcams (Garmin X310, GoPro Hero 13 Black, REDTIGER F7N, ROVE R2 4K)
         + a Raspberry Pi logging OBD-II and trailer 4-way signals.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============================ PATHS ============================

PROJECT   = Path(r"E:\claude drive 7 13 2026")
RAW_BASE  = PROJECT / "Cut Raw Data 7 13 2026"

# Independent copy of the raw footage. trim_footage.py refuses to delete
# anything unless this exists and matches, since the trim is destructive.
BACKUP_BASE = Path(r"E:\Raw Drive Cam 7 13 2026")

PROCESS   = PROJECT / "Process Footage"
WORK      = PROCESS / "_work"
TILES_DIR = WORK / "tiles"
OVERLAY_DIR = WORK / "overlays"
PANEL_DIR = WORK / "panels"

RESULTS    = PROCESS / "results"
INDIVIDUAL = RESULTS / "individual"
COMBINED   = RESULTS / "combined"
COMBINED_CSV_DIR = COMBINED / "combined csv"
COMBINED_VID_DIR = COMBINED / "combined videos"

COMBINED_CSV = COMBINED_CSV_DIR / "combined_drive.csv"
FINAL_VIDEO  = COMBINED_VID_DIR / "drive_composite.mp4"

# ============================ BINARIES ============================
# Present on this machine but NOT on PATH, so they are referenced absolutely.

FFMPEG   = r"C:\Users\trogi\bin\ffmpeg\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe"
FFPROBE  = r"C:\Users\trogi\bin\ffmpeg\ffmpeg-8.1-essentials_build\bin\ffprobe.exe"
EXIFTOOL = r"C:\Users\trogi\AppData\Local\Programs\ExifTool\ExifTool.exe"

# ============================ TIME ============================

UTC = timezone.utc
# CDT. A fixed offset is correct here: the whole drive sits inside one hour
# block, so there is no DST transition to reason about.
LOCAL = timezone(timedelta(hours=-5), "CDT")

TRIP_START = datetime(2026, 7, 13, 5, 28, 0, tzinfo=LOCAL)   # 10:28:00Z
TRIP_END   = datetime(2026, 7, 13, 6, 40, 0, tzinfo=LOCAL)   # padded past the
                                                             # last clip (~06:34)

# --- Raspberry Pi clock ---
# The Pi's RTC was wrong in both date and time: it logged 2026-03-02 for a drive
# that happened 2026-07-13. So the correction is NOT a bare time-of-day delta --
# it is the gap between two anchor datetimes. Deriving it this way keeps the
# ~133-day date shift and the time-of-day shift in one number.
#
# Stated anchor: Pi-time 00:39:16 was given as the official drive start.
PI_ANCHOR_RAW        = datetime(2026, 3, 2, 0, 39, 16)          # naive, Pi clock
PI_ANCHOR_TRUE_LOCAL = datetime(2026, 7, 13, 5, 28, 0)          # naive, real local

# MEASURED correction on top of the stated anchor.
#
# The stated anchor alone gives an offset of 4:48:44, and it is wrong by ~2
# minutes. Cross-correlating the Pi's 10 Hz OBD speed against the cameras' GPS
# speed over the whole drive gives a sharp, unambiguous peak:
#
#     lag 0        r = 0.213     <- the stated anchor
#     lag -125.2s  r = 0.996     <- measured optimum
#
# Sweeping +/-30 min found exactly one peak and it falls off symmetrically, so
# this is a real alignment, not a coincidence. The resulting offset is 4:46:38.8,
# which matches the remembered "~4:46 ahead" -- the memory was right, and the
# 00:39:16 anchor was the imprecise part (it actually lands ~2 min BEFORE the
# drive started).
#
# Why -125.2 and not each camera's own peak: the four cameras individually peak
# at -124.2 (Garmin), -125.2 (REDTIGER), -125.6 (GoPro), -125.6 (Rove). That
# 1.4 s spread is the cameras' own speed-filter latencies, not clock error --
# Garmin is the outlier because it reports whole-mph integers, so its
# correlation is the least sharp of the four. -125.2 maximises the summed
# correlation across all four (mean r = 0.9965) instead of letting the coarsest
# sensor set the answer.
#
# Kept separate from the anchor above so the stated and measured values stay
# individually auditable. Re-derive any time with:  python check_sync.py
PI_MEASURED_CORRECTION_SEC = -125.2

PI_TIME_OFFSET = ((PI_ANCHOR_TRUE_LOCAL - PI_ANCHOR_RAW)
                  + timedelta(seconds=PI_MEASURED_CORRECTION_SEC))   # ~133d 4:46:39.8

# Drop Pi rows before this raw timestamp (pre-drive idling).
PI_CUTOFF_RAW = datetime(2026, 3, 2, 0, 39, 16)

# --- Camera clocks ---
# All four cameras stamp UTC from their GPS receivers and agree to the second,
# so these default to 0. Positive = camera clock is FAST (reads later than truth)
# and gets subtracted. Adjust here if the sync check shows drift.
#
# NOTE: the April trip measured the GoPro 9 s fast. That was a different session;
# it is NOT assumed here. Left at 0 until measured for this trip.
CAMERA_CLOCK_FAST_SEC = {
    "Garmin X310":         0,
    "GOPRO Hero Black 13": 0,
    "REDTIGER F7N Touch":  0,
    "ROVE R2 4K Dual":     0,
}

# ============================ CAMERAS ============================
# `key` is the short column prefix used in the combined CSV.
# `color` is the map trace color (RGB), chosen to stay distinct on satellite.

# `sample_period` is the seconds between GPS samples. It lets a clip's start time
# be derived arithmetically from any CSV row (ts - sample_idx * period) instead
# of re-parsing the file -- which for the GoPro means three exiftool passes over
# 5.78 GB just to read one timestamp.
CAMERAS = {
    "Garmin X310": {
        "key":     "garmin",
        "dir":     RAW_BASE / "Garmin X310" / "105UNSVD",
        "glob":    "*.MP4",
        "color":   (0, 229, 255),      # cyan
        "has_accel": False,
        "has_gyro":  False,
        "accel_units": "",
        "gyro_units":  "",
        "sample_period": 1.0,
    },
    "GOPRO Hero Black 13": {
        "key":     "gopro",
        "dir":     RAW_BASE / "GOPRO Hero Black 13" / "100GOPRO",
        "glob":    "GX*.MP4",
        "color":   (255, 0, 200),      # magenta
        "has_accel": True,
        "has_gyro":  True,             # the ONLY camera with a gyro
        "accel_units": "m/s^2",
        "gyro_units":  "rad/s",
        "sample_period": 0.1,          # 10 Hz
    },
    "REDTIGER F7N Touch": {
        "key":     "redtiger",
        "dir":     RAW_BASE / "REDTIGER F7N Touch" / "Movie_F",
        "glob":    "*.MP4",
        "color":   (255, 214, 0),      # amber
        "has_accel": True,             # 2-axis in practice: Z is always 0
        "has_gyro":  False,
        "accel_units": "g-force (coarse int, gravity-compensated)",
        "gyro_units":  "",
        "sample_period": 1.0,
    },
    "ROVE R2 4K Dual": {
        "key":     "rove",
        "dir":     RAW_BASE / "ROVE R2 4K Dual" / "Front",
        "glob":    "*.mp4",            # lowercase on this camera
        "color":   (0, 255, 106),      # green
        "has_accel": True,
        "has_gyro":  False,
        "accel_units": "g",
        "gyro_units":  "",
        "sample_period": 1.0,
    },
}

CAMERA_ORDER = list(CAMERAS.keys())  # left-to-right panel order in the video

PI_NAME    = "OBD II and Four Way Port"
PI_RAW_CSV = RAW_BASE / PI_NAME / "car_telemetry.csv"

# ============================ EXTRACTION ============================

# GoPro reports GPS speed in km/h (verified: values quantize to 0.0036 = 1mm/s
# in km/h, and cross-check against Garmin mph at matched seconds). The other
# three cameras already report mph.
KMH_TO_MPH = 0.621371

# Also write full-rate IMU CSVs (GoPro ~200 Hz, Rove ~16.6 Hz) alongside the
# per-second ones. Off by default: large, and the 1 Hz join does not use them.
DUMP_FULL_RATE_IMU = False

MIN_FILE_SIZE = 1024  # outputs smaller than this are treated as failed/empty

# ============================ VIDEO ============================

OUT_W, OUT_H = 2560, 1440
FPS = 30

# Bump when the PANEL geometry or build method changes, so cached panels are
# rebuilt rather than silently reused at the wrong size.
PANEL_VERSION = 2

# How often the telemetry layer is redrawn. Two things depend on it:
#   - follow-map smoothness (position is interpolated between 1 Hz GPS samples)
#   - TAIL-LIGHT BLINK FIDELITY. Turn signals flash at ~1.5 Hz and the wire state
#     is point-sampled from the Pi's 10 Hz log at each frame's exact instant, so
#     5 fps (~3 samples per blink cycle) is enough to read as blinking. Raise to
#     10 for a crisper flash at roughly double the overlay render time; drop to
#     2-3 for quick layout iteration, where the blink stops being meaningful.
# Rendering at the full 30 fps would mean ~119k PIL frames for no visible gain.
OVERLAY_FPS = 5

# Layout.
#
# The camera panels span the FULL width rather than sharing the top row with the
# maps. Four panels across 2560 gives 640x270 each -- 78% more picture than the
# old 480x270, which was capped by squeezing them into a 1920 left column. The
# readouts sit directly under their own panel (also full width, so they stay
# aligned), and the maps move below, on the right.
#
# The cost: the maps lose height, so the overview drops one zoom level (see
# ZOOM_OVERVIEW_MAX_H below). That is the trade for larger footage.
PANEL_W, PANEL_H = OUT_W // 4, 360       # 640x360, four across, full width
PANEL_Y = 0
READOUT_Y = PANEL_Y + PANEL_H            # per-camera readings under each panel
READOUT_H = 260

# Below the readouts: left column (OBD + bottom), right column (maps).
LEFT_W  = 1920
RIGHT_W = OUT_W - LEFT_W          # 640

PI_PANEL_Y = READOUT_Y + READOUT_H       # 620 -- OBD + four-way block
PI_PANEL_H = 400                         # was 440
BOTTOM_Y = PI_PANEL_Y + PI_PANEL_H       # 1020 -- speed + tail-light circles
BOTTOM_H = OUT_H - BOTTOM_Y              # 420, was 440

MAP_W = RIGHT_W
MAP_TOP = PI_PANEL_Y                     # maps start below the readouts
MAP_OVERVIEW_H = 510
MAP_OVERVIEW_BOX = (LEFT_W, MAP_TOP, OUT_W, MAP_TOP + MAP_OVERVIEW_H)
MAP_FOLLOW_BOX   = (LEFT_W, MAP_TOP + MAP_OVERVIEW_H, OUT_W, OUT_H)

# Esri World Imagery. Free, no API key. Tiles are fetched once for the drive
# bbox, stitched into one basemap per zoom, and cached -- per-frame map drawing
# is then a crop of that cached image, so re-renders need no network.
TILE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TILE_SIZE = 256
MAX_TILES = 400          # caps the auto-selected zoom so a big bbox can't
                         # trigger a runaway download
TILE_FETCH_DELAY = 0.12  # seconds between tile requests. The server drops
                         # connections on an unthrottled bulk pull; this costs
                         # ~30 s once and saves a map full of grey holes.
# Follow-map zoom. This is the knob that decides whether per-camera GPS bias is
# actually readable:
#   z17 = ~0.90 m/px -> the cameras' ~6 m spread is ~7 px; the four traces merge
#                       into one line and you cannot separate them.
#   z18 = ~0.45 m/px -> ~13 px apart; the trail reads as four distinct ribbons.
#                       Covers ~290 m, so at 39 mph a 25 s trail runs off the
#                       edge -- fine, it is a trailing tail, not a full track.
#   z19 = ~0.23 m/px -> more separation still, but only ~145 m of context.
ZOOM_FOLLOW = 18

# The route is driven 3x. Points darken with elapsed time so the repeats are
# visually separable: brightness scales from FULL down to DARK over the drive.
TRACE_BRIGHT_START = 1.0
TRACE_BRIGHT_END   = 0.35
TRACE_DOT_R = 1.6

# EVERY MAP DOT IS PLOTTED AT ITS TRUE COORDINATE. No jitter, no dodge.
#
# Comparing the cameras' GPS bias against each other is a goal of this project,
# so displacing the traces to make them legible would destroy the very signal
# being collected. An earlier version nudged each camera ~2 px (~7 m) apart and
# that was wrong: it was larger than the real ~6 m disagreement it was hiding.
#
# The legibility problem it was solving is real, though. On the overview map
# (~3.6 m/px) all four cameras land inside ~2 px, so whoever draws last covers
# the rest -- the map came out entirely Rove-green. It is now solved by rotating
# the DRAW ORDER per sample (see make_video.phase_overlays): positions stay
# exact, and which colour sits on top just cycles, so all four show as speckle.
#
# To actually READ the bias, use the follow map: at ~0.9 m/px, 6 m is ~7 px and
# the four traces separate visibly. That is what FOLLOW_TRAIL_SEC is for.
TRACE_DODGE = None   # retained as an explicit "we do not do this"

# Follow map: how many seconds of per-camera history to trail behind the car.
# This is the bias view -- four coloured tracks, true positions, close enough
# together to compare. Older points fade toward FOLLOW_TRAIL_FADE_TO.
FOLLOW_TRAIL_SEC = 25
FOLLOW_TRAIL_FADE_TO = 0.15
FOLLOW_TRAIL_DOT_R = 2.5
# Current-position dot. Keep it SMALLER than the inter-camera separation
# (~13 px at z18) or the markers occlude the bias they exist to show -- at r=5
# on z17 they overlapped into a single blob.
FOLLOW_DOT_R = 3

# Bump when the overlay DRAWING code changes. It rides in the overlay's cache
# stamp, so editing the layout invalidates the cached overlay by itself --
# without this you would have to remember to pass FORCE, and FORCE also rebuilds
# the 1.5 h panels phase you did not touch.
LAYOUT_VERSION = 3

# Trailer 4-way wiring. White is the ground return and carries no signal, so it
# is not logged and not displayed.
LEFT_WIRE  = "Yellow_Wire"   # left tail light  (brake OR blinker)
RIGHT_WIRE = "Green_Wire"    # right tail light (brake OR blinker)

# ============================ HELPERS ============================

def ensure_dirs():
    """Create every output directory. Safe to call repeatedly."""
    dirs = [PROCESS, WORK, TILES_DIR, OVERLAY_DIR, PANEL_DIR,
            RESULTS, INDIVIDUAL, COMBINED, COMBINED_CSV_DIR, COMBINED_VID_DIR]
    dirs += [INDIVIDUAL / name for name in CAMERAS]
    dirs.append(INDIVIDUAL / PI_NAME)
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def camera_csv(name: str) -> Path:
    """Path of the per-camera individual CSV."""
    return INDIVIDUAL / name / f"{CAMERAS[name]['key']}.csv"


def camera_imu_csv(name: str) -> Path:
    """Path of the optional full-rate IMU dump (see DUMP_FULL_RATE_IMU)."""
    return INDIVIDUAL / name / f"{CAMERAS[name]['key']}_imu_full_rate.csv"


PI_CSV = INDIVIDUAL / PI_NAME / "obd_four_way.csv"


def cached(p: Path, min_size: int = MIN_FILE_SIZE) -> bool:
    """True if p exists and is big enough to be a real output, not a stub."""
    return p.exists() and p.stat().st_size > min_size


def fmt_t(sec: float) -> str:
    """Duration in seconds -> mm:ss or h:mm:ss."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def pi_raw_to_true(raw: datetime) -> datetime:
    """Pi wall-clock timestamp -> true local time."""
    return (raw + PI_TIME_OFFSET).replace(tzinfo=LOCAL)


def camera_raw_to_true(name: str, raw_utc: datetime) -> datetime:
    """Camera UTC timestamp -> true UTC, correcting for a fast clock."""
    return raw_utc - timedelta(seconds=CAMERA_CLOCK_FAST_SEC[name])
