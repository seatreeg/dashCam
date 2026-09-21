"""browser gorpro review clips without changing source recordings"""

import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


def clipTiming(row, settings, duration, lastAnnotation):
    origin = datetime.fromisoformat(settings["driveStartUtc"])
    period = settings["samplePeriodSec"]["gopro"]
    correction = settings["cameraClockFastSec"]["gopro"]
    clipStart = ((datetime.fromisoformat(row["timeUtc"]) - origin).total_seconds()
                 - int(row["sampleIndex"]) * period - correction)
    startSec = max(0, clipStart)
    endSec = min(lastAnnotation, clipStart + duration)
    return {"sourceClip": row["clip"], "startSec": startSec, "endSec": endSec,
            "sourceStartSec": clipStart, "sourceSeekSec": startSec - clipStart}


def prepareVideos(projectDir=None, outputDir=None):
    projectDir = Path(projectDir) if projectDir else Path(__file__).resolve().parents[3]
    outputDir = Path(outputDir) if outputDir else projectDir / "pipeline/03_annotate/review"
    outputDir.mkdir(parents=True, exist_ok=True)
    ffmpeg = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
    ffprobe = os.environ.get("FFPROBE", shutil.which("ffprobe") or "ffprobe")
    settings = json.loads((projectDir / "settings.json").read_text())
    with (projectDir / "pipeline/02_extract/original/gopro.csv").open(newline="", encoding="utf-8") as csvFile:
        readings = list(csv.DictReader(csvFile))
    with (projectDir / "pipeline/03_annotate/annotations.csv").open(newline="", encoding="utf-8") as csvFile:
        endTime = max(float(row["timeSec"]) for row in csv.DictReader(csvFile))

    manifestPath = outputDir / "videoClips.json"
    previous = json.loads(manifestPath.read_text()) if manifestPath.exists() else []
    previous = {clip["sourceClip"]: clip for clip in previous}
    firstRows = {}
    for row in readings:
        firstRows.setdefault(row["clip"], row)

    def durationOf(file):
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(file)], capture_output=True, text=True, check=True)
        return float(json.loads(result.stdout)["format"]["duration"])

    def checkDuration(file, expected):
        if abs(durationOf(file) - expected) > 0.15:
            raise ValueError(f"Unexpected proxy duration: {file}")

    clips = []
    for clipName, row in firstRows.items():
        sourcePath = projectDir / "pipeline/01_record/raw/gopro" / clipName
        timing = clipTiming(row, settings, durationOf(sourcePath), endTime)
        if timing["endSec"] <= timing["startSec"]:
            continue
        recipe = dict(timing, driveStartUtc=settings["driveStartUtc"],
            clockFastSec=settings["cameraClockFastSec"]["gopro"],
            samplePeriodSec=settings["samplePeriodSec"]["gopro"],
            sourceBytes=sourcePath.stat().st_size, sourceModifiedNs=sourcePath.stat().st_mtime_ns,
            encoding="h264-960w-15fps-crf23-v1")
        prior = previous.get(clipName, {})
        # use the already verified legacy clips only when their timing is unchanged.
        legacy = ("recipe" not in prior and prior.get("file") == sourcePath.stem + "_review.mp4"
                  and all(prior.get(key) == value for key, value in timing.items())
                  and recipe["clockFastSec"] == 0 and recipe["samplePeriodSec"] == 0.1)
        reusable = prior.get("recipe") == recipe or legacy
        token = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:16]
        fileName = prior["file"] if reusable else sourcePath.stem + "_review_" + token + ".mp4"
        if Path(fileName).name != fileName:
            raise ValueError("Review filenames must stay inside the review directory.")
        targetPath = outputDir / fileName
        duration = timing["endSec"] - timing["startSec"]
        if not targetPath.exists():
            # a new, owned temporary file each attempt. abandoned partials never block retry
            with tempfile.NamedTemporaryFile(prefix=sourcePath.stem + "_review-",
                    suffix=".partial.mp4", dir=outputDir, delete=False) as temporary:
                temporaryPath = Path(temporary.name)
            subprocess.run([
                ffmpeg, "-hide_banner", "-v", "warning", "-y", "-ss", str(timing["sourceSeekSec"]),
                "-i", str(sourcePath), "-t", str(duration), "-map", "0:v:0", "-an",
                "-vf", "scale=960:-2,fps=15", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-threads", "4", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(temporaryPath)], check=True)
            checkDuration(temporaryPath, duration)
            temporaryPath.replace(targetPath)
        else:
            checkDuration(targetPath, duration)
        clips.append(dict(file=targetPath.name, **timing, recipe=recipe))
        print(f"Ready: {targetPath.name}", flush=True)

    # old clips remain usable if preparation fails. publish the new timeline last
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json.tmp", dir=outputDir,
                                    encoding="utf-8", delete=False) as temporary:
        json.dump(clips, temporary, indent=2)
        temporaryPath = Path(temporary.name)
    temporaryPath.replace(manifestPath)
    return clips


if __name__ == "__main__":
    prepareVideos()
