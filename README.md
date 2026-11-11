**meeting notes:**

I found out that the redtiger cam likely only stores accelerometer data if it has gps data, as it stores both in the freeGPS records. The repeat REDTIGER calibration now provides measured X and Y offsets and scale factors. The issue with separate recording devices is there is no failure feedback until the microSD card is returned after a recording is complete.

# dashCam

This repository compares GPS observations from four vehicle-mounted cameras against a modeled reference trajectory for a three-lap drive in Lincoln, Nebraska, on 13 July 2026. The cameras are Garmin Dash Cam X310, GoPro HERO13 Black, REDTIGER F7N Touch, and ROVE R2-4K Dual. A Raspberry Pi recorded vehicle telemetry, and manually reviewed video/map annotations and lane observations define the spatial reference.

The reference is a reviewed model, not a surveyed ground-truth track. The implemented workflow uses the saved v4 lane observations; the previously proposed Lincoln GIS migration is not implemented.

The [Data Descriptor](Data%20Descriptor.qmd) describes collection, methods, file relationships, column dictionaries, and scientific limitations. A [rendered HTML copy](Data%20Descriptor.html) is available for local reading. Some writing sections remain unfinished.

The remaining historical video-extraction text is preserved in [Video Extraction Details.txt](Video%20Extraction%20Details.txt). This is an archive of earlier wording, including superseded unit and processing claims; consult the descriptor and current code for the corrected interpretation.

## Repository layout

| Location | Contents |
|---|---|
| [Data Raw](Data%20Raw/) | Original videos in camera folders and the original Pi log in `pi/`. Videos are ignored by Git. |
| [Data Raw/Dashcam Extract](Data%20Raw/Dashcam%20Extract/) | Individual Garmin, GoPro, REDTIGER, and ROVE CSVs and clip manifests; inputs to the numerical analysis. |
| [Data](Data/) | Generated combined telemetry, trajectory, errors, interval checks, lane geometry, and analysis outputs. |
| [Annotation Data](Annotation%20Data/) | Original and working annotations, review-video metadata, and review saves. |
| [pipeline](pipeline/) | Active recording, telemetry preparation, annotation, lane-model, and analysis code. |
| [imagery](imagery/) | Map configuration and a locally retained image-tile cache. |
| [settings.json](settings.json) | Clock mapping, expected camera sampling intervals, spatial settings, and quality-selection settings. |
| [audit](audit/) | Numerical build provenance; temporary staged builds are ignored. |

The older repository-root parsing scripts and historical working folders are retained for reference. Start with `pipeline/` and the commands below for the current implementation.

## Run the numerical analysis

Run commands from the repository root. Install R with `jsonlite`, `sf`, and `digest`; rendering the analysis also requires Quarto, `knitr`, `rmarkdown`, and `ggplot2`.

```r
install.packages(c("jsonlite", "sf", "digest", "knitr", "rmarkdown", "ggplot2"))
```

```sh
Rscript runAnalysis.R
quarto render pipeline/05_analysis/autocorrelationAnalysis.qmd
```

[runAnalysis.R](runAnalysis.R) synchronizes the input CSVs, rebuilds lane geometry and reference motion, computes camera errors, and records verified input/output hashes in [audit/analysisBuild.json](audit/analysisBuild.json). The analysis render refuses stale or incomplete builds. The report is written beneath `Data/analysis/`.

This numerical build needs the camera CSVs, Pi log, annotations, saved lane observations, and elevation caches. It does not need the original MP4s or cached map tiles.

## Review annotations

With Python 3 installed, run:

```sh
python pipeline/03_annotate/server.py
```

Open <http://127.0.0.1:8799>. Video/map review additionally needs the prepared videos under `Annotation Data/review/` and the local `imagery/tiles/` cache. Review saves do not automatically replace the canonical scientific annotations; changes to those inputs require spatial review.

## Recording and camera inputs

[pipeline/01_record/car_monitor.py](pipeline/01_record/car_monitor.py) is the Pi/OBD/Arduino recorder. Its agreement with the current device copy remains to be checked. [pipeline/02_extract](pipeline/02_extract/) contains the optional original-video preparation tools, which require ExifTool and FFmpeg/FFprobe. They are not needed when using the supplied camera CSVs.

Acceleration columns are retained as source-table attributes. GoPro uses measured sensor offsets and scale factors, ROVE uses measured calibration values with unresolved transfer to July, REDTIGER uses measured X and Y offsets and scale factors, and the source-table inertial averages use historical sample-count grouping. Separate timestamp-based derivatives are now available. Both ROVE and REDTIGER speed are recorded in knots; REDTIGER's conversion is supported by checks against its video speed stamps. See the descriptor's Technical Validation section before using these fields. The acceleration columns are not used in the GPS-error analysis.

## Reprocess and review inertial data

With the original local videos available, run:

```sh
python -m pip install numpy pandas
python pipeline/02_extract/rebuildInertial.py
python pipeline/05_analysis/checkStationary.py
```

These commands write [Data/inertial](Data/inertial/README.md). The camera tables and timestamp-based derivatives apply the saved acceleration offsets and scale factors in [sensorCalibration.json](pipeline/02_extract/sensorCalibration.json); ROVE calibration transfer to July remains unresolved. The derivatives use elapsed-time weights and retain timing and coverage flags. Gravity remains, and no vehicle-frame rotation or gyro correction is applied. The stationary review uses the designated stops between laps.

## Videos and documentation

The existing [video folder link](https://drive.google.com/drive/folders/1zoSIooI2SAtxGwRLMBdtWv0mleSRVWXk?usp=sharing) is also recorded in [linkToVideos](linkToVideos); Note: This is not where the video files will be hosted long term.

```sh
quarto render "Data Descriptor.qmd"
```

Large videos, the map-tile cache, and local private drafting material are excluded from version control in this checkout.
