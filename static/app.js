function renderDailyAttendance() {
    const month = document.getElementById('monthSelect').value;
    const att = currentState.attendance;
    const active = Boolean(att.daily_mode);
    const button = document.getElementById('enableDailyAttendance');
    button.hidden = active || month === 'ALL';
    button.disabled = overtimeSaving || currentState.locked;
    document.getElementById('dailyAttendanceSummary').textContent = month === 'ALL'
        ? '请选择具体月份查看出勤日历。'
        : active ? `统计至 ${att.through_date}：日历 ${att.calendar_days} 天，请假 ${att.leave_hours.toFixed(2)} 小时，折算出勤 ${att.work_days.toFixed(2)} 天，正班 ${currentState.totals.regular_hours.toFixed(2)} 小时${currentState.locked ? '（已冻结）' : ''}`
        : '本月保留原月度考勤。启用后以每日 8.5 小时重新核算正班，加班设置保留。';
}

async function enableDailyAttendance() {
    const month = document.getElementById('monthSelect').value;
    if (!confirm(`${month} 将按每天 8.5 小时、截至今天默认出勤重新计算，原月度手填出勤不再参与核算。确认启用？`)) return;
    await mutate('/api/daily-attendance', 'PUT', {month});
}

async function saveLeaveDay(event) {
    event.preventDefault();
    const button = document.getElementById('leaveSave');
    button.disabled = true;
    try {
        const saved = await mutate('/api/leave', 'PUT', {
            work_date: document.getElementById('leaveDate').value,
            hours: Number(document.getElementById('leaveHours').value),
            note: document.getElementById('leaveNote').value
        }, () => { document.getElementById('leaveError').textContent = '请假已保存'; });
        if (!saved) document.getElementById('leaveError').textContent = document.getElementById('saveStatus').textContent;
    } finally { button.disabled = false; }
}

let currentState = { records: [], part_types: [], attendance: {}, months: [] };
let distinctDays = 0;
let overtimeSaving = false;
let loadVersion = 0;
let editingRecordId = null;

function localDateKey(value = new Date()) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[character]);
}

function showStatus(message) {
    document.getElementById('saveStatus').textContent = message;
}

async function apiRequest(url, options = {}) {
    let response;
    try {
        response = await fetch(url, options);
    } catch {
        throw new Error('网络连接中断，保存结果可能不确定，请刷新核对');
    }
    if (!response.ok) {
        let message = `请求失败（${response.status}）`;
        try {
            const error = await response.json();
            message = Array.isArray(error.detail)
                ? error.detail.map(item => `${item.loc.slice(1).join('.')} ${item.msg}`).join('；')
                : error.detail || message;
        } catch { /* 非 JSON 错误响应使用状态码提示。 */ }
        if (response.status === 401) message = '登录已失效，请刷新页面重新登录';
        throw new Error(message);
    }
    return response;
}

function syncPartSelection() {
    const name = document.getElementById('recPart').value.trim();
    const select = document.getElementById('partOptions');
    select.value = Array.from(select.options).some(option => option.value === name) ? name : '';
}

async function loadData(requestedMonth) {
    const version = ++loadVersion;
    const monthSelect = document.getElementById('monthSelect');
    const month = requestedMonth || monthSelect.value || localDateKey().slice(0, 7);
    const response = await apiRequest(`/api/data?month=${encodeURIComponent(month)}`);
    const data = await response.json();
    if (version !== loadVersion) return;
    currentState = data;
    if (month !== 'ALL' && !data.months.includes(month)) data.months.push(month);
    data.months.sort().reverse();
    monthSelect.replaceChildren(new Option('全部累计', 'ALL'));
    data.months.forEach(value => monthSelect.add(new Option(`${value} 月`, value)));
    monthSelect.value = month;
    renderAll();
    renderOvertimeCalendar();
}

async function changeMonth() {
    if (overtimeSaving) return;
    try {
        await loadData();
    } catch (error) {
        showStatus(error.message);
    }
}

