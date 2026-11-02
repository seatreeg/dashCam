# RECONSTRUCT THE RECORDED SPATIAL DECISIONS, IN THEIR ORIGINAL ORDER.
source("pipeline/04_lane/geometryMath.R")

buildDraftScaffolds <- function(points, outputDir) {
  controls <- readObservations("draftControls")
  mapFrames <- readObservations("mapFrames")
  size <- 256 * 2^mapFrames$zoom
  pixelDegrees <- function(xy) cbind(xy[, 1] / size * 360 - 180,
                                   atan(sinh(pi * (1 - 2 * xy[, 2] / size))) * 180 / pi)
  drafts <- list()
  for (road in controls$routes) {
    region <- Filter(function(region) region$name == road$map, mapFrames$regions)[[1]]
    ll <- pixelDegrees(sweep(asMatrix(road$pixels), 2, c(region$x0, region$y0), "+"))
    spatial <- st_as_sf(data.frame(lon = ll[, 1], lat = ll[, 2]), coords = c("lon", "lat"), crs = 4326)
    knots <- st_coordinates(st_transform(spatial, settings$distanceCrs))
    for (pair in road$point_pairs) {
      indices <- match(unlist(pair), points$pointId)
      ends <- as.matrix(points[indices, c("east", "north")])
      xy <- rbind(ends[1, ], knots, ends[2, ])
      station <- pathDistance(xy)
      stopifnot(all(diff(station) > 0))
      sample <- sort(unique(c(station, seq(0, max(station), by = 0.5))))
      curve <- cbind(splinefun(station, xy[, 1], method = "monoH.FC")(sample),
                     splinefun(station, xy[, 2], method = "monoH.FC")(sample))
      # This earlier trace was rejected as a lane, but remains its observation
      # coordinate frame. Recreate it; do not read an old serialized curve.
      drafts[[paste0(road$id, "_", points$lap[indices[1]])]] <- curve
    }
  }
  shapes <- st_sfc(lapply(drafts, st_linestring), crs = settings$distanceCrs)
  llShapes <- st_transform(shapes, 4326)
  # The historical draft exporter passed geographic coordinates through map
  # pixels before writing JSON. Retain that arithmetic for numerical lineage.
  features <- lapply(seq_along(drafts), function(index) {
    ll <- st_coordinates(llShapes[index])[, 1:2]
    latitude <- ll[, 2] * pi / 180
    pixels <- cbind((ll[, 1] + 180) / 360 * size,
      (1 - log(tan(latitude) + 1 / cos(latitude)) / pi) / 2 * size)
    degrees <- pixelDegrees(pixels)
    list(type = "Feature", properties = list(name = names(drafts)[index]),
      geometry = list(type = "LineString", coordinates = lapply(seq_len(nrow(degrees)), function(row) unname(degrees[row, ]))))
  })
  fileName <- file.path(outputDir, "draftScaffolds.geojson")
  write_json(list(type = "FeatureCollection", features = features), fileName, auto_unbox = TRUE, digits = 15)
  saved <- st_transform(st_read(fileName, quiet = TRUE), settings$distanceCrs)
  setNames(lapply(seq_len(nrow(saved)), function(index) makeFrame(st_coordinates(saved[index, ])[, 1:2])), names(drafts))
}


