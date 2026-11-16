"""External programs are on PATH, or supplied by these environment variables."""


import os
import shutil
import json
from pathlib import Path
from datetime import timezone

EXIFTOOL = os.environ.get("EXIFTOOL", shutil.which("exiftool") or "exiftool")
FFMPEG = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", shutil.which("ffprobe") or "ffprobe")
KMH_TO_MPH = 0.621371
UTC = timezone.utc
SENSOR_CALIBRATION_PATH = Path(__file__).with_name("sensorCalibration.json")
SENSOR_CALIBRATION = json.loads(SENSOR_CALIBRATION_PATH.read_text(encoding="utf-8"))
