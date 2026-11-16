"""Offline GIS lane builder. Coordinates are generated from published features and saved choices."""
"""Reconstruct complete paths from recorded lane choices, source lines and events.
The hand-drawn reference is not read here. All extrapolations/joins are modeled.
"""
import collections, json, math, pathlib
import numpy as np
from scipy.ndimage import median_filter, gaussian_filter1d
from scipy.signal import savgol_filter
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
roads = json.loads((O / 'sourceChains.json').read_text())['roads']
CHOICES = json.loads(args.choices.read_text())

def resample(p, step=0.5):
    """Place points at regular distances along a polyline."""
    p = np.array(p, float)
    p = p[np.r_[True, np.linalg.norm(np.diff(p, axis=0), axis=1) > 1e-07]]
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    q = np.linspace(0, s[-1], max(2, math.ceil(s[-1] / step) + 1))
    return (q, np.column_stack([np.interp(q, s, p[:, k]) for k in range(2)]))

def normalize(v):
    """Return unit-length directions; a zero vector stays zero."""
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)

def bezier(p, q, h0, h1, fraction=1 / 3):
    """Connect endpoints using two endpoint directions."""
    length = np.linalg.norm(q - p)
    t = np.linspace(0, 1, max(2, math.ceil(length / 0.5) + 1))[:, None]
    return (1 - t) ** 3 * p + 3 * (1 - t) ** 2 * t * (p + h0 * length * fraction) + 3 * (1 - t) * t ** 2 * (q - h1 * length * fraction) + t ** 3 * q

def turning_arc(p, q, h0, h1):
    """Build a bounded turn from intersecting tangent rays; fall back to a cubic connector."""
    delta = q - p
    length = np.linalg.norm(delta)
    theta = math.acos(float(np.clip(h0 @ h1, -1, 1)))
    if not 0.35 < theta < 2.6:
        return bezier(p, q, h0, h1)
    matrix = np.column_stack([h0, h1])
    if abs(np.linalg.det(matrix)) < 0.1:
        return bezier(p, q, h0, h1)
    u, v = np.linalg.solve(matrix, delta)
    if min(u, v) <= 0 or max(u, v) > 2 * length:
        return bezier(p, q, h0, h1)
    factor = 4 / 3 * math.tan(theta / 4) / math.tan(theta / 2)
    t = np.linspace(0, 1, max(2, math.ceil(length / 0.5) + 1))[:, None]
    return (1 - t) ** 3 * p + 3 * (1 - t) ** 2 * t * (p + h0 * u * factor) + 3 * (1 - t) * t ** 2 * (q - h1 * v * factor) + t ** 3 * q

def smoothstep(t):
    """Ease a lateral transition with zero slope at both ends."""
    return 6 * t ** 5 - 15 * t ** 4 + 10 * t ** 3
mark = {f['attributes']['OBJECTID']: f for f in json.loads((args.source_dir / 'markings.json').read_text())['features']}
p = np.array(mark[1469]['geometry']['paths'][0])
q = np.array(mark[1470]['geometry']['paths'][0])
_, p = resample(p)
_, q = resample(q)
TYPICAL = float(np.median(cKDTree(q).query(p)[0]))
TYPICAL = round(TYPICAL, 4)
starts = []
ends = []
curb_features = json.loads((args.source_dir / 'curbs.json').read_text())['features']
curb_by_id = {f['attributes']['OBJECTID']: f for f in curb_features}
for f in curb_features:
    att = f['attributes']
    if att.get('LifeCycle') != 'Existing' or att.get('FeatureType') not in [None, 'Curb']:
        continue
    for path in f.get('geometry', {}).get('paths', []):
        if len(path) < 2:
            continue
        _, p = resample(np.array(path)[:, :2], 3)
        starts.extend(p[:-1])
        ends.extend(p[1:])
CA = np.array(starts)
CD = np.array(ends) - CA
CL = np.linalg.norm(CD, axis=1)
CU = CD / CL[:, None]
CT = cKDTree(CA + CD / 2)

