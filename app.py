import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

os.makedirs("data", exist_ok=True)
DB_PATH = "data/wage.db"

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="计件与工时薪资核算系统")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS part_types (
            name TEXT PRIMARY KEY,
            unit_price REAL DEFAULT 0.0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_date TEXT NOT NULL,
            part_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            note TEXT DEFAULT ''
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            month TEXT PRIMARY KEY,
            work_days REAL DEFAULT 0.0,
            daily_regular_hours REAL DEFAULT 8.0,
            overtime_count INTEGER DEFAULT 0,
            hours_per_ot REAL DEFAULT 2.5,
            subsidy_per_ot REAL DEFAULT 10.0
        )
    """)
    # 兼容旧版数据库：若缺少 hours_per_ot 字段则自动补全并默认设为 2.5 小时
    try:
        c.execute("ALTER TABLE attendance ADD COLUMN hours_per_ot REAL DEFAULT 2.5")
    except sqlite3.OperationalError:
        pass

    c.execute("INSERT OR IGNORE INTO part_types (name, unit_price) VALUES ('定距筒', 0.0)")
    c.execute("INSERT OR IGNORE INTO part_types (name, unit_price) VALUES ('庆祝', 0.0)")
    conn.commit()
    conn.close()


init_db()


class RecordCreate(BaseModel):
    record_date: str
    part_name: str
    quantity: int
    note: Optional[str] = ""


class PriceUpdate(BaseModel):
    part_name: str
    unit_price: float


class PartRename(BaseModel):
    old_name: str
    new_name: str


class AttendanceUpdate(BaseModel):
    month: str
    work_days: float
    daily_regular_hours: float
    overtime_count: int
    hours_per_ot: float = 2.5
    subsidy_per_ot: float = 10.0


@app.get("/api/data")
def get_all_data(month: str = "ALL"):
    conn = get_db()
    c = conn.cursor()

    if month == "ALL":
        c.execute("SELECT * FROM records ORDER BY record_date DESC, id DESC")
    else:
        c.execute(
            "SELECT * FROM records WHERE record_date LIKE ? ORDER BY record_date DESC, id DESC",
            (f"{month}%",),
        )
    records = [dict(row) for row in c.fetchall()]

    c.execute("SELECT DISTINCT part_name FROM records")
    for row in c.fetchall():
        c.execute(
            "INSERT OR IGNORE INTO part_types (name, unit_price) VALUES (?, 0.0)",
            (row["part_name"],),
        )
    conn.commit()

    c.execute("SELECT * FROM part_types ORDER BY name")
    part_types = [dict(row) for row in c.fetchall()]

    c.execute("SELECT * FROM attendance WHERE month = ?", (month,))
    att_row = c.fetchone()
    if att_row:
        attendance = dict(att_row)
        if attendance.get("hours_per_ot") is None:
            attendance["hours_per_ot"] = 2.5
    else:
        distinct_days = len(set(r["record_date"] for r in records))
        attendance = {
            "month": month,
            "work_days": float(distinct_days),
            "daily_regular_hours": 8.0,
            "overtime_count": 0,
            "hours_per_ot": 2.5,
            "subsidy_per_ot": 10.0,
        }

    c.execute("SELECT DISTINCT substr(record_date, 1, 7) as m FROM records ORDER BY m DESC")
    months = [r["m"] for r in c.fetchall() if r["m"]]
    current_m = date.today().strftime("%Y-%m")
    if current_m not in months:
        months.insert(0, current_m)

    conn.close()
    return {
        "records": records,
        "part_types": part_types,
        "attendance": attendance,
        "months": months,
    }


@app.post("/api/records")
def add_record(item: RecordCreate):
    part_name = item.part_name.strip()
    if not part_name or item.quantity <= 0:
        raise HTTPException(status_code=400, detail="种类名称不能为空且数量必须大于0")
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO part_types (name, unit_price) VALUES (?, 0.0)",
        (part_name,),
    )
    c.execute(
        "INSERT INTO records (record_date, part_name, quantity, note) VALUES (?, ?, ?, ?)",
        (item.record_date, part_name, item.quantity, item.note or ""),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/records/{record_id}")
def delete_record(record_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM records WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/price")
def update_price(item: PriceUpdate):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO part_types (name, unit_price) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET unit_price = excluded.unit_price",
        (item.part_name.strip(), item.unit_price),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/part/rename")
def rename_part(item: PartRename):
    old_n, new_n = item.old_name.strip(), item.new_name.strip()
    if not new_n or old_n == new_n:
        return {"status": "ignored"}
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT unit_price FROM part_types WHERE name = ?", (old_n,))
    row = c.fetchone()
    price = row["unit_price"] if row else 0.0
    c.execute(
        "INSERT OR IGNORE INTO part_types (name, unit_price) VALUES (?, ?)",
        (new_n, price),
    )
    c.execute("UPDATE records SET part_name = ? WHERE part_name = ?", (new_n, old_n))
    c.execute("DELETE FROM part_types WHERE name = ?", (old_n,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/attendance")
def save_attendance(item: AttendanceUpdate):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO attendance (month, work_days, daily_regular_hours, overtime_count, hours_per_ot, subsidy_per_ot)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(month) DO UPDATE SET
            work_days = excluded.work_days,
            daily_regular_hours = excluded.daily_regular_hours,
            overtime_count = excluded.overtime_count,
            hours_per_ot = excluded.hours_per_ot,
            subsidy_per_ot = excluded.subsidy_per_ot
        """,
        (
            item.month,
            item.work_days,
            item.daily_regular_hours,
            item.overtime_count,
            item.hours_per_ot,
            item.subsidy_per_ot,
        ),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(BASE_DIR / "templates" / "index.html", media_type="text/html")
