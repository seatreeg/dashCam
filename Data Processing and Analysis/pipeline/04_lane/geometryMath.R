# SMALL SPATIAL HELPERS. No camera GPS, OBD, elevation, or old paths are read.
source("helpers.R")
suppressPackageStartupMessages(library(sf))


readObservations <- function(name) {
  fromJSON(paste0("pipeline/04_lane/observations/", name, ".json"), simplifyVector = FALSE)
}

asMatrix <- function(rows) do.call(rbind, lapply(rows, unlist))
pathDistance <- function(xy) c(0, cumsum(sqrt(rowSums(diff(xy)^2))))
unitVector <- function(value) {
  size <- sqrt(sum(value^2))
  if (size > 1e-10) value / size else c(0, 0)
}
smoothStep <- function(value) {
  value <- pmin(1, pmax(0, value))
  value^3 * (10 - 15 * value + 6 * value^2)
}

readGeometryAnnotations <- function() {
  points <- readCsv("pipeline/03_annotate/annotations.csv")
  points <- points[order(points$timeSec), ]
  stopifnot(nrow(points) == 375, all(points$pointId == 0:374),
            all(diff(points$timeSec) > 0), all(points$lap %in% 1:3))
  spatial <- st_as_sf(points, coords = c("longitude", "latitude"), crs = 4326)
  points[, c("east", "north")] <- st_coordinates(st_transform(spatial, settings$distanceCrs))
  points
}

hasEvent <- function(points, index, event) {
  event %in% strsplit(points$triggers[index], "|", fixed = TRUE)[[1]]
}
eventSpans <- function(points, begin, finish) {
  result <- list()
  for (lap in unique(points$lap)) {
    active <- NULL
    for (index in which(points$lap == lap)) {
      if (hasEvent(points, index, finish) && !is.null(active)) {
        result[[length(result) + 1]] <- points$pointId[c(active, index)]
        active <- NULL
      }
      if (hasEvent(points, index, begin)) active <- index
    }
  }
  result
}

neighborDirection <- function(points, index, step) {
  neighbor <- index + step
  while (neighbor >= 1 && neighbor <= nrow(points)) {
    delta <- as.numeric(points[neighbor, c("east", "north")] - points[index, c("east", "north")])
    if (sqrt(sum(delta^2)) > 3) return(unitVector(delta) * step)
    neighbor <- neighbor + step
  }
  c(NA_real_, NA_real_)
}

annotationConnectors <- function(points) {
  paths <- vector("list", nrow(points) - 1)
  for (index in seq_along(paths)) {
    ends <- as.matrix(points[c(index, index + 1), c("east", "north")])
    lengthM <- sqrt(sum(diff(ends)^2))
    groups <- points$stopGroup[c(index, index + 1)]
    sameGroup <- all(!is.na(groups)) && groups[1] == groups[2]
    stopPair <- hasEvent(points, index, "begin_stop") && hasEvent(points, index + 1, "finish_stop")
    incoming <- neighborDirection(points, index, -1)
    outgoing <- neighborDirection(points, index + 1, 1)
    known <- all(is.finite(c(incoming, outgoing)))
    angle <- if (known) acos(pmin(1, pmax(-1, sum(incoming * outgoing)))) else NA_real_
    crossing <- hasEvent(points, index, "enter_intersection") && hasEvent(points, index + 1, "exit_intersection")
    turn <- hasEvent(points, index, "turn_begin_nonint") && hasEvent(points, index + 1, "turn_finish_nonint")
    laneChange <- hasEvent(points, index, "lane_change_begin") && hasEvent(points, index + 1, "lane_change_finish")
    curved <- turn || laneChange || (crossing && is.finite(angle) && angle > pi / 12)
    mode <- if (sameGroup && lengthM < 1e-5) "held_stop" else if (stopPair || sameGroup) "stop_conflict" else
      if (curved && !known) "unknown_turn_heading" else if (curved) "candidate_curve" else "candidate_straight"
    u <- seq(0, 1, length.out = max(3, ceiling(lengthM / 0.5) + 1))
    xy <- outer(1 - u, ends[1, ]) + outer(u, ends[2, ])
    if (mode == "candidate_curve") {
      xy <- outer(2*u^3 - 3*u^2 + 1, ends[1, ]) + outer(u^3 - 2*u^2 + u, incoming * lengthM) +
        outer(-2*u^3 + 3*u^2, ends[2, ]) + outer(u^3 - u^2, outgoing * lengthM)
    }
    status <- if (mode == "held_stop") "stop" else
      if (mode %in% c("stop_conflict", "unknown_turn_heading")) "unsupported" else "provisional"
    paths[[index]] <- list(xy = xy, mode = mode, status = status, route = "annotation_connector")
  }
  paths
}

