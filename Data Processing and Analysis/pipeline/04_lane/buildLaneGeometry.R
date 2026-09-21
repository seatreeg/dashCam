# Run from the project root: Rscript pipeline/04_lane/buildLaneGeometry.R
source("pipeline/04_lane/geometryStages.R")


buildLaneGeometry <- function(outputDir = "pipeline/04_lane/generated") {
  stopifnot(settings$distanceCrs == 32614)
  dir.create(outputDir, recursive = TRUE, showWarnings = FALSE)
  points <- readGeometryAnnotations()
  cat("1/5: Rebuild annotation connectors and imagery-coordinate scaffolds.\n")
  paths <- annotationConnectors(points)
  drafts <- buildDraftScaffolds(points, outputDir)
  cat("2/5: Fit the recorded lane boundaries and endpoint connections.\n")
  corridors <- buildCorridors(points, drafts, outputDir)
  cat("3/5: Rebuild parking, fork, intersection, and roundabout maneuvers.\n")
  maneuvers <- buildManeuvers(points, paths, corridors$paths, outputDir)
  stopifnot(!any(names(maneuvers) %in% names(corridors$paths)))
  for (replacement in list(corridors$paths, maneuvers)) {
    for (name in names(replacement)) paths[[as.integer(name)]] <- replacement[[name]]
  }
  cat("4/5: Rebuild persistent road/lane guidance across all three laps.\n")
  network <- buildNetwork(points, paths, outputDir)
  cat("5/5: Apply the recorded v4 rightmost-lane correction.\n")
  result <- applyRightmostCorrection(points, network)
  paths <- result$paths
  anchors <- result$anchors
  adjusted <- as.matrix(anchors[, c("derivedEast", "derivedNorth")])
  endpointGaps <- vapply(seq_along(paths), function(index) {
    xy <- paths[[index]]$xy
    max(sqrt(rowSums((xy[c(1, nrow(xy)), , drop = FALSE] - adjusted[c(index, index + 1), ])^2)))
  }, numeric(1))
  stopifnot(length(paths) == 374, nrow(anchors) == 375, max(endpointGaps) < 1e-6,
            length(result$changed) == 19)

  # Lane-specific check: observed right curb / left divider, not just pavement.
  observations <- asMatrix(readObservations("rightmostObservations")$boundaries)
  xy <- do.call(rbind, lapply(paths[result$changed], `[[`, "xy"))
  station <- vapply(seq_len(nrow(xy)), function(row) projectStation(result$rightmostFrame, xy[row, ]), numeric(1))
  frame <- frameAt(result$rightmostFrame, station)
  offset <- rowSums((xy - frame$xy) * frame$left)
  use <- station >= 0 & station <= 235
  right <- splinefun(observations[, 1], observations[, 2], method = "monoH.FC")(station[use])
  left <- splinefun(observations[, 1], observations[, 3], method = "monoH.FC")(station[use])
  clearance <- min(offset[use] - right, left - offset[use])
  stopifnot(clearance > 1)

  # keep the established segment metadata names for the timing consumer
  # there are no pixel coordinates or camera observations in these outputs
  segments <- lapply(seq_along(paths), function(index) {
    path <- paths[[index]]
    list(id = index, lap = points$lap[index], point_a = points$pointId[index],
      point_b = points$pointId[index + 1], time_a = points$timeSec[index], time_b = points$timeSec[index + 1],
      status = path$status, mode = path$mode, route = path$route, length_m = tail(pathDistance(path$xy), 1))
  })
  saveSpatialStage(paths, points, file.path(outputDir, "lanePaths.geojson"))
  write_json(anchors, file.path(outputDir, "laneAnchors.json"), pretty = TRUE, digits = 12, na = "null")
  write_json(list(status = "reconstructed_v4_spatial_assumptions_not_surveyed_truth", segments = segments),
    file.path(outputDir, "laneRoute.json"), auto_unbox = TRUE, digits = 12)
  inputFiles <- c("pipeline/03_annotate/annotations.csv", list.files("pipeline/04_lane/observations", "[.]json$", full.names = TRUE))
  codeFiles <- c("helpers.R", paste0("pipeline/04_lane/", c("buildLaneGeometry.R", "geometryMath.R", "geometryStages.R")))
  hashes <- function(files) as.list(setNames(vapply(files, function(file) digest::digest(file = file, algo = "sha256"), ""), files))
  outputFiles <- file.path(outputDir, c("lanePaths.geojson", "laneAnchors.json", "laneRoute.json"))
  audit <- list(status = "built_from_annotations_and_recorded_spatial_observations",
    intervals = length(paths), annotations = nrow(anchors), persistentLaneIntervals = sum(network$covered),
    v4ChangedIntervals = length(result$changed), maximumEndpointGapM = max(endpointGaps),
    rightmostMinimumClearanceM = clearance, corridorChecks = corridors$checks,
    finishedPathsRead = FALSE, cameraGpsRead = FALSE, distanceCrs = settings$distanceCrs,
    inputs = hashes(inputFiles), code = hashes(codeFiles), outputs = hashes(outputFiles))
  write_json(audit, file.path(outputDir, "geometryBuild.json"), pretty = TRUE, auto_unbox = TRUE, digits = 12)
  cat("Rebuilt", length(paths), "intervals; output:", outputDir, "\n")
  invisible(result)
}

if (sys.nframe() == 0) buildLaneGeometry()
