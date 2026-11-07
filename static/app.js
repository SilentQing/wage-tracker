let currentState = { records: [], part_types: [], attendance: {}, months: [] };
let distinctDays = 0;
let overtimeSaving = false;
let loadVersion = 0;

function localDateKey(value = new Date()) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

document.getElementById('recDate').value = localDateKey();

async function loadData(requestedMonth) {
    const version = ++loadVersion;
    const monthSelect = document.getElementById('monthSelect');
    const selectedMonth = requestedMonth || monthSelect.value || localDateKey().slice(0, 7);

    const res = await fetch(`/api/data?month=${selectedMonth}`);
    if (!res.ok) throw new Error('读取账本失败，请刷新重试');
    const data = await res.json();
    if (version !== loadVersion) return;
    currentState = data;
    if (selectedMonth !== 'ALL' && !currentState.months.includes(selectedMonth)) {
        currentState.months.push(selectedMonth);
        currentState.months.sort().reverse();
    }

    monthSelect.innerHTML = `<option value="ALL" ${selectedMonth === 'ALL' ? 'selected' : ''}>全部累计</option>`;
    currentState.months.forEach(m => {
        const selected = m === selectedMonth ? 'selected' : '';
        monthSelect.innerHTML += `<option value="${m}" ${selected}>${m} 月</option>`;
    });

    renderAll();
    renderOvertimeCalendar();
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
    const summary = currentState.overtime_summary;
    const otCount = summary.count;
    const hoursPerOt = (att.hours_per_ot !== undefined && att.hours_per_ot !== null) ? att.hours_per_ot : 2.5;
    const subsidyPerOt = (att.subsidy_per_ot !== undefined && att.subsidy_per_ot !== null) ? att.subsidy_per_ot : 10;

    document.getElementById('attDays').value = att.work_days;
    document.getElementById('attDailyHours').value = att.daily_regular_hours;
    document.getElementById('attOtCount').value = att.overtime_count || 0;
    document.getElementById('attHoursPerOt').value = hoursPerOt;
    document.getElementById('attSubsidy').value = subsidyPerOt;

    // 自动计算加班总时长与加班补贴
    const totalOtHours = summary.hours;
    const otSubsidy = summary.subsidy;
    const totalPay = pieceWage + otSubsidy;
    const dailyWage = att.work_days > 0 ? (totalPay / att.work_days) : 0;
    const regularHours = att.work_days * att.daily_regular_hours;
    const totalHours = regularHours + totalOtHours;
    const hourlyWage = totalHours > 0 ? (totalPay / totalHours) : 0;

    document.getElementById('otSummaryText').innerText =
        `当前加班：${otCount} 次（已记日期 ${summary.dated_count} 次，历史 ${summary.undated_count} 次）共 ${totalOtHours.toFixed(1)} 小时 | 加班补贴 ¥${otSubsidy.toFixed(2)}`;

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

function renderOvertimeCalendar() {
    const month = document.getElementById('monthSelect').value;
    const allMonths = month === 'ALL';
    const summary = currentState.overtime_summary;
    document.getElementById('calendarMonth').value = allMonths ? '' : month;
    document.getElementById('calendarMonth').disabled = overtimeSaving;
    document.getElementById('monthSelect').disabled = overtimeSaving;
    document.getElementById('todayOvertime').disabled = overtimeSaving;
    document.querySelectorAll('[onchange="saveAttendance()"], [onclick^="adjustOtCount"], [onclick="syncWorkDaysFromRecords()"]')
        .forEach(element => { element.disabled = allMonths || overtimeSaving; });
    document.getElementById('replaceUndatedLabel').classList.toggle('hidden', allMonths || summary.undated_count === 0);
    if (allMonths || summary.undated_count === 0) document.getElementById('replaceUndated').checked = false;
    document.getElementById('replaceUndated').disabled = overtimeSaving;
    document.getElementById('calendarSummary').textContent =
        `${allMonths ? '全部累计' : month}：已记日期 ${summary.dated_count} 次，未分配日期 ${summary.undated_count} 次，共 ${summary.hours.toFixed(1)} 小时，补贴 ¥${summary.subsidy.toFixed(2)}`;
    const grid = document.getElementById('overtimeCalendar');
    const weeks = document.getElementById('overtimeWeeks');
    grid.innerHTML = '';
    weeks.innerHTML = '';
    if (allMonths) {
        grid.textContent = '请选择月份查看加班日期和每周汇总。';
        return;
    }

    const days = new Map(currentState.overtime_days.map(day => [day.work_date, day]));
    const [year, monthNumber] = month.split('-').map(Number);
    const first = new Date(year, monthNumber - 1, 1, 12);
    first.setDate(first.getDate() - (first.getDay() + 6) % 7);
    const last = new Date(year, monthNumber, 0, 12);
    last.setDate(last.getDate() + (7 - last.getDay()) % 7);
    const today = localDateKey();
    ['一', '二', '三', '四', '五', '六', '日'].forEach(name => {
        const heading = document.createElement('div');
        heading.className = 'text-center text-xs text-slate-500 py-2';
        heading.textContent = `周${name}`;
        grid.appendChild(heading);
    });
    let weekCount = 0;
    let weekHours = 0;
    let weekSubsidy = 0;
    let weekStart = localDateKey(first);
    for (const cursor = new Date(first); cursor <= last; cursor.setDate(cursor.getDate() + 1)) {
        const key = localDateKey(cursor);
        const entry = days.get(key);
        const inMonth = key.startsWith(month);
        const button = document.createElement('button');
        button.type = 'button';
        button.disabled = !inMonth || key > today || overtimeSaving;
        button.setAttribute('aria-pressed', String(Boolean(entry)));
        button.setAttribute('aria-label', `${key}${entry ? ' 已加班，点击取消' : ' 未记录加班，点击勾选'}`);
        button.dataset.date = key;
        button.className = `min-h-16 border rounded-lg py-2 text-center text-xs disabled:cursor-default ${entry ? 'bg-amber-100 border-amber-400 text-amber-900' : 'bg-slate-50 border-slate-200'} ${inMonth ? '' : 'opacity-40'} ${key === today ? 'ring-2 ring-blue-400' : ''}`;
        button.textContent = `${cursor.getDate()}${key === today ? ' 今天' : ''}`;
        const status = document.createElement('span');
        status.className = 'block mt-1 text-xs';
        status.textContent = entry ? `✓ ${entry.hours}h` : '—';
        button.appendChild(status);
        button.addEventListener('click', () => setOvertime(key, !entry));
        grid.appendChild(button);
        if (entry) {
            weekCount += 1;
            weekHours += entry.hours;
            weekSubsidy += entry.subsidy;
        }
        if (cursor.getDay() === 0) {
            const row = document.createElement('div');
            row.className = 'rounded-lg bg-slate-50 px-3 py-2 text-sm';
            row.textContent = `${weekStart} 至 ${key}　加班 ${weekCount} 次 · ${weekHours.toFixed(1)} 小时 · 补贴 ¥${weekSubsidy.toFixed(2)}`;
            weeks.appendChild(row);
            weekCount = 0;
            weekHours = 0;
            weekSubsidy = 0;
            const next = new Date(cursor);
            next.setDate(next.getDate() + 1);
            weekStart = localDateKey(next);
        }
    }
}

async function selectCalendarMonth(month) {
    if (!month || overtimeSaving) return;
    document.getElementById('replaceUndated').checked = false;
    try {
        await loadData(month);
        document.getElementById('calendarStatus').textContent = '';
    } catch (error) {
        document.getElementById('calendarStatus').textContent = error.message;
    }
}

async function setOvertime(workDate, checked, replaceUndated = document.getElementById('replaceUndated').checked) {
    if (overtimeSaving) return;
    overtimeSaving = true;
    renderOvertimeCalendar();
    const status = document.getElementById('calendarStatus');
    status.textContent = '正在保存…';
    try {
        const response = await fetch('/api/overtime', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ work_date: workDate, checked, replace_undated: checked && replaceUndated })
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(typeof error.detail === 'string' ? error.detail : '保存失败，请刷新后重试');
        }
        await loadData(workDate.slice(0, 7));
        status.textContent = `${workDate} ${checked ? '已记录加班' : '已取消加班'}`;
    } catch (error) {
        status.textContent = `${error.message}。如保存结果不确定，请刷新核对，重复勾选不会重复计数。`;
    } finally {
        overtimeSaving = false;
        renderOvertimeCalendar();
    }
}