bezier <- function(first, firstHandle, lastHandle, last) {
  lengthM <- sum(sqrt(rowSums(diff(rbind(first, firstHandle, lastHandle, last))^2)))
  u <- seq(0, 1, length.out = max(3, ceiling(lengthM / 0.15) + 1))
  outer((1-u)^3, first) + outer(3*(1-u)^2*u, firstHandle) +
    outer(3*(1-u)*u^2, lastHandle) + outer(u^3, last)
}

guideFromKnots <- function(xy) {
  count <- nrow(xy)
  directions <- t(vapply(seq_len(count), function(index) {
    if (index == 1) unitVector(xy[2, ] - xy[1, ]) else
      if (index == count) unitVector(xy[count, ] - xy[count - 1, ]) else
        unitVector(unitVector(xy[index, ] - xy[index - 1, ]) + unitVector(xy[index + 1, ] - xy[index, ]))
  }, numeric(2)))
  pieces <- lapply(seq_len(count - 1), function(index) {
    handle <- sqrt(sum((xy[index + 1, ] - xy[index, ])^2)) / 3
    piece <- bezier(xy[index, ], xy[index, ] + handle * directions[index, ],
      xy[index + 1, ] - handle * directions[index + 1, ], xy[index + 1, ])
    if (index > 1) piece[-1, , drop = FALSE] else piece
  })
  do.call(rbind, pieces)
}

makeFrame <- function(xy) list(xy = xy, station = pathDistance(xy), length = tail(pathDistance(xy), 1))
frameAt <- function(frame, station, delta = 0.15, lower = min(frame$station)) {
  position <- function(value) cbind(approx(frame$station, frame$xy[, 1], value, rule = 2)$y,
                                   approx(frame$station, frame$xy[, 2], value, rule = 2)$y)
  tangent <- position(pmin(max(frame$station), station + delta)) - position(pmax(lower, station - delta))
  tangent <- tangent / sqrt(rowSums(tangent^2))
  list(xy = position(station), left = cbind(-tangent[, 2], tangent[, 1]), tangent = tangent)
}
projectStation <- function(frame, point) {
  vector <- diff(frame$xy)
  fraction <- pmin(1, pmax(0, rowSums(sweep(head(frame$xy, -1), 2, point, "-") * (-vector)) / rowSums(vector^2)))
  projected <- head(frame$xy, -1) + vector * fraction
  index <- which.min(rowSums(sweep(projected, 2, point, "-")^2))
  frame$station[index] + fraction[index] * diff(frame$station)[index]
}
cropGuide <- function(guide, endpoints) {
  frame <- makeFrame(guide)
  ends <- vapply(1:2, function(index) projectStation(frame, endpoints[index, ]), numeric(1))
  stopifnot(diff(ends) > 1e-6)
  sample <- sort(unique(c(ends, frame$station[frame$station > ends[1] & frame$station < ends[2]])))
  base <- cbind(approx(frame$station, guide[, 1], sample)$y, approx(frame$station, guide[, 2], sample)$y)
  blend <- min(12, diff(ends) / 2)
  weight <- function(distance) { u <- pmin(1, pmax(0, distance / blend)); 1 - 3*u^2 + 2*u^3 }
  path <- base + outer(weight(sample - ends[1]), endpoints[1, ] - base[1, ]) +
    outer(weight(ends[2] - sample), endpoints[2, ] - base[nrow(base), ])
  path[c(1, nrow(path)), ] <- endpoints
  path
}



# preserve the historical GeoJSON save/reload boundaries, including rounding.
# these files are GENERATED at runtime, never copied from approved v4 paths.
saveSpatialStage <- function(paths, points, fileName) {
  indices <- as.integer(names(paths))
  if (is.null(names(paths))) indices <- seq_along(paths)
  features <- st_sf(point_a = points$pointId[indices], point_b = points$pointId[indices + 1],
    trip = points$lap[indices], route = vapply(paths, `[[`, "", "route"),
    status = vapply(paths, `[[`, "", "status"), mode = vapply(paths, `[[`, "", "mode"),
    geometry = st_sfc(lapply(paths, function(path) st_linestring(path$xy)), crs = settings$distanceCrs))
  st_write(st_transform(features, 4326), fileName, delete_dsn = TRUE, quiet = TRUE)
  saved <- st_transform(st_read(fileName, quiet = TRUE), settings$distanceCrs)
  for (index in seq_along(paths)) paths[[index]]$xy <- st_coordinates(saved[index, ])[, 1:2]
  paths
}
