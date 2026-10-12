Note, we created a single readme that documents the process under README_MAIN.qmd

This file will just be for immediate goals.

MEETING TOPIC
While previously I had to rely heavily on computer vision and satalite imagry from Ersi, I found two datasets that describe the lanes in great detail:

- Lincoln LTU Pavement Markings (https://gis.lincoln.ne.gov/public/rest/services/LTUTraffic/Markings/MapServer/0)—coordinates tracing painted lines, including dividers, turn guides, and other markings.
- Lincoln LTU Pavement Curblines (https://gis.lincoln.ne.gov/public/rest/services/LTUStreetMaintenance/PavementCurblines/MapServer/0)—coordinates tracing curb/road edges

The depth of these datasets are impressive despite being from lincoln and not one of those big Waymo-level cities.
What's left is to readjust the code to use these. 
Important note: having the lanes still requires us to center the car on the lanes. There are also some regions that are completely unmarked, like parking lots.
