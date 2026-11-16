# Reprocessed inertial data

These generated files retain the identity of each source GPS row while replacing global sample-count grouping with time-weighted integration. The camera CSVs in [Data Raw/Dashcam Extract](../../Data%20Raw/Dashcam%20Extract/) and these derivatives use the same saved sensor correction, with different averaging windows. Join within a camera using `clip` and `sampleIndex`; a row's `timeUtc` remains the original GPS tag.

| File | Purpose |
|---|---|
| [gopro.csv](gopro.csv) | 39,509 GPS-row identities; 39,507 complete acceleration windows, plus gyro averages. |
| [rove.csv](rove.csv) | 3,977 GPS-row identities; 3,910 complete acceleration windows. |
| [build-all.json](build-all.json) | Input/output/code hashes, metadata timing summaries, and assumptions. Only `status: complete` is a successful build. |
| [stationaryReview.json](stationaryReview.json) | Full-rate sensor baseline, variation and trends during the four designated long stops. |

## Columns in the window tables

| Columns | Meaning |
|---|---|
| `camera`, `clip`, `sampleIndex` | Source identity. `sampleIndex` is the original within-clip telemetry index. |
| `timeUtc` | Original GPS timestamp; it is not replaced by a reconstructed sensor timestamp. |
| `timeStartSec`, `timeEndSec`, `timeMidSec` | GPS-window bounds and midpoint, relative to the drive origin in settings.json. Windows run from one GPS observation to the next. |
| `sensorClockStart`, `sensorClockEnd` | Integration bounds on the recorded sensor/track clock. These are seconds, with a different origin from drive time. |
| `accelX`, `accelY`, `accelZ` | Time-weighted mean after applying saved sensor offsets and scale factors; blank when coverage is incomplete. Component order is retained from the stream: GoPro Z, X, Y; ROVE X, Y, Z. No vehicle-frame rotation is applied. |
| `accelUnits` | GoPro `m/s^2`, explicitly identified by metadata; ROVE `g (measured calibration)`, with unresolved transfer to the July recording. |
| `gyroX`, `gyroY`, `gyroZ`, `gyroUnits` | GoPro angular-velocity averages in rad/s. Empty for ROVE. No stationary-bias correction is applied. |
| `accelCoverage`, `gyroCoverage` | Fraction of the window covered by supported interpolation segments. Incomplete windows have blank means rather than zero. ROVE gyro coverage is blank. |
| `accelSamples`, `gyroSamples` | Number of recorded/reconstructed sample timestamps inside the half-open interval. Boundary interpolation can use a sample just outside the interval. |
| `timingMethod`, `timingFlags` | The reconstruction method and semicolon-separated qualifications described below. |

## Time reconstruction and remaining assumptions

GoPro provides the arrival time of the first sample in a block. Sample times within a block are evenly interpolated to the next block's timestamp, using the actual block sample count. GPS records provide UTC values, which connect the device clock to the drive timeline. See [GoPro's explanation of STMP](https://github.com/gopro/gpmf-parser/issues/189). This is a reconstruction from arrival timestamps, not an independently measured acquisition time for every sample.

ROVE provides separate GPS and acceleration track timestamps. Each mean integrates the acceleration signal between consecutive GPS track times. This handles unequal recorded spacing, but does not remove GPS arrival-time jitter or establish unknown sensor latency. The UTC GPS fields have one-second resolution. Do not interpret the new means as proof of hardware synchronization.

The integration does not bridge gaps over 25 ms for GoPro or 250 ms for ROVE. These are explicit processing thresholds, not manufacturer limits. The coverage columns identify supported windows. Each clip's final GPS row lacks a next boundary and is left blank; windows are not extrapolated across clips.

For GoPro's final IMU block, the last 30 observed block periods supply an estimated within-block period. Related windows carry `accel_terminal_period_estimated` or `gyro_terminal_period_estimated`. GPS clock mapping beyond the last GPS block anchor carries `terminal_GPS_clock_extrapolation`. Other flags identify the remaining arrival-time and within-block interpolation assumptions.

## Reproduce

From the repository root, with the original videos available locally:

```sh
python -m pip install numpy pandas
python pipeline/02_extract/rebuildInertial.py
python pipeline/05_analysis/checkStationary.py
```

The stationary review uses annotation point IDs 0, 122, 250 and 373 and their following points, trimming 15 s from each boundary and requiring nearby connected OBD zero-speed readings. Sample standard deviations describe observed variation; correlated high-rate samples are not independent experimental replicates. The [saved acceleration corrections](../../pipeline/02_extract/sensorCalibration.json) are applied once; gravity is retained, with no vehicle-frame rotation or gyro correction.
