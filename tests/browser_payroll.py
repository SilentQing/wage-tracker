"""Manual browser check: isolated temporary database and headless installed Edge."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright, expect
from browser_submissions import check_submissions

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as temporary:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        env = dict(os.environ, WAGE_DB_PATH=str(Path(temporary) / 'test.db'), WAGE_PASSWORD='', WAGE_WEBDAV_ENABLED='false')
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(url + '/api/data', timeout=1).close()
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Test server did not start')
            request = urllib.request.Request(url + '/api/records', data=json.dumps(dict(record_date='2024-02-10', part_name='轴套', quantity=10, unit_price=2.5)).encode(), headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(request).close()
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', headless=True)
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept())
                page.goto(url)
                expect(page.locator('#saveStatus')).to_have_text('账本已加载')
                page.locator('#monthSelect').select_option('2024-02')
                expect(page.locator('#payrollSummary')).to_contain_text('25.00')
                expect(page.locator('#remoteBackupStatus')).to_contain_text('异地备份未启用')
                expect(page.locator('#remoteBackupRetry')).to_be_disabled()
                expect(page.locator('#dailyAttendanceCalendar')).to_have_count(0)
                expect(page.locator('#overtimeCalendar')).to_have_count(1)
                expect(page.get_by_role('heading', name='⏱️ 上班天数与加班核算', exact=True)).to_have_count(0)
                expect(page.locator('#attHoursPerOt')).not_to_be_visible()
                page.locator('#attendanceSettings > summary').click()
                expect(page.locator('#legacyAttendanceSettings')).to_be_visible()
                expect(page.locator('#legacyOvertimeSettings')).to_be_visible()
                page.locator('#legacyOvertimeSettings button').last.click()
                expect(page.locator('#calendarSummary')).to_contain_text('未分配日期 1 次')
                page.locator('#enableDailyAttendance').click()
                expect(page.locator('#dailyAttendanceSummary')).to_contain_text('246.50 小时')
                expect(page.locator('#legacyAttendanceSettings')).not_to_be_visible()
                expect(page.locator('#legacyOvertimeSettings')).to_be_visible()
                page.locator('#legacyOvertimeSettings button').first.click()
                expect(page.locator('#legacyOvertimeSettings')).not_to_be_visible()
                page.locator('#attHoursPerOt').fill('3')
                page.locator('#attSubsidy').focus()
                expect(page.locator('#saveStatus')).to_have_text('已保存')
                page.locator('#attendanceSettings > summary').click()
                day = page.locator('#overtimeCalendar [data-date="2024-02-10"]')
                day.click()
                expect(page.locator('#customOtHours')).to_have_value('3')
                page.get_by_role('button', name='半天', exact=True).click()
                page.locator('#leaveSave').click()
                expect(page.locator('#leaveError')).to_have_text('请假已保存')
                expect(day).to_contain_text('请假 4.25h')
                page.locator('#customOtHours').fill('2.5')
                page.locator('#customOtSubsidy').fill('0')
                page.locator('#dayOvertimeSave').click()
                expect(page.locator('#dayOvertimeStatus')).to_have_text('加班已保存')
                expect(day).to_contain_text('加班 2.5h')
                expect(day).to_contain_text('正班 4.25h')
                page.locator('#dayClose').click()
                day.click()
                expect(page.locator('#leaveHours')).to_have_value('4.25')
                expect(page.locator('#customOtHours')).to_have_value('2.5')
                page.locator('#dayOvertimeCancel').click()
                expect(page.locator('#dayOvertimeStatus')).to_have_text('加班已取消')
                expect(day).not_to_contain_text('加班')
                page.get_by_role('button', name='恢复出勤', exact=True).click()
                page.locator('#leaveSave').click()
                expect(day).to_contain_text('正班 8.5h')
                page.locator('#dayClose').click()
                expect(page.locator('#overtimeWeeks')).not_to_be_visible()
                page.locator('[data-rejection]').click()
                page.locator('#rejectionQuantity').fill('2')
                page.locator('#rejectionReason').fill('尺寸不合格')
                page.locator('#rejectionSubmit').click()
                expect(page.locator('#payrollSummary')).to_contain_text('应发 ¥20.00')
                page.locator('#bonusYes').click()
                expect(page.locator('#payrollSummary')).to_contain_text('应发 ¥120.00')
                page.locator('#monthLockButton').click()
                expect(page.locator('#monthLockButton')).to_have_text('解锁本月')
                expect(page.locator('#bonusYes')).to_be_disabled()
                expect(day).to_be_disabled()
                page.locator('#attendanceSettings > summary').click()
                expect(page.locator('#attHoursPerOt')).to_be_disabled()
                expect(page.locator('#attSubsidy')).to_be_disabled()
                page.locator('#attendanceSettings > summary').click()
                for value in ['50', '70']:
                    page.locator('#paymentDate').fill('2024-03-10')
                    page.locator('#paymentAmount').fill(value)
                    page.locator('#paymentSubmit').click()
                    expect(page.locator('#paymentAmount')).to_have_value('')
                    expect(page.locator('#paymentSubmit')).to_be_enabled()
                expect(page.locator('#payrollSummary')).to_contain_text('已结清')
                page.locator('#paymentList button').first.click()
                expect(page.locator('#payrollSummary')).to_contain_text('部分到账')
                page.locator('#backupNow').click()
                expect(page.locator('#backupActionStatus')).to_contain_text('备份成功')
                manual = page.locator('#backupFiles > div').filter(has_text='手动').first
                expect(manual).to_be_visible()
                manual.get_by_role('button', name='重新检查').click()
                expect(page.locator('#backupActionStatus')).to_contain_text('复查通过')
                with page.expect_download() as download_event:
                    manual.get_by_role('link', name='下载备份').click()
                backup_file = Path(temporary) / 'download.json'
                download_event.value.save_as(backup_file)
                self_check = json.loads(backup_file.read_text(encoding='utf-8'))
                assert len(self_check['tables']['records']) == 1
                request2 = urllib.request.Request(url + '/api/records', data=json.dumps(dict(record_date='2024-03-10', part_name='轴套', quantity=5, unit_price=2.5)).encode(), headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(request2).close()
                with page.expect_response(lambda response: response.url.endswith('/api/restore') and response.request.method == 'POST') as restored:
                    page.locator('#backupFile').set_input_files(backup_file)
                assert restored.value.status == 200
                expect(page.locator('#saveStatus')).to_have_text('已保存')
                current = json.loads(urllib.request.urlopen(url + '/api/backup').read())
                assert len(current['tables']['records']) == 1
                page.set_viewport_size({'width': 1280, 'height': 900})
                expect(page.locator('#payrollHeading')).to_be_visible()
                # Simulate remote states at the UI boundary; no NAS connection or credentials.
                remote_mode = {'uploaded': False}
                def remote_status(route):
                    data = route.fetch().json()
                    done = remote_mode['uploaded']
                    name = data['files'][0]['name']
                    data['remote'] = dict(enabled=True, configured=True, pending=0 if done else 1,
                        uploading=0, failed=0 if done else 1, next_retry=None,
                        last_success=dict(name=name, time=data['files'][0]['created_at']) if done else None,
                        last_error=None if done else dict(message='WebDAV 认证失败或目录读写权限不足'),
                        files={name: dict(status='uploaded' if done else 'pending', last_error=None)})
                    route.fulfill(json=data)
                def remote_retry(route):
                    remote_mode['uploaded'] = True
                    route.fulfill(json={'queued': 1})
                page.route('**/api/backups', remote_status)
                page.route('**/api/backups/remote/retry', remote_retry)
                page.locator('#backupRefresh').click()
                expect(page.locator('#remoteBackupStatus')).to_contain_text('待重试 1 份')
                expect(page.locator('#remoteBackupRetry')).to_be_enabled()
                page.locator('#remoteBackupRetry').click()
                expect(page.locator('#backupActionStatus')).to_contain_text('重新排队')
                expect(page.locator('#remoteBackupStatus')).to_contain_text('待重试 0 份')
                expect(page.locator('#backupFiles')).to_contain_text('两文件上传及下载校验通过')
                check_submissions(page, url)
                assert not errors, errors
                browser.close()
            print('PASS: calendar, payroll, backup/restore, simulated WebDAV UI, durable submissions with lost requests/responses and reload; no page errors')
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
