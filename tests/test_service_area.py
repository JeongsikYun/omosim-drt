from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from drt_pipeline.service_area import ServiceArea


class ServiceAreaTests(unittest.TestCase):
    def setUp(self):
        self.korea=json.loads((ROOT/"configs/service_area.korea.json").read_text())
        self.german=json.loads((ROOT/"examples/synthetic_germany/service_area.json").read_text())

    def test_configured_korea_matches_unchanged_reference_mapping(self):
        rng=random.Random(1729)
        points=[(rng.uniform(36.1,36.8),rng.uniform(127.0,127.65)) for _ in range(3000)]
        points += [(36.1831,127.2466),(36.5,127.5597),(36.368,127.065),(36.737,127.4081)]
        env=dict(os.environ);env.pop("DRT_SERVICE_AREA_CONFIG",None)
        query="import json,sys;from drt_pipeline.regions import map_to_region;print(json.dumps([map_to_region(*p).value for p in json.load(sys.stdin)]))"
        result=subprocess.run([sys.executable,"-c",query],input=json.dumps(points),text=True,capture_output=True,check=True,cwd=ROOT,env=env)
        expected=json.loads(result.stdout)
        configured=ServiceArea(self.korea)
        self.assertEqual(expected,[configured.map(*p) for p in points])

    def test_german_coordinates_and_outside(self):
        area=ServiceArea(self.german)
        for region,point in area.centers.items():
            self.assertEqual(region,area.map(*point))
        self.assertEqual("OUTSIDE",area.map(36.3,127.3))

    def test_reject_invalid_geometry(self):
        cases=[]
        data=copy.deepcopy(self.german);data["centers"]["DJ_SW"]=[None,None];cases.append(data)
        data=copy.deepcopy(self.german);data["centers"].pop("SJ_N");cases.append(data)
        data=copy.deepcopy(self.german);data["groups"][0]["bounds"]=[50.1,8.5,50.0,8.6];cases.append(data)
        data=copy.deepcopy(self.german);data["groups"][0]["regions"].append("DJ_SW");cases.append(data)
        data=copy.deepcopy(self.german);data["coordinate_system"]="EPSG:3857";cases.append(data)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):ServiceArea(case)

    def test_preflight_runs_with_local_configuration(self):
        command=[sys.executable,"scripts/check_input.py","--input","examples/synthetic_germany/input.json",
                 "--service-area","examples/synthetic_germany/service_area.json"]
        result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,check=True)
        counts=json.loads(result.stdout)
        self.assertEqual(1,counts["excluded_car_passenger_agents"])
        self.assertEqual(2,counts["requests_inside_service_hours"])
        self.assertEqual(36,counts["directed_cache_pairs_including_diagonal"])

    def test_template_cannot_be_used_without_editing(self):
        with self.assertRaises(ValueError):
            ServiceArea(json.loads((ROOT/"configs/service_area.germany.template.json").read_text()))


if __name__=="__main__":unittest.main()
