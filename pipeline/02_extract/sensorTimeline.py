"""Timestamp-based inertial windows for the recordings used in this study.

GoPro: STMP is first-sample ARRIVAL time, not a certified acquisition time.
Within-block IMU times are interpolated to the next STMP using sample counts.
ROVE: use its actual MP4 track times; GPS arrival windows retain timing jitter.
Neither reconstruction claims independently measured hardware synchronization.
"""
from datetime import datetime, timedelta, timezone
import struct
import hashlib
import numpy as np
from mp4Telemetry import metadataTracks, streams, number, moov


def requireIncreasing(values, label):
    values = np.asarray(values, dtype=float)
    if len(values) < 2 or not np.isfinite(values).all() or np.any(np.diff(values) <= 0):
        raise ValueError(f'{label} must contain at least two finite increasing times')
    return values


def interpolateClock(times, anchors, values):
    """Piecewise linear clock mapping; permit only one anchor interval at edges."""
    anchors = requireIncreasing(anchors, 'Clock anchors')
    values = requireIncreasing(values, 'Clock values')
    times = np.asarray(times, dtype=float)
    indices = np.searchsorted(anchors, times, side='right') - 1
    indices = np.clip(indices, 0, len(anchors) - 2)
    if np.any(times < anchors[0] - (anchors[1] - anchors[0])) or np.any(times > anchors[-1] + (anchors[-1] - anchors[-2])):
        raise ValueError('Clock extrapolation exceeds one anchor interval')
    return values[indices] + (times - anchors[indices]) * np.diff(values)[indices] / np.diff(anchors)[indices]


class TimedSignal:
    """Integrate a piecewise-linear signal, with no bridging across large gaps."""
    def __init__(self, times, values, maximumGap):
        self.times = requireIncreasing(times, 'Sensor samples')
        self.values = np.asarray(values, dtype=float)
        if self.values.ndim != 2 or self.values.shape[0] != len(self.times):
            raise ValueError('Sensor dimensions do not match timestamps')
        if not np.isfinite(self.values).all() or maximumGap <= 0:
            raise ValueError('Invalid inertial values or gap threshold')
        self.delta = np.diff(self.times)
        self.good = self.delta <= maximumGap
        areas = (self.values[:-1] + self.values[1:]) * self.delta[:, None] / 2
        areas[~self.good] = 0
        self.integral = np.vstack([np.zeros(self.values.shape[1]), np.cumsum(areas, axis=0)])
        self.coverage = np.r_[0.0, np.cumsum(self.delta * self.good)]

    def primitive(self, queries):
        q = np.clip(np.asarray(queries, dtype=float), self.times[0], self.times[-1])
        i = np.clip(np.searchsorted(self.times, q, side='right') - 1, 0, len(self.times) - 2)
        dt = q - self.times[i]
        v = self.values[i] + (self.values[i + 1] - self.values[i]) * (dt / self.delta[i])[:, None]
        area = (self.values[i] + v) * dt[:, None] / 2
        area[~self.good[i]] = 0
        return self.integral[i] + area, self.coverage[i] + dt * self.good[i]

    def averages(self, starts, ends):
        starts, ends = np.asarray(starts, dtype=float), np.asarray(ends, dtype=float)
        if starts.shape != ends.shape or np.any(ends <= starts):
            raise ValueError('Invalid averaging windows')
        a, ac = self.primitive(starts)
        b, bc = self.primitive(ends)
        fraction = np.clip((bc - ac) / (ends - starts), 0, 1)
        means = (b - a) / (ends - starts)[:, None]
        means[fraction < 1 - 1e-8] = np.nan
        counts = np.searchsorted(self.times, ends, side='left') - np.searchsorted(self.times, starts, side='left')
        return means, fraction, counts


