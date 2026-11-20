#!/usr/bin/env python3
"""fnOS M-Team performer subscription and release-upgrade service."""

import datetime
import copy
import errno
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


APP_NAME = "AVSubscribe"
APP_ROOT = Path(os.environ.get("AVSUB_APP_ROOT", "/var/apps/AVSubscribe/target"))
WEB_ROOT = APP_ROOT / "www"
DATA_ROOT = Path(os.environ.get("TRIM_PKGVAR", os.environ.get("AVSUB_DATA_DIR", "/var/apps/AVSubscribe/var")))
SETTINGS_FILE = DATA_ROOT / "settings.json"
SUBSCRIPTIONS_FILE = DATA_ROOT / "subscriptions.json"
RECORDS_FILE = DATA_ROOT / "records.json"
LOCAL_VIDEO_DATABASE = DATA_ROOT / "local-videos.sqlite3"
LOG_DATABASE_FILE = DATA_ROOT / "activity-log.sqlite3"
STATUS_FILE = DATA_ROOT / "status.json"
SCHEDULE_FILE = DATA_ROOT / "last-scheduled-scan.json"
BACKFILL_STATUS_FILE = DATA_ROOT / "mteam-id-backfill.json"
BACKFILL_STATUS_LOCK_FILE = DATA_ROOT / "mteam-id-backfill.lock"
LOG_FILE = DATA_ROOT / "activity-log.json"
REQUEST_FILE = DATA_ROOT / "request.json"
LOCK_FILE = DATA_ROOT / "scheduler.lock"
DISABLED_FILE = DATA_ROOT / "disabled"
MTEAM_LIMIT_FILE = DATA_ROOT / "mteam-rate-limit.json"
MTEAM_REQUEST_STATE_FILE = DATA_ROOT / "mteam-request-state.json"
MTEAM_REQUEST_LOCK_FILE = DATA_ROOT / "mteam-request.lock"
LOG_LOCK_FILE = DATA_ROOT / "activity-log.lock"
MTEAM_API_BASE = "https://api.m-team.cc/api"
MTEAM_API_HOST = "api.m-team.cc"
MTEAM_DOWNLOAD_DOMAINS = ("halomt.com", "manfuz.co")
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MTEAM_MIN_REQUEST_INTERVAL = 5.0
MTEAM_DEFAULT_COOLDOWN = 900
MAX_SUBTITLE_BYTES = 32 * 1024 * 1024
SUBTITLE_CHECK_VERSION = 3
LOCAL_MTEAM_RANK_VERSION = 3
MTEAM_RANK_VERSION = 2
VIDEO_EXTENSIONS = {".3gp", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogv", ".rmvb", ".strm", ".ts", ".vob", ".webm", ".wmv"}
SUBTITLE_EXTENSIONS = {".ass", ".idx", ".srt", ".ssa", ".sub", ".sup", ".vtt"}
DEFAULT_SETTINGS = {"mteam_token": "", "downloader": {"type": "qbittorrent", "url": "", "username": "", "password": "", "path_mappings": []}, "poll_interval_minutes": 60, "mteam_request_interval_seconds": 5, "auto_scan": True, "local_multi_disk_wash": False, "cleanup_orphan_sidecars": False, "subtitle_collection_dir": "", "debug_mode": False}
CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{2,10})[- _.\u2010-\u2015]+(\d{2,8})(?!\d)", re.IGNORECASE)
FC2_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(FC2)[- _.\u2010-\u2015]?(PPV)[- _.\u2010-\u2015]+(\d{3,8})(?!\d)", re.IGNORECASE)
EPISODE_CODE_RE = re.compile(r"S\d{1,2}E\d{1,3}-\d{3,4}", re.IGNORECASE)
STOP_SCHEDULER = False
MTEAM_THREAD_LOCK = threading.Lock()
QB_SESSION_THREAD_LOCK = threading.Lock()
MTEAM_POLL_CACHE = threading.local()


def is_mteam_download_host(host):
    return any(host == domain or host.endswith("." + domain) for domain in MTEAM_DOWNLOAD_DOMAINS)


def is_mteam_host(host):
    return any(host == domain or host.endswith("." + domain) for domain in ("m-team.cc", "m-team.io"))


class MTeamRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        target = urllib.parse.urlparse(new_url)
        if target.scheme != "https" or target.hostname != MTEAM_API_HOST:
            raise AppError("M-Team 返回了站外或非 HTTPS 跳转，已拒绝访问。")
        return super().redirect_request(request, response, code, message, headers, new_url)


class MTeamDownloadRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        source = urllib.parse.urlparse(request.full_url)
        target = urllib.parse.urlparse(new_url)
        source_is_api = source.hostname == MTEAM_API_HOST or is_mteam_host(source.hostname or "")
        source_is_download = is_mteam_download_host(source.hostname or "")
        target_is_api = target.hostname == MTEAM_API_HOST or is_mteam_host(target.hostname or "")
        target_is_download = is_mteam_download_host(target.hostname or "")
        if target.scheme != "https" or target.username or target.password or target.port or not (
            (source_is_api and (target_is_api or target_is_download)) or (source_is_download and target_is_download)
        ):
            raise AppError(f"M-Team 返回了不受信任的种子下载跳转主机：{target.hostname or '未知'}。")
        safe_headers = {
            key: value for key, value in request.headers.items()
            if key.casefold() not in {"x-api-key", "authorization", "cookie", "content-type"}
        }
        return urllib.request.Request(new_url, headers=safe_headers, method="GET")


MTEAM_OPENER = urllib.request.build_opener(MTeamRedirectHandler())
MTEAM_DOWNLOAD_OPENER = urllib.request.build_opener(MTeamDownloadRedirectHandler())


class AppError(Exception):
    pass


class MTeamRateLimitError(AppError):
    def __init__(self, message, retry_after=MTEAM_DEFAULT_COOLDOWN, retry_after_provided=False):
        super().__init__(message)
        self.retry_after = max(60, min(int(retry_after), 21600))
        self.retry_after_provided = bool(retry_after_provided)


class MTeamRequestInterrupted(AppError):
    pass


class PerformerMismatchError(AppError):
    pass


class CheckContext:
    """Caches are owned by one check execution, never by a CGI process or another round."""
    def __init__(self):
        self.scans = {}
        self.task_cache = {}
        self.metrics = {}
        self.stages = {}
        self.wait_seconds = 0.0
        self.generation = 0

    def count(self, name, amount=1):
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def invalidate_directories(self, *paths):
        roots = [os.path.realpath(path) for path in paths if path]
        for key in list(self.scans):
            def overlaps(root):
                try:
                    Path(key).relative_to(Path(root))
                    return True
                except ValueError:
                    try:
                        Path(root).relative_to(Path(key))
                        return True
                    except ValueError:
                        return False
            if not roots or any(overlaps(root) for root in roots):
                self.scans.pop(key, None)

    def resume_after_wait(self):
        self.scans.clear()
        self.task_cache.clear()
        self.generation += 1

    def report(self, settings, performer):
        if not settings.get("debug_mode"):
            return
        labels = (("sync", "下载器同步"), ("scan", "目录扫描"), ("match", "ID匹配"), ("detail", "详情"),
                  ("search", "搜索"), ("wash", "洗版"), ("index", "索引"), ("subtitle", "字幕处理"))
        durations = "，".join(f"{label} {self.stages.get(key, 0):.2f}秒" for key, label in labels)
        counts = (("qb_requests", "qB请求"), ("mteam_requests", "M-Team请求"), ("scan_count", "实际扫描"),
                  ("scan_hits", "扫描缓存命中"), ("task_queries", "任务查询"), ("task_hits", "任务缓存命中"),
                  ("detail_hits", "详情缓存命中"), ("option_hits", "选项缓存命中"), ("file_hits", "种子文件缓存命中"),
                  ("metadata_hits", "注释缓存命中"), ("path_checks", "路径匹配检查"))
        summary = "，".join(f"{label} {self.metrics.get(key, 0)}" for key, label in counts)
        log_event(f"性能汇总：{durations}；限流等待 {self.wait_seconds:.2f}秒；{summary}。阶段可包含子阶段，耗时已扣除限流等待。",
                  level="debug", event="debug_performance", subscription=performer)


def check_context():
    return getattr(MTEAM_POLL_CACHE, "execution", None)


def performance_count(name, amount=1):
    context = check_context()
    if context is not None:
        context.count(name, amount)


def invalidate_local_scans(*paths):
    context = check_context()
    if context is not None:
        context.invalidate_directories(*paths)


def measure_stage(name):
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            context = check_context()
            if context is None:
                return function(*args, **kwargs)
            started = time.perf_counter()
            waited = context.wait_seconds
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = max(0.0, time.perf_counter() - started - (context.wait_seconds - waited))
                context.stages[name] = context.stages.get(name, 0.0) + elapsed
        return measured
    return decorate


class CheckDownloader:
    """Batch tracked hashes and cache snapshots for at most fifteen seconds."""
    def __init__(self, downloader, context, tracked_hashes=()):
        self.downloader = downloader
        self.context = context
        self.tracked_hashes = {str(value).lower() for value in tracked_hashes if value}
        self.known = {}
        self.fetched = {}
        self.full_at = None

    def __getattr__(self, name):
        return getattr(self.downloader, name)

    @property
    def progress_callback(self):
        return getattr(self.downloader, "progress_callback", None)

    @progress_callback.setter
    def progress_callback(self, value):
        self.downloader.progress_callback = value

    def _fresh(self, timestamp):
        return timestamp is not None and 0 <= time.monotonic() - timestamp < 15

    def list_tasks(self, hashes=None):
        wanted = None if hashes is None else {str(value).lower() for value in hashes if value}
        if wanted == set():
            return {}
        if self._fresh(self.full_at):
            self.context.count("task_hits")
            return copy.deepcopy(self.known if wanted is None else {key: value for key, value in self.known.items() if key in wanted})
        if self.full_at is not None:
            self.invalidate()
        if wanted is None:
            self.context.count("task_queries")
            tasks = self.downloader.list_tasks()
            self.known = {str(key).lower(): value for key, value in tasks.items()}
            self.full_at = time.monotonic()
            self.fetched = {key: self.full_at for key in self.known}
            self.context.task_cache["tasks"] = self.known
            return copy.deepcopy(self.known)
        missing = {key for key in wanted if not self._fresh(self.fetched.get(key))}
        if not missing:
            self.context.count("task_hits")
        else:
            missing.update(key for key in self.tracked_hashes if not self._fresh(self.fetched.get(key)))
            keys = sorted(missing)
            for offset in range(0, len(keys), 100):
                batch = keys[offset:offset + 100]
                self.context.count("task_queries")
                tasks = self.downloader.list_tasks(batch)
                stamp = time.monotonic()
                for key in batch:
                    self.fetched[key] = stamp
                    self.known.pop(key, None)
                self.known.update({str(key).lower(): value for key, value in tasks.items() if str(key).lower() in batch})
        return copy.deepcopy({key: value for key, value in self.known.items() if key in wanted})

    def list_task_files(self, info_hash):
        cache = self.context.task_cache.setdefault("id_file_lists", {})
        if info_hash in cache:
            self.context.count("file_hits")
        else:
            cache[info_hash] = self.downloader.list_task_files(info_hash)
        return copy.deepcopy(cache[info_hash])

    def task_mteam_metadata(self, info_hash):
        cache = self.context.task_cache.setdefault("id_torrent_metadata", {})
        if info_hash in cache:
            self.context.count("metadata_hits")
        else:
            cache[info_hash] = self.downloader.task_mteam_metadata(info_hash)
        return copy.deepcopy(cache[info_hash])

    def invalidate(self):
        self.full_at = None
        self.known.clear()
        self.fetched.clear()
        self.context.task_cache.clear()

    def _mutate(self, method, *args, directories=(), **kwargs):
        try:
            return getattr(self.downloader, method)(*args, **kwargs)
        finally:
            # A timeout can follow a successful server-side change, so invalidate on errors too.
            self.invalidate()
            self.context.invalidate_directories(*directories)

    def add(self, torrent_data, title, download_dir):
        return self._mutate("add", torrent_data, title, download_dir, directories=(download_dir,))

    def remove(self, info_hash, delete_data=True):
        task = self.known.get(str(info_hash).lower(), {})
        return self._mutate("remove", info_hash, delete_data, directories=(task.get("content_path"), task.get("save_path")))

    def recheck(self, info_hash, download_dir=""):
        task = self.known.get(str(info_hash).lower(), {})
        return self._mutate("recheck", info_hash, download_dir, directories=(download_dir, task.get("content_path"), task.get("save_path")))


def check_task_snapshot(downloader, cache):
    if isinstance(downloader, CheckDownloader):
        return downloader.list_tasks()
    if "tasks" not in cache:
        cache["tasks"] = downloader.list_tasks()
    return cache["tasks"]


def wait_for_mteam_request():
    with MTEAM_THREAD_LOCK, file_lock(MTEAM_REQUEST_LOCK_FILE):
        state = read_json(MTEAM_REQUEST_STATE_FILE, {})
        interval = get_settings().get("mteam_request_interval_seconds", MTEAM_MIN_REQUEST_INTERVAL)
        next_request_at = float(state.get("next_request_at", 0) or 0)
        delay = max(0.0, next_request_at - time.time())
        if delay:
            time.sleep(delay)
        write_json(MTEAM_REQUEST_STATE_FILE, {"next_request_at": time.time() + interval})


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_json(path, default, allow_types=()):
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except UnicodeDecodeError as error:
        backup = backup_invalid_state_file(path)
        raise AppError(f"应用数据文件 {path.name} 编码无效，原文件已保留{f'，备份为 {backup.name}' if backup else ''}。请修复或恢复备份后重试。") from error
    except OSError as error:
        raise AppError(f"无法读取应用数据文件 {path.name}：{error}") from error
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        backup = backup_invalid_state_file(path)
        raise AppError(f"应用数据文件 {path.name} 已损坏，原文件已保留{f'，备份为 {backup.name}' if backup else ''}。请修复或恢复备份后重试。") from error
    if not isinstance(value, (type(default), *allow_types)):
        backup = backup_invalid_state_file(path)
        raise AppError(f"应用数据文件 {path.name} 的格式不正确，原文件已保留{f'，备份为 {backup.name}' if backup else ''}。请修复或恢复备份后重试。")
    return value


def backup_invalid_state_file(path):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}")
    try:
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        print(f"AVSubscribe: invalid state file {path}; preserved copy at {backup}", file=sys.stderr)
        return backup
    except OSError as error:
        print(f"AVSubscribe: invalid state file {path}; backup failed: {error}", file=sys.stderr)
        return None