async function mutate(url, method, payload, onSaved = () => {}, month) {
    if (!currentState.totals) {
        showStatus('请等待账本加载完成');
        return false;
    }
    if (overtimeSaving) return false;
    overtimeSaving = true;
    renderOvertimeCalendar();
    showStatus('正在保存…');
    let saved = false;
    try {
        await apiRequest(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: payload === undefined ? undefined : JSON.stringify(payload)
        });
        saved = true;
        onSaved();
        await loadData(month);
        showStatus('已保存');
        return true;
    } catch (error) {
        showStatus(`${saved ? '已保存，但刷新失败。' : ''}${error.message}`);
        return false;
    } finally {
        overtimeSaving = false;
        renderOvertimeCalendar();
    }
}

function renderAll() {
    const qtyByPart = Object.create(null);
    const payByPart = Object.create(null);
    const unpriced = new Set();
    const dateSet = new Set();
    const partSelect = document.getElementById('partOptions');
    partSelect.replaceChildren(new Option('请选择已有种类', ''));
    currentState.part_types.forEach(part => partSelect.add(new Option(part.name, part.name)));
    syncPartSelection();
    let totalQty = 0;
    currentState.records.forEach(record => {
        totalQty += record.quantity;
        dateSet.add(record.record_date);
        qtyByPart[record.part_name] = (qtyByPart[record.part_name] || 0) + record.quantity;
        payByPart[record.part_name] = (payByPart[record.part_name] || 0) + record.amount;
        if (record.unit_price === null) unpriced.add(record.part_name);
    });
    distinctDays = dateSet.size;
    document.getElementById('distinctDaysCount').textContent = distinctDays;
    document.getElementById('unpricedAlert').classList.toggle('hidden', unpriced.size === 0);
    document.getElementById('unpricedList').textContent = Array.from(unpriced).join('、');
    document.getElementById('priceList').innerHTML = currentState.part_types.map((part, index) => `
        <div class="py-2 flex items-center justify-between gap-2">
            <div class="min-w-0 break-words">
                <span class="font-medium text-sm">${escapeHtml(part.name)}</span>
                <button data-rename="${index}" data-write class="text-xs text-blue-600" title="改名">✎</button>
                <div class="text-xs text-slate-500">${qtyByPart[part.name] || 0}件 / ¥${(payByPart[part.name] || 0).toFixed(2)}${unpriced.has(part.name) ? ' · 有待定价交件' : ''}</div>
            </div>
            <label class="text-xs text-slate-500 shrink-0">默认¥
                <input type="number" min="0" max="100000000" step="0.000001" value="${part.unit_price}"
                    data-price="${index}" data-write aria-label="${escapeHtml(part.name)}默认单价"
                    class="w-24 border rounded px-2 py-1 text-right text-sm">
            </label>
        </div>`).join('');
    const att = currentState.attendance;
    document.getElementById('attDays').value = att.work_days;
    document.getElementById('attDailyHours').value = att.daily_regular_hours;
    document.getElementById('attOtCount').value = att.overtime_count;
    document.getElementById('attHoursPerOt').value = att.hours_per_ot;
    document.getElementById('attSubsidy').value = att.subsidy_per_ot;
    const summary = currentState.overtime_summary;
    const totals = currentState.totals;
    document.getElementById('otSummaryText').textContent = `当前加班：${summary.count} 次（已记日期 ${summary.dated_count} 次，历史 ${summary.undated_count} 次）共 ${summary.hours.toFixed(1)} 小时 | 加班补贴 ¥${summary.subsidy.toFixed(2)}`;
    document.getElementById('statTotalPay').textContent = `¥${totals.total_pay.toFixed(2)}`;
    document.getElementById('statPaySub').textContent = `合格计件 ¥${totals.piece_wage.toFixed(2)} + 补贴 ¥${summary.subsidy.toFixed(2)} + 全勤 ¥${totals.attendance_bonus.toFixed(2)}`;
    document.getElementById('statDailyWage').textContent = `¥${totals.daily_wage.toFixed(2)}`;
    document.getElementById('statDaysSub').textContent = `按 ${totals.work_days} 天计算`;
    document.getElementById('statHourlyWage').textContent = `¥${totals.hourly_wage.toFixed(2)}`;
    document.getElementById('statHoursSub').textContent = `总工时 ${totals.total_hours.toFixed(1)}h (正班${totals.regular_hours.toFixed(1)}h + 加班${summary.hours.toFixed(1)}h)`;
    document.getElementById('statTotalQty').textContent = `${totalQty} 件`;
    document.getElementById('statTypesSub').textContent = `当前记录含 ${Object.keys(qtyByPart).length} 个种类`;
    document.getElementById('authStatus').textContent = currentState.auth_enabled ? '已启用密码保护' : '应用密码保护未配置';
    const legacyWarning = document.getElementById('legacyWarning');
    legacyWarning.hidden = !currentState.legacy_all_attendance;
    if (currentState.legacy_all_attendance) {
        const legacy = currentState.legacy_all_attendance;
        legacyWarning.textContent = `发现旧版“全部累计”考勤设置（上班 ${legacy.work_days} 天，加班 ${legacy.overtime_count} 次）。原数据已保留在完整备份中，请核对并分配到各月；当前累计统计使用各月考勤，避免重复计数。`;
    }
    renderRecordTable();
    document.getElementById('deletedRecords').innerHTML = currentState.deleted_records.map(record => `
        <div class="flex flex-wrap gap-2 justify-between border-b py-2 text-sm">
            <span>${escapeHtml(record.record_date)} ${escapeHtml(record.part_name)} ${record.quantity}件</span>
            <button data-restore="${record.id}" data-write class="text-blue-600">恢复</button>
        </div>`).join('') || '<p class="text-sm text-slate-500">没有已删除交件</p>';
    document.getElementById('auditLog').replaceChildren(...currentState.audit_log.map(entry => {
        const item = document.createElement('li');
        item.className = 'border-b py-2 break-words';
        item.textContent = `${new Date(entry.created_at).toLocaleString()} ${entry.action} ${entry.detail}`;
        return item;
    }));
}

