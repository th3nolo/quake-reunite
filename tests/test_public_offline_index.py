"""Public/offline contract tests. All records are synthetic; no ingestion or network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from pipeline import build_db
from api import app as api
from pipeline.parse_photos import parse_photos
from test_html_exports import Scripts


COUNT = 205  # Exceeds both the HTML display cap (80) and /persons response cap (200).


def embedded_people(page):
    payload = page.split("const PEOPLE = ", 1)[1]
    return json.JSONDecoder().raw_decode(payload)[0]


class PublicOfflineIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for obj, name, value in (
            (build_db.db, "DB_PATH", str(self.root / "synthetic.db")),
            (build_db, "OUT", self.root),
            (build_db.legacy, "OUT", self.root),
            (api, "WEB_DIR", self.root),
            (api, "AUDIT_LOG", self.root / "audit.log"),
            (api, "RATE_PER_MIN", 1),
            (api, "_hits", api.defaultdict(api.deque)),
            (api, "_ZONES", None),
        ):
            self.enterContext(patch.object(obj, name, value))
        self.client = self.enterContext(TestClient(api.app))

    def seed_database(self):
        conn = build_db.db.connect()
        try:
            for index in range(COUNT):
                conn.execute(
                    """INSERT INTO persons
                       (name_key, display_name, cis, ages, sex, origins, hospitals,
                        statuses, n_records) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (f"synthetic person {index}", f"Synthetic Person {index}",
                     json.dumps([f"TEST-CI-{index}"]), '["42"]', "F",
                     '["Synthetic Origin"]', '["Synthetic Hospital"]',
                     '["ingresado"]', 1),
                )
            conn.commit()
        finally:
            conn.close()

    def assert_complete_index(self, people):
        self.assertEqual(len(people), COUNT)
        self.assertEqual({p["n"] for p in people},
                         {f"Synthetic Person {i}" for i in range(COUNT)})
        self.assertEqual({ci for p in people for ci in p["ci"]},
                         {f"TEST-CI-{i}" for i in range(COUNT)})
        for person in people:
            self.assertEqual(person["st"], ["ingresado"])
            self.assertEqual(person["hosp"], ["Synthetic Hospital"])
            self.assertEqual(person["org"], ["Synthetic Origin"])

    def test_database_download_is_complete_before_next_request_is_throttled(self):
        self.seed_database()
        self.assertEqual(build_db.export()["people"], COUNT)
        # Fixed time prevents a slow runner from crossing the limiter's window.
        with patch.object(api.time, "time", return_value=1000):
            response = self.client.get("/buscador")
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/html", response.headers["content-type"])
            self.assert_complete_index(embedded_people(response.text))
            self.assertEqual(response.content,
                             (self.root / "buscador.html").read_bytes())
            blocked = self.client.get("/buscador")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"], "rate_limited")
        # A saved copy remains complete without a further API request.
        self.assert_complete_index(embedded_people(response.text))
        audit = (self.root / "audit.log").read_text()
        self.assertEqual(len(audit.splitlines()), 1)
        self.assertIn("\tGET\t/buscador?", audit)

    def test_throttle_window_expires_without_restricting_download_size(self):
        self.seed_database()
        build_db.export()
        with patch.object(api.time, "time", return_value=1000):
            self.assertEqual(self.client.get("/buscador").status_code, 200)
            self.assertEqual(self.client.get("/buscador").status_code, 429)
        with patch.object(api.time, "time", return_value=1061):
            response = self.client.get("/buscador")
        self.assertEqual(response.status_code, 200)
        self.assert_complete_index(embedded_people(response.text))

    def test_missing_snapshot_returns_503_without_database_fallback(self):
        self.seed_database()
        response = self.client.get("/buscador")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Synthetic Person", response.text)

    def test_legacy_export_also_embeds_every_supplied_person(self):
        people = [dict(
            person_id=f"P{i:05d}", display_name=f"Synthetic Person {i}",
            display_name_key=f"synthetic person {i}", all_ci=[f"TEST-CI-{i}"],
            age="42", sex="F", origins=["Synthetic Origin"],
            hospitals=["Synthetic Hospital"], statuses=["ingresado"],
            deceased=False, in_multiple_hospitals=False, ci_conflict=False,
            possible_same=[], appearances=[],
        ) for i in range(COUNT)]
        build_db.legacy.write_html(people, [])
        page = (self.root / "buscador.html").read_text(encoding="utf-8")
        self.assert_complete_index(embedded_people(page))

    def test_synthetic_import_resolves_exports_and_serves_hostile_value_as_data(self):
        hostile = '</script><script id="synthetic-marker">alert(1)</script>'
        source = self.root / "synthetic-ocr.json"
        source.write_text(json.dumps({"images": [{
            "file": "synthetic-2026-01-01.png", "hospital": hostile,
            "rows": [
                {"apellidos": "SYNTHETIC", "nombres": "FIXTURE",
                 "ci": ci, "edad": "42", "estado": "ingresado"}
                for ci in ("99900001", "88800002")
            ],
        }]}), encoding="utf-8")
        records = parse_photos(source)
        self.assertEqual(len(records), 2)
        conn = build_db.db.connect()
        try:
            with conn:
                ids = [build_db.db.add_record(conn, row) for row in records]
                repeated = [build_db.db.add_record(conn, row) for row in records]
            # Same names with different identifiers stay separate; retries add no rows.
            self.assertNotEqual(ids[0], ids[1])
            self.assertEqual(repeated, ids)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
            api._geo.ensure_zones(conn)
            api._hh.rebuild(conn)
        finally:
            conn.close()

        result = build_db.export()
        self.assertEqual((result["people"], result["records"]), (2, 2))
        with patch.object(api, "RATE_PER_MIN", 20):
            response = self.client.get("/buscador")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(Scripts(response.text).scripts), 1)
            people = embedded_people(response.text)
            self.assertEqual({ci for p in people for ci in p["ci"]},
                             {"99900001", "88800002"})
            for person in people:
                self.assertEqual(person["hosp"], [hostile])
                self.assertEqual(person["ap"][0]["h"], hostile)
                self.assertEqual(person["st"], ["ingresado"])
            listing = self.client.get("/persons", params={"name": "synthetic"})
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.json()["count"], 2)
            detail = self.client.get("/persons/99900001")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["hospitals"], [hostile])
            self.assertEqual(detail.json()["n_records"], 1)
            self.assertEqual(self.client.get("/persons/77700003").status_code, 404)
            self.assertEqual(self.client.get("/persons?limit=201").status_code, 422)

    def test_empty_database_exports_a_searchable_empty_snapshot(self):
        build_db.db.connect().close()
        result = build_db.export()
        self.assertEqual((result["people"], result["records"]), (0, 0))
        response = self.client.get("/buscador")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(embedded_people(response.text), [])
        self.assertEqual(len(Scripts(response.text).scripts), 1)
        self.assertIn("function run()", response.text)


if __name__ == "__main__":
    unittest.main()