buildCorridors <- function(points, drafts, outputDir) {
  paths <- list()
  checks <- list()
  for (road in readObservations("corridorObservations")$routes) {
    seed <- drafts[[paste0(road$id, "_1")]]
    if (is.null(seed)) seed <- makeFrame(as.matrix(points[match(unlist(road$seed_pair), points$pointId), c("east", "north")]))
    sample <- sort(unique(c(seq(0, seed$length, by = 0.25), seed$length)))
    observation <- asMatrix(road$observations)
    boundaries <- function(station) cbind(
      right = splinefun(observation[, 1], observation[, 2], method = "monoH.FC")(station),
      left = splinefun(observation[, 1], observation[, 3], method = "monoH.FC")(station))
    edge <- boundaries(sample)
    frame <- frameAt(seed, sample, delta = 0.5, lower = 0)
    right <- frame$xy + frame$left * edge[, 1]
    left <- frame$xy + frame$left * edge[, 2]
    center <- (right + left) / 2
    for (spar in c(0.65, 0.55, 0.45, 0.35)) {
      smoothed <- cbind(predict(smooth.spline(sample, center[, 1], spar = spar), sample)$y,
                        predict(smooth.spline(sample, center[, 2], spar = spar), sample)$y)
      offset <- rowSums((smoothed - frame$xy) * frame$left)
      clearance <- pmin(offset - edge[, 1], edge[, 2] - offset)
      centerShift <- max(abs(offset - rowMeans(edge)))
      if (min(clearance) > 0.9 && centerShift < 0.3) break
    }
    stopifnot(min(clearance) > 0.9, centerShift < 0.3)
    center <- smoothed
    centerFrame <- list(xy = center, station = sample)
    checks[[road$id]] <- list(minimumClearanceM = min(clearance), smoothingSpar = spar, maximumCenterShiftM = centerShift)
    for (pair in road$pairs) {
      ids <- unlist(pair)
      indices <- which(points$pointId >= ids[1] & points$pointId <= ids[2])
      stopifnot(length(unique(points$lap[indices])) == 1)
      for (index in head(indices, -1)) {
        ends <- as.matrix(points[c(index, index + 1), c("east", "north")])
        if (sqrt(sum(diff(ends)^2)) < 1e-5) next
        station <- vapply(1:2, function(row) projectStation(centerFrame, ends[row, ]), numeric(1))
        stopifnot(diff(station) > 0)
        query <- sort(unique(c(station, sample[sample > station[1] & sample < station[2]])))
        base <- cbind(approx(sample, center[, 1], query)$y, approx(sample, center[, 2], query)$y)
        blend <- min(15, diff(station) / 2)
        weight <- function(value) { u <- pmin(1, pmax(0, value / blend)); 1 - 3*u^2 + 2*u^3 }
        xy <- base + outer(weight(query - station[1]), ends[1, ] - base[1, ]) +
          outer(weight(station[2] - query), ends[2, ] - base[nrow(base), ])
        queryFrame <- frameAt(seed, query, delta = 0.5, lower = 0)
        offset <- rowSums((xy - queryFrame$xy) * queryFrame$left)
        edge <- boundaries(query)
        margin <- pmin(offset - edge[, 1], edge[, 2] - offset)
        nearAnchor <- query < station[1] + blend | query > station[2] - blend
        stopifnot(!any(margin < -0.15 & !nearAnchor))
        paths[[as.character(index)]] <- list(xy = xy, mode = "reviewed_spatial_assumption", route = road$id,
          status = if (any(margin < -0.15)) "conflict" else "corrected")
      }
    }
  }
  list(paths = saveSpatialStage(paths, points, file.path(outputDir, "corridorPaths.geojson")), checks = checks)
}


buildManeuvers <- function(points, connectors, corridors, outputDir) {
  turns <- eventSpans(points, "turn_begin_nonint", "turn_finish_nonint")
  crossings <- eventSpans(points, "enter_intersection", "exit_intersection")
  paths <- list()
  for (site in readObservations("maneuverObservations")$sites) {
    guide <- if (site$method == "local_guide") guideFromKnots(sweep(asMatrix(site$knots), 2, unlist(site$origin), "+")) else NULL
    for (span in site$spans) {
      ids <- unlist(span)
      if (!is.null(site$expected_event)) {
        events <- if (site$expected_event == "turn") turns else crossings
        stopifnot(any(vapply(events, function(event) identical(as.integer(event), as.integer(ids)), logical(1))))
      }
      indices <- which(points$pointId >= ids[1] & points$pointId <= ids[2])
      stopifnot(length(unique(points$lap[indices])) == 1)
      for (index in head(indices, -1)) {
        ends <- as.matrix(points[c(index, index + 1), c("east", "north")])
        lengthM <- sqrt(sum(diff(ends)^2))
        if (lengthM < 1e-5) {
          stopifnot(connectors[[index]]$mode == "held_stop")
          next
        }
        if (lengthM < 3) {
          u <- seq(0, 1, length.out = 30)
          xy <- outer(1-u, ends[1, ]) + outer(u, ends[2, ])
        } else if (site$method == "local_guide") xy <- cropGuide(guide, ends) else {
          before <- corridors[[as.character(index - 1)]]$xy
          after <- corridors[[as.character(index + 1)]]$xy
          incoming <- if (isTRUE(site$entry_from_corrected)) unitVector(before[nrow(before), ] - before[nrow(before) - 1, ]) else unitVector(unlist(site$entry_direction))
          outgoing <- if (isTRUE(site$exit_from_corrected)) unitVector(after[2, ] - after[1, ]) else unitVector(unlist(site$exit_direction))
          handle <- lengthM * site$handle_fraction
          xy <- bezier(ends[1, ], ends[1, ] + handle * incoming, ends[2, ] - handle * outgoing, ends[2, ])
        }
        prior <- corridors[[as.character(index - 1)]]
        conflict <- !is.null(prior) && prior$status == "conflict"
        paths[[as.character(index)]] <- list(xy = xy, mode = "reviewed_spatial_assumption", route = site$id,
          status = if (conflict) "conflict" else "corrected")
      }
    }
  }
  saveSpatialStage(paths, points, file.path(outputDir, "maneuverPaths.geojson"))
}