function renderRecordTable() {
    const query = document.getElementById('recordSearch').value.trim().toLowerCase();
    const records = currentState.records.filter(record =>
        `${record.record_date} ${record.part_name} ${record.note || ''}`.toLowerCase().includes(query));
    document.getElementById('recordTableBody').innerHTML = records.map(record => `
        <tr class="hover:bg-slate-50">
            <td class="py-2.5 px-2">${escapeHtml(record.record_date)}</td>
            <td class="py-2.5 px-2 font-medium">${escapeHtml(record.part_name)}</td>
            <td class="py-2.5 px-2 text-right">提交 ${record.quantity}<br>扣 ${record.rejected_quantity} / 计薪 ${record.payable_quantity}</td>
            <td class="py-2.5 px-2 text-right">${record.unit_price === null ? '待定价' : `¥${record.unit_price.toFixed(6).replace(/0+$/, '').replace(/\.$/, '')}`}</td>
            <td class="py-2.5 px-2 text-right">¥${record.amount.toFixed(2)}</td>
            <td class="py-2.5 px-2 text-xs break-words">${escapeHtml(record.note || '-')}</td>
            <td class="py-2.5 px-2 text-center whitespace-nowrap">
                <button data-edit="${record.id}" data-write class="text-blue-600 text-xs">编辑</button>
                <button data-rejection="${record.id}" data-write class="text-blue-600 text-xs">扣件</button>
                <button data-delete="${record.id}" data-write class="text-red-600 text-xs ml-2">删除</button>
            </td>
        </tr>`).join('') || '<tr><td colspan="7" class="text-center py-6 text-slate-400 text-xs">没有符合条件的交件记录</td></tr>';
}

