interpolateCar <- function() {
  source("pipeline/04_lane/laneModel.R")
  checkReviewedAnnotations()
  source("pipeline/04_lane/buildLaneGeometry.R")
  buildLaneGeometry()

  model <- buildLaneModel()
  gps <- readCsv("pipeline/02_extract/combined/dashcams.csv")
  reference <- referenceAt(model, gps$timeSec)
  referenceDegrees <- toDegrees(reference$east, reference$north)
  gpsMeters <- toMeters(gps$longitude, gps$latitude)

  # ERROR POINTS FROM THE MODELED CAR TO THE CAMERA GPS POSITION.
  eastError <- gpsMeters[, 1] - reference$east
  northError <- gpsMeters[, 2] - reference$north
  distanceM <- sqrt(eastError^2 + northError^2)
  directionRad <- atan2(northError, eastError)
  distanceM[gps$fix != 1] <- NA
  directionRad[gps$fix != 1 | (!is.na(distanceM) & distanceM == 0)] <- NA
  errors <- data.frame(gps, segment = reference$segment,
    referenceLatitude = referenceDegrees[, 2], referenceLongitude = referenceDegrees[, 1],
    distanceM, directionRad)
  writeCsv(errors, "pipeline/04_lane/gpsErrors.csv")

  # QUALITY DECISIONS ARE SEPARATE; DO NOT DELETE ROWS OR COMPRESS TIME.
  writeCsv(model$intervals, "audit/referenceIntervals.csv")
  endTime <- tail(model$annotationTimes, 1)
  reference <- referenceAt(model, sort(unique(c(seq(0, endTime, by = 0.1), endTime))))
  coordinates <- toDegrees(reference$east, reference$north)
  car <- data.frame(timeSec = reference$timeSec, segment = reference$segment,
                    latitude = coordinates[, 2], longitude = coordinates[, 1])
  writeCsv(car, "pipeline/04_lane/carPath.csv")
  cat(nrow(errors), "GPS rows;", nrow(car), "display-path rows;",
      sum(model$intervals$referenceOk), "of", nrow(model$intervals), "supported intervals.\n")
}

# Direct runs use the same staged, verified build as runAnalysis.R.
if (sys.nframe() == 0) {
  source("buildChecks.R")
  runVerifiedBuild()
}
