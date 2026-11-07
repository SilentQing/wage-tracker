import base64
import calendar
import csv
import io
import json
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("WAGE_DB_PATH", "data/wage.db")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Money = Annotated[Decimal, Field(ge=0, le=100000000, allow_inf_nan=False)]
Hours = Annotated[float, Field(ge=0, le=24, allow_inf_nan=False)]

app = FastAPI(title="计件与工时薪资核算系统")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.exception_handler(RequestValidationError)
async def invalid_input(request, exc):
    return JSONResponse({"detail": [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]}, status_code=422)


@app.middleware("http")
async def access_control(request: Request, call_next):
    password = os.environ.get("WAGE_PASSWORD", "")
    if password:
        try:
            scheme, value = request.headers.get("authorization", "").split(" ", 1)
            credentials = base64.b64decode(value, validate=True)
            expected = f"{os.environ.get('WAGE_USERNAME', 'admin')}:{password}".encode()
            authenticated = scheme.lower() == "basic" and secrets.compare_digest(credentials, expected)
        except (ValueError, UnicodeError):
            authenticated = False
        if not authenticated:
            return JSONResponse({"detail": "请登录后访问账本"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Wage Tracker", charset="UTF-8"'})
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        if origin not in {f"http://{request.headers.get('host')}", f"https://{request.headers.get('host')}"}:
            return JSONResponse({"detail": "拒绝跨站修改账本"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def database(write=False):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    if Path(DB_PATH).exists():
        with closing(get_db()) as source:
            columns = {row["name"] for row in source.execute("PRAGMA table_info(records)")}
            if columns and "unit_price" not in columns:
                backup_dir = Path(DB_PATH).parent / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                target = backup_dir / f"before-price-migration-{uuid4().hex}.db"
                with closing(sqlite3.connect(target)) as destination:
                    source.backup(destination)
    with database(write=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS part_types (name TEXT PRIMARY KEY, unit_price REAL DEFAULT 0)")
        conn.execute("""CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, record_date TEXT NOT NULL,
            part_name TEXT NOT NULL, quantity INTEGER NOT NULL, note TEXT DEFAULT '',
            unit_price REAL, deleted INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS attendance (
            month TEXT PRIMARY KEY, work_days REAL DEFAULT 0, daily_regular_hours REAL DEFAULT 8,
            overtime_count INTEGER DEFAULT 0, hours_per_ot REAL DEFAULT 2.5, subsidy_per_ot REAL DEFAULT 10
        )""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(attendance)")}
        for name, default in [("hours_per_ot", 2.5), ("subsidy_per_ot", 10)]:
            if name not in columns:
                conn.execute(f"ALTER TABLE attendance ADD COLUMN {name} REAL DEFAULT {default}")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(records)")}
        if "unit_price" not in columns:
            conn.execute("ALTER TABLE records ADD COLUMN unit_price REAL")
            conn.execute("""UPDATE records SET unit_price = (
                SELECT CASE WHEN unit_price > 0 THEN unit_price END
                FROM part_types WHERE name = records.part_name
            )""")
        if "deleted" not in columns:
            conn.execute("ALTER TABLE records ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE TABLE IF NOT EXISTS overtime_days (work_date TEXT PRIMARY KEY, hours REAL, subsidy REAL)")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(overtime_days)")}
        for name in ["hours", "subsidy"]:
            if name not in columns:
                conn.execute(f"ALTER TABLE overtime_days ADD COLUMN {name} REAL")
        conn.execute("CREATE TABLE IF NOT EXISTS month_locks (month TEXT PRIMARY KEY)")
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            action TEXT NOT NULL, detail TEXT NOT NULL
        )""")
        conn.execute("INSERT OR IGNORE INTO part_types VALUES ('定距筒', 0)")
        conn.execute("""DELETE FROM part_types WHERE name = '庆祝'
            AND NOT EXISTS (SELECT 1 FROM records WHERE part_name = '庆祝')""")
        conn.execute("""INSERT OR IGNORE INTO part_types (name, unit_price)
            SELECT DISTINCT part_name, 0 FROM records""")
        conn.execute("CREATE INDEX IF NOT EXISTS records_date_idx ON records(record_date)")


init_db()


class RecordCreate(BaseModel):
    record_date: date
    part_name: Name
    quantity: int = Field(gt=0, le=100000000)
    note: Optional[str] = Field(default="", max_length=2000)
    unit_price: Optional[Money] = None


class PriceUpdate(BaseModel):
    part_name: Name
    unit_price: Money


class PartRename(BaseModel):
    old_name: Name
    new_name: Name


class AttendanceUpdate(BaseModel):
    month: str
    work_days: float = Field(ge=0, le=31, allow_inf_nan=False)
    daily_regular_hours: Hours
    overtime_count: int = Field(ge=0, le=31)
    hours_per_ot: Hours = 2.5
    subsidy_per_ot: Money = Decimal(10)

    @model_validator(mode="after")
    def check_month_days(self):
        start = month_start(self.month)
        if self.work_days > calendar.monthrange(start.year, start.month)[1]:
            raise ValueError("上班天数不能超过该月日历天数")
        return self


class OvertimeUpdate(BaseModel):
    work_date: date
    checked: bool
    replace_undated: bool = False
    hours: Optional[Hours] = None
    subsidy: Optional[Money] = None
    use_month_defaults: bool = False


class MonthLock(BaseModel):
    month: str
    locked: bool


def month_start(month):
    try:
        start = date.fromisoformat(f"{month}-01")
    except (ValueError, TypeError):
        raise HTTPException(400, "月份格式应为 YYYY-MM")
    if f"{start.year:04d}-{start.month:02d}" != month:
        raise HTTPException(400, "月份格式应为 YYYY-MM")
    return start


def ensure_editable(conn, month):
    if conn.execute("SELECT 1 FROM month_locks WHERE month = ?", (month,)).fetchone():
        raise HTTPException(409, f"{month} 已结算锁定，请先解锁")


def audit(conn, action, detail):
    conn.execute("INSERT INTO audit_log (created_at, action, detail) VALUES (?, ?, ?)",
                 (datetime.now(timezone.utc).isoformat(), action, json.dumps(detail, ensure_ascii=False)))


def money(value):
    return Decimal(str(value or 0))


def amount(value):
    return float(money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def attendance_for_month(conn, month):
    row = conn.execute("SELECT * FROM attendance WHERE month = ?", (month,)).fetchone()
    if row:
        result = dict(row)
        result["hours_per_ot"] = result["hours_per_ot"] if result["hours_per_ot"] is not None else 2.5
        result["subsidy_per_ot"] = result["subsidy_per_ot"] if result["subsidy_per_ot"] is not None else 10
        return result
    days = conn.execute("""SELECT COUNT(DISTINCT record_date) FROM records
        WHERE deleted = 0 AND substr(record_date, 1, 7) = ?""", (month,)).fetchone()[0]
    return dict(month=month, work_days=days, daily_regular_hours=8, overtime_count=0,
                hours_per_ot=2.5, subsidy_per_ot=10)


def overtime_data(conn, month):
    where, params = "", ()
    if month != "ALL":
        start = month_start(month)
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        first = start - timedelta(days=start.weekday())
        remaining = min(6 - end.weekday(), (date.max - end).days)
        last = end + timedelta(days=remaining)
        where = "WHERE o.work_date BETWEEN ? AND ?"
        params = (first.isoformat(), last.isoformat())
    days = [dict(row) for row in conn.execute(f"""
        SELECT o.work_date, COALESCE(o.hours, a.hours_per_ot, 2.5) AS hours,
               COALESCE(o.subsidy, a.subsidy_per_ot, 10) AS subsidy,
               o.hours AS custom_hours, o.subsidy AS custom_subsidy
        FROM overtime_days o LEFT JOIN attendance a ON a.month = substr(o.work_date, 1, 7)
        {where} ORDER BY o.work_date
    """, params)]
    monthly = [day for day in days if month == "ALL" or day["work_date"].startswith(month)]
    condition = "month != 'ALL'" if month == "ALL" else "month = ?"
    legacy = list(conn.execute(f"SELECT * FROM attendance WHERE {condition}", () if month == "ALL" else (month,)))
    count = sum(row["overtime_count"] or 0 for row in legacy)
    hours = sum((row["overtime_count"] or 0) * (row["hours_per_ot"] if row["hours_per_ot"] is not None else 2.5) for row in legacy)
    subsidy = sum((money(row["overtime_count"]) * money(row["subsidy_per_ot"] if row["subsidy_per_ot"] is not None else 10) for row in legacy), Decimal(0))
    return days, dict(dated_count=len(monthly), undated_count=count, count=len(monthly) + count,
                      hours=sum(day["hours"] for day in monthly) + hours,
                      subsidy=amount(sum((money(day["subsidy"]) for day in monthly), subsidy)))


def ledger_data(conn, month):
    if month != "ALL":
        month_start(month)
    where = "" if month == "ALL" else "WHERE substr(record_date, 1, 7) = ?"
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM records {where} ORDER BY record_date DESC, id DESC",
                                             () if month == "ALL" else (month,))]
    for row in rows:
        row["amount"] = amount(money(row["unit_price"]) * row["quantity"])
    records = [row for row in rows if not row["deleted"]]
    raw_months = [row[0] for row in conn.execute("""
        SELECT substr(record_date, 1, 7) AS m FROM records WHERE deleted = 0
        UNION SELECT substr(work_date, 1, 7) FROM overtime_days
        UNION SELECT month FROM attendance WHERE month != 'ALL'
        UNION SELECT month FROM month_locks
    """)]
    months = []
    for value in raw_months:
        try:
            month_start(value)
            months.append(value)
        except HTTPException:
            continue  # 旧版异常日期保留在全部记录中，待用户通过编辑更正。
    months = sorted(set(months + [date.today().strftime("%Y-%m")]), reverse=True)
    selected_months = months if month == "ALL" else [month]
    attendances = [attendance_for_month(conn, key) for key in selected_months]
    work_days = sum(row["work_days"] or 0 for row in attendances)
    regular_hours = sum((row["work_days"] or 0) * (row["daily_regular_hours"] or 0) for row in attendances)
    if month == "ALL":
        attendance = dict(month="ALL", work_days=work_days, daily_regular_hours=0, overtime_count=0,
                          hours_per_ot=0, subsidy_per_ot=0)
    else:
        attendance = attendances[0]
    days, summary = overtime_data(conn, month)
    piece_wage = amount(sum((money(row["amount"]) for row in records), Decimal(0)))
    total_pay = amount(money(piece_wage) + money(summary["subsidy"]))
    total_hours = regular_hours + summary["hours"]
    totals = dict(piece_wage=piece_wage, total_pay=total_pay, work_days=work_days,
                  regular_hours=regular_hours, total_hours=total_hours,
                  daily_wage=amount(money(total_pay) / money(work_days)) if work_days else 0,
                  hourly_wage=amount(money(total_pay) / money(total_hours)) if total_hours else 0)
    locks = [row[0] for row in conn.execute("SELECT month FROM month_locks")]
    legacy_all = conn.execute("SELECT * FROM attendance WHERE month = 'ALL'").fetchone()
    return dict(records=records, deleted_records=[row for row in rows if row["deleted"]],
                part_types=[dict(row) for row in conn.execute("SELECT * FROM part_types ORDER BY name")],
                attendance=attendance, months=months, overtime_days=days, overtime_summary=summary,
                totals=totals, locked_months=locks, locked=month in locks,
                audit_log=[dict(row) for row in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 20")],
                auth_enabled=bool(os.environ.get("WAGE_PASSWORD")),
                legacy_all_attendance=dict(legacy_all) if legacy_all else None)


@app.get("/api/data")
def get_all_data(month: str = "ALL"):
    with database() as conn:
        return ledger_data(conn, month)


def default_price(conn, part_name):
    row = conn.execute("SELECT unit_price FROM part_types WHERE name = ?", (part_name,)).fetchone()
    return row[0] if row and row[0] > 0 else None


def record_values(item):
    return item.record_date.isoformat(), item.part_name, item.quantity, item.note or ""


@app.post("/api/records")
def add_record(item: RecordCreate):
    with database(write=True) as conn:
        ensure_editable(conn, item.record_date.isoformat()[:7])
        conn.execute("INSERT OR IGNORE INTO part_types VALUES (?, 0)", (item.part_name,))
        price = float(item.unit_price) if item.unit_price is not None else default_price(conn, item.part_name)
        cursor = conn.execute("""INSERT INTO records (record_date, part_name, quantity, note, unit_price)
            VALUES (?, ?, ?, ?, ?)""", (*record_values(item), price))
        audit(conn, "新增交件", {"id": cursor.lastrowid, **item.model_dump(mode="json"), "unit_price": price})
        return {"status": "ok", "id": cursor.lastrowid}


@app.put("/api/records/{record_id}")
def edit_record(record_id: int, item: RecordCreate):
    with database(write=True) as conn:
        old = conn.execute("SELECT * FROM records WHERE id = ? AND deleted = 0", (record_id,)).fetchone()
        if not old:
            raise HTTPException(404, "交件记录不存在")
        ensure_editable(conn, old["record_date"][:7])
        ensure_editable(conn, item.record_date.isoformat()[:7])
        price = old["unit_price"] if item.part_name == old["part_name"] else default_price(conn, item.part_name)
        if item.unit_price is not None:
            price = float(item.unit_price)
        conn.execute("INSERT OR IGNORE INTO part_types VALUES (?, 0)", (item.part_name,))
        conn.execute("UPDATE records SET record_date=?, part_name=?, quantity=?, note=?, unit_price=? WHERE id=?",
                     (*record_values(item), price, record_id))
        audit(conn, "编辑交件", {"before": dict(old), "after": item.model_dump(mode="json"), "unit_price": price})
    return {"status": "ok"}


@app.delete("/api/records/{record_id}")
def delete_record(record_id: int):
    with database(write=True) as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, "交件记录不存在")
        ensure_editable(conn, row["record_date"][:7])
        if not row["deleted"]:
            conn.execute("UPDATE records SET deleted = 1 WHERE id = ?", (record_id,))
            audit(conn, "删除交件", dict(row))
    return {"status": "ok"}


@app.post("/api/records/{record_id}/restore")
def restore_record(record_id: int):
    with database(write=True) as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, "交件记录不存在")
        ensure_editable(conn, row["record_date"][:7])
        if row["deleted"]:
            conn.execute("UPDATE records SET deleted = 0 WHERE id = ?", (record_id,))
            audit(conn, "恢复交件", dict(row))
    return {"status": "ok"}


@app.post("/api/price")
def update_price(item: PriceUpdate):
    with database(write=True) as conn:
        conn.execute("""INSERT INTO part_types VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET unit_price=excluded.unit_price""", (item.part_name, float(item.unit_price)))
        if item.unit_price > 0:
            conn.execute("""UPDATE records SET unit_price=? WHERE part_name=? AND unit_price IS NULL
                AND substr(record_date, 1, 7) NOT IN (SELECT month FROM month_locks)""", (float(item.unit_price), item.part_name))
        audit(conn, "修改默认单价", item.model_dump(mode="json"))
    return {"status": "ok"}


@app.post("/api/part/rename")
def rename_part(item: PartRename):
    if item.old_name == item.new_name:
        return {"status": "ignored"}
    with database(write=True) as conn:
        old = conn.execute("SELECT * FROM part_types WHERE name=?", (item.old_name,)).fetchone()
        if not old:
            raise HTTPException(404, "原种类不存在")
        if conn.execute("SELECT 1 FROM part_types WHERE name=?", (item.new_name,)).fetchone():
            raise HTTPException(409, "目标种类已存在，请使用不同名称，避免合并历史账目")
        locked = conn.execute("""SELECT 1 FROM records WHERE part_name=? AND substr(record_date,1,7)
            IN (SELECT month FROM month_locks)""", (item.old_name,)).fetchone()
        if locked:
            raise HTTPException(409, "该类别关联已结算月份，请先解锁对应月份再改名")
        conn.execute("INSERT INTO part_types VALUES (?, ?)", (item.new_name, old["unit_price"]))
        conn.execute("UPDATE records SET part_name=? WHERE part_name=?", (item.new_name, item.old_name))
        conn.execute("DELETE FROM part_types WHERE name=?", (item.old_name,))
        audit(conn, "种类改名", item.model_dump())
    return {"status": "ok"}


@app.post("/api/attendance")
def save_attendance(item: AttendanceUpdate):
    with database(write=True) as conn:
        ensure_editable(conn, item.month)
        conn.execute("""INSERT INTO attendance (month, work_days, daily_regular_hours, overtime_count, hours_per_ot, subsidy_per_ot)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET work_days=excluded.work_days,
            daily_regular_hours=excluded.daily_regular_hours, overtime_count=excluded.overtime_count,
            hours_per_ot=excluded.hours_per_ot, subsidy_per_ot=excluded.subsidy_per_ot""",
            (item.month, item.work_days, item.daily_regular_hours, item.overtime_count,
             item.hours_per_ot, float(item.subsidy_per_ot)))
        audit(conn, "修改考勤", item.model_dump(mode="json"))
    return {"status": "ok"}


@app.put("/api/overtime")
def save_overtime(item: OvertimeUpdate):
    if item.work_date > date.today():
        raise HTTPException(400, "只能记录今天或过去日期的加班")
    day = item.work_date.isoformat()
    with database(write=True) as conn:
        ensure_editable(conn, day[:7])
        if item.checked:
            inserted = conn.execute("INSERT OR IGNORE INTO overtime_days (work_date) VALUES (?)", (day,)).rowcount
            if inserted and item.replace_undated:
                updated = conn.execute("""UPDATE attendance SET overtime_count=overtime_count-1
                    WHERE month=? AND overtime_count>0""", (day[:7],)).rowcount
                if not updated:
                    raise HTTPException(409, "本月已没有可转为日期记录的历史次数，请刷新后重试")
            if item.use_month_defaults:
                conn.execute("UPDATE overtime_days SET hours=NULL, subsidy=NULL WHERE work_date=?", (day,))
            else:
                if "hours" in item.model_fields_set:
                    conn.execute("UPDATE overtime_days SET hours=? WHERE work_date=?", (item.hours, day))
                if "subsidy" in item.model_fields_set:
                    value = float(item.subsidy) if item.subsidy is not None else None
                    conn.execute("UPDATE overtime_days SET subsidy=? WHERE work_date=?", (value, day))
        else:
            conn.execute("DELETE FROM overtime_days WHERE work_date=?", (day,))
        audit(conn, "记录加班" if item.checked else "取消加班", item.model_dump(mode="json"))
    return {"status": "ok"}


@app.put("/api/month-lock")
def set_month_lock(item: MonthLock):
    month_start(item.month)
    with database(write=True) as conn:
        if item.locked:
            if conn.execute("SELECT 1 FROM records WHERE deleted=0 AND unit_price IS NULL AND substr(record_date,1,7)=?", (item.month,)).fetchone():
                raise HTTPException(409, "本月还有待定价交件，请先补齐单价")
            att = attendance_for_month(conn, item.month)
            conn.execute("INSERT OR IGNORE INTO attendance (month, work_days, daily_regular_hours, overtime_count, hours_per_ot, subsidy_per_ot) VALUES (?, ?, ?, ?, ?, ?)",
                         tuple(att[key] for key in ["month", "work_days", "daily_regular_hours", "overtime_count", "hours_per_ot", "subsidy_per_ot"]))
            conn.execute("INSERT OR IGNORE INTO month_locks VALUES (?)", (item.month,))
        else:
            conn.execute("DELETE FROM month_locks WHERE month=?", (item.month,))
        audit(conn, "月份结算" if item.locked else "月份解锁", item.model_dump())
    return {"status": "ok"}


BACKUP_TABLES = ("part_types", "records", "attendance", "overtime_days", "month_locks", "audit_log")


def backup_payload(conn):
    return dict(schema_version=1, created_at=datetime.now(timezone.utc).isoformat(),
                tables={table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in BACKUP_TABLES})


@app.get("/api/backup")
def download_backup():
    with database() as conn:
        payload = backup_payload(conn)
    return Response(json.dumps(payload, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="wage-tracker-backup.json"'})


class BackupPart(BaseModel):
    name: Name
    unit_price: Money


class BackupRecord(RecordCreate):
    record_date: str = Field(min_length=1, max_length=100)
    id: int = Field(gt=0)
    deleted: Literal[0, 1] = 0


class BackupDay(BaseModel):
    work_date: date
    hours: Optional[Hours] = None
    subsidy: Optional[Money] = None


class BackupLock(BaseModel):
    month: str


class BackupAttendance(AttendanceUpdate):
    work_days: float = Field(ge=0, allow_inf_nan=False)
    overtime_count: int = Field(ge=0)

    @model_validator(mode="after")
    def check_month_days(self):
        if self.month == "ALL":
            return self
        return super().check_month_days()


class BackupAudit(BaseModel):
    id: int = Field(gt=0)
    created_at: str = Field(max_length=100)
    action: str = Field(max_length=100)
    detail: str = Field(max_length=10000)


class BackupTables(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_types: list[BackupPart] = Field(max_length=10000)
    records: list[BackupRecord] = Field(max_length=100000)
    attendance: list[BackupAttendance] = Field(max_length=120000)
    overtime_days: list[BackupDay] = Field(max_length=100000)
    month_locks: list[BackupLock] = Field(max_length=120000)
    audit_log: list[BackupAudit] = Field(max_length=100000)


class BackupPayload(BaseModel):
    schema_version: Literal[1]
    created_at: str
    tables: BackupTables


@app.post("/api/restore")
def restore_backup(payload: BackupPayload):
    tables = payload.tables.model_dump(mode="json")
    for row in tables["month_locks"]:
        month_start(row["month"])
    for row in tables["overtime_days"]:
        if date.fromisoformat(row["work_date"]) > date.today():
            raise HTTPException(400, "备份包含未来加班日期")
    names = {row["name"] for row in tables["part_types"]}
    if any(row["part_name"] not in names for row in tables["records"]):
        raise HTTPException(400, "备份中的交件引用了不存在的种类")
    try:
        with database(write=True) as conn:
            backup_dir = Path(DB_PATH).parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            safety_file = backup_dir / f"before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex}.json"
            temporary = safety_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(backup_payload(conn), ensure_ascii=False), encoding="utf-8")
            temporary.replace(safety_file)
            for table in BACKUP_TABLES:
                conn.execute(f"DELETE FROM {table}")
                for row in tables[table]:
                    columns = list(row)
                    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row.values()))
            audit(conn, "恢复完整备份", {"safety_backup": safety_file.name})
    except sqlite3.IntegrityError:
        raise HTTPException(400, "备份包含重复或冲突数据，账本已保持原样")
    return {"status": "ok", "safety_backup": safety_file.name}


@app.get("/api/export")
def export_csv(month: str = "ALL", kind: Literal["records", "overtime"] = "records"):
    with database() as conn:
        data = ledger_data(conn, month)
        condition = "month != 'ALL'" if month == "ALL" else "month = ?"
        legacy = list(conn.execute(f"SELECT * FROM attendance WHERE {condition}", () if month == "ALL" else (month,)))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if kind == "records":
        writer.writerow(["日期", "种类", "数量", "记录单价", "金额", "备注"])
        for row in data["records"]:
            writer.writerow([csv_text(row["record_date"]), csv_text(row["part_name"]), row["quantity"],
                             row["unit_price"] if row["unit_price"] is not None else "待定价", row["amount"], csv_text(row["note"])])
    else:
        writer.writerow(["日期", "加班时长", "补贴", "类型"])
        for row in data["overtime_days"]:
            if month == "ALL" or row["work_date"].startswith(month):
                writer.writerow([row["work_date"], row["hours"], row["subsidy"], "已记日期"])
        for row in legacy:
            if row["overtime_count"]:
                writer.writerow([row["month"], row["overtime_count"] * (row["hours_per_ot"] if row["hours_per_ot"] is not None else 2.5),
                                 amount(money(row["overtime_count"]) * money(row["subsidy_per_ot"] if row["subsidy_per_ot"] is not None else 10)), f"未分配日期 {row['overtime_count']} 次"])
    return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="wage-{kind}-{month}.csv"'})


def csv_text(value):
    text = str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(BASE_DIR / "templates" / "index.html", media_type="text/html")
