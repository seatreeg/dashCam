"""

telemetry parsing for each camera

Each camera records data in a differerent format, and this file helps extract the data from each one

We have four cameras:
    Garmin X310    1 Hz GPS in a private uuid atom. No IMU.
    REDTIGER F7N   1 Hz GPS in Novatek 'freeGPS' chunks + coarse 2-axis G-sensor.
    ROVE R2 4K     1 Hz GPS + ~16.6 Hz 3-axis accel, in SStarMeta ('ssmd') tracks.
    GoPro Hero 13  10 Hz GPS + ~200 Hz 3-axis accel AND gyro, in GPMF. Only gyro.


all four cameras timestampt their gps recievers, so ts_utc
"""


import re
import struct
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

from config import EXIFTOOL, FFMPEG, FFPROBE, KMH_TO_MPH, UTC


# SHARED ROW FIELDS

rowFields = [
    "camera", "clip", "sample_idx", "ts_utc_raw",
    "lat_deg", "lon_deg",
    "speed_mph_from_camera", "speed_mph_calculated", "speed_source",
    "fix_status", "fix_ok",
    "accel_x", "accel_y", "accel_z", "accel_units",
    "gyro_x", "gyro_y", "gyro_z", "gyro_units",
]


def blankRow():
    return {field: "" for field in rowFields}


# BINARY FORMATS

# Garmin X310 keeps 1 Hz GPS in a private uuid atom under this key.
garminGpsUuid = bytes.fromhex("9b630f8d637440ec8204bc5ff5091728")
mp4Epoch = datetime(1904, 1, 1, tzinfo=UTC)  # QuickTime epoch
noFixInt32 = -2147483648  # Garmin no-fix sentinel (0x80000000)

# Rove no-fix sentinel, as raw double bits.
roveNoFixBits = 0x41EFFFFFFFE00000

# REDTIGER stores three little-endian int32 acceleration values. Z was zero in
# the inspected footage. Scaling is uncertain, so retain the original integers.
redtigerAccelOffsets = (124, 128, 132)
redtigerGPerCount = 0.048  # Estimated g per count; no baseline/gravity correction. 

# REDTIGER speed is float32, little-endian, mph at byte 96. Earlier comparisons
# with Garmin supported this field (r = 0.970 over 46 matched samples).
redtigerSpeedOffset = 96


roveAccelRecordSize = 12  # Three little-endian float32 values
roveGpsRecordSize = 32

# Keep scientific notation and invalid numeric slots together when reading IMU text.
numberPattern = re.compile(
    r"(?<![\w.])[+-]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|nan|inf(?:inity)?)(?![\w.])",
    re.IGNORECASE,
)


# SHARED HELPERS

def runCommand(command, binary=False):
    """Run a command, returning stdout. Raises on non-zero exit."""
    result = subprocess.run(command, capture_output=True, check=True)
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")

