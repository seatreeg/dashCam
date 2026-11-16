// ONE TIMELINE, ONE VIDEO, AND THE ORIGINAL HAND-PLACED POINTS.
const byId = id => document.getElementById(id);
const video = byId("video");
const map = L.map("map").setView([40.81115, -96.6894], 19);
L.tileLayer("/tiles/{z}/{x}/{y}.png", {
  minZoom: 14, maxZoom: 22, maxNativeZoom: 22,
  attribution: "Imagery © Esri and contributors; cached research imagery"
}).addTo(map);
const markers = L.layerGroup().addTo(map);
const currentMarker = L.circleMarker([40.81115, -96.6894], {radius: 8, color: "#ed4a32", weight: 3}).addTo(map);
let data, annotations = [], selectedIndex = 0, activeClip = null, chosenPosition = null;
let holding = false, holdGroup = null, dirty = false;
let pendingTime = null;
let editRevision = 0, saving = false;

function showStatus(text) { byId("status").textContent = text; }
function driveTime() { return pendingTime ?? (activeClip ? activeClip.startSec + video.currentTime : Number(byId("time").value)); }
function format(value, digits = 1) { return value == null ? "no data" : value.toFixed(digits); }

function nearest(rows, timeSec, tolerance) {
  let low = 0, high = rows.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (rows[mid].timeSec < timeSec) low = mid + 1; else high = mid;
  }
  const candidates = [rows[low - 1], rows[low]].filter(Boolean);
  candidates.sort((a, b) => Math.abs(a.timeSec - timeSec) - Math.abs(b.timeSec - timeSec));
  return candidates[0] && Math.abs(candidates[0].timeSec - timeSec) <= tolerance ? candidates[0] : null;
}

function currentGpsMean() {
  if (!data?.cameraRows) return null;
  const timeSec = driveTime();
  // One nearby fix per camera: GoPro's higher sample rate must not give it extra weight.
  const points = Object.entries(data.cameraRows).map(([camera, rows]) =>
    nearest(rows, timeSec, camera === "gopro" ? 0.06 : 0.55)
  ).filter(point => point && point.fix === 1
    && Number.isFinite(point.latitude) && Math.abs(point.latitude) <= 90
    && Number.isFinite(point.longitude) && Math.abs(point.longitude) <= 180
    && (point.latitude !== 0 || point.longitude !== 0));
  if (!points.length) return null;
  return ["latitude", "longitude"].map(key =>
    points.reduce((sum, point) => sum + point[key], 0) / points.length);
}

function centerOnGps() {
  const center = currentGpsMean();
  // Move only the view, never the hand-placed annotation or held location.
  if (center) map.setView(center, map.getZoom(), {animate: false});
}

function followGpsOnPause() {
  if (video.paused && byId("followGps").checked) centerOnGps();
}

function updateReadouts() {
  const timeSec = driveTime();
  if (document.activeElement !== byId("time")) byId("time").value = timeSec.toFixed(2);
  const row = nearest(data.vehicle, timeSec, 0.3);
  byId("telemetry").textContent = row
    ? `OBD: ${format(row.speedMps)} m/s · ${format(row.rpm, 0)} rpm · throttle ${format(row.throttlePct)}% · OBD connected ${row.obdConnected === 1 ? "yes" : "no"}`
    : "No vehicle data at this time.";
  let signal = "No wire data";
  if (row && row.arduinoConnected === 1 && row.yellowWire != null && row.greenWire != null) {
    // Inspect the surrounding two seconds for flashing. This is a display aid,
    // not a verified brake/steering measurement or an analysis input.
    const nearby = data.vehicle.filter(item => Math.abs(item.timeSec - timeSec) <= 1 && item.arduinoConnected === 1);
    const edges = key => nearby.slice(1).filter((item, index) => item[key] != null && nearby[index][key] != null && item[key] !== nearby[index][key]).length;
    const left = edges("yellowWire") >= 3, right = edges("greenWire") >= 3;
    signal = left && right ? "Both wires flashing (hazards/brake pumping possible)"
      : left ? "Left flashing" : right ? "Right flashing"
      : row.yellowWire && row.greenWire ? "Both wires on (braking possible)"
      : row.yellowWire || row.greenWire ? "One wire on; ambiguous" : "Both wires off";
    signal += ` · yellow ${row.yellowWire} · green ${row.greenWire}`;
  }
  byId("signal").textContent = signal;
  byId("gpsReadout").textContent = Object.entries(data.cameraRows).map(([camera, rows]) => {
    const point = nearest(rows, timeSec, camera === "gopro" ? 0.06 : 0.55);
    return point ? `${camera}: ${format(point.latitude, 7)}, ${format(point.longitude, 7)}; ${format(point.speedMps)} m/s; fix ${point.fix}` : `${camera}: no data`;
  }).join(" | ");
}

