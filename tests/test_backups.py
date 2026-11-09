import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backup_service import CHINA, WebDAVAdapter
from test_overtime import LedgerTestCase


class BackupTests(LedgerTestCase):
    def service(self):
        return self.module.make_backup_service()

    def seed(self):
        self.client.post('/api/records', json=dict(record_date='2024-02-10', part_name='轴套', quantity=10, unit_price=2))

    def test_local_backup_download_recheck_and_preview_without_mutation(self):
        self.seed()
        created = self.client.post('/api/backups')
        self.assertEqual(created.status_code, 200, created.text)
        name = created.json()['name']
        downloaded = self.client.get(f'/api/backups/{name}/download')
        self.assertEqual(downloaded.status_code, 200)
        payload = downloaded.json()
        self.seed()
        preview = self.client.post('/api/restore/preview', json=payload)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()['totals']['total_pay'], 20)
        self.assertEqual(self.data()['totals']['total_pay'], 40)
        self.assertEqual(self.client.post(f'/api/backups/{name}/check').status_code, 200)
        self.assertEqual(self.client.post('/api/restore', json=payload).status_code, 200)
        self.assertEqual(self.data()['totals']['total_pay'], 20)
        self.assertEqual(self.client.get('/api/backups').json()['webdav'], 'disabled')

    def test_schedule_timezone_catchup_and_no_duplicate(self):
        service = self.service()
        before = datetime(2026, 11, 8, 2, 59, tzinfo=CHINA)
        self.assertTrue(service.due_slot(before).startswith('2026-11-07T03:00'))
        service.create(scheduled=True, now=before)
        self.assertTrue(service.create(scheduled=True, now=before)['skipped'])
        after = before + timedelta(minutes=1)
        service.create(scheduled=True, now=after)
        self.assertEqual(len(service.entries()), 2)
        service.create(scheduled=True, now=after + timedelta(days=4))
        self.assertEqual(len(service.entries()), 3)  # one catch-up, no fabricated past snapshots

    def test_retention_protects_manual_and_safety_backups(self):
        service = self.service()
        manual = service.create()
        safety = service.directory / 'before-restore-protected.json'
        safety.write_text('{}')
        start = datetime(2026, 9, 1, 3, tzinfo=CHINA)
        for index in range(32):
            service.create(scheduled=True, now=start + timedelta(days=index))
        self.assertEqual(len([item for item in service.entries() if item['kind'] == 'scheduled']), 30)
        self.assertTrue((service.directory / manual['name']).exists())
        self.assertTrue(safety.exists())

    def test_failed_backup_keeps_old_backup_and_retries_slot(self):
        service = self.service()
        old = service.create()
        now = datetime(2026, 11, 8, 3, tzinfo=CHINA)
        with patch.object(service, 'capture', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                service.create(scheduled=True, now=now)
        self.assertTrue((service.directory / old['name']).exists())
        self.assertIsNotNone(service.status()['last_error'])
        service.create(scheduled=True, now=now)
        self.assertIsNone(service.status()['last_error'])

    def test_corruption_detected_and_not_downloaded(self):
        service = self.service()
        entry = service.create()
        (service.directory / entry['name']).write_text('{broken', encoding='utf-8')
        self.assertEqual(self.client.get(f"/api/backups/{entry['name']}/download").status_code, 400)
        self.assertEqual(self.client.post(f"/api/backups/{entry['name']}/check").status_code, 400)
        self.assertFalse(service.entries()[0]['check_ok'])
        self.assertIsNotNone(service.status()['last_error'])
        with self.assertRaises(ValueError):
            service.read_verified('../app.py')

    def test_concurrent_schedulers_create_one_snapshot(self):
        now = datetime(2026, 11, 8, 3, tzinfo=CHINA)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.service().create(scheduled=True, now=now), range(2)))
        self.assertEqual(sum(bool(item.get('skipped')) for item in results), 1)
        self.assertEqual(len(self.service().entries()), 1)

    def test_invalid_preview_and_body_limit_preserve_ledger(self):
        self.seed()
        backup = self.client.get('/api/backup').json()
        duplicate = copy.deepcopy(backup)
        duplicate['tables']['records'].append(duplicate['tables']['records'][0])
        self.assertEqual(self.client.post('/api/restore/preview', json=duplicate).status_code, 400)
        with patch.object(self.module, 'MAX_BACKUP_BYTES', 20):
            self.assertEqual(self.client.post('/api/restore', json=backup).status_code, 413)
            self.assertEqual(self.client.post('/api/restore/preview', json=backup).status_code, 413)
        self.assertEqual(self.data()['totals']['total_pay'], 20)

    def test_remote_adapter_is_disabled_by_default(self):
        from webdav_client import WebDAVError
        with self.assertRaises(WebDAVError):
            WebDAVAdapter().upload(Path('unused'), 'unused')
        with patch.dict('os.environ', {'WAGE_WEBDAV_PASSWORD': 'secret-test-password', 'WAGE_WEBDAV_URL': 'https://example.invalid/dav'}):
            entry = self.service().create()
            self.assertNotIn('secret-test-password', json.dumps(entry))
            self.assertNotIn('example.invalid', json.dumps(self.service().status()))
