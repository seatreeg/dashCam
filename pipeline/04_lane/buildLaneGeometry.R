# Run from the project root. DASHCAM_PYTHON selects the NumPy/SciPy interpreter.
source("helpers.R")
suppressPackageStartupMessages(library(sf))

buildLaneGeometry <- function(outputDir = "Data/laneGeometry") {
  stopifnot(settings$distanceCrs == 32614)
  points <- readCsv("Annotation Data/annotations.csv")
  choices <- fromJSON("pipeline/04_lane/inputs/laneChoices.json", simplifyVector = FALSE)
  stopifnot(nrow(points) == 375, all(points$pointId == unlist(choices$annotation_point_ids)),
            all(diff(points$timeSec) > 0), all(points$lap %in% 1:3),
            choices$distance_crs == settings$distanceCrs)
  projected <- st_coordinates(st_transform(st_as_sf(points,
    coords = c("longitude", "latitude"), crs = 4326), settings$distanceCrs))
  records <- lapply(seq_len(nrow(points)), function(i) {
    row <- as.list(points[i, ])
    row$xy <- unname(projected[i, ])
    row
  })
  dir.create(outputDir, recursive = TRUE, showWarnings = FALSE)
  annotationFile <- file.path(outputDir, "annotationsUtm.json")
  write_json(records, annotationFile, auto_unbox = TRUE, digits = NA, na = "null")
  python <- Sys.getenv("DASHCAM_PYTHON", unset = "python")
  for (script in c("sourceChains.py", "laneGeometry.py")) {
    arguments <- c("-X", "utf8", shQuote(paste0("pipeline/04_lane/", script)),
      "--annotations", shQuote(annotationFile), "--output-dir", shQuote(outputDir))
    log <- system2(python, arguments, stdout = TRUE, stderr = TRUE)
    cat(paste(log, collapse = "\n"), "\n")
    if (!is.null(attr(log, "status")) && attr(log, "status") != 0)
      stop("GIS geometry build failed. Install pipeline/04_lane/requirements.txt in DASHCAM_PYTHON.")
  }
  result <- fromJSON(file.path(outputDir, "sourceGuidedGeometry.json"), simplifyVector = FALSE)
  reviewed <- fromJSON("pipeline/04_lane/inputs/reviewExclusions.json", simplifyVector = FALSE)$intervals
  heldIds <- unlist(lapply(result$stationary_anchor_groups, function(ids) head(unlist(ids), -1)))
  xyPaths <- lapply(result$paths, function(p) do.call(rbind, lapply(p$xy, unlist)))
  adjusted <- do.call(rbind, lapply(as.character(points$pointId), function(id) unlist(result$anchors[[id]])))
  lengths <- vapply(xyPaths, function(xy) sum(sqrt(rowSums(diff(xy)^2))), 0.0)
  segments <- lapply(seq_along(xyPaths), function(i) {
    p <- result$paths[[i]]
    reasons <- unique(unlist(lapply(Filter(function(x) x$point_a == points$pointId[i], reviewed), `[[`, "reasons")))
    held <- points$pointId[i] %in% heldIds
    list(id = i, lap = points$lap[i], point_a = points$pointId[i], point_b = points$pointId[i + 1],
      time_a = points$timeSec[i], time_b = points$timeSec[i + 1],
      status = if (held) "stop" else "modeled", mode = if (held) "held_stop" else "source_guided",
      route = p$road, length_m = lengths[i], method = p$method,
      source_ids = p$source_ids, curb_ids = p$curb_ids, review_flags = as.list(reasons))
  })
  features <- st_sf(point_a = points$pointId[-nrow(points)], point_b = points$pointId[-1],
    trip = points$lap[-nrow(points)], route = vapply(segments, `[[`, "", "route"),
    status = vapply(segments, `[[`, "", "status"), mode = vapply(segments, `[[`, "", "mode"),
    geometry = st_sfc(lapply(xyPaths, st_linestring), crs = settings$distanceCrs))
  st_write(st_transform(features, 4326), file.path(outputDir, "lanePaths.geojson"), delete_dsn = TRUE, quiet = TRUE)
  anchors <- data.frame(pointId = points$pointId, lap = points$lap, timeSec = points$timeSec,
    rawEast = projected[, 1], rawNorth = projected[, 2], derivedEast = adjusted[, 1], derivedNorth = adjusted[, 2],
    adjustmentM = sqrt(rowSums((adjusted - projected)^2)), model = "source_guided_gis",
    confidencePct = points$confidencePct)
  endpointGaps <- vapply(seq_along(xyPaths), function(i)
    max(sqrt(rowSums((xyPaths[[i]][c(1, nrow(xyPaths[[i]])), , drop = FALSE] - adjusted[c(i, i + 1), ])^2))), 0.0)
  stopifnot(length(xyPaths) == 374, all(is.finite(unlist(xyPaths))), max(endpointGaps) < 1e-6)
  write_json(anchors, file.path(outputDir, "laneAnchors.json"), pretty = TRUE, digits = NA, na = "null")
  write_json(list(status = "source_guided_gis_model_not_surveyed_truth", segments = segments),
    file.path(outputDir, "laneRoute.json"), auto_unbox = TRUE, digits = NA, null = "null")
  inputFiles <- c("Annotation Data/annotations.csv", "settings.json",
    "pipeline/04_lane/inputs/laneChoices.json", "pipeline/04_lane/inputs/reviewExclusions.json",
    list.files("pipeline/04_lane/inputs/gis", "[.]json$", full.names = TRUE))
  codeFiles <- c("helpers.R", paste0("pipeline/04_lane/", c("buildLaneGeometry.R", "sourceChains.py", "laneGeometry.py", "requirements.txt")))
  hashes <- function(files) as.list(setNames(vapply(files, function(file) digest::digest(file = file, algo = "sha256"), ""), files))
  outputFiles <- file.path(outputDir, c("annotationsUtm.json", "sourceChains.json", "sourceGuidedGeometry.json",
    "lanePaths.geojson", "laneAnchors.json", "laneRoute.json"))
  runtime <- system2(python, c("-c", shQuote("import sys,numpy,scipy,json;print(json.dumps(dict(python=sys.version,numpy=numpy.__version__,scipy=scipy.__version__)))")), stdout = TRUE)
  audit <- list(status = "built_from_frozen_municipal_features_and_saved_lane_choices",
    intervals = length(xyPaths), annotations = nrow(anchors), maximumEndpointGapM = max(endpointGaps),
    typicalWidthM = result$typical_width_m, modeledRoadJoins = result$modeled_road_joins,
    finishedPathsRead = FALSE, cameraGpsRead = FALSE, distanceCrs = settings$distanceCrs,
    runtime = fromJSON(paste(runtime, collapse = "\n")), inputs = hashes(inputFiles), code = hashes(codeFiles), outputs = hashes(outputFiles))
  write_json(audit, file.path(outputDir, "geometryBuild.json"), pretty = TRUE, auto_unbox = TRUE, digits = NA)
  cat("Rebuilt", length(xyPaths), "source-guided GIS intervals; output:", outputDir, "\n")
  invisible(list(paths = xyPaths, anchors = anchors))
}

if (sys.nframe() == 0) buildLaneGeometry()
