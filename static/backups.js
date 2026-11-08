let backupBusy = false;

async function refreshBackupStatus() {
    try {
        const response = await apiRequest('/api/backups');
        const status = await response.json();
        document.getElementById('backupStatus').textContent =
            `最近成功：${status.last_success ? new Date(status.last_success.time).toLocaleString() : '尚无'}；下次计划：${new Date(status.next_run).toLocaleString()}。${status.last_error ? `最近错误：${status.last_error.message}（${new Date(status.last_error.time).toLocaleString()}）` : '无已记录错误。'}`;
        const list = document.getElementById('backupFiles');
        list.replaceChildren();
        for (const file of status.files) {
            const row = document.createElement('div');
            row.className = 'border-b py-2 text-sm break-words';
            const label = document.createElement('p');
            label.textContent = `${file.kind === 'scheduled' ? '定时' : '手动'} · ${new Date(file.created_at).toLocaleString()} · ${(file.size / 1024).toFixed(1)} KB · ${file.check_ok ? '最近检查通过' : '最近检查失败'} · ${new Date(file.checked_at).toLocaleString()}`;
            row.append(label);
            const link = document.createElement('a');
            link.href = `/api/backups/${encodeURIComponent(file.name)}/download`;
            link.download = file.name; link.textContent = '下载备份';
            link.className = 'text-blue-600'; row.append(link);
            const button = document.createElement('button');
            button.textContent = '重新检查'; button.disabled = backupBusy;
            button.className = 'border rounded-lg px-3 py-2';
            button.onclick = () => runBackupAction(`/api/backups/${encodeURIComponent(file.name)}/check`, '复查通过：文件摘要、试恢复及核算检查完成');
            row.append(button); list.append(row);
        }
        if (!status.files.length) list.textContent = '尚无可下载的定时或手动备份';
    } catch (error) {
        document.getElementById('backupStatus').textContent = `读取备份状态失败：${error.message}`;
    }
}

async function runBackupAction(url, success) {
    if (backupBusy) return;
    backupBusy = true;
    document.getElementById('backupNow').disabled = true;
    document.getElementById('backupRefresh').disabled = true;
    document.getElementById('backupActionStatus').textContent = '正在备份或检查，请稍候…';
    try {
        await apiRequest(url, {method: 'POST'});
        document.getElementById('backupActionStatus').textContent = success;
    } catch (error) {
        document.getElementById('backupActionStatus').textContent = error.message;
    } finally {
        backupBusy = false;
        document.getElementById('backupNow').disabled = false;
        document.getElementById('backupRefresh').disabled = false;
        await refreshBackupStatus();
    }
}

function createLocalBackup() {
    return runBackupAction('/api/backups', '备份成功，临时数据库恢复检查及 SHA256 校验通过');
}
