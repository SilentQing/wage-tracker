import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import threading
from unittest.mock import patch

from backup_service import CHINA
from test_overtime import LedgerTestCase
from test_webdav import SETTINGS
from webdav_client import WebDAVError


class RemoteBackupTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, SETTINGS)
        env.start()
        self.addCleanup(env.stop)

    def service(self):
        return self.module.make_backup_service()

    def test_failure_retry_restart_and_independent_local_state(self):
        service = self.service()
        entry = service.create()
        now = datetime.now(CHINA)
        with patch.object(service.remote, 'upload', side_effect=WebDAVError('WebDAV 认证失败或目录读写权限不足')):
            self.assertFalse(service.sync_remote(now)['uploaded'])
        status = service.status()
        self.assertIsNone(status['last_error'])
        self.assertEqual(status['last_success']['name'], entry['name'])
        self.assertEqual(status['remote']['failed'], 1)
        self.assertNotIn('private-password', json.dumps(status))
        self.assertNotIn('nas.example.test', json.dumps(status))
        restarted = self.service()
        with patch.object(restarted.remote, 'upload', return_value={}) as upload:
            self.assertTrue(restarted.sync_remote(now + timedelta(seconds=30))['skipped'])
            upload.assert_not_called()
            self.assertTrue(restarted.sync_remote(now + timedelta(seconds=61))['uploaded'])
            self.assertTrue(restarted.sync_remote(now + timedelta(seconds=62))['skipped'])
            upload.assert_called_once()
        self.assertEqual(restarted.remote_status()['last_success']['name'], entry['name'])
        self.assertIsNone(restarted.remote_status()['last_error'])

    def test_pending_backups_survive_retention_and_disabled_uploads(self):
        service = self.service()
        start = datetime(2026, 9, 1, 3, tzinfo=CHINA)
        for day in range(32):
            service.create(scheduled=True, now=start + timedelta(days=day))
        self.assertEqual(len(service.entries()), 32)
        with patch.dict(os.environ, {'WAGE_WEBDAV_ENABLED': 'false'}):
            self.service().prune()
            self.assertEqual(len(service.entries()), 32)
        with closing(service.connect()) as conn:
            conn.execute("UPDATE remote_jobs SET status='uploaded'")
            conn.commit()
        service.prune()
        self.assertEqual(len(service.entries()), 30)

    def test_concurrent_worker_claim_and_expired_lease(self):
        service = self.service()
        service.create()
        entered, release = threading.Event(), threading.Event()
        def upload(*args):
            entered.set()
            self.assertTrue(release.wait(5))
        with patch.object(service.remote, 'upload', side_effect=upload) as mocked:
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(service.sync_remote)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertTrue(self.service().sync_remote()['skipped'])
                finally:
                    release.set()
                self.assertTrue(future.result()['uploaded'])
            mocked.assert_called_once()
        with closing(service.connect()) as conn:
            conn.execute("UPDATE remote_jobs SET status='uploading',lease_until=0,token='crashed'")
            conn.commit()
        with patch.object(service.remote, 'upload', return_value={}):
            self.assertTrue(service.sync_remote()['uploaded'])

    def test_manual_retry_existing_files_config_errors_and_corrupt_source(self):
        with patch.dict(os.environ, {'WAGE_WEBDAV_ENABLED': 'false'}):
            entry = self.service().create()
            self.assertEqual(self.client.post('/api/backups/remote/retry').status_code, 409)
        response = self.client.post('/api/backups/remote/retry')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['queued'], 1)
        service = self.service()
        service.safe_path(entry['name']).write_bytes(b'broken')
        with patch.object(service.remote, 'upload') as upload:
            self.assertFalse(service.sync_remote()['uploaded'])
            upload.assert_not_called()
        with patch.dict(os.environ, {'WAGE_WEBDAV_URL': 'http://invalid'}):
            invalid = self.service()
            invalid.create()  # Invalid remote configuration cannot stop local backups.
            self.assertEqual(invalid.status()['webdav'], 'configuration_error')
            self.assertIsNone(invalid.status()['last_error'])
