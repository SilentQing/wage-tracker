"""Verified local backups and a persistent, independent WebDAV upload queue."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from webdav_client import WebDAVAdapter, WebDAVError

MAX_BACKUP_BYTES = 64 * 1024 * 1024
CHINA = timezone(timedelta(hours=8))
NAME = re.compile(r'^(scheduled|manual)-\d{8}T\d{12}-[a-f0-9]{32}\.json$')
logger = logging.getLogger(__name__)


def atomic_write(path, raw):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class BackupService:
    def __init__(self, directory, capture, verify):
        self.directory = Path(directory)
        self.capture = capture
        self.verify = verify
        self.stop_event = threading.Event()
        self.thread = None
        self.remote_thread = None
        self.remote = WebDAVAdapter()

    def connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.directory / 'backup-control.sqlite3', timeout=30)
        conn.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('''CREATE TABLE IF NOT EXISTS remote_jobs (
            destination TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0, token TEXT, uploaded_at REAL,
            updated_at REAL NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(destination, name))''')
        conn.commit()
        return conn

    @staticmethod
    def set_state(conn, key, value):
        conn.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def get_state(conn):
        return {key: json.loads(value) for key, value in conn.execute('SELECT * FROM state')}

    @staticmethod
    def due_slot(now):
        now = now.astimezone(CHINA)
        due = now.replace(hour=3, minute=0, second=0, microsecond=0)
        return (due if now >= due else due - timedelta(days=1)).isoformat()

    def create(self, scheduled=False, now=None):
        now = (now or datetime.now(CHINA)).astimezone(CHINA)
        with closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            state = self.get_state(conn)
            slot = self.due_slot(now)
            if scheduled and state.get('scheduled_slot', '') >= slot:
                conn.rollback()
                return {'skipped': True}
            kind = 'scheduled' if scheduled else 'manual'
            name = f'{kind}-{now.strftime("%Y%m%dT%H%M%S%f")}-{uuid4().hex}.json'
            try:
                payload, report = self.capture()
                raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                if len(raw) > MAX_BACKUP_BYTES:
                    raise ValueError('备份超过64MB，请先扩展备份容量配置')
                # Verify the serialized bytes, not only the in-memory source.
                self.verify(json.loads(raw))
                digest = hashlib.sha256(raw).hexdigest()
                path = self.directory / name
                atomic_write(path, raw)
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError('备份写入后摘要不一致')
                manifest = dict(name=name, kind=kind, created_at=now.isoformat(), size=len(raw),
                                sha256=digest, checked_at=now.isoformat(), check_ok=True,
                                report=report)
                atomic_write(path.with_suffix('.meta.json'), json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
                self.set_state(conn, 'last_success', dict(name=name, time=now.isoformat()))
                self.set_state(conn, 'last_error', None)
                if scheduled:
                    self.set_state(conn, 'scheduled_slot', slot)
                self.enqueue_remote(conn, [manifest])
                conn.commit()
            except Exception as error:
                conn.rollback()
                reason = self.error_message(error)
                self.set_state(conn, 'last_error', dict(time=now.isoformat(), message=reason))
                conn.commit()
                raise
            # Only prune after a new verified scheduled backup is durable.
            if scheduled:
                try:
                    self.prune()
                except OSError:
                    self.set_state(conn, 'last_error', dict(time=now.isoformat(), message='备份成功，但旧定时备份清理失败'))
                    conn.commit()
            return manifest

    @staticmethod
    def error_message(error):
        if isinstance(error, OSError):
            return '文件读写失败，请检查剩余空间与备份目录权限'
        if isinstance(error, sqlite3.Error):
            return '数据库访问失败或繁忙'
        detail = getattr(error, 'detail', None)
        if isinstance(detail, str):
            return detail
        if isinstance(error, ValueError) and type(error) is ValueError:
            return str(error)
        return '备份数据校验失败，请核对账本字段及记录格式'

    def entries(self):
        entries = []
        for path in self.directory.glob('*.meta.json'):
            name = path.name.removesuffix('.meta.json') + '.json'
            if not NAME.fullmatch(name) or path.is_symlink():
                continue
            try:
                metadata = json.loads(path.read_text(encoding='utf-8'))
                if metadata.get('name') == name and (self.directory / name).is_file():
                    entries.append(metadata)
            except (OSError, ValueError):
                continue
        return sorted(entries, key=lambda entry: entry['name'], reverse=True)

    def prune(self):
        scheduled = [item for item in self.entries() if item['kind'] == 'scheduled']
        with closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            pending = {row[0] for row in conn.execute("SELECT name FROM remote_jobs WHERE status != 'uploaded'")}
            uploaded = {row[0] for row in conn.execute("SELECT name FROM remote_jobs WHERE destination=? AND status='uploaded'", (self.remote.destination,))}
            for item in scheduled[30:]:
                # Never prune queued work, even if remote uploads are temporarily disabled.
                if item['name'] in pending or (self.remote.enabled and item['name'] not in uploaded):
                    continue
                path = self.safe_path(item['name'])
                path.unlink(missing_ok=True)
                path.with_suffix('.meta.json').unlink(missing_ok=True)
            conn.commit()

    def enqueue_remote(self, conn, entries):
        if self.remote.ready:
            conn.executemany('INSERT OR IGNORE INTO remote_jobs(destination,name) VALUES (?,?)',
                             [(self.remote.destination, item['name']) for item in entries])

    def retry_remote(self):
        if not self.remote.ready:
            raise ValueError(self.remote.config_error or 'WebDAV 尚未启用')
        with closing(self.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.enqueue_remote(conn, self.entries())
            count = conn.execute("UPDATE remote_jobs SET next_try=0 WHERE destination=? AND status='pending'",
                                 (self.remote.destination,)).rowcount
            conn.commit()
        return {'queued': count, 'message': '已排队，后台将在一分钟内开始；上传中的任务不会重复提交'}

    def sync_remote(self, now=None):
        """Claim at most one job. Network I/O never holds a SQLite transaction."""
        if not self.remote.ready:
            return {'skipped': True}
        timestamp = (now or datetime.now(CHINA)).timestamp()
        destination = self.remote.destination
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
            self.enqueue_remote(conn, self.entries())
            conn.execute("UPDATE remote_jobs SET status='pending',token=NULL WHERE destination=? AND status='uploading' AND lease_until<=?",
                         (destination, timestamp))
            if conn.execute("SELECT 1 FROM remote_jobs WHERE destination=? AND status='uploading'", (destination,)).fetchone():
                conn.commit()
                return {'skipped': True}
            job = conn.execute("SELECT * FROM remote_jobs WHERE destination=? AND status='pending' AND next_try<=? ORDER BY next_try,name LIMIT 1",
                               (destination, timestamp)).fetchone()
            if job is None:
                conn.commit()
                return {'skipped': True}
            token = uuid4().hex
            conn.execute("UPDATE remote_jobs SET status='uploading',token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE destination=? AND name=?",
                         (token, timestamp + 900, timestamp, destination, job['name']))
            conn.commit()
        error = None
        try:
            raw = self.read_verified(job['name'])
            self.verify(json.loads(raw))
            self.remote.upload(self.safe_path(job['name']), hashlib.sha256(raw).hexdigest())
        except WebDAVError as exc:
            error = str(exc)
        except Exception:
            error = '本地备份校验或上传处理失败，请重新检查备份文件'
        finished = timestamp if now else datetime.now(CHINA).timestamp()
        with closing(self.connect()) as conn:
            if error:
                delay = min(3600, 60 * 2 ** min(job['attempts'], 6))
                conn.execute("UPDATE remote_jobs SET status='pending',next_try=?,lease_until=0,token=NULL,last_error=?,updated_at=? WHERE destination=? AND name=? AND token=?",
                             (finished + delay, error, finished, destination, job['name'], token))
            else:
                conn.execute("UPDATE remote_jobs SET status='uploaded',uploaded_at=?,lease_until=0,token=NULL,last_error=NULL,updated_at=? WHERE destination=? AND name=? AND token=?",
                             (finished, finished, destination, job['name'], token))
            conn.commit()
        return {'name': job['name'], 'uploaded': error is None}

    def remote_status(self):
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            jobs = [dict(row) for row in conn.execute('SELECT * FROM remote_jobs WHERE destination=?', (self.remote.destination,))]
        def date_text(value):
            return datetime.fromtimestamp(value, CHINA).isoformat() if value else None
        successful = [job for job in jobs if job['status'] == 'uploaded']
        failed = [job for job in jobs if job['status'] != 'uploaded' and job['last_error']]
        last = max(successful, key=lambda job: job['uploaded_at']) if successful else None
        error = max(failed, key=lambda job: job['updated_at']) if failed else None
        waiting = [job for job in jobs if job['status'] == 'pending']
        return dict(enabled=self.remote.enabled, configured=self.remote.ready,
                    configuration_error=self.remote.config_error,
                    pending=len(waiting), uploading=sum(job['status'] == 'uploading' for job in jobs),
                    failed=len(failed), uploaded=len(successful),
                    next_retry=date_text(min((job['next_try'] for job in waiting), default=0)),
                    last_success=dict(name=last['name'], time=date_text(last['uploaded_at'])) if last else None,
                    last_error=dict(name=error['name'], time=date_text(error['updated_at']), message=error['last_error']) if error else None,
                    files={job['name']: dict(status=job['status'], attempts=job['attempts'],
                                            last_error=job['last_error'], uploaded_at=date_text(job['uploaded_at'])) for job in jobs})

    def safe_path(self, name):
        if not NAME.fullmatch(name):
            raise ValueError('无效备份文件名')
        path = self.directory / name
        if path.is_symlink() or path.resolve().parent != self.directory.resolve():
            raise ValueError('无效备份路径')
        return path

    def read_verified(self, name):
        path = self.safe_path(name)
        if path.stat().st_size > MAX_BACKUP_BYTES:
            raise ValueError('备份超过64MB')
        meta_path = path.with_suffix('.meta.json')
        if meta_path.is_symlink():
            raise ValueError('无效校验文件路径')
        metadata = json.loads(meta_path.read_text(encoding='utf-8'))
        raw = path.read_bytes()
        if not isinstance(metadata, dict) or hashlib.sha256(raw).hexdigest() != metadata.get('sha256'):
            raise ValueError('文件摘要不匹配')
        return raw

    def recheck(self, name):
        path = self.safe_path(name)
        try:
            raw = self.read_verified(name)
            report = self.verify(json.loads(raw))
        except Exception as error:
            try:
                meta_path = path.with_suffix('.meta.json')
                if not meta_path.is_symlink():
                    metadata = json.loads(meta_path.read_text(encoding='utf-8'))
                    metadata.update(checked_at=datetime.now(CHINA).isoformat(), check_ok=False)
                    atomic_write(meta_path, json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
            except (OSError, ValueError, AttributeError):
                pass
            with closing(self.connect()) as conn:
                self.set_state(conn, 'last_error', dict(time=datetime.now(CHINA).isoformat(), message=f'备份复查失败：{self.error_message(error)}'))
                conn.commit()
            raise ValueError('备份复查未通过') from error
        metadata = json.loads(path.with_suffix('.meta.json').read_text(encoding='utf-8'))
        metadata.update(checked_at=datetime.now(CHINA).isoformat(), check_ok=True, report=report)
        atomic_write(path.with_suffix('.meta.json'), json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
        return metadata

    def status(self):
        with closing(self.connect()) as conn:
            state = self.get_state(conn)
        now = datetime.now(CHINA)
        due = now.replace(hour=3, minute=0, second=0, microsecond=0)
        upcoming = due if due > now else due + timedelta(days=1)
        return dict(schedule='每天北京时间 03:00，启动时补最近一期', retention=30,
                    next_run=upcoming.isoformat(), webdav='enabled' if self.remote.ready else 'configuration_error' if self.remote.enabled else 'disabled',
                    remote=self.remote_status(),
                    last_success=state.get('last_success'), last_error=state.get('last_error'),
                    files=self.entries())

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.create(scheduled=True)
            except Exception:
                logger.warning('Scheduled backup failed; see backup status')
            self.stop_event.wait(60)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='wage-backup', daemon=True)
        self.thread.start()
        self.remote_thread = threading.Thread(target=self.run_remote, name='wage-webdav', daemon=True)
        self.remote_thread.start()

    def run_remote(self):
        while not self.stop_event.is_set():
            try:
                self.sync_remote()
            except Exception:
                logger.warning('Remote backup queue unavailable; retrying next minute')
            self.stop_event.wait(60)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        if self.remote_thread:
            self.remote_thread.join()