function renderOvertimeCalendar() {
    if (!currentState.totals) return;
    renderDailyAttendance();
    renderPayroll();
    const month = document.getElementById('monthSelect').value;
    const allMonths = month === 'ALL';
    const locked = currentState.locked;
    const summary = currentState.overtime_summary;
    document.getElementById('calendarMonth').value = allMonths ? '' : month;
    document.getElementById('calendarMonth').disabled = overtimeSaving;
    document.getElementById('monthSelect').disabled = overtimeSaving;
    document.getElementById('todayOvertime').disabled = overtimeSaving;
    document.getElementById('leaveFields').disabled = !currentState.attendance.daily_mode || locked || overtimeSaving;
    document.getElementById('dayOvertimeFields').disabled = locked || overtimeSaving;
    document.getElementById('dayClose').disabled = overtimeSaving;
    document.querySelectorAll('[onchange="saveAttendance()"], [onclick^="adjustOtCount"], [onclick="syncWorkDaysFromRecords()"]')
        .forEach(element => { element.disabled = allMonths || locked || overtimeSaving; });
    document.getElementById('replaceUndatedLabel').classList.toggle('hidden', allMonths || summary.undated_count === 0);
    if (currentState.attendance.daily_mode) {
        document.getElementById('attDays').disabled = true;
        document.getElementById('attDailyHours').disabled = true;
        document.querySelector('[onclick="syncWorkDaysFromRecords()"]').disabled = true;
    }
    if (allMonths || summary.undated_count === 0) document.getElementById('replaceUndated').checked = false;
    document.getElementById('replaceUndated').disabled = locked || overtimeSaving;
    document.querySelectorAll('[data-write]').forEach(element => { element.disabled = overtimeSaving; });
    document.querySelectorAll('#recDate, #recPart, #recQty, #recNote, #recPrice, #partOptions, #newPartName, #customOtDate, #customOtHours, #customOtSubsidy, #customOtDefaults')
        .forEach(element => { element.disabled = overtimeSaving; });
    const lockButton = document.getElementById('monthLockButton');
    lockButton.disabled = allMonths || overtimeSaving;
    lockButton.textContent = locked ? '解锁本月' : '结算锁定本月';
    document.getElementById('monthLockStatus').textContent = locked ? '本月已结算，交件、考勤和加班已锁定' : '本月可编辑';
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
    const first = new Date(`${month}-01T12:00:00`);
    first.setDate(first.getDate() - (first.getDay() + 6) % 7);
    const last = new Date(first);
    last.setFullYear(year, monthNumber, 0);
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
        button.disabled = !inMonth || key > today || locked || overtimeSaving;
        const leave = currentState.leave_days.find(item => item.work_date === key);
        const att = currentState.attendance;
        const attendanceText = !inMonth ? '' : !att.daily_mode ? '月度考勤'
            : key > att.through_date ? '未计入' : `正班 ${Number((8.5 - (leave ? leave.hours : 0)).toFixed(2))}h`;
        button.setAttribute('aria-label', `${key} ${attendanceText}${leave ? ` 请假 ${leave.hours} 小时` : ''}${entry ? ` 加班 ${entry.hours} 小时` : ''}，点击修改当天考勤`);
        button.dataset.date = key;
        button.className = `min-h-16 border rounded-lg py-2 text-center text-xs disabled:cursor-default ${entry ? 'bg-amber-100 border-amber-400 text-amber-900' : 'bg-slate-50 border-slate-200'} ${inMonth ? '' : 'opacity-40'} ${key === today ? 'ring-2 ring-blue-400' : ''}`;
        button.textContent = `${cursor.getDate()}${key === today ? ' 今天' : ''}`;
        const status = document.createElement('span');
        status.className = 'block mt-1 text-xs';
        status.textContent = attendanceText;
        button.appendChild(status);
        if (leave && inMonth) {
            const leaveLabel = document.createElement('span');
            leaveLabel.className = 'block text-xs text-red-600';
            leaveLabel.textContent = `请假 ${leave.hours}h`;
            button.appendChild(leaveLabel);
        }
        if (entry) {
            const overtimeLabel = document.createElement('span');
            overtimeLabel.className = 'block text-xs';
            overtimeLabel.textContent = `加班 ${entry.hours}h`;
            button.appendChild(overtimeLabel);
        }
        button.addEventListener('click', () => openDayEditor(key));
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
    try { await loadData(month); } catch (error) { showStatus(error.message); }
}

async function setOvertime(workDate, checked, replaceUndated = document.getElementById('replaceUndated').checked, overrides = {}) {
    const saved = await mutate('/api/overtime', 'PUT', {
        work_date: workDate, checked, replace_undated: checked && replaceUndated, ...overrides
    }, () => {}, workDate.slice(0, 7));
    if (saved) document.getElementById('calendarStatus').textContent = `${workDate} ${checked ? '已记录加班' : '已取消加班'}`;
    return saved;
}

function markTodayOvertime() {
    setOvertime(localDateKey(), true, false);
}

