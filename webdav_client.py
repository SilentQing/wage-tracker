"""Explicitly enabled HTTPS WebDAV uploads, verified by downloading both files."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import time
from urllib.parse import quote, urlsplit


class WebDAVError(Exception):
    """Only fixed, user-safe messages may be raised through this class."""


class WebDAVAdapter:
    def __init__(self):
        self.enabled = os.environ.get('WAGE_WEBDAV_ENABLED', '').lower() in {'true', '1', 'yes'}
        self.config_error = None
        self.destination = None
        if not self.enabled:
            return
        url = os.environ.get('WAGE_WEBDAV_URL', '').strip()
        username = os.environ.get('WAGE_WEBDAV_USERNAME', '')
        password = os.environ.get('WAGE_WEBDAV_PASSWORD', '')
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or any(ord(char) < 33 for char in url) or '\\' in url
                    or not username or ':' in username or not password):
                raise ValueError()
            self.host = parsed.hostname
            self.port = parsed.port or 443
            self.base_path = quote(parsed.path.rstrip('/') + '/', safe='/%:@')
            self.authorization = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
            identity = json.dumps([self.host.lower(), self.port, self.base_path, username])
            self.destination = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        except ValueError:
            self.config_error = 'WebDAV 配置不完整或无效：需 HTTPS 目录地址、独立用户名和密码，地址不得带凭据、查询参数或片段'

    @property
    def ready(self):
        return self.enabled and not self.config_error

    def _request(self, method, name, content):
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=20,
                                                 context=ssl.create_default_context())
        deadline = time.monotonic() + 120
        headers = {'Authorization': self.authorization, 'Accept-Encoding': 'identity',
                   'Cache-Control': 'no-cache', 'Content-Type': 'application/json'}
        try:
            connection.request(method, self.base_path + quote(name, safe=''),
                               body=content if method == 'PUT' else None, headers=headers)
            response = connection.getresponse()
            allowed = {200, 201, 204} if method == 'PUT' else {200}
            if response.status not in allowed:
                if 300 <= response.status < 400:
                    raise WebDAVError('WebDAV 返回重定向；请配置最终 HTTPS 目录地址')
                if response.status in {401, 403}:
                    raise WebDAVError('WebDAV 认证失败或目录读写权限不足')
                if response.status in {404, 409}:
                    raise WebDAVError('WebDAV 目标目录不存在；请先创建目录')
                raise WebDAVError(f'WebDAV 请求失败（HTTP {response.status}）')
            if method == 'GET':
                digest, count = hashlib.sha256(), 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WebDAVError('WebDAV 下载校验超时')
                    if connection.sock is not None:
                        connection.sock.settimeout(min(20, remaining))
                    chunk = response.read1(min(65536, len(content) - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > len(content):
                        raise WebDAVError('WebDAV 远端文件大小或摘要不匹配')
                    digest.update(chunk)
                if count != len(content) or digest.digest() != hashlib.sha256(content).digest():
                    raise WebDAVError('WebDAV 远端文件大小或摘要不匹配')
        except WebDAVError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise WebDAVError('WebDAV 连接失败、超时或 HTTPS 证书校验失败') from None
        finally:
            connection.close()

    def upload(self, path: Path, sha256: str):
        if not self.ready:
            raise WebDAVError(self.config_error or 'WebDAV 尚未启用')
        meta = path.with_suffix('.meta.json')
        try:
            if path.is_symlink() or meta.is_symlink() or path.stat().st_size > 64 * 1024 * 1024 or meta.stat().st_size > 1024 * 1024:
                raise ValueError()
            raw, metadata_raw = path.read_bytes(), meta.read_bytes()
            metadata = json.loads(metadata_raw)
            if hashlib.sha256(raw).hexdigest() != sha256 or metadata['sha256'] != sha256 or metadata['name'] != path.name:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise WebDAVError('本地备份或校验文件已损坏，未上传') from None
        for name, content in [(path.name, raw), (meta.name, metadata_raw)]:
            self._request('PUT', name, content)
            self._request('GET', name, content)
        return {'sha256': sha256, 'size': len(raw)}
