"""Synthetic-only regression tests; run with python -m unittest discover -s tests."""
import json
import sqlite3
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import build
import build_db


PAYLOADS = (
    '</script><script id="review-marker">alert(1)</script>',
    '</ScRiPt ><ScRiPt id="review-marker">alert(1)</sCrIpT>',
    '<!--<script> & < > -->',
    'Espa\u00f1ol \u4e2d\u6587 \U0001f30e \u2028 \u2029 " \\ \n',
    '{{N_PEOPLE}} {{N_RECORDS}} {{N_MULTI}} {{N_HOSP}} /*DATA*/',
)


class Scripts(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.scripts = []
        self.active = False
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append("")
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.scripts[-1] += data


class ExportTests(unittest.TestCase):
    def assert_page(self, path, payload):
        scripts = Scripts(path.read_text(encoding="utf-8")).scripts
        self.assertEqual(len(scripts), 1)
        prefix = "const PEOPLE = "
        data, end = json.JSONDecoder().raw_decode(scripts[0].split(prefix, 1)[1])
        literal = scripts[0].split(prefix, 1)[1][:end]
        for character in '<>&\u2028\u2029':
            self.assertNotIn(character, literal)
        self.assertEqual(data[0]["n"], payload)
        self.assertEqual(data[0]["org"], [payload])
        self.assertEqual(data[0]["ap"][0]["s"], payload)
        self.assertIn('function run()', scripts[0])

    def test_legacy_export(self):
        for payload in PAYLOADS:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                person = dict(
                    person_id="P00001", display_name=payload, display_name_key=payload,
                    all_ci=[], age="", sex="", origins=[payload], hospitals=["Synthetic"],
                    statuses=[], deceased=False, in_multiple_hospitals=False,
                    ci_conflict=False, possible_same=[], appearances=[dict(
                        hospital="Synthetic", source=payload, status="", ci="", age="",
                        origin=payload, obs=payload, date="")],
                )
                with patch.object(build, "OUT", Path(tmp)):
                    build.write_html([person], [{}])
                self.assert_page(Path(tmp) / "buscador.html", payload)

    def test_database_export(self):
        for payload in PAYLOADS:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "synthetic.db"
                conn = sqlite3.connect(database)
                try:
                    conn.executescript(build_db.db.SCHEMA)
                    conn.execute(
                        "INSERT INTO persons (person_id, display_name, name_key, origins, "
                        "hospitals, n_records) VALUES (1, ?, ?, ?, ?, 1)",
                        (payload, payload, json.dumps([payload]), '["Synthetic"]'),
                    )
                    conn.execute(
                        "INSERT INTO records (person_id, source, origin) VALUES (1, ?, ?)",
                        (payload, payload),
                    )
                    conn.commit()
                finally:
                    conn.close()
                with patch.object(build_db.db, "DB_PATH", str(database)), \
                     patch.object(build_db, "OUT", Path(tmp)):
                    result = build_db.export()
                self.assertEqual(result["people"], 1)
                self.assertEqual(result["records"], 1)
                self.assert_page(Path(tmp) / "buscador.html", payload)


if __name__ == "__main__":
    unittest.main()