function openDayEditor(day) {
    if (overtimeSaving || currentState.locked || day > localDateKey()) return;
    const leave = currentState.leave_days.find(item => item.work_date === day);
    document.getElementById('dayEditorTitle').textContent = `${day} 考勤`;
    document.getElementById('leaveDate').value = day;
    document.getElementById('leaveHours').value = leave ? leave.hours : 0;
    document.getElementById('leaveNote').value = leave ? leave.note : '';
    document.getElementById('leaveError').textContent = '';
    document.getElementById('dayOvertimeStatus').textContent = '';
    document.getElementById('leaveModeHint').textContent = currentState.attendance.daily_mode
        ? '全天请假 8.5 小时，半天 4.25 小时；请假与加班分别保存。'
        : '本月尚未启用每日出勤，可修改加班；记录请假前请先在日历上方启用。';
    document.getElementById('customOtDate').value = day;
    fillOvertimeEditor();
    document.getElementById('overtimeDialog').showModal();
}

function fillOvertimeEditor() {
    const day = currentState.overtime_days.find(item => item.work_date === document.getElementById('customOtDate').value);
    document.getElementById('customOtHours').value = day ? day.hours : currentState.attendance.hours_per_ot ?? 2.5;
    document.getElementById('customOtSubsidy').value = day ? day.subsidy : currentState.attendance.subsidy_per_ot ?? 10;
    document.getElementById('customOtDefaults').checked = false;
}

async function saveCustomOvertime(event) {
    event.preventDefault();
    const saved = await setOvertime(document.getElementById('customOtDate').value, true, document.getElementById('replaceUndated').checked, {
        hours: Number(document.getElementById('customOtHours').value),
        subsidy: Number(document.getElementById('customOtSubsidy').value),
        use_month_defaults: document.getElementById('customOtDefaults').checked
    });
    document.getElementById('dayOvertimeStatus').textContent = saved ? '加班已保存' : document.getElementById('saveStatus').textContent;
}

async function cancelDayOvertime() {
    const saved = await setOvertime(document.getElementById('customOtDate').value, false, false);
    document.getElementById('dayOvertimeStatus').textContent = saved ? '加班已取消' : document.getElementById('saveStatus').textContent;
}

function cancelRecordEdit() {
    editingRecordId = null;
    document.getElementById('recordSubmit').textContent = '+ 确认添加交件记录';
    document.getElementById('cancelRecordEdit').hidden = true;
    document.getElementById('recQty').value = '';
    document.getElementById('recNote').value = '';
    document.getElementById('recPrice').value = '';
}

