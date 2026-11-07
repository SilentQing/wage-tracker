import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


class LedgerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory()
        previous = Path.cwd()
        try:
            os.chdir(cls.workspace.name)
            spec = importlib.util.spec_from_file_location("wage_app", ROOT / "app.py")
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
        finally:
            os.chdir(previous)

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def setUp(self):
        self.database = tempfile.TemporaryDirectory()
        self.module.DB_PATH = str(Path(self.database.name) / "wage.db")
        self.module.init_db()
        self.client = TestClient(self.module.app)
        self.addCleanup(self.database.cleanup)
        self.addCleanup(self.client.close)

    def attendance(self, month="2024-02", count=0, hours=2.5, subsidy=10):
        response = self.client.post("/api/attendance", json={
            "month": month, "work_days": 20, "daily_regular_hours": 8,
            "overtime_count": count, "hours_per_ot": hours,
            "subsidy_per_ot": subsidy,
        })
        self.assertEqual(response.status_code, 200)

    def mark(self, day, checked=True, replace=False):
        return self.client.put("/api/overtime", json={
            "work_date": day, "checked": checked, "replace_undated": replace,
        })

    def data(self, month="2024-02"):
        response = self.client.get("/api/data", params={"month": month})
        self.assertEqual(response.status_code, 200)
        return response.json()


class OvertimeTests(LedgerTestCase):
    def test_backfill_idempotence_and_cancellation(self):
        for _ in range(2):
            self.assertEqual(self.mark("2024-02-29").status_code, 200)
        summary = self.data()["overtime_summary"]
        self.assertEqual(summary, {
            "dated_count": 1, "undated_count": 0, "count": 1,
            "hours": 2.5, "subsidy": 10,
        })
        for _ in range(2):
            self.assertEqual(self.mark("2024-02-29", False).status_code, 200)
        self.assertEqual(self.data()["overtime_summary"]["count"], 0)

    def test_legacy_conversion_is_atomic_and_retry_safe(self):
        self.attendance(count=2)
        for _ in range(2):
            self.assertEqual(self.mark("2024-02-28", replace=True).status_code, 200)
        self.assertEqual(self.data()["overtime_summary"]["count"], 2)
        self.assertEqual(self.data()["attendance"]["overtime_count"], 1)
        self.assertEqual(self.mark("2024-02-29", replace=True).status_code, 200)
        self.assertEqual(self.mark("2024-02-27", replace=True).status_code, 409)
        self.assertEqual(len(self.data()["overtime_days"]), 2)
        self.assertEqual(self.data()["overtime_summary"]["count"], 2)

    def test_cross_month_week_and_month_rates(self):
        self.attendance("2024-01", hours=3, subsidy=20)
        self.attendance("2024-02", hours=2, subsidy=5)
        self.mark("2024-01-31")
        self.mark("2024-02-01")
        february = self.data()
        self.assertEqual(len(february["overtime_days"]), 2)
        self.assertEqual(sum(day["hours"] for day in february["overtime_days"]), 5)
        self.assertEqual(february["overtime_summary"]["hours"], 2)
        self.assertEqual(february["overtime_summary"]["subsidy"], 5)
        self.assertEqual(self.data("ALL")["overtime_summary"]["hours"], 5)
        self.assertEqual(self.data("ALL")["overtime_summary"]["subsidy"], 25)
        self.assertIn("2024-01", february["months"])

    def test_zero_rates_and_settings_edits_preserve_dates(self):
        self.mark("2024-02-29")
        self.attendance(hours=0, subsidy=0)
        summary = self.data()["overtime_summary"]
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["hours"], 0)
        self.assertEqual(summary["subsidy"], 0)

    def test_invalid_dates_future_dates_and_months(self):
        for day in ["2024-02-30", "not-a-date"]:
            self.assertEqual(self.mark(day).status_code, 422)
        future = (date.today() + timedelta(days=2)).isoformat()
        self.assertEqual(self.mark(future).status_code, 400)
        for month in ["2024-13", "2024-2", "garbage"]:
            self.assertEqual(self.client.get("/api/data", params={"month": month}).status_code, 400)

    def test_concurrent_duplicate_conversion(self):
        self.attendance(count=2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.mark("2024-02-29", replace=True), range(4)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(self.data()["attendance"]["overtime_count"], 1)
        self.assertEqual(self.data()["overtime_summary"]["count"], 2)

    def test_upgrade_old_schema_and_reopen(self):
        with closing(sqlite3.connect(self.module.DB_PATH)) as conn:
            conn.execute("DROP TABLE attendance")
            conn.execute("DROP TABLE overtime_days")
            conn.execute("""CREATE TABLE attendance (
                month TEXT PRIMARY KEY, work_days REAL, daily_regular_hours REAL,
                overtime_count INTEGER, subsidy_per_ot REAL
            )""")
            conn.execute("INSERT INTO attendance VALUES ('2024-02', 20, 8, 3, 10)")
            conn.commit()
        self.module.init_db()
        self.assertEqual(self.data()["overtime_summary"]["hours"], 7.5)
        self.mark("2024-02-29", replace=True)
        self.module.init_db()
        self.assertEqual(self.data()["overtime_summary"]["count"], 3)
        self.assertEqual(self.data()["overtime_summary"]["dated_count"], 1)

    def test_unused_legacy_part_is_removed_on_upgrade(self):
        self.assertNotIn("庆祝", [part["name"] for part in self.data()["part_types"]])
        with closing(sqlite3.connect(self.module.DB_PATH)) as conn:
            conn.execute("INSERT INTO part_types VALUES ('庆祝', 0)")
            conn.commit()
        self.module.init_db()
        self.assertNotIn("庆祝", [part["name"] for part in self.data()["part_types"]])

    def test_legacy_part_with_records_keeps_history_and_price(self):
        self.client.post("/api/records", json={
            "record_date": "2024-02-29", "part_name": "庆祝", "quantity": 3,
        })
        self.client.post("/api/price", json={"part_name": "庆祝", "unit_price": 12.5})
        self.module.init_db()
        data = self.data()
        self.assertEqual(data["records"][0]["part_name"], "庆祝")
        self.assertEqual(data["records"][0]["quantity"], 3)
        part = next(part for part in data["part_types"] if part["name"] == "庆祝")
        self.assertEqual(part["unit_price"], 12.5)


if __name__ == "__main__":
    unittest.main()
