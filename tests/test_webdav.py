import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webdav_client import WebDAVAdapter, WebDAVError

SETTINGS = dict(WAGE_WEBDAV_ENABLED='true', WAGE_WEBDAV_URL='https://nas.example.test/dav/工资/',
                WAGE_WEBDAV_USERNAME='backup', WAGE_WEBDAV_PASSWORD='private-password')


class FakeConnection:
    files = {}
    requests = []
    failure = None
    corrupt = False
    sock = None

    def __init__(self, host, port, timeout, context):
        assert host == 'nas.example.test' and port == 443 and timeout == 20
        assert context.check_hostname

    def request(self, method, path, body, headers):
        self.method, self.path = method, path
        self.requests.append((method, path, headers))
        if method == 'PUT':
            self.files[path] = body

    def getresponse(self):
        response = io.BytesIO((b'corrupt' if self.corrupt else self.files[self.path]) if self.method == 'GET' else b'')
        response.status = self.failure or (201 if self.method == 'PUT' else 200)
        return response

    def close(self):
        pass


class WebDAVTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, SETTINGS)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'backup.json'
        raw = b'{"tables":{}}'
        self.digest = hashlib.sha256(raw).hexdigest()
        self.path.write_bytes(raw)
        self.path.with_suffix('.meta.json').write_text(json.dumps(dict(name=self.path.name, sha256=self.digest)))
        FakeConnection.files, FakeConnection.requests = {}, []
        FakeConnection.failure, FakeConnection.corrupt = None, False

    @patch('webdav_client.http.client.HTTPSConnection', FakeConnection)
    def test_uploads_and_verifies_both_files(self):
        result = WebDAVAdapter().upload(self.path, self.digest)
        self.assertEqual(result['sha256'], self.digest)
        self.assertEqual([row[0] for row in FakeConnection.requests], ['PUT', 'GET', 'PUT', 'GET'])
        self.assertIn('/dav/%E5%B7%A5%E8%B5%84/backup.json', FakeConnection.files)
        self.assertTrue(all(row[2]['Authorization'].startswith('Basic ') for row in FakeConnection.requests))

    @patch('webdav_client.http.client.HTTPSConnection', FakeConnection)
    def test_redirect_auth_and_remote_corruption_are_failures(self):
        for status in [302, 401, 403, 404, 409, 500]:
            FakeConnection.failure = status
            with self.assertRaises(WebDAVError) as error:
                WebDAVAdapter().upload(self.path, self.digest)
            self.assertNotIn('private-password', str(error.exception))
            self.assertNotIn('nas.example.test', str(error.exception))
        FakeConnection.failure, FakeConnection.corrupt = None, True
        with self.assertRaises(WebDAVError):
            WebDAVAdapter().upload(self.path, self.digest)

    def test_disabled_invalid_configuration_and_local_corruption_never_connect(self):
        with patch('webdav_client.http.client.HTTPSConnection') as connection:
            with patch.dict(os.environ, {'WAGE_WEBDAV_ENABLED': 'false'}):
                with self.assertRaises(WebDAVError):
                    WebDAVAdapter().upload(self.path, self.digest)
            for url in ['http://nas.test/dav', 'https://user:pass@nas.test/dav', 'https://nas.test/dav?token=private', 'https://nas.test/#secret', 'https://nas.test:bad/']:
                with patch.dict(os.environ, {'WAGE_WEBDAV_URL': url}):
                    adapter = WebDAVAdapter()
                    self.assertFalse(adapter.ready)
                    with self.assertRaises(WebDAVError):
                        adapter.upload(self.path, self.digest)
            self.path.write_bytes(b'bad')
            with self.assertRaises(WebDAVError):
                WebDAVAdapter().upload(self.path, self.digest)
            connection.assert_not_called()

    def test_connection_timeout_is_sanitized(self):
        with patch('webdav_client.http.client.HTTPSConnection') as connection:
            connection.return_value.request.side_effect = TimeoutError('private-password nas.example.test')
            with self.assertRaises(WebDAVError) as error:
                WebDAVAdapter().upload(self.path, self.digest)
            self.assertNotIn('private-password', str(error.exception))
            connection.return_value.close.assert_called_once()

    def test_second_file_failure_and_password_rotation(self):
        adapter = WebDAVAdapter()
        with patch.dict(os.environ, {'WAGE_WEBDAV_PASSWORD': 'rotated-password'}):
            self.assertEqual(adapter.destination, WebDAVAdapter().destination)
        with patch.dict(os.environ, {'WAGE_WEBDAV_URL': 'https://nas.example.test/other/'}):
            self.assertNotEqual(adapter.destination, WebDAVAdapter().destination)
        with patch.object(adapter, '_request', side_effect=[None, None, None, WebDAVError('WebDAV 远端文件大小或摘要不匹配')]) as request:
            with self.assertRaises(WebDAVError):
                adapter.upload(self.path, self.digest)
            self.assertEqual(request.call_count, 4)
