import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.bin import app


class RepairDownloader:
    def __init__(self, tasks=None, files=None, ids=None):
        self.tasks = tasks or {}
        self.files = files or {}
        self.ids = ids or {}
        self.queries = 0
        self.file_queries = []

    def list_tasks(self, hashes=None):
        self.queries += 1
        return self.tasks

    def list_task_files(self, info_hash):
        self.file_queries.append(info_hash)
        return self.files[info_hash]

    def task_mteam_metadata(self, info_hash):
        value = self.ids[info_hash]
        return {"comment": value[0]}, [{"url": value[1]}]


class DataRepairTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("DATA_ROOT", "SETTINGS_FILE", "SUBSCRIPTIONS_FILE", "RECORDS_FILE", "STATUS_FILE", "SCHEDULE_FILE", "LOG_FILE", "REQUEST_FILE", "LOCK_FILE", "DISABLED_FILE", "MTEAM_LIMIT_FILE", "MTEAM_REQUEST_STATE_FILE", "MTEAM_REQUEST_LOCK_FILE", "LOG_DATABASE_FILE", "LOCAL_VIDEO_DATABASE"):
            value = self.root if name == "DATA_ROOT" else self.root / name.lower()
            self.enterContext(patch.object(app, name, value))
        self.enterContext(patch.object(app, "STOP_SCHEDULER", False))
        self.enterContext(patch.object(app.cover_images, "fetch", side_effect=OSError("模拟图片不可用")))
        for name in ("execution", "active", "performer", "repair_job_id"):
            self.enterContext(patch.object(app.MTEAM_POLL_CACHE, name, None, create=True))
        self.videos = self.root / "videos"
        self.videos.mkdir()
        self.subscription = {"id": "sub", "performer": "Actress", "download_dir": str(self.videos)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [self.subscription])
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"url": "http://qb", "type": "qbittorrent"}})

    def video(self, name="ABC-123.mp4", identifier="", extra=None, archived=True):
        path = (self.videos / app.normalize_code(name) if archived else self.videos) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        records = app.read_json(app.RECORDS_FILE, {})
        record = records.setdefault("sub", {}).setdefault(app.normalize_code(name), {"code": app.normalize_code(name)})
        if identifier or extra:
            record.setdefault("local_files", {})[str(path)] = {"id": identifier, **(extra or {})}
        app.write_json(app.RECORDS_FILE, records)
        return path

    def detail(self, chinese=1, mediainfo=True):
        return {"name": "ABC-123 Actress", "labelsNew": ["中字"] if chinese == 2 else [],
            "hasChineseSubtitle": chinese == 1, "resolution": "2160p", "videoCodec": "HEVC",
            **({"mediainfo": "Video\nFormat : AVC\nHeight : 1 080 pixels\nWidth : 1 920 pixels"} if mediainfo else {})}

    def matched_downloader(self, pairs):
        return RepairDownloader(
            {info_hash: {"name": path.name, "save_path": str(self.videos), "content_path": str(path)} for info_hash, path, _id in pairs},
            {info_hash: [{"name": path.relative_to(self.videos).as_posix()}] for info_hash, path, _id in pairs},
            {info_hash: (identifier, "https://bt1.manfuz.co/announce") for info_hash, _path, identifier in pairs},
        )

    def run_job(self, downloader=None, path="", code=""):
        queued = app.queue_data_repair("sub", str(path), code)
        request = app.pop_scan_request()
        self.assertEqual(request["action"], "repair_data")
        app.run_data_repair_request(request, downloader or RepairDownloader())
        return app.read_repair_jobs()[queued["job_id"]]

    def saved(self, path):
        return app.read_json(app.RECORDS_FILE, {})["sub"][app.normalize_code(path.name)]["local_files"][str(path)]

    def test_subscription_repair_archives_legacy_video_and_matching_sidecars(self):
        old = self.video("SNIS-872.mp4", "22999", archived=False)
        old.with_suffix(".nfo").write_text("<movie><title>Old title</title></movie>", encoding="utf-8")
        subtitle = old.with_name("SNIS-872.MT.zh-HK.srt")
        subtitle.write_text("captions", encoding="utf-8")
        unrelated = old.with_name("ABC-123.txt")
        unrelated.touch()
        signature = old.stat()
        with patch.object(app, "mteam_detail", return_value=self.detail(2)):
            job = self.run_job()
        self.assertEqual(job["summary"]["archived"], 1)
        target = self.videos / "SNIS-872" / old.name
        self.assertFalse(old.exists())
        self.assertEqual(target.stat().st_ino, signature.st_ino)
        self.assertTrue(target.with_suffix(".nfo").is_file())
        self.assertTrue(target.with_name(subtitle.name).is_file())
        self.assertTrue(unrelated.is_file())
        self.assertEqual(self.saved(target)["id"], "22999")

    def test_subscription_repair_moves_qbittorrent_data_via_downloader(self):
        old = self.video("SNIS-872.mp4", "22999", archived=False)
        downloader = self.matched_downloader([("one", old, "22999")])
        downloader.tasks["one"]["complete"] = True
        calls = []

        def relocate(info_hash, location):
            calls.append((info_hash, location))
            destination = Path(location)
            destination.mkdir()
            new = old.rename(destination / old.name)
            downloader.tasks[info_hash].update(save_path=location, content_path=str(new))
            downloader.files[info_hash] = [{"name": new.name}]

        downloader.set_location = relocate
        with patch.object(app, "mteam_detail", return_value=self.detail(2)):
            job = self.run_job(downloader)
        self.assertEqual(job["summary"]["archived"], 1)
        self.assertEqual(calls, [("one", str(self.videos / "SNIS-872"))])
        self.assertTrue((self.videos / "SNIS-872" / old.name).is_file())
        self.assertFalse(old.exists())

    def test_archive_preserves_both_versions_on_collision(self):
        old = self.video("SNIS-872.mp4", "22999", archived=False)
        destination = self.video("SNIS-872.mp4", "23000")
        destination.write_bytes(b"other")
        with patch.object(app, "mteam_detail", return_value=self.detail(2)):
            job = self.run_job()
        self.assertEqual(job["summary"]["archive_failed"], 1)
        self.assertEqual(old.read_bytes(), b"video")
        self.assertEqual(destination.read_bytes(), b"other")

    def test_all_ids_are_corrected_before_details_and_each_id_is_fetched_once(self):
        first = self.video("ABC-123 one.mp4", "11111", {"mediainfo": "old", "subtitle_check": {"state": "not_found"}})
        second = self.video("ABC-123 two.mp4", "33333")
        downloader = self.matched_downloader([("one", first, "22222"), ("two", second, "22222")])
        def fetch(identifier, token, force_refresh=False):
            self.assertEqual(self.saved(first)["id"], "22222")
            self.assertEqual(self.saved(second)["id"], "22222")
            self.assertTrue(force_refresh)
            return self.detail(2)
        with patch.object(app, "mteam_detail", side_effect=fetch) as request, patch.object(app, "mteam_subtitle_download") as subtitles:
            job = self.run_job(downloader)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["summary"]["corrected"], 2)
        self.assertEqual(job["summary"]["refreshed"], 2)
        self.assertEqual(downloader.queries, 1)
        self.assertEqual(request.call_count, 1)
        subtitles.assert_not_called()
        self.assertEqual(self.saved(first)["rank"], [1, 2, 1])
        self.assertNotIn("subtitle_check", self.saved(first))
        events = [entry["event"] for entry in app.get_logs() if entry["subscription"] == "Actress"]
        self.assertLess(events.index("repair_started"), events.index("repair_cooldown_check"))
        self.assertLess(events.index("repair_cooldown_check"), events.index("repair_scan_started"))
        self.assertIn("repair_scan_completed", events)
        self.assertIn("repair_matching_started", events)
        self.assertIn("repair_matching_completed", events)

    def test_single_file_without_id_repairs_only_exact_path(self):
        first = self.video("ABC-123 one.mp4")
        second = self.video("ABC-123 two.mp4", "88888", {"mediainfo": "unchanged"})
        previous = copy.deepcopy(self.saved(second))
        downloader = self.matched_downloader([("one", first, "22222"), ("two", second, "33333")])
        with patch.object(app, "mteam_detail", return_value=self.detail(2)) as details:
            job = self.run_job(downloader, first, "ABC-123")
        self.assertEqual(job["summary"]["total"], 1)
        self.assertEqual(self.saved(first)["id"], "22222")
        self.assertEqual(self.saved(second), previous)
        details.assert_called_once_with("22222", "token", force_refresh=True)

    def test_unmatched_existing_file_id_refreshes_and_missing_id_is_skipped(self):
        first = self.video("ABC-123 one.mp4", "22222")
        second = self.video("ABC-123 two.mp4")
        with patch.object(app, "mteam_detail", return_value=self.detail(2)) as details:
            job = self.run_job()
        self.assertEqual(job["summary"]["refreshed"], 1)
        self.assertEqual(job["summary"]["skipped"], 1)
        self.assertEqual(self.saved(first)["id"], "22222")
        details.assert_called_once()
        self.assertIn(str(second), job["outcomes"])

    def test_number_level_or_pending_id_is_not_used(self):
        path = self.video()
        records = app.read_json(app.RECORDS_FILE, {})
        records["sub"]["ABC-123"].update({"mteam_metadata": {"id": "22222"}, "pending": {"id": "33333"}})
        app.write_json(app.RECORDS_FILE, records)
        with patch.object(app, "mteam_detail") as details:
            job = self.run_job()
        self.assertEqual(job["summary"]["skipped"], 1)
        details.assert_not_called()

    def test_untrusted_tracker_or_wrong_file_path_never_overwrites_id(self):
        path = self.video(identifier="22222")
        downloader = self.matched_downloader([("bad", path, "33333"), ("wrong", path, "44444")])
        downloader.ids["bad"] = ("33333", "https://tracker.other/announce")
        downloader.files["wrong"] = [{"name": "elsewhere/ABC-123.mp4"}]
        with patch.object(app, "mteam_detail", return_value=self.detail(2)):
            self.run_job(downloader)
        self.assertEqual(self.saved(path)["id"], "22222")

    def test_conflicting_ids_keep_old_data_and_skip_details(self):
        path = self.video(identifier="11111", extra={"mediainfo": "old"})
        previous = self.saved(path)
        downloader = self.matched_downloader([("one", path, "22222"), ("two", path, "33333")])
        with patch.object(app, "mteam_detail") as details:
            job = self.run_job(downloader)
        self.assertEqual(self.saved(path), previous)
        self.assertEqual(job["summary"]["skipped"], 1)
        details.assert_not_called()

    def test_downloader_failure_keeps_existing_id_and_refreshes_metadata(self):
        path = self.video(identifier="22222", extra={"mediainfo": "outdated"})
        downloader = RepairDownloader()
        with patch.object(downloader, "list_tasks", side_effect=app.AppError("offline")), patch.object(app, "mteam_detail", return_value=self.detail(2, False)):
            job = self.run_job(downloader)
        self.assertEqual(job["summary"]["refreshed"], 1)
        self.assertTrue(job["errors"])
        self.assertEqual(self.saved(path)["mediainfo"], "")
        self.assertTrue(self.saved(path)["mediainfo_checked"])

    def test_detail_failure_preserves_old_metadata_and_other_files_continue(self):
        first = self.video("ABC-123 one.mp4", "22222", {"mediainfo": "old"})
        self.video("DEF-456.mp4", "33333")
        def fetch(identifier, token, force_refresh=False):
            if identifier == "22222":
                raise app.AppError("detail failed")
            return self.detail(2)
        with patch.object(app, "mteam_detail", side_effect=fetch):
            job = self.run_job()
        self.assertEqual(self.saved(first)["mediainfo"], "old")
        self.assertEqual(job["summary"]["failed"], 1)
        self.assertEqual(job["summary"]["refreshed"], 1)

    def test_same_failed_id_detail_is_not_requested_again_for_sibling_file(self):
        self.video("ABC-123 one.mp4", "22222")
        self.video("ABC-123 two.mp4", "22222")
        with patch.object(app, "mteam_detail", side_effect=app.AppError("detail failed")) as details:
            job = self.run_job()
        self.assertEqual(details.call_count, 1)
        self.assertEqual(job["summary"]["failed"], 2)

    def test_performer_mismatch_does_not_replace_old_media_info(self):
        path = self.video(identifier="22222", extra={"mediainfo": "old"})
        detail = {**self.detail(), "name": "Other performer"}
        with patch.object(app, "mteam_detail", return_value=detail), patch.object(app, "mteam_subtitle_download") as subtitles:
            job = self.run_job()
        self.assertEqual(job["summary"]["failed"], 1)
        self.assertEqual(self.saved(path)["mediainfo"], "old")
        subtitles.assert_not_called()

    def test_external_chinese_missing_subtitles_bypasses_old_cooldown_and_collects(self):
        path = self.video(identifier="22222", extra={"subtitle_check": {"state": "not_found", "checked_epoch": 9999999999}})
        collection = self.root / "subtitles"
        settings = app.get_settings()
        settings["subtitle_collection_dir"] = str(collection)
        app.write_json(app.SETTINGS_FILE, settings)
        def download(code, identifier, paths, token):
            self.assertEqual(paths, [path])
            subtitle = path.with_name(path.stem + ".MT.zh-HK.srt")
            subtitle.write_text("subtitle", encoding="utf-8")
            return {"status": "downloaded", "files": [str(subtitle)], "downloads": 42}
        with patch.object(app, "mteam_detail", return_value=self.detail()), patch.object(app, "mteam_subtitle_download", side_effect=download) as subtitles:
            job = self.run_job()
        subtitles.assert_called_once()
        self.assertEqual(job["summary"]["subtitles_downloaded"], 1)
        self.assertEqual((collection / "ABC-123.srt").read_text(), "subtitle")
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(database.execute("select mteam_id from local_video_files").fetchone()[0], "22222")

    def test_existing_subtitle_is_preserved_and_non_chinese_embedded_skip_queries(self):
        path = self.video(identifier="22222")
        path.with_suffix(".srt").write_text("existing", encoding="utf-8")
        for rank in (1, 2, 0):
            with self.subTest(rank=rank), patch.object(app, "mteam_detail", return_value=self.detail(rank)), patch.object(app, "mteam_subtitle_download") as subtitles:
                if rank != 1:
                    path.with_suffix(".srt").unlink(missing_ok=True)
                self.run_job()
                subtitles.assert_not_called()

    def test_subtitle_failure_does_not_roll_back_refreshed_info(self):
        path = self.video(identifier="22222", extra={"mediainfo": "old"})
        with patch.object(app, "mteam_detail", return_value=self.detail()), patch.object(app, "mteam_subtitle_download", side_effect=app.AppError("offline")):
            job = self.run_job()
        self.assertEqual(job["summary"]["failed"], 1)
        self.assertEqual(job["summary"]["refreshed"], 1)
        self.assertNotEqual(self.saved(path)["mediainfo"], "old")

    def test_enqueue_deduplicates_and_prevents_overlapping_all_and_single(self):
        first = self.video()
        whole = app.queue_data_repair("sub")
        self.assertEqual(app.queue_data_repair("sub")["job_id"], whole["job_id"])
        self.assertEqual(app.queue_data_repair("sub", str(first), "ABC-123")["job_id"], whole["job_id"])
        job = app.read_repair_jobs()[whole["job_id"]]
        job["state"] = "completed"
        app.save_repair_job(job)
        single = app.queue_data_repair("sub", str(first), "ABC-123")
        self.assertEqual(app.queue_data_repair("sub", str(first), "ABC-123")["job_id"], single["job_id"])
        with self.assertRaisesRegex(app.AppError, "单视频修复"):
            app.queue_data_repair("sub")

    def test_interrupt_and_recover_preserves_completed_file_and_timer(self):
        first = self.video("ABC-123.mp4", "22222")
        self.video("DEF-456.mp4", "33333")
        app.write_json(app.SCHEDULE_FILE, {"last_run_at": "2026-11-21T00:00:00+00:00"})
        app.write_json(app.STATUS_FILE, {"state": "completed", "trigger": "scheduled"})
        with patch.object(app, "mteam_detail", side_effect=[self.detail(2), app.MTeamRequestInterrupted("stop")]):
            job = self.run_job()
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["outcomes"][str(first)]["details_done"])
        app.recover_data_repairs()
        app.recover_data_repairs()
        self.assertEqual(len(app.read_scan_requests()), 1)
        with patch.object(app, "mteam_detail", return_value=self.detail(2)) as details:
            app.run_data_repair_request(app.pop_scan_request(), RepairDownloader())
        self.assertEqual(details.call_args.args[0], "33333")
        self.assertEqual(app.read_json(app.SCHEDULE_FILE, {})["last_run_at"], "2026-11-21T00:00:00+00:00")
        self.assertEqual(app.read_json(app.STATUS_FILE, {})["trigger"], "scheduled")

    def test_rate_limited_request_resumes_current_detail_in_place(self):
        self.video(identifier="22222")
        app.write_json(app.STATUS_FILE, {"state": "completed", "trigger": "scheduled", "message": "last check"})
        body = json.dumps({"code": 0, "data": self.detail(2)}).encode()
        clock = [1000.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(app, "_mteam_request_once", side_effect=[app.MTeamRateLimitError("limited", 900), (body, {})]) as request, \
             patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=sleep):
            job = self.run_job()
        self.assertEqual(job["state"], "completed")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])
        self.assertFalse(app.MTEAM_LIMIT_FILE.exists())
        self.assertEqual(app.read_json(app.STATUS_FILE, {})["message"], "last check")

    def test_restart_waits_for_saved_repair_cooldown_before_resuming(self):
        self.video(identifier="22222")
        queued = app.queue_data_repair("sub")
        request = app.pop_scan_request()
        app.write_json(app.MTEAM_LIMIT_FILE, {"repair_job_id": queued["job_id"], "retry_at": 1005.0})
        clock = [1000.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=sleep), \
             patch.object(app, "mteam_detail", side_effect=lambda *_args, **_kwargs: self._detail_after_cooldown(clock)) as detail:
            app.run_data_repair_request(request, RepairDownloader())
        self.assertGreaterEqual(clock[0], 1005.0)
        detail.assert_called_once()

    def test_repair_job_exposes_live_rate_limit_countdown(self):
        queued = app.queue_data_repair("sub")
        job = app.read_repair_jobs()[queued["job_id"]]
        job.update({"state": "running", "phase": "details"})
        app.save_repair_job(job)
        retry_at = app.time.time() + 95
        app.write_json(app.MTEAM_LIMIT_FILE, {"repair_job_id": queued["job_id"], "retry_at": retry_at})
        result = app.public_repair_job(job)
        self.assertEqual(result["phase"], "waiting_retry")
        self.assertEqual(result["retry_at"], retry_at)
        self.assertGreater(result["retry_in"], 0)

    def test_global_repair_job_list_includes_terminal_jobs_for_completion_notifications(self):
        queued = app.queue_data_repair("sub")
        job = app.read_repair_jobs()[queued["job_id"]]
        job.update({"state": "completed", "phase": "completed", "completed_at": app.now_iso()})
        app.save_repair_job(job)
        self.assertEqual(app.list_repair_jobs(), [app.public_repair_job(job)])

    def _detail_after_cooldown(self, clock):
        self.assertGreaterEqual(clock[0], 1005.0)
        return self.detail(2)

    def test_outside_scope_file_is_rejected(self):
        outside = self.root / "ABC-123.mp4"
        outside.touch()
        with self.assertRaisesRegex(app.AppError, "订阅目录"):
            app.queue_data_repair("sub", str(outside), "ABC-123")

    def test_cover_url_prefers_image_list_and_falls_back_to_description(self):
        self.assertEqual(app.detail_cover_url({"imageList": ["https://pics.dmm.co.jp/cover.jpg"],
            "descr": "![](https://img.m-team.cc/other.jpg)"}), "https://pics.dmm.co.jp/cover.jpg")
        self.assertEqual(app.detail_cover_url({"imageList": ["javascript:alert(1)"],
            "descr": "![](https://img.m-team.cc/fallback.jpg)"}), "https://img.m-team.cc/fallback.jpg")
        self.assertEqual(app.detail_cover_url({"imageList": ["https://user:secret@example.com/a", None]}), "")

    def test_single_video_repair_forces_cover_and_gallery_refresh_for_that_number(self):
        target = self.video(identifier="22222")
        self.video("DEF-123.mp4", "33333", {"cover_url": "https://example.com/other-cover.jpg"})
        cover_url = "https://example.com/cover.jpg"
        screenshot_url = "https://example.com/screenshot.jpg"
        dmm_code = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=abc123/"
        detail = {**self.detail(2), "imageList": [cover_url], "dmmCode": dmm_code}
        old = b"\xff\xd8\xff\xe0old-image\xff\xd9"
        new = b"\xff\xd8\xff\xe0new-image\xff\xd9"
        directory = target.parent
        with patch.object(app.cover_images, "crop_poster", return_value=old), patch.object(app, "mteam_detail", return_value=detail) as details, \
             patch.object(app, "mteam_json", return_value={"data": {"picData": screenshot_url}}) as dmm_api:
            with patch.object(app.cover_images, "fetch", return_value=(".jpg", old)) as fetch:
                self.run_job(path=target, code="ABC-123")
                self.assertEqual([call.args[0] for call in fetch.call_args_list], [cover_url, screenshot_url])
            with patch.object(app.cover_images, "fetch", return_value=(".jpg", new)) as fetch:
                job = self.run_job(path=target, code="ABC-123")
                self.assertEqual([call.args[0] for call in fetch.call_args_list], [cover_url, screenshot_url])
            self.assertEqual(job["summary"]["covers_downloaded"], 1)
            self.assertEqual(job["summary"]["screenshots_downloaded"], 1)
            self.assertEqual(dmm_api.call_count, 2)
            self.assertEqual([call.args[0] for call in details.call_args_list], ["22222", "22222"])
            job = self.run_job(path=target, code="ABC-123")
            self.assertEqual(job["summary"]["image_failed"], 2)
        self.assertEqual((directory / "fansrt.jpg").read_bytes(), new)
        self.assertEqual((directory / "screenshot-1.jpg").read_bytes(), new)
        self.assertFalse((directory.parent / "DEF-123" / "fansrt.jpg").exists())

    def test_repair_saves_distinct_cover_urls_per_file_and_sqlite(self):
        first = self.video("ABC-123 one.mp4", "22222")
        second = self.video("ABC-123 two.mp4", "33333")
        def fetch(identifier, token, force_refresh=False):
            return {**self.detail(2), "imageList": [f"https://example.com/{identifier}.jpg"]}
        with patch.object(app, "mteam_detail", side_effect=fetch):
            self.run_job()
        self.assertEqual(self.saved(first)["cover_url"], "https://example.com/22222.jpg")
        self.assertEqual(self.saved(second)["cover_url"], "https://example.com/33333.jpg")
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertCountEqual(database.execute("select mteam_id, cover_url from local_video_files").fetchall(),
                [("22222", "https://example.com/22222.jpg"), ("33333", "https://example.com/33333.jpg")])

    def test_lazy_cover_fetch_is_cached_and_cannot_escape_file_scope(self):
        path = self.video(identifier="22222")
        detail = {**self.detail(2), "imageList": ["https://example.com/cover.jpg"]}
        with patch.object(app, "mteam_detail", return_value=detail) as fetch:
            first = app.get_subscription_video_cover("sub", "ABC-123", str(path))
            second = app.get_subscription_video_cover("sub", "ABC-123", str(path))
        self.assertEqual(first, second)
        fetch.assert_called_once_with("22222", "token")
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(database.execute("select cover_url from local_video_files").fetchone()[0], "https://example.com/cover.jpg")
        with self.assertRaisesRegex(app.AppError, "订阅目录"):
            app.get_subscription_video_cover("sub", "ABC-123", str(self.root / "ABC-123.mp4"))

    def test_cover_migration_preserves_existing_rows(self):
        path = self.video(identifier="22222")
        app.index_subscription_videos("sub", app.scan_local_releases(str(self.videos)), app.read_json(app.RECORDS_FILE, {})["sub"])
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            database.execute("ALTER TABLE local_video_files DROP COLUMN cover_url")
            database.commit()
        with closing(app.connect_video_database()) as database:
            self.assertEqual(database.execute("select path, cover_url from local_video_files").fetchall(), [(str(path), "")])

    def test_refresh_clears_removed_cover_and_empty_cover_is_cached(self):
        path = self.video(identifier="22222", extra={"cover_url": "https://example.com/old.jpg", "cover_checked": True})
        with patch.object(app, "mteam_detail", return_value=self.detail(2)):
            self.run_job()
        self.assertEqual(self.saved(path)["cover_url"], "")
        with patch.object(app, "mteam_detail") as fetch:
            self.assertEqual(app.get_subscription_video_cover("sub", "ABC-123", str(path))["cover_url"], "")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
