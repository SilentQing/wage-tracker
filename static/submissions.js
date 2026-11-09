const submissionPrefix = 'wage.pending.v1.';

function readPendingSubmissions() {
    const pending = [];
    for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (!key.startsWith(submissionPrefix)) continue;
        const item = JSON.parse(localStorage.getItem(key));
        if (!item || !['records', 'payments'].includes(item.kind) || !/^[a-zA-Z0-9-]{16,64}$/.test(item.id)
            || !/^[a-zA-Z0-9-]{16,64}$/.test(item.epoch) || !item.payload || typeof item.payload !== 'object'
            || key !== submissionPrefix + item.kind + '.' + item.id) {
            throw new Error('待确认提交数据损坏，请先保留浏览器数据并核对账本');
        }
        pending.push(item);
    }
    return pending;
}

function submissionKey(item) {
    return submissionPrefix + item.kind + '.' + item.id;
}

function renderPendingSubmissions() {
    const section = document.getElementById('pendingSubmissions');
    const list = document.getElementById('pendingSubmissionList');
    list.replaceChildren();
    try {
        const pending = readPendingSubmissions();
        section.hidden = pending.length === 0;
        for (const item of pending) {
            const row = document.createElement('div');
            row.className = 'border-b py-2 space-y-2 text-sm break-words';
            row.dataset.submission = item.id;
            const label = document.createElement('p');
            const payload = item.payload;
            label.textContent = item.kind === 'records'
                ? `待确认交件：${payload.record_date} ${payload.part_name} ${payload.quantity}件；单价 ${payload.unit_price ?? '默认价'}；备注 ${payload.note || '无'}`
                : `待确认到账：${payload.month}工资，${payload.paid_date}到账 ${payload.amount}元；备注 ${payload.note || '无'}`;
            row.append(label);
            const stale = currentState.submission_epoch && item.epoch !== currentState.submission_epoch;
            if (stale) {
                const warning = document.createElement('p');
                warning.textContent = '账本已恢复或切换：禁止重试这笔旧提交，请人工核对现有账目。';
                row.append(warning);
            }
            for (const [action, title] of stale ? [['forget', '已人工核对，移除旧提示']]
                : [['check', '核对结果'], ['retry', '安全重试'], ['cancel', '取消本次提交']]) {
                const button = document.createElement('button');
                button.textContent = title;
                button.className = 'border rounded-lg px-3 py-2';
                button.disabled = overtimeSaving || !currentState.submission_epoch;
                button.onclick = () => processPendingSubmission(item, action);
                row.append(button);
            }
            list.append(row);
        }
    } catch {
        section.hidden = false;
        list.textContent = '浏览器待确认存储不可用或数据损坏，已停止新增交件和到账提交；请先核对账本，勿直接清除未决数据。';
    }
}

function clearMatchingSubmissionForm(item) {
    const current = item.kind === 'records' ? recordSubmissionPayload() : paymentSubmissionPayload();
    if (JSON.stringify(current) !== JSON.stringify(item.payload)) return;
    if (item.kind === 'records') {
        if (editingRecordId === null) cancelRecordEdit();
    } else {
        document.getElementById('paymentAmount').value = '';
        document.getElementById('paymentNote').value = '';
        document.getElementById('paymentError').textContent = '';
    }
}

async function processPendingSubmission(item, action) {
    if (overtimeSaving || !currentState.totals) return;
    if (action === 'cancel' && !confirm('取消这次尚未保存的提交？服务器会阻止迟到的原请求；已保存的记录不会删除。')) return;
    if (action === 'forget') {
        if (item.epoch === currentState.submission_epoch) return;
        if (confirm('确认已人工核对恢复后的账本？仅移除本机旧提示，不补交或删除任何账目。')) {
            try { localStorage.removeItem(submissionKey(item)); }
            catch { showStatus('无法移除本机提示，请检查浏览器存储'); }
            renderPendingSubmissions();
        }
        return;
    }
    overtimeSaving = true;
    renderOvertimeCalendar();
    showStatus('正在核对提交…');
    let confirmed = false;
    try {
        const stored = localStorage.getItem(submissionKey(item));
        if (!stored) { showStatus('该待确认提示已由其他页面处理，请核对账本'); return; }
        if (JSON.stringify(JSON.parse(stored)) !== JSON.stringify(item)) throw new Error('待确认内容已改变，请刷新后核对');
        const url = `/api/submissions/${item.kind}/${encodeURIComponent(item.id)}`;
        let result;
        if (action === 'cancel') {
            result = await (await apiRequest(`${url}/cancel?epoch=${encodeURIComponent(item.epoch)}`, {method: 'POST'})).json();
        } else {
            result = action === 'send' ? {state: 'absent'}
                : await (await apiRequest(`${url}?epoch=${encodeURIComponent(item.epoch)}`)).json();
            if (result.state === 'absent' && action !== 'check') {
                const payload = {...item.payload, submission_epoch: item.epoch};
                payload[item.kind === 'records' ? 'request_id' : 'id'] = item.id;
                result = await (await apiRequest(`/api/${item.kind}`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
                })).json();
            }
        }
        if (result.state === 'absent') {
            showStatus('服务器尚未查到这次提交。未自动补交；可安全重试，或取消本次提交。');
            return;
        }
        if (!['saved', 'cancelled'].includes(result.state)) throw new Error('服务器未返回明确结果，待确认内容已保留');
        confirmed = true;
        localStorage.removeItem(submissionKey(item));
        if (result.state === 'saved') clearMatchingSubmissionForm(item);
        const month = item.kind === 'records' ? item.payload.record_date.slice(0, 7) : item.payload.month;
        await loadData(month);
        showStatus(result.state === 'cancelled' ? '本次提交已取消，迟到请求不会入账。'
            : result.deleted ? '原交件已保存，但现已删除；未重新新增或恢复。'
            : result.voided ? '原到账已保存，但现已作废；未重新入账。'
            : '已保存（已核对提交结果，不会重复入账）');
    } catch (error) {
        showStatus(`${confirmed ? '服务器已确认，但本机清理或刷新失败。' : '结果待确认，提交内容和标识已保留。'}${error.message}`);
    } finally {
        overtimeSaving = false;
        renderOvertimeCalendar();
    }
}

async function submitDurable(kind, payload) {
    if (overtimeSaving || !currentState.submission_epoch) return;
    try {
        if (readPendingSubmissions().some(item => item.kind === kind)) {
            showStatus('已有同类提交结果待确认，请先在上方核对、安全重试或取消；原内容未被覆盖。');
            renderPendingSubmissions();
            document.getElementById('pendingSubmissions').scrollIntoView({block: 'center'});
            return;
        }
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        const item = {kind, payload, epoch: currentState.submission_epoch,
            id: Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')};
        const serialized = JSON.stringify(item);
        localStorage.setItem(submissionKey(item), serialized);
        if (localStorage.getItem(submissionKey(item)) !== serialized) throw new Error('写入验证失败');
        await processPendingSubmission(item, 'send');
    } catch {
        showStatus('浏览器无法可靠保存待确认内容，已停止提交；请检查浏览器存储设置后再试。');
        renderPendingSubmissions();
    }
}

window.addEventListener('storage', event => {
    if (event.key === null || event.key.startsWith(submissionPrefix)) renderPendingSubmissions();
});
