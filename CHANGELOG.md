# Preparation release — 2026-11-06

- Curated the currently used Runtime-v2 working-tree sources into an independent repository snapshot.
- Added an English installation/usage/methodology handoff for local German experiments.
- Added optional service-area JSON handling in the release copy's `regions.py` and approximate-distance latitude in `travel.py`.
- Added a launcher that freezes the area, records effective configuration and always enables the Runtime-v2 features and strict OD cache.
- Added preflight validation, synthetic German-coordinate integration input and a four-table report command using existing KPI calculation functions.
- Included the portable pre-DRT analyzer and synthetic Baseline/WFH/SO/WFH+SO examples.

The production workspace was not edited. Core ALNS, fleet allocation, Stage0 and Stage1/2 choice algorithms were copied without algorithm changes. Geographic configuration and release tooling require their own validation and are not asserted to reproduce any particular German field service.
