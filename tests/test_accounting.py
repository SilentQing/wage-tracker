import copy
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import patch

from test_overtime import LedgerTestCase


class AccountingTests(LedgerTestCase):
    def price(self, name="轴套", value=1):
        return self.client.post("/api/price", json={"part_name": name, "unit_price": value})

    def record(self, day="2024-02-29", name="轴套", quantity=100, **extra):
        return self.client.post("/api/records", json={
            "record_date": day, "part_name": name, "quantity": quantity, **extra,
        })

    def test_price_changes_preserve_priced_history_and_fill_pending(self):
        self.price(value=1)
        self.record()
        self.price(value=2)
        self.record()
        self.assertEqual(self.data()["totals"]["piece_wage"], 300)
        self.record(name="待报价")
        self.price("待报价", 3)
        self.price("待报价", 4)
        self.assertEqual(self.data()["totals"]["piece_wage"], 600)

    def test_all_attendance_uses_monthly_days_and_weighted_hours(self):
        self.attendance("2024-01", hours=3)
        self.client.post("/api/attendance", json={
            "month": "2024-02", "work_days": 21, "daily_regular_hours": 7.5,
            "overtime_count": 0, "hours_per_ot": 2.5, "subsidy_per_ot": 10,
        })
        self.record("2024-01-30", unit_price=1)
        self.record(unit_price=1)
        self.mark("2024-01-30")
        data = self.data("ALL")
        self.assertEqual(data["totals"]["work_days"], 41)
        self.assertEqual(data["totals"]["regular_hours"], 317.5)
        self.assertEqual(data["totals"]["total_hours"], 320.5)
        self.assertEqual(data["totals"]["daily_wage"], 5.12)
        self.assertEqual(data["totals"]["hourly_wage"], 0.66)

    def test_decimal_rounding_and_explicit_zero_price(self):
        self.record(quantity=1, unit_price="0.105")
        self.record(quantity=1, unit_price="0.105")
        self.record(quantity=1, unit_price=0)
        self.assertEqual(self.data()["totals"]["piece_wage"], 0.22)
        self.price(value=9)
        self.assertEqual(self.data()["totals"]["piece_wage"], 0.22)

    def test_edit_delete_and_restore_keep_snapshot(self):
        record_id = self.record(unit_price=1).json()["id"]
        self.price(value=5)
        response = self.client.put(f"/api/records/{record_id}", json={
            "record_date": "2024-02-28", "part_name": "轴套", "quantity": 12, "note": "已更正",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data()["records"][0]["amount"], 12)
        self.client.delete(f"/api/records/{record_id}")
        self.assertEqual(self.data()["totals"]["piece_wage"], 0)
        self.assertEqual(len(self.data()["deleted_records"]), 1)
        self.client.post(f"/api/records/{record_id}/restore")
        self.client.post(f"/api/records/{record_id}/restore")
        self.assertEqual(self.data()["totals"]["piece_wage"], 12)
        self.assertGreaterEqual(len(self.data()["audit_log"]), 4)

    def test_record_validation_and_nonfinite_price(self):
        for payload in [dict(record_date="2024-02-30"), dict(quantity=1.2), dict(quantity=0), dict(part_name=" ")]:
            value = dict(record_date="2024-02-29", part_name="轴套", quantity=1)
            value.update(payload)
            self.assertEqual(self.client.post("/api/records", json=value).status_code, 422)
        for value in [-1, "NaN", "Infinity"]:
            self.assertEqual(self.price(value=value).status_code, 422)
        response = self.client.post("/api/price", content='{"part_name":"test","unit_price":NaN}', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)

    def test_attendance_validation(self):
        payload = dict(month="2024-02", work_days=20, daily_regular_hours=8, overtime_count=0)
        for change in [dict(work_days=-1), dict(work_days=30), dict(daily_regular_hours=25), dict(overtime_count=-1)]:
            self.assertEqual(self.client.post("/api/attendance", json={**payload, **change}).status_code, 422)

    def test_rename_collision_preserves_both_categories(self):
        self.record(name="A", unit_price=2)
        self.record(name="B", unit_price=3)
        response = self.client.post("/api/part/rename", json={"old_name": "A", "new_name": "B"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual({row["part_name"] for row in self.data()["records"]}, {"A", "B"})

    def test_daily_overtime_overrides_and_reset(self):
        self.attendance()
        self.client.put("/api/overtime", json={"work_date": "2024-02-29", "checked": True, "hours": 4, "subsidy": 20})
        self.mark("2024-02-29")
        self.attendance(hours=3, subsidy=15)
        summary = self.data()["overtime_summary"]
        self.assertEqual((summary["hours"], summary["subsidy"]), (4, 20))
        self.client.put("/api/overtime", json={"work_date": "2024-02-29", "checked": True, "use_month_defaults": True})
        summary = self.data()["overtime_summary"]
        self.assertEqual((summary["hours"], summary["subsidy"]), (3, 15))

    def test_month_lock_blocks_mutations_until_unlock(self):
        record_id = self.record(unit_price=1).json()["id"]
        self.attendance()
        response = self.client.put("/api/month-lock", json={"month": "2024-02", "locked": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.record().status_code, 409)
        self.assertEqual(self.mark("2024-02-29").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/records/{record_id}").status_code, 409)
        self.assertEqual(self.client.post("/api/part/rename", json={"old_name": "轴套", "new_name": "新轴套"}).status_code, 409)
        self.price(value=8)
        self.assertEqual(self.data()["totals"]["piece_wage"], 100)
        self.client.put("/api/month-lock", json={"month": "2024-02", "locked": False})
        self.assertEqual(self.client.delete(f"/api/records/{record_id}").status_code, 200)

    def test_pending_records_block_settlement(self):
        self.record()
        self.assertEqual(self.client.put("/api/month-lock", json={"month": "2024-02", "locked": True}).status_code, 409)

    def test_full_backup_roundtrip_and_safety_copy(self):
        self.record(unit_price=2)
        self.mark("2024-02-29")
        backup = self.client.get("/api/backup").json()
        self.record(quantity=2, unit_price=3)
        response = self.client.post("/api/restore", json=backup)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.data()["records"]), 1)
        self.assertEqual(self.data()["totals"]["piece_wage"], 200)
        safety_file = Path(self.module.DB_PATH).parent / "backups" / response.json()["safety_backup"]
        self.assertEqual(len(json.loads(safety_file.read_text(encoding="utf-8"))["tables"]["records"]), 2)

    def test_bad_restore_is_atomic(self):
        self.record(unit_price=2)
        backup = self.client.get("/api/backup").json()
        duplicate = copy.deepcopy(backup)
        duplicate["tables"]["records"].append(duplicate["tables"]["records"][0])
        self.assertEqual(self.client.post("/api/restore", json=duplicate).status_code, 400)
        self.assertEqual(len(self.data()["records"]), 1)
        self.assertEqual(self.data()["totals"]["piece_wage"], 200)
        invalid = copy.deepcopy(backup)
        invalid["tables"]["records"][0]["quantity"] = -1
        self.assertEqual(self.client.post("/api/restore", json=invalid).status_code, 422)
        self.assertEqual(self.data()["totals"]["piece_wage"], 200)

    def test_csv_contains_snapshot_and_neutralizes_formulas(self):
        self.record(name="=1+1", unit_price=2, note="@SUM(1)")
        self.price("=1+1", 9)
        response = self.client.get("/api/export?month=2024-02")
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8-sig")
        self.assertIn("'=1+1", text)
        self.assertIn("'@SUM(1)", text)
        self.assertIn("200.0", text)

    def test_password_guards_all_routes_and_cross_site_writes(self):
        with patch.dict("os.environ", {"WAGE_USERNAME": "owner", "WAGE_PASSWORD": "test-secret"}):
            for path in ["/", "/static/app.js", "/api/data", "/api/backup", "/docs"]:
                self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get("/api/data", auth=("owner", "wrong")).status_code, 401)
            self.assertEqual(self.client.get("/api/data", auth=("owner", "test-secret")).status_code, 200)
            response = self.client.post("/api/price", auth=("owner", "test-secret"), headers={"Origin": "https://other.example"}, json={"part_name": "A", "unit_price": 1})
            self.assertEqual(response.status_code, 403)

    def test_old_price_migration_makes_database_backup_once(self):
        with closing(sqlite3.connect(self.module.DB_PATH)) as conn:
            conn.execute("DROP TABLE records")
            conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, record_date TEXT, part_name TEXT, quantity INTEGER, note TEXT)")
            conn.execute("INSERT OR REPLACE INTO part_types VALUES ('轴套', 1.25)")
            conn.execute("INSERT INTO records VALUES (11, '2024-02-29', '轴套', 100, '')")
            conn.commit()
        self.module.init_db()
        self.price(value=2)
        self.module.init_db()
        self.assertEqual(self.data()["totals"]["piece_wage"], 125)
        backups = list((Path(self.module.DB_PATH).parent / "backups").glob("before-price-migration-*.db"))
        self.assertEqual(len(backups), 1)

    def test_legacy_all_attendance_is_visible_and_backup_roundtrips(self):
        with closing(sqlite3.connect(self.module.DB_PATH)) as conn:
            conn.execute("INSERT INTO attendance VALUES ('ALL', 60, 8, 40, 2.5, 10)")
            conn.commit()
        self.assertEqual(self.data("ALL")["legacy_all_attendance"]["work_days"], 60)
        backup = self.client.get("/api/backup").json()
        self.assertEqual(self.client.post("/api/restore", json=backup).status_code, 200)
        self.assertEqual(self.data("ALL")["legacy_all_attendance"]["overtime_count"], 40)