function seekTime(timeSec) {
  video.pause();
  if (!Number.isFinite(timeSec) || timeSec < 0) return showStatus("Enter a nonnegative time in seconds.");
  pendingTime = timeSec;
  const clip = data.clips.find(item => timeSec >= item.startSec && timeSec < item.endSec)
    || (timeSec === data.clips.at(-1)?.endSec ? data.clips.at(-1) : null);
  byId("time").value = timeSec.toFixed(2);
  if (!clip) {
    activeClip = null;
    pendingTime = null;
    video.onloadedmetadata = null;
    video.removeAttribute("src");
    video.load();
    updateReadouts();
    followGpsOnPause();
    showStatus("No review video at this time. Run review/prepareVideo.py if clips are missing.");
    return;
  }
  if (activeClip !== clip) {
    activeClip = clip;
    video.src = "/video/" + clip.file;
    video.onloadedmetadata = () => {
      if (activeClip !== clip || pendingTime == null) return;
      const targetTime = Math.max(0, pendingTime - clip.startSec);
      if (Math.abs(video.currentTime - targetTime) < 0.0001) {
        // At clip start there may be no seeked event; release the display clock.
        pendingTime = null;
        updateReadouts();
      } else video.currentTime = targetTime;
    };
  } else {
    if (Math.abs(video.currentTime - (timeSec - clip.startSec)) < 0.0001) pendingTime = null;
    video.currentTime = Math.max(0, timeSec - clip.startSec);
  }
  video.playbackRate = Number(byId("rate").value);
  updateReadouts();
  // Also handle frame steps and seeks to the current frame (no new pause event).
  followGpsOnPause();
}

function choosePosition(latitude, longitude) {
  chosenPosition = {latitude, longitude};
  currentMarker.setLatLng([latitude, longitude]);
  byId("position").textContent = `${latitude.toFixed(8)}, ${longitude.toFixed(8)} (decimal degrees)`;
}

function refreshPoints() {
  markers.clearLayers();
  byId("points").replaceChildren(...annotations.map((point, index) => {
    const marker = L.circleMarker([point.latitude, point.longitude], {radius: 3, color: "#245f89", weight: 1}).addTo(markers);
    marker.on("click", () => loadPoint(index));
    return new Option(`Point ${point.pointId} · ${point.timeSec.toFixed(2)} s · lap ${point.lap}`, index);
  }));
}

function loadPoint(index) {
  holding = false; holdGroup = null;
  byId("hold").textContent = "Hold this location";
  selectedIndex = Math.max(0, Math.min(annotations.length - 1, index));
  const point = annotations[selectedIndex];
  byId("points").value = selectedIndex;
  byId("pointId").value = point.pointId;
  byId("lap").value = point.lap;
  byId("confidence").value = point.confidencePct;
  byId("notes").value = point.notes || "";
  byId("customTrigger").value = point.customTrigger || "";
  for (const checkbox of document.querySelectorAll("#triggers input")) checkbox.checked = (point.triggers || "").split("|").includes(checkbox.value);
  choosePosition(point.latitude, point.longitude);
  map.panTo([point.latitude, point.longitude]);
  showStatus(`Point ${point.pointId} loaded. ${dirty ? "There are unsaved applied changes." : "Original source file is unchanged."}`);
  seekTime(point.timeSec);
}