def side_widths(p, h, side, max_width=6.5, supported=None):
    """Measure nearly parallel curbs on the selected side; interpolate and smooth supported widths."""
    widths = []
    for xy, hh in zip(p, h):
        ix = np.array(CT.query_ball_point(xy, max_width + 3), int)
        normal = np.array([-hh[1], hh[0]]) * side
        if not len(ix):
            widths.append(float('nan'))
            continue
        den = CD[ix] @ hh
        good = (np.abs(CU[ix] @ hh) > 0.985) & (np.abs(den) > 1e-08)
        ix = ix[good]
        den = den[good]
        f = -((CA[ix] - xy) @ hh) / den
        valid = (f >= 0) & (f <= 1)
        offset = (CA[ix[valid]] + f[valid, None] * CD[ix[valid]] - xy) @ normal
        w = offset[(offset >= 2.6) & (offset <= max_width)]
        widths.append(float(min(w)) if len(w) else float('nan'))
    widths = np.array(widths)
    if supported is not None:
        widths[:supported[0]] = np.nan
        widths[supported[1] + 1:] = np.nan
    good = np.isfinite(widths)
    if good.sum() < 5:
        return (np.full(len(p), TYPICAL), good)
    widths = np.interp(np.arange(len(p)), np.where(good)[0], widths[good])
    widths = median_filter(widths, size=41, mode='nearest')
    return (gaussian_filter1d(widths, 20, mode='nearest'), good)
