source("helpers.R")

# KEEP THE ORIGINAL SAVE UNCHANGED. Make a smaller, reloadable working copy.
original <- readCsv("pipeline/03_annotate/original_annotation/anno 8 11 2026.csv")
annotations <- data.frame(
  pointId = original$point_idx, timeSec = original$video_time_sec,
  lap = original$trip, latitude = original$click_lat, longitude = original$click_lon,
  confidencePct = original$confidence_pct, notes = original$notes,
  triggers = original$triggers, customTrigger = original$custom_trigger,
  locationHeld = original$location_held == "yes", stopGroup = original$stop_group)
stopifnot(nrow(annotations) == 375, all(diff(annotations$timeSec) > 0),
          !anyDuplicated(annotations$pointId))
if (file.exists("pipeline/03_annotate/annotations.csv")) {
  stop("annotations.csv already exists. Conversion will not overwrite an annotation save.")
}
writeCsv(annotations, "pipeline/03_annotate/annotations.csv")
cat("Converted", nrow(annotations), "annotations; original file unchanged.\n")
