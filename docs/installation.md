# Installation

## Python

The checked environment is Linux with Python 3.12.3. The simulation and report calculations use the standard library; the GraphHopper cache builder also uses `aiohttp`. The small requirements file intentionally omits plotting, presentation and unrelated server packages.

From the repository root:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/test_choice_rules.py
python scripts/test_travel_cache.py
python -m unittest discover -s tests
python scripts/smoke_test.py
```

The tests write temporary files or ignored `cache/` data. They do not require research inputs. The smoke test runs a real small process pool; an execution sandbox that blocks process creation will need a local terminal outside that restriction. For Windows, run these Linux commands inside WSL; the commands are not PowerShell syntax.

## GraphHopper for real experiments

The reference pipeline uses GraphHopper 8.0 with a profile named `car`. Obtain the GraphHopper JAR separately and place it in `tools/graphhopper-web-8.0.jar`. Place the relevant German OSM extract at `data/germany.osm.pbf`. These files are not committed to this repository.

The reference GraphHopper deployment supports Java 17; the original server also runs this JAR on Java 21. This is separate from running OMOSIM 2.3.13, which requires Java 21. If you already have OMOSIM outputs, no OMOSIM process or JAR is needed by this pipeline.

Use one terminal for GraphHopper:

```bash
java -version
java -Xms2g -Xmx8g \
  -Ddw.graphhopper.datareader.file=data/germany.osm.pbf \
  -jar tools/graphhopper-web-8.0.jar server configs/graphhopper.yml
```

This starts the routing server in the foreground and creates the graph under `data/graphhopper-graph/`. Keep this terminal open while building OD caches. The heap limits are example starting values; the appropriate memory depends on the selected extract.

In a second terminal, confirm that the server is listening:

```bash
curl -I http://localhost:8989
```

An HTTP response only checks server availability. Before a full cache build, also test `/route` with two reachable coordinates from the actual study area and confirm positive distance/time. Read [German setup](germany_setup.md) for graph coverage and cache identity.

The example config binds to localhost. If another GraphHopper instance already uses port 8989, change both the HTTP port in the config and `--gh-url`. Use distinct graph folders and OD database files for different road networks/profiles.

Reference: [GraphHopper 8.0 example configuration](https://github.com/graphhopper/graphhopper/blob/8.0/config-example.yml). Pin the release and configuration; do not silently substitute another routing version for a replication experiment.