async function request(route, body) {
  const response = await fetch(route, {method: "POST", headers: {"Content-Type": "application/json", "X-Annotation-Request": "local"}, body: JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw Error(result.error);
  return result;
}

map.on("click", event => { if (!holding) choosePosition(event.latlng.lat, event.latlng.lng); else showStatus("Location held. Release the hold before moving this point."); });
byId("previous").onclick = () => loadPoint(selectedIndex - 1);
byId("next").onclick = () => loadPoint(selectedIndex + 1);
byId("points").onchange = event => loadPoint(Number(event.target.value));
byId("seek").onclick = () => seekTime(Number(byId("time").value));
byId("back").onclick = () => seekTime(Math.max(0, driveTime() - 0.1));
byId("forward").onclick = () => seekTime(driveTime() + 0.1);
byId("rate").onchange = () => { video.playbackRate = Number(byId("rate").value); };
byId("followGps").onchange = followGpsOnPause;
byId("centerGps").onclick = centerOnGps;
byId("hold").onclick = () => {
  holding = !holding;
  if (holding) holdGroup = Math.max(0, ...annotations.map(point => point.stopGroup || 0)) + 1;
  byId("hold").textContent = holding ? "Release location hold" : "Hold this location";
};
byId("newPoint").onclick = () => {
  selectedIndex = -1;
  byId("pointId").value = Math.max(...annotations.map(point => point.pointId)) + 1;
  byId("notes").value = ""; byId("customTrigger").value = "";
  document.querySelectorAll("#triggers input").forEach(input => { input.checked = false; });
  showStatus("New point: choose a time, map position, and triggers, then apply changes.");
};
byId("update").onclick = () => {
  if (!chosenPosition) return showStatus("Choose a position first.");
  const prior = annotations[selectedIndex];
  const samePosition = prior && Math.abs(prior.latitude - chosenPosition.latitude) < 1e-12
    && Math.abs(prior.longitude - chosenPosition.longitude) < 1e-12;
  const point = {pointId: Number(byId("pointId").value), timeSec: Number(byId("time").value),
    lap: Number(byId("lap").value), ...chosenPosition, confidencePct: Number(byId("confidence").value),
    notes: byId("notes").value, customTrigger: byId("customTrigger").value,
    triggers: [...document.querySelectorAll("#triggers input:checked")].map(input => input.value).join("|"),
    locationHeld: holding || (samePosition && prior.locationHeld) || false,
    stopGroup: holding ? holdGroup : (samePosition ? prior.stopGroup : null)};
  if (selectedIndex < 0) annotations.push(point); else annotations[selectedIndex] = point;
  if (point.triggers.split("|").includes("begin_stop") && !holding) {
    holding = true;
    holdGroup = point.stopGroup ?? (Math.max(0, ...annotations.map(item => item.stopGroup || 0)) + 1);
    point.stopGroup = holdGroup;
  }
  if (point.triggers.split("|").includes("finish_stop")) holding = false;
  byId("hold").textContent = holding ? "Release location hold" : "Hold this location";
  annotations.sort((a, b) => a.timeSec - b.timeSec);
  selectedIndex = annotations.findIndex(item => item.pointId === point.pointId);
  editRevision++;
  dirty = true; refreshPoints(); byId("points").value = selectedIndex;
  showStatus("Point changes applied in memory. Save a new CSV to keep them.");
};
byId("save").onclick = async () => {
  if (saving) return;
  saving = true;
  byId("save").disabled = true;
  const savedRevision = editRevision;
  try {
    const result = await request("/save", {annotations});
    if (editRevision === savedRevision) dirty = false;
    showStatus(`Saved ${result.rows} points to ${result.saved}. ` +
      (dirty ? "Newer applied edits are still unsaved; save again to keep them." : "Original untouched."));
  }
  catch (error) { showStatus(error.message); }
  finally { saving = false; byId("save").disabled = false; }
};
byId("load").onchange = async event => {
  const file = event.target.files[0]; if (!file) return;
  if (saving) return showStatus("Wait for the current save to finish before loading another CSV.");
  if (dirty && !confirm("Replace unsaved applied changes with this CSV?")) return;
  const requestedRevision = editRevision;
  try {
    const result = await request("/load", {csv: await file.text()});
    if (saving) return showStatus("A save is in progress. Load this CSV again after it finishes.");
    if (editRevision !== requestedRevision && !confirm("New changes were applied while loading. Replace them with this CSV?")) return;
    annotations = result.annotations; editRevision++; dirty = false; refreshPoints(); loadPoint(0);
  }
  catch (error) { showStatus(error.message); }
};
video.addEventListener("timeupdate", () => data && updateReadouts());
video.addEventListener("pause", followGpsOnPause);
video.addEventListener("seeked", () => {
  pendingTime = null;
  if (data) updateReadouts();
  followGpsOnPause();
});
video.addEventListener("ended", () => {
  const index = data.clips.indexOf(activeClip);
  if (data.clips[index + 1]) {
    // The inferred clip timelines overlap slightly; do not jump backward.
    seekTime(Math.max(activeClip.endSec, data.clips[index + 1].startSec));
    video.addEventListener("seeked", () => video.play(), {once: true});
  }
});
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });

fetch("/data").then(response => response.json()).then(result => {
  if (result.error) throw Error(result.error);
  data = result; annotations = data.annotations;
  data.cameraRows = Object.fromEntries(["garmin", "gopro", "redtiger", "rove"].map(camera => [camera, data.gps.filter(row => row.camera === camera)]));
  for (const [slug, label] of Object.entries(data.triggers)) {
    const wrapper = document.createElement("label"), input = document.createElement("input");
    input.type = "checkbox"; input.value = slug; wrapper.append(input, document.createTextNode(" " + label));
    byId("triggers").append(wrapper);
  }
  refreshPoints(); loadPoint(0);
}).catch(error => showStatus(error.message));
