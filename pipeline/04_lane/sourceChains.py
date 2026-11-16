"""Offline GIS lane builder. Coordinates are generated from published features and saved choices."""
import json, pathlib, math
import numpy as np
from scipy.spatial import cKDTree
import argparse
parser = argparse.ArgumentParser(description='Rebuild source-guided lane geometry from frozen inputs.')
parser.add_argument('--annotations', type=pathlib.Path, required=True)
parser.add_argument('--output-dir', type=pathlib.Path, required=True)
parser.add_argument('--source-dir', type=pathlib.Path, default=pathlib.Path('pipeline/04_lane/inputs/gis'))
parser.add_argument('--choices', type=pathlib.Path, default=pathlib.Path('pipeline/04_lane/inputs/laneChoices.json'))
args = parser.parse_args()
O = args.output_dir
O.mkdir(parents=True, exist_ok=True)
A = json.loads(args.annotations.read_text())
F = json.loads((args.source_dir / 'markings.json').read_text())['features']
CFG = json.loads(args.choices.read_text())

def dense(p, step=1):
    """Resample source linework without reading any finished reference path."""
    p = np.array(p, dtype=float)
    ds = np.linalg.norm(np.diff(p, axis=0), axis=1)
    keep = np.r_[True, ds > 1e-06]
    p = p[keep]
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    if s[-1] < 1e-06:
        return p
    q = np.linspace(0, s[-1], max(2, math.ceil(s[-1] / step) + 1))
    return np.column_stack([np.interp(q, s, p[:, k]) for k in range(2)])

def heading(p, end=False):
    """Estimate the direction at a source-line end."""
    p = np.array(p)
    d = p[-1] - p[max(0, len(p) - 5)] if end else p[min(4, len(p) - 1)] - p[0]
    return d / np.linalg.norm(d)

def connector(p, q, h0, h1):
    """Model a seam across a gap between published features."""
    length = np.linalg.norm(q - p)
    n = max(2, math.ceil(length) + 1)
    t = np.linspace(0, 1, n)[:, None]
    return (1 - t) ** 3 * p + 3 * (1 - t) ** 2 * t * (p + h0 * length / 3) + 3 * (1 - t) * t ** 2 * (q - h1 * length / 3) + t ** 3 * q
all_roads = []
for cfg in CFG['roads']:
    lo, hi = cfg['spans'][0]
    raw = np.array([a['xy'] for a in A[lo:hi + 1]])
    scaffold = dense(raw)
    guide = cKDTree(scaffold)
    if cfg['id'] == 's21_out':
        all_roads.append(dict(cfg, boundary_xy=None, source_ids=[], seams=[], method='saved user annotation guide; no usable longitudinal marking in city snapshot'))
        continue
    candidates = []
    for f in F:
        att = f['attributes']
        oid = att['OBJECTID']
        if oid in cfg.get('exclude_ids', []):
            continue
        if cfg.get('allowed_source_ids') and oid not in cfg['allowed_source_ids']:
            continue
        if att.get('LIFECYCLESTATUS') != 'Active' or att.get('RuleID') not in cfg['rules']:
            continue
        if cfg.get('segment') and att.get('Segment') and (cfg['segment'] not in str(att.get('Segment')).upper()):
            continue
        for part, path in enumerate(f.get('geometry', {}).get('paths', [])):
            if len(path) < 2:
                continue
            pp = dense(np.array(path)[:, :2])
            dist = guide.query(pp)[0]
            if np.min(dist) > 45:
                continue
            candidates.append({'id': oid, 'part': part, 'xy': pp})
    seed = next((x for x in candidates if x['id'] == cfg['seed_id']), None)
    p = seed['xy']
    forward = np.mean(raw[-min(3, len(raw)):], axis=0) - np.mean(raw[:min(3, len(raw))], axis=0)
    if (p[-1] - p[0]) @ forward < 0:
        p = p[::-1]
    chain = [{'id': seed['id'], 'part': seed['part'], 'xy': p, 'inferred': False}]
    used = {(seed['id'], seed['part'])}
    seams = []
    for direction in [-1, 1]:
        for _ in range(40):
            current = chain[0]['xy'][::-1] if direction < 0 else chain[-1]['xy']
            end = current[-1]
            h = heading(current, True)
            target = raw[0] if direction < 0 else raw[-1]
            if (target - end) @ h < -10:
                break
            options = []
            for c in candidates:
                if (c['id'], c['part']) in used:
                    continue
                for pp in [c['xy'], c['xy'][::-1]]:
                    delta = pp[0] - end
                    gap = np.linalg.norm(delta)
                    hh = heading(pp)
                    if gap > 75 or h @ hh < 0.65 or delta @ h < -0.5:
                        continue
                    average = h + hh
                    average /= np.linalg.norm(average)
                    lateral = abs(delta[0] * average[1] - delta[1] * average[0])
                    if lateral > cfg.get('max_seam_lateral_m', 4):
                        continue
                    if np.median(guide.query(pp)[0]) > 35:
                        continue
                    score = 20 * lateral ** 2 + gap * 0.06 + 30 * (1 - h @ hh)
                    options.append((score, c, pp, gap, lateral))
            if not options:
                break
            _, c, pp, gap, lateral = min(options, key=lambda x: (x[0], x[1]['id'], x[1]['part']))
            used.add((c['id'], c['part']))
            join = connector(end, pp[0], h, heading(pp))
            seams.append({'from_id': chain[0 if direction < 0 else -1]['id'], 'to_id': c['id'], 'gap_m': float(gap), 'lateral_m': float(lateral)})
            if direction > 0:
                chain.extend([{'id': None, 'part': None, 'xy': join, 'inferred': True}, {'id': c['id'], 'part': c['part'], 'xy': pp, 'inferred': False}])
            else:
                chain = [{'id': c['id'], 'part': c['part'], 'xy': pp[::-1], 'inferred': False}, {'id': None, 'part': None, 'xy': join[::-1], 'inferred': True}] + chain
    points = np.concatenate([c['xy'] for c in chain])
    points = dense(points, 0.5)
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    tree = cKDTree(points)
    i0 = tree.query(raw[0])[1]
    i1 = tree.query(raw[-1])[1]
    if i0 > i1:
        points = points[::-1]
        i0, i1 = (len(points) - 1 - i0, len(points) - 1 - i1)
    points = points[max(0, i0 - 20):min(len(points), i1 + 21)]
    result = dict(cfg, boundary_xy=points.tolist(), source_ids=[c['id'] for c in chain if c['id'] is not None], seams=seams, start_annotation_distance_m=float(np.linalg.norm(points[0] - raw[0])), end_annotation_distance_m=float(np.linalg.norm(points[-1] - raw[-1])))
    all_roads.append(result)
    print(cfg['id'], result['source_ids'], 'len', round(float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())), 'end gaps', round(result['start_annotation_distance_m'], 1), round(result['end_annotation_distance_m'], 1))
(O / 'sourceChains.json').write_text(json.dumps({'roads': all_roads, 'no_old_geometry_input': True}, indent=2))
