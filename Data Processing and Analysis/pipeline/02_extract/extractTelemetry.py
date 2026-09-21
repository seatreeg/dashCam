"""

"""


import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

import decoders

projectDir = Path(__file__).resolve().parents[2]
settings = json.loads((projectDir / "settings.json").read_text())
startTime = datetime.fromisoformat(settings["driveStartUtc"])
endTime = datetime.fromisoformat(settings["extractionEndUtc"])
cameraNames = {
    "garmin": "Garmin X310", "gopro": "GOPRO Hero Black 13",
    "redtiger": "REDTIGER F7N Touch", "rove": "ROVE R2 4K Dual",
}



def extractCamera(camera, outputDir=None):
    clipPaths = sorted((projectDir / "pipeline/01_record/raw" / camera).glob("*"))
    clipPaths = [path for path in clipPaths if path.suffix.lower() == ".mp4"]
    if not clipPaths:
        raise FileNotFoundError(f"No original MP4 files for {camera}; finish raw-data copying first.")
    if len(clipPaths) != settings["expectedClips"][camera]:
        raise ValueError(f"Expected {settings['expectedClips'][camera]} clips for {camera}, found {len(clipPaths)}.")

    rows = []
    clipStarts = []
    for clipPath in clipPaths:
        print(f"{camera}: {clipPath.name}", flush=True)
        clipRows = decoders.cameraParsers[cameraNames[camera]](clipPath)
        if not clipRows:
            raise ValueError(f"No telemetry extracted: {clipPath}; do not use a remuxed first clip.")
        firstRow = next(row for row in clipRows if row["ts_utc_raw"])
        clipStarts.append({"clip": clipPath.name, "firstGpsUtc": firstRow["ts_utc_raw"],
                           "firstSampleIndex": firstRow["sample_idx"]})
        for row in clipRows:
            if not row["ts_utc_raw"]:
                raise ValueError(f"Missing timestamp in {clipPath.name}, sample {row['sample_idx']}")
            if not startTime <= datetime.fromisoformat(row["ts_utc_raw"]) <= endTime:
                continue
            newRow = {
                "clip": row["clip"], "sampleIndex": row["sample_idx"],
                "timeUtc": row["ts_utc_raw"], "latitude": row["lat_deg"],
                "longitude": row["lon_deg"],
                "speed": row["speed_kmh"] if camera == "gopro" else row["speed_mph_from_camera"],
                "speedUnits": "km/h" if camera == "gopro" else "mph",
                "fixStatus": row["fix_status"],
            }
            if camera != "garmin":
                newRow.update({"accelX": row["accel_x"], "accelY": row["accel_y"],
                               "accelZ": row["accel_z"], "accelUnits": row["accel_units"]})
            if camera == "gopro":
                newRow.update({"gyroX": row["gyro_x"], "gyroY": row["gyro_y"],
                               "gyroZ": row["gyro_z"], "gyroUnits": row["gyro_units"]})
            rows.append(newRow)

    rows.sort(key=lambda row: (row["timeUtc"], row["clip"], row["sampleIndex"]))
    if not rows:
        raise ValueError(f"No in-window observations for {camera}")
    outputDir = Path(outputDir) if outputDir is not None else projectDir / "pipeline/02_extract/original"
    outputDir.mkdir(parents=True, exist_ok=True)
    outputPath = outputDir / f"{camera}.csv"
    with outputPath.open("w", newline="", encoding="utf-8") as csvFile:
        writer = csv.DictWriter(csvFile, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (outputDir / f"{camera}Clips.json").write_text(json.dumps(clipStarts, indent=2), encoding="utf-8")
    print(f"{camera}: wrote {len(rows):,} rows", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", choices=list(cameraNames), help="Omit to extract all four cameras.")
    args = parser.parse_args()
    for camera in [args.camera] if args.camera else cameraNames:
        extractCamera(camera)



