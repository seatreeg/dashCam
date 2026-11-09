"""Describe full-rate inertial stability with the saved acceleration corrections."""
from pathlib import Path
import sys,json,hashlib
import numpy as np
import pandas as pd
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'pipeline/02_extract'))
from sensorTimeline import goproTimeline,roveTimeline,interpolateClock
from config import SENSOR_CALIBRATION, SENSOR_CALIBRATION_PATH
from datetime import datetime
origin=datetime.fromisoformat(json.loads((root/'settings.json').read_text())['driveStartUtc'])
vehicle=pd.read_csv(root/'Data/vehicle.csv')
vt=vehicle.timeSec.to_numpy();vs=vehicle.speedMps.to_numpy();connected=vehicle.obdConnected.to_numpy()
annotations=pd.read_csv(root/'Annotation Data/annotations.csv').set_index('pointId')
windows=[(name,float(annotations.loc[i,'timeSec'])+15,float(annotations.loc[i+1,'timeSec'])-15)
         for name,i in [('initial_stop',0),('between_laps_1_2',122),('between_laps_2_3',250),('final_stop',373)]]
sourceHashes={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in
              [root/'settings.json',root/'Data/vehicle.csv',root/'Annotation Data/annotations.csv',root/'Data Raw/Dashcam Extract/redtiger.csv',SENSOR_CALIBRATION_PATH]}
metadataHashes={}
data={}
for camera in ['gopro','rove']:
    collected={}
    for path in sorted((root/'Data Raw'/camera).glob('*')):
        if path.suffix.lower()!='.mp4':continue
        timeline=(goproTimeline if camera=='gopro' else roveTimeline)(path,origin)
        metadataHashes[str(path.relative_to(root))]=timeline['metadata']['sourceMetadataSha256']
        for sensor,signal in timeline['signals'].items():
            keep=(signal.times>=timeline['clock'][0])&(signal.times<=timeline['clock'][-1])
            times=interpolateClock(signal.times[keep],timeline['clock'],timeline['utc'])
            collected.setdefault(sensor,[]).append(np.column_stack([times,signal.values[keep]]))
    data[camera]={sensor:np.vstack(parts) for sensor,parts in collected.items()}
red=pd.read_csv(root/'Data Raw/Dashcam Extract/redtiger.csv')
rt=(pd.to_datetime(red.timeUtc,format='mixed',utc=True)-pd.Timestamp(origin)).dt.total_seconds()
redtigerScale=SENSOR_CALIBRATION['acceleration']['redtiger']['gPerIntegerIncrement']
data['redtiger']={'ACCL_integer_values':np.column_stack([rt,red.accelX/redtigerScale,red.accelY/redtigerScale])}
report={'inputHashes':sourceHashes,'sourceMetadataHashes':metadataHashes,'codeSha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'selection':'Annotation stop intervals trimmed by 15 s at both ends; nearest OBD within 0.25 s must be connected and report exactly zero speed. Full-rate sensor samples with saved acceleration offsets/scales, not GPS-window averages.',
        'accelerationCalibration':SENSOR_CALIBRATION['acceleration'],
        'limitations':['A single mounting orientation cannot separate gravity, mounting tilt, zero offset, and gain.',
                       'Sample standard deviations describe variation in this recording, not independent-sample uncertainty.',
                       'Final-stop telemetry is limited by the end of the OBD log.'], 'stops':[]}
for name,start,end in windows:
    result={'name':name,'requestedStartSec':start,'requestedEndSec':end,'cameras':{}}
    for camera,sensors in data.items():
        result['cameras'][camera]={}
        for sensor,raw in sensors.items():
            t=raw[:,0];right=np.searchsorted(vt,t).clip(1,len(vt)-1);left=right-1
            index=np.where(abs(vt[left]-t)<abs(vt[right]-t),left,right)
            keep=(t>=start)&(t<=end)&(abs(vt[index]-t)<=.25)&(vs[index]==0)&(connected[index]==1)
            selected=raw[keep];v=selected[:,1:]
            if len(v)<2:continue
            entry={'nRawSamples':len(v),'firstSec':float(selected[0,0]),'lastSec':float(selected[-1,0]),
                   'axisMean':np.mean(v,axis=0).tolist(),'axisMedian':np.median(v,axis=0).tolist(),
                   'axisStd':np.std(v,axis=0,ddof=1).tolist(),
                   'axisSlopePerMinute':(np.linalg.lstsq(np.column_stack([np.ones(len(v)),selected[:,0]-selected[0,0]]),v,rcond=None)[0][1]*60).tolist()}
            if v.shape[1]==3:entry['medianMagnitude']=float(np.median(np.linalg.norm(v,axis=1)))
            result['cameras'][camera][sensor]=entry
    report['stops'].append(result)
(root/'Data/inertial/stationaryReview.json').write_text(json.dumps(report,indent=2)+'\n')
for r in report['stops']:
    print(r['name'])
    for camera,sensors in r['cameras'].items():
        for sensor,stats in sensors.items():
            print(camera,sensor,'n',stats['nRawSamples'],'median',np.round(stats['axisMedian'],5).tolist(),'std',np.round(stats['axisStd'],5).tolist(),'magnitude',round(stats.get('medianMagnitude',0),5))
