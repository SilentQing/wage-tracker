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

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as temporary:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        env = dict(os.environ, WAGE_DB_PATH=str(Path(temporary) / 'test.db'), WAGE_PASSWORD='')
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
                assert not errors, errors
                browser.close()
            print('PASS: mobile and desktop, payroll, local backup, recheck, download, preview and restore; no page errors')
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