paths = {}
anchors = {}
tangents = {}
models = []
for cfg in roads:
    if cfg['boundary_xy'] is None:
        continue
    p = np.array(cfg['boundary_xy'])
    _, p = resample(p)
    p = savgol_filter(p, min(11, len(p) // 2 * 2 - 1), 2, axis=0, mode='interp')
    h = normalize(np.gradient(p, axis=0))
    before = p[0] + np.arange(-80, 0, 0.5)[:, None] * h[0]
    after = p[-1] + np.arange(0.5, 80.1, 0.5)[:, None] * h[-1]
    original_start = len(before)
    original_end = original_start + len(p) - 1
    p = np.r_[before, p, after]
    s, p = resample(p)
    h = normalize(np.gradient(p, axis=0))
    normal = np.column_stack([-h[:, 1], h[:, 0]])
    if cfg['width_mode'] == 'measured_side':
        width, observed = side_widths(p, h, cfg['side'], cfg.get('max_width_m', 6.5), (original_start, original_end))
    else:
        width = np.full(len(p), TYPICAL)
        observed = np.zeros(len(p), bool)
    base_offset = cfg['side'] * width / 2
    base = p + normal * base_offset[:, None]
    tree = cKDTree(base)
    if cfg.get('end_curb_ids'):
        parts = []
        for oid in cfg['end_curb_ids']:
            _, cp = resample(np.array(curb_by_id[oid]['geometry']['paths'][0])[:, :2])
            if not parts:
                forward = np.array(A[cfg['spans'][0][1]]['xy']) - A[cfg['spans'][0][0]]['xy']
                if (cp[-1] - cp[0]) @ forward < 0:
                    cp = cp[::-1]
            else:
                if np.linalg.norm(cp[-1] - parts[-1][-1]) < np.linalg.norm(cp[0] - parts[-1][-1]):
                    cp = cp[::-1]
                if np.linalg.norm(cp[0] - parts[-1][-1]) > 0.05:
                    parts.append(bezier(parts[-1][-1], cp[0], normalize(parts[-1][-1] - parts[-1][-5]), normalize(cp[4] - cp[0])))
            parts.append(cp)
        _, cp = resample(np.concatenate(parts))
        cp = savgol_filter(cp, 11, 2, axis=0, mode='interp')
        ch = normalize(np.gradient(cp, axis=0))
        cn = np.column_stack([-ch[:, 1], ch[:, 0]])
        cc = cp + cn * TYPICAL / 2
        cut = int(tree.query(cc[0])[1])
        cut = max(0, cut - 40)
        link = bezier(base[cut], cc[0], normalize(base[cut] - base[max(0, cut - 6)]), normalize(cc[6] - cc[0]))
        tail = cc[-1] + np.arange(0.5, 80.1, 0.5)[:, None] * normalize(cc[-1] - cc[-7])
        s, base = resample(np.r_[base[:cut], link, cc, tail])
        h = normalize(np.gradient(base, axis=0))
        normal = np.column_stack([-h[:, 1], h[:, 0]])
        width = np.full(len(base), TYPICAL)
        observed = np.zeros(len(base), bool)
        tree = cKDTree(base)
    model = dict(cfg, typical_width_m=TYPICAL, width_range_m=[float(min(width)), float(max(width))], width_observations=int(observed.sum()))
    model['boundary_xy'] = p.tolist()
    model['base_xy'] = base.tolist()
    model['widths'] = width.tolist()
    model['source_start_index'] = original_start
    model['source_end_index'] = original_end
    model['laps'] = []
    for lapidx, (lo, hi) in enumerate(cfg['spans']):
        raw = np.array([a['xy'] for a in A[lo:hi + 1]])
        indices = tree.query(raw)[1]
        positions = s[indices]
        positions = np.maximum.accumulate(positions)
        for j in range(1, len(positions)):
            if 'begin_stop' in A[lo + j - 1]['triggers'] and 'finish_stop' in A[lo + j]['triggers']:
                positions[j] = positions[j - 1]
        for projection_pass in range(6):
            shift = np.full(len(s), cfg.get('initial_lane_shift', 0.0), float)
            current = cfg.get('initial_lane_shift', 0.0)
            for aa, bb, target in cfg.get('changes', [[], [], []])[lapidx]:
                sa = positions[aa - lo]
                sb = positions[bb - lo]
                u = np.clip((s - sa) / max(sb - sa, 0.1), 0, 1)
                shift += smoothstep(u) * (target - current)
                current = target
            xy = base + normal * (shift * width)[:, None]
            updated = np.maximum.accumulate(s[cKDTree(xy).query(raw)[1]])
            for j in range(1, len(updated)):
                if 'begin_stop' in A[lo + j - 1]['triggers'] and 'finish_stop' in A[lo + j]['triggers']:
                    updated[j] = updated[j - 1]
            if projection_pass < 5:
                positions = updated
        hx = normalize(np.gradient(xy, axis=0))
        for j, ss in enumerate(positions):
            ai = lo + j
            at = np.array([np.interp(ss, s, xy[:, k]) for k in range(2)])
            hv = normalize(np.array([np.interp(ss, s, hx[:, k]) for k in range(2)]))
            anchors[ai] = at
            tangents[ai] = hv
        for i in range(lo, hi):
            sa, sb = positions[i - lo:i - lo + 2]
            query = np.linspace(sa, sb, max(2, math.ceil((sb - sa) / 0.5) + 1))
            coords = np.column_stack([np.interp(query, s, xy[:, k]) for k in range(2)])
            paths[i] = {'interval': i, 'lap': A[i]['lap'], 'xy': coords.tolist(), 'method': 'published linework + saved lane choice', 'road': cfg['id'], 'source_ids': cfg['source_ids'], 'curb_ids': cfg.get('end_curb_ids', []), 'width_mode': cfg['width_mode'], 'geometry_is_modeled': True}
        model['laps'].append({'lap': lapidx + 1, 'point_ids': list(range(lo, hi + 1)), 'projected_s': positions.tolist()})
    models.append(model)
for i, a in enumerate(A):
    if i not in anchors:
        anchors[i] = np.array(a['xy'])
for choice in CHOICES.get('maneuver_headings', []):
    heading = {'north': [0.0, 1.0], 'east': [1.0, 0.0], 'south': [0.0, -1.0], 'west': [-1.0, 0.0]}[choice['heading']]
    for i in choice['points']:
        tangents[i] = np.array(heading)
stationary_intervals = set()
stop_anchor = None
held_groups = []
group = []
for i, a in enumerate(A):
    if 'begin_stop' in a['triggers'] and stop_anchor is None:
        stop_anchor = anchors[i].copy()
        group = []
    if stop_anchor is not None:
        anchors[i] = stop_anchor.copy()
        group.append(i)
        if 'finish_stop' in a['triggers']:
            held_groups.append(group)
            stop_anchor = None
        elif i < len(A) - 1:
            stationary_intervals.add(i)
in_intersection = False
in_turn = False
for i in range(len(A) - 1):
    trig = A[i]['triggers'].split('|')
    if 'exit_intersection' in trig:
        in_intersection = False
    if 'enter_intersection' in trig:
        in_intersection = True
    if 'turn_finish_nonint' in trig:
        in_turn = False
    if 'turn_begin_nonint' in trig:
        in_turn = True
    if i in paths:
        if i in stationary_intervals:
            paths[i]['xy'] = [anchors[i].tolist(), anchors[i].tolist()]
        continue
    p = anchors[i]
    q = anchors[i + 1]
    delta = q - p
    length = np.linalg.norm(delta)
    held = i in stationary_intervals
    if held:
        q = p.copy()
        anchors[i + 1] = q
        coords = np.array([p, q])
        method = 'recorded stationary hold'
    elif length < 0.5:
        coords = np.array([p, q])
        method = 'short recorded annotation connector'
    elif not in_intersection and (not in_turn):
        coords = np.array([p, q])
        method = 'straight unmarked travel between saved user points'
    else:
        h0 = tangents.get(i)
        h1 = tangents.get(i + 1)
        if h0 is None:
            v = np.zeros(2)
            for j in range(i - 1, -1, -1):
                v = p - anchors[j]
                if np.linalg.norm(v) >= 2:
                    break
            h0 = normalize(v) if np.linalg.norm(v) > 0.1 else normalize(delta)
        if h1 is None:
            v = np.zeros(2)
            for j in range(i + 2, len(A)):
                v = anchors[j] - q
                if np.linalg.norm(v) >= 2:
                    break
            h1 = normalize(v) if np.linalg.norm(v) > 0.1 else normalize(delta)
        if h0 @ delta < 0:
            h0 = normalize(delta)
        if h1 @ delta < 0:
            h1 = normalize(delta)
        coords = turning_arc(p, q, h0, h1) if in_turn and (not in_intersection) else bezier(p, q, h0, h1)
        method = 'modeled tangent connector between saved/source-derived anchors'
    paths[i] = {'interval': i, 'lap': A[i]['lap'], 'xy': coords.tolist(), 'method': method, 'road': 'unmarked or intersection', 'source_ids': [], 'geometry_is_modeled': True}
joins = []
for i in range(1, len(A) - 1):
    p = np.array(paths[i - 1]['xy'])
    q = np.array(paths[i]['xy'])
    gap = q[0] - p[-1]
    if np.linalg.norm(gap) < 1e-08:
        continue
    joint = (p[-1] + q[0]) / 2
    sp = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
    sq = np.r_[0, np.cumsum(np.linalg.norm(np.diff(q, axis=0), axis=1))]
    if sp[-1] < 0.1 or sq[-1] < 0.1:
        raise RuntimeError(f'Nonmatching stop endpoints at {i}')
    wp = smoothstep(np.clip((sp - (sp[-1] - min(15, sp[-1]))) / min(15, sp[-1]), 0, 1))
    wq = 1 - smoothstep(np.clip(sq / min(15, sq[-1]), 0, 1))
    p = p + wp[:, None] * (joint - p[-1])
    q = q + wq[:, None] * (joint - q[0])
    paths[i - 1]['xy'] = p.tolist()
    paths[i]['xy'] = q.tolist()
    anchors[i] = joint
    joins.append({'point': i, 'unreconciled_gap_m': float(np.linalg.norm(gap)), 'taper_m': 15, 'joint': joint.tolist()})
result = {'version': 'source-guided-3', 'typical_width_source_ids': [1469, 1470], 'typical_width_m': TYPICAL, 'no_handdrawn_geometry_input': True, 'modeled_road_joins': joins, 'stationary_anchor_groups': held_groups, 'roads': models, 'paths': [paths[i] for i in range(len(A) - 1)], 'anchors': {str(i): p.tolist() for i, p in anchors.items()}}
(O / 'sourceGuidedGeometry.json').write_text(json.dumps(result, separators=(',', ':')))
print('built', len(paths), 'complete interval geometries; typical width', TYPICAL)
for model in models:
    print(model['id'], model['width_range_m'], model['width_observations'])