def ddmmToDecimal(value: float) -> float:
    """NMEA DDDMM.MMMM -> decimal degrees."""
    sign = -1.0 if value < 0 else 1.0
    value = abs(value)
    degrees = int(value // 100)
    minutes = value - degrees * 100
    return sign * (degrees + minutes / 60.0)

def findAtom(clipFile, start, end, targetType):
    """
    Find the first targetType block within [start, end), including containers.
    Return (offset, size), or None if absent.
    """
    position = start
    while position < end - 8:
        clipFile.seek(position)
        header = clipFile.read(8)
        if len(header) < 8:
            return None
        atomSize = struct.unpack(">I", header[:4])[0]
        atomType = header[4:8]
        if atomSize == 1:  # 64-bit extended size
            atomSize = struct.unpack(">Q", clipFile.read(8))[0]
        if atomSize == 0:
            atomSize = end - position
        if atomType == targetType:
            return position, atomSize
        if atomType in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"):
            match = findAtom(clipFile, position + 8, position + atomSize, targetType)
            if match:
                return match
        if atomSize <= 0:
            return None
        position += atomSize
    return None

def findUuidAtom(clipFile, start, end, targetUuid):
    """
    Find the first UUID block whose 16-byte key matches targetUuid.
    Return (atomOffset, atomSize, bodyOffsetAfterUuid), or None if absent.
    """
    position = start
    while position < end - 8:
        clipFile.seek(position)
        header = clipFile.read(8)
        if len(header) < 8:
            return None
        atomSize = struct.unpack(">I", header[:4])[0]
        atomType = header[4:8]
        bodyStart = position + 8
        if atomSize == 1:
            atomSize = struct.unpack(">Q", clipFile.read(8))[0]
            bodyStart = position + 16
        if atomSize == 0:
            atomSize = end - position
        if atomType == b"uuid":
            clipFile.seek(bodyStart)
            if clipFile.read(16) == targetUuid:
                return position, atomSize, bodyStart + 16
        if atomType in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta"):
            match = findUuidAtom(clipFile, bodyStart, position + atomSize, targetUuid)
            if match:
                return match
        if atomSize <= 0:
            return None
        position += atomSize
    return None

# GARMIN X310

def parseGarmin(clipPath: Path):
    """1 Hz GPS from the private uuid atom. This camera has no accel or gyro."""
    with open(clipPath, "rb") as clipFile:
        clipFile.seek(0, 2)
        fileSize = clipFile.tell()
        found = findUuidAtom(clipFile, 0, fileSize, garminGpsUuid)
        if not found:
            return []
        atomOffset, atomSize, bodyOffset = found
        clipFile.seek(bodyOffset)
        body = clipFile.read(atomOffset + atomSize - bodyOffset)

    if len(body) < 17:
        return []
    # Four little-endian header values: unknown, record size, unknown, capacity.
    header0, recordSize, header2, header3 = struct.unpack("<4I", body[:16])
    if recordSize != 20:
        return []
    recordCount = body[16]
    payload = body[17:]
    recordCount = min(recordCount, len(payload) // recordSize)

    coordinateScale = 360.0 / (1 << 32)  # int32 -> degrees
    rows = []
    for sampleIndex in range(recordCount):
        record = payload[sampleIndex * recordSize:(sampleIndex + 1) * recordSize]
        timestamp, padding1, padding2, latInt, lonInt = struct.unpack(">IIIii", record)
        speedMph = (padding1 >> 16) & 0xFF  # Second byte of the big-endian word
        noFix = (latInt == noFixInt32) or (lonInt == noFixInt32)

        row = blankRow()
        row.update(
            camera="Garmin X310", clip=clipPath.name, sample_idx=sampleIndex,
            ts_utc_raw=(mp4Epoch + timedelta(seconds=timestamp)).isoformat(),
            lat_deg="" if noFix else round(latInt * coordinateScale, 7),
            lon_deg="" if noFix else round(lonInt * coordinateScale, 7),
            speed_mph_from_camera=speedMph,
            speed_source="from_camera",
            fix_status="nofix" if noFix else "fix",
            fix_ok=0 if noFix else 1,
        )
        rows.append(row)
    return rows


# REDTIGER F7N

def parseRedtigerChunk(chunk: bytes):
    """
    Decode one freeGPS chunk, or return None if the header does not match.
    Coordinates are little-endian float32 values at bytes 40/44 in ddmm.mmmm.
    """
    if len(chunk) < 100 or chunk[4:11] != b"freeGPS":
        return None
    latRaw, = struct.unpack_from("<f", chunk, 40)
    lonRaw, = struct.unpack_from("<f", chunk, 44)
    hour, minute, second, year, month, day = struct.unpack_from("<6I", chunk, 48)
    active = chr(chunk[72]) if chunk[72] else "?"
    latHemisphere = chr(chunk[73]) if chunk[73] else "?"
    lonHemisphere = chr(chunk[74]) if chunk[74] else "?"
    speedMph, = struct.unpack_from("<f", chunk, redtigerSpeedOffset)

    if len(chunk) >= redtigerAccelOffsets[2] + 4:
        accelX, accelY, accelZ = (
            struct.unpack_from("<i", chunk, offset)[0] for offset in redtigerAccelOffsets
        )
    else:
        accelX = accelY = accelZ = ""

    result = {
        "active": active, "hour": hour, "minute": minute, "second": second,
        "year": year, "month": month, "day": day, "speed_mph": speedMph,
        "gx": accelX, "gy": accelY, "gz": accelZ, "lat": "", "lon": "",
    }
    # The G-sensor is valid regardless of GPS fix, so it is filled either way.
    if active == "A":
        latitude = ddmmToDecimal(latRaw)
        longitude = ddmmToDecimal(lonRaw)
        result["lat"] = round(-latitude if latHemisphere == "S" else latitude, 7)
        result["lon"] = round(-longitude if lonHemisphere == "W" else longitude, 7)
    return result


def parseRedtiger(clipPath: Path):
    """1 Hz GPS + coarse 2-axis G-sensor from Novatek freeGPS chunks."""
    rows = []
    with open(clipPath, "rb") as clipFile:
        clipFile.seek(0, 2)
        fileSize = clipFile.tell()
        found = findAtom(clipFile, 0, fileSize, b"gps ")
        if not found:
            return []
        gpsOffset, gpsSize = found
        # An 8-byte header (version, count), followed by (offset, size) pairs.
        clipFile.seek(gpsOffset + 8)
        body = clipFile.read(gpsSize - 8)
        recordCount = struct.unpack(">I", body[4:8])[0]

        for sampleIndex in range(recordCount):
            chunkOffset, chunkLength = struct.unpack(
                ">II", body[8 + sampleIndex * 8:16 + sampleIndex * 8]
            )
            if chunkOffset + 100 > fileSize:
                continue
            clipFile.seek(chunkOffset)
            sample = parseRedtigerChunk(clipFile.read(256))
            if sample is None:
                continue

            try:
                year = sample["year"] + 2000 if sample["year"] < 100 else sample["year"]
                timestamp = datetime(
                    year, sample["month"], sample["day"],
                    sample["hour"], sample["minute"], sample["second"], tzinfo=UTC,
                ).isoformat()
            except (ValueError, KeyError):
                timestamp = ""

            row = blankRow()
            row.update(
                camera="REDTIGER F7N Touch", clip=clipPath.name, sample_idx=sampleIndex,
                ts_utc_raw=timestamp, lat_deg=sample["lat"], lon_deg=sample["lon"],
                speed_mph_from_camera=round(sample["speed_mph"], 2),
                speed_source="from_camera",
                # This camera reports NMEA-style A(ctive)/V(oid) rather than a
                # fix sentinel, so its original vocabulary is preserved.
                fix_status="active" if sample["active"] == "A" else "inactive",
                fix_ok=1 if sample["active"] == "A" else 0,
                accel_x="" if sample["gx"] == "" else sample["gx"] * redtigerGPerCount,  
                accel_y="" if sample["gy"] == "" else sample["gy"] * redtigerGPerCount,  
                accel_z="",  # Unused field; not a measured zero acceleration. 
                accel_units="g (estimated)",  
            )
            rows.append(row)
    return rows

# ROVE R2 4K

def roveStreams(clipPath: Path):
    """
    Return (gpsIndex, accelIndex). The Rove writes three 'ssmd'
    tracks per clip: ~1 frame (a JPEG thumbnail), ~60 frames (1 Hz GPS), and
    ~1000 frames (~16.6 Hz accel). They are told apart by frame count.
    """
    output = runCommand([
        FFPROBE, "-v", "error", "-show_streams", "-of", "compact",
        "-show_entries", "stream=index,codec_type,codec_tag_string,nb_frames",
        str(clipPath),
    ])
    candidates = []
    for line in output.splitlines():
        fields = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
        if fields.get("codec_type") == "data" and fields.get("codec_tag_string") == "ssmd":
            try:
                frameCount = int(fields.get("nb_frames", "0"))
            except ValueError:
                frameCount = 0
            candidates.append((int(fields["index"]), frameCount))
    if not candidates:
        return None, None
    gpsIndex = next(
        (streamIndex for streamIndex, frameCount in candidates if 30 <= frameCount <= 120),
        None,
    )
    accelIndex, accelFrames = max(candidates, key=lambda stream: stream[1])
    return gpsIndex, (accelIndex if accelFrames >= 300 else None)


def roveStreamBytes(clipPath: Path, streamIndex: int) -> bytes:
    """Demux one data stream straight to stdout -- no temp files."""
    return runCommand([
        FFMPEG, "-v", "error", "-i", str(clipPath),
        "-map", f"0:{streamIndex}", "-c", "copy", "-f", "data", "-",
    ], binary=True)


def parseRoveGpsRecord(record: bytes):
    """
    Decode one 32-byte SStarMeta GPS record.
    Date bytes 22/23/24 are year/month/day, correcting the historical decoder's
    day/year swap. That swap was hidden on 2026-04-26 because both were 26.
    """
    if len(record) < roveGpsRecordSize:
        return None
    latBits = struct.unpack_from("<Q", record, 0)[0]
    lonBits = struct.unpack_from("<Q", record, 8)[0]
    latRaw = struct.unpack_from("<d", record, 0)[0]
    lonRaw = struct.unpack_from("<d", record, 8)[0]
    noFix = (
        latBits == roveNoFixBits or lonBits == roveNoFixBits
        or not (1.0 <= abs(latRaw) < 9000.0)
        or not (1.0 <= abs(lonRaw) < 18000.0)
    )
    speedMph = record[20]
    yearSince2000, month, day = record[22], record[23], record[24]
    hour, minute, second = record[25], record[26], record[27]
    bearing = record[28]

    try:
        timestamp = datetime(
            2000 + yearSince2000, month, day, hour, minute, second, tzinfo=UTC,
        ).isoformat()
    except ValueError:
        timestamp = ""
    return {
        "ts": timestamp, "fix": 0 if noFix else 1,
        "lat": "" if noFix else round(ddmmToDecimal(latRaw), 7),
        "lon": "" if noFix else round(ddmmToDecimal(lonRaw), 7),
        "speed_mph": speedMph, "bearing": round(bearing * 360.0 / 256.0, 1),
    }


def parseRove(clipPath: Path):
    """1 Hz GPS, with the ~16.6 Hz accel averaged into each GPS second."""
    gpsIndex, accelIndex = roveStreams(clipPath)
    if gpsIndex is None:
        return []

    gpsData = roveStreamBytes(clipPath, gpsIndex)
    gpsSamples = [
        parseRoveGpsRecord(
            gpsData[sampleIndex * roveGpsRecordSize:(sampleIndex + 1) * roveGpsRecordSize]
        )
        for sampleIndex in range(len(gpsData) // roveGpsRecordSize)
    ]
    gpsSamples = [sample for sample in gpsSamples if sample]
    if not gpsSamples:
        return []

    accelSamples = []
    if accelIndex is not None:
        accelData = roveStreamBytes(clipPath, accelIndex)
        recordCount = len(accelData) // roveAccelRecordSize
        accelSamples = [
            struct.unpack_from("<3f", accelData, sampleIndex * roveAccelRecordSize)
            for sampleIndex in range(recordCount)
        ]

    # Both tracks span the same clip, so accel samples map onto GPS seconds by
    # even distribution.
    accelPerGps = (len(accelSamples) / len(gpsSamples)) if gpsSamples and accelSamples else 0.0

    rows = []
    for sampleIndex, sample in enumerate(gpsSamples):
        row = blankRow()
        row.update(
            camera="ROVE R2 4K Dual", clip=clipPath.name, sample_idx=sampleIndex,
            ts_utc_raw=sample["ts"], lat_deg=sample["lat"], lon_deg=sample["lon"],
            speed_mph_from_camera=sample["speed_mph"], speed_source="from_camera",
            fix_status="fix" if sample["fix"] else "nofix", fix_ok=sample["fix"],
        )
        if accelPerGps:
            accelWindow = accelSamples[
                int(sampleIndex * accelPerGps):int((sampleIndex + 1) * accelPerGps)
            ]
            if accelWindow:
                row.update(
                    accel_x=round(mean(accelSample[0] for accelSample in accelWindow), 5),
                    accel_y=round(mean(accelSample[1] for accelSample in accelWindow), 5),
                    accel_z=round(mean(accelSample[2] for accelSample in accelWindow), 5),
                    accel_units="g",
                )
        rows.append(row)
    return rows


def parseRoveAccelFull(clipPath: Path):
    """Read full-rate (~16.6 Hz) acceleration triplets without GPS-window averaging."""
    gpsIndex, accelIndex = roveStreams(clipPath)
    if accelIndex is None:
        return []
    accelData = roveStreamBytes(clipPath, accelIndex)
    recordCount = len(accelData) // roveAccelRecordSize
    return [
        struct.unpack_from("<3f", accelData, sampleIndex * roveAccelRecordSize)
        for sampleIndex in range(recordCount)
    ]



# GOPRO HERO 13

def parseTriplets(text: str, maxAbs: float = 1000.0):
    """Keep numeric slots aligned, including exponents and NaN/Inf. Outliers -> 0."""
    numbers = []
    for token in numberPattern.findall(text):
        try:
            value = float(token)
        except ValueError:
            continue
        if value != value or abs(value) > maxAbs:  # NaN or out of range
            value = 0.0
        numbers.append(value)
    return [
        (numbers[index], numbers[index + 1], numbers[index + 2])
        for index in range(0, len(numbers) - 2, 3)
    ]


def goproGps(clipPath: Path):
    """10 Hz GPS samples straight from GPMF (satellite UTC, not the camera RTC)."""
    output = runCommand([
        EXIFTOOL, "-ee", "-n", "-api", "LargeFileSupport=1",
        "-p", "$GPSDateTime|$GPSLatitude|$GPSLongitude|$GPSSpeed|$GPSMeasureMode|$GPSDOP",
        str(clipPath),
    ])
    samples = []
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 6 or not parts[0]:
            continue  # Warnings or entries without GPS
        try:
            timestamp = datetime.strptime(parts[0], "%Y:%m:%d %H:%M:%S.%f").replace(tzinfo=UTC)
            latitude, longitude, speedKmh = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        fixMode = parts[4].strip()  # "2" or "3" with -n
        samples.append({
            "ts": timestamp, "lat": latitude, "lon": longitude,
            # GPMF reports km/h; every other camera here reports mph.
            "speed_mph": speedKmh * KMH_TO_MPH,
            "speed_kmh": speedKmh,  # Original ExifTool value for the native CSV
            "mode": fixMode, "dop": parts[5].strip(),
        })
    return samples


def goproImu(clipPath: Path, tag: str):
    text = runCommand([
        EXIFTOOL, "-ee", "-api", "LargeFileSupport=1", "-b", f"-{tag}", str(clipPath),
    ])
    return parseTriplets(text)


def parseGopro(clipPath: Path):
    """Keep 10 Hz GPS; average ~200 Hz accel and gyro into each GPS sample window."""
    gpsSamples = goproGps(clipPath)
    if not gpsSamples:
        return []
    accelSamples = goproImu(clipPath, "Accelerometer")
    gyroSamples = goproImu(clipPath, "Gyroscope")

    accelPerGps = len(accelSamples) / len(gpsSamples) if accelSamples else 0.0
    gyroPerGps = len(gyroSamples) / len(gpsSamples) if gyroSamples else 0.0

    rows = []
    for sampleIndex, sample in enumerate(gpsSamples):
        row = blankRow()
        # GPSMeasureMode: 3 = 3D fix, 2 = 2D fix. A 2D fix has no reliable
        # altitude but lat/lon are still usable, so it counts as fix_ok.
        fixMode = sample["mode"]
        row.update(
            camera="GOPRO Hero Black 13", clip=clipPath.name, sample_idx=sampleIndex,
            ts_utc_raw=sample["ts"].isoformat(),
            lat_deg=round(sample["lat"], 7), lon_deg=round(sample["lon"], 7),
            speed_mph_from_camera=round(sample["speed_mph"], 2),
            speed_kmh=sample["speed_kmh"],
            speed_source="from_camera",
            fix_status={"3": "3D", "2": "2D"}.get(fixMode, "nofix"),
            fix_ok=1 if fixMode in ("2", "3") else 0,
        )
        if accelPerGps:
            window = accelSamples[
                int(sampleIndex * accelPerGps):int((sampleIndex + 1) * accelPerGps)
            ]
            if window:
                row.update(
                    accel_x=round(mean(imuSample[0] for imuSample in window), 4),
                    accel_y=round(mean(imuSample[1] for imuSample in window), 4),
                    accel_z=round(mean(imuSample[2] for imuSample in window), 4),
                    accel_units="m/s^2",
                )
        if gyroPerGps:
            window = gyroSamples[
                int(sampleIndex * gyroPerGps):int((sampleIndex + 1) * gyroPerGps)
            ]
            if window:
                row.update(
                    gyro_x=round(mean(imuSample[0] for imuSample in window), 5),
                    gyro_y=round(mean(imuSample[1] for imuSample in window), 5),
                    gyro_z=round(mean(imuSample[2] for imuSample in window), 5),
                    gyro_units="rad/s",
                )
        rows.append(row)
    return rows

# CHOOSE THE CAMERA DECODER

cameraParsers = {
    "Garmin X310": parseGarmin,
    "GOPRO Hero Black 13": parseGopro,
    "REDTIGER F7N Touch": parseRedtiger,
    "ROVE R2 4K Dual": parseRove,
}


def clipStartUtc(cameraName: str, clipPath: Path):
    """Decode a clip and return its first nonempty UTC timestamp, or None."""
    for row in cameraParsers[cameraName](clipPath):
        if row["ts_utc_raw"]:
            return datetime.fromisoformat(row["ts_utc_raw"])
    return None
