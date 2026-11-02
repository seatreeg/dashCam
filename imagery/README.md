# cached imagery

`tiles/` is a verified copy of the existing project's Esri World Imagery
cache. The files use `{zoom}/{x}/{y}.png` names; many contain JPEG bytes despite
that historical filename extension. Leaflet displays the cached image bytes.
No fresh tile downloads are required for the saved coverage.

Original service:
https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer

This is Esri World Imagery, not OpenStreetMap. Leaflet is the display library,
not the imagery provider. The earlier September 14 source check associated the
high-resolution route imagery with Lancaster County 2024 / EagleView / Spring
2024 NIROC, a recorded source date of March 4, 2024, and approximately 0.1 m
source pixels. Seven cached high-resolution tiles matched the then-current
service bytes. This was a spot-check, not proof of the capture date of all
tiles at every zoom, nor a claim of 10 cm positional accuracy.

Esri explains how to inspect source metadata here:
https://support.esri.com/en-us/knowledge-base/faq-can-the-date-of-an-image-be-determined-from-the-wor-000012181

Note that I have not worked out the redistribution details on this. We can add these to the private repo, but we should avoid publishing the map details on the final project. Thankfully, the map tiles are only needed to create the annotation, not for the final output, which should just be a csv and analysis. For backup purposes, I will upload this to the repo, but we must remember that the WIP repo should not be the published repo, as we risk legal violations.