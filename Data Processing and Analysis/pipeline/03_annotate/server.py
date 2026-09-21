"""annotation viewer"""

import argparse
import csv
import io
import json
import math
import mimetypes
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

projectDir = Path(__file__).resolve().parents[2]
appDir = Path(__file__).resolve().parent
fields = ["pointId", "timeSec", "lap", "latitude", "longitude", "confidencePct",
          "notes", "triggers", "customTrigger", "locationHeld", "stopGroup"]
numericFields = {"pointId", "timeSec", "lap", "latitude", "longitude", "confidencePct", "stopGroup"}
triggerLabels = {
    "enter_intersection": "Entered intersection", "exit_intersection": "Exited intersection",
    "begin_stop": "Began stopping", "finish_stop": "Finished stop", "blinker_on": "Blinker activated",
    "lane_change_begin": "Lane change began", "lane_change_finish": "Lane change finished",
    "turn_begin_nonint": "Turn began (not intersection)", "turn_finish_nonint": "Turn finished (not intersection)",
}


def readCsv(filePath):
    with filePath.open(newline="", encoding="utf-8-sig") as csvFile:
        rows = list(csv.DictReader(csvFile))
    return rows


def validateAnnotations(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected a nonempty annotation list.")
    result = []
    for row in rows:
        if set(row) != set(fields):
            raise ValueError("This save must use the new 11-column annotation schema.")
        newRow = {field: row[field] for field in fields}
        for field in numericFields:
            if isinstance(row[field], bool):
                raise ValueError(f"{field} must be numeric, not a boolean.")
            newRow[field] = None if row[field] in ("", None, "NA") else float(row[field])
            if newRow[field] is not None and not math.isfinite(newRow[field]):
                raise ValueError(f"{field} must be finite.")
        heldText = str(row["locationHeld"]).lower()
        if heldText not in ("true", "false"):
            raise ValueError("locationHeld must be TRUE or FALSE.")
        newRow["locationHeld"] = heldText == "true"
        for field in ("notes", "triggers", "customTrigger"):
            if row[field] is not None and not isinstance(row[field], str):
                raise ValueError(f"{field} must be text.")
            newRow[field] = row[field] or ""
        codes = newRow["triggers"].split("|") if newRow["triggers"] else []
        if any(code not in triggerLabels for code in codes) or len(codes) != len(set(codes)):
            raise ValueError("triggers must contain unique known event codes separated by |. Put other labels in customTrigger.")
        for field in ("pointId", "lap", "stopGroup"):
            if newRow[field] is not None:
                if newRow[field] != int(newRow[field]):
                    raise ValueError(f"{field} must be an integer.")
                newRow[field] = int(newRow[field])
        if newRow["stopGroup"] is not None and newRow["stopGroup"] < 1:
            raise ValueError("stopGroup must be a positive integer or blank.")
        if newRow["pointId"] is None or newRow["pointId"] < 0:
            raise ValueError("Point IDs must be nonnegative integers.")
        if newRow["timeSec"] is None or not 0 <= newRow["timeSec"] <= 100000:
            raise ValueError("Time must be a finite, nonnegative number of seconds.")
        if newRow["latitude"] is None or not -90 <= newRow["latitude"] <= 90:
            raise ValueError("Invalid latitude.")
        if newRow["longitude"] is None or not -180 <= newRow["longitude"] <= 180:
            raise ValueError("Invalid longitude.")
        if newRow["confidencePct"] is None or not 0 <= newRow["confidencePct"] <= 100:
            raise ValueError("Confidence must be from 0 to 100.")
        if newRow["lap"] not in (1, 2, 3):
            raise ValueError("Lap must be 1, 2, or 3 for this drive.")
        result.append(newRow)
    result.sort(key=lambda row: row["timeSec"])
    if len({row["pointId"] for row in result}) != len(result):
        raise ValueError("Duplicate point IDs.")
    if len({row["timeSec"] for row in result}) != len(result):
        raise ValueError("Two annotations cannot have the same timestamp.")
    return result


def numericRows(filePath, textFields=()):
    return [{key: value if key in textFields else (float(value) if value not in ("", "NA") else None)
             for key, value in row.items()} for row in readCsv(filePath)]



def reviewClips():
    manifest = appDir / "review/videoClips.json"
    if not manifest.exists():
        return []
    settings = json.loads((projectDir / "settings.json").read_text())
    clips = json.loads(manifest.read_text())
    expected = {"driveStartUtc": settings["driveStartUtc"],
                "clockFastSec": settings["cameraClockFastSec"]["gopro"],
                "samplePeriodSec": settings["samplePeriodSec"]["gopro"]}
    # refuse stale timelines instead of showing a frame as though it were current
    return [clip for clip in clips if all(clip.get("recipe", {}).get(key) == value
            for key, value in expected.items()) and (appDir / "review" / clip["file"]).is_file()]



class Handler(BaseHTTPRequestHandler):
    def jsonResponse(self, data, status=200):
        payload = json.dumps(data, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/data":
            try:
                self.jsonResponse({
                    "annotations": validateAnnotations(readCsv(appDir / "annotations.csv")),
                    "gps": numericRows(projectDir / "pipeline/02_extract/combined/dashcams.csv", ("camera", "clip")),
                    "vehicle": numericRows(projectDir / "pipeline/02_extract/combined/vehicle.csv"),
                    "clips": reviewClips(),
                    "triggers": triggerLabels,
                })
            except (OSError, ValueError) as error:
                self.jsonResponse({"error": str(error)}, 400)
            return
        if route in ("/", "/index.html", "/app.js", "/style.css"):
            target = appDir / ("index.html" if route == "/" else route.lstrip("/"))
        elif route in ("/vendor/leaflet.js", "/vendor/leaflet.css"):
            target = appDir / route.lstrip("/")
        elif re.fullmatch(r"/tiles/\d+/\d+/\d+\.png", route):
            target = projectDir / "imagery" / route.lstrip("/")
        elif re.fullmatch(r"/video/[A-Za-z0-9_-]+\.mp4", route):
            target = appDir / "review" / route.rsplit("/", 1)[1]
        else:
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return
        self.serveFile(target)

    def serveFile(self, filePath):
        size = filePath.stat().st_size
        start, end = 0, size - 1
        partial = self.headers.get("Range")
        if partial:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", partial)
            if not match:
                self.send_error(416)
                return
            start = int(match[1])
            end = min(int(match[2]), size - 1) if match[2] else size - 1
            if start > end or start >= size:
                self.send_error(416)
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(filePath)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with filePath.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Browsers cancel range requests when seeking.




    def do_POST(self):
        # security measure
        if self.headers.get("X-Annotation-Request") != "local":
            self.jsonResponse({"error": "Missing local-request header."}, 403)
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.jsonResponse({"error": "Cross-origin requests are not allowed."}, 403)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 5_000_000:
            self.jsonResponse({"error": "Invalid request size."}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length))
            if self.path == "/load":
                rows = validateAnnotations(list(csv.DictReader(io.StringIO(body["csv"])) ))
                self.jsonResponse({"annotations": rows})
            elif self.path == "/save":
                rows = validateAnnotations(body["annotations"])
                saveDir = appDir / "saves"
                saveDir.mkdir(exist_ok=True)
                savePath = saveDir / ("annotations_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".csv")
                with savePath.open("x", newline="", encoding="utf-8") as csvFile:
                    writer = csv.DictWriter(csvFile, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                self.jsonResponse({"saved": str(savePath.relative_to(projectDir)), "rows": len(rows)})
            else:
                self.jsonResponse({"error": "Unknown endpoint."}, 404)
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            self.jsonResponse({"error": str(error)}, 400)

    def log_message(self, format, *args):
        if not args or str(args[1]) not in ("200", "206"):
            super().log_message(format, *args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    print(f"Open http://127.0.0.1:{args.port} . Ctrl+C stops the local viewer.", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