function markTodayOvertime() {
    // 今日快捷按钮始终新增今日记录，历史日期转换通过日历操作。
    setOvertime(localDateKey(), true, false);
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
    if (month === 'ALL' || overtimeSaving) return;
    const payload = {
        month,
        work_days: parseFloat(document.getElementById('attDays').value) || 0,
        daily_regular_hours: parseFloat(document.getElementById('attDailyHours').value) || 8,
        overtime_count: parseInt(document.getElementById('attOtCount').value, 10) || 0,
        hours_per_ot: Number(document.getElementById('attHoursPerOt').value),
        subsidy_per_ot: Number(document.getElementById('attSubsidy').value)
    };
    overtimeSaving = true;
    renderOvertimeCalendar();
    try {
        const response = await fetch('/api/attendance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!response.ok) throw new Error('考勤参数保存失败，请检查次数、时长与补贴均为非负数');
        await loadData();
    } catch (error) {
        document.getElementById('calendarStatus').textContent = error.message;
    } finally {
        overtimeSaving = false;
        renderOvertimeCalendar();
    }
}

function syncWorkDaysFromRecords() {
    document.getElementById('attDays').value = distinctDays;
    saveAttendance();
}

loadData().catch(error => {
    document.getElementById('calendarStatus').textContent = error.message;
});
