# BUILD SAFETY, separate from the short scientific calculation.
# Paths/hashes are project-relative; no old-project paths or raw MP4s are needed


buildInputFiles <- function(root = ".") {
  fixed <- c("helpers.R", "buildChecks.R", "runAnalysis.R", "pipeline/02_extract/combineTelemetry.r",
    "pipeline/03_annotate/convertAnnotations.R", "Data/Data Annotate/annotations.csv",
    "Data/Data Annotate/original_annotation/anno 8 11 2026.csv", "Data/Data Raw/pi/car_telemetry.csv",
    paste0("Data/Data/", c("garmin", "gopro", "redtiger", "rove"), ".csv"),
    paste0("pipeline/04_lane/", c("buildLaneGeometry.R", "geometryMath.R", "geometryStages.R", "laneModel.R", "interpolateCar.R")))
  sort(c(fixed,
    paste0("pipeline/04_lane/observations/", list.files(file.path(root, "pipeline/04_lane/observations"), "[.]json$")),
    paste0("pipeline/04_lane/inputs/", list.files(file.path(root, "pipeline/04_lane/inputs"), "^elevation.*[.]json$"))))
}

buildOutputFiles <- function(root = ".") {
  sort(c("Data/Data/dashcams.csv", "Data/Data/vehicle.csv",
    "Data/Data/gpsErrors.csv", "Data/Data/carPath.csv", "Data/Data/referenceIntervals.csv",
    paste0("Data/Data/laneGeometry/", list.files(file.path(root, "Data/Data/laneGeometry"), "[.](json|geojson)$"))))
}

fileHashes <- function(files, root = ".") {
  setNames(vapply(files, function(file) digest::digest(file = file.path(root, file), algo = "sha256"), ""), files)
}


dataSettingsHash <- function(root = ".") {
  config <- jsonlite::fromJSON(file.path(root, "settings.json"))
  # plot settings and the inclusion switch do not change the modeled data
  config <- config[sort(setdiff(names(config), c("maximumLagSec", "preserveV4Exclusions")))]
  digest::digest(jsonlite::toJSON(config, auto_unbox = TRUE, digits = NA), algo = "sha256", serialize = FALSE)
}



replaceBuildFile <- function(source, target) {
  dir.create(dirname(target), recursive = TRUE, showWarnings = FALSE)
  temporary <- tempfile(".build-", tmpdir = dirname(target))
  on.exit(if (file.exists(temporary)) unlink(temporary))
  if (!file.copy(source, temporary) || !file.rename(temporary, target))
    stop("Could not replace output: ", target, ". Close programs holding that file and rerun.")
}

writeBuildRecord <- function(record, root = ".") {
  directory <- file.path(root, "audit")
  dir.create(directory, recursive = TRUE, showWarnings = FALSE)
  temporary <- tempfile(".manifest-", tmpdir = directory)
  on.exit(if (file.exists(temporary)) unlink(temporary))
  jsonlite::write_json(record, temporary, pretty = TRUE, auto_unbox = TRUE)
  if (!file.rename(temporary, file.path(directory, "analysisBuild.json")))
    stop("Could not update the analysis build record.")
}

assertCurrentBuild <- function(root = ".") {
  error <- tryCatch({
    record <- jsonlite::fromJSON(file.path(root, "audit/analysisBuild.json"), simplifyVector = FALSE)
    stopifnot(identical(record$status, "complete"), record$version == 1,
      identical(record$settingsHash, dataSettingsHash(root)),
      identical(names(record$inputs), buildInputFiles(root)),
      identical(names(record$outputs), buildOutputFiles(root)),
      identical(unlist(record$inputs), fileHashes(names(record$inputs), root)),
      identical(unlist(record$outputs), fileHashes(names(record$outputs), root)))
    NULL
  }, error = function(error) conditionMessage(error))
  if (!is.null(error)) stop("Analysis inputs are stale, incomplete, or unverified. Run Rscript runAnalysis.R from the project root.\n", error, call. = FALSE)
  invisible(TRUE)
}


runVerifiedBuild <- function() {
  root <- normalizePath(".", winslash = "/", mustWork = TRUE)
  # An incomplete/failed attempt must not leave an earlier 'complete' record active.
  writeBuildRecord(list(version = 1, status = "building"), root)
  complete <- FALSE
  on.exit({
    setwd(root)
    if (!complete) writeBuildRecord(list(version = 1, status = "failed",
      note = "No analysis is authorized until a successful rerun."), root)
  })
  if (!file.exists("Data/Data Annotate/annotations.csv")) source("pipeline/03_annotate/convertAnnotations.R")
  source("pipeline/04_lane/laneModel.R")
  checkReviewedAnnotations()
  inputs <- fileHashes(buildInputFiles(root), root)
  settingsHash <- dataSettingsHash(root)

  # calculate in an isolated tree. Failed calculations do not touch published CSVs
  directory <- file.path(root, "audit/tmp")
  dir.create(directory, recursive = TRUE, showWarnings = FALSE)
  stage <- tempfile("analysisBuild-", tmpdir = directory)
  dir.create(stage)
  for (file in c(names(inputs), "settings.json")) {
    target <- file.path(stage, file)
    dir.create(dirname(target), recursive = TRUE, showWarnings = FALSE)
    if (!file.copy(file.path(root, file), target)) stop("Could not stage input: ", file)
  }
  stopifnot(identical(inputs, fileHashes(names(inputs), stage)), settingsHash == dataSettingsHash(stage))
  setwd(stage)
  source("pipeline/02_extract/combineTelemetry.r")
  source("pipeline/04_lane/interpolateCar.R")
  interpolateCar()
  outputs <- fileHashes(buildOutputFiles(stage), stage)
  setwd(root)
  # detect an input edited while the calculation was running
  stopifnot(identical(names(inputs), buildInputFiles(root)),
    identical(inputs, fileHashes(names(inputs), root)), settingsHash == dataSettingsHash(root))
  for (file in names(outputs)) replaceBuildFile(file.path(stage, file), file.path(root, file))
  stopifnot(identical(outputs, fileHashes(names(outputs), root)))
  writeBuildRecord(list(version = 1, status = "complete", settingsHash = settingsHash,
    inputs = as.list(inputs), outputs = as.list(outputs)), root)
  assertCurrentBuild(root)
  complete <- TRUE
  cat("Verified data are ready. Render pipeline/05_analysis/autocorrelationAnalysis.qmd with Quarto.\n")
}
