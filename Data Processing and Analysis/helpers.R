suppressPackageStartupMessages(library(jsonlite))
settings <- fromJSON("settings.json")
driveStart <- as.POSIXct(settings$driveStartUtc, format = "%Y-%m-%dT%H:%M:%OS", tz = "UTC")

readCsv <- function(fileName) {
  read.csv(fileName, na.strings = c("", "NA"), stringsAsFactors = FALSE)
}

writeCsv <- function(data, fileName) {
  dir.create(dirname(fileName), recursive = TRUE, showWarnings = FALSE)
  write.csv(data, fileName, row.names = FALSE, na = "")
}

gpsSeconds <- function(timeUtc) {
  parsedTime <- as.POSIXct(timeUtc, format = "%Y-%m-%dT%H:%M:%OS", tz = "UTC")
  as.numeric(difftime(parsedTime, driveStart, units = "secs"))
}
