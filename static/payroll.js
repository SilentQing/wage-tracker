let rejectionRecordId = null;
let paymentAttempt = null;

function renderPayroll() {
    const month = document.getElementById('monthSelect').value;
    const all = month === 'ALL';
    const info = currentState.reconciliation;
    const totals = currentState.totals;
    const yuan = value => `¥${value.toFixed(2)}`;
    document.getElementById('payrollSummary').textContent =
        `应发 ${yuan(totals.total_pay)}（已扣不合格件 ${yuan(totals.rejection_deduction)}）｜已到账 ${yuan(info.paid)}｜${info.difference < 0 ? '超额' : '未到账'} ${yuan(Math.abs(info.difference))}｜${all ? '累计净额状态：' : ''}${info.status}`;
    document.getElementById('payrollWarning').textContent =
        `${info.pending_prices ? '有待定价交件，应发尚不完整。' : ''}${info.unconfirmed_bonus_months.length ? '存在未确认全勤奖的月份，未确认暂计 0 元；对账状态以当前已记金额为准。' : ''}${all ? '各月多发和少发可能抵消，请按月核对。' : ''}`;
    document.getElementById('bonusStatus').textContent = all ? `累计全勤奖 ${yuan(totals.attendance_bonus)}`
        : info.bonus_awarded === null ? '本月全勤奖：未确认' : info.bonus_awarded ? '本月全勤奖：已确认 ¥100' : '本月全勤奖：已确认无';
    let tooEarly = true;
    if (!all) {
        const [year, m] = month.split('-').map(Number);
        tooEarly = localDateKey() < `${month}-${String(new Date(year, m, 0).getDate()).padStart(2, '0')}`;
    }
    for (const id of ['bonusYes', 'bonusNo']) {
        document.getElementById(id).disabled = all || currentState.locked || overtimeSaving || tooEarly;
        document.getElementById(id).title = tooEarly ? '月底或以后确认' : '';
    }
    document.getElementById('paymentMonthLabel').textContent = all ? '请选择具体工资月份后登记到账。' : `工资所属月份：${month}（可在其他月份到账）`;
    for (const id of ['paymentDate', 'paymentAmount', 'paymentNote', 'paymentSubmit']) {
        document.getElementById(id).disabled = all || overtimeSaving;
    }
    const dateInput = document.getElementById('paymentDate');
    if (!dateInput.value) dateInput.value = localDateKey();
    dateInput.max = localDateKey();
    const list = document.getElementById('paymentList');
    list.replaceChildren();
    for (const payment of currentState.payments) {
        const row = document.createElement('div');
        row.className = 'flex flex-wrap gap-2 border-b py-2 text-sm';
        const label = document.createElement('span');
        label.textContent = `${payment.month}工资 · ${payment.paid_date}到账 ${yuan(payment.amount)} ${payment.note} ${payment.voided ? '（已作废）' : ''}`;
        row.append(label);
        if (!payment.voided) {
            const button = document.createElement('button');
            button.textContent = '作废'; button.disabled = overtimeSaving;
            button.onclick = async () => {
                if (confirm(`作废 ${payment.paid_date} 的 ${yuan(payment.amount)} 到账记录？原记录会保留。`)) {
                    await mutate(`/api/payments/${encodeURIComponent(payment.id)}`, 'DELETE');
                }
            };
            row.append(button);
        }
        list.append(row);
    }
    if (!currentState.payments.length) list.textContent = '尚无到账记录';
}

async function confirmBonus(awarded) {
    const month = document.getElementById('monthSelect').value;
    if (confirm(`确认 ${month} ${awarded ? '获得 100 元全勤奖' : '没有全勤奖'}？`)) {
        await mutate('/api/attendance-bonus', 'PUT', {month, awarded});
    }
}

function openRejection(id) {
    const row = currentState.records.find(item => item.id === id);
    if (!row || currentState.locked_months.includes(row.record_date.slice(0, 7))) {
        showStatus('该交件月份已锁定，请先解锁'); return;
    }
    rejectionRecordId = id;
    document.getElementById('rejectionDescription').textContent = `${row.record_date} ${row.part_name}，原提交 ${row.quantity} 件；按该记录单价扣件。`;
    document.getElementById('rejectionQuantity').value = row.rejected_quantity;
    document.getElementById('rejectionQuantity').max = row.quantity;
    document.getElementById('rejectionReason').value = row.rejection_reason;
    document.getElementById('rejectionError').textContent = '';
    document.getElementById('rejectionDialog').showModal();
}

async function submitRejection(event) {
    event.preventDefault();
    const button = document.getElementById('rejectionSubmit'); button.disabled = true;
    try {
        const ok = await mutate(`/api/records/${rejectionRecordId}/rejection`, 'PUT', {
            quantity: Number(document.getElementById('rejectionQuantity').value),
            reason: document.getElementById('rejectionReason').value
        }, () => document.getElementById('rejectionDialog').close());
        if (!ok) document.getElementById('rejectionError').textContent = document.getElementById('saveStatus').textContent;
    } finally { button.disabled = false; }
}

async function submitPayment(event) {
    event.preventDefault();
    const payload = {
        month: document.getElementById('monthSelect').value,
        paid_date: document.getElementById('paymentDate').value,
        amount: document.getElementById('paymentAmount').value,
        note: document.getElementById('paymentNote').value
    };
    const signature = JSON.stringify(payload);
    if (!paymentAttempt || paymentAttempt.signature !== signature) {
        if (paymentAttempt && !confirm('上一笔保存结果尚未确认。请先核对到账列表；确认要改为登记这笔新内容？')) return;
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        paymentAttempt = {signature, id: Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')};
    }
    const ok = await mutate('/api/payments', 'POST', {...payload, id: paymentAttempt.id}, () => {
        paymentAttempt = null;
        document.getElementById('paymentAmount').value = '';
        document.getElementById('paymentNote').value = '';
        document.getElementById('paymentError').textContent = '';
    });
    if (!ok) document.getElementById('paymentError').textContent = document.getElementById('saveStatus').textContent;
}
