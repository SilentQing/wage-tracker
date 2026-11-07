import os
import sqlite3
from datetime import date
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

os.makedirs("data", exist_ok=True)
DB_PATH = "data/wage.db"

app = FastAPI(title="计件与工时薪资核算系统")


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


HTML_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>计件交件与薪资核算</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-100 text-slate-800 min-h-screen pb-12">
    <div class="max-w-4xl mx-auto p-4 space-y-5">
        <div class="flex flex-wrap items-center justify-between bg-white p-4 rounded-xl shadow-sm gap-3">
            <div>
                <h1 class="text-xl font-bold text-slate-900">⚙️ 交件数量与薪资核算账本</h1>
                <p class="text-xs text-slate-500">飞牛NAS本地持久化存储 · 实时计算日薪与时薪</p>
            </div>
            <div class="flex items-center gap-2">
                <label class="text-sm font-medium text-slate-600">核算周期:</label>
                <select id="monthSelect" onchange="loadData()" class="border border-slate-300 rounded-lg px-3 py-1.5 text-sm bg-slate-50 font-medium">
                </select>
            </div>
        </div>

        <div id="unpricedAlert" class="hidden bg-amber-50 border-l-4 border-amber-500 p-4 rounded-r-xl shadow-sm">
            <div class="flex items-start">
                <div class="flex-shrink-0 text-amber-500 font-bold text-lg">⚠️</div>
                <div class="ml-3 w-full">
                    <h3 class="text-sm font-bold text-amber-800">检测到有已交件种类尚未设置单价！</h3>
                    <p class="text-xs text-amber-700 mt-1">请在下方【种类与单价配置】中补全以下种类的单价，否则其计件金额暂按 0 元计算：</p>
                    <div id="unpricedList" class="mt-2 flex flex-wrap gap-2"></div>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div class="bg-emerald-600 text-white p-4 rounded-xl shadow-sm">
                <div class="text-xs opacity-85">当前总收入 (含补贴)</div>
                <div id="statTotalPay" class="text-2xl font-bold mt-1">¥0.00</div>
                <div id="statPaySub" class="text-xs opacity-80 mt-1">计件 ¥0 + 补贴 ¥0</div>
            </div>
            <div class="bg-white p-4 rounded-xl shadow-sm border border-slate-200">
                <div class="text-xs text-slate-500">平均日薪</div>
                <div id="statDailyWage" class="text-2xl font-bold text-slate-900 mt-1">¥0.00</div>
                <div id="statDaysSub" class="text-xs text-slate-400 mt-1">按 0 天计算</div>
            </div>
            <div class="bg-white p-4 rounded-xl shadow-sm border border-slate-200">
                <div class="text-xs text-slate-500">实际时薪</div>
                <div id="statHourlyWage" class="text-2xl font-bold text-blue-600 mt-1">¥0.00</div>
                <div id="statHoursSub" class="text-xs text-slate-400 mt-1">总工时 0 小时</div>
            </div>
            <div class="bg-white p-4 rounded-xl shadow-sm border border-slate-200">
                <div class="text-xs text-slate-500">累计交件总数</div>
                <div id="statTotalQty" class="text-2xl font-bold text-slate-900 mt-1">0 <span class="text-sm font-normal">件</span></div>
                <div id="statTypesSub" class="text-xs text-slate-400 mt-1">共 0 个种类</div>
            </div>
        </div>

        <div class="grid md:grid-cols-2 gap-4">
            <div class="bg-white p-4 rounded-xl shadow-sm space-y-3">
                <h2 class="font-bold text-slate-800 border-b pb-2">📝 录入上交数量</h2>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">日期</label>
                        <input type="date" id="recDate" class="w-full border rounded-lg px-3 py-2 text-sm">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">零件种类 (可选或手输新种类)</label>
                        <input type="text" id="recPart" list="partOptions" placeholder="如：定距筒" class="w-full border rounded-lg px-3 py-2 text-sm">
                        <datalist id="partOptions"></datalist>
                    </div>
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">上交数量 (件)</label>
                        <input type="number" id="recQty" placeholder="输入数量" min="1" class="w-full border rounded-lg px-3 py-2 text-sm font-bold">
                    </div>
                    <div>
                        <label class="block text-xs text-slate-500 mb-1">备注 (选填)</label>
                        <input type="text" id="recNote" placeholder="批次/机台号" class="w-full border rounded-lg px-3 py-2 text-sm">
                    </div>
                </div>
                <button onclick="submitRecord()" class="w-full bg-slate-900 hover:bg-slate-800 text-white font-medium py-2.5 rounded-lg text-sm transition">
                    + 确认添加交件记录
                </button>
            </div>

            <div class="bg-white p-4 rounded-xl shadow-sm flex flex-col justify-between">
                <div>
                    <div class="flex justify-between items-center border-b pb-2">
                        <h2 class="font-bold text-slate-800">🏷️ 种类单价与分类汇总</h2>
                        <span class="text-xs text-slate-400">修改单价自动保存</span>
                    </div>
                    <div id="priceList" class="divide-y divide-slate-100 mt-2 max-h-52 overflow-y-auto pr-1"></div>
                </div>
                <div class="mt-3 pt-2 border-t flex items-center gap-2">
                    <input type="text" id="newPartName" placeholder="新增其它种类名称..." class="flex-1 border rounded-lg px-2.5 py-1.5 text-xs">
                    <button onclick="addNewPartType()" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-3 py-1.5 rounded-lg text-xs font-medium">新增种类</button>
                </div>
            </div>
        </div>

        <div class="bg-white p-4 rounded-xl shadow-sm">
            <div class="flex flex-wrap justify-between items-center border-b pb-2 mb-3 gap-2">
                <div>
                    <h2 class="font-bold text-slate-800">⏱️ 上班天数与加班核算 (固定 2.5h/次)</h2>
                    <p id="otSummaryText" class="text-xs text-blue-600 font-medium mt-0.5">当前加班：0 次 = 0.0 小时 | 累计补贴 ¥0</p>
                </div>
                <button onclick="syncWorkDaysFromRecords()" class="text-xs bg-blue-50 text-blue-600 hover:bg-blue-100 px-2.5 py-1.5 rounded-md font-medium">
                    按有交件的日期数填入上班天数 (<span id="distinctDaysCount">0</span>天)
                </button>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-5 gap-3">
                <div>
                    <label class="block text-xs text-slate-500 mb-1">上班天数 (天)</label>
                    <input type="number" step="0.5" id="attDays" onchange="saveAttendance()" class="w-full border rounded-lg px-3 py-2 text-sm font-semibold">
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">正班日工时 (小时/天)</label>
                    <input type="number" step="0.5" id="attDailyHours" onchange="saveAttendance()" class="w-full border rounded-lg px-3 py-2 text-sm">
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">加班天数/次数 (次)</label>
                    <div class="flex items-center">
                        <button onclick="adjustOtCount(-1)" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold px-2.5 py-2 rounded-l-lg text-sm">-</button>
                        <input type="number" step="1" min="0" id="attOtCount" onchange="saveAttendance()" class="w-full border-y px-2 py-2 text-center text-sm font-bold text-amber-600">
                        <button onclick="adjustOtCount(1)" class="bg-amber-500 hover:bg-amber-600 text-white font-bold px-2.5 py-2 rounded-r-lg text-sm">+</button>
                    </div>
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">单次加班时长 (小时)</label>
                    <input type="number" step="0.5" id="attHoursPerOt" onchange="saveAttendance()" class="w-full border rounded-lg px-3 py-2 text-sm bg-slate-50">
                </div>
                <div>
                    <label class="block text-xs text-slate-500 mb-1">单次加班补贴 (元)</label>
                    <input type="number" step="1" id="attSubsidy" onchange="saveAttendance()" class="w-full border rounded-lg px-3 py-2 text-sm bg-slate-50">
                </div>
            </div>
        </div>

        <div class="bg-white p-4 rounded-xl shadow-sm">
            <h2 class="font-bold text-slate-800 border-b pb-2 mb-3">📋 交件记录明细</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse text-sm">
                    <thead>
                        <tr class="border-b text-xs text-slate-400">
                            <th class="py-2 px-2">日期</th>
                            <th class="py-2 px-2">种类</th>
                            <th class="py-2 px-2 text-right">上交数量</th>
                            <th class="py-2 px-2 text-right">单价</th>
                            <th class="py-2 px-2 text-right">小计金额</th>
                            <th class="py-2 px-2">备注</th>
                            <th class="py-2 px-2 text-center">操作</th>
                        </tr>
                    </thead>
                    <tbody id="recordTableBody" class="divide-y divide-slate-100"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        let currentState = { records: [], part_types: [], attendance: {}, months: [] };
        let distinctDays = 0;

        document.getElementById('recDate').valueAsDate = new Date();

        async function loadData() {
            const monthSelect = document.getElementById('monthSelect');
            const selectedMonth = monthSelect.value || new Date().toISOString().slice(0, 7);

            const res = await fetch(`/api/data?month=${selectedMonth}`);
            currentState = await res.json();

            monthSelect.innerHTML = `<option value="ALL" ${selectedMonth === 'ALL' ? 'selected' : ''}>全部累计</option>`;
            currentState.months.forEach(m => {
                const selected = m === selectedMonth ? 'selected' : '';
                monthSelect.innerHTML += `<option value="${m}" ${selected}>${m} 月</option>`;
            });

            renderAll();
        }

        function renderAll() {
            const priceMap = {};
            const qtyByPart = {};
            const datalist = document.getElementById('partOptions');
            datalist.innerHTML = '';

            currentState.part_types.forEach(pt => {
                priceMap[pt.name] = pt.unit_price;
                qtyByPart[pt.name] = 0;
                datalist.innerHTML += `<option value="${pt.name}">`;
            });

            let totalQty = 0;
            let pieceWage = 0;
            const dateSet = new Set();

            currentState.records.forEach(r => {
                totalQty += r.quantity;
                dateSet.add(r.record_date);
                qtyByPart[r.part_name] = (qtyByPart[r.part_name] || 0) + r.quantity;
                const p = priceMap[r.part_name] || 0;
                pieceWage += r.quantity * p;
            });

            distinctDays = dateSet.size;
            document.getElementById('distinctDaysCount').innerText = distinctDays;

            const unpricedParts = currentState.part_types.filter(pt => (qtyByPart[pt.name] || 0) > 0 && (!pt.unit_price || pt.unit_price <= 0));
            const alertBox = document.getElementById('unpricedAlert');
            const unpricedList = document.getElementById('unpricedList');
            if (unpricedParts.length > 0) {
                alertBox.classList.remove('hidden');
                unpricedList.innerHTML = unpricedParts.map(pt => `
                    <span class="inline-flex items-center bg-amber-200 text-amber-900 text-xs font-bold px-2.5 py-1 rounded-full">
                        ${pt.name} (已交 ${qtyByPart[pt.name]} 件)
                    </span>
                `).join('');
            } else {
                alertBox.classList.add('hidden');
            }

            const priceListEl = document.getElementById('priceList');
            priceListEl.innerHTML = currentState.part_types.map(pt => {
                const count = qtyByPart[pt.name] || 0;
                const sub = count * (pt.unit_price || 0);
                const isUnpriced = count > 0 && (!pt.unit_price || pt.unit_price <= 0);
                return `
                    <div class="py-2 flex items-center justify-between gap-2 ${isUnpriced ? 'bg-amber-50 px-2 rounded' : ''}">
                        <div class="flex items-center gap-1.5">
                            <span class="font-medium text-sm">${pt.name}</span>
                            <button onclick="promptRename('${pt.name}')" title="改名" class="text-xs text-slate-400 hover:text-blue-600">✎</button>
                            <span class="text-xs text-slate-400">(${count}件 / ¥${sub.toFixed(2)})</span>
                        </div>
                        <div class="flex items-center gap-1">
                            <span class="text-xs text-slate-500">单价¥</span>
                            <input type="number" step="0.01" value="${pt.unit_price}"
                                onchange="updatePrice('${pt.name}', this.value)"
                                class="w-20 border ${isUnpriced ? 'border-amber-500 bg-white ring-2 ring-amber-300' : 'border-slate-300'} rounded px-2 py-1 text-right text-sm font-semibold">
                        </div>
                    </div>
                `;
            }).join('');

            const att = currentState.attendance;
            const otCount = att.overtime_count || 0;
            const hoursPerOt = (att.hours_per_ot !== undefined && att.hours_per_ot !== null) ? att.hours_per_ot : 2.5;
            const subsidyPerOt = (att.subsidy_per_ot !== undefined && att.subsidy_per_ot !== null) ? att.subsidy_per_ot : 10;

            document.getElementById('attDays').value = att.work_days;
            document.getElementById('attDailyHours').value = att.daily_regular_hours;
            document.getElementById('attOtCount').value = otCount;
            document.getElementById('attHoursPerOt').value = hoursPerOt;
            document.getElementById('attSubsidy').value = subsidyPerOt;

            // 自动计算加班总时长与加班补贴
            const totalOtHours = otCount * hoursPerOt;
            const otSubsidy = otCount * subsidyPerOt;
            const totalPay = pieceWage + otSubsidy;
            const dailyWage = att.work_days > 0 ? (totalPay / att.work_days) : 0;
            const regularHours = att.work_days * att.daily_regular_hours;
            const totalHours = regularHours + totalOtHours;
            const hourlyWage = totalHours > 0 ? (totalPay / totalHours) : 0;

            document.getElementById('otSummaryText').innerText =
                `当前加班：${otCount} 次 × ${hoursPerOt}h = ${totalOtHours.toFixed(1)} 小时 | 加班补贴：${otCount} 次 × ¥${subsidyPerOt} = ¥${otSubsidy.toFixed(0)}`;

            document.getElementById('statTotalPay').innerText = `¥${totalPay.toFixed(2)}`;
            document.getElementById('statPaySub').innerText = `计件 ¥${pieceWage.toFixed(1)} + 补贴 ¥${otSubsidy.toFixed(0)}`;
            document.getElementById('statDailyWage').innerText = `¥${dailyWage.toFixed(2)}`;
            document.getElementById('statDaysSub').innerText = `按 ${att.work_days} 天计算`;
            document.getElementById('statHourlyWage').innerText = `¥${hourlyWage.toFixed(2)}`;
            document.getElementById('statHoursSub').innerText = `总工时 ${totalHours.toFixed(1)}h (正班${regularHours.toFixed(1)}h + 加班${totalOtHours.toFixed(1)}h)`;
            document.getElementById('statTotalQty').innerHTML = `${totalQty} <span class="text-sm font-normal">件</span>`;
            document.getElementById('statTypesSub').innerText = `当前记录含 ${Object.values(qtyByPart).filter(v => v > 0).length} 个种类`;

            const tbody = document.getElementById('recordTableBody');
            if (currentState.records.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-slate-400 text-xs">暂无交件记录，请在上方录入</td></tr>`;
            } else {
                tbody.innerHTML = currentState.records.map(r => {
                    const price = priceMap[r.part_name] || 0;
                    const sub = r.quantity * price;
                    return `
                        <tr class="hover:bg-slate-50">
                            <td class="py-2.5 px-2 text-slate-600">${r.record_date}</td>
                            <td class="py-2.5 px-2 font-medium">${r.part_name}</td>
                            <td class="py-2.5 px-2 text-right font-bold">${r.quantity}</td>
                            <td class="py-2.5 px-2 text-right text-slate-500">¥${price.toFixed(3)}</td>
                            <td class="py-2.5 px-2 text-right font-semibold text-emerald-700">¥${sub.toFixed(2)}</td>
                            <td class="py-2.5 px-2 text-slate-400 text-xs">${r.note || '-'}</td>
                            <td class="py-2.5 px-2 text-center">
                                <button onclick="deleteRecord(${r.id})" class="text-red-500 hover:text-red-700 text-xs">删除</button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }

        async function submitRecord() {
            const record_date = document.getElementById('recDate').value;
            const part_name = document.getElementById('recPart').value.trim();
            const quantity = parseInt(document.getElementById('recQty').value, 10);
            const note = document.getElementById('recNote').value.trim();

            if (!record_date || !part_name || !quantity || quantity <= 0) {
                alert('请完整填写日期、种类和大于0的数量！');
                return;
            }
            await fetch('/api/records', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ record_date, part_name, quantity, note })
            });
            document.getElementById('recQty').value = '';
            document.getElementById('recNote').value = '';
            loadData();
        }

        async function deleteRecord(id) {
            if (!confirm('确定删除这条交件记录吗？')) return;
            await fetch(`/api/records/${id}`, { method: 'DELETE' });
            loadData();
        }

        async function updatePrice(part_name, val) {
            const unit_price = parseFloat(val) || 0;
            await fetch('/api/price', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ part_name, unit_price })
            });
            loadData();
        }

        async function addNewPartType() {
            const input = document.getElementById('newPartName');
            const name = input.value.trim();
            if (!name) return;
            await updatePrice(name, 0);
            input.value = '';
        }

        async function promptRename(old_name) {
            const new_name = prompt(`将种类【${old_name}】改名为：`, old_name);
            if (!new_name || new_name.trim() === old_name) return;
            await fetch('/api/part/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ old_name, new_name: new_name.trim() })
            });
            loadData();
        }

        function adjustOtCount(delta) {
            const input = document.getElementById('attOtCount');
            const current = parseInt(input.value, 10) || 0;
            const next = Math.max(0, current + delta);
            input.value = next;
            saveAttendance();
        }

        async function saveAttendance() {
            const month = document.getElementById('monthSelect').value;
            const payload = {
                month,
                work_days: parseFloat(document.getElementById('attDays').value) || 0,
                daily_regular_hours: parseFloat(document.getElementById('attDailyHours').value) || 8,
                overtime_count: parseInt(document.getElementById('attOtCount').value, 10) || 0,
                hours_per_ot: parseFloat(document.getElementById('attHoursPerOt').value) || 2.5,
                subsidy_per_ot: parseFloat(document.getElementById('attSubsidy').value) || 10
            };
            await fetch('/api/attendance', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            loadData();
        }

        function syncWorkDaysFromRecords() {
            document.getElementById('attDays').value = distinctDays;
            saveAttendance();
        }

        loadData();
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE
