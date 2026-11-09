import base64
import hashlib
import calendar
import csv
import io
import json
import os
import secrets
import sqlite3
import asyncio
from contextlib import closing, contextmanager, asynccontextmanager
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
from backup_service import BackupService, MAX_BACKUP_BYTES

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("WAGE_DB_PATH", "data/wage.db")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Money = Annotated[Decimal, Field(ge=0, le=100000000, allow_inf_nan=False)]
Hours = Annotated[float, Field(ge=0, le=24, allow_inf_nan=False)]
SubmissionId = Annotated[str, StringConstraints(pattern=r'^[a-zA-Z0-9-]{16,64}$')]

@asynccontextmanager
async def lifespan(application):
    service = make_backup_service()
    service.start()
    try:
        yield
    finally:
        await asyncio.to_thread(service.stop)


app = FastAPI(title="计件与工时薪资核算系统", lifespan=lifespan)
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
    if request.method == 'POST' and request.url.path in {'/api/restore', '/api/restore/preview'}:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BACKUP_BYTES:
                return JSONResponse({'detail': '备份文件不能超过64MB'}, status_code=413)
        request._body = bytes(body)
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
        conn.execute("CREATE TABLE IF NOT EXISTS daily_months (month TEXT PRIMARY KEY, through_date TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS leave_days (work_date TEXT PRIMARY KEY, hours REAL NOT NULL, note TEXT NOT NULL DEFAULT '')")
        conn.execute("CREATE TABLE IF NOT EXISTS rejections (record_id INTEGER PRIMARY KEY, quantity INTEGER NOT NULL, reason TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS attendance_bonuses (month TEXT PRIMARY KEY, awarded INTEGER NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY, month TEXT NOT NULL, paid_date TEXT NOT NULL,
            amount TEXT NOT NULL, note TEXT NOT NULL, voided INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            action TEXT NOT NULL, detail TEXT NOT NULL
        )""")
        conn.execute('CREATE TABLE IF NOT EXISTS record_requests (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, record_id INTEGER NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS submission_cancellations (kind TEXT NOT NULL, request_id TEXT NOT NULL, PRIMARY KEY(kind,request_id))')
        conn.execute('CREATE TABLE IF NOT EXISTS submission_identity (id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO submission_identity VALUES (1,?)', (uuid4().hex,))
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


class RecordSubmission(RecordCreate):
    request_id: Optional[SubmissionId] = None
    submission_epoch: Optional[SubmissionId] = None


def submission_epoch(conn):
    row = conn.execute('SELECT epoch FROM submission_identity WHERE id=1').fetchone()
    return row[0] if row else None


def check_submission_epoch(conn, epoch):
    if epoch is not None and epoch != submission_epoch(conn):
        raise HTTPException(409, '账本已恢复或切换；旧提交不能重试，请人工核对后取消本机待确认提示')


def submission_result(conn, kind, request_id):
    if kind == 'records':
        row = conn.execute('SELECT r.id,r.deleted FROM record_requests q JOIN records r ON r.id=q.record_id WHERE q.request_id=?', (request_id,)).fetchone()
    else:
        row = conn.execute('SELECT id,voided FROM payments WHERE id=?', (request_id,)).fetchone()
    if row:
        return dict(state='saved', **dict(row))
    if conn.execute('SELECT 1 FROM submission_cancellations WHERE kind=? AND request_id=?', (kind, request_id)).fetchone():
        return dict(state='cancelled')
    return dict(state='absent')


def ensure_not_cancelled(conn, kind, request_id):
    if conn.execute('SELECT 1 FROM submission_cancellations WHERE kind=? AND request_id=?', (kind, request_id)).fetchone():
        raise HTTPException(409, '该次提交已取消，不能重试；如需登记请新建一次提交')


@app.get('/api/submissions/{kind}/{request_id}')
def check_submission(kind: Literal['records', 'payments'], request_id: SubmissionId, epoch: SubmissionId):
    with database() as conn:
        check_submission_epoch(conn, epoch)
        return submission_result(conn, kind, request_id)


@app.post('/api/submissions/{kind}/{request_id}/cancel')
def cancel_submission(kind: Literal['records', 'payments'], request_id: SubmissionId, epoch: SubmissionId):
    with database(write=True) as conn:
        check_submission_epoch(conn, epoch)
        result = submission_result(conn, kind, request_id)
        if result['state'] == 'absent':
            conn.execute('INSERT INTO submission_cancellations VALUES (?,?)', (kind, request_id))
            audit(conn, '取消待确认提交', dict(kind=kind, request_id=request_id))
            result = dict(state='cancelled')
        return result


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
    mode = conn.execute("SELECT * FROM daily_months WHERE month=?", (month,)).fetchone()
    if mode:
        row = conn.execute("SELECT * FROM attendance WHERE month=?", (month,)).fetchone()
        result = dict(row) if row else dict(month=month, overtime_count=0, hours_per_ot=2.5, subsidy_per_ot=10)
        start = month_start(month)
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        cutoff = min(end, date.fromisoformat(mode['through_date']) if mode['through_date'] else date.today())
        days = max(0, (cutoff - start).days + 1)
        leave = conn.execute("SELECT COALESCE(SUM(hours),0) FROM leave_days WHERE work_date BETWEEN ? AND ?", (start.isoformat(), cutoff.isoformat())).fetchone()[0]
        result.update(work_days=(days * 8.5 - leave) / 8.5, daily_regular_hours=8.5,
                      daily_mode=True, leave_hours=leave, calendar_days=days, through_date=cutoff.isoformat())
        return result
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
    rejected = {row['record_id']: dict(row) for row in conn.execute('SELECT * FROM rejections')}
    for row in rows:
        rejection = rejected.get(row['id'], {})
        row['rejected_quantity'] = rejection.get('quantity', 0)
        row['rejection_reason'] = rejection.get('reason', '')
        row['payable_quantity'] = row['quantity'] - row['rejected_quantity']
        row['gross_amount'] = amount(money(row['unit_price']) * row['quantity'])
        row["amount"] = amount(money(row["unit_price"]) * row['payable_quantity'])
        row['deduction_amount'] = amount(money(row['gross_amount']) - money(row['amount']))
    records = [row for row in rows if not row["deleted"]]
    raw_months = [row[0] for row in conn.execute("""
        SELECT substr(record_date, 1, 7) AS m FROM records WHERE deleted = 0
        UNION SELECT substr(work_date, 1, 7) FROM overtime_days
        UNION SELECT month FROM attendance WHERE month != 'ALL'
        UNION SELECT month FROM month_locks
        UNION SELECT month FROM daily_months
        UNION SELECT month FROM attendance_bonuses
        UNION SELECT month FROM payments
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
    bonus_rows = [dict(row) for row in conn.execute('SELECT * FROM attendance_bonuses') if month == 'ALL' or row['month'] == month]
    bonus = sum(100 for row in bonus_rows if row['awarded'])
    payments = [dict(row) for row in conn.execute('SELECT * FROM payments ORDER BY paid_date DESC, id') if month == 'ALL' or row['month'] == month]
    paid = amount(sum((money(row['amount']) for row in payments if not row['voided']), Decimal(0)))
    for row in payments:
        row['amount'] = amount(row['amount'])
    total_pay = amount(money(piece_wage) + money(summary["subsidy"]) + bonus)
    difference = amount(money(total_pay) - money(paid))
    status = '超额到账' if difference < 0 else '已结清' if difference == 0 else '部分到账' if paid else '未到账'
    pending_prices = any(row['unit_price'] is None for row in records)
    unconfirmed = [key for key in selected_months if key not in {row['month'] for row in bonus_rows}]
    reconciliation = dict(paid=paid, difference=difference, status=status,
                          pending_prices=pending_prices, unconfirmed_bonus_months=unconfirmed,
                          bonus_awarded=bool(bonus_rows[0]['awarded']) if month != 'ALL' and bonus_rows else None)
    total_hours = regular_hours + summary["hours"]
    totals = dict(piece_wage=piece_wage, total_pay=total_pay, work_days=work_days, attendance_bonus=bonus,
                  rejection_deduction=amount(sum((money(row['deduction_amount']) for row in records), Decimal(0))),
                  regular_hours=regular_hours, total_hours=total_hours,
                  daily_wage=amount(money(total_pay) / money(work_days)) if work_days else 0,
                  hourly_wage=amount(money(total_pay) / money(total_hours)) if total_hours else 0)
    locks = [row[0] for row in conn.execute("SELECT month FROM month_locks")]
    legacy_all = conn.execute("SELECT * FROM attendance WHERE month = 'ALL'").fetchone()
    return dict(records=records, deleted_records=[row for row in rows if row["deleted"]], submission_epoch=submission_epoch(conn),
                reconciliation=reconciliation, payments=payments,
                leave_days=[dict(row) for row in conn.execute("SELECT * FROM leave_days ORDER BY work_date") if month == 'ALL' or row['work_date'].startswith(month)],
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
def add_record(item: RecordSubmission):
    original = item.model_dump(mode='json', exclude={'request_id', 'submission_epoch'})
    original['note'] = item.note or ''
    original['unit_price'] = format(item.unit_price.normalize(), 'f') if item.unit_price is not None else None
    fingerprint = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with database(write=True) as conn:
        check_submission_epoch(conn, item.submission_epoch)
        if item.request_id:
            ensure_not_cancelled(conn, 'records', item.request_id)
            previous = conn.execute('SELECT * FROM record_requests WHERE request_id=?', (item.request_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise HTTPException(409, '该提交标识已保存其他交件内容，请先核对原提交')
                return dict(status='ok', **submission_result(conn, 'records', item.request_id))
        ensure_editable(conn, item.record_date.isoformat()[:7])
        conn.execute("INSERT OR IGNORE INTO part_types VALUES (?, 0)", (item.part_name,))
        price = float(item.unit_price) if item.unit_price is not None else default_price(conn, item.part_name)
        cursor = conn.execute("""INSERT INTO records (record_date, part_name, quantity, note, unit_price)
            VALUES (?, ?, ?, ?, ?)""", (*record_values(item), price))
        audit(conn, "新增交件", {"id": cursor.lastrowid, **item.model_dump(mode="json"), "unit_price": price})
        if item.request_id:
            conn.execute('INSERT INTO record_requests VALUES (?,?,?)', (item.request_id, fingerprint, cursor.lastrowid))
        return {"status": "ok", "state": "saved", "id": cursor.lastrowid, "deleted": 0}


@app.put("/api/records/{record_id}")
def edit_record(record_id: int, item: RecordCreate):
    with database(write=True) as conn:
        old = conn.execute("SELECT * FROM records WHERE id = ? AND deleted = 0", (record_id,)).fetchone()
        if not old:
            raise HTTPException(404, "交件记录不存在")
        ensure_editable(conn, old["record_date"][:7])
        ensure_editable(conn, item.record_date.isoformat()[:7])
        price = old["unit_price"] if item.part_name == old["part_name"] else default_price(conn, item.part_name)
        rejection = conn.execute('SELECT quantity FROM rejections WHERE record_id=?', (record_id,)).fetchone()
        if rejection and item.quantity < rejection['quantity']:
            raise HTTPException(400, '提交数量不能小于不合格数量，请先更正扣件')
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
        if conn.execute("SELECT 1 FROM daily_months WHERE month=?", (item.month,)).fetchone():
            calculated = attendance_for_month(conn, item.month)
            item.work_days = calculated['work_days']
            item.daily_regular_hours = 8.5
        conn.execute("""INSERT INTO attendance (month, work_days, daily_regular_hours, overtime_count, hours_per_ot, subsidy_per_ot)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET work_days=excluded.work_days,
            daily_regular_hours=excluded.daily_regular_hours, overtime_count=excluded.overtime_count,
            hours_per_ot=excluded.hours_per_ot, subsidy_per_ot=excluded.subsidy_per_ot""",
            (item.month, item.work_days, item.daily_regular_hours, item.overtime_count,
             item.hours_per_ot, float(item.subsidy_per_ot)))
        audit(conn, "修改考勤", item.model_dump(mode="json"))
    return {"status": "ok"}


class DailyMonth(BaseModel):
    month: str


class LeaveDay(BaseModel):
    work_date: date
    hours: float = Field(ge=0, le=8.5, allow_inf_nan=False)
    note: str = Field(default='', max_length=2000)


@app.put('/api/daily-attendance')
def enable_daily_attendance(item: DailyMonth):
    month_start(item.month)
    with database(write=True) as conn:
        ensure_editable(conn, item.month)
        conn.execute('INSERT OR IGNORE INTO daily_months(month) VALUES (?)', (item.month,))
        audit(conn, '启用每日出勤', item.model_dump())
    return {'status': 'ok'}


@app.put('/api/leave')
def save_leave(item: LeaveDay):
    if item.work_date > date.today():
        raise HTTPException(400, '不能记录未来日期的请假')
    day = item.work_date.isoformat()
    with database(write=True) as conn:
        ensure_editable(conn, day[:7])
        if not conn.execute('SELECT 1 FROM daily_months WHERE month=?', (day[:7],)).fetchone():
            raise HTTPException(409, '请先启用该月每日出勤')
        if item.hours == 0:
            conn.execute('DELETE FROM leave_days WHERE work_date=?', (day,))
        else:
            conn.execute('INSERT INTO leave_days VALUES (?,?,?) ON CONFLICT(work_date) DO UPDATE SET hours=excluded.hours,note=excluded.note', (day, item.hours, item.note))
        audit(conn, '修改请假', item.model_dump(mode='json'))
    return {'status': 'ok'}


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
            conn.execute('UPDATE daily_months SET through_date=COALESCE(through_date,?) WHERE month=?', (date.today().isoformat(), item.month))
        else:
            conn.execute("DELETE FROM month_locks WHERE month=?", (item.month,))
            conn.execute('UPDATE daily_months SET through_date=NULL WHERE month=?', (item.month,))
        audit(conn, "月份结算" if item.locked else "月份解锁", item.model_dump())
    return {"status": "ok"}


class RejectionUpdate(BaseModel):
    quantity: int = Field(ge=0, le=100000000)
    reason: str = Field(default='', max_length=2000)


class BonusUpdate(BaseModel):
    month: str
    awarded: bool


class PaymentCreate(BaseModel):
    id: Annotated[str, StringConstraints(pattern=r'^[a-zA-Z0-9-]{16,64}$')]
    month: str
    paid_date: date
    amount: Decimal = Field(gt=0, le=100000000, decimal_places=2, allow_inf_nan=False)
    note: str = Field(default='', max_length=2000)


class PaymentSubmission(PaymentCreate):
    submission_epoch: Optional[SubmissionId] = None


@app.put('/api/records/{record_id}/rejection')
def save_rejection(record_id: int, item: RejectionUpdate):
    with database(write=True) as conn:
        row = conn.execute('SELECT * FROM records WHERE id=? AND deleted=0', (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, '交件记录不存在')
        ensure_editable(conn, row['record_date'][:7])
        if item.quantity > row['quantity']:
            raise HTTPException(400, '不合格数量不能超过提交数量')
        if item.quantity:
            conn.execute('INSERT INTO rejections VALUES (?,?,?) ON CONFLICT(record_id) DO UPDATE SET quantity=excluded.quantity,reason=excluded.reason', (record_id, item.quantity, item.reason))
        else:
            conn.execute('DELETE FROM rejections WHERE record_id=?', (record_id,))
        audit(conn, '修改不合格扣件', dict(record_id=record_id, **item.model_dump()))
    return {'status': 'ok'}


@app.put('/api/attendance-bonus')
def save_attendance_bonus(item: BonusUpdate):
    start = month_start(item.month)
    if date.today() < start.replace(day=calendar.monthrange(start.year, start.month)[1]):
        raise HTTPException(400, '请在月底或之后确认全勤奖')
    with database(write=True) as conn:
        ensure_editable(conn, item.month)
        conn.execute('INSERT INTO attendance_bonuses VALUES (?,?) ON CONFLICT(month) DO UPDATE SET awarded=excluded.awarded', (item.month, int(item.awarded)))
        audit(conn, '确认全勤奖', item.model_dump())
    return {'status': 'ok'}


@app.post('/api/payments')
def add_payment(item: PaymentSubmission):
    month_start(item.month)
    if item.paid_date > date.today():
        raise HTTPException(400, '到账日期不能在未来')
    values = dict(id=item.id, month=item.month, paid_date=item.paid_date.isoformat(), amount=format(item.amount, '.2f'), note=item.note)
    with database(write=True) as conn:
        check_submission_epoch(conn, item.submission_epoch)
        ensure_not_cancelled(conn, 'payments', item.id)
        existing = conn.execute('SELECT * FROM payments WHERE id=?', (item.id,)).fetchone()
        if existing:
            if any(str(existing[key]) != str(value) for key, value in values.items()):
                raise HTTPException(409, '该请求已登记其他到账内容，请刷新核对')
            return {'status': 'ok', 'state': 'saved', 'id': item.id, 'voided': existing['voided']}
        conn.execute('INSERT INTO payments(id,month,paid_date,amount,note) VALUES (?,?,?,?,?)', tuple(values.values()))
        audit(conn, '登记工资到账', values)
    return {'status': 'ok', 'state': 'saved', 'id': item.id, 'voided': 0}


@app.delete('/api/payments/{payment_id}')
def void_payment(payment_id: str):
    with database(write=True) as conn:
        row = conn.execute('SELECT * FROM payments WHERE id=?', (payment_id,)).fetchone()
        if not row:
            raise HTTPException(404, '到账记录不存在')
        if not row['voided']:
            conn.execute('UPDATE payments SET voided=1 WHERE id=?', (payment_id,))
            audit(conn, '作废工资到账', dict(row))
    return {'status': 'ok'}


BACKUP_TABLES = ("part_types", "records", "attendance", "overtime_days", "month_locks", "audit_log", "daily_months", "leave_days", "rejections", "attendance_bonuses", "payments", "record_requests", "submission_cancellations")


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


class BackupDailyMonth(BaseModel):
    month: str
    through_date: Optional[date] = None


class BackupRejection(RejectionUpdate):
    record_id: int = Field(gt=0)


class BackupBonus(BaseModel):
    month: str
    awarded: Literal[0, 1]


class BackupPayment(PaymentCreate):
    voided: Literal[0, 1] = 0


class BackupRecordRequest(BaseModel):
    request_id: SubmissionId
    fingerprint: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]
    record_id: int = Field(gt=0)


class BackupCancellation(BaseModel):
    kind: Literal['records', 'payments']
    request_id: SubmissionId


class BackupTables(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_types: list[BackupPart] = Field(max_length=10000)
    records: list[BackupRecord] = Field(max_length=100000)
    attendance: list[BackupAttendance] = Field(max_length=120000)
    overtime_days: list[BackupDay] = Field(max_length=100000)
    month_locks: list[BackupLock] = Field(max_length=120000)
    audit_log: list[BackupAudit] = Field(max_length=100000)
    daily_months: list[BackupDailyMonth] = Field(default_factory=list, max_length=120000)
    leave_days: list[LeaveDay] = Field(default_factory=list, max_length=100000)
    rejections: list[BackupRejection] = Field(default_factory=list, max_length=100000)
    attendance_bonuses: list[BackupBonus] = Field(default_factory=list, max_length=120000)
    payments: list[BackupPayment] = Field(default_factory=list, max_length=100000)
    record_requests: list[BackupRecordRequest] = Field(default_factory=list, max_length=100000)
    submission_cancellations: list[BackupCancellation] = Field(default_factory=list, max_length=100000)


class BackupPayload(BaseModel):
    schema_version: Literal[1]
    created_at: str
    tables: BackupTables


def validate_backup_tables(payload: BackupPayload):
    tables = payload.tables.model_dump(mode="json")
    records_by_id = {row['id']: row for row in tables['records']}
    for row in tables['record_requests']:
        if row['record_id'] not in records_by_id:
            raise HTTPException(400, '交件提交标识引用了不存在的记录')
    saved_requests = {('records', row['request_id']) for row in tables['record_requests']}
    saved_requests.update(('payments', row['id']) for row in tables['payments'])
    if any((row['kind'], row['request_id']) in saved_requests for row in tables['submission_cancellations']):
        raise HTTPException(400, '提交取消标记与已保存记录冲突')
    for row in tables['rejections']:
        original = records_by_id.get(row['record_id'])
        if not original or row['quantity'] > original['quantity']:
            raise HTTPException(400, '扣件记录引用无效或数量超过提交数量')
    for row in tables['attendance_bonuses']:
        start = month_start(row['month'])
        if date.today() < start.replace(day=calendar.monthrange(start.year, start.month)[1]):
            raise HTTPException(400, '备份包含尚未到月底的全勤确认')
    for row in tables['payments']:
        month_start(row['month'])
        if date.fromisoformat(row['paid_date']) > date.today():
            raise HTTPException(400, '备份包含未来到账日期')
        row['amount'] = format(money(row['amount']), '.2f')
    for row in tables["month_locks"]:
        month_start(row["month"])
    daily = {row['month'] for row in tables['daily_months']}
    locked = {row['month'] for row in tables['month_locks']}
    for row in tables['daily_months']:
        month_start(row['month'])
        if bool(row['through_date']) != (row['month'] in locked):
            raise HTTPException(400, '每日考勤冻结状态与月份锁不一致')
        if row['through_date'] and date.fromisoformat(row['through_date']) > date.today():
            raise HTTPException(400, '考勤截止日期不能在未来')
    for row in tables['leave_days']:
        if row['work_date'][:7] not in daily or date.fromisoformat(row['work_date']) > date.today():
            raise HTTPException(400, '请假日期或月份无效')
    for row in tables["overtime_days"]:
        if date.fromisoformat(row["work_date"]) > date.today():
            raise HTTPException(400, "备份包含未来加班日期")
    names = {row["name"] for row in tables["part_types"]}
    if any(row["part_name"] not in names for row in tables["records"]):
        raise HTTPException(400, "备份中的交件引用了不存在的种类")
    return tables


def insert_backup_tables(conn, tables):
    for table in BACKUP_TABLES:
        conn.execute(f"DELETE FROM {table}")
        for row in tables[table]:
            columns = list(row)
            conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row.values()))


def check_backup(payload, source=None):
    if not isinstance(payload, BackupPayload):
        payload = BackupPayload.model_validate(payload)
    tables = validate_backup_tables(payload)
    with closing(sqlite3.connect(':memory:')) as restored:
        restored.row_factory = sqlite3.Row
        if source is None:
            with database() as original:
                schema = [row[0] for row in original.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        else:
            schema = [row[0] for row in source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for sql in schema:
            restored.execute(sql)
        try:
            insert_backup_tables(restored, tables)
        except sqlite3.IntegrityError:
            raise HTTPException(400, '备份包含重复或冲突数据')
        if restored.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise HTTPException(400, '临时数据库完整性检查失败')
        counts = {table: restored.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in BACKUP_TABLES}
        if counts != {table: len(tables[table]) for table in BACKUP_TABLES}:
            raise HTTPException(400, '恢复记录数量不一致')
        data = ledger_data(restored, 'ALL')
        if source is not None:
            original = ledger_data(source, 'ALL')
            if data['totals'] != original['totals'] or data['reconciliation'] != original['reconciliation']:
                raise HTTPException(400, '试恢复后的核算结果与备份源不一致')
        return dict(created_at=payload.created_at, counts=counts, months=data['months'],
                    totals=data['totals'], paid=data['reconciliation']['paid'], verified=True)


def capture_checked_backup():
    with database() as conn:
        payload = backup_payload(conn)
        report = check_backup(payload, source=conn)
    return payload, report


def make_backup_service():
    return BackupService(Path(DB_PATH).resolve().parent / 'backups', capture_checked_backup, check_backup)


@app.get('/api/backups')
def backup_status():
    return make_backup_service().status()


@app.post('/api/backups')
def create_server_backup():
    try:
        return make_backup_service().create()
    except Exception:
        raise HTTPException(500, '备份失败，请查看备份状态中的原因')


@app.post('/api/backups/{name}/check')
def recheck_server_backup(name: str):
    try:
        return make_backup_service().recheck(name)
    except (ValueError, FileNotFoundError):
        raise HTTPException(400, '备份不存在、内容损坏或校验未通过')


@app.post('/api/backups/remote/retry')
def retry_remote_backups():
    try:
        return make_backup_service().retry_remote()
    except ValueError as error:
        raise HTTPException(409, str(error))


@app.get('/api/backups/{name}/download')
def download_server_backup(name: str):
    try:
        raw = make_backup_service().read_verified(name)
    except (ValueError, FileNotFoundError):
        raise HTTPException(400, '备份不存在或文件校验失败')
    return Response(raw, media_type='application/json', headers={'Content-Disposition': f'attachment; filename="{name}"'})


@app.post('/api/restore/preview')
def preview_restore(payload: BackupPayload):
    return check_backup(payload)


@app.post("/api/restore")
def restore_backup(payload: BackupPayload):
    check_backup(payload)
    tables = validate_backup_tables(payload)
    try:
        with database(write=True) as conn:
            backup_dir = Path(DB_PATH).parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            safety_file = backup_dir / f"before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex}.json"
            temporary = safety_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(backup_payload(conn), ensure_ascii=False), encoding="utf-8")
            temporary.replace(safety_file)
            insert_backup_tables(conn, tables)
            conn.execute('UPDATE submission_identity SET epoch=? WHERE id=1', (uuid4().hex,))
            audit(conn, "恢复完整备份", {"safety_backup": safety_file.name})
    except sqlite3.IntegrityError:
        raise HTTPException(400, "备份包含重复或冲突数据，账本已保持原样")
    return {"status": "ok", "safety_backup": safety_file.name}


@app.get("/api/export")
def export_csv(month: str = "ALL", kind: Literal["records", "overtime", "statement"] = "records"):
    if kind == "statement":
        if month == "ALL":
            raise HTTPException(400, "请选择具体月份后导出月结单")
        month_start(month)
    with database() as conn:
        data = ledger_data(conn, month)
        condition = "month != 'ALL'" if month == "ALL" else "month = ?"
        legacy = list(conn.execute(f"SELECT * FROM attendance WHERE {condition}", () if month == "ALL" else (month,)))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if kind == "statement":
        write_monthly_statement(writer, data, legacy, month)
    elif kind == "records":
        writer.writerow(["日期", "种类", "提交数量", "记录单价", "合格计件金额", "备注", "不合格数量", "计薪数量", "扣件金额", "扣件原因"])
        for row in data["records"]:
            writer.writerow([csv_text(row["record_date"]), csv_text(row["part_name"]), row["quantity"],
                             row["unit_price"] if row["unit_price"] is not None else "待定价", row["amount"], csv_text(row["note"]), row['rejected_quantity'], row['payable_quantity'], row['deduction_amount'], csv_text(row['rejection_reason'])])
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


def write_monthly_statement(writer, data, legacy, month):
    totals, info, att = data['totals'], data['reconciliation'], data['attendance']
    ot = data['overtime_summary']
    yuan = lambda value: format(money(value), '.2f')
    number = lambda value: format(money(value).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP), 'f').rstrip('0').rstrip('.') or '0'
    writer.writerows([
        ['完整月结单'],
        ['工资所属月份', month],
        ['导出时间（北京时间）', datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')],
        ['锁定状态', '已锁定' if data['locked'] else '未锁定'],
        ['说明', '当前账本快照；到账按工资所属月份归集，作废到账不计合计。'],
        [], ['出勤汇总'],
        ['考勤模式', '每日出勤' if att.get('daily_mode') else '旧月度考勤'],
        ['考勤截止日期', att.get('through_date', '按月度填写值')],
        ['出勤天数（折算）', number(totals['work_days'])],
        ['正班工时（小时）', number(totals['regular_hours'])],
        ['请假时长（小时）', number(att['leave_hours']) if att.get('daily_mode') else '未记录（旧月度考勤）'],
        ['加班次数', ot['count']], ['加班工时（小时）', number(ot['hours'])],
        ['总工时（小时）', number(totals['total_hours'])],
        [], ['工资组成'],
        ['原计件金额（元）', yuan(sum((money(row['gross_amount']) for row in data['records']), Decimal(0)))],
        ['不合格扣件（元）', yuan(totals['rejection_deduction'])],
        ['合格计件金额（元）', yuan(totals['piece_wage'])],
        ['加班补贴（元）', yuan(ot['subsidy'])],
        ['全勤奖（元）', yuan(totals['attendance_bonus'])],
        ['全勤确认', '未确认（暂计0元）' if info['bonus_awarded'] is None else '已确认有奖' if info['bonus_awarded'] else '已确认无奖'],
        ['应发工资（元）', yuan(totals['total_pay'])],
        ['平均日薪（元）', yuan(totals['daily_wage'])],
        ['实际时薪（元）', yuan(totals['hourly_wage'])],
        [], ['到账对账'],
        ['有效到账合计（元）', yuan(info['paid'])],
        ['未到账金额（元）', yuan(max(0, info['difference']))],
        ['超额到账金额（元）', yuan(max(0, -info['difference']))],
        ['对账状态（按当前已记金额）', info['status']],
        [], ['核对提示'],
    ])
    warnings = []
    if info['pending_prices']:
        warnings.append('存在待定价交件，相关金额暂计0元，应发尚不完整。')
    if info['unconfirmed_bonus_months']:
        warnings.append('全勤奖未确认，暂计0元；对账状态不代表最终核薪完成。')
    if ot['undated_count']:
        warnings.append(f"存在未分配日期的历史加班 {ot['undated_count']} 次，已计入汇总。")
    if not data['locked']:
        warnings.append('月份未锁定，后续补记、调账或每日出勤累计会改变结果。')
    for warning in warnings or ['无待定价、未确认全勤奖或未分配历史加班提示。']:
        writer.writerow([warning])
    writer.writerows([[], ['交件明细（不含已删除）'], ['日期', '种类', '提交数量', '单价（元）', '原金额（元）', '不合格数量', '计薪数量', '扣件金额（元）', '合格金额（元）', '扣件原因', '备注']])
    for row in data['records']:
        writer.writerow([csv_text(row['record_date']), csv_text(row['part_name']), row['quantity'],
                         row['unit_price'] if row['unit_price'] is not None else '待定价', yuan(row['gross_amount']),
                         row['rejected_quantity'], row['payable_quantity'], yuan(row['deduction_amount']),
                         yuan(row['amount']), csv_text(row['rejection_reason']), csv_text(row['note'])])
    writer.writerows([[], ['请假明细'], ['日期', '请假小时', '备注']])
    for row in data['leave_days']:
        writer.writerow([row['work_date'], number(row['hours']), csv_text(row['note'])])
    if not data['leave_days']:
        writer.writerow(['无请假记录' if att.get('daily_mode') else '未记录（旧月度考勤）'])
    writer.writerows([[], ['加班明细'], ['日期或月份', '时长（小时）', '补贴（元）', '类型']])
    for row in data['overtime_days']:
        if row['work_date'].startswith(month):
            writer.writerow([row['work_date'], number(row['hours']), yuan(row['subsidy']), '已记日期'])
    for row in legacy:
        count = row['overtime_count'] or 0
        if count:
            writer.writerow([row['month'], number(count * (row['hours_per_ot'] if row['hours_per_ot'] is not None else 2.5)),
                             yuan(count * money(row['subsidy_per_ot'] if row['subsidy_per_ot'] is not None else 10)), f'未分配日期 {count} 次'])
    writer.writerows([[], ['到账明细（含作废记录）'], ['工资所属月份', '到账日期', '金额（元）', '状态', '备注', '记录编号']])
    for row in data['payments']:
        writer.writerow([row['month'], row['paid_date'], yuan(row['amount']),
                         '已作废（不计入合计）' if row['voided'] else '有效', csv_text(row['note']), csv_text(row['id'])])


def csv_text(value):
    text = str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(BASE_DIR / "templates" / "index.html", media_type="text/html")