function editRecord(id) {
    if (overtimeSaving) return;
    const record = currentState.records.find(item => item.id === id);
    if (!record) return;
    editingRecordId = id;
    document.getElementById('recDate').value = record.record_date;
    document.getElementById('recPart').value = record.part_name;
    document.getElementById('recQty').value = record.quantity;
    document.getElementById('recNote').value = record.note;
    document.getElementById('recPrice').value = record.unit_price ?? '';
    syncPartSelection();
    document.getElementById('recordSubmit').textContent = '保存交件修改';
    document.getElementById('cancelRecordEdit').hidden = false;
    document.getElementById('recDate').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function submitRecord() {
    const payload = {
        record_date: document.getElementById('recDate').value,
        part_name: document.getElementById('recPart').value.trim(),
        quantity: Number(document.getElementById('recQty').value),
        note: document.getElementById('recNote').value.trim()
    };
    const price = document.getElementById('recPrice').value;
    if (price !== '') payload.unit_price = price;
    if (!payload.record_date || !payload.part_name || !Number.isInteger(payload.quantity) || payload.quantity <= 0) {
        showStatus('请填写日期、种类和大于0的整数数量');
        return;
    }
    const id = editingRecordId;
    await mutate(id === null ? '/api/records' : `/api/records/${id}`, id === null ? 'POST' : 'PUT',
        payload, cancelRecordEdit, payload.record_date.slice(0, 7));
}

async function deleteRecord(id) {
    if (confirm('将这条交件移入已删除记录？之后可恢复。')) await mutate(`/api/records/${id}`, 'DELETE');
}

async function updatePrice(part_name, value) {
    if (value === '' || !Number.isFinite(Number(value)) || Number(value) < 0) {
        showStatus('单价应为非负数');
        return false;
    }
    return mutate('/api/price', 'POST', { part_name, unit_price: value });
}

async function addNewPartType() {
    const input = document.getElementById('newPartName');
    const name = input.value.trim();
    if (name && await updatePrice(name, 0)) input.value = '';
}

async function promptRename(old_name) {
    const new_name = prompt(`将种类【${old_name}】改名为：`, old_name);
    if (new_name && new_name.trim() !== old_name) await mutate('/api/part/rename', 'POST', { old_name, new_name: new_name.trim() });
}

function adjustOtCount(delta) {
    const input = document.getElementById('attOtCount');
    input.value = Math.max(0, Number(input.value) + delta);
    saveAttendance();
}

async function saveAttendance() {
    const month = document.getElementById('monthSelect').value;
    if (month === 'ALL' || currentState.locked) return;
    await mutate('/api/attendance', 'POST', {
        month,
        work_days: Number(document.getElementById('attDays').value),
        daily_regular_hours: Number(document.getElementById('attDailyHours').value),
        overtime_count: Number(document.getElementById('attOtCount').value),
        hours_per_ot: Number(document.getElementById('attHoursPerOt').value),
        subsidy_per_ot: document.getElementById('attSubsidy').value
    });
}

function syncWorkDaysFromRecords() {
    document.getElementById('attDays').value = distinctDays;
    saveAttendance();
}

async function toggleMonthLock() {
    const month = document.getElementById('monthSelect').value;
    if (month === 'ALL') return;
    if (confirm(`${currentState.locked ? '解锁后可以修改' : '结算后将锁定'} ${month} 的交件、考勤和加班，确认继续？`)) {
        await mutate('/api/month-lock', 'PUT', { month, locked: !currentState.locked });
    }
}

function downloadExport(kind) {
    const month = document.getElementById('monthSelect').value;
    window.location.assign(`/api/export?month=${encodeURIComponent(month)}&kind=${kind}`);
}

async function restoreBackup(input) {
    const file = input.files[0];
    if (!file) return;
    try {
        if (file.size > 64 * 1024 * 1024) throw new Error('备份文件不能超过64MB');
        const payload = JSON.parse(await file.text());
        showStatus('正在检查备份并进行临时数据库试恢复…');
        const response = await apiRequest('/api/restore/preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
        const preview = await response.json();
        const message = `备份检查通过\n生成时间：${preview.created_at}\n月份：${preview.months.join('、')}\n交件：${preview.counts.records} 条（含已删除）\n加班日期：${preview.counts.overtime_days} 条\n到账记录：${preview.counts.payments} 条\n累计应发：¥${preview.totals.total_pay.toFixed(2)}\n累计到账：¥${preview.paid.toFixed(2)}\n\n恢复将覆盖当前完整账本，服务器会先保存当前账本安全备份。确认恢复？`;
        if (!confirm(message)) { showStatus('已取消恢复，账本未修改'); return; }
        await mutate('/api/restore', 'POST', payload, cancelRecordEdit);
    } catch (error) {
        showStatus(`恢复失败。${error.message}`);
    } finally {
        input.value = '';
    }
}

document.getElementById('priceList').addEventListener('click', event => {
    const button = event.target.closest('[data-rename]');
    if (button) promptRename(currentState.part_types[Number(button.dataset.rename)].name);
});
document.getElementById('priceList').addEventListener('change', event => {
    const input = event.target.closest('[data-price]');
    if (input) updatePrice(currentState.part_types[Number(input.dataset.price)].name, input.value);
});
document.getElementById('recordTableBody').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.edit) editRecord(Number(button.dataset.edit));
    if (button.dataset.rejection) openRejection(Number(button.dataset.rejection));
    if (button.dataset.delete) deleteRecord(Number(button.dataset.delete));
});
document.getElementById('deletedRecords').addEventListener('click', event => {
    const button = event.target.closest('[data-restore]');
    if (button) mutate(`/api/records/${Number(button.dataset.restore)}/restore`, 'POST');
});
document.getElementById('recDate').value = localDateKey();
loadData().then(() => showStatus('账本已加载')).catch(error => showStatus(error.message));
refreshBackupStatus();