def blockSignal(blocks):
    """Interpolate sample times inside each block; flag last-block extrapolation."""
    stamps = requireIncreasing([b['stamp'] for b in blocks], 'STMP')
    counts = np.array([len(b['values']) for b in blocks])
    counters = np.array([b['counter'] for b in blocks])
    if np.any(np.diff(counters) != counts[1:]):
        raise ValueError('GPMF TSMP discontinuity; missing samples need separate handling')
    periods = np.diff(stamps) / counts[:-1]
    if np.any(periods <= 0):
        raise ValueError('Invalid block clock')
    lastPeriod = float(np.median(periods[-30:]))
    times = np.concatenate([stamps[i] + np.arange(counts[i]) * (periods[i] if i < len(periods) else lastPeriod)
                            for i in range(len(blocks))])
    values = np.vstack([b['values'] for b in blocks])
    return TimedSignal(times, values, 0.025), stamps[-1], {
        'samples': len(times), 'blocks': len(blocks), 'periodMinSec': float(periods.min()),
        'periodMaxSec': float(periods.max()), 'lastBlockPeriodSec': lastPeriod,
        'counterDiscontinuities': 0}


def goproTimeline(path, origin):
    metadataHash = hashlib.sha256(moov(path))
    gps = []
    anchors = []
    blocks = {'ACCL': [], 'GYRO': []}
    epochOffset = (datetime(2000, 1, 1, tzinfo=timezone.utc) - origin).total_seconds()
    tracks = [t for t in metadataTracks(path) if t['codec'] == 'gpmd']
    if len(tracks) != 1:
        raise ValueError('Expected exactly one GPMF track')
    with path.open('rb') as handle:
        for position, size in zip(tracks[0]['positions'], tracks[0]['sizes']):
            handle.seek(position)
            payloadBlock = handle.read(size)
            metadataHash.update(payloadBlock)
            for fields in streams(payloadBlock):
                if 'GPS9' in fields:
                    kind, width, count, payload = fields['GPS9']
                    if width != 32 or fields['TYPE'][3].rstrip(b'\0') != b'lllllllSS':
                        raise ValueError('Unsupported GPS9 layout')
                    scale = struct.unpack('>9i', fields['SCAL'][3])
                    if scale != (10000000, 10000000, 1000, 1000, 100, 1, 1000, 100, 1):
                        raise ValueError('Unexpected GPS9 scaling')
                    stamp = number(fields['STMP']) / 1e6
                    rows = [struct.unpack_from('>7i2H', payload, i * width) for i in range(count)]
                    times = [epochOffset + row[5] * 86400 + row[6] / 1000 for row in rows]
                    anchors.append((times[0], stamp))
                    gps.extend(times)
                for sensor in blocks:
                    if sensor not in fields:
                        continue
                    kind, width, count, payload = fields[sensor]
                    expectedUnit = b'm/s\xb2' if sensor == 'ACCL' else b'rad/s'
                    if kind != ord('s') or width != 6 or fields['SIUN'][3].rstrip(b'\0') != expectedUnit:
                        raise ValueError(f'Unexpected {sensor} type or units')
                    scale = number(fields['SCAL'])
                    if scale <= 0:
                        raise ValueError('Nonpositive sensor scale')
                    values = np.frombuffer(payload, dtype='>i2').reshape(count, 3).astype(float) / scale
                    blocks[sensor].append(dict(stamp=number(fields['STMP']) / 1e6,
                                               counter=number(fields['TSMP']), values=values))
    utc = requireIncreasing(gps, 'GPS UTC')
    anchorUtc, anchorClock = np.asarray(anchors).T
    # GPS9 has UTC per sample; relate it to the device clock using packet anchors.
    gpsClock = interpolateClock(utc, anchorUtc, anchorClock)
    signals = {}; metadata = {}
    for sensor, packets in blocks.items():
        signal, terminal, summary = blockSignal(packets)
        signals[sensor] = signal
        metadata[sensor] = dict(summary, terminalStart=terminal)
    metadata['gpsAnchorOffsetRangeSec'] = float(np.ptp(anchorUtc - anchorClock))
    metadata['lastGpsAnchorClock'] = float(anchorClock[-1])
    metadata['sourceMetadataSha256'] = metadataHash.hexdigest()
    metadata['sourceBytes'] = path.stat().st_size
    return dict(utc=utc, clock=gpsClock, signals=signals, metadata=metadata,
                method='gopro_STMP_arrival_with_interpolated_block_samples')


