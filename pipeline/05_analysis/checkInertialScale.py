"""Reproduce the shared-gain REDTIGER comparison with both IMU preparations.

Run after rebuildInertial.py. Requires numpy, pandas, scipy, matplotlib.
Fit/filter functions retained from the 2026-09-20 exploratory investigation.
Neither reference IMU is independently calibrated; the fitted gain is provisional.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter1d, minimum_filter1d
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

root = Path(__file__).resolve().parents[2]
gravity = 9.80665
step = 0.2
grid = np.arange(0, 3950, step)

def resample(times, values, targets=grid, maxGap=2.1):
    times, values = np.asarray(times), np.asarray(values)
    valid = np.isfinite(times) & np.isfinite(values)
    times, values = times[valid], values[valid]
    result = np.interp(targets, times, values, left=np.nan, right=np.nan)
    right = np.searchsorted(times, targets).clip(1, len(times) - 1)
    gap = times[right] - times[right - 1]
    exact = np.isclose(targets, times[right], atol=1e-6, rtol=0) | np.isclose(targets, times[right - 1], atol=1e-6, rtol=0)
    result[(gap > maxGap) & ~exact] = np.nan
    return result

def smooth(values, sigmaSec):
    valid = np.isfinite(values)
    result = gaussian_filter1d(np.nan_to_num(values), sigmaSec / step)
    # Do not bridge missing telemetry or use filter-edge values for inference.
    window = 2 * int(np.ceil(4 * sigmaSec / step)) + 1
    keep = minimum_filter1d(valid.astype(int), size=window, mode="constant", cval=0).astype(bool)
    result[~keep] = np.nan
    return result

def fitScale(counts, reference, mask=None):
    valid = np.isfinite(counts) & np.isfinite(reference)
    if mask is not None:
        valid &= mask
    x, y = counts[valid], reference[valid]
    if len(x) < 30 or np.std(x) < 1e-8 or np.std(y) < 1e-8:
        return None
    slope = np.sum((x - x.mean()) * (y - y.mean())) / np.sum((x - x.mean()) ** 2)
    intercept = y.mean() - slope * x.mean()
    predicted = intercept + slope * x
    return {"nGrid": len(x), "r": float(np.corrcoef(x, y)[0, 1]),
            "scaleMps2PerCount": float(slope), "scaleGPerCount": float(slope / gravity),
            "interceptMps2": float(intercept), "rmseMps2": float(np.sqrt(np.mean((y - predicted) ** 2)))}

def lagFit(counts, reference, mask=None):
    results = []
    for lag in np.arange(-10, 10.01, step):
        shifted = resample(grid, reference, grid + lag, maxGap=step * 1.1)
        fitted = fitScale(counts, shifted, mask)
        if fitted:
            results.append(dict(fitted, lagSec=float(round(lag, 3))))
    return max(results, key=lambda item: abs(item["r"]))

def scoreFixed(counts, reference, mask, slope, intercept):
    valid = mask & np.isfinite(counts) & np.isfinite(reference)
    residual = reference[valid] - (intercept + slope * counts[valid])
    return {"nSeconds": int(valid.sum()), "rmseMps2": float(np.sqrt(np.mean(residual ** 2))),
            "maeMps2": float(np.mean(np.abs(residual))),
            "rSquared": float(1 - np.sum(residual ** 2) / np.sum((reference[valid] - reference[valid].mean()) ** 2))}

def vectorFit(counts, reference, mask):
    valid = mask & np.isfinite(counts).all(axis=1) & np.isfinite(reference).all(axis=1)
    x = np.column_stack([np.ones(valid.sum()), counts[valid]])
    y = reference[valid]
    coefficients = np.linalg.lstsq(x, y, rcond=None)[0]
    residual = y - x @ coefficients
    rSquared = 1 - np.sum(residual ** 2) / np.sum((y - y.mean(axis=0)) ** 2)
    return {"nSeconds": int(valid.sum()), "rSquared": float(rSquared),
            "coefficients": coefficients.tolist(),
            "axisScaleMps2PerCount": np.linalg.norm(coefficients[1:], axis=1).tolist()}

def commonScaleFit(counts, reference, mask):
    """One positive gain and an orthogonal two-axis-to-three-axis orientation map."""
    valid = mask & np.isfinite(counts).all(axis=1) & np.isfinite(reference).all(axis=1)
    x, y = counts[valid], reference[valid]
    centeredX, centeredY = x - x.mean(axis=0), y - y.mean(axis=0)
    left, singular, right = np.linalg.svd(centeredX.T @ centeredY, full_matrices=False)
    rotation = left @ right
    scale = singular.sum() / np.sum(centeredX ** 2)
    offset = y.mean(axis=0) - scale * x.mean(axis=0) @ rotation
    return {"scaleMps2PerCount": float(scale), "scaleGPerCount": float(scale / gravity),
            "rotation": rotation.tolist(), "offsetMps2": offset.tolist(), "nSeconds": int(valid.sum())}

def commonScaleScore(counts, reference, mask, fit, scale=None, training=None):
    valid = mask & np.isfinite(counts).all(axis=1) & np.isfinite(reference).all(axis=1)
    rotation = np.array(fit["rotation"])
    offset = np.array(fit["offsetMps2"])
    if scale is None:
        scale = fit["scaleMps2PerCount"]
    else:
        usable = training & np.isfinite(counts).all(axis=1) & np.isfinite(reference).all(axis=1)
        offset = reference[usable].mean(axis=0) - scale * counts[usable].mean(axis=0) @ rotation
    residual = reference[valid] - (offset + scale * counts[valid] @ rotation)
    return {"nSeconds": int(valid.sum()), "vectorRmseMps2": float(np.sqrt(np.mean(np.sum(residual ** 2, axis=1)))),
            "rSquared": float(1 - np.sum(residual ** 2) / np.sum((reference[valid] - reference[valid].mean(axis=0)) ** 2))}

def main():
    out = root / "Data/inertial"
    manifest = json.loads((out / "build-all.json").read_text())
    if manifest.get("status") != "complete":
        raise ValueError("Run rebuildInertial.py successfully before scale review")
    if hashlib.sha256((root / 'settings.json').read_bytes()).hexdigest() != manifest['settingsSha256']:
        raise ValueError('Settings changed; rebuild the inertial derivatives')
    for name, expected in manifest['codeSha256'].items():
        if hashlib.sha256((root / 'pipeline/02_extract' / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Inertial code changed; rebuild the derivatives: ' + name)
    settings = json.loads((root / "settings.json").read_text())
    origin = pd.Timestamp(settings["driveStartUtc"])
    native = {}
    hashes = {}
    for camera in ("gopro", "rove", "redtiger"):
        path = root / "Data Raw/Dashcam Extract" / (camera + ".csv")
        hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        if camera in manifest and hashes[str(path.relative_to(root))] != manifest[camera]['sourceCsvSha256']:
            raise ValueError('Camera source changed; rebuild the inertial derivatives: ' + camera)
        data = pd.read_csv(path)
        data["t"] = (pd.to_datetime(data.timeUtc, format="mixed", utc=True) - origin).dt.total_seconds()
        native[camera] = data
    updated = {}
    for camera in ("gopro", "rove"):
        path = out / (camera + ".csv")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != manifest[camera]["outputSha256"]:
            raise ValueError("Stale inertial derivative: " + camera)
        hashes[str(path.relative_to(root))] = digest
        updated[camera] = pd.read_csv(path)
    for rel in ("Data/vehicle.csv", "Annotation Data/annotations.csv", "settings.json"):
        hashes[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    annotations = pd.read_csv(root / "Annotation Data/annotations.csv")
    lap = annotations.lap.to_numpy()[np.searchsorted(annotations.timeSec, grid, side="right").clip(1, len(annotations)) - 1]
    vehicle = pd.read_csv(root / "Data/vehicle.csv")
    vehicle.loc[vehicle.obdConnected != 1, "speedMps"] = np.nan
    obd = resample(vehicle.timeSec, vehicle.speedMps)
    moving = (obd > 3) & (grid > 150) & (grid < 3740) & np.isclose(grid % 1, 0, atol=1e-6)
    red = native["redtiger"]
    # Undo the documented historical CSV conversion; this is not a new fitted scale.
    raw = red[["accelX", "accelY"]].to_numpy() / 0.048
    if not np.allclose(raw, np.round(raw), atol=1e-10):
        raise ValueError("The expected REDTIGER integer-count provenance changed")
    raw = np.round(raw)
    countsGrid = np.column_stack([resample(red.t, raw[:, i]) for i in range(2)])
    report = {"inputHashes": hashes, "comparisons": [], "method": {
        "fit": "Shared positive gain, orthogonal 2D-to-3D orientation and constant offset; lag selected on lap 1 with the original unconstrained vector-fit criterion.",
        "smoothingSigmaSeconds": [1,2,4], "lagSearchSeconds": [-10,10], "lagStepSeconds": step,
        "selection": "Connected OBD speed >3 m/s, 150<t<3740, one fitted value per integer second.",
        "evaluation": "Apply lap-1 model unchanged to laps 2/3; report independent per-lap refits separately.",
        "timestampedTime": "Inertial GPS-window midpoint; terminal extrapolation/undefined windows excluded.",
        "limits": "Exploratory agreement with uncalibrated references. Arrival-based timing, smoothing, gravity, road grade, quantization and signal delay remain limitations. The earlier adverse lateral check remains documented in Data Descriptor."}}
    for mode in ("historical", "timestamped"):
        for camera in ("gopro", "rove"):
            data = native[camera] if mode == "historical" else updated[camera].copy()
            times = data.t if mode == "historical" else data.timeMidSec
            if mode == "timestamped":
                excluded = data.timingFlags.str.contains("terminal_|incomplete_|no_next_")
                data.loc[excluded, ["accelX", "accelY", "accelZ"]] = np.nan
            factor = gravity if camera == "rove" else 1
            for sigma in (1,2,4):
                counts = np.column_stack([smooth(countsGrid[:,i], sigma) for i in range(2)])
                vectors = np.column_stack([smooth(resample(times,data[axis]*factor),sigma) for axis in ("accelX","accelY","accelZ")])
                best = None
                for lag in np.arange(-10,10.01,step):
                    reference = np.column_stack([resample(grid,vectors[:,i],grid+lag,maxGap=step*1.1) for i in range(3)])
                    fit = vectorFit(counts,reference,moving & (lap==1))
                    if best is None or fit["rSquared"] > best["rSquared"]:
                        best = dict(fit,lagSec=float(round(lag,3)))
                reference = np.column_stack([resample(grid,vectors[:,i],grid+best["lagSec"],maxGap=step*1.1) for i in range(3)])
                training = commonScaleFit(counts,reference,moving & (lap==1))
                laps = [dict(commonScaleFit(counts,reference,moving & (lap==n)),lap=n,
                             lap1Model=commonScaleScore(counts,reference,moving & (lap==n),training)) for n in (1,2,3)]
                record = dict(mode=mode,reference=camera,sigmaSec=sigma,lagSec=best["lagSec"],training=training,lapRefits=laps)
                report["comparisons"].append(record)
                if sigma==2:
                    print(mode,camera,"g/count",[round(x["scaleGPerCount"],6) for x in laps],"lag",best["lagSec"],flush=True)
    for rel,digest in hashes.items():
        if hashlib.sha256((root / rel).read_bytes()).hexdigest()!=digest:
            raise ValueError("Input changed during scale review: " + rel)
    report["codeSha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (out / "scaleReview.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    fig,axes=plt.subplots(1,2,figsize=(11,4.5),layout="constrained")
    for axis,camera in zip(axes,("gopro","rove")):
        for mode,marker in (("historical","o"),("timestamped","s")):
            r=next(r for r in report["comparisons"] if r["mode"]==mode and r["reference"]==camera and r["sigmaSec"]==2)
            axis.plot([1,2,3],[x["scaleGPerCount"] for x in r["lapRefits"]],marker=marker,label=mode)
        axis.axhline(.048,color="0.4",linestyle="--",label="retained 0.048 estimate")
        axis.set(xlabel="Lap",ylabel="Estimated g per REDTIGER count",title=camera.upper()+" comparison",xticks=[1,2,3],ylim=(.044,.052))
        axis.grid(alpha=.2);axis.legend(fontsize=8)
    fig.suptitle("Timing-aware comparison supports an approximate scale; it is not a hardware calibration",fontsize=11)
    fig.savefig(out / "scaleReview.png",dpi=170)
    plt.close(fig)


if __name__ == "__main__":
    main()
