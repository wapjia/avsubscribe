import base64
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from email.message import Message
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import Mock, patch

from app.bin import app, azure_translator, cover_images


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
    def test_nfo_records_rightmost_cover_crop_and_screenshots_in_video_directory(self):
        from PIL import Image

        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        self.records["a"]["ABC-123"]["local_files"][str(video)]["screenshot_urls"] = ["https://example.com/shot"]
        nfo = video.with_suffix(".nfo")
        nfo.write_text('<movie><director>Director</director><uniqueid type="mteam">10001</uniqueid></movie>', encoding="utf-8")
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)):
            self.sync("a", rows)
        self.assertEqual((video.parent / "fansrt.png").read_bytes(), PNG)
        self.assertEqual((video.parent / "screenshot-1.png").read_bytes(), PNG)
        with Image.open(video.parent / "poster.png") as poster:
            self.assertEqual(poster.size, (2, 2))
            self.assertEqual(poster.getextrema(), ((0, 0), (0, 0), (255, 255)))
        app.attach_local_cover_addresses("a", rows)
        poster_url = rows[0]["poster_image"].split("?", 1)[0]
        self.assertEqual(poster_url, "/api/subscriptions/a/videos/ABC-123/poster-image")
        with patch.dict(os.environ, {"PATH_INFO": poster_url, "REQUEST_METHOD": "GET"}), patch.object(app, "emit") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", "image/png", (video.parent / "poster.png").read_bytes())
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
        self.assertIsNone(app.get_local_cover_image("b", "ABC-123", poster=True))
        metadata = ET.parse(nfo)
        self.assertEqual(metadata.findtext("fansrt"), "fansrt.png")
        self.assertEqual(metadata.findtext("poster"), "poster.png")
        self.assertEqual(metadata.findtext("screenshots/screenshot"), "screenshot-1.png")
        self.assertEqual(metadata.findtext("director"), "Director")

    def test_reuses_local_cover_and_regenerates_old_poster_once(self):
        rows, video = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)), \
             patch.object(cover_images, "crop_poster", return_value=PNG):
            self.sync("a", rows)
        connection = app.connect_video_database()
        try:
            with connection:
                connection.execute("ALTER TABLE local_video_covers DROP COLUMN poster_crop_version")
        finally:
            connection.close()
        with patch.object(cover_images, "fetch") as fetch, \
             patch.object(cover_images, "crop_poster", return_value=PNG) as crop:
            self.sync("a", rows)
            self.sync("a", rows)
        crop.assert_called_once_with(PNG, ".png")
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
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)), \
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
        with patch.object(cover_images, "fetch") as fetch:
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
                               "hashtag_translation_version", "title", "title_zh"):
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
             patch.object(cover_images, "fetch", return_value=(".png", PNG)):
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
                                               "title": "电影名称"})
        self.assertEqual(len(gallery["screenshots"]), 1)
        self.assertEqual(app.get_local_gallery("b", "ABC-123"), {"metadata": {}, "screenshots": []})
        connection = app.connect_video_database()
        try:
            contents = connection.execute("SELECT series, series_zh, text_data, text_data_zh, title, title_zh FROM dmm_screenshot_catalog").fetchone()
            self.assertEqual(contents, ("シリーズ名", "系列名称", "新説明", "中文新説明", "映画名", "电影名称"))
        finally:
            connection.close()
        updated = ET.parse(sidecar)
        self.assertEqual(updated.findtext("title"), "电影名称")
        self.assertEqual(updated.findtext("plot"), "中文新説明")
        self.assertEqual(updated.find("uniqueid").attrib, {"type": "mteam"})
        self.assertEqual(updated.findtext("uniqueid"), "10001")
        self.assertIn("<!--keep-->", sidecar.read_text(encoding="utf-8"))

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
             patch.object(cover_images, "fetch", return_value=(".png", PNG)):
            app.sync_subscription_covers(self.subscriptions[0], rows, self.records, config, allowed_sources={("a", str(first))})
            self.assertFalse(second.with_suffix(".nfo").exists())
            root = ET.parse(first.with_suffix(".nfo"))
            self.assertEqual(root.findtext("title"), "电影")
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
            self.assertEqual(generated.findtext("title"), "DMM 标题")
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
        app.log_http_json("M-Team", "/torrent/detail?credential=request-secret", 200, raw,
                          settings={"debug_mode": True, "azure_key": "test-secret"})
        entry = app.get_logs()[0]
        self.assertEqual((entry["level"], entry["event"]), ("debug", "debug_http_response"))
        self.assertIn("M-Team /torrent/detail · HTTP 200 · ABC-123", entry["message"])
        self.assertIn("已截断", entry["message"])
        self.assertLessEqual(len(entry["message"]), 12000)
        for secret in ("private-passkey", "hidden-query", "test-secret", "request-secret"):
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
        response.read.return_value = b'{"code":0,"data":{"name":"ABC-123","id":"10001"}}'
        with patch.object(app, "wait_for_mteam_request"), patch.object(app.MTEAM_OPENER, "open", return_value=response):
            app.mteam_json("/torrent/detail", "mteam-secret")
        qb = app.Downloader({"url": "http://qb.test", "password": "qb-secret"})
        qb.cookies["SID"] = "session-secret"
        ok = io.BytesIO(b'[{"name":"ABC-123","status":"working"}]')
        ok.status = 200
        error = urllib.error.HTTPError("http://qb.test/api/v2/torrents/info", 429, "", headers,
                                       io.BytesIO(b'{"error":"try again session-secret"}'))
        with patch.object(app.urllib.request, "urlopen", side_effect=[ok, error]):
            qb._json("/api/v2/torrents/info")
            with self.assertRaisesRegex(app.AppError, "429"):
                qb._json("/api/v2/torrents/info")
        logs = [entry["message"] for entry in app.get_logs()]
        self.assertIn("M-Team /torrent/detail · HTTP 200 · ABC-123", logs[0])
        self.assertIn("qBittorrent /api/v2/torrents/info · HTTP 200 · ABC-123", logs[1])
        self.assertIn("qBittorrent /api/v2/torrents/info · HTTP 429", logs[2])
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
             patch.object(cover_images, "fetch", return_value=(".png", PNG)):
            self.sync("a", rows, mteam_token="token", azure_key="test-key", debug_mode=True)
        logs = [entry for entry in app.get_logs() if entry["event"] == "debug_http_response"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["subscription"], "Actress A")
        self.assertIn("Azure /translate · HTTP 200 · ABC-123", logs[0]["message"])
        self.assertIn('"text": "translated"', logs[0]["message"])

    def test_azure_failure_keeps_originals_and_continues_saving_screenshots(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        self.records["a"]["ABC-123"]["local_files"][str(path)]["dmm_code"] = DMM_CODE
        with patch.object(app, "mteam_json", return_value={"data": {"picData": "https://example.com/shot", "textData": "原文"}}), \
             patch.object(azure_translator, "translate_metadata", side_effect=azure_translator.TranslationError("Azure 翻译请求失败（HTTP 429）。", True)), \
             patch.object(cover_images, "fetch", return_value=(".png", PNG)):
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
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)) as fetch:
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
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)):
            self.sync("a", rows)
        self.index("a", "ABC-123", "10002", [2, 0, 1], "https://example.com/high")
        rows, _ = self.index("a", "DEF-123", "20001", [1, 0, 1], "https://example.com/other")

        def fetch(url):
            if url.endswith("high"):
                raise ValueError("invalid image")
            return ".gif", GIF

        with patch.object(cover_images, "fetch", side_effect=fetch):
            self.sync("a", rows)
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
        self.assertEqual(app.get_local_cover_image("a", "DEF-123"), ("image/gif", GIF))
        self.assertIn("失败 1 张", [log["message"] for log in app.get_logs() if log["event"] == "local_cover_sync"][-1])

        with patch.object(cover_images, "fetch", return_value=(".gif", GIF)), \
             patch.object(Path, "replace", side_effect=PermissionError):
            self.sync("a", rows)
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
        with patch.object(cover_images, "fetch", return_value=(".gif", GIF)):
            self.sync("a", rows)
        self.assertFalse((self.root / "a" / "ABC-123" / "fansrt.png").exists())
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/gif", GIF))

    def test_uses_bound_detail_and_ignores_old_cover_directory(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 0, 1])
        self.index("b", "ABC-123", "10002", [2, 0, 1])
        detail = {"name": "ABC-123 Actress A", "imageList": ["https://example.com/cover"]}
        with patch.object(app, "mteam_detail", return_value=detail) as detail_api, \
             patch.object(cover_images, "fetch", return_value=(".png", PNG)) as fetch:
            self.sync("a", rows, mteam_token="token")
            fetch.assert_called_once_with("https://example.com/cover")
            self.assertEqual([args.args[0] for args in detail_api.call_args_list], ["10002", "10001"])
        self.assertTrue(self.records["a"]["ABC-123"]["local_files"][str(path)]["cover_checked"])
        legacy = self.default_dir / "ABC-123"
        legacy.mkdir(parents=True)
        (legacy / "Cover.gif").write_bytes(GIF)
        app.write_json(app.SETTINGS_FILE, {"cover_dir": str(self.default_dir)})
        with patch.object(cover_images, "fetch") as fetch:
            self.sync("a", rows)
        fetch.assert_not_called()
        self.assertTrue((legacy / "Cover.gif").exists())
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))

    def test_cgi_only_serves_registered_images_at_the_current_directory(self):
        rows, _ = self.index("a", "ABC-123", "10001", [1, 0, 1], "https://example.com/cover")
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)):
            self.sync("a", rows)
        path = "/api/subscriptions/a/videos/ABC-123/cover-image"
        with patch.dict(os.environ, {"PATH_INFO": path, "REQUEST_METHOD": "GET"}), patch.object(app, "emit") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", "image/png", PNG)
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
             patch.object(cover_images, "fetch", return_value=(".png", PNG)) as fetch:
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
            return (".png", PNG) if url == DMM_COVER else (".jpg", jpeg)

        with patch.object(app, "mteam_detail", return_value=detail) as detail_api, \
             patch.object(app, "mteam_json", return_value={"data": {"picData": f"{first}, {second},{first}"}}) as dmm_api, \
             patch.object(cover_images, "fetch", side_effect=download) as fetch:
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
        with patch.object(cover_images, "fetch", side_effect=[(".jpg", original), cover_images.ImageHTTPError(404)]) as fetch:
            self.sync("a", rows)
            self.assertEqual([args.args[0] for args in fetch.call_args_list], [f"{DMM_SAMPLES}jp-1.jpg", f"{DMM_SAMPLES}jp-2.jpg"])
            self.sync("a", rows)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual((directory / "screenshot-1.jpg").read_bytes(), original)
        self.assertEqual((directory / "screenshot-2.jpg").read_bytes(), thumbnail)
        self.assertEqual(app.dmm_large_screenshot_url(f"{DMM_SAMPLES}jp-1.jpg"), "")
        self.assertEqual(app.dmm_large_screenshot_url(first.replace("pics.dmm.co.jp", "example.com")), "")

    def test_gallery_lists_existing_registered_screenshots_and_serves_only_matching_subscription(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1])
        metadata = self.records["a"]["ABC-123"]["local_files"][str(path)]
        metadata["screenshot_urls"] = ["https://example.com/first", "https://example.com/second", "https://example.com/third"]
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)):
            self.sync("a", rows)
        (self.root / "a" / "ABC-123" / "screenshot-2.png").unlink()
        app.attach_local_cover_addresses("a", rows)
        self.assertTrue(rows[0]["has_screenshots"])
        self.assertNotIn("cover_image", rows[0])
        gallery = app.get_local_screenshots("a", "ABC-123")
        self.assertEqual([image["position"] for image in gallery], [1, 3])
        self.assertEqual(rows[0]["screenshot_thumbnail"], gallery[0]["url"])
        self.assertEqual(app.get_local_screenshots("b", "ABC-123"), [])
        path = gallery[-1]["url"].split("?", 1)[0]
        with patch.dict(os.environ, {"PATH_INFO": path, "REQUEST_METHOD": "GET"}), patch.object(app, "emit") as emit:
            app.handle_request()
        emit.assert_called_once_with("200 OK", "image/png", PNG)
        self.assertIsNone(app.get_local_screenshot_image("b", "ABC-123", 3))
        self.assertIsNone(app.get_local_screenshot_image("a", "ABC-123", 2))

    def test_gallery_failure_leaves_saved_screenshots_and_retries_only_failed_slot(self):
        rows, path = self.index("a", "ABC-123", "10001", [1, 2, 1], "https://example.com/cover")
        first, second = "https://example.com/sample1", "https://example.com/sample2"
        metadata = self.records["a"]["ABC-123"]["local_files"][str(path)]
        metadata["screenshot_urls"] = [first, second]
        with patch.object(cover_images, "fetch", side_effect=[(".png", PNG), TimeoutError, (".gif", GIF)]) as fetch:
            self.sync("a", rows)
        self.assertEqual([args.args[0] for args in fetch.call_args_list], ["https://example.com/cover", first, second])
        directory = self.root / "a" / "ABC-123"
        self.assertTrue((directory / "screenshot-2.gif").exists())
        with patch.object(cover_images, "fetch", return_value=(".png", PNG)) as fetch:
            self.sync("a", rows)
            fetch.assert_called_once_with(first)
        self.assertTrue((directory / "screenshot-1.png").exists())
        self.assertEqual(app.get_local_cover_image("a", "ABC-123"), ("image/png", PNG))
