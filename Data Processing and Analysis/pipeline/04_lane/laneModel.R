source("helpers.R")
suppressPackageStartupMessages(library(sf))

# DISTANCE HELPERS: projected coordinates exist only while calculating meters
toMeters <- function(longitude, latitude) {
  points <- st_as_sf(data.frame(longitude, latitude),
                    coords = c("longitude", "latitude"), crs = 4326, na.fail = FALSE)
  st_coordinates(st_transform(points, settings$distanceCrs))
}

toDegrees <- function(east, north) {
  result <- matrix(NA_real_, nrow = length(east), ncol = 2)
  use <- is.finite(east) & is.finite(north)
  if (any(use)) {
    points <- st_as_sf(data.frame(east = east[use], north = north[use]),
                      coords = c("east", "north"), crs = settings$distanceCrs)
    result[use, ] <- st_coordinates(st_transform(points, 4326))
  }
  result
}

pathDistance <- function(xy) c(0, cumsum(sqrt(rowSums(diff(xy)^2))))

# THE ODOMETER IS THE EXACT INTEGRAL OF LINEARLY INTERPOLATED OBD SPEED.
readOdometer <- function() {
  vehicle <- readCsv("pipeline/02_extract/combined/vehicle.csv")
  vehicle <- vehicle[is.finite(vehicle$speedMps) & vehicle$obdConnected == 1, ]
  stopifnot(all(diff(vehicle$timeSec) > 0), all(vehicle$speedMps >= 0))
  vehicle$distanceM <- c(0, cumsum(diff(vehicle$timeSec) *
    (head(vehicle$speedMps, -1) + tail(vehicle$speedMps, -1)) / 2))
  vehicle
}

odometerAt <- function(vehicle, timeSec) {
  row <- findInterval(timeSec, vehicle$timeSec)
  distanceM <- rep(NA_real_, length(timeSec))
  use <- row >= 1 & row < nrow(vehicle)
  index <- row[use]
  elapsed <- timeSec[use] - vehicle$timeSec[index]
  slope <- (vehicle$speedMps[index + 1] - vehicle$speedMps[index]) /
           (vehicle$timeSec[index + 1] - vehicle$timeSec[index])
  distanceM[use] <- vehicle$distanceM[index] + vehicle$speedMps[index] * elapsed +
                   0.5 * slope * elapsed^2
  distanceM[timeSec == tail(vehicle$timeSec, 1)] <- tail(vehicle$distanceM, 1)
  distanceM
}

readElevation <- function() {
  elevation <- read_json("pipeline/04_lane/inputs/elevation.json", simplifyVector = TRUE)
  stopifnot(elevation$grid_m == 5, elevation$units == "meters")
  cells <- unlist(elevation$cells)
  for (fileName in c("elevationSupplement.json", "elevationV4.json")) {
    extra <- read_json(paste0("pipeline/04_lane/inputs/", fileName))$cells
    for (key in setdiff(names(extra), names(cells))) cells[key] <- extra[[key]]$value_m
  }
  cells
}

# SAME V4 PROFILE: elevation every 5 m, local linear smoothing over 30 m
surfaceProfile <- function(path, cells) {
  sampleM <- sort(unique(c(seq(0, tail(path$distanceM, 1), by = 5), tail(path$distanceM, 1))))
  east <- approx(path$distanceM, path$east, sampleM)$y
  north <- approx(path$distanceM, path$north, sampleM)$y
  keys <- paste(floor(east / 5), floor(north / 5), sep = "_")
  elevation <- unname(cells[keys])
  if (any(!is.finite(elevation))) stop("Missing elevation cell; do not substitute GPS altitude.")
  smoothElevation <- vapply(sampleM, function(position) {
    use <- abs(sampleM - position) <= 15 + 1e-8
    if (sum(use) < 2) use <- order(abs(sampleM - position))[1:2]
    fit <- lm.fit(cbind(1, sampleM[use] - position), elevation[use])
    unname(fit$coefficients[1])
  }, numeric(1))
  elevation <- approx(sampleM, smoothElevation, path$distanceM)$y
  distanceSurface <- c(0, cumsum(sqrt(diff(path$distanceM)^2 + diff(elevation)^2)))
  list(distanceSurface = distanceSurface, maximumGrade = max(abs(diff(elevation) / diff(path$distanceM))))
}

checkReviewedAnnotations <- function() {
  annotations <- readCsv("pipeline/03_annotate/annotations.csv")
  original <- readCsv("pipeline/03_annotate/original_annotation/anno 8 11 2026.csv")
  # these recorded imagery observations are tied to the original reviewed drive
  # the geometry can be rebuilt, but new annotations need a new spatial review
  stopifnot(identical(annotations$pointId, original$point_idx),
            identical(annotations$timeSec, original$video_time_sec),
            max(abs(annotations$latitude - original$click_lat)) < 1e-11,
            max(abs(annotations$longitude - original$click_lon)) < 1e-11,
            identical(annotations$confidencePct, original$confidence_pct),
            identical(annotations$lap, original$trip),
            identical(annotations$triggers, original$triggers),
            identical(annotations$stopGroup, original$stop_group),
            identical(annotations$locationHeld, original$location_held == "yes"))
  invisible(annotations)
}

