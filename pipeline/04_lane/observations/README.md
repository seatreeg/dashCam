# Recorded spatial observations: the geometry inputs

These seven JSON files are unchanged copies of observations made during the
original lane review. Source paths and SHA-256 hashes are recorded in
`audit/setup/laneInputManifest.json`. They contain spatial assumptions and
provenance, not GPS receiver fits or finished v4 paths.

- `draftControls.json`: visually traced image-pixel controls for two early guides. These guides were rejected as lane centers, but their regenerated shapes locate later boundary observations. `pixels` are local x/y pixels; `point_pairs` identify endpoints on each lap.
- `mapFrames.json`: zoom and global x/y origins of those image panels, plus historical tile hashes. The old absolute tile paths are provenance strings; the builder does not open them. The copied imagery is under the current `imagery/tiles/` directory.
- `corridorObservations.json`: four traced lane corridors. Each `observations` row is `[station, rightBoundaryOffset, leftBoundaryOffset]` in meters. Offsets are positive to the left of the scaffold. `seed_pair` selects annotation endpoints where no earlier image-pixel scaffold is needed. Inferred/occluded edges are noted explicitly.
- `maneuverObservations.json`: parking, fork, intersection, and roundabout models. `origin` is UTM 14N east/north metres; `knots`, `envelope`, and `obstacles` are metre offsets from it. `spans` identify annotation IDs; local entry/exit directions or neighboring-corridor tangents determine the curve. Envelopes/obstacles document approximate visual guardrails, not surveyed clearance.
- `networkObservations.json`: eight continuous road/lane models across the three laps. `center_knots` are `[scaffoldStation, leftOffset]` in metres. `initial_offset` and `changes` define lane occupancy; each change is `[firstPointId, lastPointId, newLeftOffset]`. The scaffolds are rebuilt from the first-lap annotation spans in `geometryStages.R`, not loaded as finished paths.
- `rightmostObservations.json`: the final v4 curb/divider observations and center knots for Salt Creek. The listed spans receive the rightmost-lane correction; their first/last derived anchors remain fixed to the preceding network construction.
- `roadChecks.json`: historical, approximate visual road guardrails. Preserved as supporting observation evidence; they are not used to fit paths. Their v3 Salt Creek limits are superseded by the v4 lane-specific check.

All meter coordinates use EPSG:32614. Station is distance along a spatial
scaffold, not elapsed time. These inputs were selected from imagery/footage and
user review; regeneration does not automatically recognize roads in pixels.
The current builder checks corridor clearance, shared endpoints, and the v4
rightmost-lane boundaries. Numerical regression checks all 374 intervals against
the approved v4 reference. Neither check establishes surveyed vehicle accuracy.

The generated `lanePaths.geojson` stores one geographic LineString per interval.
Its properties identify endpoints/lap and retain the historical route/mode/status
metadata for timing and exclusion compatibility. `laneRoute.json` adds endpoint
times in seconds and horizontal path length in metres. `laneAnchors.json` stores
original versus derived UTM positions, their displacement, and lane-model owner.
These geometry/audit files are not extra columns in the main scientific CSV.
