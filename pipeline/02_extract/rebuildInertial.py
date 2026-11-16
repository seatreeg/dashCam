"""Generate reviewed inertial derivatives; never overwrite source camera CSVs.

Run: python pipeline/02_extract/rebuildInertial.py
Requires NumPy. Outputs go to Data/inertial/; original GPS fields remain in
Data Raw/Dashcam Extract/. Join by clip and sampleIndex within each camera.
"""
import argparse
import csv
from datetime import datetime
import hashlib
import json
import tempfile
from pathlib import Path
from sensorTimeline import goproTimeline, roveTimeline, gpsWindowAverages
from config import SENSOR_CALIBRATION

root = Path(__file__).resolve().parents[2]


def rebuild(camera, output):
    settings = json.loads((root / 'settings.json').read_text())
    origin = datetime.fromisoformat(settings['driveStartUtc'])
    source = root / 'Data Raw/Dashcam Extract' / f'{camera}.csv'
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with source.open(newline='', encoding='utf-8-sig') as handle:
        native = list(csv.DictReader(handle))
    byClip = {}
    for row in native:
        byClip.setdefault(row['clip'], []).append(row)
    records, summaries = [], {}
    for clip, observations in byClip.items():
        path = root / 'Data Raw' / camera / clip
        timeline = (goproTimeline if camera == 'gopro' else roveTimeline)(path, origin)
        means = gpsWindowAverages(timeline, camera)
        for observation in observations:
            i = int(observation['sampleIndex'])
            row = dict(means[i])
            elapsed = (datetime.fromisoformat(observation['timeUtc']) - origin).total_seconds()
            if abs(row['timeStartSec'] - elapsed) > 1e-6:
                raise ValueError(f'GPS index/UTC mismatch: {clip}, {i}')
            records.append(dict(camera=camera, clip=clip, timeUtc=observation['timeUtc'], **row))
        summaries[clip] = timeline['metadata']
        print(f'{camera}: {clip}: {len(observations)} retained rows', flush=True)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f'{camera}.csv'
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    return {'sourceCsvSha256': before, 'outputSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'rows': len(records), 'completeAccelerationWindows': sum(r['accelX'] != '' for r in records),
            'clips': summaries}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--camera', choices=['gopro', 'rove'])
    parser.add_argument('--output', type=Path, default=root / 'Data/inertial')
    args = parser.parse_args()
    temporaryRoot = root / 'audit/tmp'
    temporaryRoot.mkdir(parents=True, exist_ok=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / ('build-' + (args.camera or 'all') + '.json')
    manifest.write_text(json.dumps({'status': 'building'}) + '\n')
    cameras = [args.camera] if args.camera else ['gopro', 'rove']
    with tempfile.TemporaryDirectory(prefix='inertial-', dir=temporaryRoot) as directory:
        stage = Path(directory).resolve()
        assert stage.is_relative_to(root.resolve())
        try:
            report = {camera: rebuild(camera, stage) for camera in cameras}
            for camera in cameras:
                (stage / f'{camera}.csv').replace(args.output / f'{camera}.csv')
        except Exception:
            manifest.write_text(json.dumps({'status': 'failed'}) + '\n')
            raise
    report['status'] = 'complete'
    report['settingsSha256'] = hashlib.sha256((root / 'settings.json').read_bytes()).hexdigest()
    report['assumptions'] = ['Arrival/record timing is not independent hardware synchronization.',
        'GoPro samples are evenly interpolated within each timestamped block.',
        'ROVE averages use actual track intervals; their association with GPS has arrival jitter.',
        'Piecewise-linear integration excludes gaps >25 ms (GoPro) or >250 ms (ROVE).',
        'Incomplete and terminal undefined windows are blank, never zero-filled.',
        'Saved per-axis acceleration offsets/scales are applied once; ROVE calibration transfer to July remains unresolved.',
        'Gravity is retained; no camera-to-vehicle rotation or gyro bias correction is applied.']
    report['accelerationCalibration'] = SENSOR_CALIBRATION['acceleration']
    report['codeSha256'] = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                           for name in ['mp4Telemetry.py', 'sensorTimeline.py', 'rebuildInertial.py', 'config.py', 'sensorCalibration.json']}
    report['sourceMetadataHashRecipe'] = ('SHA-256 over moov payload, then all gpmd payloads for GoPro; '
        'moov payload, then GPS payloads, then acceleration payloads for ROVE. Video pixels are not read.')
    manifest.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