buildLaneModel <- function() {
  annotations <- checkReviewedAnnotations()
  build <- fromJSON("pipeline/04_lane/generated/geometryBuild.json", simplifyVector = FALSE)
  stopifnot(build$distanceCrs == settings$distanceCrs)
  for (file in names(c(build$inputs, build$code, build$outputs))) {
    expected <- c(build$inputs, build$code, build$outputs)[[file]]
    if (digest::digest(file = file, algo = "sha256") != expected)
      stop("Stale lane geometry: rerun Rscript pipeline/04_lane/buildLaneGeometry.R")
  }
  shapes <- st_transform(st_read("pipeline/04_lane/generated/lanePaths.geojson", quiet = TRUE), settings$distanceCrs)
  route <- fromJSON("pipeline/04_lane/generated/laneRoute.json", simplifyVector = FALSE)$segments
  stopifnot(nrow(shapes) == nrow(annotations) - 1, length(route) == nrow(shapes))
  vehicle <- readOdometer()
  cells <- readElevation()
  paths <- intervals <- vector("list", length(route))

  for (index in seq_along(route)) {
    segment <- route[[index]]
    xy <- st_coordinates(shapes[index, ])[, 1:2]
    stopifnot(segment$point_a == annotations$pointId[index],
              segment$point_b == annotations$pointId[index + 1],
              segment$time_a == annotations$timeSec[index],
              segment$time_b == annotations$timeSec[index + 1])
    held <- segment$status == "stop" || segment$mode == "stop_conflict" || segment$length_m < 1e-6
    if (held) xy <- rbind(xy[1, ], xy[1, ])
    distanceM <- pathDistance(xy)
    if (!held) {
      samples <- sort(unique(c(distanceM, seq(0, tail(distanceM, 1), by = 0.5))))
      xy <- cbind(approx(distanceM, xy[, 1], samples)$y, approx(distanceM, xy[, 2], samples)$y)
      distanceM <- samples
    }
    path <- data.frame(east = xy[, 1], north = xy[, 2], distanceM)
    odo <- odometerAt(vehicle, c(segment$time_a, segment$time_b))
    reasons <- character()
    if (segment$mode == "stop_conflict") reasons <- c(reasons, "conflicting_stop_coordinates")
    if (held && !segment$mode %in% c("held_stop", "stop_conflict"))
      reasons <- c(reasons, "coincident_anchors_without_held_stop")
    if (segment$status %in% c("conflict", "unsupported")) reasons <- c(reasons, "flagged_v4_geometry")
    confidence <- min(annotations$confidencePct[index:(index + 1)])
    if (confidence < 60) reasons <- c(reasons, "anchor_confidence_below_60")
    if (segment$route %in% c("fork", "salt_creek_after_circle"))
      reasons <- c(reasons, "construction_or_occluded_lane_uncertainty")
    maximumGrade <- NA_real_
    available <- TRUE
    path$distanceSurface <- 0
    if (!held) {
      overlap <- head(vehicle$timeSec, -1) < segment$time_b & tail(vehicle$timeSec, -1) > segment$time_a
      obdOk <- segment$time_a >= min(vehicle$timeSec) && segment$time_b <= max(vehicle$timeSec) &&
               all(diff(vehicle$timeSec)[overlap] <= 0.5)
      motionOk <- is.finite(diff(odo)) && diff(odo) > 1e-6
      if (!obdOk) reasons <- c(reasons, "missing_or_gapped_obd")
      if (!motionOk) reasons <- c(reasons, "no_supported_obd_motion")
      profile <- surfaceProfile(path, cells)
      path$distanceSurface <- profile$distanceSurface
      maximumGrade <- profile$maximumGrade
      if (maximumGrade > 0.15) reasons <- c(reasons, "elevation_grade_needs_review")
      ratio <- tail(path$distanceSurface, 1) / diff(odo)
      if (is.finite(ratio) && (ratio < 0.75 || ratio > 1.25)) reasons <- c(reasons, "path_obd_distance_disagreement")
      available <- obdOk && motionOk
    }
    paths[[index]] <- path
    intervals[[index]] <- data.frame(
      segment = index, pointA = segment$point_a, pointB = segment$point_b,
      timeA = segment$time_a, timeB = segment$time_b, lap = annotations$lap[index],
      held, modelAvailable = available, referenceOk = available && !length(reasons),
      reason = if (length(reasons)) paste(unique(reasons), collapse = ";") else "supported_v4_model",
      obdA = odo[1], obdB = odo[2], surfaceM = tail(path$distanceSurface, 1), maximumGrade)
  }
  list(paths = paths, intervals = do.call(rbind, intervals), vehicle = vehicle,
       annotationTimes = annotations$timeSec)
}

referenceAt <- function(model, timeSec) {
  segment <- findInterval(timeSec, model$annotationTimes)
  segment[timeSec == tail(model$annotationTimes, 1)] <- length(model$paths)
  inside <- is.finite(timeSec) & timeSec >= model$annotationTimes[1] & timeSec <= tail(model$annotationTimes, 1)
  east <- north <- rep(NA_real_, length(timeSec))
  for (index in unique(segment[inside])) {
    use <- which(inside & segment == index)
    interval <- model$intervals[index, ]
    path <- model$paths[[index]]
    if (!interval$modelAvailable) next
    if (interval$held) {
      east[use] <- path$east[1]
      north[use] <- path$north[1]
    } else {
      fraction <- (odometerAt(model$vehicle, timeSec[use]) - interval$obdA) / (interval$obdB - interval$obdA)
      stopifnot(all(is.finite(fraction)), all(fraction >= -1e-9 & fraction <= 1 + 1e-9))
      position <- pmin(1, pmax(0, fraction)) * interval$surfaceM
      east[use] <- approx(path$distanceSurface, path$east, position)$y
      north[use] <- approx(path$distanceSurface, path$north, position)$y
    }
  }
  data.frame(timeSec, segment = ifelse(inside, segment, NA_integer_), east, north)
}