buildNetwork <- function(points, paths, outputDir) {
  raw <- as.matrix(points[, c("east", "north")])
  adjusted <- raw
  owner <- rep("original", nrow(points))
  anchorStation <- rep(NA_real_, nrow(points))
  covered <- rep(FALSE, length(paths))
  # These first-lap click spans locate the imagery observations, not GPS fixes.
  spans <- list(n_st = c(12, 42), north10_urban = c(43, 54), north10_approach = c(55, 59),
    salt_turn = c(68, 75), antelope_north = c(75, 82), antelope_south = c(85, 100),
    k_st = c(100, 105), s21_return = c(105, 110))
  scaffolds <- lapply(spans, function(ids) {
    xy <- raw[points$pointId >= ids[1] & points$pointId <= ids[2], ]
    makeFrame(xy[c(TRUE, rowSums(diff(xy)^2) > 1e-8), ])
  })
  fileName <- file.path(outputDir, "networkScaffolds.json")
  write_json(scaffolds, fileName, auto_unbox = TRUE, digits = 12)
  scaffolds <- fromJSON(fileName)
  for (road in readObservations("networkObservations")$roads) {
    scaffold <- scaffolds[[road$id]]
    xy <- scaffold$xy
    seed <- list(xy = rbind(xy[1, ] - 30 * unitVector(xy[2, ] - xy[1, ]), xy,
      tail(xy, 1) + 30 * unitVector(tail(xy, 1) - xy[nrow(xy) - 1, ])),
      station = c(-30, scaffold$station, tail(scaffold$station, 1) + 30))
    knots <- asMatrix(road$center_knots)
    frame <- frameAt(seed, knots[, 1])
    guide <- makeFrame(guideFromKnots(frame$xy + frame$left * knots[, 2]))
    for (lap in 1:3) {
      ids <- unlist(road$spans[[lap]])
      indices <- which(points$pointId >= ids[1] & points$pointId <= ids[2])
      stopifnot(length(indices) > 1, all(points$lap[indices] == lap), all(owner[indices] == "original"))
      station <- vapply(indices, function(index) projectStation(guide, raw[index, ]), numeric(1))
      stopifnot(all(diff(station) >= -0.1))
      station <- cummax(station)
      changes <- lapply(road$changes[[lap]], function(change) {
        change <- unlist(change)
        rows <- match(change[1:2], points$pointId)
        stopifnot(all(rows %in% indices))
        ends <- station[match(rows, indices)]
        stopifnot(diff(ends) > 1)
        c(ends, change[3])
      })
      lateralAt <- function(query) {
        offset <- rep(road$initial_offset, length(query))
        previous <- road$initial_offset
        for (change in changes) {
          offset <- offset + (change[3] - previous) * smoothStep((query - change[1]) / (change[2] - change[1]))
          previous <- change[3]
        }
        offset
      }
      positionAt <- function(query) { frame <- frameAt(guide, query); frame$xy + frame$left * lateralAt(query) }
      adjusted[indices, ] <- positionAt(station)
      owner[indices] <- road$id
      anchorStation[indices] <- station
      for (row in seq_len(length(indices) - 1)) {
        index <- indices[row]
        query <- seq(station[row], station[row + 1], length.out = max(2, ceiling(diff(station[row:(row+1)]) / 0.2) + 1))
        moving <- diff(station[row:(row+1)]) > 1e-6
        paths[[index]] <- list(xy = positionAt(query), route = road$id,
          mode = if (moving) "persistent_lane_model" else "held_stop", status = if (moving) "corrected" else "stop")
        covered[index] <- TRUE
      }
    }
  }
  for (index in which(!covered)) {
    xy <- paths[[index]]$xy
    ends <- adjusted[c(index, index + 1), , drop = FALSE]
    shift <- ends - xy[c(1, nrow(xy)), , drop = FALSE]
    if (max(abs(shift)) < 1e-8) next
    lengthM <- tail(pathDistance(xy), 1)
    if (lengthM < 1e-5) {
      stopifnot(max(abs(diff(ends))) < 1e-5)
      paths[[index]]$xy <- ends
      next
    }
    both <- index > 1 && index < length(paths) && covered[index - 1] && covered[index + 1]
    if (both && lengthM < 45) {
      before <- paths[[index - 1]]$xy
      after <- paths[[index + 1]]$xy
      incoming <- as.numeric(unitVector(tail(before, 1) - before[nrow(before) - 1, ]))
      outgoing <- as.numeric(unitVector(after[2, ] - after[1, ]))
      handle <- sqrt(sum(diff(ends)^2)) * 0.43
      low <- apply(ends, 2, min); high <- apply(ends, 2, max)
      bound <- function(point) pmax(low, pmin(high, point))
      xy <- bezier(ends[1, ], bound(ends[1, ] + handle * incoming), bound(ends[2, ] - handle * outgoing), ends[2, ])
      paths[[index]]$status <- "corrected"
      paths[[index]]$mode <- "local_lane_join"
      paths[[index]]$route <- "lane_network_join"
    } else {
      station <- pathDistance(xy)
      taper <- min(15, lengthM / 2)
      xy <- xy + outer(1 - smoothStep(station / taper), shift[1, ]) +
        outer(1 - smoothStep((lengthM - station) / taper), shift[2, ])
      paths[[index]]$mode <- paste0(paths[[index]]$mode, "_derived_endpoint_join")
    }
    xy[c(1, nrow(xy)), ] <- ends
    paths[[index]]$xy <- xy
  }
  anchors <- data.frame(pointId = points$pointId, lap = points$lap, timeSec = points$timeSec,
    rawEast = raw[, 1], rawNorth = raw[, 2], derivedEast = adjusted[, 1], derivedNorth = adjusted[, 2],
    adjustmentM = sqrt(rowSums((adjusted - raw)^2)), model = owner, stationM = anchorStation,
    confidencePct = points$confidencePct)
  fileName <- file.path(outputDir, "networkAnchors.json")
  write_json(anchors, fileName, pretty = TRUE, digits = 12, na = "null")
  paths <- saveSpatialStage(paths, points, file.path(outputDir, "networkPaths.geojson"))
  list(paths = paths, anchors = fromJSON(fileName), scaffolds = scaffolds, covered = covered)
}



