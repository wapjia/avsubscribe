import base64
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from email.message import Message
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import Mock, call, patch

from app.bin import app, azure_translator, cover_images


ImageResponse = cover_images.ImageResponse

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAUAAAACCAIAAAAfCIEKAAAAFUlEQVR4nGP8zwACjAwQmoEJQsEBADZDAgM9HBeyAAAAAElFTkSuQmCC")
GIF = base64.b64decode("R0lGODdhBQACAIEAAP8AAAAA/wAAAAAAACwAAAAABQACAAAICwABAAhAUCDBAAEBADs=")
DMM_COVER = "https://pics.dmm.co.jp/mono/movie/adult/ssni208/ssni208pl.jpg"
DMM_SAMPLES = "https://pics.dmm.co.jp/digital/video/ssni00208/ssni00208"
DMM_CODE = "http://www.dmm.co.jp/mono/dvd/-/detail/=/cid=ssni208/"


class CoverFetchTests(unittest.TestCase):
    def response(self, status=200, headers=None, chunks=()):
        response = Mock(status=status)
        response.getheader.side_effect = (headers or {}).get
        response.read1.side_effect = [*chunks, b""]
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *_: False
        return response

    def resolver(self, hostname, port, **_):
        address = "127.0.0.1" if hostname == "internal.test" else "1.1.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, port))]

    def test_validates_each_redirect_and_pins_the_public_connection(self):
        response = self.response(302, {"Location": "http://internal.test/image.png"})
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(socket, "getaddrinfo", side_effect=self.resolver), \
             patch.object(cover_images.http.client, "HTTPSConnection", return_value=connection) as https, \
             patch.object(cover_images.http.client, "HTTPConnection") as http:
            with self.assertRaisesRegex(ValueError, "非公开"):
                cover_images.fetch("https://public.test/image.png")
            https.assert_called_once_with("public.test", 443, timeout=5)
            http.assert_not_called()
            request_headers = connection.request.call_args.kwargs["headers"]
            self.assertEqual(set(request_headers), {"Accept"})
            with patch.object(socket, "create_connection") as connect:
                connection._create_connection(("public.test", 443), 5, None)
            connect.assert_called_once_with(("1.1.1.1", 443), 5, None)

    def test_checks_image_signature_and_limits_body(self):
        connection = Mock()
        responses = [
            self.response(headers={"Content-Type": "application/octet-stream"}, chunks=[PNG]),
            self.response(chunks=[b"<html>error</html>"]),
            self.response(headers={"Content-Length": str(cover_images.MAX_BYTES + 1)}),
        ]
        connection.getresponse.side_effect = responses
        with patch.object(socket, "getaddrinfo", side_effect=self.resolver), \
             patch.object(cover_images.http.client, "HTTPSConnection", return_value=connection):
            self.assertEqual(cover_images.fetch("https://public.test/signed?file=cover.webp"), (".png", PNG))
            with self.assertRaisesRegex(ValueError, "格式"):
                cover_images.fetch("https://public.test/wrong.png")
            with self.assertRaisesRegex(ValueError, "12 MiB"):
                cover_images.fetch("https://public.test/large.png")
            connection.getresponse.side_effect = None
            connection.getresponse.return_value = self.response(chunks=[b"x" * 17])
            with patch.object(cover_images, "MAX_BYTES", 16), self.assertRaisesRegex(ValueError, "12 MiB"):
                cover_images.fetch("https://public.test/stream.png")
            responses[-1].read1.assert_not_called()

    def test_conditional_image_download_reuses_only_the_verified_redirect_target(self):
        url = "https://public.test/image.png"
        target = "https://cdn.test/one.png"
        changed_target = "https://cdn.test/two.png"
        unchanged = self.response(304)
        changed = self.response(headers={"Last-Modified": "Tue, 24 Nov 2026 01:00:00 GMT"}, chunks=[GIF])
        connection = Mock()
        connection.getresponse.side_effect = [
            self.response(302, {"Location": target}), self.response(headers={"ETag": '"image-v1"'}, chunks=[PNG]),
            self.response(302, {"Location": target}), unchanged,
            self.response(302, {"Location": changed_target}), changed,
        ]
        with patch.object(socket, "getaddrinfo", side_effect=self.resolver), \
             patch.object(cover_images.http.client, "HTTPSConnection", return_value=connection):
            cached = cover_images.fetch_image(url)
            self.assertEqual(cached.image, (".png", PNG))
            self.assertIsNone(cover_images.fetch_image(url, cached.validator).image)
            replacement = cover_images.fetch_image(url, cached.validator)
            self.assertEqual(replacement.image, (".gif", GIF))
        sent = [request.kwargs["headers"].get("If-None-Match") for request in connection.request.call_args_list]
        self.assertEqual(sent, [None, None, None, '"image-v1"', None, None])
        unchanged.read1.assert_not_called()
        self.assertEqual(cover_images.conditional_image_headers(replacement.validator),
                         {"If-Modified-Since": "Tue, 24 Nov 2026 01:00:00 GMT"})

    def test_extracts_explicit_gallery_without_the_cover_and_supports_dmm_album(self):
        detail = {
            "imageList": [DMM_COVER],
            "descr": "[img]https://img.m-team.cc/images/proof.jpg[/img]",
        }
        self.assertEqual(app.detail_image_metadata(detail)["screenshot_urls"], [])
        self.assertEqual(app.dmm_screenshot_base(DMM_COVER), DMM_SAMPLES)
        self.assertEqual(app.dmm_screenshot_base(DMM_SAMPLES + "pl.jpg"), DMM_SAMPLES)
        self.assertEqual(app.dmm_screenshot_base(DMM_COVER.replace("pics.dmm.co.jp", "example.com")), "")
        explicit = [f"{DMM_SAMPLES}-2.jpg", "https://example.com/another.png"]
        detail["imageList"] += [*explicit, explicit[0], "javascript:alert(1)"]
        self.assertEqual(app.detail_image_metadata(detail)["screenshot_urls"], explicit)
        self.assertEqual(app.detail_image_metadata({
            "imageList": ["https://example.com/cover.jpg"],
            "descr": "[img]https://example.com/shot.jpg[/img]",
        })["screenshot_urls"], ["https://example.com/shot.jpg"])

    def test_dmm_cover_jpeg_accepts_trailing_newline(self):
        jpeg = b"\xff\xd8\xff\xe0test-data\xff\xd9"
        self.assertEqual(cover_images.image_extension(jpeg + b"\n"), ".jpg")
        with self.assertRaises(ValueError):
            cover_images.image_extension(jpeg + b"<script>")

    def test_azure_batches_japanese_and_preserves_dmm_base_info_structure(self):
        fields = {"base_info": {"シリーズ": "サンプル", "品番": "ABC-123", "発売日": "2018/05/19"},
                  "series": "サンプル", "text_data": "テストです。"}
        response = Mock()
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *_: False
        response.read.return_value = json.dumps([
            {"translations": [{"text": "样例"}]}, {"translations": [{"text": "这是一段测试。"}]},
        ]).encode()
        with patch.object(azure_translator.OPENER, "open", return_value=response) as open_request:
            translated = azure_translator.translate_metadata(fields, "test-key", "eastasia")
        self.assertEqual(translated, {"base_info": {"系列": "样例", "番号": "ABC-123", "发行日期": "2018/05/19"},
                                      "series": "样例", "text_data": "这是一段测试。"})
        open_request.assert_called_once()
        request = open_request.call_args.args[0]
        self.assertEqual(json.loads(request.data), [{"Text": "サンプル"}, {"Text": "テストです。"}])
        self.assertIn("from=ja&to=zh-Hans", request.full_url)
        self.assertEqual(request.get_header("Ocp-apim-subscription-key"), "test-key")
        self.assertEqual(request.get_header("Ocp-apim-subscription-region"), "eastasia")

    def test_azure_translates_each_hashtag_without_sending_its_marker(self):
        fields = {"base_info": {"関連タグ": "#衣装 #学校, #AV ＃季節"}}
        with patch.object(azure_translator, "translate_batch", return_value=["服装", "学校", "季节"]) as batch:
            translated = azure_translator.translate_metadata(fields, "key", "")
        self.assertEqual(batch.call_args.args[0], ["衣装", "学校", "季節"])
        self.assertEqual(translated, {"base_info": {"相关标签": "#服装 #学校, #AV ＃季节"}})

    def test_azure_translation_preserves_manual_actress_names_across_nested_fields(self):
        fields = {
            "title": "楓カレンと新ありな カレンの共演",
            "text_data": "楓カレンと新ありなの作品。",
            "base_info": {"出演者": "楓カレン,新ありな", "関連タグ": "#カレン #新ありな"},
        }
        translations = {"楓カレン": "枫可怜", "カレン": "凯伦", "新ありな": "新有菜"}
        with patch.object(azure_translator, "translate_batch", side_effect=lambda texts, _key, _region, _callback: [f"译文{text}" for text in texts]) as batch:
            translated = azure_translator.translate_metadata(fields, "key", "", actress_translations=translations)
        submitted = " ".join(batch.call_args.args[0])
        for source_name in translations:
            self.assertNotIn(source_name, submitted)
        self.assertIn("枫可怜", translated["title"])
        self.assertIn("新有菜", translated["title"])
        self.assertIn("凯伦", translated["title"])
        self.assertEqual(translated["base_info"]["演员"], "枫可怜,新有菜")
        self.assertIn("#凯伦", translated["base_info"]["相关标签"])
        self.assertIn("#新有菜", translated["base_info"]["相关标签"])

    def test_azure_uses_source_text_only_for_blank_translations(self):
        fields = {"title": "映画名", "text_data": "あらすじ", "base_info": {"関連タグ": "#試写 #季節"}}
        response = Mock(status=200)
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *_: False
        response.read.return_value = json.dumps([
            {"translations": [{"text": text, "to": "zh-Hans"}]}
            for text in ("片名", "", "　", "季节")
        ]).encode("utf-8")
        with patch.object(azure_translator.OPENER, "open", return_value=response):
            translated = azure_translator.translate_metadata(fields, "test-key", "")
        self.assertEqual(translated, {"title": "片名", "text_data": "あらすじ", "base_info": {"相关标签": "#試写 #季节"}})

    def test_azure_retries_without_name_when_translated_placeholder_is_lost(self):
        with patch.object(azure_translator, "translate_batch", side_effect=[["错误人名参演"], ["参演"]]) as batch:
            result = azure_translator.translate_metadata(
                {"title": "彩月七緒出演", "base_info": {"出演者": "彩月七緒"}},
                "key", "", actress_translations={"彩月七緒": "彩月七绪"},
            )
        self.assertEqual(batch.call_args.args[0], ["出演"])
        self.assertEqual(result, {"title": "彩月七绪参演", "base_info": {"演员": "彩月七绪"}})


class CoverLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch.object(app, "DATA_ROOT", self.root))
        for name in ("SETTINGS_FILE", "SUBSCRIPTIONS_FILE", "RECORDS_FILE", "LOCAL_VIDEO_DATABASE",
                     "LOG_FILE", "LOG_DATABASE_FILE", "LOCK_FILE", "REQUEST_FILE", "STATUS_FILE",
                     "SCHEDULE_FILE", "MTEAM_LIMIT_FILE"):
            self.enterContext(patch.object(app, name, self.root / name.lower()))
        self.subscriptions = [
            {"id": "a", "performer": "Actress A", "download_dir": str(self.root / "a")},
            {"id": "b", "performer": "Actress B", "download_dir": str(self.root / "b")},
        ]
        app.write_json(app.SUBSCRIPTIONS_FILE, self.subscriptions)
        self.records = {"a": {}, "b": {}}
        self.default_dir = self.root / "Cover"

    def test_versioned_local_image_responses_are_cacheable_and_unversioned_are_not(self):
        self.assertEqual(app.versioned_image_cache_control("v=123456789"), "private, max-age=31536000, immutable")
        self.assertEqual(app.versioned_image_cache_control(""), "no-store")
        self.assertEqual(app.versioned_image_cache_control("v=unknown"), "no-store")

    def test_cover_index_cleanup_removes_stale_rows_and_preserves_video_references(self):
        _, video = self.index("a", "ABC-123", "10001", [1, 2, 1])
        root = video.parent.parent
        old_cover = root / "ABC-123-fansrt.png"
        old_cover.write_bytes(PNG)
        (root / "DEF-234.mp4").touch()
        (root / "GHI-345.mp4.!qB").touch()
        unknown = root / "unknown"
        unknown.mkdir()
        (unknown / "video.mp4").touch()
        offline = Path(self.subscriptions[1]["download_dir"]) / "JKL-456"
        detached = self.root / "former-subscription"
        kept = {(video.parent, "ABC-123"), (root, "DEF-234"), (root, "GHI-345"),
                (unknown, "DEF-789"), (offline, "JKL-456")}
        connection = app.connect_video_database()
        try:
            with connection:
                connection.executemany(
                    """INSERT INTO local_video_covers(directory, code, source_id, source_url, filename)
                       VALUES (?, ?, '', '', 'fansrt.png')""",
                    [(str(path), code) for path, code in kept | {(root, "ABC-123"), (detached, "PQR-789")}],
                )
            self.assertEqual(app.clean_orphan_cover_index(), 2)
            remaining = {(Path(path), code) for path, code in connection.execute("SELECT directory, code FROM local_video_covers")}
            self.assertEqual(remaining, kept)
            self.assertEqual(app.clean_orphan_cover_index(), 0)
        finally:
            connection.close()
        self.assertEqual(old_cover.read_bytes(), PNG)
        self.assertTrue(video.exists())

    def test_cover_index_cleanup_has_an_independent_persisted_daily_schedule(self):
        with patch.object(app.time, "time", return_value=100000.0) as clock, \
             patch.object(app, "clean_orphan_cover_index", return_value=1) as cleanup:
            app.run_due_cover_index_cleanup()
            clock.return_value += app.COVER_INDEX_CLEANUP_INTERVAL - 1
            app.run_due_cover_index_cleanup()
            cleanup.assert_called_once()
            clock.return_value += 1
            app.run_due_cover_index_cleanup()
            self.assertEqual(cleanup.call_count, 2)
        self.assertFalse(app.SCHEDULE_FILE.exists())

    def index(self, owner, code, identifier, rank, url=None):
        subscription = next(item for item in self.subscriptions if item["id"] == owner)
        directory = Path(subscription["download_dir"]) / code
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{code}-{identifier}.mp4"
        path.touch()
        metadata = {"id": str(identifier), "rank": rank}
        if url is not None:
            metadata.update(cover_url=url, cover_checked=True, screenshot_urls=[], dmm_code="")
        self.records[owner].setdefault(code, {}).setdefault("local_files", {})[str(path)] = metadata
        return self.indexed(owner), path

    def indexed(self, owner):
        subscription = next(item for item in self.subscriptions if item["id"] == owner)
        return app.index_subscription_videos(owner, app.scan_local_releases(subscription["download_dir"]), self.records[owner])

    def sync(self, owner, rows, **settings):
        subscription = next(item for item in self.subscriptions if item["id"] == owner)
        app.sync_subscription_covers(subscription, rows, self.records, settings)

    def test_legacy_cover_directory_setting_is_removed(self):
        app.write_json(app.SETTINGS_FILE, {"cover_dir": str(self.default_dir)})
        self.assertNotIn("cover_dir", app.get_settings())
        self.assertNotIn("default_cover_dir", app.public_settings())
        app.save_settings({})
        self.assertNotIn("cover_dir", app.read_json(app.SETTINGS_FILE, {}))

    def test_dmm_info_discovers_actresses_and_keeps_manual_names_on_refresh(self):
        data = {"picData": "", "actressList": ["楓カレン", "新ありな", "楓カレン", " ", None]}
        connection = app.connect_video_database()
        try:
            with patch.object(app, "mteam_json", return_value={"data": data}):
                app.dmm_screenshot_urls(connection, DMM_CODE, "token")
            first = {item["source_name"]: item for item in app.list_actresses()}
            self.assertEqual(set(first), {"楓カレン", "新ありな"})
            self.assertTrue(first["楓カレン"]["first_seen"])
            app.save_actress_translation({"source_name": "楓カレン", "chinese_name": "枫可怜"})
            original_first_seen = first["楓カレン"]["first_seen"]
            data["actressList"] = ["楓カレン", "新ありな", "白上咲花"]
            with patch.object(app, "mteam_json", return_value={"data": data}):
                app.dmm_screenshot_urls(connection, DMM_CODE, "token", refresh=True)
            refreshed = {item["source_name"]: item for item in app.list_actresses()}
            self.assertEqual(refreshed["楓カレン"]["chinese_name"], "枫可怜")
            self.assertEqual(refreshed["楓カレン"]["first_seen"], original_first_seen)
            self.assertIn("白上咲花", refreshed)
            self.assertEqual(app.get_actress_translations(connection), {"楓カレン": "枫可怜"})
        finally:
            connection.close()

    def test_dmm_info_stores_the_full_response_and_reuses_it_for_seven_days(self):
        data = {"picData": "https://example.com/shot.jpg", "actressList": ["楓カレン"],
                "title": "タイトル", "baseInfo": {"出演者": "楓カレン"}, "futureField": {"value": 42}}
        connection = app.connect_video_database()
        try:
            with patch.object(app.time, "time", return_value=1000.0), \
                 patch.object(app, "mteam_json", return_value={"data": data}) as request:
                app.dmm_screenshot_urls(connection, DMM_CODE, "token", settings={"debug_mode": True})
                self.assertEqual(app.cached_dmm_response(connection, DMM_CODE), {"data": data})
                app.dmm_screenshot_urls(connection, DMM_CODE, "token", settings={"debug_mode": True},
                                        code="SSNI-208", subscription="Actress A")
                self.assertEqual(request.call_count, 1)
            with patch.object(app.time, "time", return_value=1000.0 + app.DMM_INFO_CACHE_TTL_SECONDS + 1), \
                 patch.object(app, "mteam_json", return_value={"data": {**data, "futureField": {"value": 43}}}) as request:
                app.dmm_screenshot_urls(connection, DMM_CODE, "token", settings={"debug_mode": True})
                self.assertEqual(request.call_count, 1)
                self.assertEqual(app.cached_dmm_response(connection, DMM_CODE)["data"]["futureField"]["value"], 43)
        finally:
            connection.close()
        logs = [entry for entry in app.get_logs() if entry["event"] == "debug_http_response"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["subscription"], "Actress A")
        heading, parameters, body = logs[0]["message"].split("\n", 2)
        self.assertRegex(heading, r"^M-Team /dmm/dmmInfo · HTTP 200\(缓存\) · SSNI-208 · 耗时 (?:\d+ ms|\d+\.\d{2} 秒)$")
        self.assertEqual(json.loads(parameters.removeprefix("请求参数：")), {"dmmCode": DMM_CODE})
        self.assertEqual(json.loads(body)["data"]["futureField"], {"value": 42})

    def test_detail_response_logs_show_sqlite_and_round_cache_hits_in_debug_mode(self):
        data = {"name": "ABC-123", "description": "Contains private-token", "id": "10001"}
        response = {"code": 0, "data": data, "message": "SUCCESS"}
        with patch.object(app.MTEAM_POLL_CACHE, "active", None, create=True), \
             patch.object(app, "mteam_json", return_value=response) as request:
            self.assertEqual(app.mteam_detail("10001", "private-token"), data)
            self.assertEqual(app.mteam_detail("10001", "private-token"), data)
            self.assertEqual(app.get_logs(), [])
            app.write_json(app.SETTINGS_FILE, {"debug_mode": True})
            app.MTEAM_POLL_CACHE.active = {"details": {}, "options": {}}
            self.assertEqual(app.mteam_detail("10001", "private-token"), data)
            self.assertEqual(app.mteam_detail("10001", "private-token"), data)
        request.assert_called_once()
        logs = [entry["message"] for entry in app.get_logs() if entry["event"] == "debug_http_response"]
        self.assertEqual(len(logs), 2)
        heading, parameters, body = logs[0].split("\n", 2)
        self.assertRegex(heading, r"^M-Team /torrent/detail · HTTP 200\(缓存\) · ABC-123 · 耗时 ")
        self.assertEqual(json.loads(parameters.removeprefix("请求参数：")), {"id": "10001"})
        sanitized = {**response, "data": {**data, "description": "Contains [已隐藏]"}}
        self.assertEqual(json.loads(body), sanitized)
        heading, parameters, body = logs[1].split("\n", 2)
        self.assertEqual(heading, "M-Team /torrent/detail · HTTP 200(缓存) · M-Team ID 10001")
        self.assertEqual(json.loads(parameters.removeprefix("请求参数：")), {"id": "10001"})
        self.assertEqual(json.loads(body), sanitized)

    def test_dmm_gallery_reuse_logs_original_json_for_each_number_only_once(self):
        for code, identifier in (("ABC-123", "10001"), ("DEF-456", "10002")):
            rows, path = self.index("a", code, identifier, [1, 2, 1], "")
            self.records["a"][code]["local_files"][str(path)].update(dmm_code=DMM_CODE, dmm_detail_checked=True)
        response = {"code": "0", "data": {"picData": "https://example.com/shot", "title": "Example"}, "message": "SUCCESS"}
        with patch.object(app, "mteam_json", return_value=response) as request, \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows, mteam_token="token", debug_mode=True)
        request.assert_called_once_with("/dmm/dmmInfo", "token", {"dmmCode": DMM_CODE}, multipart=True)
        logs = [entry["message"] for entry in app.get_logs() if entry["event"] == "debug_http_response"]
        self.assertEqual(len(logs), 1)
        heading, parameters, body = logs[0].split("\n", 2)
        self.assertIn("HTTP 200(缓存)", heading)
        self.assertEqual(json.loads(parameters.removeprefix("请求参数：")), {"dmmCode": DMM_CODE})
        self.assertEqual(json.loads(body), response)

    def test_option_and_subtitle_cache_logs_keep_untransformed_response_envelopes(self):
        app.write_json(app.SETTINGS_FILE, {"debug_mode": True})
        options = {"code": 0, "data": [{"id": 1, "name": "1080p"}]}
        languages = {"code": 0, "data": [{"id": 2, "name": "Chinese"}]}
        subtitles = {"code": 0, "data": {"items": []}, "message": "SUCCESS"}
        with patch.object(app.MTEAM_POLL_CACHE, "active", {"details": {}, "options": {}}, create=True), \
             patch.object(app, "mteam_json", side_effect=[options, languages, subtitles]) as request:
            for _ in range(2):
                self.assertEqual(app.mteam_option_map("/torrent/standardList", "token"), {"1": "1080p"})
                self.assertEqual(app.mteam_subtitle_language_map("token"), {"2": "Chinese"})
                app.mteam_subtitle_download("ABC-123", "10001", [], "token")
        self.assertEqual(request.call_count, 3)
        logs = [entry["message"] for entry in app.get_logs() if entry["event"] == "debug_http_response"]
        self.assertEqual(len(logs), 3)
        self.assertTrue(all("HTTP 200(缓存)" in message.split("\n", 1)[0] for message in logs))
        self.assertEqual([json.loads(message.split("\n", 2 if "\n请求参数：" in message else 1)[-1]) for message in logs],
                         [options, languages, subtitles])

    def test_option_lists_survive_new_rounds_and_only_unknown_ids_trigger_refresh(self):
        app.write_json(app.SETTINGS_FILE, {"debug_mode": True})
        standard = "/torrent/standardList"
        codec = "/torrent/videoCodecList"
        sd = {"code": 0, "data": [{"id": 1, "name": "1080p"}], "message": "SUCCESS"}
        avc = {"code": 0, "data": [{"id": 2, "name": "H.264"}]}
        hevc = {"code": 0, "data": [*avc["data"], {"id": 3, "name": "H.265"}]}
        with patch.object(app.MTEAM_POLL_CACHE, "active", None, create=True), \
             patch.object(app.time, "time", return_value=100000.0) as clock, \
             patch.object(app, "mteam_json", side_effect=[sd, avc, hevc, {"code": 0, "data": []}]) as request:
            self.assertEqual(app.mteam_option_map(standard, "token", (1,)), {"1": "1080p"})
            self.assertEqual(app.mteam_option_map(codec, "token", (2,)), {"2": "H.264"})
            clock.return_value += 3 * 365 * 24 * 60 * 60
            app.MTEAM_POLL_CACHE.active = {"details": {}, "options": {}}
            for _ in range(3):
                self.assertEqual(app.mteam_option_map(standard, "token", (1,)), {"1": "1080p"})
                self.assertEqual(app.mteam_option_map(codec, "token", (2,)), {"2": "H.264"})
            self.assertEqual(request.call_count, 2)
            logs = [entry["message"] for entry in app.get_logs() if entry["event"] == "debug_http_response"]
            self.assertEqual(len(logs), 2)
            self.assertTrue(all("HTTP 200(缓存)" in message.split("\n", 1)[0] for message in logs))
            self.assertEqual([json.loads(message.split("\n", 1)[1]) for message in logs], [sd, avc])
            self.assertEqual(app.mteam_option_map(codec, "token", (3,)), {"2": "H.264", "3": "H.265"})
            self.assertEqual(app.mteam_option_map(standard, "token", (99,)), {"1": "1080p"})
            app.MTEAM_POLL_CACHE.active = None
            self.assertEqual(app.mteam_option_map(standard, "token", (99,)), {"1": "1080p"})
            self.assertEqual(app.mteam_option_map(codec, "token", (3,)), {"2": "H.264", "3": "H.265"})
            self.assertEqual([entry.args[0] for entry in request.call_args_list], [standard, codec, codec, standard])

    def test_subtitle_languages_refresh_only_for_unknown_ids_after_cooldown(self):
        endpoint = "/subtitle/langs"
        original = {"code": 0, "data": {"2": {"langName": "English"}}}
        updated = {"code": 0, "data": [{"langId": 2, "langName": "English"}, {"langId": 3, "langName": "简体中文"}]}
        with patch.object(app.MTEAM_POLL_CACHE, "active", None, create=True), \
             patch.object(app.time, "time", return_value=100000.0) as clock, \
             patch.object(app, "mteam_json", side_effect=[original, updated]) as request:
            self.assertEqual(app.mteam_subtitle_language_map("token", {2}), {"2": "English"})
            self.assertEqual(app.mteam_subtitle_language_map("token", {3}), {"2": "English"})
            clock.return_value += 3 * 365 * 24 * 60 * 60
            self.assertEqual(app.mteam_subtitle_language_map("token", {2}), {"2": "English"})
            request.assert_called_once_with(endpoint, "token")
            self.assertEqual(app.mteam_subtitle_language_map("token", {3}), {"2": "English", "3": "简体中文"})
            self.assertEqual(app.mteam_subtitle_language_map("token", {3}), {"2": "English", "3": "简体中文"})
            self.assertEqual(request.call_count, 2)

    def test_database_upgrade_recovers_actress_names_from_existing_dmm_records(self):
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute(
                    """INSERT INTO dmm_screenshot_catalog(dmm_code, urls_json, checked_at, base_info_json, title)
                       VALUES (?, '[]', 1234, ?, '旧影片')""",
                    (DMM_CODE, json.dumps({"出演者": "楓カレン,新ありな"})),
                )
                connection.execute("PRAGMA user_version=0")
        finally:
            connection.close()
        connection = app.connect_video_database()
        try:
            self.assertEqual({item["source_name"] for item in app.list_actresses()}, {"楓カレン", "新ありな"})
        finally:
            connection.close()

    def test_dmm_cache_backfills_missing_performers_without_overwriting_manual_names(self):
        response = {"code": "0", "data": {"actressList": ["彩月七緒", "北野未奈"]}}
        app.save_actress_translation({"source_name": "彩月七緒", "chinese_name": "彩月七绪"}, create=True)
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute(
                    """INSERT INTO dmm_screenshot_catalog(dmm_code, urls_json, checked_at, base_info_json, title, response_json)
                       VALUES (?, '[]', 1234, '{}', 'Example', ?)""",
                    (DMM_CODE, json.dumps(response)),
                )
                connection.execute("PRAGMA user_version=1")
        finally:
            connection.close()

        with patch.object(app, "mteam_json") as request:
            listed = {item["source_name"]: item for item in app.list_actresses()}
            self.assertEqual(listed["彩月七緒"]["chinese_name"], "彩月七绪")
            self.assertEqual(listed["北野未奈"]["first_seen"], 1234)
            connection = app.connect_video_database()
            try:
                with connection:
                    connection.execute("DELETE FROM actresses WHERE source_name=?", ("北野未奈",))
                self.assertEqual(app.cached_dmm_response(connection, DMM_CODE), response)
                self.assertIn("北野未奈", {item["source_name"] for item in app.list_actresses()})
            finally:
                connection.close()
            request.assert_not_called()

    def test_actress_showcase_stores_selected_profile_in_actresses_and_reuses_it(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.save_actress_translation({"source_name": "三上悠亚", "chinese_name": "三上悠亚"}, create=True)
        note = {"reading": "みかみゆあ", "birthday": "1993-08-16", "size": {"height": 159},
                "origin": "愛知県", "blood": "A型", "active": {"start": 2015, "end": 2023}}
        pic = "showcase/2026/0522/avatar.jpg"
        response = {"code": "0", "data": {"data": [
            {"id": "14256", "cntitle": "三上咲"},
            {"id": "4006", "cntitle": "三上悠亜", "entitle": "Mikami Yua", "pic": pic, "note": json.dumps(note)},
        ]}}
        with patch.dict(os.environ, {"PATH_INFO": "/api/actresses/profile", "REQUEST_METHOD": "POST"}), \
             patch.object(app, "read_body", return_value={"source_name": "三上悠亚"}), \
             patch.object(app, "mteam_json", side_effect=[{"data": {"data": []}}, response]) as request, \
             patch.object(app, "emit_json") as emit:
            app.handle_request()
            self.assertEqual(emit.call_args.args[0], "200 OK")
            result = emit.call_args.args[1]
            self.assertEqual(app.get_actress_profile({"source_name": "三上悠亚"}), result)
        self.assertEqual([call.args for call in request.call_args_list], [
            ("/dmm/showcase/search", "token", {"keyword": name, "pageNumber": 1, "pageSize": 20})
            for name in ("三上悠亚", "三上悠亜")
        ])
        self.assertEqual(result["profile"], {
            "showcase_id": "4006", "name": "三上悠亜", "romanized_name": "Mikami Yua", "reading": "みかみゆあ",
            "birthday": "1993-08-16", "height_cm": "159", "origin": "愛知県", "blood_type": "A型",
            "active_start": "2015", "active_end": "2023", "avatar_url": f"{app.MTEAM_API_BASE}/upload/{pic}",
        })
        actress = app.list_actresses()[0]
        self.assertEqual(actress["chinese_name"], "三上悠亚")
        self.assertEqual(actress["profile"], result["profile"])
        self.assertFalse(actress["profile_pending"])

    def test_subscription_title_uses_canonical_actress_cache_and_can_discover_a_missing_profile(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.save_actress_translation({"source_name": "原名", "chinese_name": "中文名"}, create=True)
        app.save_actress_translation({"source_name": "别名"}, create=True)
        app.merge_actress_name({"source_name": "别名", "canonical_name": "原名"})
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "chinese", "performer": "中文名"}, {"id": "alias", "performer": "别名"},
            {"id": "new", "performer": "三上悠亚"},
        ])
        responses = [
            {"data": {"data": [{"id": "1", "cntitle": "原名", "pic": "showcase/first.jpg"}]}},
            {"data": {"data": []}},
            {"data": {"data": [{"id": "4006", "cntitle": "三上悠亜", "pic": "showcase/second.jpg"}]}},
        ]
        with patch.object(app, "mteam_json", side_effect=responses) as request:
            with patch.dict(os.environ, {"PATH_INFO": "/api/subscriptions/chinese/actress-profile", "REQUEST_METHOD": "POST"}), \
                 patch.object(app, "emit_json") as emit:
                app.handle_request()
            status, cached = emit.call_args.args
            self.assertEqual(status, "200 OK")
            self.assertEqual(cached["chinese_name"], "中文名")
            self.assertEqual(cached["profile"]["showcase_id"], "1")
            self.assertEqual(app.get_subscription_actress_profile("alias"), cached)
            discovered = app.get_subscription_actress_profile("new")
            self.assertEqual(discovered["profile"]["name"], "三上悠亜")
            self.assertEqual(app.get_subscription_actress_profile("new"), discovered)
            listed = {item["id"]: item["actress_profile"] for item in app.subscriptions_with_video_counts()}
            self.assertEqual(listed, {"chinese": cached, "alias": cached, "new": discovered})
        self.assertEqual([call.args[2]["keyword"] for call in request.call_args_list], ["原名", "三上悠亚", "三上悠亜"])
        self.assertIn("三上悠亜", {item["source_name"] for item in app.list_actresses()})

    def test_actress_showcase_caches_an_ambiguous_search_without_binding_the_wrong_person(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.save_actress_translation({"source_name": "同名"}, create=True)
        result = {"data": {"data": [
            {"id": "1", "cntitle": "同名", "pic": "showcase/a.jpg"},
            {"id": "2", "cntitle": "同名", "pic": "showcase/b.jpg"},
        ]}}
        with patch.object(app, "mteam_json", return_value=result) as request:
            for _ in range(2):
                self.assertEqual(app.get_actress_profile({"source_name": "同名"}), {"profile": None, "profile_pending": False})
        request.assert_called_once()
        self.assertIsNone(app.list_actresses()[0]["profile"])

    def test_actress_http_endpoints_read_save_clear_and_leave_cached_translations_untouched(self):
        connection = app.connect_video_database()
        try:
            app.upsert_dmm_actresses(connection, ["楓カレン"])
            with connection:
                connection.execute(
                    """INSERT INTO dmm_screenshot_catalog(dmm_code, urls_json, checked_at, title, title_zh, text_data, text_data_zh)
                       VALUES (?, '[]', 1, ?, ?, ?, ?)""",
                    (DMM_CODE, "楓カレンの作品", "枫可怜的作品", "楓カレン主演", "枫可怜主演"),
                )
        finally:
            connection.close()

        with patch.dict(os.environ, {"PATH_INFO": "/api/actresses", "REQUEST_METHOD": "POST"}), \
             patch.object(app, "read_body", return_value={"source_name": "楓カレン", "chinese_name": "枫可怜"}), \
             patch.object(app, "emit_json") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", {"source_name": "楓カレン", "chinese_name": "枫可怜"})

        with patch.dict(os.environ, {"PATH_INFO": "/api/actresses/add", "REQUEST_METHOD": "POST"}), \
             patch.object(app, "read_body", return_value={"source_name": "手动演员", "chinese_name": "手动译名"}), \
             patch.object(app, "emit_json") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", {"source_name": "手动演员", "chinese_name": "手动译名"})

        with patch.dict(os.environ, {"PATH_INFO": "/api/actresses", "REQUEST_METHOD": "GET"}), \
             patch.object(app, "emit_json") as emit:
            app.handle_request()
        self.assertEqual(emit.call_args.args[0], "200 OK")
        listed = {item["source_name"]: item for item in emit.call_args.args[1]}
        self.assertEqual(listed["楓カレン"]["chinese_name"], "枫可怜")
        self.assertEqual(listed["手动演员"]["chinese_name"], "手动译名")

        with patch.dict(os.environ, {"PATH_INFO": "/api/actresses", "REQUEST_METHOD": "POST"}), \
             patch.object(app, "read_body", return_value={"source_name": "楓カレン", "chinese_name": " "}), \
             patch.object(app, "emit_json") as emit:
            app.handle_request()
        self.assertEqual(emit.call_args.args[1], {"source_name": "楓カレン", "chinese_name": ""})
        connection = app.connect_video_database()
        try:
            self.assertEqual(app.get_actress_translations(connection), {"手动演员": "手动译名"})
            self.assertIn("手动演员", {item["source_name"] for item in app.list_actresses()})
            stored = connection.execute(
                "SELECT title_zh, text_data_zh FROM dmm_screenshot_catalog WHERE dmm_code=?", (DMM_CODE,),
            ).fetchone()
            self.assertEqual(stored, ("枫可怜的作品", "枫可怜主演"))
        finally:
            connection.close()

    @unittest.skipUnless(find_spec("PIL"), "Pillow required to inspect poster pixels")
    def test_poster_crops_rightmost_47_percent_at_original_height(self):
        from PIL import Image

        cover = Image.new("RGB", (200, 40), "red")
        cover.paste("blue", (106, 0, 200, 40))
        source = io.BytesIO()
        cover.save(source, "PNG")
        with Image.open(io.BytesIO(cover_images.crop_poster(source.getvalue(), ".png"))) as poster:
            self.assertEqual(poster.size, (94, 40))
            self.assertEqual(poster.getextrema(), ((0, 0), (0, 0), (255, 255)))

    @unittest.skipUnless(find_spec("PIL"), "Pillow required to inspect poster pixels")
    def test_poster_stamps_multiple_video_labels_without_changing_source_cover(self):
        from PIL import Image

        cover = Image.new("RGB", (400, 240), "navy")
        source = io.BytesIO()
        cover.save(source, "PNG")
        original = source.getvalue()
        poster_data = cover_images.crop_poster(
            original, ".png", labels=["内嵌中文", "外挂中文", "4K", "1080P", "4K"],
        )
        with Image.open(io.BytesIO(poster_data)) as poster:
            self.assertEqual(poster.size, (188, 240))
            poster = poster.convert("RGB")
            self.assertEqual(poster.getpixel((0, 0)), (0, 0, 128))
            self.assertNotEqual(poster.getpixel((12, 12)), (0, 0, 128))
            self.assertEqual(poster.getpixel((poster.width - 3, poster.height - 3)), (0, 0, 128))
        self.assertEqual(source.getvalue(), original)

    @unittest.skipUnless(find_spec("PIL"), "Pillow required to inspect poster pixels")
    def test_poster_labels_keep_jpeg_output_valid(self):
        from PIL import Image

        cover = Image.new("RGB", (400, 240), "navy")
        source = io.BytesIO()
        cover.save(source, "JPEG")
        result = cover_images.crop_poster(source.getvalue(), ".jpg", labels=["4K"])
        self.assertEqual(cover_images.image_extension(result), ".jpg")
        with Image.open(io.BytesIO(result)) as poster:
            self.assertEqual(poster.mode, "RGB")
            self.assertEqual(poster.size, (188, 240))

    def test_ffmpeg_fallback_uses_packaged_label_png_overlays(self):
        completed = subprocess.CompletedProcess([], 0, stdout=PNG, stderr=b"")
        with patch.dict(sys.modules, {"PIL": None}), \
             patch.object(cover_images.shutil, "which", return_value="ffmpeg"), \
             patch.object(cover_images.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                cover_images.crop_poster(PNG, ".png", labels=["内嵌中文", "4K"]),
                PNG,
            )
        command = run.call_args.args[0]
        self.assertIn("-filter_complex", command)
        self.assertIn("-map", command)
        self.assertTrue(any(value.endswith("embedded-zh.png") for value in command))
        self.assertTrue(any(value.endswith("4k.png") for value in command))

    def test_poster_tags_are_derived_from_video_rank_in_its_directory(self):
        root = self.root
        rows = [
            {"path": str(root / "ABC-123-4k.mp4"), "rank": [2, 2, 2]},
            {"path": str(root / "ABC-123-1080p.mp4"), "rank": [1, 1, 1]},
            {"path": str(root / "nested" / "ABC-123.mp4"), "rank": [2, 2, 2]},
        ]
        self.assertEqual(app.local_poster_labels(rows, root), ["内嵌中文", "外挂中文", "4K", "1080P"])
        self.assertEqual(
            app.local_poster_labels(rows, root, language_watermark=False),
            ["4K", "1080P"],
        )
        self.assertEqual(
            app.local_poster_labels(rows, root, resolution_watermark=False),
            ["内嵌中文", "外挂中文"],
        )

    def test_poster_generation_version_encodes_both_watermark_settings(self):
        self.assertEqual(
            len({
                app.poster_generation_version(language, resolution)
                for language in (False, True)
                for resolution in (False, True)
            }),
            4,
        )

    @unittest.skipUnless(find_spec("PIL"), "Pillow required to inspect poster pixels")
    def test_nfo_records_rightmost_cover_crop_and_screenshots_in_video_directory(self):
        from PIL import Image

        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        self.records["a"]["ABC-123"]["local_files"][str(video)]["screenshot_urls"] = ["https://example.com/shot"]
        nfo = video.with_suffix(".nfo")
        nfo.write_text('<movie><director>Director</director><uniqueid type="mteam">10001</uniqueid></movie>', encoding="utf-8")
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows)
        self.assertEqual((video.parent / "fansrt.png").read_bytes(), PNG)
        self.assertEqual((video.parent / "screenshot-1.png").read_bytes(), PNG)
        with Image.open(video.parent / "poster.png") as poster:
            self.assertEqual(poster.size, (2, 2))
            self.assertEqual(poster.getextrema(), ((0, 0), (0, 0), (255, 255)))
        app.attach_local_cover_addresses("a", rows)
        poster_url = rows[0]["poster_image"]
        self.assertTrue(poster_url.startswith("/api/subscriptions/a/videos/ABC-123/poster-image?v="))
        with patch.dict(os.environ, {"PATH_INFO": poster_url.split("?", 1)[0], "QUERY_STRING": poster_url.split("?", 1)[1], "REQUEST_METHOD": "GET"}), patch.object(app, "emit") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", "image/png", (video.parent / "poster.png").read_bytes(), cache_control="private, max-age=31536000, immutable")
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
        self.assertIsNone(app.get_local_cover_image("b", "ABC-123", poster=True))
        metadata = ET.parse(nfo)
        self.assertEqual(metadata.findtext("fansrt"), "fansrt.png")
        self.assertEqual(metadata.findtext("poster"), "poster.png")
        self.assertEqual(metadata.findtext("screenshots/screenshot"), "screenshot-1.png")
        self.assertEqual(metadata.findtext("director"), "Director")

    def test_reuses_local_cover_and_regenerates_old_poster_once(self):
        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))), \
             patch.object(cover_images, "crop_poster", return_value=PNG):
            self.sync("a", rows)
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute("ALTER TABLE local_video_covers DROP COLUMN poster_crop_version")
        finally:
            connection.close()
        with patch.object(cover_images, "fetch_image") as fetch, \
             patch.object(cover_images, "crop_poster", return_value=PNG) as crop:
            self.sync("a", rows)
            self.sync("a", rows)
        crop.assert_called_once_with(PNG, ".png", labels=["内嵌中文", "1080P"])
        fetch.assert_not_called()
        self.assertEqual((video.parent / "fansrt.png").read_bytes(), PNG)

    def test_old_flat_media_directory_uses_distinct_artwork_names(self):
        _, first = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/abc")
        flat = first.parent.parent
        second = flat / "DEF-456.mp4"
        second.touch()
        first.rename(flat / first.name)
        sources = self.records["a"]["ABC-123"]["local_files"]
        sources[str(flat / first.name)] = sources.pop(str(first))
        sources[str(flat / first.name)]["screenshot_urls"] = ["https://example.com/abc-shot"]
        self.records["a"]["DEF-456"] = {"local_files": {str(second): {
            "id": "10002", "rank": [1, 2, 1], "cover_url": "https://example.com/def", "cover_checked": True,
            "screenshot_urls": ["https://example.com/def-shot"],
        }}}
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))), \
             patch.object(cover_images, "crop_poster", return_value=PNG):
            self.sync("a", self.indexed("a"))
        self.assertEqual(sorted(path.name for path in flat.glob("*.png")),
                         ["ABC-123-fansrt.png", "ABC-123-poster.png", "ABC-123-screenshot-1.png",
                          "DEF-456-fansrt.png", "DEF-456-poster.png", "DEF-456-screenshot-1.png"])

    def test_migrates_registered_screenshot_and_nfo_without_remote_urls(self):
        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "")
        legacy = video.parent / "ABC-123-1.png"
        renamed = video.parent / "screenshot-1.png"
        legacy.write_bytes(PNG)
        nfo = video.with_suffix(".nfo")
        nfo.write_text('<movie><uniqueid type="mteam">10001</uniqueid>'
                       '<screenshots source="AVSubscribe"><screenshot>ABC-123-1.png</screenshot></screenshots>'
                       '</movie>', encoding="utf-8")
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute(
                    """INSERT INTO local_video_screenshots(directory, code, position, source_url, filename, large_checked)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (str(video.parent), "ABC-123", 1, "https://example.com/shot", legacy.name, 1),
                )
        finally:
            connection.close()
        self.assertEqual(app.get_local_screenshot_image("a", "ABC-123", 1), ("image/png", PNG))

        renamed.write_bytes(b"external file")
        with patch.object(cover_images, "fetch_image") as fetch:
            self.sync("a", rows)
            self.assertEqual(renamed.read_bytes(), b"external file")
            self.assertEqual(legacy.read_bytes(), PNG)
            self.assertEqual(ET.parse(nfo).findtext("screenshots/screenshot"), legacy.name)
            renamed.unlink()
            self.sync("a", rows)
            self.sync("a", rows)
        fetch.assert_not_called()
        self.assertFalse(legacy.exists())
        self.assertEqual(renamed.read_bytes(), PNG)
        self.assertEqual(app.get_local_screenshot_image("a", "ABC-123", 1), ("image/png", PNG))
        self.assertEqual(ET.parse(nfo).findtext("screenshots/screenshot"), renamed.name)

    def test_azure_settings_hide_and_preserve_saved_key(self):
        public = app.save_settings({"azure_key": "test-key", "azure_region": " EastAsia "})
        self.assertTrue(public["azure_key_configured"])
        self.assertEqual(public["azure_region"], "eastasia")
        self.assertNotIn("azure_key", public)
        app.save_settings({"azure_key": ""})
        self.assertEqual(app.get_settings()["azure_key"], "test-key")
        with self.assertRaises(app.AppError):
            app.save_settings({"azure_region": "https://example.com"})
        self.assertFalse(app.save_settings({"clear_azure_key": True})["azure_key_configured"])
        self.assertEqual(app.get_settings()["azure_key"], "")

    def test_legacy_dmm_cache_backfills_originals_and_translates_only_changed_fields(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        self.records["a"]["ABC-123"]["local_files"][str(path)]["dmm_code"] = DMM_CODE
        source = {"baseInfo": {"ジャンル": "映画"}, "series": "シリーズ名", "textData": "旧説明", "title": "映画名",
                  "picData": "https://example.com/shot"}
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute("ALTER TABLE local_video_covers DROP COLUMN dmm_code")
                for column in ("base_info_json", "series", "text_data", "base_info_zh_json", "series_zh", "text_data_zh",
                               "hashtag_translation_version", "title", "title_zh", "response_json"):
                    connection.execute(f"ALTER TABLE dmm_screenshot_catalog DROP COLUMN {column}")
                connection.execute("INSERT INTO dmm_screenshot_catalog VALUES (?, ?, ?)",
                                   (DMM_CODE, json.dumps([source["picData"]]), 0))
        finally:
            connection.close()

        def translate(fields, _key, _region):
            labels = {"base_info": {"类型": "电影"}, "series": "系列名称", "text_data": "中文" + fields.get("text_data", ""),
                      "title": "电影名称"}
            return {name: labels[name] for name in fields}

        config = {"mteam_token": "token", "azure_key": "test-key", "azure_region": "eastasia"}
        sidecar = path.with_suffix(".nfo")
        sidecar.write_text('<movie><title>旧标题</title><plot>旧简介</plot><uniqueid type="mteam">10001</uniqueid><!--keep--></movie>', encoding="utf-8")
        with patch.object(app, "mteam_json", return_value={"data": source}) as dmm, \
             patch.object(azure_translator, "translate_metadata", side_effect=translate) as request, \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows, mteam_token="token")
            self.assertEqual(app.get_local_gallery("a", "ABC-123")["metadata"]["text_data"], "旧説明")
            self.assertEqual(ET.parse(sidecar).findtext("title"), "旧标题")
            request.assert_not_called()
            self.sync("a", rows, **config)
            first_write = sidecar.stat().st_mtime_ns
            self.sync("a", rows, **config)
            self.assertEqual(sidecar.stat().st_mtime_ns, first_write)
            dmm.assert_called_once()
            request.assert_called_once_with({"base_info": {"ジャンル": "映画"}, "series": "シリーズ名", "text_data": "旧説明", "title": "映画名"},
                                            "test-key", "eastasia")
            source["textData"] = "新説明"
            app.sync_subscription_covers(self.subscriptions[0], rows, self.records, config, refresh=True)
            request.assert_called_with({"text_data": "新説明"}, "test-key", "eastasia")
            self.assertEqual(request.call_count, 2)
        gallery = app.get_local_gallery("a", "ABC-123")
        self.assertEqual(gallery["metadata"], {"base_info": {"类型": "电影"}, "series": "系列名称", "text_data": "中文新説明",
                                               "title": "ABC-123 电影名称"})
        self.assertEqual(len(gallery["screenshots"]), 1)
        self.assertEqual(app.get_local_gallery("b", "ABC-123"), {"metadata": {}, "screenshots": []})
        connection = app.connect_video_database()
        try:
            contents = connection.execute("SELECT series, series_zh, text_data, text_data_zh, title, title_zh FROM dmm_screenshot_catalog").fetchone()
            self.assertEqual(contents, ("シリーズ名", "系列名称", "新説明", "中文新説明", "映画名", "电影名称"))
        finally:
            connection.close()
        updated = ET.parse(sidecar)
        self.assertEqual(updated.findtext("title"), "ABC-123 电影名称")
        self.assertEqual(updated.findtext("plot"), "中文新説明")
        self.assertEqual(updated.find("uniqueid").attrib, {"type": "mteam"})
        self.assertEqual(updated.findtext("uniqueid"), "10001")
        self.assertIn("<!--keep-->", sidecar.read_text(encoding="utf-8"))

    def test_explicit_repair_updates_cached_actress_names_in_gallery_and_nfo(self):
        rows, path = self.index("a", "START-463", "10001", [1, 2, 1], "https://example.com/cover")
        self.records["a"]["START-463"]["local_files"][str(path)]["dmm_code"] = DMM_CODE
        config = {"mteam_token": "token", "azure_key": "key"}
        source = {"actressList": ["彩月七緒"], "title": "彩月七緒 出演作品", "textData": "彩月七緒の作品",
                  "baseInfo": {"出演者": "彩月七緒"}, "series": "シリーズ名"}
        with patch.object(app, "mteam_json", return_value={"data": source}), \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))), \
             patch.object(azure_translator, "translate_batch", return_value=["齐月七尾", "系列", "绫木直简介", "绫槻直标题"]):
            self.sync("a", rows, **config)
        app.save_actress_translation({"source_name": "彩月七緒", "chinese_name": "彩月七绪"})
        gallery = app.get_local_gallery("a", "START-463")["metadata"]
        self.assertEqual(gallery["base_info"]["演员"], "齐月七尾")
        with patch.object(app, "mteam_json", return_value={"data": source}), \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))), \
             patch.object(azure_translator, "translate_batch",
                          side_effect=lambda texts, *_: [text.replace("の作品", "的作品").replace("出演作品", "主演作品") for text in texts]) as translate:
            self.sync("a", rows, **config)
            translate.assert_not_called()
            app.sync_subscription_covers(self.subscriptions[0], rows, self.records, config, refresh=True,
                                         allowed_sources={("a", str(path))})
        translate.assert_called_once()
        self.assertEqual(len(translate.call_args.args[0]), 2)
        gallery = app.get_local_gallery("a", "START-463")["metadata"]
        self.assertEqual(gallery["title"], "START-463 彩月七绪 主演作品")
        self.assertEqual(gallery["text_data"], "彩月七绪的作品")
        self.assertEqual(gallery["base_info"]["演员"], "彩月七绪")
        self.assertEqual(gallery["series"], "系列")
        nfo = ET.parse(path.with_suffix(".nfo"))
        self.assertEqual(nfo.findtext("title"), gallery["title"])
        self.assertEqual(nfo.findtext("plot"), gallery["text_data"])

    def test_nfo_sync_respects_file_scope_and_preserves_invalid_nfo(self):
        _, first = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        rows, second = self.index("a", "ABC-123", "10002", [2, 2, 1], "https://example.com/cover")
        for path in (first, second):
            self.records["a"]["ABC-123"]["local_files"][str(path)]["dmm_code"] = DMM_CODE
        self.records["a"]["ABC-123"]["local_files"][str(first)]["mediainfo"] = "Video\nFormat : AVC\nWidth : 1 920 pixels\nHeight : 1 080 pixels"
        config = {"mteam_token": "token", "azure_key": "test-key"}
        source = {"title": "映画", "textData": "説明", "series": "シリーズ名",
                  "baseInfo": {"ジャンル": "分類", "品番": "abc123", "収録時間": "150分", "発売日": "2018/05/19", "出演者": "出演者 A"}}
        with patch.object(app, "mteam_json", return_value={"data": source}), \
             patch.object(azure_translator, "translate_metadata", return_value={"title": "电影", "text_data": "简介"}), \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            app.sync_subscription_covers(self.subscriptions[0], rows, self.records, config, allowed_sources={("a", str(first))})
            self.assertFalse(second.with_suffix(".nfo").exists())
            root = ET.parse(first.with_suffix(".nfo"))
            self.assertEqual(root.findtext("title"), "ABC-123 电影")
            self.assertEqual(root.findtext("originaltitle"), "映画")
            self.assertEqual(root.findtext("plot"), "简介")
            self.assertEqual(root.findtext("set/name"), "シリーズ名")
            self.assertEqual(root.findtext("premiered"), "2018-05-19")
            self.assertEqual(root.findtext("runtime"), "150")
            self.assertEqual(root.findtext("actor/name"), "出演者 A")
            self.assertEqual(root.findtext("uniqueid[@type='mteam']"), "10001")
            self.assertEqual(root.findtext("fileinfo/streamdetails/video/height"), "1080")
            second_nfo = second.with_suffix(".nfo")
            second_nfo.write_text("<invalid", encoding="utf-8")
            self.sync("a", rows, **config)
        self.assertEqual(second_nfo.read_text(encoding="utf-8"), "<invalid")
        self.assertTrue(any(entry["event"] == "local_nfo_sync" and entry["level"] == "warning" for entry in app.get_logs()))

    def test_creates_nfo_from_bound_mteam_metadata_without_azure(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1])
        self.records["a"]["ABC-123"]["local_files"][str(path)]["title"] = "ABC-123 M-Team 标题"
        self.sync("a", rows)
        generated = ET.parse(path.with_suffix(".nfo"))
        self.assertEqual(generated.findtext("title"), "ABC-123 M-Team 标题")
        self.assertEqual(generated.findtext("uniqueid[@type='mteam']"), "10001")

    def test_replaces_foreign_nfo_with_complete_app_nfo_and_keeps_it_on_next_check(self):
        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "")
        metadata = self.records["a"]["ABC-123"]["local_files"][str(video)]
        metadata.update(title="M-Team 标题", dmm_code=DMM_CODE)
        nfo = video.with_suffix(".nfo")
        nfo.write_text('<movie><title>旧标题</title><uniqueid type="dmm">old</uniqueid>'
                       '<uniqueid type="mteam">invalid</uniqueid><director>旧导演</director></movie>', encoding="utf-8")
        data = {"title": "DMM 标题", "textData": "DMM 简介", "baseInfo": {"出演者": "演员"}}
        with patch.object(app, "mteam_json", return_value={"data": data}) as request:
            self.sync("a", rows, mteam_token="token")
            generated = ET.parse(nfo)
            self.assertEqual(generated.findtext("title"), "ABC-123 DMM 标题")
            self.assertEqual(generated.findtext("plot"), "DMM 简介")
            self.assertEqual(generated.findtext("actor/name"), "演员")
            self.assertEqual(generated.findtext("uniqueid[@type='mteam']"), "10001")
            self.assertIsNone(generated.find("director"))
            self.assertIsNone(generated.find("uniqueid[@type='dmm']"))

            written = nfo.stat().st_mtime_ns
            self.sync("a", rows, mteam_token="token")
            self.assertEqual(nfo.stat().st_mtime_ns, written)
            request.assert_called_once()

    def test_keeps_foreign_nfo_until_replacement_data_and_write_are_available(self):
        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "")
        metadata = self.records["a"]["ABC-123"]["local_files"][str(video)]
        nfo = video.with_suffix(".nfo")
        old = "<movie><title>旧标题</title></movie>".encode("utf-8")
        nfo.write_bytes(old)
        self.sync("a", rows)
        self.assertEqual(nfo.read_bytes(), old)

        metadata.update(title="M-Team 标题", dmm_code=DMM_CODE)
        with patch.object(app, "mteam_json", side_effect=app.AppError("DMM unavailable")):
            self.sync("a", rows, mteam_token="token")
        self.assertEqual(nfo.read_bytes(), old)

        with patch.object(Path, "replace", side_effect=PermissionError("无法替换")):
            self.sync("a", rows)
        self.assertEqual(nfo.read_bytes(), old)
        self.assertEqual(list(video.parent.glob(".nfo-*")), [])

    def test_retranslates_cached_hashtags_once_without_refreshing_unrelated_fields(self):
        connection = app.connect_video_database()
        try:
            old_base = {"関連タグ": "#衣装 #AV"}
            with connection:
                connection.execute(
                    """INSERT INTO dmm_screenshot_catalog (dmm_code, urls_json, checked_at, base_info_json, series, series_zh, base_info_zh_json)
                       VALUES (?, '[]', 0, ?, ?, ?, ?)""",
                    (DMM_CODE, json.dumps(old_base), "サンプル", "样例", json.dumps({"相关标签": "#衣装 #AV"})),
                )
            translate = Mock(return_value={"base_info": {"相关标签": "#服装 #AV"}})
            app.translate_dmm_catalog(connection, DMM_CODE, translate)
            app.translate_dmm_catalog(connection, DMM_CODE, translate)
            translate.assert_called_once_with({"base_info": old_base})
            translated_base, series = connection.execute("SELECT base_info_zh_json, series_zh FROM dmm_screenshot_catalog").fetchone()
            self.assertEqual(json.loads(translated_base), {"相关标签": "#服装 #AV"})
            self.assertEqual(series, "样例")
        finally:
            connection.close()

    def test_json_debug_logs_mask_credentials_and_bound_the_output(self):
        raw = json.dumps({"data": {"name": "ABC-123 Example", "passkey": "private-passkey",
                                  "url": "https://example.com/asset?id=hidden-query",
                                  "message": "echoed test-secret", "description": "x" * 16000}}).encode()
        app.log_http_json("M-Team", "/torrent/detail?credential=request-secret", 200, raw, settings={"azure_key": "test-secret"})
        self.assertEqual(app.get_logs(), [])
        app.log_http_json("M-Team", "/torrent/detail?credential=request-secret&id=10001", 200, raw,
                          request_payload={"nested": {"api_token": "body-token", "title": "echoed test-secret"}},
                          settings={"debug_mode": True, "azure_key": "test-secret"})
        entry = app.get_logs()[0]
        self.assertEqual((entry["level"], entry["event"]), ("debug", "debug_http_response"))
        self.assertIn("M-Team /torrent/detail · HTTP 200 · ABC-123", entry["message"])
        self.assertIn("已截断", entry["message"])
        self.assertLessEqual(len(entry["message"]), 12000)
        parameters = json.loads(entry["message"].split("\n", 2)[1].removeprefix("请求参数："))
        self.assertEqual(parameters, {
            "query": {"credential": "[已隐藏]", "id": "10001"},
            "body": {"nested": {"api_token": "[已隐藏]", "title": "echoed [已隐藏]"}},
        })
        for secret in ("private-passkey", "hidden-query", "test-secret", "request-secret", "body-token"):
            self.assertNotIn(secret, entry["message"])
        app.log_http_json("M-Team", "/torrent/genDlToken", 200, b'{"data":"opaque-secret"}', settings={"debug_mode": True})
        self.assertNotIn("opaque-secret", app.get_logs()[-1]["message"])

    def test_debug_logs_mteam_and_qbittorrent_json_responses(self):
        app.write_json(app.SETTINGS_FILE, {"debug_mode": True, "mteam_token": "mteam-secret"})
        headers = Message()
        headers["Content-Type"] = "application/json"
        response = Mock(status=200, headers=headers)
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *_: False
        response.geturl.return_value = "https://api.m-team.cc/api/torrent/detail"
        with patch.object(app.time, "perf_counter", return_value=10.0) as clock, \
             patch.object(app, "wait_for_mteam_request") as pace, \
             patch.object(app.MTEAM_OPENER, "open", return_value=response):
            def read_detail(_limit):
                clock.return_value = 16.25
                return b'{"code":0,"data":{"name":"ABC-123","id":"10001"}}'

            pace.side_effect = lambda: setattr(clock, "return_value", 15.0)
            response.read.side_effect = read_detail
            app.mteam_json("/torrent/detail", "mteam-secret", {"id": "10001"}, multipart=True)
        qb = app.Downloader({"url": "http://qb.test", "password": "qb-secret"})
        qb.cookies["SID"] = "session-secret"
        ok = io.BytesIO(b'[{"name":"ABC-123","status":"working"}]')
        ok.status = 200
        error = urllib.error.HTTPError("http://qb.test/api/v2/torrents/info", 429, "", headers,
                                       io.BytesIO(b'{"error":"try again session-secret"}'))
        with patch.object(app.urllib.request, "urlopen", side_effect=[ok, error]), \
             patch.object(app.time, "perf_counter", side_effect=[20.0, 20.235, 21.0, 21.1, 22.0, 22.025]):
            qb._json("/api/v2/torrents/info?hashes=abc%7Cdef")
            with self.assertRaisesRegex(app.AppError, "429"):
                qb._json("/api/v2/torrents/info?rid=7", {"password": "request-password"}, form=True)
        logs = [entry["message"] for entry in app.get_logs()]
        self.assertEqual(logs[0].split("\n", 1)[0], "M-Team /torrent/detail · HTTP 200 · ABC-123 · 耗时 1.25 秒")
        self.assertEqual(logs[1].split("\n", 1)[0], "qBittorrent /api/v2/torrents/info · HTTP 200 · ABC-123 · 耗时 235 ms")
        self.assertEqual(logs[2].split("\n", 1)[0], "qBittorrent /api/v2/torrents/info · HTTP 429 · 耗时 125 ms")
        self.assertIn('请求参数：{"id": "10001"}\n', logs[0])
        self.assertIn('请求参数：{"hashes": "abc|def"}\n', logs[1])
        self.assertIn('请求参数：{"query": {"rid": "7"}, "body": {"password": "[已隐藏]"}}\n', logs[2])
        self.assertNotIn("request-password", logs[2])
        self.assertNotIn("session-secret", logs[2])

    def test_azure_json_response_log_includes_the_number(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        self.records["a"]["ABC-123"]["local_files"][str(path)]["dmm_code"] = DMM_CODE
        response = Mock(status=200)
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *_: False
        response.read.return_value = b'[{"translations":[{"text":"translated","to":"zh-Hans"}]}]'
        with patch.object(app, "mteam_json", return_value={"data": {"textData": "テスト"}}), \
             patch.object(azure_translator.OPENER, "open", return_value=response), \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows, mteam_token="token", azure_key="test-key", debug_mode=True)
        logs = [entry for entry in app.get_logs() if entry["event"] == "debug_http_response"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["subscription"], "Actress A")
        self.assertRegex(logs[0]["message"], r"^Azure /translate · HTTP 200 · ABC-123 · 耗时 (?:\d+ ms|\d+\.\d{2} 秒)\n")
        self.assertIn('请求参数：{"query": {"api-version": "3.0", "from": "ja", "to": "zh-Hans"}, "body": [{"Text": "テスト"}]}\n', logs[0]["message"])
        self.assertIn('"text": "translated"', logs[0]["message"])

    def test_debug_logs_show_artwork_steps_and_screenshot_fallback(self):
        cover = "https://example.com/cover?token=private-value"
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1], cover)
        metadata = self.records["a"]["ABC-123"]["local_files"][str(path)]
        metadata.update(title="Example", screenshot_urls=[f"{DMM_SAMPLES}-1.jpg"])
        with patch.object(app.time, "perf_counter", return_value=10.0) as clock, \
             patch.object(cover_images, "crop_poster", return_value=PNG), \
             patch.object(cover_images, "fetch_image") as fetch:
            def download(url):
                if url == cover:
                    clock.return_value += 1.25
                elif url == f"{DMM_SAMPLES}jp-1.jpg":
                    clock.return_value += 2.0
                    raise cover_images.ImageHTTPError(404)
                else:
                    clock.return_value += 0.5
                return ImageResponse((".png", PNG))

            fetch.side_effect = download
            self.sync("a", rows, debug_mode=True)
        logs = [entry for entry in app.get_logs() if entry["event"] == "debug_artwork"]
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["message"], "ABC-123 开始处理本地资料（1 个目录）。")
        self.assertEqual((logs[1]["level"], logs[1]["subscription"]), ("debug", "Actress A"))
        summary = logs[1]["message"]
        for detail in ("处理耗时 3.75 秒", "封面请求 1.25 秒", "封面核对与保存", "海报裁切", "NFO 核对与写入",
                       "NFO 更新 1 个", "截图大图请求 2.00 秒", "回退请求小图 0.50 秒", "截图写入"):
            self.assertIn(detail, summary)
        self.assertNotIn("private-value", summary)

        with patch.object(cover_images, "fetch_image") as fetch:
            self.sync("a", rows)
            self.assertEqual(len([entry for entry in app.get_logs() if entry["event"] == "debug_artwork"]), 2)
            self.sync("a", rows, debug_mode=True)
        fetch.assert_not_called()
        cached = [entry["message"] for entry in app.get_logs() if entry["event"] == "debug_artwork"][-1]
        self.assertIn("截图保存 0 张、复用 1 张", cached)
        self.assertNotIn("截图大图请求", cached)

    def test_azure_failure_keeps_originals_and_continues_saving_screenshots(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        self.records["a"]["ABC-123"]["local_files"][str(path)]["dmm_code"] = DMM_CODE
        with patch.object(app, "mteam_json", return_value={"data": {"picData": "https://example.com/shot", "textData": "原文"}}), \
             patch.object(azure_translator, "translate_metadata", side_effect=azure_translator.TranslationError("Azure 翻译请求失败（HTTP 429）。", True)), \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows, mteam_token="token", azure_key="test-key")
        gallery = app.get_local_gallery("a", "ABC-123")
        self.assertEqual(gallery["metadata"]["text_data"], "原文")
        self.assertEqual(len(gallery["screenshots"]), 1)
        self.assertTrue(any(entry["event"] == "dmm_translation_failed" for entry in app.get_logs()))

    def test_each_subscription_saves_highest_live_cover_beside_its_video(self):
        rows_a, _ = self.index("a", "ABC-123", "10001", [1, 0, 1], "https://example.com/low")
        rows_b, _ = self.index("b", "ABC-123", "10002", [2, 0, 2], "https://example.com/high")
        rows_b, missing = self.index("b", "ABC-123", "10003", [2, 2, 2], "https://example.com/deleted")
        missing.unlink()
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))) as fetch:
            self.sync("a", rows_a)
            self.sync("b", rows_b)
            self.assertEqual(fetch.call_args_list, [unittest.mock.call("https://example.com/high")] * 2)
        self.assertEqual(sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("fansrt.png")), ["a/ABC-123/fansrt.png", "b/ABC-123/fansrt.png"])
        for subscription, rows in (("a", rows_a), ("b", rows_b)):
            app.attach_local_cover_addresses(subscription, rows)
            self.assertIn(f"/{subscription}/videos/ABC-123/cover-image", rows[0]["cover_image"])
            self.assertEqual(app.get_local_cover_image(subscription, "ABC-123"), ("image/png", PNG))
        self.assertIsNone(app.get_local_cover_image("missing", "ABC-123"))
        app.write_json(app.RECORDS_FILE, self.records)
        videos = app.get_subscription_videos("a")["videos"]
        self.assertIn("cover_image", videos[0])

    def test_replacement_failure_retains_old_cover_and_other_numbers_continue(self):
        rows, _ = self.index("a", "ABC-123", "10001", [1, 0, 1], "https://example.com/low")
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows)
        self.index("a", "ABC-123", "10002", [2, 0, 1], "https://example.com/high")
        rows, _ = self.index("a", "DEF-123", "20001", [1, 0, 1], "https://example.com/other")

        def fetch(url):
            if url.endswith("high"):
                raise ValueError("invalid image")
            return ImageResponse((".gif", GIF))

        with patch.object(cover_images, "fetch_image", side_effect=fetch):
            self.sync("a", rows)
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
        self.assertEqual(app.get_local_cover_image("a", "DEF-123"), ("image/gif", GIF))
        self.assertIn("失败 1 张", [log["message"] for log in app.get_logs() if log["event"] == "local_cover_sync"][-1])

        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".gif", GIF))), \
             patch.object(Path, "replace", side_effect=PermissionError):
            self.sync("a", rows)
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".gif", GIF))):
            self.sync("a", rows)
        self.assertFalse((self.root / "a" / "ABC-123" / "fansrt.png").exists())
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/gif", GIF))

    def test_uses_bound_detail_and_ignores_old_cover_directory(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 0, 1])
        self.index("b", "ABC-123", "10002", [2, 0, 1])
        detail = {"name": "ABC-123 Actress A", "imageList": ["https://example.com/cover"]}
        with patch.object(app, "mteam_detail", return_value=detail) as detail_api, \
             patch.object(app, "mteam_search", return_value=[]), \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))) as fetch:
            self.sync("a", rows, mteam_token="token")
            fetch.assert_called_once_with("https://example.com/cover")
            self.assertEqual([args.args[0] for args in detail_api.call_args_list], ["10002", "10001"])
        self.assertTrue(self.records["a"]["ABC-123"]["local_files"][str(path)]["cover_checked"])
        legacy = self.default_dir / "ABC-123"
        legacy.mkdir(parents=True)
        (legacy / "Cover.gif").write_bytes(GIF)
        app.write_json(app.SETTINGS_FILE, {"cover_dir": str(self.default_dir)})
        with patch.object(cover_images, "fetch_image") as fetch:
            self.sync("a", rows)
        fetch.assert_not_called()
        self.assertTrue((legacy / "Cover.gif").exists())
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))

    def test_cgi_only_serves_registered_images_at_the_current_directory(self):
        rows, _ = self.index("a", "ABC-123", "10001", [1, 0, 1], "https://example.com/cover")
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows)
        path = "/api/subscriptions/a/videos/ABC-123/cover-image"
        with patch.dict(os.environ, {"PATH_INFO": path, "REQUEST_METHOD": "GET", "QUERY_STRING": "v=123"}), patch.object(app, "emit") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", "image/png", PNG, cache_control="private, max-age=31536000, immutable")
        self.assertEqual(app.versioned_image_cache_control("v=123"), "private, max-age=31536000, immutable")
        self.assertEqual(app.versioned_image_cache_control(""), "no-store")
        outside = self.root / "outside.png"
        outside.write_bytes(PNG)
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute("UPDATE local_video_covers SET filename = '../outside.png'")
        finally:
            connection.close()
        self.assertIsNone(app.get_local_cover_image("a", "ABC-123"))
        self.assertIsNone(app.get_local_cover_image("a", "../outside"))

    def test_manual_and_scheduled_checks_download_after_indexing(self):
        self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        (self.root / "b").mkdir()
        app.write_json(app.RECORDS_FILE, self.records)
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "test-token", "downloader": {"url": "http://qb.test"}})
        with patch.object(app, "mteam_search", return_value=[]), \
             patch.object(app.Downloader, "list_tasks", return_value={}), \
             patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))) as fetch:
            for trigger in ("manual", "scheduled"):
                with self.subTest(trigger=trigger):
                    file = self.root / "a" / "ABC-123" / "fansrt.png"
                    file.unlink(missing_ok=True)
                    result = app.run_poll(trigger, "a" if trigger == "manual" else "")
                    self.assertEqual(result["state"], "completed", result["message"])
                    self.assertEqual(file.read_bytes(), PNG)
            self.assertEqual(fetch.call_count, 2)

    def test_dmm_gallery_uses_pic_data_and_cached_list_to_restore_a_missing_file(self):
        rows, path = self.index("a", "SSNI-208", "287533", [1, 2, 1], DMM_COVER)
        metadata = self.records["a"]["SSNI-208"]["local_files"][str(path)]
        metadata.pop("dmm_code")
        detail = {"name": "SSNI-208 Actress A", "imageList": [DMM_COVER], "dmmCode": DMM_CODE}
        first, second = (f"{DMM_SAMPLES}-{index}.jpg" for index in (1, 2))
        first_large, second_large = (f"{DMM_SAMPLES}jp-{index}.jpg" for index in (1, 2))
        jpeg = b"\xff\xd8\xff\xe0" + b"test-data" + b"\xff\xd9"

        def download(url):
            return ImageResponse((".png", PNG) if url == DMM_COVER else (".jpg", jpeg))

        with patch.object(app, "mteam_detail", return_value=detail) as detail_api, \
             patch.object(app, "mteam_json", return_value={"data": {"picData": f"{first}, {second},{first}"}}) as dmm_api, \
             patch.object(cover_images, "fetch_image", side_effect=download) as fetch:
            self.sync("a", rows, mteam_token="token")
            self.assertEqual([args.args[0] for args in fetch.call_args_list], [DMM_COVER, first_large, second_large])
            directory = self.root / "a" / "SSNI-208"
            self.assertEqual(sorted(image.name for image in directory.iterdir() if image.suffix in cover_images.IMAGE_TYPES), ["fansrt.png", "poster.png", "screenshot-1.jpg", "screenshot-2.jpg"])
            self.sync("a", rows, mteam_token="token")
            self.assertEqual(fetch.call_count, 3)
            (directory / "screenshot-1.jpg").unlink()
            self.sync("a", rows, mteam_token="token")
            self.assertEqual((directory / "screenshot-1.jpg").read_bytes(), jpeg)
            self.assertEqual(fetch.call_count, 4)
            fetch.assert_called_with(first_large)
            detail_api.assert_called_once_with("287533", "token")
            dmm_api.assert_called_once_with("/dmm/dmmInfo", "token", {"dmmCode": DMM_CODE}, multipart=True)
            self.assertEqual(metadata["dmm_code"], DMM_CODE)

    def test_cover_403_falls_back_to_dmm_full_cover_and_reuses_saved_source(self):
        detail_cover = "https://example.com/detail-cover.jpg"
        full_cover = "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ipzz00079/ipzz00079pl.jpg"
        rows, path = self.index("a", "IPZZ-079", "1294644", [1, 2, 2], detail_cover)
        self.records["a"]["IPZZ-079"]["local_files"][str(path)].update(
            dmm_code=DMM_CODE, dmm_detail_checked=True,
        )
        response = {"data": {"pic1": full_cover, "pic2": full_cover.replace("pl.jpg", "ps.jpg"), "picData": ""}}
        with patch.object(app, "mteam_json", return_value=response) as catalog, \
             patch.object(cover_images, "fetch_image", side_effect=[cover_images.ImageHTTPError(403), ImageResponse((".png", PNG))]) as fetch:
            self.sync("a", rows, mteam_token="token")
            self.sync("a", rows, mteam_token="token")
        self.assertEqual(fetch.call_args_list, [call(detail_cover), call(full_cover)])
        catalog.assert_called_once()
        self.assertEqual((path.parent / "fansrt.png").read_bytes(), PNG)
        self.assertTrue((path.parent / "poster.png").is_file())
        self.assertFalse(any(entry["event"] == "local_cover_write_failed" for entry in app.get_logs()))

    def test_missing_dmm_code_searches_same_number_alternate_mteam_ids(self):
        metadata = {"id": "1294644"}
        alternate_detail = {"dmmCode": DMM_CODE, "imageList": [DMM_COVER]}
        details = {}
        candidates = [
            {"id": "1294644", "code": "IPZZ-079"},
            {"id": "1295000", "code": "IPZZ-079"},
            {"id": "1295001", "code": "IPZZ-078"},
        ]
        with patch.object(app, "mteam_detail", side_effect=[{"name": "IPZZ-079 release"}, alternate_detail]) as detail, \
             patch.object(app, "mteam_search", return_value=candidates) as search:
            screenshots = app.select_screenshot_urls(
                [(metadata, "女优甲")], "token", details, lambda dmm_code: [DMM_SAMPLES + "-1.jpg"], "IPZZ-079",
            )
            self.assertEqual(metadata["dmm_code"], DMM_CODE)
            self.assertEqual(metadata["dmm_source_mteam_id"], "1295000")
            self.assertEqual(screenshots, ([DMM_SAMPLES + "-1.jpg"], DMM_CODE))
            app.select_screenshot_urls(
                [(metadata, "女优甲")], "token", details, lambda dmm_code: [DMM_SAMPLES + "-1.jpg"], "IPZZ-079",
            )
        search.assert_called_once_with("IPZZ-079", "token")
        detail.assert_has_calls([call("1294644", "token"), call("1295000", "token")])
        self.assertEqual(metadata["cover_url"], DMM_COVER)

    def test_missing_dmm_code_search_negative_result_is_cached_for_metadata(self):
        metadata = {"id": "1294644"}
        details = {}
        with patch.object(app, "mteam_detail", return_value={"name": "IPZZ-079 release"}), \
             patch.object(app, "mteam_search", return_value=[{"id": "1295000", "code": "IPZZ-079"}]) as search:
            app.read_image_detail(metadata, "女优甲", "token", details, "IPZZ-079")
            app.read_image_detail(metadata, "女优甲", "token", details, "IPZZ-079")
        search.assert_called_once_with("IPZZ-079", "token")

    def test_existing_thumbnails_upgrade_in_place_and_unavailable_large_images_are_cached(self):
        rows, path = self.index("a", "SSNI-208", "287533", [1, 2, 1])
        first, second = (f"{DMM_SAMPLES}-{index}.jpg" for index in (1, 2))
        self.records["a"]["SSNI-208"]["local_files"][str(path)]["screenshot_urls"] = [first, second]
        directory = self.root / "a" / "SSNI-208"
        directory.mkdir(parents=True, exist_ok=True)
        thumbnail = b"\xff\xd8\xff\xe0small-thumb\xff\xd9"
        original = b"\xff\xd8\xff\xe0large-image\xff\xd9"
        connection = app.connect_video_database()
        try:
            with connection:
                connection.executemany(
                    "INSERT INTO local_video_screenshots(directory, code, position, source_url, filename) VALUES (?, ?, ?, ?, ?)",
                    [(str(directory), "SSNI-208", i, url, f"SSNI-208-{i}.jpg") for i, url in enumerate((first, second), 1)],
                )
                connection.execute("ALTER TABLE local_video_screenshots DROP COLUMN large_checked")
        finally:
            connection.close()
        for position in (1, 2):
            (directory / f"SSNI-208-{position}.jpg").write_bytes(thumbnail)
        with patch.object(cover_images, "fetch_image", side_effect=[ImageResponse((".jpg", original)), cover_images.ImageHTTPError(404)]) as fetch:
            self.sync("a", rows)
            self.assertEqual([args.args[0] for args in fetch.call_args_list], [f"{DMM_SAMPLES}jp-1.jpg", f"{DMM_SAMPLES}jp-2.jpg"])
            self.sync("a", rows)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual((directory / "screenshot-1.jpg").read_bytes(), original)
        self.assertEqual((directory / "screenshot-2.jpg").read_bytes(), thumbnail)
        self.assertEqual(app.dmm_large_screenshot_url(f"{DMM_SAMPLES}jp-1.jpg"), "")
        self.assertEqual(app.dmm_large_screenshot_url(first.replace("pics.dmm.co.jp", "example.com")), "")

    def test_dmm_cdn_rechecks_previously_cached_thumbnails_once(self):
        rows, video = self.index("a", "SNOS-239", "1277084", [1, 2, 1], "")
        sample = "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/snos00239/snos00239"
        thumbnail_url = sample + "-1.jpg"
        original_url = sample + "jp-1.jpg"
        metadata = self.records["a"]["SNOS-239"]["local_files"][str(video)]
        metadata["screenshot_urls"] = [thumbnail_url]
        screenshot = video.parent / "screenshot-1.jpg"
        thumbnail = b"\xff\xd8\xff\xe0small-thumb\xff\xd9"
        original = b"\xff\xd8\xff\xe0large-image\xff\xd9"
        screenshot.write_bytes(thumbnail)
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute(
                    """INSERT INTO local_video_screenshots(directory, code, position, source_url, filename, large_checked)
                       VALUES (?, ?, ?, ?, ?, 1)""",
                    (str(video.parent), "SNOS-239", 1, thumbnail_url, screenshot.name),
                )
        finally:
            connection.close()
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".jpg", original))) as fetch:
            self.sync("a", rows)
            self.sync("a", rows)
        fetch.assert_called_once_with(original_url)
        self.assertEqual(screenshot.read_bytes(), original)
        self.assertEqual(app.dmm_large_screenshot_url(original_url), "")

    def test_gallery_lists_existing_registered_screenshots_and_serves_only_matching_subscription(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1])
        metadata = self.records["a"]["ABC-123"]["local_files"][str(path)]
        metadata["screenshot_urls"] = ["https://example.com/first", "https://example.com/second", "https://example.com/third"]
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows)
        (self.root / "a" / "ABC-123" / "screenshot-2.png").unlink()
        app.attach_local_cover_addresses("a", rows)
        self.assertTrue(rows[0]["has_screenshots"])
        self.assertNotIn("cover_image", rows[0])
        gallery = app.get_local_screenshots("a", "ABC-123")
        self.assertEqual([image["position"] for image in gallery], [1, 3])
        self.assertEqual(rows[0]["screenshot_thumbnail"], gallery[0]["url"])
        self.assertEqual(app.get_local_screenshots("b", "ABC-123"), [])
        image_url = gallery[-1]["url"]
        with patch.dict(os.environ, {"PATH_INFO": image_url.split("?", 1)[0], "QUERY_STRING": image_url.split("?", 1)[1], "REQUEST_METHOD": "GET"}), patch.object(app, "emit") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", "image/png", PNG, cache_control="private, max-age=31536000, immutable")
        self.assertIsNone(app.get_local_screenshot_image("b", "ABC-123", 3))
        self.assertIsNone(app.get_local_screenshot_image("a", "ABC-123", 2))

    def test_screenshot_download_returns_original_image_and_numbered_filename(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1])
        metadata = self.records["a"]["ABC-123"]["local_files"][str(path)]
        metadata["screenshot_urls"] = ["https://example.com/sample"]
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))):
            self.sync("a", rows)
        endpoint = "/api/subscriptions/a/videos/ABC-123/screenshots/1"
        variables = {"PATH_INFO": endpoint, "REQUEST_METHOD": "GET", "QUERY_STRING": "v=123&download=1"}
        output = io.BytesIO()
        with io.TextIOWrapper(output, encoding="utf-8", newline="") as stream:
            with patch.dict(os.environ, variables), patch.object(app.sys, "stdout", stream):
                app.handle_request()
            headers, content = output.getvalue().split(b"\r\n\r\n", 1)
        self.assertIn(b'Content-Disposition: attachment; filename="ABC-123-screenshot-1.png"', headers)
        self.assertIn(b"Content-Type: image/png", headers)
        self.assertEqual(content, PNG)
        with patch.dict(os.environ, {**variables, "PATH_INFO": endpoint.replace("/a/", "/b/")}), \
             patch.object(app, "emit_json") as emit_json:
            app.handle_request()
        emit_json.assert_called_once_with("404 Not Found", {"error": "找不到这张本地截图。"})

    def test_local_cover_download_keeps_full_cover_and_resolves_scoped_native_path(self):
        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/full")
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))), \
             patch.object(cover_images, "crop_poster", return_value=PNG):
            self.sync("a", rows)
        cover_path = video.parent / "fansrt.png"
        gallery = app.get_local_gallery("a", "ABC-123")
        self.assertEqual(gallery["cover_path"], str(cover_path))
        self.assertEqual(gallery["poster_path"], str(video.parent / "poster.png"))
        self.assertNotIn("cover_path", app.get_local_gallery("b", "ABC-123"))
        env = {"PATH_INFO": "/api/subscriptions/a/videos/ABC-123/cover-image", "REQUEST_METHOD": "GET", "QUERY_STRING": "download=1"}
        with patch.dict(os.environ, env), patch.object(app, "emit") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", "image/png", PNG, download_filename="ABC-123-cover.png")

    def test_remote_cover_download_uses_indexed_video_metadata_and_ignores_supplied_url(self):
        _rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/full")
        app.write_json(app.RECORDS_FILE, self.records)
        endpoint = "/api/subscriptions/a/videos/ABC-123/cover-download"
        with patch.dict(os.environ, {"PATH_INFO": endpoint, "REQUEST_METHOD": "POST"}), \
             patch.object(app, "read_body", return_value={"path": str(video), "url": "https://example.com/ignored"}), \
             patch.object(cover_images, "fetch", return_value=(".png", PNG)) as fetch, patch.object(app, "emit") as emit:
            app.handle_request()
        fetch.assert_called_once_with("https://example.com/full")
        emit.assert_called_once_with("200 OK", "image/png", PNG, download_filename="ABC-123-cover.png")
        with self.assertRaisesRegex(app.AppError, "订阅目录"):
            app.get_subscription_cover_download("a", "ABC-123", str(self.root / "outside.mp4"))

    def test_gallery_failure_leaves_saved_screenshots_and_retries_only_failed_slot(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        first, second = "https://example.com/sample1", "https://example.com/sample2"
        metadata = self.records["a"]["ABC-123"]["local_files"][str(path)]
        metadata["screenshot_urls"] = [first, second]
        with patch.object(cover_images, "fetch_image", side_effect=[ImageResponse((".png", PNG)), TimeoutError, ImageResponse((".gif", GIF))]) as fetch:
            self.sync("a", rows)
        self.assertEqual([args.args[0] for args in fetch.call_args_list], ["https://example.com/cover", first, second])
        directory = self.root / "a" / "ABC-123"
        self.assertTrue((directory / "screenshot-2.gif").exists())
        with patch.object(cover_images, "fetch_image", return_value=ImageResponse((".png", PNG))) as fetch:
            self.sync("a", rows)
            fetch.assert_called_once_with(first)
        self.assertTrue((directory / "screenshot-1.png").exists())
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
