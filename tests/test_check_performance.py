import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bin import app


class SnapshotDownloader:
    def __init__(self, tasks=None):
        self.tasks = tasks or {}
        self.queries = []
        self.mutations = []
        self.progress_callback = None

    def list_tasks(self, hashes=None):
        self.queries.append(None if hashes is None else set(hashes))
        return {key: dict(value) for key, value in self.tasks.items() if hashes is None or key in hashes}

    def add(self, data, title, directory):
        self.mutations.append(("add", directory))
        return "addedhash"

    def remove(self, info_hash, delete_data=True):
        self.mutations.append(("remove", info_hash))
        self.tasks.pop(info_hash, None)

    def recheck(self, info_hash, directory=""):
        self.mutations.append(("recheck", directory))
        return directory


class CheckPerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.object(app, "DATA_ROOT", self.root))
        for name in ("SETTINGS_FILE", "SUBSCRIPTIONS_FILE", "RECORDS_FILE", "STATUS_FILE", "SCHEDULE_FILE",
                     "LOG_FILE", "REQUEST_FILE", "LOCK_FILE", "LOG_LOCK_FILE", "MTEAM_LIMIT_FILE",
                     "MTEAM_REQUEST_STATE_FILE", "MTEAM_REQUEST_LOCK_FILE"):
            self.enterContext(patch.object(app, name, self.root / f"{name.lower()}.json"))
        self.enterContext(patch.object(app, "LOCAL_VIDEO_DATABASE", self.root / "videos.sqlite3"))
        self.enterContext(patch.object(app, "LOG_DATABASE_FILE", self.root / "logs.sqlite3"))
        self.context = app.CheckContext()
        self.enterContext(patch.object(app.MTEAM_POLL_CACHE, "execution", self.context, create=True))
        self.enterContext(patch.object(app.MTEAM_POLL_CACHE, "active", None, create=True))
        self.enterContext(patch.object(app.MTEAM_POLL_CACHE, "downloader", None, create=True))

    def test_hash_queries_are_batched_and_empty_set_never_queries_all(self):
        hashes = {f"{index:040x}" for index in range(205)}
        source = SnapshotDownloader({key: {"complete": False} for key in hashes})
        downloader = app.CheckDownloader(source, self.context, hashes)
        self.assertEqual(downloader.list_tasks([]), {})
        self.assertEqual(source.queries, [])
        for info_hash in hashes:
            self.assertIn(info_hash, downloader.list_tasks([info_hash]))
        self.assertEqual(len(source.queries), 3)
        self.assertEqual(sorted(map(len, source.queries)), [5, 100, 100])
        self.assertEqual(set().union(*source.queries), hashes)

    def test_downloader_cache_logs_real_json_fields_without_exposing_secrets(self):
        app.write_json(app.SETTINGS_FILE, {"debug_mode": True})
        source = app.Downloader({"url": "http://qb.test", "type": "qbittorrent"})
        source.cookies["SID"] = "private-session"
        downloader = app.CheckDownloader(source, self.context)
        tasks = [{"hash": "one", "name": "ABC-123", "size": 100, "magnet_uri": "private-magnet"}]
        files = [{"name": "ABC-123.mp4", "size": 100}]
        properties = {"comment": "Copied private-session"}
        trackers = [{"url": "https://tracker.test/passkey", "status": 2}]
        with patch.object(source, "_json", side_effect=[(value, None) for value in (tasks, files, properties, trackers)]) as request:
            for _ in range(2):
                self.assertIn("one", downloader.list_tasks(["one"]))
                self.assertEqual(downloader.list_task_files("one"), files)
                self.assertEqual(downloader.task_mteam_metadata("one"), (properties, trackers))
        self.assertEqual(request.call_count, 4)
        logs = [entry["message"] for entry in app.get_logs() if entry["event"] == "debug_http_response"]
        self.assertEqual(len(logs), 4)
        self.assertEqual([json.loads(message.split("\n", 2)[2]) for message in logs], [
            [{**tasks[0], "magnet_uri": "[已隐藏]"}], files, {"comment": "Copied [已隐藏]"},
            [{"url": "[已隐藏]", "status": 2}],
        ])

    def test_sync_processes_large_batch_from_one_snapshot(self):
        hashes = {f"{index:040x}" for index in range(205)}
        source = SnapshotDownloader({key: {"progress": .25} for key in hashes})
        downloader = app.CheckDownloader(source, self.context, hashes)
        records = {key: {"current": {"hash": key, "state": "downloading"}} for key in hashes}
        with patch.object(downloader, "list_tasks", wraps=downloader.list_tasks) as snapshots:
            results = []
            app.poll_downloader_records(downloader, records, results)
        snapshots.assert_called_once_with(hashes)
        self.assertEqual(len(results), 205)
        self.assertEqual(sorted(map(len, source.queries)), [5, 100, 100])

    def test_submission_checks_local_file_changes_during_request_cooldown(self):
        folder = self.root / "videos"
        folder.mkdir()
        app.scan_local_releases(str(folder))
        subscription = {"performer": "Actress", "download_dir": str(folder)}
        candidate = {"id": "10001", "code": "ABC-123", "title": "ABC-123", "rank": [1, 2, 1]}
        downloader = SnapshotDownloader()

        def download_after_wait(*_):
            (folder / "ABC-123.mp4").touch()
            self.context.resume_after_wait()
            return b"torrent"

        with patch.object(app, "mteam_detail", return_value={"performer": "Actress"}), \
             patch.object(app, "fetch_torrent", side_effect=download_after_wait):
            with self.assertRaisesRegex(app.AppError, "限流等待期间发生变化"):
                app.submit_candidate(subscription, candidate, "token", downloader, None)
        self.assertEqual(downloader.mutations, [])

    def test_clean_scan_and_unchanged_index_share_enumeration_and_skip_writes(self):
        directory = self.root / "videos"
        directory.mkdir()
        video = directory / "ABC-123.mp4"
        video.touch()
        (directory / "ABC-123.srt").touch()
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "sub", "download_dir": str(directory)}])
        releases = app.scan_local_releases(str(directory))
        self.assertEqual(app.cleanup_orphan_sidecars(str(directory)), ([], []))
        app.index_subscription_videos("sub", releases, {})
        self.assertEqual(self.context.metrics["directory_walks"], 1)
        self.assertEqual(self.context.metrics["index_writes"], 2)
        app.index_subscription_videos("sub", releases, {})
        self.assertEqual(self.context.metrics["index_writes"], 2)
        video.write_bytes(b"new signature")
        app.index_subscription_videos("sub", releases, {})
        self.assertEqual(self.context.metrics["index_writes"], 3)
        video.unlink()
        app.index_subscription_videos("sub", {}, {})
        self.assertEqual(self.context.metrics["index_writes"], 5)

    def test_snapshot_ttl_refreshes_existing_and_missing_hashes(self):
        source = SnapshotDownloader({"one": {"progress": .1}})
        downloader = app.CheckDownloader(source, self.context, {"one", "missing"})
        clock = [100.0]
        with patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(downloader.list_tasks(["one"])["one"]["progress"], .1)
            self.assertEqual(downloader.list_tasks(["missing"]), {})
            source.tasks["one"]["progress"] = .9
            clock[0] = 114.9
            self.assertEqual(downloader.list_tasks(["one"])["one"]["progress"], .1)
            clock[0] = 115
            self.assertEqual(downloader.list_tasks(["one"])["one"]["progress"], .9)
        self.assertEqual(len(source.queries), 2)

    def test_full_snapshot_is_on_demand_and_expires_after_fifteen_seconds(self):
        source = SnapshotDownloader({"one": {"progress": .1}, "two": {"progress": .2}})
        downloader = app.CheckDownloader(source, self.context, {"one"})
        clock = [100.0]
        with patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
            downloader.list_tasks(["one"])
            self.assertEqual(source.queries, [{"one"}])
            downloader.list_tasks()
            downloader.list_tasks(["two"])
            self.assertEqual(source.queries, [{"one"}, None])
            clock[0] = 115.0
            downloader.list_tasks()
            self.assertEqual(source.queries, [{"one"}, None, None])

    def test_mutations_invalidate_task_and_related_directory_caches_even_on_failure(self):
        source = SnapshotDownloader({"one": {"content_path": str(self.root / "videos" / "SSNI-618.mp4")}})
        downloader = app.CheckDownloader(source, self.context)
        key = str((self.root / "videos").resolve())
        unrelated = str((self.root / "other").resolve())
        for action in ("add", "remove", "recheck", "failed_remove"):
            with self.subTest(action=action):
                source.tasks["one"] = {"content_path": str(self.root / "videos" / "SSNI-618.mp4")}
                downloader.list_tasks()
                self.context.scans[key] = {}
                self.context.scans[unrelated] = {}
                self.context.task_cache["id_file_lists"] = {"one": []}
                if action == "add":
                    downloader.add(b"torrent", "name", key)
                elif action == "remove":
                    downloader.remove("one", False)
                elif action == "recheck":
                    downloader.recheck("one", key)
                else:
                    with patch.object(source, "remove", side_effect=app.AppError("response lost")):
                        with self.assertRaises(app.AppError):
                            downloader.remove("one")
                self.assertNotIn(key, self.context.scans)
                self.assertIn(unrelated, self.context.scans)
                self.assertEqual(self.context.task_cache, {})
                self.assertEqual(downloader.known, {})

    def test_directory_scan_is_shared_and_results_are_not_mutable_cache_aliases(self):
        directory = self.root / "videos"
        directory.mkdir()
        video = directory / "SSNI-618 1080p.mp4"
        video.touch()
        video.with_suffix(".nfo").write_text("<movie><video><width>1920</width><height>1080</height></video></movie>", encoding="utf-8")
        with patch.object(app, "_scan_local_releases_uncached", wraps=app._scan_local_releases_uncached) as scan:
            first = app.scan_local_releases(str(directory))
            first["SSNI-618"][0]["rank"][0] = 99
            self.assertEqual(app.find_local_releases(str(directory), "SSNI-618")[0]["rank"][0], 1)
            app.scan_local_releases(str(directory))
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(self.context.metrics["scan_hits"], 2)

    def test_hardlink_and_video_deletion_invalidate_the_scanned_directory(self):
        source_dir, target_dir = self.root / "source", self.root / "target"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "SSNI-618 1080p.mp4"
        source.touch()
        self.assertEqual(app.scan_local_releases(str(target_dir)), {})
        linked = app.link_local_release_to_subscription(str(source), str(target_dir), "SSNI-618")
        self.assertEqual(len(app.scan_local_releases(str(target_dir))["SSNI-618"]), 1)
        app.remove_local_releases([linked["path"]], str(target_dir), "SSNI-618")
        self.assertEqual(app.scan_local_releases(str(target_dir)), {})
        self.assertTrue(source.exists())

    def test_subtitle_download_invalidates_scan_and_new_subtitle_reaches_index(self):
        directory = self.root / "videos"
        directory.mkdir()
        video = directory / "SSNI-618.mp4"
        video.touch()
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "one"}])
        before = app.scan_local_releases(str(directory))
        self.assertFalse(before["SSNI-618"][0]["metadata"]["subtitle_available"])
        listing = {"code": 0, "data": {"data": [{"id": 11, "torrentId": 1283711, "lang": "繁體中文", "hits": 10}]}}
        link = {"code": 0, "data": {"credential": "subtitle-token"}}
        content = b"1\n00:00:00,000 --> 00:00:01,000\nsubtitle"
        with patch.object(app, "mteam_json", side_effect=[listing, link]), patch.object(app, "mteam_request", return_value=(content, {"content_type": "application/x-subrip"})):
            app.mteam_subtitle_download("SSNI-618", "1283711", [video], "token")
        after = app.scan_local_releases(str(directory))
        self.assertTrue(after["SSNI-618"][0]["metadata"]["subtitle_available"])
        rows = app.index_subscription_videos("one", after, {})
        self.assertEqual(rows[0]["files"][0]["subtitle_paths"], [str(video.with_name("SSNI-618.MT.zh-HK.srt"))])

    def test_sync_and_processing_share_one_directory_traversal(self):
        directory = self.root / "videos"
        directory.mkdir()
        video = directory / "SSNI-618 1080p 内嵌中文 x265.mp4"
        video.touch()
        subscription = {"id": "one", "performer": "Actress", "download_dir": str(directory)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])
        records = {"one": {"SSNI-618": {"code": "SSNI-618", "current": {"hash": "one", "id": "1283711", "state": "completed", "download_dir": str(directory), "rank": [1, 2, 2]}}}}
        downloader = app.CheckDownloader(SnapshotDownloader({"one": {"complete": True, "progress": 1}}), self.context, {"one"})
        with patch.object(app, "_scan_local_releases_uncached", wraps=app._scan_local_releases_uncached) as scan, patch.object(app, "mteam_search", return_value=[]):
            app.poll_downloader_records(downloader, records["one"], [], records)
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        self.assertEqual(scan.call_count, 1)

    def test_single_number_lookup_copies_only_the_requested_scan_items(self):
        directory = self.root / "videos"
        key = str(directory.resolve())
        items = [{"path": str(directory / "ABC-123.mp4"), "rank": [1, 0, 1]}]
        self.context.scans[key] = {"ABC-123": items, "DEF-456": [{"rank": [2, 1, 2]}]}
        with patch.object(app.copy, "deepcopy", wraps=app.copy.deepcopy) as clone:
            result = app.find_local_releases(str(directory), "ABC-123")
        clone.assert_called_once_with(items)
        result[0]["rank"][0] = 99
        self.assertEqual(items[0]["rank"], [1, 0, 1])

    def test_subtitle_listing_is_reused_per_round_but_refetched_next_round(self):
        listing = {"code": 0, "data": {"data": []}}
        with patch.object(app, "mteam_json", return_value=listing) as request:
            for _ in range(2):
                app.MTEAM_POLL_CACHE.active = {}
                for name in ("ABC-123 one.mp4", "ABC-123 two.mp4"):
                    result = app.mteam_subtitle_download("ABC-123", "123456", [self.root / name], "token")
                    self.assertEqual(result["status"], "not_found")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.context.metrics["subtitle_list_hits"], 2)

    def test_large_path_matching_uses_one_overlap_lookup_per_task(self):
        targets = {}
        for index in range(1000):
            path = (self.root / "videos" / f"ABC-{index + 1000}.mp4").resolve()
            targets[str(path)] = {"path": path, "code": f"ABC-{index + 1000}"}
        tasks = {str(index): {"name": "unrelated", "content_path": str(self.root / "elsewhere" / str(index))} for index in range(1000)}
        source = SnapshotDownloader(tasks)
        results = app.match_qbittorrent_file_ids(targets, source)
        self.assertEqual(results[:3], ({}, {}, set()))
        self.assertEqual(self.context.metrics["path_checks"], 1000)
        self.assertEqual(len(source.queries), 1)
        self.assertLess(self.context.metrics["path_checks"], len(targets) * len(tasks))

    def test_limit_resume_invalidates_disk_and_downloader_snapshots(self):
        self.context.scans["/videos"] = {}
        source = SnapshotDownloader({"one": {"progress": .1}})
        downloader = app.CheckDownloader(source, self.context)
        downloader.list_tasks()
        app.MTEAM_POLL_CACHE.downloader = downloader
        app.write_json(app.STATUS_FILE, {"state": "running"})
        clock = [1000.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(app, "STOP_SCHEDULER", False), patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=advance):
            app.wait_for_mteam_retry(app.MTeamRateLimitError("limit", 60), "/torrent/detail", 1)
        self.assertEqual(self.context.scans, {})
        self.assertEqual(self.context.generation, 1)
        self.assertEqual(downloader.known, {})
        downloader.list_tasks()
        self.assertEqual(len(source.queries), 2)

    def test_performance_summary_is_debug_only(self):
        self.context.count("scan_count")
        self.context.stages["scan"] = .1
        self.context.wait_seconds = 900
        self.context.report({}, "Actress")
        self.assertEqual(app.get_logs(), [])
        self.context.report({"debug_mode": True}, "Actress")
        entry = app.get_logs()[0]
        self.assertEqual(entry["event"], "debug_performance")
        self.assertIn("目录扫描 0.10秒", entry["message"])
        self.assertIn("限流等待 900.00秒", entry["message"])

    def test_global_check_batches_subscriptions_but_single_check_only_fetches_own_hashes(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://qb"}})
        subscriptions = [{"id": key, "performer": key, "download_dir": str(self.root / key)} for key in ("one", "two")]
        for subscription in subscriptions:
            Path(subscription["download_dir"]).mkdir()
        app.write_json(app.SUBSCRIPTIONS_FILE, subscriptions)
        records = {key: {f"ABC-{index + 100}": {"code": f"ABC-{index + 100}", "current": {"hash": key, "state": "downloading"}}} for index, key in enumerate(("one", "two"))}
        source = SnapshotDownloader({key: {"progress": .5, "complete": False} for key in ("one", "two")})
        with patch.object(app, "Downloader", return_value=source), patch.object(app, "mteam_search", return_value=[]):
            app.write_json(app.RECORDS_FILE, records)
            self.assertEqual(app.run_poll("manual")["state"], "completed")
            self.assertEqual(source.queries, [{"one", "two"}])
            source.queries.clear()
            app.write_json(app.RECORDS_FILE, records)
            self.assertEqual(app.run_poll("manual", "two")["state"], "completed")
            self.assertEqual(source.queries, [{"two"}])


if __name__ == "__main__":
    unittest.main()
