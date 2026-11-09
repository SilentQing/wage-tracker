let backupBusy = false;

async function refreshBackupStatus() {
    try {
        const response = await apiRequest('/api/backups');
        const status = await response.json();
        document.getElementById('backupStatus').textContent =
            `最近成功：${status.last_success ? new Date(status.last_success.time).toLocaleString() : '尚无'}；下次计划：${new Date(status.next_run).toLocaleString()}。${status.last_error ? `最近错误：${status.last_error.message}（${new Date(status.last_error.time).toLocaleString()}）` : '无已记录错误。'}`;
        const remote = status.remote;
        document.getElementById('remoteBackupStatus').textContent = !remote.enabled
            ? '异地备份未启用：配置飞牛 NAS 的 HTTPS WebDAV 后开启。'
            : !remote.configured ? `异地备份配置错误：${remote.configuration_error}`
            : `异地备份：待上传 ${remote.pending} 份，上传中 ${remote.uploading} 份，待重试 ${remote.failed} 份；最近远端校验成功：${remote.last_success ? new Date(remote.last_success.time).toLocaleString() : '尚无'}。${remote.next_retry ? `最早重试：${new Date(remote.next_retry).toLocaleString()}。` : ''}${remote.last_error ? `上传错误：${remote.last_error.message}` : ''}`;
        document.getElementById('remoteBackupRetry').disabled = backupBusy || !remote.configured;
        const list = document.getElementById('backupFiles');
        list.replaceChildren();
        for (const file of status.files) {
            const row = document.createElement('div');
            row.className = 'border-b py-2 text-sm break-words';
            const label = document.createElement('p');
            label.textContent = `${file.kind === 'scheduled' ? '定时' : '手动'} · ${new Date(file.created_at).toLocaleString()} · ${(file.size / 1024).toFixed(1)} KB · ${file.check_ok ? '最近检查通过' : '最近检查失败'} · ${new Date(file.checked_at).toLocaleString()}`;
            row.append(label);
            const remoteLabel = document.createElement('p');
            const job = remote.files[file.name];
            remoteLabel.textContent = job
                ? `远端：${({pending: '等待上传/重试', uploading: '上传并校验中', uploaded: '两文件上传及下载校验通过'})[job.status]}${job.last_error ? `；${job.last_error}` : ''}`
                : remote.configured ? '远端：等待后台加入队列' : '远端：未配置上传';
            row.append(remoteLabel);
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
    document.getElementById('remoteBackupRetry').disabled = true;
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
    return runBackupAction('/api/backups', '本地备份成功，恢复及 SHA256 校验通过；已启用的异地备份由后台上传，请查看独立远端状态');
}

function retryRemoteBackups() {
    return runBackupAction('/api/backups/remote/retry', '待传文件已重新排队，后台将在一分钟内处理；请刷新查看远端结果');
}