def write_json(path, value):
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_ROOT, 0o700)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def connect_video_database():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_ROOT, 0o700)
    connection = sqlite3.connect(LOCAL_VIDEO_DATABASE, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS local_videos (
            subscription_id TEXT NOT NULL,
            code TEXT NOT NULL,
            rank_json TEXT NOT NULL,
            rank_source TEXT NOT NULL,
            rank_title TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (subscription_id, code)
        );
        CREATE TABLE IF NOT EXISTS local_video_files (
            subscription_id TEXT NOT NULL,
            code TEXT NOT NULL,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            PRIMARY KEY (subscription_id, code, path),
            FOREIGN KEY (subscription_id, code) REFERENCES local_videos(subscription_id, code) ON DELETE CASCADE
        );
    """)
    file_columns = {row[1] for row in connection.execute("PRAGMA table_info(local_video_files)")}
    for name, declaration in (
        ("mteam_id", "TEXT NOT NULL DEFAULT ''"),
        ("rank_json", "TEXT NOT NULL DEFAULT '[0,0,0]'"),
        ("rank_source", "TEXT NOT NULL DEFAULT ''"),
        ("rank_title", "TEXT NOT NULL DEFAULT ''"),
        ("subtitle", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in file_columns:
            connection.execute(f"ALTER TABLE local_video_files ADD COLUMN {name} {declaration}")
    os.chmod(LOCAL_VIDEO_DATABASE, 0o600)
    return connection


def sync_video_database(subscription_id, indexed_rows, known_subscriptions):
    connection = connect_video_database()
    try:
        with connection:
            connection.execute("DELETE FROM local_videos WHERE subscription_id = ?", (subscription_id,))
            known_ids = sorted(known_subscriptions)
            if known_ids:
                placeholders = ",".join("?" for _ in known_ids)
                connection.execute(f"DELETE FROM local_videos WHERE subscription_id NOT IN ({placeholders})", known_ids)
            else:
                connection.execute("DELETE FROM local_videos")
            for row in indexed_rows:
                connection.execute(
                    "INSERT INTO local_videos(subscription_id, code, rank_json, rank_source, rank_title, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (subscription_id, row["code"], json.dumps(row["rank"]), row["rank_source"], row["rank_title"], row["updated_at"]),
                )
                connection.executemany(
                    "INSERT INTO local_video_files(subscription_id, code, path, name, size, mtime_ns, mteam_id, rank_json, rank_source, rank_title, subtitle) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(subscription_id, row["code"], item["path"], item["name"], item["size"], item["mtime_ns"], item.get("mteam_id", ""), json.dumps(item.get("rank", [0, 0, 0])), item.get("rank_source", ""), item.get("rank_title", ""), item.get("subtitle", "")) for item in row["files"]],
                )
    finally:
        connection.close()


def find_indexed_local_release(code, exclude_subscription_id, target_dir):
    connection = connect_video_database()
    try:
        rows = connection.execute(
            """SELECT f.subscription_id, f.path, f.size, f.mtime_ns, v.rank_json
               FROM local_video_files AS f
               JOIN local_videos AS v USING (subscription_id, code)
               WHERE f.code = ? AND f.subscription_id != ?""",
            (code, exclude_subscription_id),
        ).fetchall()
        valid = []
        stale = []
        target_root = Path(target_dir).resolve()
        for subscription_id, value, saved_size, saved_mtime, rank_json in rows:
            path = Path(value)
            try:
                if path.is_symlink() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
                    stale.append((subscription_id, value))
                    continue
                resolved = path.resolve(strict=True)
                resolved.relative_to(target_root)
                continue
            except ValueError:
                pass
            except OSError:
                stale.append((subscription_id, value))
                continue
            try:
                stat = path.stat()
                rank = json.loads(rank_json)
                valid.append({
                    "subscription_id": subscription_id,
                    "path": str(path),
                    "rank": rank if isinstance(rank, list) else [0, 0, 0],
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "stale_metadata": stat.st_size != saved_size or stat.st_mtime_ns != saved_mtime,
                })
            except (OSError, ValueError, TypeError):
                stale.append((subscription_id, value))
        if stale:
            with connection:
                connection.executemany(
                    "DELETE FROM local_video_files WHERE subscription_id = ? AND code = ? AND path = ?",
                    [(subscription_id, code, value) for subscription_id, value in stale],
                )
        return max(valid, key=lambda item: (tuple(item["rank"]), item["mtime_ns"])) if valid else None
    finally:
        connection.close()


def delete_video_database_subscription(subscription_id):
    connection = connect_video_database()
    try:
        with connection:
            connection.execute("DELETE FROM local_videos WHERE subscription_id = ?", (subscription_id,))
    finally:
        connection.close()


def log_event(message, level="info", event="status", subscription=""):
    round_id = getattr(MTEAM_POLL_CACHE, "round_id", "")
    entry = {"time": now_iso(), "level": level, "event": event, "subscription": subscription, "message": str(message)[:2000], "round_id": round_id}
    connection = connect_log_database()
    try:
        with connection:
            connection.execute(
                "INSERT INTO activity_logs(time, level, event, subscription, message, round_id) VALUES (?, ?, ?, ?, ?, ?)",
                (entry["time"], entry["level"], entry["event"], entry["subscription"], entry["message"], entry["round_id"]),
            )
            connection.execute("DELETE FROM activity_logs WHERE id NOT IN (SELECT id FROM activity_logs ORDER BY id DESC LIMIT 500)")
    finally:
        connection.close()


def log_debug(settings, message, subscription=""):
    if settings.get("debug_mode", False):
        log_event(message, level="debug", event="debug_decision", subscription=subscription)


def log_poll_separator(phase, trigger, subscription_id=""):
    trigger_name = {"manual": "手动检查", "scheduled": "定时检查"}.get(trigger, str(trigger))
    performer = ""
    if subscription_id:
        try:
            performer = next((item["performer"] for item in list_subscriptions() if item.get("id") == subscription_id), "")
        except AppError:
            pass
    log_event(f"本轮检查{phase} · {trigger_name}", event="run_separator", subscription=performer)


def log_poll_round_separator(phase, trigger, subscription_id="", round_id=""):
    previous_round_id = getattr(MTEAM_POLL_CACHE, "round_id", "")
    if round_id:
        MTEAM_POLL_CACHE.round_id = round_id
    if subscription_id:
        log_poll_separator(phase, trigger, subscription_id)
        MTEAM_POLL_CACHE.round_id = previous_round_id
        return
    try:
        subscriptions = list_subscriptions()
    except AppError:
        subscriptions = []
    if not subscriptions:
        log_poll_separator(phase, trigger)
        MTEAM_POLL_CACHE.round_id = previous_round_id
        return
    for subscription in subscriptions:
        log_poll_separator(phase, trigger, subscription.get("id", ""))
    MTEAM_POLL_CACHE.round_id = previous_round_id


def connect_log_database():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_ROOT, 0o700)
    connection = sqlite3.connect(LOG_DATABASE_FILE, timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            level TEXT NOT NULL,
            event TEXT NOT NULL,
            subscription TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            round_id TEXT NOT NULL DEFAULT ''
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS activity_logs_subscription_id ON activity_logs(subscription, id)")
    connection.execute("CREATE TABLE IF NOT EXISTS activity_log_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.commit()
    try:
        imported = connection.execute("SELECT 1 FROM activity_log_meta WHERE key = 'legacy_json_imported'").fetchone()
        if not imported:
            connection.execute("BEGIN IMMEDIATE")
            try:
                imported = connection.execute("SELECT 1 FROM activity_log_meta WHERE key = 'legacy_json_imported'").fetchone()
                if not imported:
                    try:
                        entries = read_json(LOG_FILE, [])
                    except AppError:
                        entries = []
                    connection.executemany(
                        "INSERT INTO activity_logs(time, level, event, subscription, message, round_id) VALUES (?, ?, ?, ?, ?, ?)",
                        [(str(item.get("time", "")), str(item.get("level", "info")), str(item.get("event", "status")), str(item.get("subscription", "")), str(item.get("message", ""))[:2000], str(item.get("round_id", ""))) for item in entries[-500:] if isinstance(item, dict)],
                    )
                    connection.execute("INSERT INTO activity_log_meta(key, value) VALUES ('legacy_json_imported', '1')")
                    connection.execute("DELETE FROM activity_logs WHERE id NOT IN (SELECT id FROM activity_logs ORDER BY id DESC LIMIT 500)")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        os.chmod(LOG_DATABASE_FILE, 0o600)
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def get_logs(limit=500, after_id=None):
    limit = max(1, min(int(limit), 500))
    connection = connect_log_database()
    try:
        if after_id is not None:
            try:
                after_id = max(0, int(after_id))
            except (TypeError, ValueError):
                raise AppError("日志增量游标无效。")
            rows = connection.execute(
                "SELECT id, time, level, event, subscription, message, round_id FROM activity_logs WHERE id > ? ORDER BY id ASC LIMIT ?",
                (after_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, time, level, event, subscription, message, round_id FROM activity_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()[::-1]
        return [dict(zip(("id", "time", "level", "event", "subscription", "message", "round_id"), row)) for row in rows]
    finally:
        connection.close()


def clear_logs(subscription=None):
    if subscription is not None:
        subscription = str(subscription).strip()
        if not subscription or len(subscription) > 120:
            raise AppError("日志清理的女优名称无效。")
    connection = connect_log_database()
    try:
        with connection:
            if subscription is None:
                deleted = connection.execute("SELECT COUNT(*) FROM activity_logs").fetchone()[0]
                connection.execute("DELETE FROM activity_logs")
            else:
                cursor = connection.execute("DELETE FROM activity_logs WHERE subscription = ?", (subscription,))
                deleted = cursor.rowcount
        return {"deleted": deleted}
    finally:
        connection.close()


@contextmanager
def file_lock(lock_path=None):
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with (lock_path or LOCK_FILE).open("a+b") as stream:
        if fcntl:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def get_settings():
    settings = read_json(SETTINGS_FILE, {})
    downloader = {**DEFAULT_SETTINGS["downloader"], **settings.get("downloader", {})}
    # Older releases allowed Transmission; normalize persisted settings on upgrade.
    downloader["type"] = "qbittorrent"
    return {**DEFAULT_SETTINGS, **settings, "downloader": downloader}


def normalize_path_mappings(value):
    if not isinstance(value, list) or len(value) > 50:
        raise AppError("路径映射必须是列表，最多可添加 50 条。")
    mappings = []
    local_paths = set()
    for index, mapping in enumerate(value, 1):
        if not isinstance(mapping, dict):
            raise AppError(f"第 {index} 条路径映射格式无效。")
        local = str(mapping.get("local", "")).strip()
        remote = str(mapping.get("remote", "")).strip()
        if not local and not remote:
            continue
        if not local.startswith("/") or not remote.startswith("/"):
            raise AppError(f"第 {index} 条路径映射必须填写以 / 开头的绝对路径。")
        if any(ord(char) < 32 or ord(char) == 127 for char in local + remote):
            raise AppError(f"第 {index} 条路径映射包含无效字符。")
        local = posixpath.normpath(local)
        remote = posixpath.normpath(remote)
        if local == "/" or remote == "/":
            raise AppError(f"第 {index} 条路径映射不能映射文件系统根目录。")
        if local in local_paths:
            raise AppError(f"本地路径重复：{local}")
        local_paths.add(local)
        mappings.append({"local": local, "remote": remote})
    return mappings


def map_path_prefix(path, mappings, source_key, target_key):
    if not isinstance(path, str) or not path.startswith("/"):
        return path
    normalized = posixpath.normpath(path)
    matches = [mapping for mapping in mappings if normalized == mapping[source_key] or normalized.startswith(mapping[source_key] + "/")]
    if not matches:
        return path
    selected = max(matches, key=lambda mapping: len(mapping[source_key]))
    suffix = normalized[len(selected[source_key]):].lstrip("/")
    return posixpath.join(selected[target_key], suffix) if suffix else selected[target_key]


def save_settings(payload):
    settings = get_settings()
    token = payload.get("mteam_token", settings.get("mteam_token", ""))
    if payload.get("clear_mteam_token"):
        token = ""
    elif not token:
        token = settings.get("mteam_token", "")
    if not isinstance(token, str) or len(token.strip()) > 4096 or any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise AppError("M-Team 令牌格式无效。")
    downloader = payload.get("downloader", settings["downloader"])
    if not isinstance(downloader, dict):
        raise AppError("下载器设置无效。")
    kind = downloader.get("type", "qbittorrent")
    if kind != "qbittorrent":
        raise AppError("下载器类型仅支持 qBittorrent。")
    url = str(downloader.get("url", "")).strip().rstrip("/")
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise AppError("下载器地址必须是有效的 HTTP 或 HTTPS 地址。")
    path_mappings = normalize_path_mappings(downloader.get("path_mappings", settings["downloader"].get("path_mappings", [])))
    interval = payload.get("poll_interval_minutes", settings["poll_interval_minutes"])
    if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 1440:
        raise AppError("轮询间隔须为 5 到 1440 分钟。")
    request_interval = payload.get("mteam_request_interval_seconds", settings.get("mteam_request_interval_seconds", MTEAM_MIN_REQUEST_INTERVAL))
    if isinstance(request_interval, bool) or not isinstance(request_interval, int) or not 1 <= request_interval <= 60:
        raise AppError("M-Team 请求间隔须为 1 到 60 秒。")
    subtitle_collection_dir = payload.get("subtitle_collection_dir", settings.get("subtitle_collection_dir", ""))
    if not isinstance(subtitle_collection_dir, str) or any(ord(char) < 32 or ord(char) == 127 for char in subtitle_collection_dir):
        raise AppError("字幕汇总目录格式无效。")
    subtitle_collection_dir = subtitle_collection_dir.strip()
    if subtitle_collection_dir:
        if not subtitle_collection_dir.startswith("/"):
            raise AppError("字幕汇总目录须填写飞牛本地绝对路径。")
        subtitle_collection_dir = posixpath.normpath(subtitle_collection_dir)
        if subtitle_collection_dir == "/":
            raise AppError("字幕汇总目录不能为文件系统根目录。")
    result = {
        "mteam_token": token.strip(),
        "downloader": {
            "type": kind,
            "url": url,
            "username": str(downloader.get("username", "")).strip(),
            "password": str(downloader.get("password", "")) or settings["downloader"].get("password", ""),
            "path_mappings": path_mappings,
        },
        "poll_interval_minutes": interval,
        "mteam_request_interval_seconds": request_interval,
        "auto_scan": bool(payload.get("auto_scan", settings["auto_scan"])),
        "local_multi_disk_wash": bool(payload.get("local_multi_disk_wash", settings.get("local_multi_disk_wash", False))),
        "cleanup_orphan_sidecars": bool(payload.get("cleanup_orphan_sidecars", settings.get("cleanup_orphan_sidecars", False))),
        "subtitle_collection_dir": subtitle_collection_dir,
        "debug_mode": bool(payload.get("debug_mode", settings.get("debug_mode", False))),
    }
    write_json(SETTINGS_FILE, result)
    return public_settings(result)


def public_settings(settings=None):
    settings = settings or get_settings()
    client = settings["downloader"]
    return {
        "mteam_token_configured": bool(settings.get("mteam_token")),
        "downloader": {"type": client.get("type", "qbittorrent"), "url": client.get("url", ""), "username": client.get("username", ""), "password_configured": bool(client.get("password")), "path_mappings": client.get("path_mappings", [])},
        "poll_interval_minutes": settings.get("poll_interval_minutes", 60),
        "mteam_request_interval_seconds": settings.get("mteam_request_interval_seconds", MTEAM_MIN_REQUEST_INTERVAL),
        "auto_scan": settings.get("auto_scan", True),
        "local_multi_disk_wash": settings.get("local_multi_disk_wash", False),
        "cleanup_orphan_sidecars": settings.get("cleanup_orphan_sidecars", False),
        "subtitle_collection_dir": settings.get("subtitle_collection_dir", ""),
        "debug_mode": settings.get("debug_mode", False),
    }


def normalize_code(value):
    text = str(value or "")
    if re.search(r"(?<![A-Za-z0-9])S\d{1,2}E\d{1,3}(?![A-Za-z0-9])", text, re.IGNORECASE):
        return ""
    special = FC2_CODE_RE.search(text)
    if special:
        return f"{special.group(1).upper()}-{special.group(2).upper()}-{special.group(3)}"
    match = CODE_RE.search(text)
    if not match or not any(char.isalpha() for char in match.group(1)):
        return ""
    return f"{match.group(1).upper()}-{match.group(2)}"


def read_video_metadata(video_path, nfo_directory_cache=None, nfo_metadata_cache=None):
    video_path = Path(video_path)
    nfo_directory_cache = nfo_directory_cache if nfo_directory_cache is not None else {}
    nfo_metadata_cache = nfo_metadata_cache if nfo_metadata_cache is not None else {}
    candidates = [video_path.with_suffix(".nfo"), video_path.parent / f"{video_path.parent.name}.nfo"]
    directory_key = str(video_path.parent)
    if directory_key not in nfo_directory_cache:
        try:
            nfo_directory_cache[directory_key] = sorted(video_path.parent.glob("*.nfo"))
        except OSError:
            nfo_directory_cache[directory_key] = []
    siblings = nfo_directory_cache[directory_key]
    if len(siblings) == 1:
        candidates.append(siblings[0])
    nfo = next((candidate for candidate in candidates if candidate.is_file()), None)
    if nfo is None:
        return {}
    try:
        stat = nfo.stat()
        if stat.st_size > 1024 * 1024:
            return {}
        cache_key = str(nfo)
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = nfo_metadata_cache.get(cache_key)
        if cached and cached[0] == signature:
            return dict(cached[1])
        raw = nfo.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(raw)
    except OSError:
        return {}
    except ET.ParseError:
        def match(pattern):
            found = re.search(pattern, raw, re.IGNORECASE | re.MULTILINE)
            return found.group(1).strip() if found else ""
        metadata = {
            "width": count_value(match(r"^\s*width\s*[:=]\s*(\d+)")),
            "height": count_value(match(r"^\s*height\s*[:=]\s*(\d+)")),
            "resolution": match(r"^\s*resolution\s*[:=]\s*([^\r\n]+)"),
            "codec": match(r"^\s*(?:video\s+)?codec\s*[:=]\s*([^\r\n]+)"),
            "language": match(r"^\s*(?:audio\s+|subtitle\s+)?language\s*[:=]\s*([^\r\n]+)"),
            "identifier_text": " ".join(filter(None, (
                match(r"^\s*title\s*[:=]\s*([^\r\n]+)"),
                match(r"^\s*originaltitle\s*[:=]\s*([^\r\n]+)"),
                match(r"^\s*id\s*[:=]\s*([^\r\n]+)"),
            ))),
        }
        nfo_metadata_cache[cache_key] = (signature, metadata)
        return dict(metadata)
    values = {}
    for element in root.iter():
        tag = str(element.tag).rsplit("}", 1)[-1].casefold()
        value = " ".join((element.text or "").split())
        if tag and value:
            values.setdefault(tag, []).append(value)
    def first(*keys):
        return next((value for key in keys for value in values.get(key, []) if value), "")
    metadata = {
        "width": count_value(first("width")),
        "height": count_value(first("height")),
        "resolution": first("resolution", "videoresolution"),
        "codec": " ".join(values.get("codec", []) + values.get("videocodec", [])),
        "language": " ".join(values.get("language", []) + values.get("audiolanguage", []) + values.get("subtitlelanguage", [])),
        "identifier_text": " ".join(values.get("title", []) + values.get("originaltitle", []) + values.get("id", []) + values.get("uniqueid", [])),
    }
    nfo_metadata_cache[cache_key] = (signature, metadata)
    return dict(metadata)


def release_rank(title, metadata=None):
    metadata = metadata or {}
    label_sources = [metadata.get(key) for key in ("labelsNew", "labels", "labelList", "label_list", "tags") if metadata.get(key)]
    def metadata_text(value):
        if isinstance(value, dict):
            return " ".join(metadata_text(item) for key, item in value.items() if str(key).casefold() in {
                "name", "label", "labelname", "label_name", "tag", "value", "text", "title", "code",
                "description", "desc", "tooltip", "tip", "titletext", "title_text", "hovertext", "hover_text",
                "data", "items", "labels", "list", "tags",
            })
        if isinstance(value, (list, tuple, set)):
            return " ".join(metadata_text(item) for item in value)
        return str(value or "")
    def label_name(value):
        if not isinstance(value, dict):
            return str(value or "").strip()
        return " ".join(str(value.get(key, "")).strip() for key in ("name", "label", "labelName", "label_name", "tag", "text", "value") if value.get(key) not in (None, ""))
    labels_new_items = []
    def collect_labels(value, target):
        if isinstance(value, dict):
            if any(key in value for key in ("name", "label", "labelName", "label_name", "tag", "text", "title")):
                target.append(value)
            for key in ("data", "items", "labels", "list", "tags"):
                if key in value:
                    collect_labels(value[key], target)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                collect_labels(item, target)
        elif value not in (None, ""):
            target.append(value)
    collect_labels(metadata.get("labelsNew", []), labels_new_items)
    embedded_label = any("中字" in label_name(item) for item in labels_new_items)
    labels = " ".join(metadata_text(value) for value in label_sources)
    tooltip_text = " ".join(metadata_text(value) for key, value in metadata.items() if str(key).casefold() in {
        "tooltip", "tip", "titletext", "title_text", "hovertext", "hover_text", "labeltooltip", "label_tooltip",
    })
    resolution_tags = " ".join(metadata_text(metadata.get(key)) for key in ("resolution", "videoResolution", "video_resolution", "definition", "quality", "standard", "standardName", "standard_name", "standardTag", "standard_tag"))
    codec_tags = " ".join(metadata_text(metadata.get(key)) for key in ("codec", "videoCodec", "video_codec", "videoCodecName", "video_codec_name", "videoEncoding", "video_encoding", "encoding"))
    subtitle_tags = " ".join(metadata_text(metadata.get(key)) for key in (
        "smallDescr", "subtitle", "subtitleType", "subtitle_type", "subtitleLanguage",
        "subtitle_language", "language", "description", "descr", "audioInfo", "audio_info",
        "processing", "processingType", "processing_type", "processingName",
        "processing_name", "mediaInfo", "media_info",
    ))
    text = " ".join((
        str(title or ""), resolution_tags, codec_tags,
        str(metadata.get("resolution", "")), str(metadata.get("codec", "")),
        str(metadata.get("language", "")), str(metadata.get("audio_language", "")),
        str(metadata.get("subtitle_language", "")), subtitle_tags,
        str(labels), tooltip_text,
    )).casefold()
    height = count_value(metadata.get("height", 0))
    resolution = 2 if height >= 2160 or re.search(r"(?:2160\s*p?|4\s*k|uhd|3840\s*[x×]\s*2160)", text) else 1 if height >= 1080 or re.search(r"1080\s*p?|fhd|1920\s*[x×]\s*1080", text) else 0
    has_chinese_subtitle = metadata.get("hasChineseSubtitle") is True or str(metadata.get("hasChineseSubtitle", "")).casefold() in {"1", "true", "yes"}
    if "labelsNew" in metadata or "hasChineseSubtitle" in metadata:
        chinese = 2 if embedded_label else 1 if has_chinese_subtitle else 0
    else:
        embedded_chinese = bool(re.search(r"[内內]嵌\s*(?:中文|中字|字幕)|(?:中文|中字|字幕)\s*[内內]嵌|[内內]封\s*(?:中文|中字|字幕)|(?:国语|國語|普通话|普通話)\s*(?:配音|音轨|音軌)|中配|国配|國配|mandarin\s+(?:audio|dub)|chinese\s+(?:audio|dub)", text))
        subtitle_chinese = bool(re.search(r"中文字幕|中字|中文|簡體中文|繁體中文|简体中文|繁体中文|简中|繁中|chinese|\bchs\b|\bcht\b|\b(?:chi|zho|zh)\b|字幕", text))
        chinese = 2 if embedded_chinese else 1 if subtitle_chinese or metadata.get("subtitle_available") else 0
    codec = 2 if re.search(r"x\.?265|h\.?265|hevc", text) else 1 if re.search(r"x\.?264|h\.?264|avc", text) else 0
    return resolution, chinese, codec


def explicit_resolution_rank(resolution, height=0):
    height = count_value(height)
    if height:
        return 2 if height >= 2160 else 1 if height >= 1080 else 0
    text = str(resolution or "").casefold().replace(" ", "")
    if not text:
        return None
    if re.search(r"4k|2160|uhd|3840[x×]2160", text):
        return 2
    if re.search(r"1080|fhd|1920[x×]1080", text):
        return 1
    if re.search(r"(?:720|576|480)p?|hd|sd", text):
        return 0
    return None


def list_local_video_files(download_dir):
    root = Path(download_dir)
    if not root.is_dir():
        raise AppError(f"订阅目录不存在或应用用户无法访问：{root}")
    files = []
    walk_errors = []
    def on_walk_error(error):
        walk_errors.append(error)
    for current, directories, filenames in os.walk(root, onerror=on_walk_error, followlinks=False):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for filename in filenames:
            path = Path(current) / filename
            if path.suffix.casefold() in VIDEO_EXTENSIONS:
                files.append(path)
    if walk_errors:
        error = walk_errors[0]
        raise AppError(f"应用用户无权完整读取订阅目录 {root}：{error}")
    return files


def cleanup_orphan_sidecars(download_dir, excluded_dirs=()):
    root = Path(download_dir)
    if not root.is_dir():
        raise AppError(f"订阅目录不存在或应用用户无法访问：{root}")
    videos_by_directory = {}
    sidecars_by_directory = {}
    walk_errors = []
    excluded = {str(Path(value).resolve()) for value in excluded_dirs if value}

    def on_walk_error(error):
        walk_errors.append(error)

    for current, directories, filenames in os.walk(root, onerror=on_walk_error, followlinks=False):
        if any(Path(current).resolve() == Path(value) or Path(value) in Path(current).resolve().parents for value in excluded):
            directories[:] = []
            continue
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        directory = Path(current)
        for filename in filenames:
            path = directory / filename
            if path.is_symlink():
                continue
            suffix = path.suffix.casefold()
            if suffix in VIDEO_EXTENSIONS:
                videos_by_directory.setdefault(directory, []).append(path)
            elif suffix == ".nfo" or suffix in SUBTITLE_EXTENSIONS:
                sidecars_by_directory.setdefault(directory, []).append(path)
    if walk_errors:
        raise AppError(f"应用用户无权完整读取订阅目录 {root}：{walk_errors[0]}")

    root = root.resolve()
    removed = []
    for directory, sidecars in sidecars_by_directory.items():
        videos = videos_by_directory.get(directory, [])
        video_stems = {video.stem.casefold() for video in videos}
        video_codes = {normalize_code(video.stem) for video in videos} - {""}
        for sidecar in sidecars:
            stem = sidecar.stem.casefold()
            code = normalize_code(sidecar.stem)
            paired = stem in video_stems or (code and code in video_codes)
            if not paired:
                paired = any(
                    stem.startswith(tuple(video_stem + separator for separator in (".", "_", "-", " ", "[")))
                    for video_stem in video_stems
                )
            if paired:
                continue
            try:
                sidecar.unlink(missing_ok=True)
                removed.append(str(sidecar))
                parent = sidecar.parent
                while True:
                    try:
                        parent.resolve().relative_to(root)
                    except (OSError, ValueError):
                        break
                    if parent.resolve() == root:
                        break
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except OSError as error:
                raise AppError(f"清理孤立 NFO/字幕失败：{sidecar}：{error}") from error
    return removed


@measure_stage("scan")
def scan_local_releases(download_dir):
    context = check_context()
    key = os.path.realpath(download_dir)
    if context is not None and key in context.scans:
        context.count("scan_hits")
        return copy.deepcopy(context.scans[key])
    performance_count("scan_count")
    matches = _scan_local_releases_uncached(download_dir)
    if context is not None:
        context.scans[key] = copy.deepcopy(matches)
    return matches


def _scan_local_releases_uncached(download_dir):
    root = Path(download_dir)
    files = list_local_video_files(download_dir)
    matches = {}
    nfo_directory_cache = {}
    nfo_metadata_cache = {}
    subtitle_directory_cache = {}
    for path in files:
        code = normalize_code(path.stem)
        metadata = read_video_metadata(path, nfo_directory_cache, nfo_metadata_cache)
        if not code:
            code = normalize_code(metadata.get("identifier_text", ""))
        if not code:
            for parent in path.parents:
                if parent == root.parent:
                    break
                code = normalize_code(parent.name)
                if code:
                    break
        if code:
            title = " ".join((path.parent.name, path.name))
            try:
                video_stem = path.stem.casefold()
                if path.parent not in subtitle_directory_cache:
                    subtitle_directory_cache[path.parent] = [
                        item for item in path.parent.iterdir()
                        if item.is_file() and not item.is_symlink() and item.suffix.casefold() in SUBTITLE_EXTENSIONS
                    ]
                subtitle_files = subtitle_directory_cache[path.parent]
                metadata["subtitle_available"] = any(
                    item.stem.casefold() == video_stem
                    or normalize_code(item.stem) == code
                    or item.stem.casefold().startswith(tuple(video_stem + separator for separator in (".", "_", "-", " ", "[")))
                    for item in subtitle_files
                )
            except OSError:
                metadata["subtitle_available"] = False
            matches.setdefault(code, []).append({"path": str(path), "title": title, "metadata": metadata, "rank": list(release_rank(title, metadata))})
    return matches


def find_local_releases(download_dir, code):
    return scan_local_releases(download_dir).get(code, [])


def local_video_sidecars(video_path):
    video_path = Path(video_path)
    video_stem = video_path.stem.casefold()
    sidecars = []
    try:
        for item in video_path.parent.iterdir():
            if not item.is_file() or item.is_symlink():
                continue
            suffix = item.suffix.casefold()
            if suffix == ".nfo" and item.stem.casefold() == video_stem:
                sidecars.append(item)
            elif suffix in SUBTITLE_EXTENSIONS:
                stem = item.stem.casefold()
                if stem == video_stem or stem.startswith(tuple(video_stem + separator for separator in (".", "_", "-", " ", "["))):
                    sidecars.append(item)
    except OSError:
        return []
    return sidecars


def matching_video_sidecars(video_path, directory_entries):
    video_path = Path(video_path)
    video_stem = video_path.stem.casefold()
    sidecars = []
    for item in directory_entries:
        suffix = item.suffix.casefold()
        if suffix == ".nfo" and item.stem.casefold() == video_stem:
            sidecars.append(item)
        elif suffix in SUBTITLE_EXTENSIONS:
            stem = item.stem.casefold()
            if stem == video_stem or stem.startswith(tuple(video_stem + separator for separator in (".", "_", "-", " ", "["))):
                sidecars.append(item)
    return sidecars


@measure_stage("subtitle")
def collect_subscription_subtitles(rows, collection_dir, performer=""):
    """Export .MT.-marked subtitle files for locally graded Chinese-subtitle videos."""
    summary = {"copied": 0, "unchanged": 0, "failed": 0}
    if not collection_dir:
        return summary
    candidates = {}
    for row in rows:
        code = normalize_code(row.get("code", ""))
        if not code:
            continue
        for video in row.get("files", []):
            rank = video.get("rank") or []
            if len(rank) < 2 or rank[1] != 1:
                continue
            for source_path in video.get("subtitle_paths", []):
                source = Path(source_path)
                extension = source.suffix.casefold()
                if extension not in SUBTITLE_EXTENSIONS or source.is_symlink() or ".mt." not in source.name.casefold():
                    continue
                try:
                    if not source.is_file():
                        continue
                    stat = source.stat()
                except OSError:
                    summary["failed"] += 1
                    continue
                key = code + extension
                candidate = (stat.st_mtime_ns, str(source), source)
                if key not in candidates or candidate[:2] > candidates[key][:2]:
                    candidates[key] = candidate
    if not candidates:
        return summary
    destination = Path(collection_dir)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        summary["failed"] += len(candidates)
        log_event(f"字幕汇总失败，无法创建或访问目录 {destination}：{error}", level="warning", event="subtitle_collection_failed", subscription=performer)
        return summary
    for filename, (source_mtime, _path, source) in sorted(candidates.items()):
        target = destination / filename
        temporary = destination / f".{filename}.tmp-{uuid.uuid4().hex}"
        try:
            if target.is_symlink():
                raise OSError("目标文件为符号链接，已跳过")
            if target.exists():
                if not target.is_file():
                    raise OSError("目标不是普通字幕文件")
                target_stat = target.stat()
                if source.samefile(target) or target_stat.st_mtime_ns > source_mtime:
                    summary["unchanged"] += 1
                    continue
                same = target_stat.st_size == source.stat().st_size
                if same:
                    with source.open("rb") as source_stream, target.open("rb") as target_stream:
                        while True:
                            chunk = source_stream.read(64 * 1024)
                            if chunk != target_stream.read(64 * 1024):
                                same = False
                                break
                            if not chunk:
                                break
                if same:
                    summary["unchanged"] += 1
                    continue
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            summary["copied"] += 1
        except OSError as error:
            summary["failed"] += 1
            log_event(f"字幕汇总失败 {filename}：{error}", level="warning", event="subtitle_collection_failed", subscription=performer)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    log_event(
        f"字幕汇总：复制 {summary['copied']} 个，跳过 {summary['unchanged']} 个，失败 {summary['failed']} 个。目录：{destination}",
        level="warning" if summary["failed"] else "info", event="subtitle_collection_completed", subscription=performer,
    )
    return summary


def remove_local_releases(paths, download_dir, code, allowed_dirs=None):
    invalidate_local_scans(*(allowed_dirs or [download_dir]))
    roots = [Path(value).resolve() for value in (allowed_dirs or [download_dir])]
    videos = []
    for value in paths:
        path = Path(value)
        try:
            resolved = path.resolve()
            inside_roots = False
            for root in roots:
                try:
                    resolved.relative_to(root)
                    inside_roots = True
                    break
                except ValueError:
                    pass
            if not inside_roots:
                raise ValueError("outside configured directories")
        except (OSError, ValueError) as error:
            raise AppError("旧版文件路径不在订阅目录中，已拒绝删除。") from error
        if path.is_symlink() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
            raise AppError("旧版路径不是受支持的视频文件，已拒绝删除。")
        if path.exists() and normalize_code(path.stem) != code and not any(normalize_code(parent.name) == code for parent in path.parents):
            raise AppError("旧版文件番号不匹配，已拒绝删除。")
        videos.append(path)
    files_to_remove = list(dict.fromkeys([*videos, *(sidecar for path in videos for sidecar in local_video_sidecars(path))]))
    for path in files_to_remove:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise AppError(f"删除旧版视频或其字幕/NFO失败：{error}") from error


def count_value(value):
    match = re.search(r"\d[\d,]*", str(value or "").replace(" ", ""))
    return int(match.group().replace(",", "")) if match else 0


def _mteam_request_once(path, token, payload=None, method=None, limit=MAX_RESPONSE_BYTES, form=False, multipart=False):
    if not path.startswith("/") or ".." in Path(path).parts:
        raise AppError("M-Team API 路径无效。")
    url = f"{MTEAM_API_BASE}{path}"
    if urllib.parse.urlparse(url).hostname != MTEAM_API_HOST:
        raise AppError("M-Team API 地址无效。")
    headers = {"Accept": "application/json, application/octet-stream, */*", "User-Agent": "AVSubscribe/1.0", "x-api-key": token}
    data = None
    if payload is not None:
        if multipart:
            boundary = "----AVSubscribe" + uuid.uuid4().hex
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            body = bytearray()
            for name, value in payload.items():
                body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode("ascii"))
                body.extend(str(value).encode("utf-8") + b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode("ascii"))
            data = bytes(body)
        elif form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(payload).encode("ascii")
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
    try:
        wait_for_mteam_request()
        performance_count("mteam_requests")
        with MTEAM_OPENER.open(request, timeout=30) as response:
            body = response.read(limit + 1)
            metadata = {"status": response.status, "content_type": response.headers.get_content_type(), "content_disposition": response.headers.get("Content-Disposition", ""), "url": response.geturl()}
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AppError("M-Team 令牌无效或无权访问影片区。") from error
        if error.code == 429:
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            try:
                retry_after = int(retry_after)
            except (TypeError, ValueError):
                retry_after = MTEAM_DEFAULT_COOLDOWN
            raise MTeamRateLimitError("M-Team 请求过于频繁，已暂停本轮请求。", retry_after, retry_after_provided=True) from error
        raise AppError(f"M-Team API 请求失败 (HTTP {error.code})。") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise AppError("连接 M-Team API 失败，请检查 NAS 网络。") from error
    if len(body) > limit:
        raise AppError("M-Team 返回内容超过允许大小。")
    return body, metadata


def wait_for_mteam_retry(error, request_label, attempt):
    previous = read_json(MTEAM_LIMIT_FILE, {})
    consecutive, retry_after = rate_limit_retry_delay(error, {"consecutive_rate_limits": attempt - 1})
    retry_at = time.time() + retry_after
    performer = getattr(MTEAM_POLL_CACHE, "performer", "")
    try:
        status = read_json(STATUS_FILE, {})
    except AppError:
        status = {}
    state = {
        **previous,
        "retry_at": retry_at,
        "retry_after": retry_after,
        "consecutive_rate_limits": consecutive,
        "message": str(error),
        "request": request_label,
        "round_id": getattr(MTEAM_POLL_CACHE, "round_id", ""),
        "trigger": status.get("trigger", "scheduled"),
        "subscription_id": status.get("subscription_id", ""),
        "refresh_existing_info": bool(status.get("refresh_existing_info", False)),
        "completed_subscription_ids": sorted(getattr(MTEAM_POLL_CACHE, "completed_subscription_ids", set())),
        "limited_performer": performer,
    }
    write_json(MTEAM_LIMIT_FILE, state)
    if status.get("state") in ("running", "waiting_retry"):
        status.update({"state": "waiting_retry", "retry_at": retry_at, "retry_in": retry_after, "completed_at": "", "message": f"M-Team 请求被限流，等待 {retry_after} 秒后继续重试当前请求。"})
        write_json(STATUS_FILE, status)
    log_event(f"M-Team 限流，第 {consecutive} 次；等待 {retry_after} 秒后继续重试当前请求：{request_label}。", event="retry_waiting", subscription=performer)
    remaining = retry_after
    wait_started = time.perf_counter()
    while remaining > 0 and not STOP_SCHEDULER:
        time.sleep(min(5, remaining))
        remaining = max(0, int(retry_at - time.time() + 0.999))
    context = check_context()
    if context is not None:
        context.wait_seconds += time.perf_counter() - wait_started
        context.resume_after_wait()
        downloader = getattr(MTEAM_POLL_CACHE, "downloader", None)
        if isinstance(downloader, CheckDownloader):
            downloader.invalidate()
    if STOP_SCHEDULER:
        raise MTeamRequestInterrupted("后台调度器正在停止，当前 M-Team 请求未完成。")
    try:
        status = read_json(STATUS_FILE, {})
    except AppError:
        status = {}
    if status.get("state") == "waiting_retry":
        status.update({"state": "running", "retry_at": 0, "retry_in": 0, "message": "限流冷却结束，正在继续重试当前请求。"})
        write_json(STATUS_FILE, status)
    log_event(f"限流冷却结束，继续重试当前请求：{request_label}。", event="retry_resuming", subscription=performer)


def mteam_request(path, token, payload=None, method=None, limit=MAX_RESPONSE_BYTES, form=False, multipart=False):
    attempt = 0
    while True:
        try:
            result = _mteam_request_once(path, token, payload, method, limit, form, multipart)
            if attempt and getattr(MTEAM_POLL_CACHE, "active", None) is not None:
                MTEAM_LIMIT_FILE.unlink(missing_ok=True)
            return result
        except MTeamRateLimitError as error:
            if getattr(MTEAM_POLL_CACHE, "active", None) is None:
                raise
            attempt += 1
            wait_for_mteam_retry(error, path, attempt)


def _mteam_json_once(path, token, payload=None, method="POST", form=False, multipart=False):
    data, _ = mteam_request(path, token, payload, method=method, limit=4 * 1024 * 1024, form=form, multipart=multipart)
    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppError("M-Team API 返回了无效 JSON。") from error
    if not isinstance(result, dict):
        raise AppError("M-Team API 返回结构无法识别。")
    code = result.get("code")
    if code not in (None, 0, 200, "0", "200"):
        message = str(result.get("message", "")).replace(token, "[已隐藏]")
        if re.search(r"请求过于频繁|請求過於頻繁|rate.?limit|too many requests", message, re.IGNORECASE):
            retry_after = mteam_retry_after(result)
            raise MTeamRateLimitError(
                "M-Team 提示请求过于频繁，已暂停本轮请求。",
                retry_after or MTEAM_DEFAULT_COOLDOWN,
                retry_after_provided=retry_after is not None,
            )
        raise AppError(message or f"M-Team API 错误 ({code})。")
    return result


def mteam_json(path, token, payload=None, method="POST", form=False, multipart=False):
    attempt = 0
    while True:
        try:
            result = _mteam_json_once(path, token, payload, method, form, multipart)
            if attempt and getattr(MTEAM_POLL_CACHE, "active", None) is not None:
                MTEAM_LIMIT_FILE.unlink(missing_ok=True)
            return result
        except MTeamRateLimitError as error:
            if getattr(MTEAM_POLL_CACHE, "active", None) is None:
                raise
            attempt += 1
            wait_for_mteam_retry(error, path, attempt)


def mteam_retry_after(result):
    retry_keys = {"retryafter", "retryafterseconds", "retryseconds", "retryin"}
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = re.sub(r"[^a-z]", "", str(key).casefold())
                if normalized_key in retry_keys:
                    try:
                        seconds = int(float(item))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if seconds > 1_000_000_000:
                        seconds -= int(time.time())
                    if seconds > 0:
                        return max(60, min(seconds, 21600))
                elif isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(item for item in value if isinstance(item, (dict, list)))
    return None


def rate_limit_retry_delay(error, previous_limit):
    consecutive = max(0, int(previous_limit.get("consecutive_rate_limits", 0) or 0)) + 1
    base = error.retry_after if error.retry_after_provided else MTEAM_DEFAULT_COOLDOWN
    delay = min(21600, max(base, MTEAM_DEFAULT_COOLDOWN) * (2 ** min(consecutive - 1, 5)))
    return consecutive, delay


def result_data(result):
    value = result.get("data")
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


@measure_stage("detail")
def mteam_detail(identifier, token, force_refresh=False):
    cache = getattr(MTEAM_POLL_CACHE, "active", None)
    cache_key = str(identifier)
    if not force_refresh and cache is not None and cache_key in cache["details"]:
        performance_count("detail_hits")
        return dict(cache["details"][cache_key])
    result = mteam_json("/torrent/detail", token, {"id": identifier}, multipart=True)
    detail = result_data(result)
    if isinstance(detail, dict):
        for key in ("torrent", "detail", "item"):
            if isinstance(detail.get(key), dict):
                detail = detail[key]
                break
        if cache is not None:
            cache["details"][cache_key] = dict(detail)
        return detail
    raise AppError("M-Team 影片详情结果结构无法识别。")


def mteam_option_map(endpoint, token):
    cache = getattr(MTEAM_POLL_CACHE, "active", None)
    if cache is not None and endpoint in cache["options"]:
        performance_count("option_hits")
        return dict(cache["options"][endpoint])
    data = result_data(mteam_json(endpoint, token, {}))
    items = data if isinstance(data, list) else next((data.get(key) for key in ("data", "records", "items", "results", "list") if isinstance(data, dict) and isinstance(data.get(key), list)), [])
    output = {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        names = [item.get(key) for key in ("name", "nameCn", "nameZh", "nameEn", "label", "value") if item.get(key) not in (None, "")]
        output[str(item["id"])] = " ".join(str(name) for name in names)
    if cache is not None:
        cache["options"][endpoint] = dict(output)
    return output


@measure_stage("search")
def mteam_search(keyword, token, refresh_existing_info=False, refresh_codes=None):
    refresh_codes = set(refresh_codes or ())
    result = mteam_json("/torrent/search", token, {
        "mode": "adult", "categories": [], "visible": 1, "pageNumber": 1, "pageSize": 100,
        "sortDirection": "DESC", "sortField": "CREATED_DATE", "keyword": keyword,
    })
    data = result_data(result)
    items = data if isinstance(data, list) else next((data.get(key) for key in ("data", "records", "items", "results", "list") if isinstance(data, dict) and isinstance(data.get(key), list)), None)
    if items is None:
        raise AppError("M-Team 影片搜索结果结构无法识别。")
    standard_map = {}
    codec_map = {}
    if any(isinstance(item, dict) and str(item.get("standard", "")).isdigit() for item in items):
        standard_map = mteam_option_map("/torrent/standardList", token)
    if any(isinstance(item, dict) and str(item.get("videoCodec", "")).isdigit() for item in items):
        codec_map = mteam_option_map("/torrent/videoCodecList", token)
    output = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name", item.get("title", "")))
        code = normalize_code(title)
        identifier = item.get("id", item.get("torrentId", item.get("torrent_id")))
        if not code or identifier is None:
            continue
        metadata = {
            "labelsNew": item.get("labelsNew") or item.get("labels") or item.get("labelList") or item.get("label_list") or item.get("tags", []),
            "labels": item.get("labels", []),
            "labelList": item.get("labelList", item.get("label_list", [])),
            "smallDescr": item.get("smallDescr", ""),
            "subtitle": item.get("subtitle", ""),
            "subtitleType": item.get("subtitleType", item.get("subtitle_type", "")),
            "hasChineseSubtitle": item.get("hasChineseSubtitle", False),
            "description": item.get("description", item.get("descr", "")),
            "tooltip": item.get("tooltip", item.get("labelTooltip", item.get("label_tooltip", ""))),
            "audioInfo": item.get("audioInfo", item.get("audio_info", "")),
            "resolution": next((item.get(key) for key in ("resolution", "videoResolution", "video_resolution", "definition", "quality") if item.get(key)), ""),
            "videoCodec": next((item.get(key) for key in ("videoCodec", "video_codec", "videoEncoding", "video_encoding", "codec") if item.get(key)), ""),
            "standard": item.get("standard", ""),
            "standardTag": item.get("standardTag", item.get("standard_tag", [])),
        }
        detail = {}
        if refresh_existing_info and code in refresh_codes:
            language_marker = json.dumps((metadata["labelsNew"], metadata["subtitleType"], metadata["smallDescr"]), ensure_ascii=False)
            has_language = "hasChineseSubtitle" in item or re.search(r"中字|中文|字幕|subtitle", language_marker, re.IGNORECASE)
            has_resolution = any(metadata.get(key) not in (None, "", 0) for key in ("resolution", "standard", "height"))
            has_codec = metadata.get("videoCodec") not in (None, "", 0)
            if not (has_resolution and has_codec and has_language):
                try:
                    detail = mteam_detail(identifier, token)
                except MTeamRateLimitError:
                    raise
                except AppError:
                    detail = {}
                for key in (
                    "labels", "labelsNew", "labelList", "label_list", "tags", "smallDescr", "subtitle", "subtitleType", "subtitle_type",
                    "subtitleLanguage", "subtitle_language", "language", "description", "descr",
                    "tooltip", "labelTooltip", "label_tooltip",
                    "audioInfo", "audio_info", "hasChineseSubtitle", "standard", "videoCodec",
                    "resolution", "videoResolution", "video_resolution", "definition", "quality", "height",
                    "video_codec", "videoEncoding", "video_encoding", "codec",
                ):
                    if detail.get(key) not in (None, "", [], {}, 0):
                        metadata[key] = detail[key]
                if str(metadata.get("standard", "")).isdigit() and str(metadata["standard"]) not in standard_map:
                    standard_map.update(mteam_option_map("/torrent/standardList", token))
                if str(metadata.get("videoCodec", "")).isdigit() and str(metadata["videoCodec"]) not in codec_map:
                    codec_map.update(mteam_option_map("/torrent/videoCodecList", token))
        if str(metadata["standard"]).isdigit():
            metadata["standardName"] = standard_map.get(str(metadata["standard"]), "")
        if str(metadata["videoCodec"]).isdigit():
            metadata["videoCodecName"] = codec_map.get(str(metadata["videoCodec"]), "")
        language_marker = json.dumps((metadata["labelsNew"], metadata["subtitleType"], metadata["smallDescr"]), ensure_ascii=False)
        has_language = "hasChineseSubtitle" in metadata or re.search(r"中字|中文|字幕|subtitle", language_marker, re.IGNORECASE)
        metadata_complete = bool(
            (metadata.get("resolution") or metadata.get("standardName") or metadata.get("standard") or metadata.get("height"))
            and (metadata.get("videoCodecName") or metadata.get("videoCodec") or metadata.get("video_codec") or metadata.get("codec"))
            and has_language
        )
        output.append({
            "id": str(identifier), "code": code, "title": title,
            "subtitle": str(metadata.get("smallDescr") or metadata.get("subtitle") or ""),
            "resolution": str(metadata.get("resolution") or metadata.get("standardName") or metadata.get("standard") or ""),
            "video_codec": str(metadata.get("videoCodecName") or metadata.get("videoCodec") or metadata.get("video_codec") or metadata.get("codec") or ""),
            "rank": list(release_rank(title, metadata)), "metadata_complete": bool(metadata_complete),
            "seeders": count_value(item.get("seeders", item.get("seed", item.get("seedersCount", 0)))),
            "created": str(item.get("createdDate", item.get("createdAt", ""))),
        })
    return output


def candidate_metadata(candidate):
    subtitle = candidate.get("subtitle", "")
    return {
        "id": candidate["id"],
        "title": candidate["title"],
        "subtitle": subtitle,
        "subtitle_checked": bool(candidate.get("subtitle_checked", subtitle)),
        "rank": candidate["rank"],
        "resolution": candidate.get("resolution", ""),
        "video_codec": candidate.get("video_codec", ""),
        "mediainfo": str(candidate.get("mediainfo", "") or ""),
        "mediainfo_checked": bool(candidate.get("mediainfo_checked", bool(candidate.get("mediainfo")))),
        "metadata_complete": bool(candidate.get("metadata_complete", False)),
        "mteam_rank_version": MTEAM_RANK_VERSION,
    }


def metadata_is_complete(metadata):
    return bool(metadata and metadata.get("metadata_complete") and metadata.get("mteam_rank_version") == MTEAM_RANK_VERSION)


def detail_mediainfo(detail):
    return str(next((detail.get(key) for key in ("mediainfo", "mediaInfo", "media_info") if detail.get(key)), "") or "")


def enrich_candidate_from_detail(candidate, token, detail=None):
    detail = detail if detail is not None else mteam_detail(candidate["id"], token)
    metadata = dict(detail)
    if "labelsNew" not in metadata and "labels" in metadata:
        metadata["labelsNew"] = metadata["labels"]
    resolution = next((metadata.get(key) for key in ("resolution", "videoResolution", "video_resolution", "definition", "quality", "standardName", "standard_name", "standard", "height") if metadata.get(key) not in (None, "", 0)), "")
    codec = next((metadata.get(key) for key in ("videoCodecName", "video_codec_name", "videoCodec", "video_codec", "videoEncoding", "video_encoding", "codec") if metadata.get(key) not in (None, "", 0)), "")
    standard_map = {}
    codec_map = {}
    if str(resolution).isdigit():
        standard_map = mteam_option_map("/torrent/standardList", token)
        resolution = standard_map.get(str(resolution), resolution)
    if str(codec).isdigit():
        codec_map = mteam_option_map("/torrent/videoCodecList", token)
        codec = codec_map.get(str(codec), codec)
    language_keys = ("labelsNew", "labels", "smallDescr", "subtitle", "subtitleType", "subtitle_type", "subtitleLanguage", "subtitle_language", "language", "description", "descr", "hasChineseSubtitle")
    has_language = any(key in metadata for key in language_keys)
    metadata_complete = bool(resolution and codec and has_language)
    rank = list(release_rank(candidate["title"], {**metadata, "resolution": resolution or candidate.get("resolution", ""), "videoCodec": codec or candidate.get("video_codec", "")}))
    detail_resolution_rank = explicit_resolution_rank(resolution, metadata.get("height", 0))
    if detail_resolution_rank is not None:
        rank[0] = detail_resolution_rank
    previous_rank = candidate.get("rank", [0, 0, 0])
    if not resolution:
        rank[0] = previous_rank[0]
    if not has_language:
        rank[1] = previous_rank[1]
    if not codec:
        rank[2] = previous_rank[2]
    return {
        **candidate,
        "subtitle": str(next((detail.get(key) for key in ("smallDescr", "small_descr", "subtitle", "subTitle") if detail.get(key)), candidate.get("subtitle", "")) or ""),
        "resolution": str(resolution or candidate.get("resolution", "")),
        "video_codec": str(codec or candidate.get("video_codec", "")),
        "rank": rank,
        "metadata_complete": metadata_complete,
        "mediainfo": detail_mediainfo(metadata),
        "mediainfo_checked": True,
        "_mteam_detail": detail,
    }


def detail_contains_performer(detail, performer):
    expected = normalized_performer_name(performer)
    if not expected:
        return False
    values = []

    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                values.append(str(key))
                collect(child)
        elif isinstance(value, (list, tuple, set)):
            for child in value:
                collect(child)
        elif value is not None:
            values.append(str(value))

    collect(detail)
    haystack = "".join(char for char in unicodedata.normalize("NFKC", " ".join(values)).casefold() if char.isalnum())
    return expected in haystack


def normalized_performer_name(performer):
    return "".join(char for char in unicodedata.normalize("NFKC", str(performer)).casefold() if char.isalnum())


def find_download_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "downloadUrl", "download_url", "token", "downloadToken", "credential", "dlToken"):
            if value.get(key):
                return str(value[key])
    return ""


def mteam_download_url(value):
    if value.startswith(("https://", "http://")):
        parsed = urllib.parse.urlparse(value)
        allowed_host = parsed.hostname == MTEAM_API_HOST or is_mteam_host(parsed.hostname or "") or is_mteam_download_host(parsed.hostname or "")
        if parsed.scheme != "https" or not allowed_host or parsed.username or parsed.password or parsed.port:
            raise AppError(f"M-Team 返回了不受信任的种子下载主机：{parsed.hostname or '未知'}。")
        return value
    if value.startswith("/api/"):
        return f"https://{MTEAM_API_HOST}{value}"
    if value.startswith("/"):
        return f"{MTEAM_API_BASE}{value}"
    if value.startswith("api/"):
        return f"https://{MTEAM_API_HOST}/{value}"
    if "torrent/" in value:
        return f"{MTEAM_API_BASE}/torrent/{value.split('torrent/', 1)[-1].lstrip('/')}"
    return f"{MTEAM_API_BASE}/torrent/download?token={urllib.parse.quote(value, safe='%')}"


def _mteam_download_request_once(url, limit=MAX_RESPONSE_BYTES):
    if not url.startswith("https://"):
        raise AppError("M-Team 返回了无效的种子下载地址。")
    request = urllib.request.Request(url, headers={
        "Accept": "application/x-bittorrent, application/octet-stream, application/json, */*",
        "User-Agent": "AVSubscribe/1.0",
    }, method="GET")
    try:
        wait_for_mteam_request()
        with MTEAM_DOWNLOAD_OPENER.open(request, timeout=30) as response:
            body = response.read(limit + 1)
            metadata = {"status": response.status, "content_type": response.headers.get_content_type(), "content_disposition": response.headers.get("Content-Disposition", ""), "url": response.geturl()}
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AppError("M-Team 种子下载授权无效或已过期。") from error
        if error.code == 429:
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            try:
                retry_after = int(retry_after)
            except (TypeError, ValueError):
                retry_after = MTEAM_DEFAULT_COOLDOWN
            raise MTeamRateLimitError("M-Team 种子下载请求过于频繁，已暂停本轮请求。", retry_after, retry_after_provided=True) from error
        raise AppError(f"M-Team 种子下载失败 (HTTP {error.code})。") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise AppError("连接 M-Team 种子下载节点失败，请检查 NAS 网络。") from error
    if len(body) > limit:
        raise AppError("M-Team 种子文件超过允许大小。")
    return body, metadata


def mteam_download_request(url, limit=MAX_RESPONSE_BYTES):
    attempt = 0
    while True:
        try:
            result = _mteam_download_request_once(url, limit)
            if attempt and getattr(MTEAM_POLL_CACHE, "active", None) is not None:
                MTEAM_LIMIT_FILE.unlink(missing_ok=True)
            return result
        except MTeamRateLimitError as error:
            if getattr(MTEAM_POLL_CACHE, "active", None) is None:
                raise
            attempt += 1
            wait_for_mteam_retry(error, "种子文件下载", attempt)


def fetch_torrent(torrent_id, token):
    link = result_data(mteam_json("/torrent/genDlToken", token, {"id": str(torrent_id)}, form=True))
    value = find_download_value(link)
    if not value:
        raise AppError("M-Team 未返回有效的种子下载链接。")
    data, metadata = mteam_download_request(mteam_download_url(value))
    if metadata["content_type"] == "application/json" or data.lstrip().startswith(b"{"):
        try:
            error = json.loads(data.decode("utf-8", errors="replace"))
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
        except json.JSONDecodeError:
            message = ""
        raise AppError("M-Team 种子下载失败" + (f"：{message}" if message else "。"))
    if not data.startswith(b"d"):
        raise AppError("M-Team 下载响应不是有效种子文件。")
    return data


def mteam_subtitle_items(result):
    data = result_data(result)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "records", "items", "results", "list"):
            if isinstance(data.get(key), list):
                return data[key]
    raise AppError("M-Team 字幕搜索结果结构无法识别。")


def mteam_subtitle_language_map(token):
    cache = getattr(MTEAM_POLL_CACHE, "active", None)
    if cache is not None and cache.get("subtitle_languages") is not None:
        return dict(cache["subtitle_languages"])
    data = result_data(mteam_json("/subtitle/langs", token))
    languages = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id", item.get("value", item.get("langId")))
            label = item.get("langName", item.get("name", item.get("label", item.get("text", item.get("lang", "")))))
            if identifier is not None and label:
                languages[str(identifier)] = str(label)
    elif isinstance(data, dict):
        for identifier, item in data.items():
            if isinstance(item, dict):
                label = item.get("langName", item.get("name", item.get("label", item.get("text", ""))))
                identifier = item.get("id", item.get("value", identifier))
            else:
                label = item
            if label:
                languages[str(identifier)] = str(label)
    if cache is not None:
        cache["subtitle_languages"] = dict(languages)
    return languages


def mteam_subtitle_count(value):
    if isinstance(value, int):
        return value
    match = re.search(r"\d[\d,]*", str(value or "").replace(" ", ""))
    return int(match.group().replace(",", "")) if match else 0


def mteam_subtitle_language(item, languages):
    value = item.get("lang", item.get("langId", item.get("language", "")))
    if isinstance(value, dict):
        value = value.get("name", value.get("label", value.get("id", "")))
    label = str(languages.get(str(value), value) or "").strip()
    normalized = unicodedata.normalize("NFKC", label).casefold()
    if not any(marker in normalized for marker in ("chinese", "中文", "汉语", "漢語", "mandarin", "繁体", "繁體", "简体", "簡體", "zh-hk", "zh-tw", "zh-cn", "zh-hans", "zh-hant", "\bchi\b", "\bzho\b")):
        return ""
    if "简体" in normalized or "簡體" in normalized or "simplified" in normalized or "zh-cn" in normalized or "zh-hans" in normalized:
        return "zh-CN"
    if "繁体" in normalized or "繁體" in normalized or "traditional" in normalized or any(tag in normalized for tag in ("zh-hk", "zh-tw", "zh-hant")):
        return "zh-HK"
    return "zh-HK"


def mteam_subtitle_extension(metadata, data=b""):
    disposition = str(metadata.get("content_disposition", ""))
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^;\"]+)", disposition, re.IGNORECASE)
    extension = Path(urllib.parse.unquote(match.group(1).strip().strip('"'))).suffix.casefold() if match else ""
    if extension in SUBTITLE_EXTENSIONS:
        return extension
    media_type = str(metadata.get("content_type", "")).casefold().split(";", 1)[0].strip()
    extension = {"application/x-subrip": ".srt", "text/srt": ".srt", "text/x-ssa": ".ssa", "text/x-ass": ".ass", "text/vtt": ".vtt"}.get(media_type, "")
    if extension:
        return extension
    return ".srt" if data.lstrip().startswith((b"1\n", b"1\r\n", b"WEBVTT", b"[Script Info]")) else ""


@measure_stage("subtitle")
def mteam_subtitle_download(code, torrent_id, video_paths, token):
    settings = get_settings()
    results = mteam_subtitle_items(mteam_json(
        f"/subtitle/list?id={urllib.parse.quote(str(torrent_id), safe='')}", token, method="POST",
    ))
    needs_language_map = any(
        isinstance(item, dict)
        and (
            isinstance(item.get("lang", item.get("langId", item.get("language", ""))), dict)
            or str(item.get("lang", item.get("langId", item.get("language", "")))).isdigit()
        )
        for item in results
    )
    languages = mteam_subtitle_language_map(token) if needs_language_map else {}
    candidates = []
    unknown_languages = 0
    foreign_ids = 0
    for item in results:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        linked_torrent_id = next((item.get(key) for key in ("torrentId", "torrent_id", "torrentID", "videoId", "video_id", "resourceId", "resource_id", "mteamId", "mteam_id", "movieId", "movie_id") if item.get(key) not in (None, "")), None)
        if isinstance(linked_torrent_id, dict):
            linked_torrent_id = linked_torrent_id.get("id", linked_torrent_id.get("torrentId"))
        if linked_torrent_id is not None and str(linked_torrent_id) != str(torrent_id):
            foreign_ids += 1
            continue
        language = mteam_subtitle_language(item, languages)
        if language:
            candidates.append((mteam_subtitle_count(item.get("hits", item.get("downloads", item.get("downloadCount", 0)))), item, language))
        else:
            unknown_languages += 1
    log_debug(
        settings,
        f"M-Team 字幕列表 id={torrent_id}（{code}）：返回 {len(results)} 条，识别中文字幕 {len(candidates)} 条，排除其他影片 ID {foreign_ids} 条，无法识别语言 {unknown_languages} 条。",
    )
    if not candidates:
        return {"status": "not_found", "files": []}
    _downloads, chosen, language = max(candidates, key=lambda row: row[0])
    link = result_data(mteam_json(f"/subtitle/genlink?id={urllib.parse.quote(str(chosen['id']), safe='')}", token, method="POST"))
    if isinstance(link, dict):
        link = link.get("credential", link.get("token", ""))
    if not isinstance(link, str) or not link:
        raise AppError("M-Team 没有返回有效的字幕下载凭据。")
    data, metadata = mteam_request(f"/subtitle/dlV2?credential={urllib.parse.quote(link, safe='%')}", token, limit=MAX_SUBTITLE_BYTES)
    archive = io.BytesIO(data)
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as source:
                accepted = []
                total_size = 0
                for member in source.infolist():
                    name = member.filename.replace("\\", "/")
                    path = Path(name)
                    if path.is_absolute() or ".." in path.parts or (len(name) > 1 and name[1] == ":"):
                        raise AppError("M-Team 字幕压缩包包含越界路径。")
                    if member.is_dir():
                        continue
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise AppError("M-Team 字幕压缩包包含符号链接。")
                    if path.suffix.casefold() in SUBTITLE_EXTENSIONS:
                        if member.file_size > MAX_SUBTITLE_BYTES:
                            raise AppError("M-Team 字幕文件超过允许大小。")
                        total_size += member.file_size
                        if total_size > MAX_SUBTITLE_BYTES:
                            raise AppError("M-Team 字幕压缩包解压体积超过限制。")
                        accepted.append(member)
                if not accepted:
                    raise AppError("M-Team 字幕压缩包内没有支持的字幕文件。")
                data = source.read(accepted[0])
                extension = Path(accepted[0].filename).suffix.casefold()
        except zipfile.BadZipFile as error:
            raise AppError("M-Team 字幕压缩包无效。") from error
    else:
        if metadata.get("content_type") == "application/json" or data.lstrip().startswith(b"{"):
            raise AppError("M-Team 字幕下载接口返回 JSON，而不是字幕文件。")
        extension = mteam_subtitle_extension(metadata, data)
    if extension not in SUBTITLE_EXTENSIONS or not data or len(data) > MAX_SUBTITLE_BYTES:
        raise AppError("M-Team 返回的内容不是有效字幕文件。")
    saved = []
    for video in video_paths:
        video = Path(video)
        target = video.with_name(f"{video.stem}.MT.{language}{extension}")
        if target.parent.resolve() != video.parent.resolve():
            raise AppError("字幕文件名无效。")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        invalidate_local_scans(str(video.parent))
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        saved.append(str(target))
    return {"status": "downloaded", "id": str(chosen["id"]), "downloads": _downloads, "language": language, "files": saved}


def bencode_end(data, start):
    marker = data[start:start + 1]
    if marker == b"i":
        return data.index(b"e", start) + 1
    if marker in (b"l", b"d"):
        position = start + 1
        while data[position:position + 1] != b"e":
            position = bencode_end(data, position)
        return position + 1
    colon = data.index(b":", start)
    length = int(data[start:colon])
    return colon + 1 + length


def torrent_info_hash(data):
    if not data.startswith(b"d"):
        raise AppError("种子文件格式无效。")
    position = 1
    while data[position:position + 1] != b"e":
        key_end = bencode_end(data, position)
        colon = data.index(b":", position)
        key = data[colon + 1:key_end]
        value_start = key_end
        value_end = bencode_end(data, value_start)
        if key == b"info":
            return hashlib.sha1(data[value_start:value_end]).hexdigest()
        position = value_end
    raise AppError("种子文件缺少 info 区块。")


def read_response_body(response, limit):
    reader = getattr(response, "read1", None) or response.read
    chunks = []
    size = 0
    while size <= limit:
        chunk = reader(min(64 * 1024, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise AppError("下载器响应超过允许大小。")
    return b"".join(chunks)


class Downloader:
    def __init__(self, config):
        self.config = config
        self.base = config.get("url", "").rstrip("/")
        if not self.base:
            raise AppError("请先配置下载器 Web API 地址。")
        self.cookies = {}
        self._qb_authenticated = False
        session_identity = json.dumps([self.base, config.get("username", ""), config.get("password", "")], ensure_ascii=False)
        session_key = hashlib.sha256(session_identity.encode("utf-8")).hexdigest()
        self._session_file = DATA_ROOT / f"qb-session-{session_key}.json"
        self._session_lock = DATA_ROOT / f"qb-session-{session_key}.lock"
        self.progress_callback = None

    def _progress(self, message, event):
        if self.progress_callback:
            self.progress_callback(message, event)

    def _url(self, path):
        return self.base + path

    def _to_downloader_path(self, path):
        return map_path_prefix(path, self.config.get("path_mappings", []), "local", "remote")

    def _to_local_path(self, path):
        return map_path_prefix(path, self.config.get("path_mappings", []), "remote", "local")

    def _request(self, path, data=None, headers=None, method=None, content_type=None, accept_status=(), _retry_auth=True):
        performance_count("qb_requests")
        request_headers = dict(headers or {})
        if path != "/api/v2/auth/login" and self.cookies:
            request_headers.update(self._qb_headers())
        if content_type:
            request_headers["Content-Type"] = content_type
        request = urllib.request.Request(self._url(path), data=data, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403) and path != "/api/v2/auth/login" and _retry_auth:
                rejected_sid = self.cookies.get("SID", "")
                error.close()
                self.cookies.clear()
                self._qb_authenticated = False
                self._qb_login(rejected_sid=rejected_sid)
                return self._request(path, data, headers, method, content_type, accept_status, _retry_auth=False)
            if error.code in accept_status:
                return error
            if error.code in (401, 403):
                raise AppError("下载器认证失败，请检查用户名和密码。") from error
            raise AppError(f"下载器 API 请求失败 (HTTP {error.code})。") from error
        return response

    def _json(self, path, payload=None, form=False, headers=None, method=None, accept_status=()):
        if form:
            body = urllib.parse.urlencode(payload or {}).encode()
            content_type = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(payload).encode() if payload is not None else None
            content_type = "application/json" if body is not None else None
        response = self._request(path, body, headers, method or ("POST" if body is not None else "GET"), content_type, accept_status)
        raw = read_response_body(response, 8 * 1024 * 1024)
        if response.status >= 400:
            raise AppError(f"下载器 API 请求失败 (HTTP {response.status})。")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AppError("下载器返回了无效 JSON。") from error
        return value, response

    def _qb_login(self, rejected_sid=None):
        if rejected_sid is None and (self._qb_authenticated or self.cookies.get("SID")):
            return
        # Share the SID between scheduler and CGI processes; only one may log in.
        with QB_SESSION_THREAD_LOCK, file_lock(self._session_lock):
            try:
                cached = read_json(self._session_file, {})
            except AppError:
                cached = {}
            sid = cached.get("sid", "")
            if isinstance(sid, str) and sid and sid != rejected_sid and not any(ord(ch) < 32 or ord(ch) == 127 for ch in sid):
                self.cookies = {"SID": sid}
                self._qb_authenticated = True
                return
            self._session_file.unlink(missing_ok=True)
            self.cookies.clear()
            self._qb_authenticated = False
            self._progress("正在登录 qBittorrent Web API。", "downloader_auth_started")
            self._qb_login_request()
            self._qb_authenticated = True
            if self.cookies.get("SID"):
                write_json(self._session_file, {"sid": self.cookies["SID"]})
            self._progress("qBittorrent 会话已建立。", "downloader_auth_completed")

    def _qb_login_request(self):
        body = urllib.parse.urlencode({"username": self.config.get("username", ""), "password": self.config.get("password", "")}).encode()
        response = self._request("/api/v2/auth/login", body, method="POST", content_type="application/x-www-form-urlencoded")
        result = read_response_body(response, 1024).decode("utf-8", errors="replace")
        cookie_headers = response.headers.get_all("Set-Cookie", [])
        if response.status != 204 and result.strip() != "Ok.":
            content_type = response.headers.get_content_type()
            if "html" in content_type or result.lstrip().lower().startswith(("<!doctype html", "<html")):
                raise AppError("qBittorrent 地址返回了网页而不是 Web API 登录响应。请确认填写的是 Web UI 地址和端口；若经反向代理访问，请在地址中包含代理子路径。")
            if result.strip().casefold() == "fails.":
                raise AppError("qBittorrent Web API 已拒绝登录。请确认此 Web UI 的用户名和密码，并检查是否因登录失败触发 IP 封禁或访问限制。")
            raise AppError("qBittorrent Web API 登录响应无法识别。请检查地址、端口和 Web UI 配置。")
        if response.status == 204 and not cookie_headers:
            raise AppError("qBittorrent 返回空登录响应但没有会话 Cookie，登录未建立。")
        for cookie in cookie_headers:
            pair = cookie.split(";", 1)[0]
            if "=" in pair:
                key, value = pair.split("=", 1)
                self.cookies[key] = value

    def _qb_headers(self):
        return {"Cookie": "; ".join(f"{key}={value}" for key, value in self.cookies.items())}

    def test(self):
        self._qb_login()
        response = self._request("/api/v2/app/version", headers=self._qb_headers())
        if not response.read(1024).strip():
            raise AppError("qBittorrent version response was empty.")
        return "qBittorrent 连接成功。"

    def add(self, torrent_data, title, download_dir):
        info_hash = torrent_info_hash(torrent_data)
        local_download_dir = download_dir
        download_dir = self._to_downloader_path(download_dir)
        self._qb_login()
        boundary = "----AVSubscribe" + uuid.uuid4().hex
        fields = [
            ("savepath", download_dir.encode()), ("paused", b"false"), ("autoTMM", b"false"),
            ("contentLayout", b"Original"), ("rename", title.encode("utf-8")),
        ]
        body = bytearray()
        for name, value in fields:
            body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode())
            body.extend(value + b"\r\n")
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"release.torrent\"\r\nContent-Type: application/x-bittorrent\r\n\r\n".encode())
        body.extend(torrent_data + f"\r\n--{boundary}--\r\n".encode())
        response = self._request("/api/v2/torrents/add", bytes(body), headers=self._qb_headers(), method="POST", content_type=f"multipart/form-data; boundary={boundary}", accept_status=(409,))
        message = response.read(1024).decode("utf-8", errors="replace").strip()
        if response.status == 409:
            existing = self.list_tasks([info_hash]).get(info_hash)
            if existing:
                self._progress(self._duplicate_task_message(title, existing), "duplicate_task")
                return self._track_existing_qb_task(info_hash, existing, local_download_dir)
            detail = " ".join(message.split())[:300]
            suffix = f"：{detail}" if detail else "，且下载器任务列表中没有对应种子。"
            raise AppError("qBittorrent 添加任务冲突 (HTTP 409)" + suffix)
        if message and message != "Ok.":
            try:
                result = json.loads(message)
            except json.JSONDecodeError:
                result = None
            if isinstance(result, dict) and all(key in result for key in ("success_count", "failure_count")):
                try:
                    success_count = int(result["success_count"])
                    failure_count = int(result["failure_count"])
                    pending_count = int(result.get("pending_count", 0))
                except (TypeError, ValueError):
                    success_count = failure_count = pending_count = -1
                added_ids = result.get("added_torrent_ids", [])
                if success_count > 0 and failure_count == 0 and pending_count >= 0 and isinstance(added_ids, list) and info_hash in {str(item).lower() for item in added_ids}:
                    return info_hash
                if failure_count > 0 or success_count < 0 or pending_count < 0:
                    raise AppError("qBittorrent 添加任务失败：" + message)
            existing = self.list_tasks([info_hash]).get(info_hash)
            if existing:
                self._progress(self._duplicate_task_message(title, existing), "duplicate_task")
                return self._track_existing_qb_task(info_hash, existing, local_download_dir)
            raise AppError("qBittorrent 添加任务失败：" + message)
        return info_hash

    @staticmethod
    def _duplicate_task_message(title, task):
        path = task.get("content_path") or task.get("save_path") or "未知"
        state = task.get("state", "未知")
        return f"qBittorrent 已存在相同种子：{title}；状态 {state}；保存位置 {path}。按设置仅记录警告，不重复添加，也不自动硬链接。"

    def _track_existing_qb_task(self, info_hash, task, download_dir=""):
        state = str(task.get("state", ""))
        content_path = str(task.get("content_path", ""))
        missing_data = state == "missingFiles" or (bool(content_path) and not Path(content_path).exists())
        if missing_data:
            if state.startswith("paused"):
                task["recheck_skipped_paused"] = True
                self._progress(
                    f"qBittorrent 任务数据缺失，但任务处于暂停状态；为保留暂停设置，未自动校验或启动：{content_path or info_hash}。",
                    "duplicate_task_paused_missing",
                )
            else:
                location = self.recheck(info_hash, download_dir)
                task["recheck_requested"] = True
                task["recheck_location"] = location
                self._progress(
                    f"qBittorrent 任务数据缺失，已对现有任务触发强制校验并请求补下载缺失数据：{content_path or info_hash}。",
                    "duplicate_task_recheck",
                )
        return {
            "hash": info_hash, "owned": False, "existing_task": task,
            "recheck_requested": bool(task.get("recheck_requested")),
            "recheck_skipped_paused": bool(task.get("recheck_skipped_paused")),
            "recheck_location": task.get("recheck_location", ""),
        }

    def recheck(self, info_hash, download_dir=""):
        if self.config.get("type") != "qbittorrent":
            raise AppError("当前下载器不支持对已有任务执行强制校验。")
        self._qb_login()
        location = self._to_downloader_path(download_dir) if download_dir else ""
        if location:
            location_payload = urllib.parse.urlencode({"hashes": str(info_hash).lower(), "location": location}).encode()
            self._request(
                "/api/v2/torrents/setLocation", location_payload, headers=self._qb_headers(),
                method="POST", content_type="application/x-www-form-urlencoded",
            )
            self._progress(f"已将已有种子的保存目录切换为下载器路径：{location}。", "downloader_location_changed")
        payload = urllib.parse.urlencode({"hashes": str(info_hash).lower()}).encode()
        self._request(
            "/api/v2/torrents/recheck", payload, headers=self._qb_headers(),
            method="POST", content_type="application/x-www-form-urlencoded",
        )
        return location

    def list_tasks(self, hashes=None):
        self._qb_login()
        path = "/api/v2/torrents/info"
        hashes = sorted({str(value).strip().lower() for value in (hashes or ()) if str(value).strip()})
        if hashes:
            path += "?" + urllib.parse.urlencode({"hashes": "|".join(hashes)})
        self._progress(f"正在请求 qBittorrent 任务接口（{len(hashes)} 个指定哈希）。", "downloader_query_started")
        data, _ = self._json(path, headers=self._qb_headers())
        self._progress(f"qBittorrent 返回 {len(data)} 个任务。", "downloader_query_completed")
        return {str(item.get("hash", "")).lower(): {
            "name": item.get("name", ""), "progress": float(item.get("progress", 0)),
            "complete": float(item.get("progress", 0)) >= 0.999999 or item.get("state") in ("uploading", "stalledUP", "queuedUP", "pausedUP"),
            "state": item.get("state", "unknown"), "save_path": self._to_local_path(item.get("save_path", "")),
            "content_path": self._to_local_path(item.get("content_path", "")),
            "failed": item.get("state") in ("error", "missingFiles"),
            "error": "下载器报告任务错误。" if item.get("state") in ("error", "missingFiles") else "",
        } for item in data if item.get("hash")}

    def list_task_files(self, info_hash):
        if self.config.get("type") != "qbittorrent":
            raise AppError("补全 M-Team ID 目前需要使用 qBittorrent。")
        if not self.cookies:
            self._qb_login()
        data, _ = self._json(
            "/api/v2/torrents/files?" + urllib.parse.urlencode({"hash": str(info_hash).lower()}),
            headers=self._qb_headers(),
        )
        if not isinstance(data, list):
            raise AppError("qBittorrent 返回的种子文件列表格式无效。")
        return data

    def task_mteam_metadata(self, info_hash):
        if self.config.get("type") != "qbittorrent":
            raise AppError("补全 M-Team ID 目前需要使用 qBittorrent。")
        if not self.cookies:
            self._qb_login()
        query = urllib.parse.urlencode({"hash": str(info_hash).lower()})
        properties, _ = self._json("/api/v2/torrents/properties?" + query, headers=self._qb_headers())
        trackers, _ = self._json("/api/v2/torrents/trackers?" + query, headers=self._qb_headers())
        return properties, trackers

    def remove(self, info_hash, delete_data=True):
        self._qb_login()
        self._request("/api/v2/torrents/delete", urllib.parse.urlencode({"hashes": info_hash, "deleteFiles": str(bool(delete_data)).lower()}).encode(), headers=self._qb_headers(), method="POST", content_type="application/x-www-form-urlencoded")


def validate_subscription(payload, current=None):
    performer = str(payload.get("performer", current.get("performer", "") if current else "")).strip()
    keywords = str(payload.get("keywords", current.get("keywords", "") if current else "")).strip()
    download_dir = str(payload.get("download_dir", current.get("download_dir", "") if current else "")).strip()
    raw_blacklist = payload.get("blacklist", current.get("blacklist", []) if current else [])
    if not performer or len(performer) > 120:
        raise AppError("请填写女优名字（最多 120 个字符）。")
    if len(keywords) > 200:
        raise AppError("附加关键字不能超过 200 个字符。")
    if not isinstance(raw_blacklist, list) or len(raw_blacklist) > 200:
        raise AppError("番号黑名单最多可添加 200 个番号。")
    blacklist = []
    for value in raw_blacklist:
        code = normalize_code(value)
        if not code:
            raise AppError(f"黑名单番号格式无效：{value}")
        if code not in blacklist:
            blacklist.append(code)
    if not download_dir.startswith("/"):
        raise AppError("下载目录必须填写飞牛上的绝对路径。")
    download_dir = posixpath.normpath(download_dir)
    if download_dir == "/":
        raise AppError("下载目录不能是文件系统根目录。")
    return {"performer": performer, "keywords": keywords, "blacklist": blacklist, "download_dir": download_dir, "wash_enabled": bool(payload.get("wash_enabled", current.get("wash_enabled", True) if current else True))}


def list_subscriptions():
    return read_json(SUBSCRIPTIONS_FILE, [])


def subscriptions_with_video_counts():
    connection = connect_video_database()
    try:
        counts = dict(connection.execute(
            "SELECT subscription_id, COUNT(*) FROM local_video_files GROUP BY subscription_id"
        ).fetchall())
    finally:
        connection.close()
    return [{**item, "video_count": counts.get(item["id"], 0)} for item in list_subscriptions()]


def save_subscriptions(subscriptions):
    write_json(SUBSCRIPTIONS_FILE, subscriptions)


def create_subscription(payload):
    item = validate_subscription(payload)
    item.update({"id": uuid.uuid4().hex, "created_at": now_iso()})
    with file_lock():
        subscriptions = list_subscriptions()
        subscriptions.append(item)
        save_subscriptions(subscriptions)
    return item


def update_subscription(subscription_id, payload):
    with file_lock():
        subscriptions = list_subscriptions()
        for index, item in enumerate(subscriptions):
            if item["id"] == subscription_id:
                subscriptions[index] = {**item, **validate_subscription(payload, item)}
                save_subscriptions(subscriptions)
                return subscriptions[index]
    raise AppError("订阅不存在。")


def delete_subscription(subscription_id):
    with file_lock():
        subscriptions = list_subscriptions()
        retained = [item for item in subscriptions if item.get("id") != subscription_id]
        if len(retained) == len(subscriptions):
            raise AppError("订阅不存在。")
        save_subscriptions(retained)
        records = read_json(RECORDS_FILE, {})
        records.pop(subscription_id, None)
        write_json(RECORDS_FILE, records)
        delete_video_database_subscription(subscription_id)
    return {"ok": True}


def queue_scan(trigger="manual", subscription_id="", refresh_existing_info=False):
    subscriptions = list_subscriptions()
    subscription = next((item for item in subscriptions if item.get("id") == subscription_id), None) if subscription_id else None
    if subscription_id and subscription is None:
        raise AppError("订阅不存在。")
    with file_lock():
        request = {"trigger": trigger, "subscription_id": subscription_id, "refresh_existing_info": bool(refresh_existing_info), "created_at": now_iso()}
        requests = read_scan_requests()
        if len(requests) >= 100:
            raise AppError("检查队列已满，请稍后重试。")
        requests.append(request)
        write_json(REQUEST_FILE, requests)
        status = read_json(STATUS_FILE, {})
        if status.get("state") not in ("running", "queued"):
            status = {"state": "queued", "message": "等待后台检查。", "started_at": "", "completed_at": "", "results": []}
            write_json(STATUS_FILE, status)
        log_event("检查任务已加入队列。", event="queued", subscription=subscription.get("performer", "") if subscription else "")
    return {"state": "queued", "message": "检查任务已加入队列。"}


def queue_mteam_id_backfill(subscription_id):
    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    if subscription is None:
        raise AppError("订阅不存在。")
    with file_lock():
        with file_lock(BACKFILL_STATUS_LOCK_FILE):
            statuses = read_mteam_id_backfill_statuses()
        current = statuses.get(subscription_id, {})
        if current.get("subscription_id") == subscription_id and current.get("state") in ("queued", "running"):
            return {"state": current["state"], "job_id": current.get("job_id", ""), "message": current.get("message", "M-Team ID 补全任务正在执行。")}
        requests = read_scan_requests()
        existing = next((item for item in requests if item.get("action") == "mteam_id_backfill" and item.get("subscription_id") == subscription_id), None)
        if existing:
            return {"state": "queued", "job_id": existing.get("job_id", ""), "message": "M-Team ID 补全任务已在队列中。"}
        if len(requests) >= 100:
            raise AppError("后台任务队列已满，请稍后重试。")
        request = {"action": "mteam_id_backfill", "job_id": uuid.uuid4().hex, "subscription_id": subscription_id, "created_at": now_iso()}
        requests.append(request)
        write_json(REQUEST_FILE, requests)
        with file_lock(BACKFILL_STATUS_LOCK_FILE):
            statuses = read_mteam_id_backfill_statuses()
            statuses[subscription_id] = {
                "job_id": request["job_id"], "subscription_id": subscription_id, "performer": subscription["performer"],
                "state": "queued", "phase": "queued", "completed": 0, "total": 0,
                "message": "补全任务已排队，等待后台处理。", "queued_at": request["created_at"],
            }
            write_json(BACKFILL_STATUS_FILE, statuses)
        log_event("M-Team ID 补全已加入后台队列。", event="queued", subscription=subscription["performer"])
    return {"state": "queued", "job_id": request["job_id"], "message": "M-Team ID 补全已加入后台队列。"}


def get_mteam_id_backfill_status(subscription_id):
    current = read_mteam_id_backfill_statuses().get(subscription_id)
    if current:
        cooldown = read_json(MTEAM_LIMIT_FILE, {})
        if current.get("state") == "running" and cooldown.get("limited_performer") == current.get("performer") and float(cooldown.get("retry_at", 0) or 0) > time.time():
            return {**current, "phase": "waiting_retry", "retry_at": cooldown["retry_at"], "message": "M-Team 限流冷却中，恢复后会继续当前补全任务。"}
        return current
    queued = next((item for item in read_scan_requests() if item.get("action") == "mteam_id_backfill" and item.get("subscription_id") == subscription_id), None)
    if queued:
        subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), {})
        return {"job_id": queued.get("job_id", ""), "subscription_id": subscription_id, "performer": subscription.get("performer", ""), "state": "queued", "phase": "queued", "completed": 0, "total": 0, "message": "补全任务已排队，等待后台处理。"}
    return {"state": "idle", "subscription_id": subscription_id}


def read_mteam_id_backfill_statuses():
    value = read_json(BACKFILL_STATUS_FILE, {})
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("jobs"), dict):
        return value["jobs"]
    if value.get("subscription_id"):
        return {value["subscription_id"]: value}
    return value


def write_mteam_id_backfill_status(status):
    with file_lock(BACKFILL_STATUS_LOCK_FILE):
        statuses = read_mteam_id_backfill_statuses()
        statuses[status["subscription_id"]] = status
        write_json(BACKFILL_STATUS_FILE, statuses)


def read_scan_requests():
    value = read_json(REQUEST_FILE, [], allow_types=(dict,))
    if isinstance(value, dict):
        return [value]
    if not all(isinstance(item, dict) for item in value):
        backup = backup_invalid_state_file(REQUEST_FILE)
        raise AppError(f"检查队列格式不正确，原文件已保留{f'，备份为 {backup.name}' if backup else ''}。")
    return value


def has_queued_scans():
    return any(item.get("action") != "mteam_id_backfill" for item in read_scan_requests())


def pop_scan_request():
    with file_lock():
        requests = read_scan_requests()
        request = requests.pop(0) if requests else {}
        if requests:
            write_json(REQUEST_FILE, requests)
        else:
            REQUEST_FILE.unlink(missing_ok=True)
        return request


def get_status():
    status = read_json(STATUS_FILE, {"state": "idle", "message": "尚未检查。", "results": [], "last_scan_at": ""})
    if status.get("state") == "waiting_retry" and status.get("retry_at"):
        status["retry_in"] = max(0, int(float(status["retry_at"]) - time.time() + 0.999))
    try:
        settings = get_settings()
        status["auto_scan"] = bool(settings.get("auto_scan", True))
        status["poll_interval_minutes"] = int(settings.get("poll_interval_minutes", 60))
        if status["auto_scan"]:
            last_run = scheduler_last_run()
            if last_run:
                status["next_scan_at"] = last_run + status["poll_interval_minutes"] * 60
            else:
                status["next_scan_at"] = 0
        else:
            status["next_scan_at"] = 0
    except (AppError, TypeError, ValueError):
        status["auto_scan"] = True
        status["next_scan_at"] = 0
    return status


def downloader_state_label(state):
    return {
        "error": "错误",
        "missingFiles": "文件缺失",
        "uploading": "做种中",
        "pausedUP": "做种已暂停",
        "queuedUP": "等待做种",
        "stalledUP": "等待上传",
        "checkingUP": "正在校验上传数据",
        "forcedUP": "强制做种中",
        "allocating": "正在分配磁盘空间",
        "downloading": "下载中",
        "metaDL": "正在获取种子信息",
        "pausedDL": "下载已暂停",
        "queuedDL": "等待下载",
        "stalledDL": "等待连接或做种",
        "checkingDL": "正在校验下载数据",
        "forcedDL": "强制下载中",
        "checkingResumeData": "正在校验恢复数据",
        "moving": "正在移动文件",
        "unknown": "状态未知",
    }.get(str(state), "其他状态")


def get_activity():
    config = get_settings()["downloader"]
    if not config.get("url"):
        return []
    try:
        tasks = Downloader(config).list_tasks()
    except AppError as error:
        return [{"subscription": "下载器", "code": "", "title": "", "state": "failed", "progress": 0, "message": f"无法读取下载器任务：{error}", "updated_at": ""}]

    subscriptions = {item["id"]: item for item in list_subscriptions()}
    records = read_json(RECORDS_FILE, {})
    associations = {}
    code_associations = {}
    for subscription_id, entries in records.items():
        subscription = subscriptions.get(subscription_id, {})
        for code, item in entries.items():
            normalized_code = normalize_code(code)
            if normalized_code and subscription:
                metadata = item.get("mteam_metadata") or {}
                code_associations.setdefault(normalized_code, {
                    "subscription_id": subscription_id,
                    "subscription": subscription.get("performer", "其他"),
                    "code": normalized_code,
                    "rank": (item.get("mteam_metadata") or {}).get("rank", [0, 0, 0]),
                    "mteam_id": str(metadata.get("id", "")),
                })
            for key in ("current", "pending"):
                release = item.get(key)
                info_hash = str(release.get("hash", "")).lower() if release else ""
                if info_hash:
                    associations.setdefault(info_hash, {
                        "subscription_id": subscription_id,
                        "subscription": subscription.get("performer", "其他"),
                        "code": code,
                        "pending": key == "pending",
                        "rank": release.get("rank", [0, 0, 0]),
                        "mteam_id": str(release.get("id") or (item.get("mteam_metadata") or {}).get("id", "")),
                    })

    output = []
    for info_hash, task in tasks.items():
        complete = bool(task.get("complete"))
        failed = bool(task.get("failed"))
        if complete and not failed:
            continue
        associated = associations.get(info_hash)
        if associated is None:
            associated = code_associations.get(normalize_code(task.get("name", "")), {})
        associated = associated or {}
        code = associated.get("code") or normalize_code(task.get("name", ""))
        if not code:
            continue
        state = "failed" if failed else "upgrading" if associated.get("pending") else "downloading"
        task_state = str(task.get("state", "unknown"))
        message = task.get("error", "") if failed else f"qBittorrent：{downloader_state_label(task_state)}"
        output.append({
            "subscription_id": associated.get("subscription_id", ""),
            "subscription": associated.get("subscription", "qBittorrent"),
            "code": code,
            "mteam_id": associated.get("mteam_id", ""),
            "title": task.get("name", ""),
            "state": state,
            "progress": round(float(task.get("progress", 0)) * 100, 1),
            "message": message,
            "rank": associated.get("rank", [0, 0, 0]),
            "updated_at": "",
            "pending": associated.get("pending", False),
            "hash": info_hash,
        })
    return output[:200]


@measure_stage("index")
def index_subscription_videos(subscription_id, videos, records):
    rows = []
    indexed_subscription = {}
    directory_entries = {}
    for code, items in sorted(videos.items()):
        record = records.get(code, {})
        mteam = record.get("mteam_metadata") or {}
        if not mteam:
            mteam = next((release for release in (record.get("pending"), record.get("current")) if release and release.get("rank") and not release.get("local_existing")), {})
        local_mteam = record.get("local_files") or {}
        file_rows = []
        for item in items:
            path = Path(item["path"])
            per_file = local_mteam.get(str(path)) or {}
            if not per_file and len(items) == 1:
                per_file = mteam
            file_rank = list(per_file.get("rank") or item["rank"])
            rank_source = "M-Team" if per_file.get("id") else ("NFO" if item.get("metadata") else "文件名")
            rank_title = (per_file.get("title") or item["title"]) if per_file.get("id") else item["title"]
            has_local_subtitle = (item.get("metadata") or {}).get("subtitle_available")
            if per_file.get("id") and has_local_subtitle and len(file_rank) > 1 and file_rank[1] == 0:
                file_rank[1] = 1
            subtitle_value = per_file.get("subtitle") or per_file.get("smallDescr") or per_file.get("small_descr") or ""
            subtitle = subtitle_display_text(subtitle_value, rank_title) if per_file.get("id") else ""
            try:
                stat = path.stat()
                if path.parent not in directory_entries:
                    try:
                        directory_entries[path.parent] = [
                            item for item in path.parent.iterdir()
                            if not item.is_symlink() and item.is_file() and (item.suffix.casefold() == ".nfo" or item.suffix.casefold() in SUBTITLE_EXTENSIONS)
                        ]
                    except OSError:
                        directory_entries[path.parent] = []
                file_row = {
                    "path": str(path), "name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "mteam_id": str(per_file.get("id", "")), "rank": file_rank,
                    "rank_source": rank_source, "rank_title": rank_title, "subtitle": subtitle,
                    "mediainfo": localize_mediainfo(per_file.get("mediainfo", "") or ""),
                    "mediainfo_checked": bool(per_file.get("mediainfo_checked", False)),
                    "subtitle_paths": [str(sidecar) for sidecar in matching_video_sidecars(path, directory_entries[path.parent]) if sidecar.suffix.casefold() in SUBTITLE_EXTENSIONS],
                }
                file_rows.append(file_row)
            except OSError:
                continue
        if not file_rows:
            continue
        local_best = max(file_rows, key=lambda item: tuple(item["rank"]))
        index_row = {
            "code": code, "count": len(file_rows), "files": file_rows,
            "rank": list(local_best["rank"]), "rank_source": local_best["rank_source"], "rank_title": local_best["rank_title"],
            "updated_at": now_iso(),
        }
        indexed_subscription[code] = index_row
        rows.append(index_row)
    known_subscriptions = {item.get("id") for item in list_subscriptions()}
    sync_video_database(subscription_id, list(indexed_subscription.values()), known_subscriptions)
    return rows


def subtitle_display_text(subtitle, title):
    subtitle = str(subtitle or "").strip()
    title = str(title or "").strip()
    return subtitle if len(subtitle) >= 6 else (title or subtitle)


def get_subscription_videos(subscription_id):
    started_at = time.perf_counter()
    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    if subscription is None:
        raise AppError("订阅不存在。")
    directory = subscription["download_dir"]
    videos = scan_local_releases(directory)
    video_count = sum(len(items) for items in videos.values())
    records = read_json(RECORDS_FILE, {}).get(subscription_id, {})
    rows = index_subscription_videos(subscription_id, videos, records)
    settings = get_settings()
    if settings.get("debug_mode"):
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        log_event(f"本地影片扫描与索引耗时 {elapsed_ms:.1f} ms，共识别 {video_count} 个视频。", level="debug", event="debug_decision", subscription=subscription.get("performer", ""))
    return {"directory": directory, "video_count": video_count, "videos": rows}


def mteam_id_from_torrent_comment(comment):
    text = str(comment or "").strip()
    if re.fullmatch(r"\d{5,}", text):
        return text
    match = re.search(r"(?:https?://[^\s<>\"']+)?/detail/(\d{5,})(?:\b|/|$)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\b(?:m[- ]?team\s*(?:id)?\s*[:=#]?\s*)(\d{5,})\b", text, re.IGNORECASE)
    return match.group(1) if match else ""


def has_mteam_tracker(trackers):
    if not isinstance(trackers, list):
        return False
    for tracker in trackers:
        value = tracker.get("url", "") if isinstance(tracker, dict) else tracker
        value = str(value or "").casefold()
        if "m-team" in value or "manfuz" in value:
            return True
    return False


@measure_stage("match")
def match_qbittorrent_file_ids(targets, downloader, task_cache=None, progress_callback=None, settings=None, performer=""):
    """Use torrent file paths as the binding key; a number or title is only a prefilter."""
    cache = task_cache if task_cache is not None else {}
    tasks = check_task_snapshot(downloader, cache)
    file_cache = cache.setdefault("id_file_lists", {})
    metadata_cache = cache.setdefault("id_torrent_metadata", {})
    wanted_codes = {target["code"] for target in targets.values()}
    # Ancestors of all target files turn task/path overlap into one set lookup.
    target_ancestors = set(targets)
    for path in targets:
        target_ancestors.update(str(parent) for parent in Path(path).parents)
    matches, matched_hashes, ambiguous = {}, {}, set()
    checked = errors = 0
    if progress_callback:
        progress_callback("matching", 0, len(tasks), f"正在核对 {len(tasks)} 个下载任务。")
    for index, (info_hash, task) in enumerate(tasks.items(), 1):
        if progress_callback and (index % 25 == 0 or index == len(tasks)):
            progress_callback("matching", index, len(tasks), f"已核对 {index}/{len(tasks)} 个下载任务。")
        possible_codes = {normalize_code(task.get("name", "")), normalize_code(task.get("content_path", ""))} & wanted_codes
        content = Path(task["content_path"]).resolve() if task.get("content_path") else None
        performance_count("path_checks")
        path_overlap = content is not None and str(content) in target_ancestors
        if not possible_codes and not path_overlap:
            continue
        try:
            save_value = task.get("save_path", "")
            if not save_value or not Path(save_value).is_absolute():
                continue
            save_path = Path(save_value).resolve()
            if info_hash not in file_cache:
                file_cache[info_hash] = downloader.list_task_files(info_hash)
            else:
                performance_count("file_hits")
            task_video_paths = set()
            for file_info in file_cache[info_hash]:
                relative = str(file_info.get("name", "")).replace("\\", "/")
                if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts or Path(relative).suffix.casefold() not in VIDEO_EXTENSIONS:
                    continue
                task_video_paths.add(str((save_path / Path(relative)).resolve()))
            matched_targets = task_video_paths.intersection(targets)
            if not matched_targets:
                continue
            if info_hash not in metadata_cache:
                metadata_cache[info_hash] = downloader.task_mteam_metadata(info_hash)
            else:
                performance_count("metadata_hits")
            properties, trackers = metadata_cache[info_hash]
            checked += 1
            if not has_mteam_tracker(trackers):
                continue
            identifier = mteam_id_from_torrent_comment(properties.get("comment", "") if isinstance(properties, dict) else "")
            if not identifier:
                continue
            for path in matched_targets:
                if path in matches and matches[path] != identifier:
                    ambiguous.add(path)
                else:
                    matches[path] = identifier
                    matched_hashes[path] = info_hash
        except (AppError, OSError, TypeError, ValueError) as error:
            errors += 1
            log_debug(settings or {}, f"补全 M-Team ID 时跳过种子 {info_hash}：{error}", performer)
    return matches, matched_hashes, ambiguous, checked, errors


def bind_missing_local_mteam_ids(subscription, videos, records, downloader, task_cache=None, settings=None):
    targets = {}
    for code, items in videos.items():
        local_files = records.get(code, {}).get("local_files") or {}
        existing_paths = {str(Path(path).resolve()) for path, value in local_files.items() if value.get("id")}
        for item in items:
            path = Path(item["path"])
            path_key = str(path.resolve())
            item.pop("_id_binding_error", None)
            if path_key not in existing_paths:
                targets[path_key] = {"path": path, "code": code, "item": item}
    if not targets:
        return
    # Lightweight downloader implementations may not expose the file/metadata API.
    file_api = downloader.downloader if isinstance(downloader, CheckDownloader) else downloader
    if not callable(getattr(file_api, "list_task_files", None)) or not callable(getattr(file_api, "task_mteam_metadata", None)):
        return
    performer = subscription["performer"]
    log_event(f"发现 {len(targets)} 个本地视频未绑定 M-Team ID，正在按文件路径读取 qBittorrent 注释。", event="mteam_id_backfill_started", subscription=performer)
    try:
        matches, hashes, ambiguous, checked, errors = match_qbittorrent_file_ids(
            targets, downloader, task_cache, settings=settings, performer=performer,
        )
    except AppError as error:
        matches, hashes, ambiguous, checked, errors = {}, {}, set(), 0, 1
        log_event(f"自动补全 M-Team ID 失败：{error}；本轮保留未能核对的重复文件。", level="warning", event="mteam_id_backfill_failed", subscription=performer)
    filled = 0
    for path_key, target in targets.items():
        identifier = matches.get(path_key)
        if not identifier or path_key in ambiguous:
            target["item"]["_id_binding_error"] = "qBittorrent 文件匹配存在 ID 冲突" if path_key in ambiguous else "未从 qBittorrent 找到可绑定的 M-Team 种子注释"
            continue
        record = records.setdefault(target["code"], {"code": target["code"]})
        local_files = record.setdefault("local_files", {})
        path_string = str(target["path"])
        # Discard grades that were not tied to this torrent ID.
        local_files[path_string] = {"id": identifier, "torrent_hash": hashes[path_key], "id_source": "qbittorrent_file"}
        if path_key != path_string:
            local_files.pop(path_key, None)
        filled += 1
    log_event(
        f"自动补全 M-Team ID：新增 {filled} 个，未匹配 {len(targets) - filled - len(ambiguous)} 个，匹配冲突 {len(ambiguous)} 个，读取失败 {errors} 个。",
        level="warning" if errors or ambiguous else "info", event="mteam_id_backfill", subscription=performer,
    )
    token = (settings or {}).get("mteam_token", "")
    if not token:
        return
    for path_key, target in targets.items():
        if path_key in ambiguous or path_key not in matches:
            continue
        path = target["path"]
        metadata = records[target["code"]]["local_files"][str(path)]
        try:
            detail = mteam_detail(metadata["id"], token)
            if not detail_contains_performer(detail, performer):
                raise AppError("详情中没有对应女优关键字")
            enriched = enrich_candidate_from_detail({"id": metadata["id"], "code": target["code"], "title": target["item"].get("title") or path.name, "rank": [0, 0, 0]}, token, detail)
            if not metadata_is_complete(candidate_metadata(enriched)):
                raise AppError("M-Team 详情分级信息不完整")
            stat = path.stat()
            metadata.update({**candidate_metadata(enriched), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "rank_version": LOCAL_MTEAM_RANK_VERSION})
        except MTeamRateLimitError:
            raise
        except (AppError, OSError) as error:
            target["item"]["_id_binding_error"] = f"ID 已绑定，但分级核对失败：{error}"
            log_event(f"{target['code']}：已绑定 {path.name} 的 M-Team ID {metadata['id']}，分级核对失败：{error}", level="warning", event="mteam_id_backfill_failed", subscription=performer)


def backfill_subscription_mteam_ids(subscription_id, downloader=None, progress_callback=None):
    def progress(phase, completed, total, message):
        if progress_callback:
            progress_callback({"phase": phase, "completed": completed, "total": total, "message": message})

    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    if subscription is None:
        raise AppError("订阅不存在。")
    downloader = downloader or Downloader(get_settings()["downloader"])
    if downloader.config.get("type") != "qbittorrent":
        raise AppError("补全 M-Team ID 目前需要使用 qBittorrent，因为 ID 保存在种子注释中。")

    directory = subscription["download_dir"]
    progress("scan", 0, 0, "正在扫描本地视频。")
    videos = scan_local_releases(directory)
    records_by_subscription = read_json(RECORDS_FILE, {})
    records = records_by_subscription.setdefault(subscription_id, {})
    database = connect_video_database()
    try:
        saved_ids = {
            str(path): str(identifier or "")
            for path, identifier in database.execute(
                "SELECT path, mteam_id FROM local_video_files WHERE subscription_id = ?", (subscription_id,)
            ).fetchall()
        }
    finally:
        database.close()

    targets = {}
    missing_count = 0
    for code, items in videos.items():
        record = records.get(code, {})
        local_files = record.get("local_files") or {}
        for item in items:
            path = Path(item["path"])
            path_key = str(path.resolve())
            per_file = local_files.get(str(path)) or local_files.get(path_key) or {}
            existing_id = per_file.get("id", "")
            if not existing_id and len(items) == 1:
                existing_id = (record.get("mteam_metadata") or {}).get("id", "")
            if not existing_id:
                existing_id = saved_ids.get(str(path), saved_ids.get(path_key, ""))
                if existing_id:
                    records.setdefault(code, record)
                    record.setdefault("local_files", {}).setdefault(str(path), {}).setdefault("id", existing_id)
            if not existing_id:
                missing_count += 1
            # Revalidate duplicate-number files even when an older release filled
            # the same ID into them. An exact torrent/file binding is authoritative.
            if not existing_id or len(items) > 1:
                targets[path_key] = {"path": path, "code": code, "existing_id": str(existing_id or "")}
    if not targets:
        write_json(RECORDS_FILE, records_by_subscription)
        index_subscription_videos(subscription_id, videos, records)
        summary = {"filled": 0, "already_present": sum(len(items) for items in videos.values()), "ambiguous": 0, "checked": 0, "errors": 0}
        progress("completed", 1, 1, "本地视频均已有 M-Team ID。")
        log_event("本地视频均已有 M-Team ID，无需补全。", event="mteam_id_backfill", subscription=subscription["performer"])
        return summary

    progress("downloader", 0, 0, "正在读取 qBittorrent 任务列表。")
    matches, matched_hashes, ambiguous, checked, errors = match_qbittorrent_file_ids(
        targets, downloader, progress_callback=progress, settings=get_settings(), performer=subscription["performer"],
    )

    filled = 0
    corrected = 0
    verified = 0
    token = get_settings().get("mteam_token", "")
    total_matches = len(matches)
    for index, (path_key, identifier) in enumerate(matches.items(), 1):
        if path_key in ambiguous:
            continue
        target = targets[path_key]
        record = records.setdefault(target["code"], {})
        local_files = record.setdefault("local_files", {})
        path_string = str(target["path"])
        previous = local_files.get(path_string) or local_files.get(path_key) or {}
        old_identifier = target["existing_id"]
        if old_identifier and old_identifier != identifier:
            corrected += 1
            # A changed ID invalidates all metadata and subtitle checks for the old torrent.
            per_file = {}
            log_event(f"{target['code']}：已修正 {target['path'].name} 的 M-Team ID（{old_identifier} → {identifier}）。", event="mteam_id_backfill_corrected", subscription=subscription["performer"])
        else:
            per_file = dict(previous)
        per_file.update({"id": identifier, "torrent_hash": matched_hashes[path_key], "id_source": "qbittorrent_file"})
        local_files[path_string] = per_file
        if path_key != path_string:
            local_files.pop(path_key, None)
        write_json(RECORDS_FILE, records_by_subscription)
        if token and (old_identifier != identifier or not per_file.get("metadata_complete")):
            try:
                detail = mteam_detail(identifier, token)
                enriched = enrich_candidate_from_detail(
                    {"id": identifier, "code": target["code"], "title": target["path"].name, "rank": [0, 0, 0]},
                    token, detail,
                )
                per_file.update(candidate_metadata(enriched))
            except MTeamRateLimitError:
                raise
            except AppError as error:
                log_debug(get_settings(), f"已补全 {target['code']} 的 M-Team ID {identifier}，但详情信息读取失败：{error}", subscription["performer"])
        if not old_identifier:
            filled += 1
        verified += 1
        progress("details", index, total_matches, f"已核对 {verified} 个文件：新增 {filled} 个 ID，修正 {corrected} 个 ID。")
    for index, (code, items) in enumerate(videos.items(), 1):
        record = records.get(code)
        if record and token:
            progress("subtitles", index, len(videos), f"正在检查字幕信息（{index}/{len(videos)}）：{code}。")
            _download_existing_local_subtitles(code, items, record, token, subscription["performer"])
    write_json(RECORDS_FILE, records_by_subscription)
    index_subscription_videos(subscription_id, videos, records)
    summary = {"filled": filled, "corrected": corrected, "verified": verified, "already_present": sum(len(items) for items in videos.values()) - missing_count, "ambiguous": len(ambiguous), "checked": checked, "errors": errors}
    log_event(
        f"M-Team ID 补全完成：新增 {filled} 个，修正 {corrected} 个，已有 ID {summary['already_present']} 个，候选冲突 {summary['ambiguous']} 个，已验证 {checked} 个种子，读取失败 {errors} 个。",
        event="mteam_id_backfill", subscription=subscription["performer"],
    )
    progress("completed", 1, 1, "M-Team ID 补全完成。")
    return summary


def get_subscription_video_subtitle(subscription_id, code):
    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    if subscription is None:
        raise AppError("订阅不存在。")
    code = normalize_code(code)
    if not code:
        raise AppError("番号格式无效。")
    all_records = read_json(RECORDS_FILE, {})
    records = all_records.get(subscription_id, {})
    record = records.get(code)
    if not record:
        raise AppError("该订阅中没有此番号的记录。")
    metadata = record.get("mteam_metadata") or {}
    cached = str(metadata.get("subtitle") or metadata.get("smallDescr") or metadata.get("small_descr") or "")
    release = record.get("pending") or record.get("current") or {}
    title = str(metadata.get("title") or release.get("title") or "")
    if cached or metadata.get("subtitle_checked"):
        return {"subtitle": subtitle_display_text(cached, title)}
    identifier = metadata.get("id") or release.get("id")
    if not identifier or not str(identifier).isdigit():
        return {"subtitle": subtitle_display_text(cached, title)}
    detail = mteam_detail(identifier, get_settings().get("mteam_token", ""))
    subtitle = str(next((detail.get(key) for key in ("smallDescr", "small_descr", "subtitle", "subTitle") if detail.get(key)), "") or "")
    title = title or str(next((detail.get(key) for key in ("name", "title") if detail.get(key)), "") or "")
    record["mteam_metadata"] = {**metadata, "id": str(identifier), "title": title, "subtitle": subtitle, "subtitle_checked": True}
    write_json(RECORDS_FILE, all_records)
    return {"subtitle": subtitle_display_text(subtitle, title)}


def get_subscription_video_file_info(subscription_id, code, file_path):
    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    if subscription is None:
        raise AppError("订阅不存在。")
    code = normalize_code(code)
    if not code:
        raise AppError("番号格式无效。")
    root = Path(subscription["download_dir"]).resolve()
    target = Path(str(file_path or "")).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise AppError("视频路径不在当前订阅目录内。") from error
    videos = scan_local_releases(subscription["download_dir"])
    items = videos.get(code, [])
    item = next((value for value in items if Path(value.get("path", "")).resolve() == target), None)
    if item is None:
        raise AppError("当前订阅中没有找到该视频文件。")
    all_records = read_json(RECORDS_FILE, {})
    records = all_records.get(subscription_id, {})
    record = records.get(code, {})
    local_files = record.get("local_files") or {}
    path_key = str(target)
    metadata = local_files.get(path_key) or local_files.get(str(Path(item["path"]))) or {}
    mediainfo = str(metadata.get("mediainfo", "") or "")
    checked = bool(metadata.get("mediainfo_checked", False))
    if not checked:
        identifier = str(metadata.get("id", ""))
        if not identifier and len(items) == 1:
            identifier = str((record.get("mteam_metadata") or {}).get("id", ""))
        if not identifier.isdigit():
            raise AppError("该视频没有 M-Team ID，无法读取详情 MediaInfo。")
        token = get_settings().get("mteam_token", "")
        if not token:
            raise AppError("请先配置 M-Team 存取令牌。")
        detail = mteam_detail(identifier, token)
        if not detail_contains_performer(detail, subscription["performer"]):
            raise AppError(f"M-Team 详情中未找到女优关键字“{subscription['performer']}”，已拒绝读取详情信息。")
        mediainfo = detail_mediainfo(detail)
        record = records.setdefault(code, {"code": code})
        local_files = record.setdefault("local_files", {})
        metadata = local_files.setdefault(path_key, {"id": identifier})
        metadata["mediainfo"] = mediainfo
        metadata["mediainfo_checked"] = True
        write_json(RECORDS_FILE, all_records)
    return {"path": str(target), "mediainfo": localize_mediainfo(mediainfo), "mediainfo_checked": True}


def localize_mediainfo(value):
    sections = {
        "general": "常规", "video": "视频", "audio": "音频", "text": "字幕",
        "menu": "菜单", "image": "图像", "other": "其他",
    }
    fields = {
        "complete name": "完整名称", "folder name": "文件夹名称", "file name": "文件名",
        "file extension": "文件扩展名", "format": "格式", "format profile": "格式配置",
        "codec id": "编码标识", "codec id/info": "编码标识/说明", "file size": "文件大小",
        "duration": "时长", "overall bit rate mode": "总体码率模式", "overall bit rate": "总体码率",
        "frame rate": "帧率", "frame rate mode": "帧率模式", "encoded date": "编码日期",
        "tagged date": "标记日期", "writing application": "封装程序", "writing library": "编码库",
        "unique id": "唯一标识", "bit rate": "码率", "maximum bit rate": "最大码率",
        "width": "宽度", "height": "高度", "display aspect ratio": "显示宽高比",
        "aspect ratio": "宽高比", "bit depth": "位深", "bits/(pixel*frame)": "每像素每帧位数",
        "stream size": "流大小", "source stream size": "源流大小", "language": "语言",
        "default": "默认轨道", "forced": "强制轨道", "channel(s)": "声道数",
        "channel layout": "声道布局", "sampling rate": "采样率", "compression mode": "压缩模式",
        "delay relative to video": "相对视频延迟", "count of elements": "元素数量",
        "count of events": "事件数量", "muxing mode": "封装模式", "writing application": "封装程序",
        "writing library": "封装库", "format/info": "格式/说明", "format settings": "格式设置",
        "format settings, cabac": "格式设置，CABAC", "format settings, reference frames": "格式设置，参考帧",
        "color space": "色彩空间", "chroma subsampling": "色度抽样", "color range": "色彩范围",
        "color primaries": "色彩基色", "transfer characteristics": "传递特性",
        "matrix coefficients": "矩阵系数", "scan type": "扫描方式", "scan order": "扫描顺序",
        "title": "标题", "id": "轨道编号", "type": "类型", "codec": "编码器",
        "codec/info": "编码器/说明", "service kind": "服务类型", "channel positions": "声道位置",
        "channel positions": "声道位置", "original source medium": "原始来源介质",
        "complete name": "完整名称", "other": "其他信息",
    }
    language_values = {
        "japanese": "日语", "chinese": "中文", "english": "英语", "korean": "韩语",
        "french": "法语", "german": "德语", "spanish": "西班牙语", "undetermined": "未指定",
    }
    value_terms = (
        (r"\bpixels\b", "像素"), (r"\bchannels\b", "声道"),
        (r"\bchannel\b", "声道"), (r"\bfps\b", "帧/秒"),
        (r"\bbits\b", "位"), (r"\bbytes\b", "字节"),
    )
    output = []
    for line in str(value or "").splitlines():
        stripped = line.strip()
        section_key = stripped.rstrip(":").casefold()
        if section_key in sections and ":" not in stripped:
            output.append(line[:len(line) - len(line.lstrip())] + sections[section_key])
            continue
        match = re.match(r"^(\s*)([^:]+?)(\s*:\s*)(.*)$", line)
        if not match:
            output.append(line)
            continue
        indent, key, separator, field_value = match.groups()
        localized_key = fields.get(key.strip().casefold(), key.strip())
        localized_value = language_values.get(field_value.strip().casefold(), field_value)
        for pattern, replacement in value_terms:
            localized_value = re.sub(pattern, replacement, localized_value, flags=re.IGNORECASE)
        output.append(f"{indent}{localized_key}{separator}{localized_value}")
    return "\n".join(output)


def refresh_subscription_video_file(subscription_id, code, file_path):
    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    if subscription is None:
        raise AppError("订阅不存在。")
    code = normalize_code(code)
    if not code:
        raise AppError("番号格式无效。")
    token = get_settings().get("mteam_token", "")
    if not token:
        raise AppError("请先配置 M-Team 存取令牌。")
    root = Path(subscription["download_dir"]).resolve()
    target = Path(str(file_path or "")).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise AppError("视频路径不在当前订阅目录内。") from error
    if not target.is_file() or target.suffix.casefold() not in VIDEO_EXTENSIONS:
        raise AppError("视频文件不存在或格式不受支持。")

    videos = scan_local_releases(subscription["download_dir"])
    items = videos.get(code, [])
    item = next((value for value in items if Path(value.get("path", "")).resolve() == target), None)
    if item is None:
        raise AppError("当前订阅中没有找到该视频文件。")
    all_records = read_json(RECORDS_FILE, {})
    records = all_records.setdefault(subscription_id, {})
    record = records.setdefault(code, {"code": code})
    local_files = record.setdefault("local_files", {})
    path_key = str(target)
    previous = local_files.get(path_key) or local_files.get(str(Path(item["path"]))) or {}
    identifier = str(previous.get("id", ""))
    if not identifier and len(items) == 1:
        metadata = record.get("mteam_metadata") or {}
        identifier = str(metadata.get("id", ""))
    if not identifier and len(items) == 1:
        for release in (record.get("pending"), record.get("current")):
            if release and release.get("id"):
                identifier = str(release["id"])
                break
    if not identifier.isdigit():
        raise AppError("该视频没有 M-Team ID，请先使用“补全 M-Team ID”。")
    detail = mteam_detail(identifier, token, force_refresh=True)
    if not detail_contains_performer(detail, subscription["performer"]):
        raise AppError(f"M-Team 详情中未找到女优关键字“{subscription['performer']}”，已拒绝写入该视频信息。")
    candidate = {
        "id": identifier, "code": code, "title": previous.get("title") or item.get("title") or target.name,
        "rank": previous.get("rank") or item.get("rank", [0, 0, 0]),
        "resolution": previous.get("resolution", ""), "video_codec": previous.get("video_codec", ""),
    }
    enriched = enrich_candidate_from_detail(candidate, token, detail)
    stat = target.stat()
    refreshed = {
        **candidate_metadata(enriched), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        "rank_version": LOCAL_MTEAM_RANK_VERSION,
    }
    for key in ("torrent_hash", "id_source"):
        if previous.get(key):
            refreshed[key] = previous[key]
    if previous.get("subtitle_check"):
        refreshed["subtitle_check"] = previous["subtitle_check"]
    local_files[path_key] = refreshed
    record["local_files"] = local_files
    write_json(RECORDS_FILE, all_records)
    rows = index_subscription_videos(subscription_id, videos, records)
    row = next((value for value in rows if value.get("code") == code), {})
    file_row = next((value for value in row.get("files", []) if value.get("path") == path_key), {})
    return {"code": code, "file": file_row, "message": "视频信息已刷新。"}


def refresh_subscription_videos(subscription_id):
    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    if subscription is None:
        raise AppError("订阅不存在。")
    token = get_settings().get("mteam_token", "")
    if not token:
        raise AppError("请先配置 M-Team 存取令牌。")
    videos = scan_local_releases(subscription["download_dir"])
    all_records = read_json(RECORDS_FILE, {})
    records = all_records.setdefault(subscription_id, {})
    refreshed = 0
    skipped = 0
    failed = 0
    for code, items in videos.items():
        record = records.setdefault(code, {"code": code})
        local_files = record.setdefault("local_files", {})
        for item in items:
            path = Path(item["path"])
            path_key = str(path)
            previous = local_files.get(path_key) or {}
            identifier = str(previous.get("id", ""))
            if not identifier and len(items) == 1:
                identifier = str((record.get("mteam_metadata") or {}).get("id", ""))
            if not identifier.isdigit():
                skipped += 1
                continue
            try:
                detail = mteam_detail(identifier, token)
                if not detail_contains_performer(detail, subscription["performer"]):
                    failed += 1
                    continue
                enriched = enrich_candidate_from_detail({
                    "id": identifier, "code": code,
                    "title": previous.get("title") or item.get("title") or path.name,
                    "rank": previous.get("rank") or item.get("rank", [0, 0, 0]),
                    "resolution": previous.get("resolution", ""),
                    "video_codec": previous.get("video_codec", ""),
                }, token, detail)
                stat = path.stat()
                refreshed_file = {
                    **candidate_metadata(enriched), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "rank_version": LOCAL_MTEAM_RANK_VERSION,
                }
                if previous.get("subtitle_check"):
                    refreshed_file["subtitle_check"] = previous["subtitle_check"]
                local_files[path_key] = refreshed_file
                refreshed += 1
            except MTeamRateLimitError:
                raise
            except (AppError, OSError):
                failed += 1
    write_json(RECORDS_FILE, all_records)
    index_subscription_videos(subscription_id, videos, records)
    log_event(
        f"已刷新当前女优视频信息：成功 {refreshed} 个，跳过 {skipped} 个，失败 {failed} 个。",
        event="local_video_info_refreshed", subscription=subscription["performer"],
    )
    return {"refreshed": refreshed, "skipped": skipped, "failed": failed, "message": "视频信息刷新完成。"}


def best_candidates(items):
    chosen = {}
    for item in items:
        existing = chosen.get(item["code"])
        if existing is None or (tuple(item["rank"]), item["seeders"], item["created"]) > (tuple(existing["rank"]), existing["seeders"], existing["created"]):
            chosen[item["code"]] = item
    return chosen


def local_file_variant_tokens(value):
    stem = Path(str(value)).stem
    code_match = FC2_CODE_RE.search(stem) or CODE_RE.search(stem)
    if code_match:
        stem = stem[:code_match.start()] + " " + stem[code_match.end():]
    return [token for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", stem.casefold()) if len(token) > 1]


def match_local_file_candidates(items, candidates):
    scores = {}
    for item in items:
        path = Path(item["path"])
        local_tokens = local_file_variant_tokens(path.name)
        if not local_tokens:
            continue
        local_set = set(local_tokens)
        for candidate in candidates:
            title_tokens = local_file_variant_tokens(candidate.get("title", ""))
            title_set = set(title_tokens)
            if not local_set.issubset(title_set):
                continue
            identifier = str(candidate.get("id", ""))
            if not identifier:
                continue
            scores[(str(path), identifier)] = sum(len(token) for token in local_set) + len(local_set) * 10
    by_file = {}
    by_candidate = {}
    for (path, identifier), score in scores.items():
        by_file.setdefault(path, []).append((score, identifier))
        by_candidate.setdefault(identifier, []).append((score, path))
    assignments = {}
    for path, matches in by_file.items():
        best_score = max(score for score, _identifier in matches)
        best = {identifier for score, identifier in matches if score == best_score}
        if len(best) != 1:
            continue
        identifier = next(iter(best))
        candidate_matches = by_candidate.get(identifier, [])
        if max(score for score, _path in candidate_matches) != best_score:
            continue
        if sum(score == best_score for score, _path in candidate_matches) != 1:
            continue
        assignments[path] = identifier
    return assignments


def cache_local_file_mteam_metadata(subscription, local_releases, search_results, records, token, force_refresh=False):
    candidates_by_code = {}
    for candidate in search_results:
        candidates_by_code.setdefault(candidate["code"], []).append(candidate)
    for code, items in local_releases.items():
        record = records.get(code)
        if record is None:
            continue
        saved_files = record.get("local_files") or {}
        current_files = {}
        candidates = candidates_by_code.get(code, [])
        assignments = match_local_file_candidates(items, candidates)
        used_ids = set()
        for item in items:
            path = str(Path(item["path"]))
            try:
                stat = Path(path).stat()
            except OSError:
                continue
            previous = saved_files.get(path, {})
            if not force_refresh and previous.get("id") and previous.get("rank_version") == LOCAL_MTEAM_RANK_VERSION and previous.get("size") == stat.st_size and previous.get("mtime_ns") == stat.st_mtime_ns:
                current_files[path] = previous
                used_ids.add(str(previous["id"]))
                continue
            identifier = ""
            candidate = None
            # A file-level M-Team ID is authoritative. Refresh its detail directly,
            # even when the current search page no longer contains that torrent.
            if previous.get("id"):
                identifier = str(previous["id"])
                candidate = {
                    "id": identifier, "code": code,
                    "title": previous.get("title") or item.get("title") or code,
                    "rank": previous.get("rank") or item.get("rank", [0, 0, 0]),
                    "resolution": previous.get("resolution", ""),
                    "video_codec": previous.get("video_codec", ""),
                }
            if candidate is None:
                identifier = assignments.get(path, "")
                candidate = next((value for value in candidates if str(value["id"]) == identifier), None)
            if candidate is None and len(items) == 1:
                metadata = record.get("mteam_metadata") or {}
                identifier = str(metadata.get("id", ""))
                candidate = next((value for value in candidates if str(value["id"]) == identifier), None)
                if candidate is None and identifier:
                    candidate = {
                        "id": identifier, "code": code,
                        "title": metadata.get("title") or item.get("title") or code,
                        "rank": metadata.get("rank") or item.get("rank", [0, 0, 0]),
                        "resolution": metadata.get("resolution", ""),
                        "video_codec": metadata.get("video_codec", ""),
                    }
            if candidate is None or identifier in used_ids:
                if previous:
                    current_files[path] = previous
                continue
            try:
                detail = mteam_detail(candidate["id"], token)
                if not detail_contains_performer(detail, subscription["performer"]):
                    if previous:
                        current_files[path] = previous
                        used_ids.add(identifier)
                    continue
                enriched = enrich_candidate_from_detail(candidate, token, detail)
            except MTeamRateLimitError:
                raise
            except AppError:
                if previous:
                    current_files[path] = previous
                    used_ids.add(identifier)
                continue
            current_files[path] = {
                **candidate_metadata(enriched), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "rank_version": LOCAL_MTEAM_RANK_VERSION,
            }
            used_ids.add(identifier)
        record["local_files"] = current_files


def apply_local_file_mteam_ranks(code, items, record_sources):
    for item in items:
        path_key = str(Path(item["path"]).resolve())
        matches = []
        for records in record_sources:
            for saved_path, metadata in (records.get(code, {}).get("local_files") or {}).items():
                if metadata.get("id") and str(Path(saved_path).resolve()) == path_key:
                    matches.append(metadata)
        identifiers = {str(metadata["id"]) for metadata in matches}
        if len(identifiers) != 1:
            continue
        metadata = matches[0]
        rank = list(metadata.get("rank") or [])
        if len(rank) != 3 or any(not isinstance(value, int) or value not in (0, 1, 2) for value in rank):
            continue
        if rank[1] == 0 and (item.get("metadata") or {}).get("subtitle_available"):
            rank[1] = 1
        item.update({"rank": rank, "mteam_id": next(iter(identifiers)), "rank_source": "M-Team"})


def record_state(item, state, message=""):
    return {**item, "state": state, "message": message, "updated_at": now_iso()}


def rank_dimension_labels(rank):
    rank = tuple(rank or (0, 0, 0))
    resolution = {0: "其他分辨率", 1: "1080p", 2: "4K"}.get(rank[0], "其他分辨率")
    language = {0: "非中文", 1: "中文字幕", 2: "内嵌中文"}.get(rank[1], "非中文")
    return resolution, language


def wash_rank(rank):
    return tuple((rank or (0, 0, 0))[:2])


def local_wash_rank(rank):
    return tuple((rank or (0, 0, 0))[:3])


def local_wash_reason(old_rank, new_rank):
    old_rank = tuple(old_rank or (0, 0, 0))
    new_rank = tuple(new_rank or (0, 0, 0))
    if old_rank == new_rank:
        return "版本等级相同，清理重复文件并保留一个版本"
    labels = ("分辨率", "中文等级", "编码")
    old_values = (*rank_dimension_labels(old_rank), {0: "其他编码", 1: "x264", 2: "x265"}.get(old_rank[2], "其他编码"))
    new_values = (*rank_dimension_labels(new_rank), {0: "其他编码", 1: "x264", 2: "x265"}.get(new_rank[2], "其他编码"))
    changes = [f"{label}：{old} → {new}" for label, old, new in zip(labels, old_values, new_values) if old != new]
    return "；".join(changes) or "版本优先级提高"


def prepare_local_wash_metadata(releases, record_sources, token, performer, force_refresh=False):
    """Resolve duplicate files individually before allowing a destructive comparison."""
    prepared = dict(releases)
    for code, items in releases.items():
        if len(items) < 2:
            continue
        prepared[code] = [{key: value for key, value in item.items() if key != "_wash_skip_reason"} for item in items]
        reasons = []
        bound = 0
        for item in prepared[code]:
            path = Path(item["path"])
            if item.get("_id_binding_error"):
                reasons.append(f"{path.name}：{item['_id_binding_error']}，保留文件")
                continue
            path_key = str(path.resolve())
            matches = []
            number_only_id = False
            for records in record_sources:
                record = records.get(code, {})
                number_only_id |= bool((record.get("mteam_metadata") or {}).get("id"))
                for saved_path, metadata in (record.get("local_files") or {}).items():
                    if metadata.get("id") and str(Path(saved_path).resolve()) == path_key:
                        matches.append((record, saved_path, metadata))
            identifiers = {str(metadata["id"]) for _record, _saved_path, metadata in matches}
            if len(identifiers) > 1:
                reasons.append(f"{path.name} 的逐文件 M-Team ID 冲突")
                continue
            if not matches:
                if number_only_id:
                    reasons.append(f"{path.name} 缺少逐文件 M-Team ID，不能套用同番号其他版本的信息，请先补全 ID")
                item["rank_source"] = "NFO/文件名"
                continue
            bound += 1
            _record, _saved_path, previous = matches[0]
            identifier = str(previous["id"])
            try:
                stat = path.stat()
                fresh = (
                    not force_refresh and metadata_is_complete(previous)
                    and previous.get("rank_version") == LOCAL_MTEAM_RANK_VERSION
                    and previous.get("size") == stat.st_size
                    and previous.get("mtime_ns") == stat.st_mtime_ns
                )
                metadata = previous
                if not fresh:
                    if not token:
                        raise AppError("未配置 M-Team 令牌，无法核对文件等级")
                    detail = mteam_detail(identifier, token)
                    if not detail_contains_performer(detail, performer):
                        raise AppError("详情中没有对应女优关键字")
                    candidate = {
                        "id": identifier, "code": code,
                        "title": previous.get("title") or item.get("title") or path.name,
                        "rank": previous.get("rank") or item.get("rank", [0, 0, 0]),
                    }
                    enriched = enrich_candidate_from_detail(candidate, token, detail)
                    if not metadata_is_complete(candidate_metadata(enriched)):
                        raise AppError("M-Team 详情分级信息不完整")
                    metadata = {
                        **previous, **candidate_metadata(enriched),
                        "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "rank_version": LOCAL_MTEAM_RANK_VERSION,
                    }
                    for owner_record, owner_path, _previous in matches:
                        owner_record["local_files"][owner_path] = dict(metadata)
                rank = list(metadata.get("rank") or [])
                if len(rank) != 3 or any(not isinstance(value, int) or value not in (0, 1, 2) for value in rank):
                    raise AppError("缓存的版本等级无效")
                if rank[1] == 0 and (item.get("metadata") or {}).get("subtitle_available"):
                    rank[1] = 1
                item.update({"rank": rank, "mteam_id": identifier, "rank_source": "M-Team", "_wash_signature": (stat.st_size, stat.st_mtime_ns)})
            except MTeamRateLimitError:
                raise
            except (AppError, OSError, TypeError, ValueError) as error:
                reasons.append(f"{path.name}（M-Team ID {identifier}）核对失败：{error}")
        if bound and bound != len(items):
            reasons.append("部分文件没有独立的 M-Team ID，请先补全 ID 后再比较")
        if reasons:
            for item in prepared[code]:
                item["_wash_skip_reason"] = "；".join(dict.fromkeys(reasons))
    return prepared


def local_wash_description(item):
    rank = tuple(item.get("rank") or (0, 0, 0))
    resolution, language = rank_dimension_labels(rank)
    codec = {0: "其他编码", 1: "x264", 2: "x265"}.get(rank[2], "其他编码")
    identifier = item.get("mteam_id") or "未绑定"
    source = item.get("rank_source") or "NFO/文件名"
    return f"{Path(item['path']).name}（M-Team ID {identifier}，{resolution} · {language} · {codec}，依据：{source}）"


def local_task_for_video(video_path, tasks):
    video = Path(video_path).resolve()
    matches = []
    for info_hash, task in (tasks or {}).items():
        content_path = str(task.get("content_path", "") or "")
        if not content_path:
            continue
        content = Path(content_path).resolve()
        if content == video:
            matches.append((str(info_hash).lower(), task))
            continue
        if content.is_dir():
            try:
                video.relative_to(content)
            except ValueError:
                continue
            contained_videos = [item.resolve() for item in list_local_video_files(str(content))]
            if contained_videos == [video]:
                matches.append((str(info_hash).lower(), task))
    return matches


def remove_local_release_task(video_path, downloader, tasks, all_records):
    matches = local_task_for_video(video_path, tasks)
    if not matches:
        return ""
    if len(matches) != 1:
        raise AppError("多个下载器任务都关联到此视频，已保留任务和本地文件。")
    info_hash, task = matches[0]
    if not task.get("complete"):
        raise AppError("对应下载任务尚未完成，已保留任务和本地文件。")
    references = []
    for subscription_id, items in (all_records or {}).items():
        for code, record in items.items():
            for field in ("current", "pending"):
                release = record.get(field)
                if release and str(release.get("hash", "")).lower() == info_hash:
                    references.append((subscription_id, code, record, field, release))
    if len(references) > 1:
        raise AppError("该下载任务被多个订阅记录共用，已保留任务和本地文件。")
    downloader.remove(info_hash, False)
    for _subscription_id, _code, record, field, _release in references:
        record.pop(field, None)
        if not record.get("current") and not record.get("pending"):
            record.pop("shared_local", None)
    return info_hash


@measure_stage("wash")
def wash_local_duplicate_releases(download_dir, releases, performer="", downloader=None, tasks=None, all_records=None):
    kept = {}
    for code, items in releases.items():
        if len(items) < 2:
            kept[code] = items
            continue
        skip_reason = next((item.get("_wash_skip_reason") for item in items if item.get("_wash_skip_reason")), "")
        if skip_reason:
            kept[code] = items
            log_event(f"跳过 {code} 本地洗版，保留全部文件：{skip_reason}", level="warning", event="local_wash_skipped", subscription=performer)
            continue
        ranked = [(local_wash_rank(item.get("rank")), item) for item in items]
        best_rank = max((rank for rank, _item in ranked), default=(0, 0, 0))
        best_items = [item for rank, item in ranked if rank == best_rank]
        winner = max(best_items, key=lambda item: str(item["path"]))
        remaining = []
        for rank, item in ranked:
            if item is winner:
                remaining.append(item)
                continue
            if rank[:2] == (0, 0) and best_rank[:2] != (0, 0):
                remaining.append(item)
                continue
            try:
                for checked_item in (winner, item):
                    checked_path = Path(checked_item["path"])
                    stat = checked_path.stat()
                    if not checked_path.is_file() or (checked_item.get("_wash_signature") and checked_item["_wash_signature"] != (stat.st_size, stat.st_mtime_ns)):
                        raise AppError("比较后视频文件发生变化，已停止清理")
                if isinstance(downloader, CheckDownloader):
                    tasks = downloader.list_tasks()
                removed_hash = remove_local_release_task(item["path"], downloader, tasks, all_records) if downloader else ""
                remove_local_releases([item["path"]], download_dir, code, [download_dir])
                task_note = f"并已移除下载器任务 {removed_hash}（保留任务数据，由应用精确清理视频及配套文件）" if removed_hash else ""
                reason = local_wash_reason(rank, best_rank)
                log_event(
                    f"本地洗版原因：{reason}；保留：{local_wash_description(winner)}；已删除文件 {local_wash_description(item)}及其 NFO/字幕。{task_note}",
                    subscription=performer,
                    event="local_wash_completed",
                )
            except (AppError, OSError) as error:
                remaining.append(item)
                log_event(
                    f"本地洗版失败，未删除 {Path(item['path']).name}：{error}",
                    level="error",
                    subscription=performer,
                    event="local_wash_failed",
                )
        if remaining:
            kept[code] = remaining
    return kept


def upgrade_reason(old_rank, new_rank):
    old_values = rank_dimension_labels(old_rank)
    new_values = rank_dimension_labels(new_rank)
    labels = ("分辨率", "中文等级")
    changes = [f"{label}：{old} → {new}" for label, old, new in zip(labels, old_values, new_values) if old != new]
    return "；".join(changes) or "版本优先级提高"


def copy_release_to_subscription(source, download_dir):
    invalidate_local_scans(download_dir)
    if not isinstance(source, str):
        raise AppError("下载器没有提供有效的资源路径，无法为此订阅创建硬链接。")
    source_path = Path(source)
    if not source_path.is_absolute():
        raise AppError("下载器没有提供有效的资源路径，无法为此订阅创建硬链接。")
    target_root = Path(download_dir)
    if not source_path.exists():
        raise AppError("共享下载任务已完成，但找不到源文件，无法创建硬链接。")
    target = target_root / source_path.name
    if target.resolve().parent != target_root.resolve():
        raise AppError("资源名称无效，已拒绝在订阅目录之外创建硬链接。")
    if source_path.resolve() == target.resolve():
        return
    created_files = []
    created_directories = []
    def create_hardlink(source_file, target_file):
        if target_file.exists():
            try:
                if os.path.samefile(source_file, target_file):
                    return
            except OSError:
                pass
            raise AppError(f"订阅目录已存在同名文件，未覆盖：{target_file.name}")
        try:
            os.link(source_file, target_file)
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise AppError("共享任务源目录与订阅目录位于不同文件系统，无法硬链接；已停止处理，未复制文件。") from error
            raise
        created_files.append(target_file)
    try:
        target_root.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            if source_path.is_symlink():
                raise AppError("共享任务源目录是符号链接，已拒绝创建链接。")
            try:
                target.resolve().relative_to(source_path.resolve())
            except ValueError:
                pass
            else:
                raise AppError("订阅目标目录位于共享任务源目录内，已拒绝递归链接。")
            def on_walk_error(error):
                raise error
            for current, directories, filenames in os.walk(source_path, onerror=on_walk_error, followlinks=False):
                source_dir = Path(current)
                target_dir = target / source_dir.relative_to(source_path)
                if not target_dir.exists():
                    target_dir.mkdir(parents=True)
                    created_directories.append(target_dir)
                for name in directories:
                    if (source_dir / name).is_symlink():
                        raise AppError(f"共享任务包含符号链接目录，已拒绝处理：{name}")
                for name in filenames:
                    source_file = source_dir / name
                    if source_file.is_symlink():
                        raise AppError(f"共享任务包含符号链接文件，已拒绝处理：{name}")
                    create_hardlink(source_file, target_dir / name)
        else:
            create_hardlink(source_path, target)
    except (OSError, AppError) as error:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for path in reversed(created_directories):
            try:
                path.rmdir()
            except OSError:
                pass
        if isinstance(error, AppError):
            raise
        raise AppError(f"共享任务已完成，但无法创建硬链接：{error}") from error


def link_local_release_to_subscription(source, download_dir, code):
    invalidate_local_scans(download_dir)
    source_path = Path(source)
    if source_path.is_symlink() or not source_path.is_file() or source_path.suffix.casefold() not in VIDEO_EXTENSIONS:
        raise AppError("数据库中的来源影片已不存在或不是有效视频文件。")
    target_root = Path(download_dir)
    target_root.mkdir(parents=True, exist_ok=True)
    target_root = target_root.resolve()
    sources = [source_path]
    source_stem = source_path.stem.casefold()
    try:
        for sibling in source_path.parent.iterdir():
            if not sibling.is_file() or sibling.is_symlink() or sibling == source_path:
                continue
            if sibling.suffix.casefold() not in SUBTITLE_EXTENSIONS | {".nfo"}:
                continue
            sibling_stem = sibling.stem.casefold()
            if sibling_stem == source_stem or (sibling.suffix.casefold() in SUBTITLE_EXTENSIONS and normalize_code(sibling.stem) == code):
                sources.append(sibling)
    except OSError:
        pass

    created = []
    target_video = None
    try:
        for item in sources:
            target = target_root / item.name
            if target.resolve().parent != target_root:
                raise AppError("来源文件名无效，已拒绝在订阅目录外创建链接。")
            if target.exists():
                if item == source_path:
                    raise AppError("订阅目录中已有同名文件，无法安全创建硬链接。")
                continue
            try:
                os.link(item, target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                raise AppError("影片与订阅目录位于不同文件系统，无法硬链接；为避免整文件复制，已停止复用。") from error
            else:
                created.append(target)
            if item == source_path:
                target_video = target
    except (OSError, AppError) as error:
        for path in created:
            path.unlink(missing_ok=True)
        if isinstance(error, AppError):
            raise
        raise AppError(f"无法将已有影片链接到订阅目录：{error}") from error
    if target_video is None:
        raise AppError("未能在订阅目录创建影片硬链接。")
    return {"path": str(target_video)}


@measure_stage("sync")
def poll_downloader_records(downloader, records, results, all_records=None, performer="", token=""):
    if not records:
        return
    def report(message, event):
        progress = getattr(downloader, "_progress", None)
        if callable(progress):
            progress(message, event)

    hashes = {
        str(release.get("hash", "")).lower()
        for item in records.values()
        for release in (item.get("pending"), item.get("current"))
        if release and release.get("hash") and not release.get("local_existing")
        and release.get("state") not in ("failed", "superseded")
    }
    tasks = downloader.list_tasks(hashes) if hashes else {}
    context = check_context()
    task_generation = context.generation if context is not None else 0
    processable = sum(
        1 for item in records.values()
        if (active := (item.get("pending") or item.get("current")))
        and not (active.get("local_existing") and not item.get("pending"))
        and active.get("state") not in ("superseded", "failed")
    )
    if processable:
        report(f"正在处理下载任务状态（{processable} 个任务）。", "task_status_started")
    for item in records.values():
        if context is not None and task_generation != context.generation:
            tasks = downloader.list_tasks(hashes) if hashes else {}
            task_generation = context.generation
        pending = item.get("pending")
        active = pending or item.get("current")
        if active and active.get("local_existing") and not pending:
            results.append({"code": item.get("code", ""), "title": active.get("title", ""), "state": "completed", "progress": 100, "message": active.get("message", "本地已存在该番号。")})
            continue
        if not active or active.get("state") in ("superseded", "failed"):
            continue
        code = item.get("code") or active.get("code") or "未知番号"
        if isinstance(downloader, CheckDownloader) and active.get("hash"):
            tasks.pop(str(active["hash"]).lower(), None)
            tasks.update(downloader.list_tasks([active["hash"]]))
        if active.get("state") == "completed" and not pending and active.get("download_dir"):
            directory = active["download_dir"]
            try:
                local_files = scan_local_releases(directory).get(code, [])
            except AppError as error:
                active["message"] = f"下载任务曾标记完成，但无法核对本地文件：{error}"
                report(f"{code} 已完成记录无法核对本地文件，保留记录并跳过重新提交。", "completed_file_check_failed")
                continue
            if not local_files:
                message = "下载任务曾标记完成，但订阅目录中没有识别到该番号的视频，将重新检查并提交。"
                if active.get("owns_task", True):
                    task = tasks.get(str(active.get("hash", "")).lower())
                    if task:
                        if not active.get("completed_missing_recheck_requested"):
                            try:
                                location = downloader.recheck(active["hash"], active["download_dir"])
                                active["completed_missing_recheck_requested"] = True
                                active["state"] = "downloading"
                                active["message"] = f"qBittorrent 中仍有该任务，但订阅目录缺少视频；已切换到订阅目录并执行强制校验。{f'下载器路径：{location}。' if location else ''}"
                                active["updated_at"] = now_iso()
                                report(f"{code} 的已完成任务仍存在，但订阅目录没有视频；已切换下载器目录并触发强制校验。", "completed_missing_files_recheck")
                            except AppError as error:
                                active["message"] = f"{message} 强制校验失败：{error}"
                                report(f"{code} 的已完成任务仍存在，但强制校验失败：{error}", "completed_missing_files_recheck_failed")
                        else:
                            active["state"] = "downloading"
                            active["message"] = "已请求强制校验，等待下载器检查并补齐缺失文件。"
                        results.append({"code": code, "title": active.get("title", ""), "state": "downloading", "progress": active.get("progress", 0), "message": active["message"]})
                    else:
                        item.pop("current", None)
                        item.pop("last_error", None)
                        item.pop("retry_after", None)
                        results.append({"code": code, "title": active.get("title", ""), "state": "missing", "message": message})
                        report(message, "completed_file_missing")
                    continue
        task = tasks.get(str(active.get("hash", "")).lower()) if active.get("state") != "completed" or not active.get("owns_task", True) else {"complete": True, "progress": 1}
        if task is None:
            active["state"] = "missing"
            active["message"] = "下载器中找不到该任务，请检查下载器任务列表。"
            if pending:
                item.pop("pending", None)
                item.pop("last_error", None)
                item.pop("retry_after", None)
                results.append({"code": item.get("code", ""), "title": active.get("title", ""), "state": "missing", "message": active["message"]})
            continue
        if task.get("state") == "missingFiles":
            recheck_now = not active.get("missing_files_recheck_requested")
            if recheck_now:
                location = downloader.recheck(active["hash"], active.get("download_dir", ""))
                active["missing_files_recheck_requested"] = True
            active["state"] = "downloading"
            active["message"] = "qBittorrent 报告任务数据缺失，已触发强制校验并请求补下载缺失数据。" if recheck_now else "qBittorrent 任务数据仍在校验或等待补下载。"
            active["updated_at"] = now_iso()
            if recheck_now:
                report(f"{code} 的 qBittorrent 任务数据缺失，已触发强制校验和补下载。", "missing_files_recheck")
            results.append({"code": code, "title": active.get("title", ""), "state": "downloading", "progress": active.get("progress", 0), "message": active["message"]})
            continue
        active.pop("missing_files_recheck_requested", None)
        if task.get("failed"):
            active["state"] = "failed"
            active["message"] = task.get("error", "下载器报告任务失败。")
            try:
                report(f"正在移除下载器报告失败的任务：{code}。", "failed_task_removal_started")
                downloader.remove(active["hash"], True)
                report(f"失败任务已移除：{code}。", "failed_task_removal_completed")
            except AppError:
                pass
            if pending:
                item["last_error"] = active["message"]
                item["retry_after"] = time.time() + 3600
                item.pop("pending", None)
            else:
                item["last_error"] = active["message"]
                item["retry_after"] = time.time() + 3600
                item.pop("current", None)
            results.append({"code": item.get("code", ""), "title": active.get("title", ""), "state": "failed", "message": active["message"]})
            continue
        active["progress"] = round(float(task.get("progress", 0)) * 100, 2)
        active["state"] = "completed" if task.get("complete") else ("upgrading" if pending else "downloading")
        active["updated_at"] = now_iso()
        active.pop("copy_required", None)
        if active["state"] == "completed" and not active.get("owns_task", True):
            try:
                local_files = find_local_releases(active.get("download_dir", ""), code)
            except AppError:
                local_files = []
            if not local_files:
                new_directory_warning = not active.get("directory_warning_logged")
                active.setdefault("message", "下载器中的相同任务已完成，但订阅目录未找到该番号的视频。")
                if new_directory_warning:
                    active["directory_warning_logged"] = True
                if not active.get("directory_recheck_requested"):
                    try:
                        location = downloader.recheck(active["hash"], active.get("download_dir", ""))
                        active["directory_recheck_requested"] = True
                        active["state"] = "downloading"
                        active["message"] += " 已切换到女优订阅对应的下载器目录并执行强制校验；若保存位置缺少数据，qBittorrent 会继续补下载。"
                        location_message = f"下载器目录 {location}" if location else "原下载器目录"
                        warning = f"订阅目录中未找到 {code}，已将 qBittorrent 任务切换到{location_message}并执行强制校验。"
                        log_event(warning, level="warning", event="existing_task_directory_recheck", subscription=performer)
                    except AppError as error:
                        active["message"] += f" 强制校验未能启动：{error}"
                        warning = f"订阅目录中未找到 {code}；尝试校验下载器中的同一任务失败：{error}"
                        log_event(warning, level="warning", event="existing_task_directory_missing", subscription=performer)
                elif new_directory_warning:
                    warning = f"下载器中的同一任务已校验，但订阅目录仍未找到 {code}；请检查任务保存路径。"
                    log_event(warning, level="warning", event="existing_task_directory_missing", subscription=performer)
            elif local_files:
                active.pop("directory_warning_logged", None)
                active.pop("directory_recheck_requested", None)
                active.pop("completed_missing_recheck_requested", None)
        if active["state"] == "completed" and pending:
            old = item.get("current")
            still_referenced = False
            if old:
                if old.get("local_existing"):
                    try:
                        remove_local_releases(old.get("local_paths", []), old["download_dir"], item.get("code", ""), old.get("local_roots"))
                    except AppError as error:
                        active["state"] = "cleanup_failed"
                        active["message"] = str(error)
                        results.append({"code": item.get("code", ""), "title": active.get("title", ""), "state": "cleanup_failed", "progress": active.get("progress", 0), "message": active["message"]})
                        continue
                elif old.get("hash") and old.get("hash") != active.get("hash"):
                    still_referenced = any(
                        release.get("hash") == old["hash"]
                        for subscription_items in (all_records or {}).values()
                        for other in subscription_items.values()
                        if other is not item
                        for release in (other.get("current"), other.get("pending"))
                        if release
                    )
                    if not still_referenced and old.get("owns_task", True):
                        try:
                            report(f"新版已完成，正在清理旧下载任务和数据：{code}。", "old_task_cleanup_started")
                            downloader.remove(old["hash"], True)
                            report(f"旧下载任务和数据已清理：{code}。", "old_task_cleanup_completed")
                        except AppError as error:
                            active["state"] = "cleanup_failed"
                            active["message"] = f"新版已下载完成，旧版本清理失败：{error}"
                            results.append({"code": item.get("code", ""), "title": active.get("title", ""), "state": "cleanup_failed", "progress": active.get("progress", 0), "message": active["message"]})
                            continue
                    elif still_referenced and old.get("owns_task", True):
                        owner_transferred = False
                        for subscription_items in (all_records or {}).values():
                            for other in subscription_items.values():
                                if other is item:
                                    continue
                                for reference in (other.get("current"), other.get("pending")):
                                    if reference and reference.get("hash") == old["hash"]:
                                        reference["owns_task"] = True
                                        old["owns_task"] = False
                                        owner_transferred = True
                                        break
                                if owner_transferred:
                                    break
                old["state"] = "superseded"
                if old.get("local_existing"):
                    old["message"] = "本地旧版文件已删除。"
                elif still_referenced:
                    old["message"] = "已由更高优先级版本替换，其他订阅仍在使用该任务。"
                elif old.get("owns_task") is False:
                    old["message"] = "已由更高优先级版本替换。原任务由其他用户或流程创建，未自动删除。"
                elif old.get("hash") and old.get("hash") != active.get("hash"):
                    old["message"] = "已由更高优先级版本替换，旧任务和数据已删除。"
                else:
                    old["message"] = "已由更高优先级版本替换。"
            item["current"] = active
            item.pop("pending", None)
            item["current"]["message"] = "下载完成。"
        if active["state"] == "completed" and token and not active.get("local_existing"):
            _download_release_subtitles(active, code, token, performer, item.get("mteam_metadata"))
        results.append({"code": item.get("code", ""), "title": active.get("title", ""), "state": active["state"], "progress": active.get("progress", 0), "message": active.get("message", "")})


def _download_release_subtitles(active, code, token, performer, mteam_metadata=None):
    ranks = [active.get("rank", [0, 0, 0]), (mteam_metadata or {}).get("rank", [])]
    chinese_ranks = [rank[1] for rank in ranks if isinstance(rank, (list, tuple)) and len(rank) > 1]
    if not chinese_ranks or 2 in chinese_ranks or 1 not in chinese_ranks:
        return
    try:
        local_files = find_local_releases(active.get("download_dir", ""), code)
    except AppError as error:
        log_event(f"{code} 字幕检查跳过：无法读取影片目录：{error}", level="warning", event="subtitle_directory_unavailable", subscription=performer)
        return
    videos = [Path(item["path"]) for item in local_files if item.get("path") and Path(item["path"]).is_file()]
    if not videos:
        return
    pending_videos = [
        video for video in videos
        if not any(video.parent.glob(f"{video.stem}.MT.*"))
    ]
    if not pending_videos:
        return
    prior = active.get("subtitle_check") or {}
    current_paths = sorted(str(path) for path in pending_videos)
    same_files = prior.get("video_paths") == current_paths and prior.get("version") == SUBTITLE_CHECK_VERSION
    state = prior.get("state")
    checked_at = float(prior.get("checked_epoch", 0) or 0) if same_files else 0
    retry_interval = 24 * 3600 if state == "not_found" else 3600 if state == "failed" else 0
    if retry_interval and time.time() - checked_at < retry_interval:
        return
    try:
        torrent_id = active.get("id")
        if not torrent_id:
            active["subtitle_check"] = {"state": "failed", "message": "缺少影片 M-Team ID。", "checked_at": now_iso(), "checked_epoch": time.time(), "video_paths": current_paths, "version": SUBTITLE_CHECK_VERSION}
            log_event(f"{code} 字幕检查跳过：记录中没有影片 M-Team ID。", level="warning", event="subtitle_id_missing", subscription=performer)
            return
        result = mteam_subtitle_download(code, torrent_id, pending_videos, token)
    except MTeamRateLimitError:
        raise
    except AppError as error:
        active["subtitle_check"] = {
            "state": "failed", "message": str(error), "checked_at": now_iso(),
            "checked_epoch": time.time(), "video_paths": current_paths, "version": SUBTITLE_CHECK_VERSION,
        }
        log_event(f"{code} 外挂字幕下载失败：{error}", level="warning", event="subtitle_download_failed", subscription=performer)
        return
    active["subtitle_check"] = {
        **result, "state": result["status"], "checked_at": now_iso(), "checked_epoch": time.time(), "video_paths": current_paths, "version": SUBTITLE_CHECK_VERSION,
    }
    if result["status"] == "downloaded":
        log_event(
            f"{code} 已下载 M-Team 外挂字幕（下载量 {result['downloads']}）：{', '.join(Path(path).name for path in result['files'])}。",
            event="subtitle_download_completed", subscription=performer,
        )
    else:
        log_event(f"{code} 在 M-Team 字幕区没有找到可下载的中文字幕。", event="subtitle_not_found", subscription=performer)


def _download_existing_local_subtitles(code, local_items, record, token, performer):
    """Download bound external subtitles for local files whose M-Team metadata is cached."""
    local_files = record.get("local_files") or {}
    for item in local_items:
        if item.get("_id_binding_error"):
            continue
        video = Path(item.get("path", ""))
        if not video.is_file():
            continue
        metadata = local_files.get(str(video)) or (record.get("mteam_metadata") if len(local_items) == 1 else {}) or {}
        identifier = str(metadata.get("id", "")).strip()
        rank = metadata.get("rank") or item.get("rank") or [0, 0, 0]
        # Rank 2 means embedded Chinese; it must never trigger an external subtitle request.
        if not identifier or not isinstance(rank, (list, tuple)) or len(rank) < 2 or rank[1] not in (0, 1):
            continue
        if any(sidecar.suffix.casefold() in SUBTITLE_EXTENSIONS for sidecar in local_video_sidecars(video)):
            continue
        non_chinese = rank[1] == 0
        prior = metadata.get("subtitle_check") or {}
        paths = [str(video)]
        if prior.get("video_paths") == paths and prior.get("version") == SUBTITLE_CHECK_VERSION:
            checked_epoch = float(prior.get("checked_epoch", 0) or 0)
            retry_interval = 0 if non_chinese else 24 * 3600 if prior.get("state") == "not_found" else 3600 if prior.get("state") == "failed" else 0
            if retry_interval and time.time() - checked_epoch < retry_interval:
                continue
        try:
            result = mteam_subtitle_download(code, identifier, [video], token)
        except MTeamRateLimitError:
            raise
        except AppError as error:
            metadata["subtitle_check"] = {
                "state": "failed", "message": str(error), "checked_at": now_iso(),
                "checked_epoch": time.time(), "video_paths": paths, "version": SUBTITLE_CHECK_VERSION,
            }
            log_event(f"{code} 外挂字幕下载失败：{error}", level="warning", event="subtitle_download_failed", subscription=performer)
            continue
        metadata["subtitle_check"] = {
            **result, "state": result["status"], "checked_at": now_iso(),
            "checked_epoch": time.time(), "video_paths": paths, "version": SUBTITLE_CHECK_VERSION,
        }
        if result["status"] == "downloaded":
            updated_rank = list(rank)
            updated_rank[1] = max(1, updated_rank[1])
            metadata["rank"] = updated_rank
            metadata["subtitle_available"] = True
            record.setdefault("local_files", {})[str(video)] = metadata
            if non_chinese:
                try:
                    enriched = enrich_candidate_from_detail({
                        **metadata, "id": identifier, "code": code,
                        "title": metadata.get("title") or item.get("title") or video.name,
                    }, token)
                    metadata.update(candidate_metadata(enriched))
                    metadata["rank"][1] = max(1, metadata["rank"][1])
                except MTeamRateLimitError:
                    raise
                except AppError as error:
                    log_event(f"{code} 字幕已下载，详情刷新失败：{error}", level="warning", event="subtitle_metadata_failed", subscription=performer)
            log_event(
                f"{code} 已为本地影片下载 M-Team 外挂字幕（下载量 {result['downloads']}）：{', '.join(Path(path).name for path in result['files'])}。",
                event="subtitle_download_completed", subscription=performer,
            )
        else:
            log_event(f"{code} 在 M-Team 字幕区没有找到可下载的中文字幕。", event="subtitle_not_found", subscription=performer)


def submit_candidate(subscription, candidate, token, downloader, item):
    detail = candidate.get("_mteam_detail") or mteam_detail(candidate["id"], token)
    if not detail_contains_performer(detail, subscription["performer"]):
        raise PerformerMismatchError(f"M-Team 详情中未找到女优关键字“{subscription['performer']}”，已跳过 {candidate['code']}。")
    clean_candidate = {key: value for key, value in candidate.items() if not key.startswith("_")}
    if not clean_candidate.get("subtitle"):
        clean_candidate["subtitle"] = str(next((detail.get(key) for key in ("smallDescr", "small_descr", "subtitle", "subTitle") if detail.get(key)), "") or "")
    clean_candidate["subtitle_checked"] = True
    torrent_data = fetch_torrent(candidate["id"], token)
    added = downloader.add(torrent_data, candidate["title"], subscription["download_dir"])
    if isinstance(added, dict):
        info_hash = added["hash"]
        owns_task = bool(added.get("owned", False))
    else:
        info_hash = added
        owns_task = True
    if owns_task:
        message = "已提交到下载器。"
    else:
        existing = added.get("existing_task", {}) if isinstance(added, dict) else {}
        location = existing.get("content_path") or existing.get("save_path")
        message = "下载器中已存在相同种子；按设置仅警告，不会重复添加，也不会自动硬链接。"
        if location:
            message += f" 下载器保存位置：{location}。"
        if isinstance(added, dict) and added.get("recheck_requested"):
            mapped = added.get("recheck_location")
            message += " 检测到任务数据缺失，已将任务目录切换为下载器路径并请求 qBittorrent 校验补下载。"
            if mapped:
                message += f" 当前下载器目录：{mapped}。"
        elif isinstance(added, dict) and added.get("recheck_skipped_paused"):
            message += " 检测到任务数据缺失，但下载任务已暂停；未自动启动，请在 qBittorrent 中恢复任务。"
    return record_state({**clean_candidate, "hash": info_hash, "progress": 0, "owns_task": owns_task, "download_dir": subscription["download_dir"], "copy_required": False}, "downloading", message)


def process_subscription(subscription, records, settings, downloader, results, refresh_existing_info=False, local_scan_cache=None, local_task_cache=None):
    if local_task_cache is None:
        local_task_cache = {}
    keyword = " ".join(part for part in (subscription["performer"], subscription.get("keywords", "")) if part).strip()
    subscription_records = records.setdefault(subscription["id"], {})
    local_dirs = [subscription["download_dir"]]
    local_subscriptions = [subscription]
    if settings.get("local_multi_disk_wash", False):
        performer_key = normalized_performer_name(subscription.get("performer", ""))
        local_subscriptions.extend(
            item for item in list_subscriptions()
            if item.get("id") != subscription["id"]
            and normalized_performer_name(item.get("performer", "")) == performer_key
        )
        local_dirs.extend(
            item.get("download_dir", "") for item in local_subscriptions
        )
    local_dirs = list(dict.fromkeys(directory for directory in local_dirs if directory))
    local_releases = {}
    scans_by_directory = {}
    log_event(f"开始扫描本地目录，共 {len(local_dirs)} 个。", event="local_scan_started", subscription=subscription["performer"])
    for directory in local_dirs:
        cache_key = os.path.realpath(directory)
        if settings.get("cleanup_orphan_sidecars", False):
            removed_sidecars = cleanup_orphan_sidecars(directory, excluded_dirs=[settings.get("subtitle_collection_dir", "")])
            if removed_sidecars:
                invalidate_local_scans(directory)
                if local_scan_cache is not None:
                    local_scan_cache.pop(cache_key, None)
                log_event(
                    f"自动清理了 {len(removed_sidecars)} 个没有对应视频的 NFO/字幕文件。",
                    event="orphan_sidecars_cleaned",
                    subscription=subscription["performer"],
                )
        if check_context() is not None:
            scanned = scan_local_releases(directory)
        elif local_scan_cache is not None and cache_key not in local_scan_cache:
            local_scan_cache[cache_key] = scan_local_releases(directory)
            scanned = local_scan_cache[cache_key]
        else:
            scanned = local_scan_cache[cache_key] if local_scan_cache is not None else scan_local_releases(directory)
        directory_owners = [
            owner for owner in local_subscriptions
            if os.path.realpath(owner.get("download_dir", "")) == cache_key
        ]
        for owner in directory_owners:
            owner_records = records.setdefault(owner["id"], {})
            bind_missing_local_mteam_ids(owner, scanned, owner_records, downloader, local_task_cache, settings)
        if settings.get("local_multi_disk_wash", False):
            has_duplicates = any(len(items) > 1 for items in scanned.values())
            if has_duplicates:
                directory_records = [
                    records.get(owner["id"], {}) for owner in directory_owners
                ]
                scanned = prepare_local_wash_metadata(
                    scanned, directory_records, settings.get("mteam_token", ""),
                    subscription["performer"], refresh_existing_info,
                )
            tasks = check_task_snapshot(downloader, local_task_cache) if has_duplicates else None
            scanned = wash_local_duplicate_releases(
                directory, scanned, subscription["performer"], downloader,
                tasks, records,
            )
            if local_scan_cache is not None:
                local_scan_cache[cache_key] = scanned
        scans_by_directory[cache_key] = scanned
        for code, items in scanned.items():
            local_releases.setdefault(code, []).extend(items)
    log_event(f"本地扫描完成，识别到 {sum(len(items) for items in local_releases.values())} 个视频、{len(local_releases)} 个番号。", event="local_scan_completed", subscription=subscription["performer"])
    refresh_codes = set(subscription_records) | set(local_releases)
    log_event("正在请求 M-Team 搜索结果。", event="mteam_search_started", subscription=subscription["performer"])
    search_results = mteam_search(keyword, settings["mteam_token"])
    log_event(f"M-Team 搜索返回 {len(search_results)} 条资源。", event="mteam_search_completed", subscription=subscription["performer"])
    candidates = best_candidates(search_results)
    log_debug(settings, f"搜索词“{keyword}”解析出 {len(candidates)} 个番号：{', '.join(sorted(candidates)) or '无'}。", subscription["performer"])
    blacklist = set(subscription.get("blacklist", []))
    blocked_codes = sorted(set(candidates) & blacklist)
    if blocked_codes:
        log_debug(settings, f"黑名单过滤番号：{', '.join(blocked_codes)}。", subscription["performer"])
    candidates = {code: candidate for code, candidate in candidates.items() if code not in blacklist}
    refreshed_existing = {}
    for code in refresh_codes:
        record = subscription_records.get(code)
        if not record:
            continue
        saved = record.get("mteam_metadata") or {}
        current = record.get("current") or {}
        identifier = saved.get("id") or current.get("id")
        current_rank = current.get("rank", saved.get("rank", [0, 0, 0]))
        needs_rank_lookup = wash_rank(current_rank) == (0, 0)
        if not identifier or not str(identifier).isdigit() or not (refresh_existing_info or needs_rank_lookup):
            continue
        seed = {
            "id": str(identifier), "code": code,
            "title": saved.get("title") or current.get("title") or code,
            "rank": saved.get("rank") or current.get("rank", [0, 0, 0]),
            "resolution": saved.get("resolution", ""),
            "video_codec": saved.get("video_codec", ""),
            "metadata_complete": False,
        }
        try:
            refreshed = enrich_candidate_from_detail(seed, settings["mteam_token"])
            record["mteam_metadata"] = candidate_metadata(refreshed)
            if current and (refresh_existing_info or needs_rank_lookup):
                current["rank"] = list(refreshed["rank"])
                current["resolution"] = refreshed.get("resolution", current.get("resolution", ""))
                current["video_codec"] = refreshed.get("video_codec", current.get("video_codec", ""))
            refreshed_existing[code] = refreshed
        except MTeamRateLimitError:
            raise
        except AppError:
            continue
    for code, candidate in candidates.items():
        record = subscription_records.get(code)
        log_debug(settings, f"检查候选 {code}（M-Team ID {candidate.get('id', '未知')}），已有记录={'是' if record else '否'}，本地文件={'是' if local_releases.get(code) else '否'}。", subscription["performer"])
        saved_metadata = (record or {}).get("mteam_metadata") or {}
        saved_complete = metadata_is_complete(saved_metadata)
        same_saved_id = bool(saved_metadata.get("id")) and str(saved_metadata.get("id", "")) == str(candidate.get("id", ""))
        same_saved_candidate = saved_complete and same_saved_id
        force_refresh = refresh_existing_info and code in refresh_codes
        cached_refresh = refreshed_existing.get(code)
        already_refreshed = cached_refresh and str(cached_refresh.get("id", "")) == str(candidate.get("id", ""))
        refreshed = bool(already_refreshed)
        if already_refreshed:
            candidate = cached_refresh
            candidates[code] = candidate
        elif force_refresh or (candidate.get("metadata_complete") is False and not same_saved_candidate):
            try:
                candidate = enrich_candidate_from_detail(candidate, settings["mteam_token"])
                candidates[code] = candidate
                refreshed = True
            except MTeamRateLimitError:
                raise
            except AppError:
                pass
        elif same_saved_candidate:
            candidate = {
                **candidate,
                "rank": saved_metadata.get("rank", candidate["rank"]),
                "resolution": saved_metadata.get("resolution", candidate.get("resolution", "")),
                "video_codec": saved_metadata.get("video_codec", candidate.get("video_codec", "")),
                "metadata_complete": True,
            }
            candidates[code] = candidate
        if record and same_saved_id and not saved_complete:
            for tracked in (record.get("pending"), record.get("current")):
                if tracked and str(tracked.get("id", "")) == str(candidate.get("id", "")):
                    tracked["rank"] = list(candidate["rank"])
                    tracked["resolution"] = candidate.get("resolution", tracked.get("resolution", ""))
                    tracked["video_codec"] = candidate.get("video_codec", tracked.get("video_codec", ""))
        if local_releases.get(code) and (record is None or (not record.get("current") and not record.get("pending"))):
            found = local_releases[code]
            best_local = max(found, key=lambda item: tuple(item["rank"]))
            current = record_state({
                "id": "", "code": code, "title": best_local["title"], "rank": best_local["rank"],
                "hash": "", "owns_task": False, "local_existing": True,
                "local_paths": [item["path"] for item in found], "download_dir": subscription["download_dir"], "progress": 100,
            }, "completed", "订阅目录中已存在该番号，已标记为完成。")
            record = record or {"code": code}
            record["current"] = current
            subscription_records[code] = record
        if record and (record.get("current") or {}).get("local_existing"):
            local_items = local_releases.get(code, [])
            if local_items:
                best_local = max(local_items, key=lambda item: tuple(item["rank"]))
                record["current"].update({
                    "title": best_local["title"], "rank": best_local["rank"],
                    "local_paths": [item["path"] for item in local_items],
                    "local_roots": local_dirs if settings.get("local_multi_disk_wash", False) else [subscription["download_dir"]],
                })
            existing_paths = record["current"].get("local_paths", [])
            if not any(Path(value).is_file() for value in existing_paths) and record["current"].get("owns_task", True):
                record.pop("current", None)
                record.pop("shared_local", None)
        if record and local_releases.get(code):
            cache_local_file_mteam_metadata(
                subscription, {code: local_releases[code]}, search_results,
                {code: record}, settings["mteam_token"], refresh_existing_info,
            )
            path_record_sources = [records.get(owner.get("id"), {}) for owner in local_subscriptions]
            apply_local_file_mteam_ranks(code, local_releases[code], path_record_sources)
            current_local = record.get("current") or {}
            if current_local.get("local_existing") and local_releases[code]:
                best_local = max(local_releases[code], key=lambda item: tuple(item["rank"]))
                current_local["rank"] = list(best_local["rank"])
                current_local["title"] = best_local["title"]
        if record and not record.get("pending") and (record.get("current") or {}).get("state") == "missing":
            record.pop("current", None)
            record.pop("last_error", None)
            record.pop("retry_after", None)
        if (record is None or record.get("current") is None) and not local_releases.get(code):
            shared = find_indexed_local_release(code, subscription["id"], subscription["download_dir"])
            if shared:
                try:
                    detail = candidate.get("_mteam_detail") or mteam_detail(candidate["id"], settings["mteam_token"])
                    if not detail_contains_performer(detail, subscription["performer"]):
                        raise PerformerMismatchError(f"M-Team 详情中未找到女优关键字“{subscription['performer']}”，已跳过 {code}。")
                    linked = link_local_release_to_subscription(shared["path"], subscription["download_dir"], code)
                    own_scan_key = os.path.realpath(subscription["download_dir"])
                    refreshed_local = scan_local_releases(subscription["download_dir"])
                    if local_scan_cache is not None:
                        local_scan_cache[own_scan_key] = refreshed_local
                    scans_by_directory[own_scan_key] = refreshed_local
                    linked_items = refreshed_local.get(code, [])
                    if not linked_items:
                        raise AppError("影片已创建链接，但无法从目标目录识别其番号。")
                    best_local = max(linked_items, key=lambda item: tuple(item["rank"]))
                    current = record_state({
                        "id": "", "code": code, "title": best_local["title"], "rank": best_local["rank"],
                        "hash": "", "owns_task": False, "local_existing": True,
                        "local_paths": [item["path"] for item in linked_items],
                        "download_dir": subscription["download_dir"], "progress": 100,
                    }, "completed", "该番号已存在于其他订阅，已复用本地影片并跳过下载。")
                    record = record or {"code": code}
                    record["current"] = current
                    record["shared_local"] = True
                    record.pop("last_error", None)
                    record.pop("retry_after", None)
                    subscription_records[code] = record
                    results.append({
                        "subscription": subscription["performer"], "code": code,
                        "title": candidate["title"], "state": "completed",
                        "message": "该番号已存在于其他订阅，已复用本地影片并跳过下载。",
                    })
                    continue
                except PerformerMismatchError as error:
                    results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "skipped", "message": str(error)})
                    continue
                except MTeamRateLimitError:
                    raise
                except AppError as error:
                    record = record or {"code": code}
                    record["last_error"] = str(error)
                    record["retry_after"] = time.time() + 3600
                    subscription_records[code] = record
                    results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "failed", "message": str(error)})
                    continue
        if record is not None:
            if not record.get("shared_local") and code not in refreshed_existing and ((force_refresh and refreshed) or (not saved_complete and (not saved_metadata or same_saved_id))):
                record["mteam_metadata"] = candidate_metadata(candidate)
        if record and record.get("shared_local") and (record.get("current") or {}).get("local_existing"):
            log_debug(settings, f"跳过 {code}：已复用其他订阅的本地影片。", subscription["performer"])
            results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "completed", "message": "该番号已复用其他订阅的本地影片，跳过重复下载。"})
            continue
        if record is None:
            try:
                pending = submit_candidate(subscription, candidate, settings["mteam_token"], downloader, None)
                subscription_records[code] = {"code": code, "current": pending, "mteam_metadata": candidate_metadata(pending)}
                results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "downloading", "message": pending.get("message", "已提交到下载器。")})
            except PerformerMismatchError as error:
                results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "skipped", "message": str(error)})
            except MTeamRateLimitError:
                raise
            except AppError as error:
                subscription_records[code] = {"code": code, "last_error": str(error), "retry_after": time.time() + 3600, "mteam_metadata": candidate_metadata(candidate)}
                results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "failed", "message": str(error)})
            continue
        current = record.get("pending") or record.get("current")
        if record.get("pending"):
            pending = record["pending"]
            if str(candidate.get("id", "")) == str(pending.get("id", "")):
                log_debug(settings, f"跳过 {code}：相同 M-Team ID 已在待下载/洗版任务中跟踪。", subscription["performer"])
                if tuple(candidate["rank"]) > tuple(pending.get("rank", [0, 0, 0])):
                    pending["rank"] = list(candidate["rank"])
                    pending["resolution"] = candidate.get("resolution", pending.get("resolution", ""))
                    pending["video_codec"] = candidate.get("video_codec", pending.get("video_codec", ""))
                    record["mteam_metadata"] = candidate_metadata(candidate)
                continue
            if wash_rank(candidate["rank"]) > wash_rank(pending["rank"]):
                reason = upgrade_reason(pending["rank"], candidate["rank"])
                replacement = None
                try:
                    replacement = submit_candidate(subscription, candidate, settings["mteam_token"], downloader, record)
                    if replacement.get("hash") != pending.get("hash"):
                        downloader.remove(pending["hash"], True)
                    record["pending"] = replacement
                    record["pending"]["upgrade_reason"] = reason
                    record["pending"]["message"] = f"发现更高优先级版本（{reason}），已替换待下载版本。"
                    results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "upgrading", "message": record["pending"]["message"]})
                except PerformerMismatchError as error:
                    results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "skipped", "message": str(error)})
                except MTeamRateLimitError:
                    raise
                except AppError as error:
                    if replacement and replacement.get("hash") != pending.get("hash"):
                        try:
                            downloader.remove(replacement["hash"], True)
                        except AppError:
                            pass
                    pending["message"] = f"更高版本提交失败，当前下载继续：{error}"
            continue
        current = record.get("current")
        if current is None:
            if time.time() < record.get("retry_after", 0):
                log_debug(settings, f"跳过 {code}：仍在失败重试冷却期，剩余约 {int(record['retry_after'] - time.time())} 秒。", subscription["performer"])
                continue
            try:
                record["current"] = submit_candidate(subscription, candidate, settings["mteam_token"], downloader, record)
                record.pop("last_error", None)
                record.pop("retry_after", None)
            except PerformerMismatchError as error:
                results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "skipped", "message": str(error)})
            except MTeamRateLimitError:
                raise
            except AppError as error:
                record["last_error"] = str(error)
                record["retry_after"] = time.time() + 3600
                results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "failed", "message": str(error)})
            continue
        local_items = local_releases.get(code, [])
        installed_ids = {str(item.get("mteam_id", "")) for item in local_items if item.get("mteam_id")}
        candidate_id = str(candidate.get("id", ""))
        same_tracked_id = bool(candidate_id) and str(current.get("id", "")) == candidate_id
        if same_tracked_id and (not local_items or candidate_id in installed_ids):
            log_debug(settings, f"跳过 {code}：M-Team ID {candidate.get('id')} 与当前记录相同，当前状态为 {current.get('state', '未知')}。", subscription["performer"])
            continue
        if same_tracked_id:
            log_debug(settings, f"{code} 的记录 ID {candidate_id} 对应文件已不存在，改按本地现存版本等级比较。", subscription["performer"])
        current_rank = tuple(current.get("rank", [0, 0, 0]))
        local_rank = max((tuple(item["rank"]) for item in local_items), default=(0, 0, 0))
        if local_items and candidate_id not in installed_ids and any(item.get("mteam_id") for item in local_items):
            current_rank = local_rank
        elif wash_rank(local_rank) > wash_rank(current_rank):
            current["rank"] = list(local_rank)
            current_rank = local_rank
        if wash_rank(current_rank) == (0, 0):
            can_replace_unknown_local = (
                current.get("local_existing")
                and subscription.get("wash_enabled", True)
                and wash_rank(candidate["rank"]) > (0, 0)
            )
            if not can_replace_unknown_local:
                results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "upgrade_available", "message": "当前版本等级未知，为避免自动替换成较低版本，已跳过洗版。"})
                continue
        if wash_rank(candidate["rank"]) <= wash_rank(current_rank):
            log_debug(settings, f"跳过 {code}：候选等级 {wash_rank(candidate['rank'])} 未高于当前等级 {wash_rank(current_rank)}。", subscription["performer"])
            continue
        if not subscription.get("wash_enabled", True):
            log_debug(settings, f"跳过 {code}：发现更高等级版本，但该订阅已关闭洗版。", subscription["performer"])
            results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "upgrade_available", "message": "发现更高优先级版本，洗版当前已关闭。"})
            continue
        if time.time() < record.get("retry_after", 0):
            log_debug(settings, f"跳过 {code}：洗版任务仍在失败重试冷却期。", subscription["performer"])
            continue
        try:
            reason = upgrade_reason(current_rank, candidate["rank"])
            record["pending"] = submit_candidate(subscription, candidate, settings["mteam_token"], downloader, record)
            record.pop("retry_after", None)
            record.pop("last_error", None)
            record["pending"]["state"] = "upgrading"
            record["pending"]["upgrade_reason"] = reason
            record["pending"]["message"] = f"洗版原因：{reason}。更高优先级版本正在下载；完成后将清理旧版。"
            results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "upgrading", "message": record["pending"]["message"]})
        except PerformerMismatchError as error:
            results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "skipped", "message": str(error)})
        except MTeamRateLimitError:
            raise
        except AppError as error:
            record["last_error"] = str(error)
            record["retry_after"] = time.time() + 3600
            results.append({"subscription": subscription["performer"], "code": code, "title": candidate["title"], "state": "failed", "message": str(error)})
    own_scan_key = os.path.realpath(subscription["download_dir"])
    own_local_releases = scans_by_directory.get(own_scan_key, {})
    for code, items in own_local_releases.items():
        record = subscription_records.get(code)
        if record:
            _download_existing_local_subtitles(code, items, record, settings["mteam_token"], subscription["performer"])
    context = check_context()
    if context is not None and own_scan_key not in context.scans:
        own_local_releases = scan_local_releases(subscription["download_dir"])
    rows = index_subscription_videos(subscription["id"], own_local_releases, subscription_records)
    collect_subscription_subtitles(rows, settings.get("subtitle_collection_dir", ""), subscription["performer"])


def _run_poll_once(trigger, subscription_id="", refresh_existing_info=False, completed_subscription_ids=None):
    previous_context = check_context()
    previous_downloader = getattr(MTEAM_POLL_CACHE, "downloader", None)
    MTEAM_POLL_CACHE.execution = CheckContext()
    try:
        return _run_poll_once_impl(trigger, subscription_id, refresh_existing_info, completed_subscription_ids)
    finally:
        MTEAM_POLL_CACHE.execution = previous_context
        MTEAM_POLL_CACHE.downloader = previous_downloader


def _run_poll_once_impl(trigger, subscription_id="", refresh_existing_info=False, completed_subscription_ids=None):
    MTEAM_POLL_CACHE.active = {"details": {}, "options": {}}
    if not SCHEDULE_FILE.exists():
        legacy_last_run = scheduler_last_run()
        if legacy_last_run:
            legacy_time = datetime.datetime.fromtimestamp(legacy_last_run, datetime.timezone.utc).isoformat(timespec="seconds")
            write_json(SCHEDULE_FILE, {"last_run_at": legacy_time})
    try:
        read_json(STATUS_FILE, {})
    except AppError as error:
        status = {"state": "failed", "trigger": trigger, "started_at": now_iso(), "completed_at": now_iso(), "last_scan_at": "", "results": [], "message": str(error)}
        write_json(STATUS_FILE, status)
        log_event(str(error), level="error", event="failed")
        MTEAM_POLL_CACHE.active = None
        MTEAM_POLL_CACHE.performer = ""
        return status
    status = {"state": "running", "trigger": trigger, "subscription_id": subscription_id, "refresh_existing_info": bool(refresh_existing_info), "started_at": now_iso(), "completed_at": "", "last_scan_at": "", "results": [], "message": "正在检查 M-Team 订阅。"}
    write_json(STATUS_FILE, status)
    log_event("开始检查订阅。", event="started")
    settings = {}
    subscriptions = []
    records = {}
    local_scan_cache = {}
    local_task_cache = {}
    completed_subscription_ids = set(completed_subscription_ids or ())
    MTEAM_POLL_CACHE.completed_subscription_ids = completed_subscription_ids
    try:
        settings = get_settings()
        subscriptions = list_subscriptions()
        if subscription_id:
            subscriptions = [item for item in subscriptions if item.get("id") == subscription_id]
        records = read_json(RECORDS_FILE, {})
        cleanup_records = [records.get(subscription_id, {})] if subscription_id else records.values()
        for subscription_records in cleanup_records:
            for code, item in list(subscription_records.items()):
                if EPISODE_CODE_RE.fullmatch(str(code)) and not any(
                    release and release.get("hash") for release in (item.get("current"), item.get("pending"))
                ):
                    del subscription_records[code]
        if trigger == "manual":
            retry_records = [records.get(subscription_id, {})] if subscription_id else records.values()
            for subscription_records in retry_records:
                for item in subscription_records.values():
                    if item.get("last_error"):
                        item.pop("retry_after", None)
        cooldown = read_json(MTEAM_LIMIT_FILE, {})
        remaining = int(cooldown.get("retry_at", 0) - time.time())
        if remaining > 0:
            raise MTeamRateLimitError(f"M-Team 限流冷却中，约 {remaining} 秒后可重试。", remaining)
        if not settings.get("mteam_token"):
            raise AppError("请先配置 M-Team 存取令牌。")
        if not subscriptions:
            raise AppError("未找到要检查的订阅。" if subscription_id else "请先添加至少一个女优订阅。")
        context = check_context()
        selected_ids = {item["id"] for item in subscriptions if item["id"] not in completed_subscription_ids}
        tracked_hashes = {
            release["hash"] for identifier in selected_ids
            for item in records.get(identifier, {}).values()
            for release in (item.get("current"), item.get("pending"))
            if release and release.get("hash") and not release.get("local_existing")
            and release.get("state") not in ("failed", "superseded")
        }
        downloader = CheckDownloader(Downloader(settings["downloader"]), context, tracked_hashes)
        MTEAM_POLL_CACHE.downloader = downloader
        local_task_cache = context.task_cache
        for subscription in subscriptions:
            if subscription.get("id") in completed_subscription_ids:
                continue
            context.metrics = {}
            context.stages = {}
            context.wait_seconds = 0.0
            MTEAM_POLL_CACHE.performer = subscription["performer"]
            log_event("开始搜索。", event="subscription_started", subscription=subscription["performer"])
            try:
                if hasattr(downloader, "progress_callback"):
                    downloader.progress_callback = lambda message, event, performer=subscription["performer"]: log_event(message, event=event, subscription=performer)
                subscription_records = records.get(subscription["id"], {})
                for item in subscription_records.values():
                    for release in (item.get("pending"), item.get("current")):
                        if release and release.get("hash"):
                            release["download_dir"] = subscription["download_dir"]
                tracked_hashes = {
                    release.get("hash")
                    for item in subscription_records.values()
                    for release in (item.get("pending"), item.get("current"))
                    if release and release.get("hash") and release.get("state") not in ("completed", "failed", "superseded")
                }
                log_event(f"正在同步下载器任务状态（{len(tracked_hashes)} 个活动任务）。", event="downloader_sync_started", subscription=subscription["performer"])
                poll_downloader_records(downloader, subscription_records, status["results"], records, subscription["performer"], settings["mteam_token"])
                log_event("下载器任务状态同步完成。", event="downloader_sync_completed", subscription=subscription["performer"])
                first_result = len(status["results"])
                process_subscription(subscription, records, settings, downloader, status["results"], refresh_existing_info, local_scan_cache, local_task_cache)
                for item in status["results"][first_result:]:
                    detail = " ".join(part for part in (item.get("code", ""), item.get("state", ""), item.get("title", ""), item.get("message", "")) if part)
                    log_event(detail or "订阅处理完成。", level="error" if item.get("state") in ("failed", "missing", "copy_failed", "cleanup_failed") else "info", event="release", subscription=subscription["performer"])
                write_json(RECORDS_FILE, records)
                log_event("订阅检查完成。", event="subscription_completed", subscription=subscription["performer"])
                completed_subscription_ids.add(subscription["id"])
            except MTeamRequestInterrupted as error:
                write_json(RECORDS_FILE, records)
                status["results"].append({"subscription": subscription["performer"], "state": "interrupted", "message": str(error)})
                status["state"] = "failed"
                status["message"] = str(error)
                log_event(str(error), level="warning", event="request_interrupted", subscription=subscription["performer"])
                break
            except MTeamRateLimitError as error:
                previous_limit = read_json(MTEAM_LIMIT_FILE, {})
                consecutive, retry_after = rate_limit_retry_delay(error, previous_limit)
                write_json(MTEAM_LIMIT_FILE, {"retry_at": time.time() + retry_after, "retry_after": retry_after, "consecutive_rate_limits": consecutive, "message": str(error), "trigger": trigger, "subscription_id": subscription_id, "limited_performer": subscription["performer"], "refresh_existing_info": bool(refresh_existing_info), "completed_subscription_ids": sorted(completed_subscription_ids), "round_id": getattr(MTEAM_POLL_CACHE, "round_id", "")})
                write_json(RECORDS_FILE, records)
                status["results"].append({"subscription": subscription["performer"], "state": "failed", "message": str(error)})
                status["state"] = "failed"
                status["message"] = f"{error} 连续第 {consecutive} 次限流，{retry_after} 秒后继续。"
                log_event(status["message"], level="error", event="rate_limited", subscription=subscription["performer"])
                break
            except AppError as error:
                status["results"].append({"subscription": subscription["performer"], "state": "failed", "message": str(error)})
                log_event(str(error), level="error", event="subscription_failed", subscription=subscription["performer"])
            finally:
                context.report(settings, subscription["performer"])
        if status["state"] != "failed":
            status["state"] = "completed"
            status["message"] = f"检查完成，共处理 {len(status['results'])} 条资源状态。"
    except MTeamRateLimitError as error:
        previous_limit = read_json(MTEAM_LIMIT_FILE, {})
        consecutive, retry_after = rate_limit_retry_delay(error, previous_limit)
        subscription_name = next((item["performer"] for item in list_subscriptions() if item.get("id") == subscription_id), "") if subscription_id else getattr(MTEAM_POLL_CACHE, "performer", "")
        write_json(MTEAM_LIMIT_FILE, {"retry_at": time.time() + retry_after, "retry_after": retry_after, "consecutive_rate_limits": consecutive, "message": str(error), "trigger": trigger, "subscription_id": subscription_id, "limited_performer": subscription_name, "refresh_existing_info": bool(refresh_existing_info), "completed_subscription_ids": sorted(completed_subscription_ids), "round_id": getattr(MTEAM_POLL_CACHE, "round_id", "")})
        status["state"] = "failed"
        status["message"] = f"{error} 连续第 {consecutive} 次限流，{retry_after} 秒后继续。"
        log_event(status["message"], level="error", event="rate_limited", subscription=subscription_name)
    except AppError as error:
        status["state"] = "failed"
        status["message"] = str(error)
        log_event(str(error), level="error", event="failed")
    except Exception as error:
        traceback.print_exc()
        status["state"] = "failed"
        status["message"] = f"检查失败：{error}"
        log_event(status["message"], level="error", event="failed")
    status["completed_at"] = now_iso()
    status["last_scan_at"] = status["completed_at"]
    record_scheduled_run(trigger, status["completed_at"])
    try:
        with file_lock():
            if has_queued_scans():
                status["state"] = "queued"
                status["message"] = "当前检查已完成，后续检查仍在队列中。"
            write_json(STATUS_FILE, status)
    except AppError as error:
        status["state"] = "failed"
        status["message"] = str(error)
        write_json(STATUS_FILE, status)
    log_event(status["message"], level="error" if status["state"] == "failed" else "success", event=status["state"])
    MTEAM_POLL_CACHE.active = None
    MTEAM_POLL_CACHE.performer = ""
    return status


def run_poll(trigger, subscription_id="", refresh_existing_info=False):
    completed_subscription_ids = set()
    round_started = False
    round_id = uuid.uuid4().hex
    while True:
        cooldown = read_json(MTEAM_LIMIT_FILE, {})
        retry_at = float(cooldown.get("retry_at", 0) or 0)
        if retry_at > 0:
            trigger = cooldown.get("trigger", trigger)
            subscription_id = cooldown.get("subscription_id", subscription_id)
            round_id = cooldown.get("round_id") or round_id
            refresh_existing_info = bool(cooldown.get("refresh_existing_info", refresh_existing_info))
            completed_subscription_ids.update(cooldown.get("completed_subscription_ids", []))
        MTEAM_POLL_CACHE.round_id = round_id
        if not round_started:
            log_poll_round_separator("开始", trigger, subscription_id, round_id)
            round_started = True
        remaining = int(retry_at - time.time() + 0.999) if retry_at else 0
        if remaining > 0:
            try:
                status = read_json(STATUS_FILE, {})
            except AppError:
                status = {}
            status.update({
                "state": "waiting_retry", "trigger": trigger, "subscription_id": subscription_id,
                "refresh_existing_info": bool(refresh_existing_info), "retry_at": retry_at,
                "retry_in": remaining, "completed_at": "",
                "message": f"M-Team 限流，{remaining} 秒后自动重新检查。",
            })
            write_json(STATUS_FILE, status)
            log_event(status["message"], event="retry_waiting", subscription=cooldown.get("limited_performer", ""))
            while remaining > 0 and not STOP_SCHEDULER:
                time.sleep(min(5, remaining))
                remaining = int(retry_at - time.time() + 0.999)
            if STOP_SCHEDULER:
                MTEAM_POLL_CACHE.round_id = ""
                return status
            continue

        if completed_subscription_ids:
            log_event(f"限流冷却结束，跳过已完成的 {len(completed_subscription_ids)} 条订阅，继续处理剩余订阅。", event="retry_resuming", subscription=cooldown.get("limited_performer", ""))
        status = _run_poll_once(trigger, subscription_id, refresh_existing_info, completed_subscription_ids)
        cooldown = read_json(MTEAM_LIMIT_FILE, {})
        retry_at = float(cooldown.get("retry_at", 0) or 0)
        if status.get("state") == "failed" and retry_at > time.time():
            continue
        if retry_at:
            MTEAM_LIMIT_FILE.unlink(missing_ok=True)
        log_poll_round_separator("结束 · 失败" if status.get("state") == "failed" else "结束", trigger, subscription_id, round_id)
        MTEAM_POLL_CACHE.round_id = ""
        return status


def run_mteam_id_backfill_request(request):
    subscription_id = request.get("subscription_id", "")
    subscription = next((item for item in list_subscriptions() if item.get("id") == subscription_id), None)
    status = {
        "job_id": request.get("job_id", uuid.uuid4().hex), "subscription_id": subscription_id,
        "performer": subscription.get("performer", "") if subscription else "",
        "state": "running", "phase": "starting", "completed": 0, "total": 0,
        "message": "正在启动 M-Team ID 补全。", "started_at": now_iso(),
    }
    write_mteam_id_backfill_status(status)
    MTEAM_POLL_CACHE.active = {"details": {}, "options": {}}
    MTEAM_POLL_CACHE.performer = status["performer"]
    log_event("开始补全本地视频的 M-Team ID。", event="mteam_id_backfill_started", subscription=status["performer"])

    last_logged_progress = ""

    def update_progress(progress):
        nonlocal last_logged_progress
        status.update(progress)
        status["state"] = "running"
        status["updated_at"] = now_iso()
        write_mteam_id_backfill_status(status)
        if progress.get("phase") != last_logged_progress and progress.get("phase") not in ("matching", "details", "subtitles"):
            log_event(progress.get("message", "正在补全。"), event="mteam_id_backfill_progress", subscription=status["performer"])
            last_logged_progress = progress.get("phase")
        elif progress.get("phase") in ("matching", "details") and progress.get("total") and progress.get("completed") == progress.get("total"):
            log_event(progress.get("message", "补全进度已更新。"), event="mteam_id_backfill_progress", subscription=status["performer"])

    try:
        if subscription is None:
            raise AppError("订阅已删除，无法继续补全 M-Team ID。")
        summary = backfill_subscription_mteam_ids(subscription_id, progress_callback=update_progress)
        status.update({"state": "completed", "phase": "completed", "completed": 1, "total": 1, "summary": summary, "message": "M-Team ID 补全完成。", "completed_at": now_iso()})
        write_mteam_id_backfill_status(status)
        log_event("M-Team ID 补全后台任务完成。", level="success", event="mteam_id_backfill_completed", subscription=status["performer"])
    except Exception as error:
        status.update({"state": "failed", "message": str(error), "error": str(error), "completed_at": now_iso()})
        write_mteam_id_backfill_status(status)
        log_event(f"M-Team ID 补全失败：{error}", level="error", event="mteam_id_backfill_failed", subscription=status["performer"])
    finally:
        MTEAM_POLL_CACHE.active = None
        MTEAM_POLL_CACHE.performer = ""


def recover_interrupted_mteam_id_backfills():
    statuses = read_mteam_id_backfill_statuses()
    interrupted = [item for item in statuses.values() if item.get("state") == "running"]
    if not interrupted:
        return
    with file_lock():
        requests = read_scan_requests()
        for status in interrupted:
            subscription_id = status.get("subscription_id", "")
            if not any(item.get("action") == "mteam_id_backfill" and item.get("subscription_id") == subscription_id for item in requests):
                requests.insert(0, {"action": "mteam_id_backfill", "job_id": status.get("job_id", uuid.uuid4().hex), "subscription_id": subscription_id, "created_at": now_iso()})
            status.update({"state": "queued", "phase": "queued", "message": "后台重启后已恢复补全任务，将从本地扫描重新开始。", "completed": 0, "total": 0})
            write_mteam_id_backfill_status(status)
        write_json(REQUEST_FILE, requests)


def scheduler_last_run():
    try:
        scheduled = read_json(SCHEDULE_FILE, {})
    except AppError as error:
        log_event(str(error), level="error", event="state_error")
        scheduled = {}
    value = scheduled.get("last_run_at")
    if value:
        try:
            return datetime.datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        status = read_json(STATUS_FILE, {})
    except AppError as error:
        log_event(str(error), level="error", event="state_error")
        return 0.0
    value = status.get("last_scan_at") or status.get("completed_at") or status.get("started_at")
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def record_scheduled_run(trigger, timestamp=None):
    if trigger != "scheduled":
        return False
    write_json(SCHEDULE_FILE, {"last_run_at": timestamp or now_iso()})
    return True


def recover_interrupted_poll():
    try:
        status = read_json(STATUS_FILE, {})
    except AppError as error:
        status = {"state": "failed", "results": [], "trigger": "recovery"}
        message = str(error)
    else:
        if status.get("state") != "running":
            return False
        message = "上次检查未正常完成，已恢复调度状态。"
    try:
        has_queued_requests = has_queued_scans()
    except AppError:
        has_queued_requests = False
    timestamp = now_iso()
    status.update({
        "state": "queued" if has_queued_requests else "failed",
        "message": "上次检查已中断，正在继续处理排队任务。" if has_queued_requests else message,
        "completed_at": timestamp,
        "last_scan_at": timestamp,
    })
    record_scheduled_run(status.get("trigger", ""), timestamp)
    write_json(STATUS_FILE, status)
    log_event(status["message"], level="error" if not has_queued_requests else "info", event="scheduler_recovered")
    return True


def scheduler_loop():
    global STOP_SCHEDULER
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "scheduler.pid").write_text(str(os.getpid()), encoding="ascii")
    log_event("后台调度器已启动。", event="scheduler_started")
    signal.signal(signal.SIGTERM, lambda _signum, _frame: _stop())
    signal.signal(signal.SIGINT, lambda _signum, _frame: _stop())
    recover_interrupted_poll()
    recover_interrupted_mteam_id_backfills()
    last_run = scheduler_last_run()
    while not STOP_SCHEDULER and not DISABLED_FILE.exists():
        try:
            cooldown = read_json(MTEAM_LIMIT_FILE, {})
            retry_pending = bool(cooldown.get("retry_at"))
            request = {} if retry_pending else pop_scan_request()
            settings = get_settings()
            due = settings.get("auto_scan", True) and time.time() - last_run >= settings.get("poll_interval_minutes", 60) * 60
            if retry_pending:
                run_trigger = cooldown.get("trigger", "scheduled")
                run_poll(
                    run_trigger, cooldown.get("subscription_id", ""),
                    cooldown.get("refresh_existing_info", False),
                )
                if run_trigger == "scheduled":
                    last_run = time.time()
            elif request and request.get("action") == "mteam_id_backfill":
                run_mteam_id_backfill_request(request)
            elif request or due:
                run_trigger = request.get("trigger", "scheduled")
                run_poll(run_trigger, request.get("subscription_id", ""), request.get("refresh_existing_info", False))
                if run_trigger == "scheduled":
                    last_run = time.time()
        except Exception as error:
            message = f"后台调度器执行异常：{error}"
            try:
                current = read_json(STATUS_FILE, {})
            except AppError:
                current = {}
            current.update({"state": "failed", "message": message, "completed_at": now_iso()})
            write_json(STATUS_FILE, current)
            log_event(message, level="error", event="scheduler_error")
            traceback.print_exc()
            time.sleep(5)
        time.sleep(2)
    (DATA_ROOT / "scheduler.pid").unlink(missing_ok=True)


def _stop():
    global STOP_SCHEDULER
    STOP_SCHEDULER = True


def read_body():
    try:
        length = int(os.environ.get("CONTENT_LENGTH", "0"))
        if length > 1024 * 1024:
            raise AppError("请求内容超过限制。")
        return json.loads(sys.stdin.buffer.read(length) or b"{}")
    except (ValueError, json.JSONDecodeError) as error:
        raise AppError("请求内容不是有效 JSON。") from error


def request_path():
    path = os.environ.get("PATH_INFO", "") or "/"
    if "index.cgi" in path:
        path = path.split("index.cgi", 1)[1] or "/"
    return path or "/"


def emit(status, content_type, body):
    sys.stdout.write(f"Status: {status}\r\nContent-Type: {content_type}\r\nCache-Control: no-store\r\n\r\n")
    if isinstance(body, bytes):
        sys.stdout.flush()
        sys.stdout.buffer.write(body)
    else:
        sys.stdout.write(body)


def emit_json(status, value):
    emit(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False))


def handle_request():
    path = request_path()
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    try:
        if path == "/api/settings" and method == "GET":
            result = public_settings()
        elif path == "/api/settings" and method == "POST":
            result = save_settings(read_body())
        elif path == "/api/subscriptions" and method == "GET":
            result = subscriptions_with_video_counts()
        elif path == "/api/subscriptions" and method == "POST":
            result = create_subscription(read_body())
        elif path.startswith("/api/subscriptions/") and path.endswith("/scan") and method == "POST":
            subscription_id = path.removeprefix("/api/subscriptions/").removesuffix("/scan").strip("/")
            payload = read_body()
            result = queue_scan("manual", subscription_id, payload.get("refresh_existing_info", False))
        elif path.startswith("/api/subscriptions/") and path.endswith("/mteam-id-backfill") and method in ("GET", "POST"):
            subscription_id = path.removeprefix("/api/subscriptions/").removesuffix("/mteam-id-backfill").strip("/")
            result = queue_mteam_id_backfill(subscription_id) if method == "POST" else get_mteam_id_backfill_status(subscription_id)
        elif path.startswith("/api/subscriptions/") and path.endswith("/videos/refresh") and method == "POST":
            subscription_id = path.removeprefix("/api/subscriptions/").removesuffix("/videos/refresh").strip("/")
            result = refresh_subscription_videos(subscription_id)
        elif path.startswith("/api/subscriptions/") and "/videos/" in path and path.endswith("/file-info") and method == "POST":
            subscription_id, code = path.removeprefix("/api/subscriptions/").removesuffix("/file-info").split("/videos/", 1)
            payload = read_body()
            result = get_subscription_video_file_info(subscription_id, code, payload.get("path", ""))
        elif path.startswith("/api/subscriptions/") and "/videos/" in path and path.endswith("/refresh") and method == "POST":
            subscription_id, code = path.removeprefix("/api/subscriptions/").removesuffix("/refresh").split("/videos/", 1)
            payload = read_body()
            result = refresh_subscription_video_file(subscription_id, code, payload.get("path", ""))
        elif path.startswith("/api/subscriptions/") and "/videos/" in path and path.endswith("/subtitle") and method == "GET":
            subscription_id, code = path.removeprefix("/api/subscriptions/").removesuffix("/subtitle").split("/videos/", 1)
            result = get_subscription_video_subtitle(subscription_id, code)
        elif path.startswith("/api/subscriptions/") and path.endswith("/videos") and method == "GET":
            subscription_id = path.removeprefix("/api/subscriptions/").removesuffix("/videos").strip("/")
            result = get_subscription_videos(subscription_id)
        elif path.startswith("/api/subscriptions/"):
            subscription_id = path.removeprefix("/api/subscriptions/").strip("/")
            if method == "PUT":
                result = update_subscription(subscription_id, read_body())
            elif method == "DELETE":
                result = delete_subscription(subscription_id)
            else:
                raise AppError("订阅接口方法无效。")
        elif path == "/api/scan" and method == "POST":
            payload = read_body()
            result = queue_scan("manual", refresh_existing_info=payload.get("refresh_existing_info", False))
        elif path == "/api/status" and method == "GET":
            result = get_status()
        elif path == "/api/activity" and method == "GET":
            result = get_activity()
        elif path == "/api/logs" and method == "GET":
            query = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
            after_id = query.get("after_id", [None])[0]
            result = get_logs(after_id=after_id)
        elif path == "/api/logs" and method == "DELETE":
            subscription = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", "")).get("subscription", [None])[0]
            result = clear_logs(subscription)
        elif path == "/api/downloader/test" and method == "POST":
            config = get_settings()["downloader"]
            Downloader(config).test()
            result = {"ok": True, "message": "下载器连接成功。"}
        elif path == "/api/mteam/test" and method == "POST":
            token = get_settings().get("mteam_token", "")
            if not token:
                raise AppError("请先保存 M-Team 令牌。")
            mteam_json("/torrent/search", token, {"mode": "adult", "categories": [], "visible": 1, "pageNumber": 1, "pageSize": 1, "keyword": "test"})
            result = {"ok": True, "message": "M-Team 令牌有效。"}
        elif not path.startswith("/api/"):
            relative = path.removeprefix("/") or "index.html"
            target = (WEB_ROOT / relative).resolve()
            try:
                target.relative_to(WEB_ROOT.resolve())
            except ValueError:
                emit("400 Bad Request", "text/plain; charset=utf-8", "Bad Request")
                return
            if not target.is_file():
                emit("404 Not Found", "text/plain; charset=utf-8", "Not Found")
                return
            media_types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}
            emit("200 OK", media_types.get(target.suffix, "application/octet-stream"), target.read_bytes())
            return
        else:
            emit_json("404 Not Found", {"error": "找不到该接口。"})
            return
        emit_json("200 OK", result)
    except AppError as error:
        emit_json("400 Bad Request", {"error": str(error)})
    except (OSError, urllib.error.URLError, RuntimeError) as error:
        emit_json("500 Internal Server Error", {"error": f"处理失败：{error}"})


if __name__ == "__main__":
    if sys.argv[1:] == ["--scheduler"]:
        scheduler_loop()
    else:
        handle_request()
