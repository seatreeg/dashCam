source("helpers.R")

# CAMERA READINGS: one row for every original GPS observation.
cameraData <- list()
for (camera in names(settings$samplePeriodSec)) {
  original <- readCsv(paste0("Data Raw/Dashcam Extract/", camera, ".csv"))
  stopifnot(all(original$speedUnits %in% c("mph", "km/h", "knots")))
  timeSec <- gpsSeconds(original$timeUtc) - settings$cameraClockFastSec[[camera]]
  metresPerSecond <- c("mph" = 0.44704, "km/h" = 1 / 3.6, "knots" = 1852 / 3600)
  speedMps <- original$speed * unname(metresPerSecond[original$speedUnits])
  fix <- as.integer(original$fixStatus %in% c("fix", "active", "2D", "3D"))
  stopifnot(all(is.finite(timeSec)), !anyDuplicated(timeSec))

  cameraData[[camera]] <- data.frame(
    camera, readingIndex = as.integer(rank(timeSec)),
    clip = original$clip, sampleIndex = original$sampleIndex,
    timeSec, latitude = original$latitude, longitude = original$longitude, speedMps, fix)
}
dashcams <- do.call(rbind, cameraData)
dashcams <- dashcams[order(dashcams$timeSec, dashcams$camera), ]
writeCsv(dashcams, "Data/dashcams.csv")

# VEHICLE READINGS: keep the original logging rate for the odometer and wires.
# The Pi clock was wrong. Apply the documented clock mapping exactly once.
original <- readCsv("Data Raw/pi/car_telemetry.csv")
rawTime <- as.POSIXct(original$Computer_DateTime, format = "%Y-%m-%d %H:%M:%OS", tz = "UTC")
piAnchor <- as.POSIXct(settings$piClockAnchor, tz = "UTC")
keep <- rawTime >= piAnchor
original <- original[keep, ]
timeSec <- as.numeric(difftime(rawTime[keep], piAnchor, units = "secs")) + settings$piCorrectionSec
vehicle <- data.frame(
  timeSec, speedMps = original$Speed_MPH * 0.44704,
  rpm = original$RPM, throttlePct = original$Throttle_Pct,
  yellowWire = original$Yellow_Wire, greenWire = original$Green_Wire,
  obdConnected = original$OBD_Connected, arduinoConnected = original$Arduino_Connected)
vehicle <- vehicle[order(vehicle$timeSec), ]
stopifnot(all(is.finite(vehicle$timeSec)), all(diff(vehicle$timeSec) > 0))
writeCsv(vehicle, "Data/vehicle.csv")

cat(nrow(dashcams), "original GPS readings;", nrow(vehicle), "vehicle log rows.\n")