def roveTimeline(path, origin):
    metadataHash = hashlib.sha256(moov(path))
    candidates = [t for t in metadataTracks(path) if t['codec'] == 'ssmd']
    gpsTracks = [t for t in candidates if set(t['sizes']) == {32}]
    imuTracks = [t for t in candidates if set(t['sizes']) == {12}]
    if len(gpsTracks) != 1 or len(imuTracks) != 1:
        raise ValueError('Expected one ROVE GPS and one acceleration track')
    gpsTrack, imuTrack = gpsTracks[0], imuTracks[0]
    utc, values = [], []
    with path.open('rb') as handle:
        for position in gpsTrack['positions']:
            handle.seek(position)
            payload = handle.read(32)
            metadataHash.update(payload)
            year, month, day, hour, minute, second = payload[22:28]
            date = datetime(2000 + year, month, day, hour, minute, second, tzinfo=timezone.utc)
            utc.append((date - origin).total_seconds())
        for position in imuTrack['positions']:
            handle.seek(position)
            payload = handle.read(12)
            metadataHash.update(payload)
            values.append(struct.unpack('<3f', payload))
    utc = requireIncreasing(utc, 'ROVE UTC')
    clock = requireIncreasing(gpsTrack['times'], 'ROVE GPS track')
    signal = TimedSignal(imuTrack['times'], values, 0.25)
    return dict(utc=utc, clock=clock, signals={'ACCL': signal},
                metadata={'sourceMetadataSha256': metadataHash.hexdigest(), 'sourceBytes': path.stat().st_size,
                          'samples': len(values), 'gpsTrackUtcOffsetRangeSec': float(np.ptp(utc - clock)),
                          'imuMedianIntervalSec': float(np.median(np.diff(signal.times))),
                          'imuMaxIntervalSec': float(np.max(np.diff(signal.times)))},
                method='rove_MP4_track_time_GPS_arrival_windows')


def gpsWindowAverages(timeline, camera):
    """Forward windows [GPS_i, GPS_i+1); last GPS row has no defined endpoint."""
    utc, clock = timeline['utc'], timeline['clock']
    results = []
    averaged = {sensor: signal.averages(clock[:-1], clock[1:])
                for sensor, signal in timeline['signals'].items()}
    for i, start in enumerate(utc):
        terminal = i == len(utc) - 1
        flags = ['arrival_time_alignment', 'within_block_time_interpolation'] if camera == 'gopro' else ['arrival_time_alignment', 'GPS_time_quantized_to_seconds']
        row = dict(sampleIndex=i, timeStartSec=float(start), timeEndSec='' if terminal else float(utc[i + 1]),
                   timeMidSec='' if terminal else float((start + utc[i + 1]) / 2),
                   sensorClockStart=float(clock[i]), sensorClockEnd='' if terminal else float(clock[i + 1]),
                   accelX='', accelY='', accelZ='', accelUnits='m/s^2' if camera == 'gopro' else 'g (provisional)',
                   gyroX='', gyroY='', gyroZ='', gyroUnits='rad/s' if camera == 'gopro' else '',
                   accelCoverage=0.0, gyroCoverage='' if camera == 'rove' else 0.0,
                   accelSamples=0, gyroSamples='' if camera == 'rove' else 0, timingMethod=timeline['method'])
        if terminal:
            flags.append('no_next_GPS_window_boundary')
        else:
            for sensor, prefix in [('ACCL', 'accel'), ('GYRO', 'gyro')]:
                if sensor not in averaged:
                    continue
                means, coverage, counts = averaged[sensor]
                row[prefix + 'Coverage'] = float(coverage[i])
                row[prefix + 'Samples'] = int(counts[i])
                if np.isfinite(means[i]).all():
                    for axis, value in zip('XYZ', means[i]):
                        row[prefix + axis] = float(value)
                else:
                    flags.append(prefix + '_incomplete_coverage')
                if camera == 'gopro' and clock[i + 1] > timeline['metadata'][sensor]['terminalStart']:
                    flags.append(prefix + '_terminal_period_estimated')
            if camera == 'gopro' and clock[i + 1] > timeline['metadata']['lastGpsAnchorClock']:
                flags.append('terminal_GPS_clock_extrapolation')
        row['timingFlags'] = ';'.join(flags)
        results.append(row)
    return results