applyRightmostCorrection <- function(points, network) {
  spec <- readObservations("rightmostObservations")
  scaffold <- network$scaffolds$salt_turn
  xy <- scaffold$xy
  seed <- list(xy = rbind(xy[1, ] - 50 * unitVector(xy[2, ] - xy[1, ]), xy),
               station = c(-50, scaffold$station))
  knots <- asMatrix(spec$center_knots)
  frame <- frameAt(seed, knots[, 1])
  guide <- guideFromKnots(frame$xy + frame$left * knots[, 2])
  guideFrame <- makeFrame(guide)
  paths <- network$paths
  anchors <- network$anchors
  prior <- as.matrix(anchors[, c("derivedEast", "derivedNorth")])
  adjusted <- prior
  raw <- as.matrix(points[, c("east", "north")])
  changed <- integer()
  for (span in spec$spans) {
    ids <- unlist(span)
    indices <- which(points$pointId >= ids[1] & points$pointId <= ids[2])
    station <- vapply(indices, function(index) projectStation(guideFrame, raw[index, ]), numeric(1))
    stopifnot(all(diff(station) >= -1e-6))
    adjusted[indices, ] <- frameAt(guideFrame, station)$xy
    fixed <- indices[c(1, length(indices))]
    adjusted[fixed, ] <- prior[fixed, ]
    for (index in head(indices, -1)) {
      ends <- adjusted[c(index, index + 1), , drop = FALSE]
      stopped <- paths[[index]]$status == "stop"
      paths[[index]]$xy <- if (stopped) ends else cropGuide(guide, ends)
      paths[[index]]$route <- "salt_rightmost_v4"
      paths[[index]]$mode <- if (stopped) "held_stop" else "rightmost_lane_hold_v4"
      if (!stopped) paths[[index]]$status <- "corrected"
      changed <- c(changed, index)
    }
  }
  anchors$derivedEast <- adjusted[, 1]; anchors$derivedNorth <- adjusted[, 2]
  anchors$adjustmentM <- sqrt(rowSums((adjusted - raw)^2))
  anchors$v4ChangeM <- sqrt(rowSums((adjusted - prior)^2))
  anchors$model[anchors$v4ChangeM > 1e-6] <- "salt_rightmost_v4"
  list(paths = paths, anchors = anchors, changed = sort(unique(changed)), rightmostFrame = seed)
}
