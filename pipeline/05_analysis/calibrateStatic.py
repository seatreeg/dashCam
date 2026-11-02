"""Fit a candidate ROVE static calibration from new, known-orientation recordings.

Example: python pipeline/05_analysis/calibrateStatic.py poses.csv --clips PATH
Manifest columns: clip,startSec,endSec,expectedGX,expectedGY,expectedGZ,set
set is train or validation. Times are seconds in the MP4 acceleration track.
Record all six +/-axis-up orientations, plus independent validation repeats.
Never applies a fit to research data automatically. Requires NumPy.
"""
import argparse
import csv
import json
from pathlib import Path
import struct
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'02_extract'))
from mp4Telemetry import metadataTracks
from sensorTimeline import TimedSignal


def fitCalibration(measured, expected):
    measured, expected = np.asarray(measured,float), np.asarray(expected,float)
    if measured.shape != expected.shape or measured.ndim != 2 or measured.shape[1] != 3:
        raise ValueError('Expected matching n x 3 pose means and known g vectors')
    if not np.isfinite(measured).all() or not np.isfinite(expected).all():
        raise ValueError('Nonfinite pose values')
    if not np.allclose(np.linalg.norm(expected,axis=1),1,atol=1e-6):
        raise ValueError('Each known stationary gravity vector must have magnitude 1 g')
    faces = np.vstack([np.eye(3),-np.eye(3)])
    if any(not np.any(np.all(np.isclose(expected,face,atol=1e-6),axis=1)) for face in faces):
        raise ValueError('Training requires all six known +/-axis-up orientations')
    design = np.column_stack([measured,np.ones(len(measured))])
    if np.linalg.matrix_rank(design) != 4 or np.linalg.cond(design)>1e5:
        raise ValueError('Pose readings cannot identify a stable affine calibration')
    coefficients = np.linalg.lstsq(design,expected,rcond=None)[0]
    residual = design@coefficients-expected
    return {'matrixRawToG':coefficients[:3].tolist(),'offsetG':coefficients[3].tolist(),
            'trainingVectorRmseG':float(np.sqrt(np.mean(np.sum(residual**2,axis=1)))),
            'conditionNumber':float(np.linalg.cond(design)),
            'formula':'corrected_row_vector_g = raw_row_vector @ matrixRawToG + offsetG'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('manifest',type=Path)
    parser.add_argument('--clips',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('candidateCalibration.json'))
    args=parser.parse_args()
    with args.manifest.open(newline='',encoding='utf-8-sig') as f:poses=list(csv.DictReader(f))
    cache={};means=[];expected=[];training=[]
    for row in poses:
        if row['set'] not in ('train','validation'):raise ValueError('set must be train or validation')
        if row['clip'] not in cache:
            p=args.clips/row['clip']
            candidates=[t for t in metadataTracks(p) if t['codec']=='ssmd' and set(t['sizes'])=={12}]
            if len(candidates)!=1:raise ValueError('Expected one ROVE acceleration track')
            track=candidates[0];values=[]
            with p.open('rb') as f:
                for offset in track['positions']:
                    f.seek(offset);values.append(struct.unpack('<3f',f.read(12)))
            cache[row['clip']]=TimedSignal(track['times'],values,.25)
        start,end=float(row['startSec']),float(row['endSec'])
        if end-start<10:raise ValueError('Use at least 10 stationary seconds after settling')
        average,coverage,_=cache[row['clip']].averages([start],[end])
        if not np.isfinite(average).all():raise ValueError('Incomplete stationary interval')
        means.append(average[0]);expected.append([float(row['expectedG'+a]) for a in 'XYZ'])
        training.append(row['set']=='train')
    means,expected,training=np.asarray(means),np.asarray(expected),np.asarray(training)
    result=fitCalibration(means[training],expected[training])
    if (~training).any():
        errors=means[~training]@np.array(result['matrixRawToG'])+result['offsetG']-expected[~training]
        result['validationVectorRmseG']=float(np.sqrt(np.mean(np.sum(errors**2,axis=1))))
        result['validationPoses']=int((~training).sum())
    result['status']='candidate_only_not_applied'
    result['limitations']='Requires correctly labeled, stable physical orientations. Does not validate dynamic response, timing, temperature transfer or other cameras.'
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(args.output)


if __name__=='__main__':main()
