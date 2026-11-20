import io
import json
import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from email.message import Message
from unittest.mock import patch

from app.bin import app


def torrent_bytes(name="demo"):
    return b"d4:infod4:name" + str(len(name.encode())).encode() + b":" + name.encode() + b"ee"


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def geturl(self):
        return "https://api.m-team.cc/api/torrent/search"


class FakeDownloader:
    def __init__(self, hash_value="newhash", complete=False):
        self.hash_value = hash_value
        self.complete = complete
        self.added = []
        self.removed = []

    def add(self, data, title, download_dir):
        self.added.append((data, title, download_dir))
        return self.hash_value

    def list_tasks(self, hashes=None):
        return {self.hash_value: {"progress": 1.0 if self.complete else .42, "complete": self.complete}}

    def remove(self, info_hash, delete_data=True):
        self.removed.append((info_hash, delete_data))


class ReleaseLogicTests(unittest.TestCase):
    def test_mteam_id_backfill_helpers_parse_comment_and_filter_trackers(self):
        self.assertEqual(app.mteam_id_from_torrent_comment("1294522"), "1294522")
        self.assertEqual(app.mteam_id_from_torrent_comment("https://kp.m-team.cc/detail/1289514"), "1289514")
        self.assertEqual(app.mteam_id_from_torrent_comment("M-Team ID: 1283063"), "1283063")
        self.assertEqual(app.mteam_id_from_torrent_comment("unrelated comment"), "")
        self.assertTrue(app.has_mteam_tracker([{"url": "https://tracker.M-Team.cc/announce"}]))
        self.assertTrue(app.has_mteam_tracker([{"url": "udp://bt1.manfuz.co:8080/announce"}]))
        self.assertFalse(app.has_mteam_tracker([{"url": "https://tracker.example.com/announce"}]))

    def test_normalizes_number_from_titles(self):
        self.assertEqual(app.normalize_code("三上悠亚 SSNI 618 4K"), "SSNI-618")
        self.assertEqual(app.normalize_code("IPX_811"), "IPX-811")
        self.assertEqual(app.normalize_code("SSIS–181"), "SSIS-181")
        self.assertEqual(app.normalize_code("SSIS.181"), "SSIS-181")
        self.assertEqual(app.normalize_code("FC2-PPV-1234567 1080p"), "FC2-PPV-1234567")
        self.assertEqual(app.normalize_code("1PONDO-010124"), "1PONDO-010124")
        self.assertEqual(app.normalize_code("White Cat Legend S03E11 1080p"), "")
        self.assertEqual(app.normalize_code("no release number"), "")

    def test_release_rank_is_lexicographic(self):
        self.assertGreater(app.release_rank("Title 4K x264"), app.release_rank("Title 1080p 中文 x265"))
        self.assertGreater(app.release_rank("Title 1080p 中文字幕 x264"), app.release_rank("Title 1080p x265"))
        self.assertGreater(app.release_rank("Title 1080p x265"), app.release_rank("Title 1080p x264"))

    def test_release_rank_recognizes_aliases_and_unmarked_tags_rank_lowest(self):
        self.assertEqual(app.release_rank("UHD H.265 中文字幕"), (2, 1, 2))
        self.assertEqual(app.release_rank("FHD H264"), (1, 0, 1))
        self.assertEqual(app.release_rank("release"), (0, 0, 0))

    def test_release_rank_reads_mteam_detail_resolution_and_video_codec_fields(self):
        self.assertEqual(app.release_rank("SNOS-047", {"resolution": "1080p", "videoCodec": "H.264(x264/AVC)"}), (1, 0, 1))
        self.assertEqual(app.release_rank("SNOS-047", {"standardTag": [{"name": "2160p"}], "video_codec": "H.265(x265/HEVC)"}), (2, 0, 2))

    def test_chinese_release_rank_prefers_embedded_over_subtitles_over_non_chinese(self):
        embedded = app.release_rank("SSNI-939", {"labelsNew": ["中字"], "hasChineseSubtitle": False})
        subtitles = app.release_rank("SSNI-618", {"labelsNew": [], "hasChineseSubtitle": True})
        non_chinese = app.release_rank("IPX-811", {"labelsNew": [], "hasChineseSubtitle": False})
        self.assertEqual((embedded[1], subtitles[1], non_chinese[1]), (2, 1, 0))
        self.assertGreater(embedded, subtitles)
        self.assertGreater(subtitles, non_chinese)

    def test_embedded_chinese_recognizes_traditional_mteam_metadata(self):
        embedded = app.release_rank("SSIS-181", {"subtitleType": "內嵌中字"})
        subtitles = app.release_rank("SSNI-618", {"subtitleType": "中文字幕"})
        self.assertEqual((embedded[1], subtitles[1]), (2, 1))
        self.assertGreater(embedded, subtitles)

    def test_mteam_chinese_rank_uses_labels_new_and_has_chinese_subtitle_fields(self):
        embedded = app.release_rank("SSIS-181", {"labelsNew": [{"name": "中字", "color": "green"}], "hasChineseSubtitle": True})
        subtitles = app.release_rank("SSNI-674", {"labelsNew": [], "hasChineseSubtitle": True})
        non_chinese = app.release_rank("IPX-811", {"labelsNew": [], "hasChineseSubtitle": False})
        self.assertEqual((embedded[1], subtitles[1], non_chinese[1]), (2, 1, 0))

    def test_embedded_zhongzi_recognizes_nested_mteam_color_fields(self):
        red_hex = app.release_rank("SSIS-181", {"labelsNew": {"data": [{"labelName": "中字", "labelColor": "#f44336"}], "hasChineseSubtitle": False}})
        plain_label = app.release_rank("SSIS-181", {"labelsNew": ["中字"], "hasChineseSubtitle": False})
        with_subtitles = app.release_rank("SSIS-181", {"labelsNew": [], "hasChineseSubtitle": True})
        self.assertEqual((red_hex[1], plain_label[1], with_subtitles[1]), (2, 2, 1))

    def test_actual_mteam_ids_follow_confirmed_chinese_metadata_rule(self):
        embedded_1287538 = app.release_rank("SSIS-241 4K x265", {
            "labelsNew": ["中字", "4k", "AI破解"], "hasChineseSubtitle": False,
        })
        subtitle_1294085 = app.release_rank("TOTTE-290 1080p H.264", {
            "labelsNew": [], "hasChineseSubtitle": True,
        })
        non_chinese = app.release_rank("IPX-811", {"labelsNew": [], "hasChineseSubtitle": False})
        self.assertEqual((embedded_1287538, subtitle_1294085, non_chinese), ((2, 2, 2), (1, 1, 1), (0, 0, 0)))

    def test_mteam_search_preserves_label_color_when_only_legacy_labels_field_exists(self):
        search = {"code": 0, "data": {"data": [
            {"id": 1281991, "name": "SSIS-181", "labels": [{"name": "中字", "color": "#f44336", "title": "內嵌中文"}]},
            {"id": 1287499, "name": "SSIS-419", "labelsNew": [], "hasChineseSubtitle": True},
        ]}}
        with patch.object(app, "mteam_json", return_value=search):
            rows = app.mteam_search("女优", "token")
        self.assertEqual({row["code"]: row["rank"][1] for row in rows}, {"SSIS-181": 2, "SSIS-419": 1})

    def test_nfo_resolution_and_codec_override_missing_or_wrong_title_tags(self):
        metadata = {"width": 3840, "height": 2160, "codec": "HEVC", "language": "chi"}
        self.assertEqual(app.release_rank("SSNI-618 1080p x264", metadata), (2, 1, 2))

    def test_reads_resolution_and_codec_from_xml_nfo(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "SSNI-618.mkv"
            video.touch()
            video.with_suffix(".nfo").write_text("<movie><fileinfo><streamdetails><video><width>3840</width><height>2160</height><codec>hevc</codec></video></streamdetails></fileinfo></movie>", encoding="utf-8")
            metadata = app.read_video_metadata(video)
        self.assertEqual((metadata["width"], metadata["height"]), (3840, 2160))
        self.assertEqual(app.release_rank("SSNI-618", metadata), (2, 0, 2))

    def test_best_candidate_uses_rank_then_seeders_then_date(self):
        items = [
            {"code": "SSNI-618", "rank": [2, 0, 1], "seeders": 100, "created": "2025"},
            {"code": "SSNI-618", "rank": [2, 0, 1], "seeders": 110, "created": "2024"},
            {"code": "SSNI-618", "rank": [2, 0, 1], "seeders": 110, "created": "2026"},
        ]
        self.assertEqual(app.best_candidates(items)["SSNI-618"]["created"], "2026")

    def test_mteam_search_combines_query_and_parses_result(self):
        response = {"code": 0, "data": {"data": [{"id": 42, "name": "SSNI-618", "smallDescr": "LTX模型无码破解 三上悠亚", "labelsNew": [{"name": "中字", "color": "red"}], "hasChineseSubtitle": False, "resolution": "1080p", "videoCodec": "H.264(x264/AVC)", "seeders": "1,234", "createdDate": "2026-01-01"}]}}
        with patch.object(app, "mteam_json", return_value=response) as request:
            rows = app.mteam_search("三上悠亚 ltx", "token")
        self.assertEqual(request.call_args.args[:2], ("/torrent/search", "token"))
        self.assertEqual(request.call_args.args[2]["keyword"], "三上悠亚 ltx")
        self.assertEqual(request.call_args.args[2]["mode"], "adult")
        self.assertEqual(rows[0]["id"], "42")
        self.assertEqual(rows[0]["code"], "SSNI-618")
        self.assertEqual(rows[0]["rank"], [1, 2, 1])
        self.assertEqual((rows[0]["resolution"], rows[0]["video_codec"]), ("1080p", "H.264(x264/AVC)"))
        self.assertEqual(rows[0]["subtitle"], "LTX模型无码破解 三上悠亚")
        self.assertEqual(rows[0]["seeders"], 1234)
        request.assert_called_once()

    def test_mteam_subtitle_download_uses_most_downloaded_chinese_item_and_video_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "SNOS-047-破解.mp4"
            video.touch()
            subtitle_list = {"code": 0, "data": {"data": [
                {"id": 11, "torrentId": 1289514, "name": "SNOS-047 zh-HK", "lang": "繁體中文", "hits": "1,200"},
                {"id": 12, "torrentId": 1289514, "name": "SNOS-047 zh-CN", "lang": "简体中文", "hits": 300},
                {"id": 13, "torrentId": 1289514, "name": "SNOS-047 Japanese", "lang": "Japanese", "hits": 9000},
                {"id": 14, "torrentId": 999, "name": "other release", "lang": "繁體中文", "hits": 9999},
            ]}}
            link = {"code": 0, "data": {"credential": "subtitle-token"}}
            metadata = {"content_type": "application/x-subrip", "content_disposition": 'attachment; filename="subtitle.srt"'}
            with patch.object(app, "mteam_json", side_effect=[subtitle_list, link]) as api, patch.object(
                app, "mteam_request", return_value=(b"1\\n00:00:01,000 --> 00:00:02,000\\nhello\\n", metadata),
            ) as download:
                result = app.mteam_subtitle_download("SNOS-047", "1289514", [video], "token")
            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(result["id"], "11")
            self.assertEqual(result["language"], "zh-HK")
            self.assertEqual(Path(result["files"][0]).name, "SNOS-047-破解.MT.zh-HK.srt")
            self.assertEqual(Path(result["files"][0]).read_bytes(), b"1\\n00:00:01,000 --> 00:00:02,000\\nhello\\n")
            self.assertEqual(api.call_args_list[0].args, ("/subtitle/list?id=1289514", "token"))
            self.assertEqual(api.call_args_list[0].kwargs["method"], "POST")
            self.assertEqual(api.call_args_list[1].args[0], "/subtitle/genlink?id=11")
            self.assertEqual(api.call_args_list[1].kwargs["method"], "POST")
            self.assertIn("subtitle-token", download.call_args.args[0])

    def test_mteam_subtitle_download_maps_numeric_language_id_once(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "SONE-948-破解-C.mp4"
            video.touch()
            subtitle_list = {"code": 0, "data": {"data": [{"id": 21, "langId": 3, "downloads": 8}]}}
            languages = {"code": 0, "data": [{"id": 3, "langName": "Chinese"}]}
            link = {"code": 0, "data": {"credential": "credential"}}
            metadata = {"content_type": "application/x-subrip", "content_disposition": 'attachment; filename="subtitle.srt"'}
            with patch.object(app, "mteam_json", side_effect=[subtitle_list, languages, link]) as api, patch.object(
                app, "mteam_request", return_value=(b"subtitle", metadata),
            ):
                result = app.mteam_subtitle_download("SONE-948", "123456", [video], "token")
            self.assertEqual(Path(result["files"][0]).name, "SONE-948-破解-C.MT.zh-HK.srt")
            self.assertEqual([call.args[0] for call in api.call_args_list], ["/subtitle/list?id=123456", "/subtitle/langs", "/subtitle/genlink?id=21"])

    def test_mteam_subtitle_download_searches_by_video_id_without_requiring_response_link_field(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "SSNI-802.mp4"
            video.touch()
            subtitle_list = {"code": 0, "data": {"data": [{"id": 91, "lang": "zh-HK", "hits": 7}]}}
            link = {"code": 0, "data": {"credential": "credential"}}
            metadata = {"content_type": "application/x-subrip", "content_disposition": 'attachment; filename="subtitle.srt"'}
            with patch.object(app, "mteam_json", side_effect=[subtitle_list, link]) as api, patch.object(
                app, "mteam_request", return_value=(b"subtitle", metadata),
            ):
                result = app.mteam_subtitle_download("SSNI-802", "1283063", [video], "token")
            self.assertEqual(result["id"], "91")
            self.assertEqual(Path(result["files"][0]).name, "SSNI-802.MT.zh-HK.srt")
            self.assertEqual(api.call_args_list[0].args, ("/subtitle/list?id=1283063", "token"))

    def test_mteam_subtitle_download_uses_bound_list_without_keyword_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "SSNI-802.mp4"
            video.touch()
            empty_list = {"code": 0, "data": {"data": []}}
            with patch.object(app, "mteam_json", return_value=empty_list) as api:
                result = app.mteam_subtitle_download("SSNI-802", "1283063", [video], "token")
            self.assertEqual(result["status"], "not_found")
            api.assert_called_once_with("/subtitle/list?id=1283063", "token", method="POST")

    def test_completed_download_gets_subtitle_once_using_local_video_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "SNOS-047-破解.mp4"
            video.touch()
            records = {"SNOS-047": {"code": "SNOS-047", "current": {
                "id": "1289514", "rank": [1, 1, 1], "hash": "newhash", "state": "downloading", "owns_task": True,
                "download_dir": str(root), "title": "SNOS-047",
            }}}
            downloader = FakeDownloader(complete=True)

            def save_subtitle(code, torrent_id, videos, token,):
                target = videos[0].with_name(f"{videos[0].stem}.MT.zh-HK.srt")
                target.write_text("subtitle", encoding="utf-8")
                return {"status": "downloaded", "id": "22", "downloads": 42, "language": "zh-HK", "files": [str(target)]}

            with patch.object(app, "mteam_subtitle_download", side_effect=save_subtitle) as download:
                app.poll_downloader_records(downloader, records, [], performer="Actress", token="token")
                app.poll_downloader_records(downloader, records, [], performer="Actress", token="token")
            download.assert_called_once_with("SNOS-047", "1289514", [video], "token")
            self.assertTrue((root / "SNOS-047-破解.MT.zh-HK.srt").is_file())
            self.assertEqual(records["SNOS-047"]["current"]["subtitle_check"]["state"], "downloaded")

    def test_subtitle_checker_retries_not_found_results_from_old_matching_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "SSNI-802.mp4"
            video.touch()
            active = {
                "id": "1283063", "rank": [1, 1, 1], "state": "completed", "download_dir": directory,
                "subtitle_check": {"state": "not_found", "version": 1, "checked_epoch": time.time(), "video_paths": [str(video)]},
            }
            with patch.object(app, "find_local_releases", return_value=[{"path": str(video)}]), patch.object(
                app, "mteam_subtitle_download", return_value={"status": "not_found", "files": []},
            ) as download:
                app._download_release_subtitles(active, "SSNI-802", "token", "Actress")
            download.assert_called_once_with("SSNI-802", "1283063", [video], "token")
            self.assertEqual(active["subtitle_check"]["version"], app.SUBTITLE_CHECK_VERSION)

    def test_embedded_chinese_cached_mteam_rank_skips_subtitle_lookup(self):
        active = {"id": "1283063", "rank": [1, 1, 1], "state": "completed"}
        metadata = {"id": "1283063", "rank": [1, 2, 1]}
        with patch.object(app, "mteam_subtitle_download") as download:
            app._download_release_subtitles(active, "SSIS-181", "token", "Actress", metadata)
        download.assert_not_called()

    def test_embedded_chinese_active_rank_skips_subtitle_lookup(self):
        active = {"id": "1283063", "rank": [1, 2, 1], "state": "completed"}
        with patch.object(app, "mteam_subtitle_download") as download:
            app._download_release_subtitles(active, "SSIS-181", "token", "Actress")
        download.assert_not_called()

    def test_existing_external_chinese_local_video_gets_missing_subtitle(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "SSNI-802.mp4"
            video.touch()
            record = {"local_files": {str(video): {"id": "1283063", "rank": [1, 1, 1]}}}
            result = {"status": "downloaded", "id": "91", "downloads": 4, "language": "zh-HK", "files": [str(video.with_suffix(".MT.zh-HK.srt"))]}
            with patch.object(app, "mteam_subtitle_download", return_value=result) as download:
                app._download_existing_local_subtitles("SSNI-802", [{"path": str(video), "rank": [1, 1, 1]}], record, "token", "Actress")
            download.assert_called_once_with("SSNI-802", "1283063", [video], "token")
            self.assertEqual(record["local_files"][str(video)]["subtitle_check"]["state"], "downloaded")

    def test_existing_embedded_chinese_local_video_does_not_search_subtitle(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "SSIS-181.mp4"
            video.touch()
            record = {"local_files": {str(video): {"id": "1283063", "rank": [1, 2, 1]}}}
            with patch.object(app, "mteam_subtitle_download") as download:
                app._download_existing_local_subtitles("SSIS-181", [{"path": str(video), "rank": [1, 2, 1]}], record, "token", "Actress")
            download.assert_not_called()

    def test_non_chinese_video_checks_subtitles_every_round_and_updates_after_download(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "IPX-536.mp4"
            video.touch()
            record = {"local_files": {str(video): {"id": "1278591", "rank": [2, 0, 2]}}}
            files = [{"path": str(video), "title": video.name, "rank": [2, 0, 2]}]
            subtitle = video.with_name("IPX-536.MT.zh-HK.srt")

            def download_subtitle(*args):
                subtitle.write_text("Chinese subtitle", encoding="utf-8")
                return {"status": "downloaded", "downloads": 10, "files": [str(subtitle)]}

            with patch.object(app, "mteam_subtitle_download", return_value={"status": "not_found", "files": []}) as download:
                app._download_existing_local_subtitles("IPX-536", files, record, "token", "Actress")
                app._download_existing_local_subtitles("IPX-536", files, record, "token", "Actress")
                self.assertEqual(download.call_count, 2)
            with patch.object(app, "mteam_subtitle_download", side_effect=download_subtitle) as download, patch.object(
                app, "mteam_detail", return_value={"resolution": "4K", "videoCodec": "x265", "labelsNew": [], "hasChineseSubtitle": True},
            ) as detail:
                app._download_existing_local_subtitles("IPX-536", files, record, "token", "Actress")
                app._download_existing_local_subtitles("IPX-536", files, record, "token", "Actress")
                download.assert_called_once()
                detail.assert_called_once_with("1278591", "token")
            self.assertEqual(record["local_files"][str(video)]["rank"], [2, 1, 2])
            self.assertTrue(subtitle.is_file())

    def test_mteam_search_stops_after_latest_page_when_it_is_full(self):
        items = [{"id": index, "name": f"SSNI-{index:03d} 1080p"} for index in range(100)]
        with patch.object(app, "mteam_json", return_value={"code": 0, "data": {"data": items}}) as request:
            rows = app.mteam_search("Actress", "token")
        self.assertEqual(len(rows), 100)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[2]["pageNumber"], 1)
        self.assertEqual(request.call_args.args[2]["pageSize"], 100)
        self.assertEqual(request.call_args.args[2]["sortDirection"], "DESC")

    def test_mteam_search_resolves_numeric_standard_and_codec_ids(self):
        search = {"code": 0, "data": {"data": [{"id": 42, "name": "SSIS-181", "standard": 5, "videoCodec": 3}]}}
        standards = {"code": 0, "data": [{"id": 5, "name": "2160p"}]}
        codecs = {"code": 0, "data": [{"id": 3, "name": "H.265(x265/HEVC)"}]}
        with patch.object(app, "mteam_json", side_effect=[search, standards, codecs]) as request:
            rows = app.mteam_search("三上悠亚", "token")
        self.assertEqual(rows[0]["rank"], [2, 0, 2])
        self.assertEqual((rows[0]["resolution"], rows[0]["video_codec"]), ("2160p", "H.265(x265/HEVC)"))
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/torrent/search", "/torrent/standardList", "/torrent/videoCodecList"])

    def test_refresh_search_fills_missing_existing_metadata_from_detail(self):
        search = {"code": 0, "data": {"data": [{"id": 1289514, "name": "SSIS-181", "standard": 5, "videoCodec": 3}]}}
        standards = {"code": 0, "data": [{"id": 5, "name": "2160p"}]}
        codecs = {"code": 0, "data": [{"id": 3, "name": "H.264(x264/AVC)"}]}
        detail = {"code": 0, "data": {"labelsNew": [{"name": "中字", "color": "red"}], "hasChineseSubtitle": False, "standard": 5, "videoCodec": 3}}
        with patch.object(app, "mteam_json", side_effect=[search, standards, codecs, detail]) as request:
            rows = app.mteam_search("三上悠亚", "token", True, {"SSIS-181"})
        self.assertEqual(rows[0]["rank"], [2, 2, 1])
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/torrent/search", "/torrent/standardList", "/torrent/videoCodecList", "/torrent/detail"])
        self.assertTrue(request.call_args.kwargs["multipart"])

    def test_incomplete_selected_candidate_is_enriched_from_detail_before_submission(self):
        subscription = {"id": "sub", "performer": "三上悠亚", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        candidate = {"id": "1289514", "code": "SSIS-181", "title": "SSIS-181", "rank": [0, 0, 0], "seeders": 10, "created": "2026", "metadata_complete": False}
        downloader = FakeDownloader()
        records = {}
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "三上悠亚", "labelsNew": [{"name": "中字", "color": "red"}], "resolution": "2160p", "videoCodec": "H.265(x265/HEVC)"}) as detail, patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        detail.assert_called_once_with("1289514", "token")
        saved = records["sub"]["SSIS-181"]["mteam_metadata"]
        self.assertEqual(saved["rank"], [2, 2, 2])
        self.assertEqual(saved["resolution"], "2160p")
        self.assertEqual(saved["video_codec"], "H.265(x265/HEVC)")

    def test_explicit_mteam_detail_resolution_overrides_conflicting_title_resolution(self):
        candidate = {"id": "1283711", "code": "SSNI-618", "title": "SSNI-618 4K 中文字幕 x265", "rank": [2, 1, 2], "resolution": "4K", "video_codec": "x265"}
        detail = {"resolution": "1080p", "videoCodec": "x265", "labelsNew": [], "hasChineseSubtitle": True}
        with patch.object(app, "mteam_detail", return_value=detail):
            enriched = app.enrich_candidate_from_detail(candidate, "token")
        self.assertEqual(enriched["rank"], [1, 1, 2])
        self.assertEqual(enriched["resolution"], "1080p")

    def test_detail_lookup_failure_prevents_download(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        candidate = {"id": "1", "code": "SSNI-618", "title": "SSNI-618 1080p x265", "rank": [1, 0, 2], "seeders": 10, "created": "2026", "metadata_complete": False}
        downloader = FakeDownloader()
        records = {}
        results = []
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", side_effect=app.AppError("detail unavailable")), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)
        self.assertEqual(downloader.added, [])
        self.assertEqual(results[0]["state"], "failed")

    def test_mteam_detail_uses_multipart_form_data(self):
        response = FakeResponse(b'{"code":0,"data":{}}')
        with patch.object(app.MTEAM_OPENER, "open", return_value=response) as opener:
            app.mteam_detail(1289514, "token")
        request = opener.call_args.args[0]
        content_type = request.get_header("Content-type")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1]
        self.assertIn(f'--{boundary}\r\nContent-Disposition: form-data; name="id"\r\n\r\n1289514\r\n'.encode(), request.data)

    def test_mteam_poll_cache_reuses_detail_and_option_list_responses(self):
        app.MTEAM_POLL_CACHE.active = {"details": {}, "options": {}}
        detail = {"code": 0, "data": {"id": 1281991, "labelsNew": [{"name": "中字", "color": "#f44336"}]}}
        options = {"code": 0, "data": [{"id": 5, "name": "2160p"}]}
        try:
            with patch.object(app, "mteam_json", side_effect=[detail, options]) as request:
                first = app.mteam_detail("1281991", "token")
                second = app.mteam_detail("1281991", "token")
                first_options = app.mteam_option_map("/torrent/standardList", "token")
                second_options = app.mteam_option_map("/torrent/standardList", "token")
            self.assertEqual(first, second)
            self.assertEqual(first_options, second_options)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(app.release_rank("SSIS-181", first)[1], 2)
        finally:
            app.MTEAM_POLL_CACHE.active = None

    def test_mteam_detail_force_refresh_bypasses_poll_detail_cache(self):
        app.MTEAM_POLL_CACHE.active = {"details": {"1278591": {"mediainfo": "cached"}}, "options": {}}
        self.addCleanup(setattr, app.MTEAM_POLL_CACHE, "active", None)
        with patch.object(app, "mteam_json", return_value={"code": 0, "data": {"mediainfo": "fresh"}}) as request:
            self.assertEqual(app.mteam_detail("1278591", "token", force_refresh=True), {"mediainfo": "fresh"})
        request.assert_called_once()
        self.assertEqual(app.MTEAM_POLL_CACHE.active["details"]["1278591"], {"mediainfo": "fresh"})

    def test_mteam_form_request_urlencodes_payload(self):
        response = FakeResponse(b'{"code":0,"data":"token"}')
        with patch.object(app.MTEAM_OPENER, "open", return_value=response) as opener:
            app.mteam_json("/torrent/genDlToken", "private", {"id": "42"}, form=True)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(request.data, b"id=42")

    def test_mteam_request_sends_api_key_and_validates_host(self):
        response = FakeResponse(b'{"code":0,"data":[]}')
        with patch.object(app.MTEAM_OPENER, "open", return_value=response) as opener:
            app.mteam_json("/torrent/search", "private", {"searchText": "keyword"})
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.m-team.cc/api/torrent/search")
        self.assertEqual(request.get_header("X-api-key"), "private")
        self.assertEqual(json.loads(request.data), {"searchText": "keyword"})
        with self.assertRaises(app.AppError):
            app.mteam_request("https://attacker.example/collect", "private")

    def test_mteam_business_rate_limit_message_is_recognized(self):
        response = json.dumps({"code": 500, "message": "請求過於頻繁"}).encode()
        with patch.object(app, "mteam_request", return_value=(response, {})):
            with self.assertRaisesRegex(app.MTeamRateLimitError, "请求过于频繁") as error:
                app.mteam_json("/torrent/search", "token", {"keyword": "test"})
        self.assertGreaterEqual(error.exception.retry_after, 60)

    def test_mteam_business_rate_limit_reads_server_retry_after(self):
        response = json.dumps({"code": 429, "message": "請求過於頻繁", "data": {"retryAfter": 1200}}).encode()
        with patch.object(app, "mteam_request", return_value=(response, {})):
            with self.assertRaises(app.MTeamRateLimitError) as error:
                app.mteam_json("/torrent/search", "token", {"keyword": "test"})
        self.assertEqual(error.exception.retry_after, 1200)
        self.assertTrue(error.exception.retry_after_provided)

    def test_repeated_rate_limits_increase_cooldown_with_a_cap(self):
        error = app.MTeamRateLimitError("limited", 900)
        delays = [app.rate_limit_retry_delay(error, {"consecutive_rate_limits": count}) for count in range(7)]
        self.assertEqual([delay for _, delay in delays[:4]], [900, 1800, 3600, 7200])
        self.assertEqual(delays[-1][1], 21600)

    def test_mteam_rejects_untrusted_redirect_targets(self):
        handler = app.MTeamRedirectHandler()
        with self.assertRaises(app.AppError):
            handler.redirect_request(urllib.request.Request("https://api.m-team.cc/start"), None, 302, "Found", {}, "https://attacker.example/file")
        self.assertIsNotNone(handler.redirect_request(urllib.request.Request("https://api.m-team.cc/start"), None, 302, "Found", {}, "https://api.m-team.cc/api/file"))

    def test_fetch_torrent_generates_link_and_rejects_non_bencode_body(self):
        metadata = {"status": 200, "content_type": "application/x-bittorrent", "content_disposition": "", "url": "https://api.m-team.cc/api/torrent/download"}
        with patch.object(app, "mteam_json", return_value={"code": 0, "data": {"token": "abc%2Fdef"}}) as api_call, patch.object(app, "mteam_download_request", return_value=(torrent_bytes(), metadata)) as download:
            self.assertEqual(app.fetch_torrent(17, "token"), torrent_bytes())
        self.assertEqual(api_call.call_args.args[0], "/torrent/genDlToken")
        self.assertTrue(api_call.call_args.kwargs["form"])
        self.assertEqual(download.call_args.args[0], "https://api.m-team.cc/api/torrent/download?token=abc%2Fdef")
        with patch.object(app, "mteam_json", return_value={"code": 0, "data": {"url": "https://api.m-team.cc/api/torrent/download"}}), patch.object(app, "mteam_download_request", return_value=(b"<html>", {**metadata, "content_type": "text/html"})):
            with self.assertRaises(app.AppError):
                app.fetch_torrent(17, "token")

    def test_mteam_download_redirect_allows_official_cdn_without_forwarding_api_key(self):
        handler = app.MTeamDownloadRedirectHandler()
        request = urllib.request.Request("https://api.m-team.cc/api/rss/dlv2?token=private", headers={"x-api-key": "private-key", "Accept": "application/x-bittorrent"})
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://fr1.halomt.com/file.torrent")
        self.assertEqual(redirected.full_url, "https://fr1.halomt.com/file.torrent")
        self.assertIsNone(redirected.get_header("X-api-key"))
        self.assertEqual(redirected.get_header("Accept"), "application/x-bittorrent")
        manfuz_redirect = handler.redirect_request(request, None, 302, "Found", {}, "https://bt1.manfuz.co/file.torrent")
        self.assertEqual(manfuz_redirect.full_url, "https://bt1.manfuz.co/file.torrent")
        nested_manfuz_redirect = handler.redirect_request(manfuz_redirect, None, 302, "Found", {}, "https://bt2.manfuz.co/file.torrent")
        self.assertEqual(nested_manfuz_redirect.full_url, "https://bt2.manfuz.co/file.torrent")
        kp_redirect = handler.redirect_request(request, None, 302, "Found", {}, "https://kp.m-team.cc/api/torrent/download")
        self.assertEqual(kp_redirect.full_url, "https://kp.m-team.cc/api/torrent/download")
        with self.assertRaises(app.AppError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://attacker.example/file.torrent")

    def test_mteam_download_url_accepts_official_site_hosts_and_rejects_lookalikes(self):
        self.assertEqual(app.mteam_download_url("https://kp.m-team.cc/api/rss/dlv2?token=abc"), "https://kp.m-team.cc/api/rss/dlv2?token=abc")
        self.assertEqual(app.mteam_download_url("https://api.m-team.io/api/rss/dlv2?token=abc"), "https://api.m-team.io/api/rss/dlv2?token=abc")
        self.assertEqual(app.mteam_download_url("https://bt1.manfuz.co/file.torrent"), "https://bt1.manfuz.co/file.torrent")
        with self.assertRaisesRegex(app.AppError, "attacker-m-team\\.cc"):
            app.mteam_download_url("https://attacker-m-team.cc/file.torrent")

    def test_bencode_info_hash_uses_exact_info_bytes(self):
        data = torrent_bytes("demo")
        self.assertEqual(app.torrent_info_hash(data), hashlib.sha1(b"d4:name4:demoe").hexdigest())
        with self.assertRaises(app.AppError):
            app.torrent_info_hash(b"invalid")


class SettingsAndSubscriptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.enterContext(patch.object(app, "DATA_ROOT", root))
        self.enterContext(patch.object(app, "LOCAL_VIDEO_DATABASE", root / "local-videos.sqlite3"))
        for name in ("SETTINGS_FILE", "SUBSCRIPTIONS_FILE", "RECORDS_FILE", "STATUS_FILE", "BACKFILL_STATUS_FILE", "BACKFILL_STATUS_LOCK_FILE", "LOG_FILE", "LOG_LOCK_FILE", "REQUEST_FILE", "LOCK_FILE", "DISABLED_FILE", "MTEAM_LIMIT_FILE", "MTEAM_REQUEST_STATE_FILE", "MTEAM_REQUEST_LOCK_FILE"):
            self.enterContext(patch.object(app, name, root / (name.lower() + ".json")))
        self.enterContext(patch.object(app, "mteam_detail", return_value={"performer": "三上悠亚 Actress"}))

    def test_settings_mask_token_and_preserve_secrets_when_saving_other_fields(self):
        app.save_settings({"mteam_token": "private", "downloader": {"type": "qbittorrent", "url": "http://nas:8080", "username": "admin", "password": "secret"}, "poll_interval_minutes": 60, "auto_scan": True})
        public = app.save_settings({"downloader": {"type": "qbittorrent", "url": "http://nas:8081", "username": "admin", "password": ""}, "poll_interval_minutes": 30, "auto_scan": False})
        self.assertTrue(public["mteam_token_configured"])
        self.assertTrue(public["downloader"]["password_configured"])
        self.assertEqual(app.get_settings()["mteam_token"], "private")
        self.assertEqual(app.get_settings()["downloader"]["password"], "secret")
        self.assertFalse(public["local_multi_disk_wash"])
        enabled = app.save_settings({"local_multi_disk_wash": True})
        self.assertTrue(enabled["local_multi_disk_wash"])
        self.assertTrue(app.get_settings()["local_multi_disk_wash"])

        self.assertFalse(public["cleanup_orphan_sidecars"])
        self.assertFalse(public["debug_mode"])
        self.assertEqual(public["mteam_request_interval_seconds"], 5)
        enabled = app.save_settings({"cleanup_orphan_sidecars": True})
        self.assertTrue(enabled["cleanup_orphan_sidecars"])
        self.assertTrue(app.get_settings()["cleanup_orphan_sidecars"])
        enabled = app.save_settings({"debug_mode": True})
        self.assertTrue(enabled["debug_mode"])
        self.assertTrue(app.get_settings()["debug_mode"])
        app.save_settings({"clear_mteam_token": True})
        self.assertFalse(app.public_settings()["mteam_token_configured"])

    def test_transmission_setting_is_rejected_and_legacy_setting_defaults_to_qbittorrent(self):
        with self.assertRaisesRegex(app.AppError, "仅支持 qBittorrent"):
            app.save_settings({"downloader": {"type": "transmission", "url": "http://nas:9091"}})
        app.write_json(app.SETTINGS_FILE, {"downloader": {"type": "transmission", "url": "http://nas:8080", "username": "u", "password": "p"}})
        self.assertEqual(app.get_settings()["downloader"]["type"], "qbittorrent")
        self.assertEqual(app.public_settings()["downloader"]["type"], "qbittorrent")

    def test_subtitle_collection_directory_setting_validation_and_persistence(self):
        self.assertEqual(app.public_settings()["subtitle_collection_dir"], "")
        saved = app.save_settings({"subtitle_collection_dir": " /vol2/media/subs/../Subtitles/ "})
        self.assertEqual(saved["subtitle_collection_dir"], "/vol2/media/Subtitles")
        app.save_settings({"debug_mode": True})
        self.assertEqual(app.get_settings()["subtitle_collection_dir"], "/vol2/media/Subtitles")
        for invalid in ("relative", "/", "/tmp/..", "/tmp/\nsubs", None, 123):
            with self.subTest(invalid=invalid), self.assertRaises(app.AppError):
                app.save_settings({"subtitle_collection_dir": invalid})
        self.assertEqual(app.save_settings({"subtitle_collection_dir": ""})["subtitle_collection_dir"], "")

    def test_subtitle_collection_copies_only_external_chinese_and_preserves_sources(self):
        source_dir = Path(app.DATA_ROOT) / "subtitle-source"
        source_dir.mkdir()
        sources = []
        rows = []
        for code, grade, extension in (("ABC-123", 1, ".srt"), ("ABC-124", 2, ".ass"), ("ABC-125", 0, ".srt")):
            subtitle = source_dir / f"{code}-U.MT.zh-HK{extension}"
            subtitle.write_text(f"subtitle {code}", encoding="utf-8")
            sources.append(subtitle)
            rows.append({"code": code, "files": [{"rank": [1, grade, 1], "subtitle_paths": [str(subtitle)]}]})
        unrelated = source_dir / "ABC-123-elsewhere.srt"
        unrelated.write_text("not downloaded by app", encoding="utf-8")
        rows[0]["files"][0]["subtitle_paths"].append(str(unrelated))
        dest = Path(app.DATA_ROOT) / "collection"
        self.assertEqual(app.collect_subscription_subtitles(rows, str(dest), "Actress"), {"copied": 1, "unchanged": 0, "failed": 0})
        self.assertEqual([file.name for file in dest.iterdir()], ["ABC-123.srt"])
        self.assertEqual((dest / "ABC-123.srt").read_bytes(), sources[0].read_bytes())
        self.assertNotIn("not downloaded by app", (dest / "ABC-123.srt").read_text(encoding="utf-8"))
        self.assertTrue(all(source.exists() for source in sources))
        with patch.object(app.shutil, "copy2", side_effect=AssertionError("unchanged subtitles must not be copied")):
            self.assertEqual(app.collect_subscription_subtitles(rows, str(dest))["unchanged"], 1)
        disabled = Path(app.DATA_ROOT) / "disabled"
        self.assertEqual(app.collect_subscription_subtitles(rows, "")["copied"], 0)
        self.assertFalse(disabled.exists())

    def test_subtitle_collection_prefers_newest_same_extension_and_copies_other_formats(self):
        root = Path(app.DATA_ROOT) / "collection-versions"
        root.mkdir()
        older = root / "ABC-123-old.MT.zh-HK.srt"
        newer = root / "ABC-123-new.MT.zh-HK.srt"
        ass = root / "ABC-123.MT.zh-HK.ass"
        for source, content, stamp in ((older, "old", 1000), (newer, "new", 2000), (ass, "ass", 2000)):
            source.write_text(content, encoding="utf-8")
            os.utime(source, (stamp, stamp))
        rows = [{"code": "ABC-123", "files": [{"rank": [1, 1, 1], "subtitle_paths": [str(older), str(newer), str(ass)]}]}]
        dest = root / "out"
        self.assertEqual(app.collect_subscription_subtitles(rows, str(dest))["copied"], 2)
        self.assertEqual((dest / "ABC-123.srt").read_text(), "new")
        self.assertTrue((dest / "ABC-123.ass").exists())
        newer.write_text("updated", encoding="utf-8")
        os.utime(newer, (3000, 3000))
        self.assertEqual(app.collect_subscription_subtitles(rows, str(dest))["copied"], 1)
        self.assertEqual((dest / "ABC-123.srt").read_text(), "updated")
        self.assertEqual(app.collect_subscription_subtitles([{ "code": "ABC-123", "files": [{"rank": [1, 1, 1], "subtitle_paths": [str(older)]}]}], str(dest))["unchanged"], 1)

    def test_subtitle_collection_failure_preserves_existing_target_and_logs_warning(self):
        root = Path(app.DATA_ROOT) / "collection-failed"
        root.mkdir()
        source = root / "ABC-123.MT.zh-HK.srt"
        source.write_text("new", encoding="utf-8")
        dest = root / "out"
        dest.mkdir()
        target = dest / "ABC-123.srt"
        target.write_text("old", encoding="utf-8")
        os.utime(target, (1000, 1000))
        rows = [{"code": "ABC-123", "files": [{"rank": [1, 1, 1], "subtitle_paths": [str(source)]}]}]
        with patch.object(app.shutil, "copy2", side_effect=PermissionError("read only")):
            self.assertEqual(app.collect_subscription_subtitles(rows, str(dest), "Actress")["failed"], 1)
        self.assertEqual(target.read_text(), "old")
        self.assertTrue(source.exists())
        self.assertTrue(any(entry["event"] == "subtitle_collection_failed" and entry["level"] == "warning" for entry in app.get_logs()))
        self.assertEqual(sorted(file.name for file in dest.iterdir()), ["ABC-123.srt"])

    def test_orphan_cleanup_excludes_subtitle_collection_directory(self):
        root = Path(app.DATA_ROOT) / "collection-cleanup"
        collected = root / "collection"
        collected.mkdir(parents=True)
        exported = collected / "ABC-123.srt"
        exported.touch()
        orphan = root / "DEF-456.srt"
        orphan.touch()
        app.cleanup_orphan_sidecars(str(root), excluded_dirs=[str(collected)])
        self.assertTrue(exported.exists())
        self.assertFalse(orphan.exists())

    def test_subscription_counts_follow_indexed_video_files_and_removals(self):
        root = Path(app.DATA_ROOT) / "counts"
        root.mkdir()
        first = root / "IPX-536-A.mp4"
        second = root / "IPX-536-B.mp4"
        first.touch()
        second.touch()
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "one", "performer": "Actress", "download_dir": str(root)},
            {"id": "two", "performer": "Other", "download_dir": str(root / "other")},
        ])
        app.get_subscription_videos("one")
        self.assertEqual([item["video_count"] for item in app.subscriptions_with_video_counts()], [2, 0])
        second.unlink()
        app.get_subscription_videos("one")
        self.assertEqual(app.subscriptions_with_video_counts()[0]["video_count"], 1)

    def test_mteam_request_interval_can_be_configured_and_is_validated(self):
        saved = app.save_settings({"mteam_request_interval_seconds": 12})
        self.assertEqual(saved["mteam_request_interval_seconds"], 12)
        self.assertEqual(app.get_settings()["mteam_request_interval_seconds"], 12)
        for interval in (0, 61, True, 1.5):
            with self.subTest(interval=interval), self.assertRaisesRegex(app.AppError, "请求间隔须为 1 到 60 秒"):
                app.save_settings({"mteam_request_interval_seconds": interval})

    def test_mteam_id_backfill_is_queued_and_processed_by_background_worker(self):
        subscription = app.create_subscription({"performer": "Actress", "download_dir": "/media/async-backfill"})
        queued = app.queue_mteam_id_backfill(subscription["id"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(app.get_mteam_id_backfill_status(subscription["id"])["state"], "queued")
        request = app.pop_scan_request()
        self.assertEqual(request["action"], "mteam_id_backfill")
        class QueuedDownloader:
            config = {"type": "qbittorrent"}

        with patch.object(app, "scan_local_releases", return_value={}):
            with patch.object(app, "Downloader", return_value=QueuedDownloader()):
                app.run_mteam_id_backfill_request(request)
        status = app.get_mteam_id_backfill_status(subscription["id"])
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["summary"]["filled"], 0)

    def test_subscription_validation_and_separate_same_number_records(self):
        one = app.create_subscription({"performer": "三上悠亚", "keywords": "ltx", "blacklist": ["ssni 939", "FC2 PPV 1234567", "SSNI-939"], "download_dir": "/vol1/video/a"})
        two = app.create_subscription({"performer": "三上悠亚", "keywords": "1080p", "download_dir": "/vol1/video/b"})
        records = {one["id"]: {"SSNI-618": {"code": "SSNI-618"}}, two["id"]: {"SSNI-618": {"code": "SSNI-618"}}}
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(one["blacklist"], ["SSNI-939", "FC2-PPV-1234567"])
        self.assertEqual(len(records), 2)
        with self.assertRaises(app.AppError):
            app.create_subscription({"performer": "Test", "download_dir": "relative"})
        with self.assertRaises(app.AppError):
            app.create_subscription({"performer": "Test", "download_dir": "/"})
        with self.assertRaisesRegex(app.AppError, "黑名单番号格式无效"):
            app.create_subscription({"performer": "Test", "blacklist": ["not-a-code"], "download_dir": "/video"})

    def test_subscription_edit_and_delete_preserve_other_subscription(self):
        one = app.create_subscription({"performer": "One", "download_dir": "/video/one"})
        two = app.create_subscription({"performer": "Two", "download_dir": "/video/two"})
        updated = app.update_subscription(one["id"], {"performer": "One Updated", "download_dir": "/video/one"})
        self.assertEqual(updated["performer"], "One Updated")
        app.delete_subscription(one["id"])
        self.assertEqual([item["id"] for item in app.list_subscriptions()], [two["id"]])

    def test_subscription_edit_preserves_blacklist_when_field_is_omitted(self):
        one = app.create_subscription({"performer": "One", "blacklist": ["IPX-811"], "download_dir": "/video/one"})
        updated = app.update_subscription(one["id"], {"performer": "One Updated", "download_dir": "/video/one"})
        self.assertEqual(updated["blacklist"], ["IPX-811"])

    def test_queue_scan_can_target_one_subscription(self):
        one = app.create_subscription({"performer": "One", "download_dir": "/video/one"})
        two = app.create_subscription({"performer": "Two", "download_dir": "/video/two"})
        queued = app.queue_scan("manual", one["id"], True)
        self.assertEqual(queued["state"], "queued")
        app.queue_scan("manual", two["id"])
        requests = app.read_scan_requests()
        self.assertEqual([item["subscription_id"] for item in requests], [one["id"], two["id"]])
        self.assertTrue(requests[0]["refresh_existing_info"])
        self.assertEqual(app.pop_scan_request()["subscription_id"], one["id"])
        self.assertEqual(app.pop_scan_request()["subscription_id"], two["id"])
        app.write_json(app.REQUEST_FILE, {"trigger": "manual", "subscription_id": one["id"]})
        self.assertEqual(app.pop_scan_request()["subscription_id"], one["id"])
        with self.assertRaisesRegex(app.AppError, "订阅不存在"):
            app.queue_scan("manual", "missing")

    def test_corrupt_state_is_backed_up_and_check_status_reports_failure(self):
        original = "{broken state"
        app.RECORDS_FILE.write_text(original, encoding="utf-8")
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "sub", "performer": "Actress", "download_dir": "/video"}])
        status = app.run_poll("manual")
        self.assertEqual(status["state"], "failed")
        self.assertIn("已损坏", status["message"])
        self.assertEqual(app.RECORDS_FILE.read_text(encoding="utf-8"), original)
        backups = list(Path(app.DATA_ROOT).glob(f"{app.RECORDS_FILE.name}.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_mteam_request_spacing_is_persisted_between_calls(self):
        with patch.object(app.time, "time", side_effect=[100.0, 100.0, 100.2, 101.0]), patch.object(app.time, "sleep") as sleep:
            app.wait_for_mteam_request()
            first = app.read_json(app.MTEAM_REQUEST_STATE_FILE, {})
            app.wait_for_mteam_request()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 4.8)
        self.assertEqual(first["next_request_at"], 105.0)
        self.assertEqual(app.read_json(app.MTEAM_REQUEST_STATE_FILE, {})["next_request_at"], 106.0)

    def test_configured_mteam_request_interval_controls_cross_process_spacing(self):
        app.save_settings({"mteam_request_interval_seconds": 12})
        with patch.object(app.time, "time", side_effect=[200.0, 200.0, 200.2, 201.0]), patch.object(app.time, "sleep") as sleep:
            app.wait_for_mteam_request()
            app.wait_for_mteam_request()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 11.8)
        self.assertEqual(app.read_json(app.MTEAM_REQUEST_STATE_FILE, {})["next_request_at"], 213.0)

    def test_scheduler_restores_last_scan_time_from_persisted_status(self):
        app.write_json(app.STATUS_FILE, {"state": "completed", "last_scan_at": "2026-11-13T02:30:00+00:00"})
        expected = app.datetime.datetime.fromisoformat("2026-11-13T02:30:00+00:00").timestamp()
        self.assertEqual(app.scheduler_last_run(), expected)

    def test_status_exposes_next_scheduled_scan_time(self):
        app.write_json(app.SETTINGS_FILE, {"poll_interval_minutes": 30, "auto_scan": True})
        app.write_json(app.STATUS_FILE, {"state": "completed", "last_scan_at": "2026-11-13T02:30:00+00:00"})
        status = app.get_status()
        expected = app.datetime.datetime.fromisoformat("2026-11-13T02:30:00+00:00").timestamp() + 30 * 60
        self.assertEqual(status["next_scan_at"], expected)
        self.assertEqual(status["poll_interval_minutes"], 30)
        self.assertTrue(status["auto_scan"])

    def test_status_hides_next_scheduled_scan_when_auto_scan_disabled(self):
        app.write_json(app.SETTINGS_FILE, {"poll_interval_minutes": 30, "auto_scan": False})
        app.write_json(app.STATUS_FILE, {"state": "completed", "last_scan_at": "2026-11-13T02:30:00+00:00"})
        status = app.get_status()
        self.assertFalse(status["auto_scan"])
        self.assertEqual(status["next_scan_at"], 0)

    def test_scheduler_recovers_stale_running_state_without_queued_requests(self):
        app.write_json(app.STATUS_FILE, {"state": "running", "started_at": "2026-11-17T00:00:00+00:00", "results": []})
        self.assertTrue(app.recover_interrupted_poll())
        status = app.get_status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("未正常完成", status["message"])
        self.assertTrue(status["last_scan_at"])
        self.assertEqual(app.get_logs()[-1]["event"], "scheduler_recovered")

    def test_scheduler_preserves_queued_work_when_recovering_stale_run(self):
        subscription = app.create_subscription({"performer": "One", "download_dir": "/video/one"})
        app.write_json(app.STATUS_FILE, {"state": "running", "started_at": "2026-11-17T00:00:00+00:00", "results": []})
        app.write_json(app.REQUEST_FILE, [{"trigger": "manual", "subscription_id": subscription["id"]}])
        self.assertTrue(app.recover_interrupted_poll())
        self.assertEqual(app.get_status()["state"], "queued")
        self.assertEqual(app.read_scan_requests()[0]["subscription_id"], subscription["id"])

    def test_subscription_video_listing_returns_number_and_local_filenames(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        video = root / "SSNI-618 1080p 中文字幕 x264.mkv"
        video.touch()
        subtitle = root / "SSNI-618 1080p 中文字幕 x264.MT.zh-HK.srt"
        subtitle.touch()
        subscription = app.create_subscription({"performer": "Actress", "download_dir": "/vol1/video"})
        local_rows = {"SSNI-618": [{"path": str(video), "title": video.name, "rank": [1, 1, 1], "metadata": {"subtitle_available": True}}]}
        app.write_json(app.RECORDS_FILE, {subscription["id"]: {"SSNI-618": {"mteam_metadata": {"id": "1289514", "title": "SSNI-618 4K 非中文 x265", "smallDescr": "LTX模型无码破解 三上悠亚", "rank": [2, 0, 2]}}}})
        with patch.object(app, "scan_local_releases", return_value=local_rows), patch.object(app, "list_local_video_files", side_effect=AssertionError("subscription listing must reuse the scan result")):
            rows = app.get_subscription_videos(subscription["id"])
        self.assertEqual(rows["videos"][0]["code"], "SSNI-618")
        self.assertEqual([item["name"] for item in rows["videos"][0]["files"]], [video.name])
        self.assertEqual(rows["videos"][0]["rank"], [2, 1, 2])
        self.assertEqual(rows["videos"][0]["files"][0]["rank_source"], "M-Team")
        self.assertEqual(rows["videos"][0]["files"][0]["mteam_id"], "1289514")
        self.assertEqual(rows["videos"][0]["files"][0]["subtitle"], "LTX模型无码破解 三上悠亚")
        self.assertEqual(rows["videos"][0]["files"][0]["subtitle_paths"], [str(subtitle)])
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(database.execute("SELECT code, rank_source, rank_json FROM local_videos").fetchall(), [("SSNI-618", "M-Team", "[2, 1, 2]")])
            self.assertEqual(database.execute("SELECT name FROM local_video_files").fetchall(), [(video.name,)])

        video.unlink()
        with patch.object(app, "scan_local_releases", return_value={}), patch.object(app, "list_local_video_files", return_value=[]):
            rows = app.get_subscription_videos(subscription["id"])
        self.assertEqual(rows["videos"], [])
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM local_videos").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM local_video_files").fetchone()[0], 0)

    def test_backfill_mteam_id_requires_matching_mteam_tracker_and_exact_file_path(self):
        root = Path(app.DATA_ROOT) / "backfill"
        root.mkdir()
        video = root / "SSNI-618 1080p x264.mkv"
        video.touch()
        unrelated = root / "SSIS-181 1080p.mkv"
        unrelated.touch()
        subscription = {"id": "backfill", "performer": "Actress", "download_dir": str(root)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])

        class BackfillDownloader:
            config = {"type": "qbittorrent"}

            def list_tasks(self, hashes=None):
                return {
                    "abc": {"name": video.name, "content_path": str(video), "save_path": str(root)},
                    "def": {"name": unrelated.name, "content_path": str(unrelated), "save_path": str(root)},
                    "ghi": {"name": video.name, "content_path": str(root / "other" / video.name), "save_path": str(root / "other")},
                }

            def list_task_files(self, info_hash):
                return [{"name": video.name if info_hash == "abc" else unrelated.name}]

            def task_mteam_metadata(self, info_hash):
                if info_hash == "abc":
                    return {"comment": "https://kp.m-team.cc/detail/1289514"}, [{"url": "udp://tracker.manfuz.co/announce"}]
                return {"comment": "1280000" if info_hash == "def" else "1287777"}, [{"url": "https://tracker.example.net/announce" if info_hash == "def" else "https://tracker.m-team.cc/announce"}]

        result = app.backfill_subscription_mteam_ids(subscription["id"], BackfillDownloader())
        self.assertEqual(result["filled"], 1)
        self.assertEqual(result["already_present"], 0)
        rows = app.get_subscription_videos(subscription["id"])
        by_name = {item["name"]: item["mteam_id"] for row in rows["videos"] for item in row["files"]}
        self.assertEqual(by_name, {video.name: "1289514", unrelated.name: ""})

    def test_backfill_mteam_id_does_not_overwrite_existing_or_ambiguous_ids(self):
        root = Path(app.DATA_ROOT) / "backfill-existing"
        root.mkdir()
        video = root / "SSNI-618 1080p x264.mkv"
        video.touch()
        subscription = {"id": "backfill", "performer": "Actress", "download_dir": str(root)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])
        app.write_json(app.RECORDS_FILE, {subscription["id"]: {"SSNI-618": {"local_files": {str(video): {"id": "1111111"}}}}})

        class BackfillDownloader:
            config = {"type": "qbittorrent"}

            def list_tasks(self, hashes=None):
                return {}

        result = app.backfill_subscription_mteam_ids(subscription["id"], BackfillDownloader())
        self.assertEqual(result["filled"], 0)
        self.assertEqual(result["already_present"], 1)
        self.assertEqual(app.read_json(app.RECORDS_FILE, {})[subscription["id"]]["SSNI-618"]["local_files"][str(video)]["id"], "1111111")

    def test_backfill_repairs_same_number_files_using_their_own_torrent_comments(self):
        root = Path(app.DATA_ROOT) / "repair-duplicate-ids"
        root.mkdir()
        first = root / "SSNI-618-1080p.mp4"
        second = root / "SSNI-618-4K.mp4"
        first.touch()
        second.touch()
        subscription = {"id": "repair", "performer": "Actress", "download_dir": str(root)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])
        app.write_json(app.RECORDS_FILE, {"repair": {"SSNI-618": {"local_files": {
            str(first): {"id": "1289514", "mediainfo": "correct first info"},
            str(second): {"id": "1289514", "mediainfo": "wrong first info", "rank": [1, 0, 1], "subtitle_check": {"status": "not_found"}},
        }}}})

        class BackfillDownloader:
            config = {"type": "qbittorrent"}

            def list_tasks(self):
                return {
                    "firsthash": {"name": first.name, "save_path": str(root), "content_path": str(first)},
                    "secondhash": {"name": second.name, "save_path": str(root), "content_path": str(second)},
                }

            def list_task_files(self, info_hash):
                return [{"name": first.name if info_hash == "firsthash" else second.name}]

            def task_mteam_metadata(self, info_hash):
                identifier = "1289514" if info_hash == "firsthash" else "1283063"
                return {"comment": f"https://kp.m-team.cc/detail/{identifier}"}, [{"url": "https://tracker.manfuz.co/announce"}]

        result = app.backfill_subscription_mteam_ids("repair", BackfillDownloader())
        self.assertEqual(result["filled"], 0)
        self.assertEqual(result["corrected"], 1)
        self.assertEqual(result["verified"], 2)
        saved = app.read_json(app.RECORDS_FILE, {})["repair"]["SSNI-618"]["local_files"]
        self.assertEqual(saved[str(first)]["id"], "1289514")
        self.assertEqual(saved[str(second)]["id"], "1283063")
        self.assertEqual(saved[str(second)]["torrent_hash"], "secondhash")
        self.assertNotIn("mediainfo", saved[str(second)])
        self.assertNotIn("subtitle_check", saved[str(second)])
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(dict(database.execute("SELECT name, mteam_id FROM local_video_files").fetchall()), {first.name: "1289514", second.name: "1283063"})

    def test_backfill_does_not_guess_paths_from_content_path_or_filename(self):
        root = Path(app.DATA_ROOT) / "strict-torrent-paths"
        root.mkdir()
        first = root / "SSNI-618-A.mp4"
        second = root / "SSNI-618-B.mp4"
        first.touch()
        second.touch()
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "strict", "performer": "Actress", "download_dir": str(root)}])

        class BackfillDownloader:
            config = {"type": "qbittorrent"}

            def list_tasks(self):
                return {"hash": {"name": first.name, "save_path": str(root), "content_path": str(second)}}

            def list_task_files(self, info_hash):
                return [{"name": first.name}]

            def task_mteam_metadata(self, info_hash):
                return {"comment": "1289514"}, [{"url": "https://tracker.m-team.cc/announce"}]

        result = app.backfill_subscription_mteam_ids("strict", BackfillDownloader())
        self.assertEqual(result["filled"], 1)
        saved = app.read_json(app.RECORDS_FILE, {})["strict"]["SSNI-618"]["local_files"]
        self.assertEqual(saved[str(first)]["id"], "1289514")
        self.assertNotIn(str(second), saved)

    def test_backfill_distinguishes_identical_filenames_in_mapped_subfolders_and_reuses_sid(self):
        root = Path(app.DATA_ROOT) / "mapped-duplicates"
        (root / "1080p").mkdir(parents=True)
        (root / "4K").mkdir()
        filename = "SSNI-618.mp4"
        first = root / "1080p" / filename
        second = root / "4K" / filename
        first.touch()
        second.touch()
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "mapped", "performer": "Actress", "download_dir": str(root)}])
        client = app.Downloader({"type": "qbittorrent", "url": "http://qb", "path_mappings": [{"local": str(root), "remote": "/downloads"}]})
        calls = []

        def fake_open(request, timeout=30):
            calls.append(request)
            parsed = urllib.parse.urlparse(request.full_url)
            if parsed.path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=one-session"})
            self.assertEqual(request.get_header("Cookie"), "SID=one-session")
            info_hash = urllib.parse.parse_qs(parsed.query).get("hash", [""])[0]
            folder = "1080p" if info_hash == "first" else "4K"
            if parsed.path.endswith("/torrents/info"):
                value = [{"hash": key, "name": f"SSNI-618 {subfolder}", "save_path": "/downloads", "content_path": f"/downloads/{subfolder}"} for key, subfolder in (("first", "1080p"), ("second", "4K"))]
            elif parsed.path.endswith("/files"):
                value = [{"name": f"{folder}/{filename}"}]
            elif parsed.path.endswith("/properties"):
                value = {"comment": "1289514" if info_hash == "first" else "1283063"}
            elif parsed.path.endswith("/trackers"):
                value = [{"url": "https://tracker.manfuz.co/announce"}]
            else:
                raise AssertionError(request.full_url)
            return FakeResponse(json.dumps(value).encode())

        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            result = app.backfill_subscription_mteam_ids("mapped", client)
        saved = app.read_json(app.RECORDS_FILE, {})["mapped"]["SSNI-618"]["local_files"]
        self.assertEqual(result["filled"], 2)
        self.assertEqual(saved[str(first)]["id"], "1289514")
        self.assertEqual(saved[str(second)]["id"], "1283063")
        self.assertEqual(sum(item.full_url.endswith("/auth/login") for item in calls), 1)

    def test_backfill_mteam_id_skips_conflicting_torrents_for_same_local_file(self):
        root = Path(app.DATA_ROOT) / "backfill-ambiguous"
        root.mkdir()
        video = root / "SSNI-618 1080p x264.mkv"
        video.touch()
        subscription = {"id": "backfill", "performer": "Actress", "download_dir": str(root)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])

        class BackfillDownloader:
            config = {"type": "qbittorrent"}

            def list_tasks(self, hashes=None):
                return {
                    "abc": {"name": video.name, "content_path": str(video), "save_path": str(root)},
                    "def": {"name": video.name, "content_path": str(video), "save_path": str(root)},
                }

            def list_task_files(self, info_hash):
                return [{"name": video.name}]

            def task_mteam_metadata(self, info_hash):
                identifier = "1289514" if info_hash == "abc" else "1283063"
                return {"comment": identifier}, [{"url": "https://tracker.m-team.cc/announce"}]

        result = app.backfill_subscription_mteam_ids(subscription["id"], BackfillDownloader())
        self.assertEqual(result["filled"], 0)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(app.read_json(app.RECORDS_FILE, {})[subscription["id"]], {})

    def test_same_number_local_files_keep_individual_mteam_ids_and_ranks(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        first = root / "SSNI-865-UC.mp4"
        second = root / "SSNI-865-U.ltx.mp4"
        first.touch()
        second.touch()
        subscription = app.create_subscription({"performer": "Actress", "download_dir": "/vol1/video"})
        files = [
            {"path": str(first), "title": first.name, "rank": [1, 0, 1], "metadata": {}},
            {"path": str(second), "title": second.name, "rank": [2, 1, 2], "metadata": {}},
        ]
        records = {subscription["id"]: {"SSNI-865": {
            "mteam_metadata": {"id": "999", "rank": [2, 2, 2], "title": "shared wrong release"},
            "local_files": {
                str(first): {"id": "111", "rank": [1, 1, 1], "title": "SSNI-865 UC", "subtitle": "UC release"},
                str(second): {"id": "222", "rank": [2, 2, 2], "title": "SSNI-865 U LTX", "subtitle": "LTX release"},
            },
        }}}
        app.write_json(app.RECORDS_FILE, records)
        with patch.object(app, "scan_local_releases", return_value={"SSNI-865": files}), patch.object(app, "list_local_video_files", return_value=[first, second]):
            result = app.get_subscription_videos(subscription["id"])

        self.assertEqual(len(result["videos"]), 1)
        grouped = result["videos"][0]
        self.assertEqual(grouped["code"], "SSNI-865")
        self.assertEqual({file["mteam_id"] for file in grouped["files"]}, {"111", "222"})
        self.assertEqual({tuple(file["rank"]) for file in grouped["files"]}, {(1, 1, 1), (2, 2, 2)})
        self.assertEqual({file["name"] for file in grouped["files"]}, {first.name, second.name})
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(dict(database.execute("SELECT name, mteam_id FROM local_video_files").fetchall()), {first.name: "111", second.name: "222"})

    def test_local_file_matching_assigns_distinct_ids_from_release_title_variants(self):
        items = [
            {"path": "/videos/SSNI-865-UC.mp4"},
            {"path": "/videos/SSNI-865-U.ltx.mp4"},
        ]
        candidates = [
            {"id": "111", "title": "SSNI-865 UC 1080p 中文字幕"},
            {"id": "222", "title": "SSNI-865 U LTX 4K 内嵌中文"},
        ]
        self.assertEqual(app.match_local_file_candidates(items, candidates), {
            str(Path(items[0]["path"])): "111",
            str(Path(items[1]["path"])): "222",
        })

    def test_local_file_mteam_ids_are_cached_only_after_detail_actor_validation(self):
        root = Path(app.DATA_ROOT) / "variants"
        root.mkdir()
        first = root / "SSNI-865-UC.mp4"
        second = root / "SSNI-865-U.ltx.mp4"
        first.touch()
        second.touch()
        subscription = {"performer": "Actress"}
        items = [
            {"path": str(first), "title": first.name, "rank": [1, 0, 1], "metadata": {}},
            {"path": str(second), "title": second.name, "rank": [2, 0, 2], "metadata": {}},
        ]
        candidates = [
            {"id": "111", "code": "SSNI-865", "title": "SSNI-865 UC 1080p", "rank": [1, 1, 1], "resolution": "1080p", "video_codec": "x264", "metadata_complete": True},
            {"id": "222", "code": "SSNI-865", "title": "SSNI-865 U LTX 4K", "rank": [2, 2, 2], "resolution": "4K", "video_codec": "x265", "metadata_complete": True},
        ]
        record = {"code": "SSNI-865", "mteam_metadata": {"id": "999", "rank": [2, 2, 2], "title": "wrong shared metadata"}, "local_files": {
            str(first): {"id": "111", "rank": [1, 1, 1], "size": first.stat().st_size, "mtime_ns": first.stat().st_mtime_ns, "rank_version": app.LOCAL_MTEAM_RANK_VERSION - 1},
            str(second): {"id": "222", "rank": [2, 2, 2], "size": second.stat().st_size, "mtime_ns": second.stat().st_mtime_ns, "rank_version": app.LOCAL_MTEAM_RANK_VERSION - 1},
        }}
        details = {
            "111": {"performer": "Actress", "resolution": "1080p", "videoCodec": "x264", "hasChineseSubtitle": True},
            "222": {"performer": "Actress", "resolution": "4K", "videoCodec": "x265", "labelsNew": [{"name": "中字", "color": "red"}]},
        }
        with patch.object(app, "mteam_detail", side_effect=lambda identifier, token: details[str(identifier)]):
            app.cache_local_file_mteam_metadata(subscription, {"SSNI-865": items}, candidates, {"SSNI-865": record}, "token")

        self.assertEqual({Path(path).name: item["id"] for path, item in record["local_files"].items()}, {first.name: "111", second.name: "222"})
        self.assertTrue(all(item["rank_version"] == app.LOCAL_MTEAM_RANK_VERSION for item in record["local_files"].values()))
        rows = app.index_subscription_videos("sub", {"SSNI-865": items}, {"SSNI-865": record})
        self.assertEqual({file["mteam_id"] for row in rows for file in row["files"]}, {"111", "222"})

    def test_refresh_local_file_uses_saved_mteam_id_when_search_does_not_return_candidate(self):
        root = Path(app.DATA_ROOT) / "direct-refresh"
        root.mkdir()
        video = root / "IPX-536 4K x265.mkv"
        video.touch()
        subscription = {"performer": "Actress"}
        previous = {"id": "1278591", "title": video.name, "rank": [2, 1, 2], "resolution": "4K", "video_codec": "x265", "size": video.stat().st_size, "mtime_ns": video.stat().st_mtime_ns, "rank_version": app.LOCAL_MTEAM_RANK_VERSION - 1}
        record = {"code": "IPX-536", "local_files": {str(video): previous}}
        detail = {"performer": "Actress", "labelsNew": ["中字", "4k", "AI破解"], "hasChineseSubtitle": False, "resolution": "4K", "videoCodec": "H.265(x265/HEVC)"}
        with patch.object(app, "mteam_detail", return_value=detail) as request:
            app.cache_local_file_mteam_metadata(subscription, {"IPX-536": [{"path": str(video), "title": video.name, "rank": [2, 1, 2], "metadata": {}}]}, [], {"IPX-536": record}, "token", force_refresh=True)
        request.assert_called_once_with("1278591", "token")
        self.assertEqual(record["local_files"][str(video)]["id"], "1278591")
        self.assertEqual(record["local_files"][str(video)]["rank"], [2, 2, 2])

    def test_refresh_subscription_videos_updates_all_local_files_by_their_saved_ids(self):
        root = Path(app.DATA_ROOT) / "refresh-all"
        root.mkdir()
        first = root / "IPX-536 4K x265.mkv"
        second = root / "IPX-691 4K x265.mkv"
        first.touch()
        second.touch()
        subscription = {"id": "refresh-all", "performer": "Actress", "download_dir": str(root)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.write_json(app.RECORDS_FILE, {"refresh-all": {
            "IPX-536": {"local_files": {str(first): {"id": "1278591", "rank": [2, 1, 2]}}},
            "IPX-691": {"local_files": {str(second): {"id": "1286061", "rank": [2, 1, 2]}}},
        }})
        details = {
            "1278591": {"performer": "Actress", "labelsNew": ["中字"], "hasChineseSubtitle": False, "resolution": "4K", "videoCodec": "x265"},
            "1286061": {"performer": "Actress", "labelsNew": ["中字"], "hasChineseSubtitle": False, "resolution": "4K", "videoCodec": "x265"},
        }
        with patch.object(app, "mteam_detail", side_effect=lambda identifier, token: details[str(identifier)]) as detail:
            result = app.refresh_subscription_videos("refresh-all")
        self.assertEqual(result, {"refreshed": 2, "skipped": 0, "failed": 0, "message": "视频信息刷新完成。"})
        self.assertEqual(detail.call_count, 2)
        saved = app.read_json(app.RECORDS_FILE, {})["refresh-all"]
        self.assertEqual(saved["IPX-536"]["local_files"][str(first)]["rank"], [2, 2, 2])
        self.assertEqual(saved["IPX-691"]["local_files"][str(second)]["rank"], [2, 2, 2])

    def test_hover_file_info_loads_and_caches_mteam_mediainfo(self):
        root = Path(app.DATA_ROOT) / "file-info"
        root.mkdir()
        video = root / "IPX-536.mkv"
        video.touch()
        subscription = {"id": "file-info", "performer": "Actress", "download_dir": str(root)}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.write_json(app.RECORDS_FILE, {"file-info": {"IPX-536": {"local_files": {str(video): {"id": "1278591"}}}}})
        mediainfo = "General\nComplete name : IPX-536.mkv\nVideo\nFormat : HEVC"
        with patch.object(app, "mteam_detail", return_value={"performer": "Actress", "mediainfo": mediainfo}) as detail:
            first = app.get_subscription_video_file_info("file-info", "IPX-536", str(video))
            second = app.get_subscription_video_file_info("file-info", "IPX-536", str(video))
        self.assertEqual(first, {"path": str(video.resolve()), "mediainfo": "常规\n完整名称 : IPX-536.mkv\n视频\n格式 : HEVC", "mediainfo_checked": True})
        self.assertEqual(second, first)
        detail.assert_called_once_with("1278591", "token")
        saved = app.read_json(app.RECORDS_FILE, {})["file-info"]["IPX-536"]["local_files"][str(video.resolve())]
        self.assertEqual(saved["mediainfo"], mediainfo)
        self.assertTrue(saved["mediainfo_checked"])

    def test_single_video_refresh_replaces_cached_mediainfo_and_keeps_other_file_metadata(self):
        root = Path(app.DATA_ROOT) / "refresh-mediainfo"
        root.mkdir()
        first = root / "IPX-536-A.mkv"
        second = root / "IPX-536-B.mkv"
        first.touch()
        second.touch()
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "refresh-info", "performer": "Actress", "download_dir": str(root)}])
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        for field in ("mediainfo", "mediaInfo", "media_info"):
            with self.subTest(field=field):
                app.write_json(app.RECORDS_FILE, {"refresh-info": {"IPX-536": {"local_files": {
                    str(first): {"id": "1278591", "mediainfo": "old info", "mediainfo_checked": True, "torrent_hash": "firsthash", "id_source": "qbittorrent_file"},
                    str(second): {"id": "1286061", "mediainfo": "other file info", "mediainfo_checked": True},
                }}}})
                detail = {"performer": "Actress", "resolution": "1080p", "videoCodec": "x265", "labelsNew": ["中字"], field: "Video\nFormat : HEVC\nWidth : 1920 pixels"}
                with patch.object(app, "mteam_detail", return_value=detail) as request:
                    result = app.refresh_subscription_video_file("refresh-info", "IPX-536", str(first))
                    hover = app.get_subscription_video_file_info("refresh-info", "IPX-536", str(first))
                request.assert_called_once_with("1278591", "token", force_refresh=True)
                self.assertEqual(result["file"]["mediainfo"], "视频\n格式 : HEVC\n宽度 : 1920 像素")
                self.assertEqual(result["file"]["mediainfo"], hover["mediainfo"])
                saved = app.read_json(app.RECORDS_FILE, {})["refresh-info"]["IPX-536"]["local_files"]
                self.assertEqual(saved[str(first)]["mediainfo"], detail[field])
                self.assertEqual(saved[str(first)]["torrent_hash"], "firsthash")
                self.assertEqual(saved[str(second)]["mediainfo"], "other file info")

    def test_single_video_refresh_removes_old_mediainfo_when_detail_has_none(self):
        root = Path(app.DATA_ROOT) / "refresh-empty-mediainfo"
        root.mkdir()
        video = root / "IPX-536.mkv"
        video.touch()
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "empty-info", "performer": "Actress", "download_dir": str(root)}])
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.write_json(app.RECORDS_FILE, {"empty-info": {"IPX-536": {"local_files": {str(video): {"id": "1278591", "mediainfo": "outdated", "mediainfo_checked": True}}}}})
        with patch.object(app, "mteam_detail", return_value={"performer": "Actress"}):
            result = app.refresh_subscription_video_file("empty-info", "IPX-536", str(video))
        self.assertEqual(result["file"]["mediainfo"], "")
        self.assertTrue(result["file"]["mediainfo_checked"])

    def test_mediainfo_localization_translates_labels_but_preserves_technical_values(self):
        source = "General\nComplete name : /media/IPX-536.mkv\nFile size : 14.7 GiB\nVideo\nFormat : HEVC\nWidth : 3 840 pixels\nHeight : 2 160 pixels\nAudio\nChannel(s) : 2 channels\nLanguage : Japanese\nText\nLanguage : Chinese"
        self.assertEqual(app.localize_mediainfo(source), "常规\n完整名称 : /media/IPX-536.mkv\n文件大小 : 14.7 GiB\n视频\n格式 : HEVC\n宽度 : 3 840 像素\n高度 : 2 160 像素\n音频\n声道数 : 2 声道\n语言 : 日语\n字幕\n语言 : 中文")

    def test_existing_video_subtitle_is_fetched_once_and_cached(self):
        subscription = app.create_subscription({"performer": "三上悠亚", "download_dir": "/vol1/video"})
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token"})
        app.write_json(app.RECORDS_FILE, {subscription["id"]: {"SSNI-618": {"code": "SSNI-618", "mteam_metadata": {"id": "1279622", "title": "SSNI-618"}}}})
        subtitle = "LTX模型无码破解 三上悠亚 4k高清高画质破解 无水印 5090显卡自行破解 4k x265 60FPS"
        with patch.object(app, "mteam_detail", return_value={"smallDescr": subtitle}) as detail:
            self.assertEqual(app.get_subscription_video_subtitle(subscription["id"], "SSNI-618"), {"subtitle": subtitle})
            self.assertEqual(app.get_subscription_video_subtitle(subscription["id"], "SSNI-618"), {"subtitle": subtitle})
        detail.assert_called_once_with("1279622", "token")
        saved = app.read_json(app.RECORDS_FILE, {})[subscription["id"]]["SSNI-618"]["mteam_metadata"]
        self.assertTrue(saved["subtitle_checked"])

    def test_short_video_subtitle_falls_back_to_mteam_main_title(self):
        subscription = app.create_subscription({"performer": "三上悠亚", "download_dir": "/vol1/video"})
        app.write_json(app.RECORDS_FILE, {subscription["id"]: {"SSNI-618": {"code": "SSNI-618", "mteam_metadata": {"id": "1279622", "title": "SSNI-618 M-Team 主标题", "subtitle": "LTX", "subtitle_checked": True}}}})
        self.assertEqual(app.get_subscription_video_subtitle(subscription["id"], "SSNI-618"), {"subtitle": "SSNI-618 M-Team 主标题"})
        self.assertEqual(app.subtitle_display_text("短标题", "主标题"), "主标题")
        self.assertEqual(app.subtitle_display_text("六个中文字符", "主标题"), "六个中文字符")

    def test_video_tooltip_returns_cached_mteam_subtitle_instead_of_main_title(self):
        subscription = app.create_subscription({"performer": "三上悠亚", "download_dir": "/vol1/video"})
        app.write_json(app.RECORDS_FILE, {subscription["id"]: {"SSNI-618": {"code": "SSNI-618", "mteam_metadata": {
            "id": "1279622", "title": "SSNI-618 M-Team 主标题", "subtitle": "LTX模型无码破解 三上悠亚", "subtitle_checked": True,
        }}}})
        self.assertEqual(app.get_subscription_video_subtitle(subscription["id"], "SSNI-618"), {"subtitle": "LTX模型无码破解 三上悠亚"})

    def test_activity_reads_live_downloader_tasks_and_joins_subscription_by_hash(self):
        subscription = app.create_subscription({"performer": "Actress", "download_dir": "/vol1/video"})
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080", "username": "user", "password": "secret"}})
        app.write_json(app.RECORDS_FILE, {subscription["id"]: {
            "SSNI-618": {"current": {"id": "1289514", "hash": "livehash", "rank": [2, 1, 2]}},
            "FAILED-001": {"last_error": "M-Team 参数错误"},
        }})

        class LiveDownloader:
            def __init__(self, config):
                self.config = config

            def list_tasks(self, hashes=None):
                return {
                    "livehash": {"name": "SSNI-618 4K", "progress": .42, "complete": False, "state": "downloading", "failed": False},
                    "otherhash": {"name": "Other active torrent", "progress": .25, "complete": False, "state": "stalledDL", "failed": False},
                    "numberedhash": {"name": "IPX-811 1080p x264", "progress": .25, "complete": False, "state": "stalledDL", "failed": False},
                    "queuedhash": {"name": "IPX-812 1080p x264", "progress": 0, "complete": False, "state": "queuedDL", "failed": False},
                    "donehash": {"name": "Completed torrent", "progress": 1, "complete": True, "state": "uploading", "failed": False},
                }

        with patch.object(app, "Downloader", LiveDownloader):
            rows = app.get_activity()
        self.assertEqual(len(rows), 3)
        mapped = next(item for item in rows if item["hash"] == "livehash")
        self.assertEqual((mapped["subscription"], mapped["code"], mapped["progress"], mapped["mteam_id"]), ("Actress", "SSNI-618", 42.0, "1289514"))
        self.assertEqual(mapped["message"], "qBittorrent：下载中")
        unknown_subscription = next(item for item in rows if item["hash"] == "numberedhash")
        self.assertEqual((unknown_subscription["subscription"], unknown_subscription["code"]), ("qBittorrent", "IPX-811"))
        queued = next(item for item in rows if item["hash"] == "queuedhash")
        self.assertEqual(queued["message"], "qBittorrent：等待下载")
        self.assertNotIn("otherhash", [item["hash"] for item in rows])
        self.assertNotIn("FAILED-001", [item["code"] for item in rows])

    def test_activity_joins_untracked_task_by_number_to_failed_subscription_record(self):
        subscription = app.create_subscription({"performer": "三上悠亚", "download_dir": "/vol1/video"})
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080", "username": "user", "password": "secret"}})
        app.write_json(app.RECORDS_FILE, {subscription["id"]: {
            "SSIS-663": {
                "last_error": "下载器 API 请求失败 (HTTP 409)",
                "mteam_metadata": {"title": "SSIS-663 引退発表 126日後にAV女優をやめる三上悠亜", "rank": [1, 2, 2]},
            },
        }})

        class LiveDownloader:
            def __init__(self, config):
                self.config = config

            def list_tasks(self, hashes=None):
                return {"orphanhash": {"name": "SSIS-663 引退発表 126日後にAV女優をやめる三上悠亜", "progress": .06, "complete": False, "state": "downloading", "failed": False}}

        with patch.object(app, "Downloader", LiveDownloader):
            rows = app.get_activity()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["subscription_id"], rows[0]["subscription"], rows[0]["code"]), (subscription["id"], "三上悠亚", "SSIS-663"))

    def test_activity_surfaces_downloader_read_errors(self):
        app.write_json(app.SETTINGS_FILE, {"downloader": {"type": "qbittorrent", "url": "http://nas:8080"}})

        class UnavailableDownloader:
            def __init__(self, config):
                pass

            def list_tasks(self, hashes=None):
                raise app.AppError("连接失败")

        with patch.object(app, "Downloader", UnavailableDownloader):
            rows = app.get_activity()
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("无法读取下载器任务", rows[0]["message"])

    def test_manual_scan_queue_and_run_are_written_to_log(self):
        app.log_event("queue test", event="queued")
        app.log_event("worker started", event="started")
        self.assertEqual([item["event"] for item in app.get_logs()], ["queued", "started"])

    def test_logs_can_be_cleared_for_one_performer_or_all(self):
        app.log_event("one", subscription="Actress")
        app.log_event("other subscription", subscription="Other")
        app.log_event("background")
        self.assertEqual(app.clear_logs("Actress"), {"deleted": 1})
        self.assertEqual([item["message"] for item in app.get_logs()], ["other subscription", "background"])
        self.assertEqual(app.clear_logs(), {"deleted": 2})
        self.assertEqual(app.get_logs(), [])

    def test_local_video_files_are_indexed_by_number(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        (root / "IPX-811 1080p x264.mkv").write_bytes(b"old")
        (root / "notes.txt").write_text("not a video", encoding="utf-8")
        found = app.scan_local_releases(str(root))
        self.assertEqual(found["IPX-811"][0]["rank"], [1, 0, 1])
        self.assertNotIn("NOTES", found)

    def test_local_video_scan_uses_matching_nfo_for_wash_rank(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        video = root / "IPX-811 1080p x264.mkv"
        video.touch()
        video.with_suffix(".nfo").write_text("<movie><fileinfo><streamdetails><video><width>1920</width><height>1080</height><codec>h264</codec></video></streamdetails></fileinfo></movie>", encoding="utf-8")
        (root / "IPX-811.zh-CN.srt").touch()
        found = app.scan_local_releases(str(root))
        self.assertEqual(found["IPX-811"][0]["rank"], [1, 1, 1])
        self.assertTrue(found["IPX-811"][0]["metadata"]["subtitle_available"])

    def test_cleanup_orphan_sidecars_removes_only_unpaired_nfo_and_subtitles(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        video = root / "SSNI-229-U.ltx.mp4"
        video.touch()
        paired_nfo = video.with_suffix(".nfo")
        paired_subtitle = root / "SSNI-229.zh.srt"
        orphan_nfo = root / "IPX-811.nfo"
        orphan_subtitle = root / "IPX-811.eng.ass"
        for path in (paired_nfo, paired_subtitle, orphan_nfo, orphan_subtitle):
            path.touch()

        removed = app.cleanup_orphan_sidecars(str(root))

        self.assertCountEqual(removed, [str(orphan_nfo), str(orphan_subtitle)])
        self.assertTrue(paired_nfo.exists())
        self.assertTrue(paired_subtitle.exists())
        self.assertFalse(orphan_nfo.exists())
        self.assertFalse(orphan_subtitle.exists())

    def test_cleanup_orphan_sidecars_removes_newly_empty_subfolders_only(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        orphan_dir = root / "orphan" / "nested"
        orphan_dir.mkdir(parents=True)
        orphan_nfo = orphan_dir / "IPX-811.nfo"
        orphan_subtitle = orphan_dir / "IPX-811.zh.srt"
        orphan_nfo.touch()
        orphan_subtitle.touch()
        keep_dir = root / "has-video"
        keep_dir.mkdir()
        (keep_dir / "SSNI-229.mp4").touch()
        (keep_dir / "other.txt").touch()

        removed = app.cleanup_orphan_sidecars(str(root))

        self.assertCountEqual(removed, [str(orphan_nfo), str(orphan_subtitle)])
        self.assertFalse((root / "orphan").exists())
        self.assertTrue(root.is_dir())
        self.assertTrue(keep_dir.is_dir())
        self.assertTrue((keep_dir / "SSNI-229.mp4").exists())
        self.assertTrue((keep_dir / "other.txt").exists())

    def test_subscription_scan_cleans_orphan_sidecars_only_when_enabled(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        orphan = root / "IPX-811.nfo"
        orphan.touch()
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        with patch.object(app, "mteam_search", return_value=[]):
            app.process_subscription(subscription, {}, {"mteam_token": "token", "cleanup_orphan_sidecars": False}, FakeDownloader(), [])
        self.assertTrue(orphan.exists())
        with patch.object(app, "mteam_search", return_value=[]):
            app.process_subscription(subscription, {}, {"mteam_token": "token", "cleanup_orphan_sidecars": True}, FakeDownloader(), [])
        self.assertFalse(orphan.exists())

    def test_local_video_scan_enumerates_nfo_files_once_per_directory(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        for number in (811, 812, 813):
            video = root / f"IPX-{number}.mkv"
            video.touch()
            video.with_suffix(".nfo").write_text("<movie><video><height>2160</height></video></movie>", encoding="utf-8")
        original_glob = Path.glob
        nfo_glob_calls = []

        def tracked_glob(path, pattern):
            if pattern == "*.nfo":
                nfo_glob_calls.append(str(path))
            return original_glob(path, pattern)

        with patch.object(Path, "glob", tracked_glob):
            found = app.scan_local_releases(str(root))
        self.assertEqual(len(found), 3)
        self.assertEqual(nfo_glob_calls, [str(root)])

    def test_local_video_scan_uses_nfo_title_when_filename_has_no_number(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        video = root / "long-title.mkv"
        video.touch()
        video.with_suffix(".nfo").write_text("<movie><title>SSIS-181</title></movie>", encoding="utf-8")
        found = app.scan_local_releases(str(root))
        self.assertEqual(found["SSIS-181"][0]["path"], str(video))

    def test_unrelated_subtitle_does_not_raise_local_video_language_rank(self):
        root = Path(app.DATA_ROOT) / "videos"
        root.mkdir()
        (root / "IPX-811 1080p x264.mkv").touch()
        (root / "IPX-812.srt").touch()
        found = app.scan_local_releases(str(root))
        self.assertEqual(found["IPX-811"][0]["rank"], [1, 0, 1])
        self.assertFalse(found["IPX-811"][0]["metadata"]["subtitle_available"])

    def test_local_video_scan_reports_inaccessible_or_missing_directory(self):
        with self.assertRaisesRegex(app.AppError, "目录不存在或应用用户无法访问"):
            app.scan_local_releases(str(Path(app.DATA_ROOT) / "missing-directory"))


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(app, "DATA_ROOT", Path(temporary.name)))

    def test_qbittorrent_backfill_queries_torrent_files_properties_and_trackers(self):
        downloader = app.Downloader({"type": "qbittorrent", "url": "http://qb", "username": "user", "password": "pass"})
        downloader.cookies["SID"] = "session"
        responses = {
            "/api/v2/torrents/files?hash=abc": [{"name": "SSNI-618.mkv"}],
            "/api/v2/torrents/properties?hash=abc": {"comment": "1294522"},
            "/api/v2/torrents/trackers?hash=abc": [{"url": "https://tracker.m-team.cc/announce"}],
        }
        requested = []

        def fake_json(path, **kwargs):
            requested.append((path, kwargs.get("headers")))
            return responses[path], None

        with patch.object(downloader, "_json", side_effect=fake_json):
            files = downloader.list_task_files("ABC")
            metadata = downloader.task_mteam_metadata("ABC")
        self.assertEqual(files, [{"name": "SSNI-618.mkv"}])
        self.assertEqual(metadata, (responses["/api/v2/torrents/properties?hash=abc"], responses["/api/v2/torrents/trackers?hash=abc"]))
        self.assertEqual([path for path, _headers in requested], list(responses))
        self.assertTrue(all(headers == {"Cookie": "SID=session"} for _path, headers in requested))

    def test_downloader_response_body_reads_incrementally(self):
        class ChunkedResponse:
            def __init__(self):
                self.parts = [b"{\"state\":", b"\"downloading\"}", b""]
                self.read_sizes = []

            def read1(self, size):
                self.read_sizes.append(size)
                return self.parts.pop(0)

            def read(self, _size):
                raise AssertionError("bulk reads can wait for the full requested size")

        response = ChunkedResponse()
        self.assertEqual(app.read_response_body(response, 1024), b'{"state":"downloading"}')
        self.assertTrue(response.read_sizes)
        self.assertLessEqual(max(response.read_sizes), 64 * 1024)

    def test_poll_downloader_records_queries_only_tracked_active_hashes(self):
        class FilteredDownloader:
            def __init__(self):
                self.requested = None
                self.progress = []
                self._progress = lambda message, event: self.progress.append((message, event))

            def list_tasks(self, hashes=None):
                self.requested = set(hashes or ())
                return {"activehash": {"progress": .5, "complete": False}}

        downloader = FilteredDownloader()
        records = {
            "IPX-811": {"current": {"hash": "activehash", "state": "downloading"}},
            "IPX-812": {"current": {"hash": "donehash", "state": "completed"}},
            "IPX-813": {"last_error": "previous failure"},
        }
        app.poll_downloader_records(downloader, records, [])
        self.assertEqual(downloader.requested, {"activehash", "donehash"})
        self.assertEqual(downloader.progress, [("正在处理下载任务状态（2 个任务）。", "task_status_started")])

    def test_qbittorrent_task_lookup_filters_to_requested_hashes(self):
        info_hash = "a" * 40
        calls = []

        def fake_open(request, timeout=30):
            calls.append(request)
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc"})
            if "/torrents/info?" in path:
                return FakeResponse(json.dumps([{"hash": info_hash, "progress": .5, "state": "downloading"}]).encode())
            raise AssertionError(path)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            tasks = client.list_tasks([info_hash])
        request = next(item for item in calls if "/torrents/info?" in item.full_url)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query), {"hashes": [info_hash]})
        self.assertIn(info_hash, tasks)

    def test_path_mappings_validate_and_use_longest_prefix(self):
        mappings = app.normalize_path_mappings([
            {"local": "/vol2/1000/media", "remote": "/media"},
            {"local": "/vol2/1000/media/adult", "remote": "/adult"},
            {"local": "", "remote": ""},
        ])
        self.assertEqual(app.map_path_prefix("/vol2/1000/media/adult/女优", mappings, "local", "remote"), "/adult/女优")
        self.assertEqual(app.map_path_prefix("/vol2/1000/media2/file", mappings, "local", "remote"), "/vol2/1000/media2/file")
        self.assertEqual(app.map_path_prefix("/adult/女优/file", mappings, "remote", "local"), "/vol2/1000/media/adult/女优/file")
        with self.assertRaisesRegex(app.AppError, "必须填写"):
            app.normalize_path_mappings([{"local": "/vol2/media", "remote": ""}])

    def test_qbittorrent_maps_add_and_task_paths(self):
        raw_torrent = torrent_bytes()
        info_hash = app.torrent_info_hash(raw_torrent)
        calls = []

        def fake_open(request, timeout=30):
            calls.append(request)
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc"})
            if path.endswith("/torrents/add"):
                return FakeResponse(b"Ok.")
            if "/torrents/info" in path:
                return FakeResponse(json.dumps([{"hash": info_hash, "name": "demo", "progress": .4, "state": "downloading", "save_path": "/media/adult", "content_path": "/media/adult/demo.mkv"}]).encode())
            raise AssertionError(path)

        config = {"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p", "path_mappings": [{"local": "/vol2/1000/media", "remote": "/media"}]}
        client = app.Downloader(config)
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            client.add(raw_torrent, "demo", "/vol2/1000/media/adult")
            task = client.list_tasks()[info_hash]
        add_request = next(item for item in calls if item.full_url.endswith("/torrents/add"))
        self.assertIn(b"/media/adult", add_request.data)
        self.assertEqual(task["save_path"], "/vol2/1000/media/adult")
        self.assertEqual(task["content_path"], "/vol2/1000/media/adult/demo.mkv")

    def test_qbittorrent_login_add_list_and_remove(self):
        raw_torrent = torrent_bytes()
        info_hash = app.torrent_info_hash(raw_torrent)
        calls = []

        def fake_open(request, timeout=30):
            calls.append(request)
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc; HttpOnly"})
            if path.endswith("/app/version"):
                return FakeResponse(b"v4")
            if path.endswith("/torrents/add"):
                return FakeResponse(b"Ok.")
            if "/torrents/info" in path:
                return FakeResponse(json.dumps([{"hash": info_hash, "name": "demo", "progress": 1, "state": "uploading", "save_path": "/video"}]).encode())
            if path.endswith("/torrents/delete"):
                return FakeResponse(b"")
            raise AssertionError(path)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            self.assertEqual(client.test(), "qBittorrent 连接成功。")
            self.assertEqual(client.add(raw_torrent, "demo", "/video"), info_hash)
            self.assertTrue(client.list_tasks()[info_hash]["complete"])
            client.remove(info_hash, True)
        add_request = next(item for item in calls if item.full_url.endswith("/torrents/add"))
        self.assertIn(b"savepath", add_request.data)
        self.assertIn(b"/video", add_request.data)
        delete_request = next(item for item in calls if item.full_url.endswith("/torrents/delete"))
        self.assertIn(b"deleteFiles=true", delete_request.data)
        self.assertEqual(sum(item.full_url.endswith("/auth/login") for item in calls), 1)

    def test_qbittorrent_sid_is_shared_between_clients_and_isolated_by_credentials_and_url(self):
        config = {"url": "http://nas:8080", "username": "u", "password": "p"}
        logins = []

        def fake_open(request, timeout=30):
            if request.full_url.endswith("/auth/login"):
                logins.append(request.full_url)
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=shared; HttpOnly"})
            self.assertEqual(request.get_header("Cookie"), "SID=shared")
            return FakeResponse(b"v5.0")

        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            app.Downloader(config).test()
            second = app.Downloader(dict(config))
            second.test()
            self.assertEqual(len(logins), 1)
            app.Downloader({**config, "password": "new-password"}).test()
            app.Downloader({**config, "url": "http://other:8080"}).test()
            app.Downloader({**config, "username": "other"}).test()
        self.assertEqual(len(logins), 4)
        session = json.loads(second._session_file.read_text(encoding="utf-8"))
        self.assertEqual(session, {"sid": "shared"})

    def test_qbittorrent_expired_sid_reauthenticates_once_and_preserves_post_body(self):
        for status in (401, 403):
            with self.subTest(status=status):
                config = {"url": f"http://nas:{status}", "username": "u", "password": "p"}
                client = app.Downloader(config)
                app.write_json(client._session_file, {"sid": "expired"})
                logins = []
                requests = []

                def fake_open(request, timeout=30):
                    if request.full_url.endswith("/auth/login"):
                        logins.append(request)
                        return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=renewed"})
                    requests.append(request)
                    if request.get_header("Cookie") == "SID=expired":
                        raise urllib.error.HTTPError(request.full_url, status, "expired", Message(), io.BytesIO())
                    self.assertEqual(request.get_header("Cookie"), "SID=renewed")
                    return FakeResponse()

                with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
                    client.remove("abc", True)
                self.assertEqual(len(logins), 1)
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0].data, requests[1].data)
                self.assertEqual(requests[1].get_method(), "POST")
                self.assertIn(b"deleteFiles=true", requests[1].data)

    def test_qbittorrent_auth_failure_does_not_retry_forever(self):
        client = app.Downloader({"url": "http://nas:8080"})
        app.write_json(client._session_file, {"sid": "expired"})
        calls = []

        def fake_open(request, timeout=30):
            calls.append(request)
            if request.full_url.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=also-rejected"})
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", Message(), io.BytesIO())

        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            with self.assertRaisesRegex(app.AppError, "认证失败"):
                client.test()
        self.assertEqual(len(calls), 3)

    def test_qbittorrent_reuses_sid_already_renewed_by_another_client(self):
        client = app.Downloader({"url": "http://nas:8080"})
        client.cookies["SID"] = "expired"
        app.write_json(client._session_file, {"sid": "renewed-by-other-client"})
        calls = []

        def fake_open(request, timeout=30):
            calls.append(request)
            if request.get_header("Cookie") == "SID=expired":
                raise urllib.error.HTTPError(request.full_url, 403, "expired", Message(), io.BytesIO())
            self.assertEqual(request.get_header("Cookie"), "SID=renewed-by-other-client")
            return FakeResponse(b"v5")

        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            client.test()
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(item.full_url.endswith("/auth/login") for item in calls))

    def test_qbittorrent_duplicate_is_tracked_without_claiming_task_ownership(self):
        raw_torrent = torrent_bytes()
        info_hash = app.torrent_info_hash(raw_torrent)
        calls = []
        progress = []

        def fake_open(request, timeout=30):
            calls.append(request)
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc"})
            if path.endswith("/torrents/add"):
                return FakeResponse(b"Fails.")
            if "/torrents/info" in path:
                return FakeResponse(json.dumps([{"hash": info_hash, "progress": 1, "state": "uploading", "save_path": "/downloads", "content_path": "/downloads/demo.mkv"}]).encode())
            if path.endswith("/torrents/setLocation") or path.endswith("/torrents/recheck"):
                return FakeResponse(b"")
            raise AssertionError(path)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p"})
        client.progress_callback = lambda message, event: progress.append((message, event))
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            result = client.add(raw_torrent, "demo", "/other")
        self.assertEqual(result["hash"], info_hash)
        self.assertFalse(result["owned"])
        self.assertEqual(result["existing_task"]["content_path"], "/downloads/demo.mkv")
        self.assertTrue(result["recheck_requested"])
        info_request = next(item for item in calls if item.full_url.endswith("/torrents/info?hashes=" + info_hash))
        self.assertIsNotNone(info_request)
        duplicate_log = next(message for message, _event in progress if "已存在相同种子" in message)
        self.assertIn("/downloads/demo.mkv", duplicate_log)
        self.assertTrue(any(item.full_url.endswith("/torrents/recheck") for item in calls))

    def test_qbittorrent_http_409_tracks_matching_existing_task(self):
        raw_torrent = torrent_bytes()
        info_hash = app.torrent_info_hash(raw_torrent)

        def fake_open(request, timeout=30):
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc"})
            if path.endswith("/torrents/add"):
                raise urllib.error.HTTPError(request.full_url, 409, "Conflict", Message(), io.BytesIO(b"Torrent already exists"))
            if "/torrents/info" in path:
                return FakeResponse(json.dumps([{"hash": info_hash, "progress": 0.5, "state": "downloading"}]).encode())
            raise AssertionError(path)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            result = client.add(raw_torrent, "demo", "/video")
        self.assertEqual(result["hash"], info_hash)
        self.assertFalse(result["owned"])
        self.assertEqual(result["existing_task"]["state"], "downloading")

    def test_qbittorrent_recheck_requests_only_the_missing_task_hash(self):
        calls = []
        info_hash = "a" * 40

        def fake_open(request, timeout=30):
            calls.append(request)
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc"})
            if path.endswith("/torrents/setLocation") or path.endswith("/torrents/recheck"):
                return FakeResponse(b"")
            raise AssertionError(path)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p", "path_mappings": [{"local": "/vol2/media", "remote": "/downloads"}]})
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            location = client.recheck(info_hash, "/vol2/media/Actress")
        self.assertEqual(location, "/downloads/Actress")
        location_request = next(item for item in calls if item.full_url.endswith("/torrents/setLocation"))
        location_fields = urllib.parse.parse_qs(location_request.data.decode())
        self.assertEqual(location_fields["hashes"], [info_hash])
        self.assertEqual(location_fields["location"], ["/downloads/Actress"])
        request = next(item for item in calls if item.full_url.endswith("/torrents/recheck"))
        self.assertIn(("hashes=" + info_hash).encode(), request.data)
        self.assertLess(calls.index(location_request), calls.index(request))

    def test_qbittorrent_missing_data_does_not_restart_a_paused_task(self):
        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080"})
        with patch.object(client, "recheck") as recheck:
            result = client._track_existing_qb_task("a" * 40, {"state": "pausedDL", "content_path": "/missing/video.mkv"})
        recheck.assert_not_called()
        self.assertTrue(result["recheck_skipped_paused"])

    def test_qbittorrent_http_409_reports_response_when_hash_is_absent(self):
        raw_torrent = torrent_bytes()

        def fake_open(request, timeout=30):
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc"})
            if path.endswith("/torrents/add"):
                raise urllib.error.HTTPError(request.full_url, 409, "Conflict", Message(), io.BytesIO(b"save path is invalid"))
            if "/torrents/info" in path:
                return FakeResponse(b"[]")
            raise AssertionError(path)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            with self.assertRaisesRegex(app.AppError, "HTTP 409.*save path is invalid"):
                client.add(raw_torrent, "demo", "/video")

    def test_qbittorrent_add_accepts_success_json_response(self):
        raw_torrent = torrent_bytes()
        info_hash = app.torrent_info_hash(raw_torrent)

        def fake_open(request, timeout=30):
            path = request.full_url.split("nas:8080", 1)[-1]
            if path.endswith("/auth/login"):
                return FakeResponse(b"Ok.", headers={"Set-Cookie": "SID=abc; HttpOnly"})
            if path.endswith("/torrents/add"):
                result = {"added_torrent_ids": [info_hash], "failure_count": 0, "pending_count": 0, "success_count": 1}
                return FakeResponse(json.dumps(result).encode())
            raise AssertionError(path)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            self.assertEqual(client.add(raw_torrent, "demo", "/video"), info_hash)

    def test_qbittorrent_login_distinguishes_rejected_credentials_from_html_route(self):
        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8080", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", return_value=FakeResponse(b"Fails.")):
            with self.assertRaisesRegex(app.AppError, "Web API 已拒绝登录"):
                client._qb_login()
        with patch.object(app.urllib.request, "urlopen", return_value=FakeResponse(b"<!doctype html><html>login</html>", headers={"Content-Type": "text/html"})):
            with self.assertRaisesRegex(app.AppError, "返回了网页"):
                client._qb_login()

    def test_qbittorrent_accepts_204_login_when_session_cookie_is_present(self):
        calls = []

        def fake_open(request, timeout=30):
            calls.append(request)
            if request.full_url.endswith("/api/v2/auth/login"):
                return FakeResponse(b"", status=204, headers={"Set-Cookie": "SID=session-cookie; HttpOnly"})
            if request.full_url.endswith("/api/v2/app/version"):
                self.assertIn("SID=session-cookie", request.get_header("Cookie"))
                return FakeResponse(b"v5.3.0")
            raise AssertionError(request.full_url)

        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8085", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", side_effect=fake_open):
            self.assertEqual(client.test(), "qBittorrent 连接成功。")
        self.assertEqual(len(calls), 2)

    def test_qbittorrent_rejects_204_login_without_session_cookie(self):
        client = app.Downloader({"type": "qbittorrent", "url": "http://nas:8085", "username": "u", "password": "p"})
        with patch.object(app.urllib.request, "urlopen", return_value=FakeResponse(b"", status=204)):
            with self.assertRaisesRegex(app.AppError, "没有会话 Cookie"):
                client._qb_login()

class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.directory = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(patch.object(app, "LOCAL_VIDEO_DATABASE", __import__("pathlib").Path(self.directory.name) / "local-videos.sqlite3"))
        for name in ("DATA_ROOT", "SETTINGS_FILE", "SUBSCRIPTIONS_FILE", "RECORDS_FILE", "STATUS_FILE", "BACKFILL_STATUS_FILE", "BACKFILL_STATUS_LOCK_FILE", "LOG_FILE", "LOG_LOCK_FILE", "REQUEST_FILE", "LOCK_FILE", "DISABLED_FILE", "MTEAM_LIMIT_FILE", "MTEAM_REQUEST_STATE_FILE", "MTEAM_REQUEST_LOCK_FILE"):
            self.enterContext(patch.object(app, name, __import__("pathlib").Path(self.directory.name) / (name.lower() + ".json")))
        self.enterContext(patch.object(app, "mteam_detail", return_value={"performer": "三上悠亚 Actress"}))

    def test_subscription_check_collects_subtitles_with_no_search_results(self):
        root = Path(self.directory.name) / "export-local-subtitles"
        root.mkdir()
        video = root / "ABC-123 1080p x264.mkv"
        video.touch()
        subtitle = root / "ABC-123 1080p x264.MT.zh-HK.srt"
        subtitle.write_text("Chinese subtitles", encoding="utf-8")
        dest = Path(self.directory.name) / "collected-subtitles"
        subscription = {"id": "export", "performer": "Actress", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        app.write_json(app.SUBSCRIPTIONS_FILE, [subscription])
        records = {"export": {"ABC-123": {"local_files": {str(video): {"id": "1289514", "rank": [1, 1, 1], "metadata_complete": True}}}}}
        settings = {"mteam_token": "token", "subtitle_collection_dir": str(dest)}
        with patch.object(app, "mteam_search", return_value=[]), patch.object(app, "mteam_subtitle_download", side_effect=AssertionError("local subtitle already exists")):
            app.process_subscription(subscription, records, settings, FakeDownloader(), [])
        self.assertEqual((dest / "ABC-123.srt").read_text(), "Chinese subtitles")
        self.assertTrue(subtitle.exists())

    def test_non_chinese_local_video_checks_subtitles_even_when_search_returns_no_results(self):
        root = Path(self.directory.name) / "subtitle-check"
        root.mkdir()
        video = root / "IPX-536.mp4"
        video.touch()
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(root)}
        records = {"sub": {"IPX-536": {"local_files": {str(video): {"id": "1278591", "rank": [2, 0, 2]}}}}}
        with patch.object(app, "mteam_search", return_value=[]), patch.object(app, "mteam_subtitle_download", return_value={"status": "not_found", "files": []}) as download:
            app.process_subscription(subscription, records, {"mteam_token": "token"}, FakeDownloader(), [])
        download.assert_called_once_with("IPX-536", "1278591", [video], "token")

    def test_keyword_query_is_performer_plus_additional_term(self):
        subscription = {"id": "sub", "performer": "三上悠亚", "keywords": "ltx", "download_dir": "/video", "wash_enabled": True}
        downloader = FakeDownloader()
        records = {}
        captured = []
        candidate = {"id": "1", "code": "SSNI-618", "title": "SSNI-618 1080p x265", "rank": [1, 0, 2], "seeders": 10, "created": "2026"}
        with patch.object(app, "mteam_search", side_effect=lambda keyword, token: captured.append(keyword) or [candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        self.assertEqual(captured, ["三上悠亚 ltx"])
        self.assertEqual(records["sub"]["SSNI-618"]["current"]["state"], "downloading")
        self.assertEqual(records["sub"]["SSNI-618"]["mteam_metadata"]["id"], "1")
        self.assertEqual(downloader.added[0][2], "/video")

    def test_multi_directory_local_wash_is_opt_in_and_cleans_only_after_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_dir = root / "disk-one" / "三上悠亚"
            second_dir = root / "disk-two" / "三上悠亚"
            unrelated_dir = root / "disk-three" / "樱空桃"
            first_dir.mkdir(parents=True)
            second_dir.mkdir(parents=True)
            unrelated_dir.mkdir(parents=True)
            first_video = first_dir / "IPX-811 1080p x264.mkv"
            second_video = second_dir / "IPX-811 1080p 中文字幕 x265.mkv"
            unrelated_video = unrelated_dir / "IPX-811 4K 内嵌中文 x265.mkv"
            first_video.write_bytes(b"old 1")
            second_video.write_bytes(b"old 2")
            unrelated_video.write_bytes(b"other performer")
            app.write_json(app.SUBSCRIPTIONS_FILE, [
                {"id": "sub", "performer": "三上 悠亚", "download_dir": str(first_dir)},
                {"id": "other", "performer": "三上悠亚", "download_dir": str(second_dir)},
                {"id": "unrelated", "performer": "樱空桃", "download_dir": str(unrelated_dir)},
            ])
            subscription = {"id": "sub", "performer": "三上 悠亚", "keywords": "", "download_dir": str(first_dir), "wash_enabled": True}
            candidate = {"id": "1", "code": "IPX-811", "title": "IPX-811 4K 内嵌中文 x265", "rank": [2, 2, 2], "seeders": 10, "created": "2026"}
            records = {}
            downloader = FakeDownloader("newhash")
            with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()):
                app.process_subscription(subscription, records, {"mteam_token": "token", "local_multi_disk_wash": True}, downloader, [])
            current = records["sub"]["IPX-811"]["current"]
            self.assertCountEqual(current["local_paths"], [str(first_video), str(second_video)])
            self.assertCountEqual(current["local_roots"], [str(first_dir), str(second_dir)])
            self.assertIn("pending", records["sub"]["IPX-811"])
            self.assertTrue(first_video.exists())
            self.assertTrue(second_video.exists())

            downloader.complete = True
            app.poll_downloader_records(downloader, records["sub"], [])
            self.assertFalse(first_video.exists())
            self.assertFalse(second_video.exists())
            self.assertTrue(unrelated_video.exists())

    def test_multi_directory_local_wash_disabled_scans_only_subscription_directory(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video/one", "wash_enabled": True}
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "other", "performer": "Actress", "download_dir": "/video/two"}])
        with patch.object(app, "scan_local_releases", return_value={}) as scan, patch.object(app, "mteam_search", return_value=[]):
            app.process_subscription(subscription, {}, {"mteam_token": "token", "local_multi_disk_wash": False}, FakeDownloader(), [])
        scan.assert_called_once_with("/video/one")

    def test_multi_directory_scan_cache_scans_each_folder_once_per_poll(self):
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "one", "performer": "Actress", "download_dir": "/disk/one"},
            {"id": "two", "performer": "Actress", "download_dir": "/disk/two"},
        ])
        subscriptions = app.list_subscriptions()
        cache = {}
        scan_results = {
            "/disk/one": {"IPX-811": [{"path": "/disk/one/IPX-811.mkv", "title": "IPX-811 local one", "rank": [1, 0, 1], "metadata": {}}]},
            "/disk/two": {"IPX-811": [{"path": "/disk/two/IPX-811.mkv", "title": "IPX-811 local two", "rank": [2, 1, 2], "metadata": {}}]},
        }
        with patch.object(app, "scan_local_releases", side_effect=lambda directory: scan_results[directory]) as scan, patch.object(app, "mteam_search", return_value=[]), patch.object(app, "index_subscription_videos") as index:
            for subscription in subscriptions:
                app.process_subscription(subscription, {}, {"mteam_token": "token", "local_multi_disk_wash": True}, FakeDownloader(), [], local_scan_cache=cache)
        self.assertCountEqual([call.args[0] for call in scan.call_args_list], ["/disk/one", "/disk/two"])
        self.assertEqual(index.call_args_list[0].args[1], scan_results["/disk/one"])
        self.assertEqual(index.call_args_list[1].args[1], scan_results["/disk/two"])

    def test_detail_actor_mismatch_is_skipped_instead_of_saved_to_wrong_folder(self):
        subscription = {"id": "sub", "performer": "桃乃木香奈", "keywords": "", "download_dir": "/video/momonogi", "wash_enabled": True}
        candidate = {"id": "1280000", "code": "IPZZ-034", "title": "IPZZ-034", "rank": [1, 0, 1], "seeders": 10, "created": "2026"}
        records = {}
        results = []
        downloader = FakeDownloader()
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "樱空桃"}), patch.object(app, "fetch_torrent") as fetch, patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)
        fetch.assert_not_called()
        self.assertEqual(downloader.added, [])
        self.assertEqual(records["sub"], {})
        self.assertEqual(results[0]["state"], "skipped")
        self.assertIn("未找到女优关键字", results[0]["message"])

    def test_detail_performer_match_ignores_spacing_and_punctuation(self):
        self.assertTrue(app.detail_contains_performer({"actors": [{"name": "樱空桃"}]}, " 樱空 桃 "))

    def test_refresh_existing_info_forces_detail_for_cached_number(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        cached = {"id": "1289514", "title": "SSIS-181", "rank": [1, 1, 1], "resolution": "1080p", "video_codec": "x264", "metadata_complete": True}
        candidate = {"id": "1289514", "code": "SSIS-181", "title": "SSIS-181", "rank": [1, 1, 1], "resolution": "1080p", "video_codec": "x264", "metadata_complete": True, "seeders": 10, "created": "2026"}
        records = {"sub": {"SSIS-181": {"code": "SSIS-181", "mteam_metadata": cached}}}
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"labelsNew": [{"name": "中字", "color": "red"}], "resolution": "2160p", "videoCodec": "H.265(x265/HEVC)"}) as detail, patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, FakeDownloader(), [], refresh_existing_info=True)
        detail.assert_called_once_with("1289514", "token")
        self.assertEqual(records["sub"]["SSIS-181"]["mteam_metadata"]["rank"], [2, 2, 2])

    def test_unchecked_scan_refreshes_rank_when_mteam_rank_rule_version_changes(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        cached = {"id": "1", "title": "SSNI-618", "rank": [2, 2, 2], "resolution": "4K", "video_codec": "x265", "metadata_complete": True}
        candidate = {"id": "1", "code": "SSNI-618", "title": "SSNI-618", "rank": [0, 0, 0], "metadata_complete": False, "seeders": 10, "created": "2026"}
        records = {"sub": {"SSNI-618": {"code": "SSNI-618", "mteam_metadata": cached}}}
        detail_data = {"performer": "Actress", "labelsNew": ["中字"], "hasChineseSubtitle": False, "resolution": "4K", "videoCodec": "x265"}
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value=detail_data) as detail, patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, FakeDownloader(), [])
        detail.assert_called_once_with("1", "token")
        self.assertEqual(records["sub"]["SSNI-618"]["mteam_metadata"]["rank"], [2, 2, 2])
        self.assertEqual(records["sub"]["SSNI-618"]["mteam_metadata"]["mteam_rank_version"], app.MTEAM_RANK_VERSION)

    def test_forced_refresh_fetches_stored_video_even_when_search_does_not_return_it(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        cached = {"id": "1289514", "title": "SSIS-181", "rank": [1, 1, 1], "resolution": "1080p", "video_codec": "x264", "metadata_complete": True}
        records = {"sub": {"SSIS-181": {"code": "SSIS-181", "mteam_metadata": cached}}}
        detail_data = {"labelsNew": [{"name": "中字", "color": "red"}], "resolution": "2160p", "videoCodec": "H.265(x265/HEVC)"}
        with patch.object(app, "mteam_search", return_value=[]), patch.object(app, "mteam_detail", return_value=detail_data) as detail, patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, FakeDownloader(), [], refresh_existing_info=True)
        detail.assert_called_once_with("1289514", "token")
        self.assertEqual(records["sub"]["SSIS-181"]["mteam_metadata"]["rank"], [2, 2, 2])

    def test_unchecked_scan_preserves_saved_existing_video_metadata(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        existing_metadata = {"id": "old", "title": "SSIS-181 内嵌中文 4K x265", "rank": [2, 2, 2], "resolution": "4K", "video_codec": "x265", "metadata_complete": True}
        candidate = {"id": "same", "code": "SSIS-181", "title": "SSIS-181 中文字幕 4K x265", "rank": [2, 1, 2], "seeders": 8, "created": "2026"}
        records = {"sub": {"SSIS-181": {"code": "SSIS-181", "current": {"id": "old", "rank": [2, 2, 2], "hash": "oldhash"}, "mteam_metadata": existing_metadata}}}
        with patch.object(app, "mteam_search", return_value=[candidate]) as search, patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, FakeDownloader(), [])
        search.assert_called_once_with("Actress", "token")
        self.assertIs(records["sub"]["SSIS-181"]["mteam_metadata"], existing_metadata)

    def test_blacklisted_number_is_not_submitted_or_upgraded(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "blacklist": ["SSNI-618"], "download_dir": "/video", "wash_enabled": True}
        candidate = {"id": "1", "code": "SSNI-618", "title": "SSNI-618 4K x265", "rank": [2, 0, 2], "seeders": 10, "created": "2026"}
        downloader = FakeDownloader()
        records = {}
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        self.assertEqual(downloader.added, [])
        self.assertEqual(records["sub"], {})

    def test_manual_poll_persists_number_and_downloader_state(self):
        candidate = {"id": "1", "code": "SSNI-618", "title": "SSNI-618 1080p x265", "rank": [1, 0, 2], "seeders": 10, "created": "2026"}
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080"}, "poll_interval_minutes": 60, "auto_scan": True})
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "sub", "performer": "三上悠亚", "keywords": "ltx", "download_dir": "/video", "wash_enabled": True}])
        app.write_json(app.RECORDS_FILE, {"sub": {"SSNI-618": {"code": "SSNI-618", "last_error": "參數錯誤", "retry_after": time.time() + 3600}}})
        with patch.object(app, "Downloader", return_value=FakeDownloader()), patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            status = app.run_poll("manual")
        saved = app.read_json(app.RECORDS_FILE, {})
        self.assertEqual(status["state"], "completed")
        self.assertEqual(saved["sub"]["SSNI-618"]["current"]["state"], "downloading")
        self.assertNotIn("retry_after", saved["sub"]["SSNI-618"])
        events = [item["event"] for item in app.get_logs()]
        for event in ("downloader_sync_started", "downloader_sync_completed", "local_scan_started", "local_scan_completed", "mteam_search_started", "mteam_search_completed"):
            self.assertIn(event, events)
        separators = [item["message"] for item in app.get_logs() if item["event"] == "run_separator"]
        self.assertEqual(separators, ["本轮检查开始 · 手动检查", "本轮检查结束 · 手动检查"])
        round_entries = [item for item in app.get_logs() if item.get("round_id")]
        self.assertTrue(round_entries)
        self.assertEqual(len({item["round_id"] for item in round_entries}), 1)
        self.assertTrue(all(item["subscription"] == "三上悠亚" for item in round_entries if item["event"] == "run_separator"))

    def test_missing_downloader_task_is_resubmitted_for_same_mteam_release(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        candidate = {"id": "1287499", "code": "SSIS-419", "title": "SSIS-419 初体験は三上悠亜", "rank": [1, 0, 1], "seeders": 10, "created": "2026", "metadata_complete": True}
        records = {"sub": {"SSIS-419": {"code": "SSIS-419", "current": {
            "id": "1287499", "code": "SSIS-419", "title": candidate["title"], "rank": [1, 0, 1],
            "hash": "removed-task", "state": "downloading", "progress": 25,
        }}}}
        downloader = FakeDownloader("replacement-task")
        downloader.complete = False
        results = []

        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            app.poll_downloader_records(downloader, records["sub"], results)
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)

        self.assertEqual(len(downloader.added), 1)
        self.assertEqual(records["sub"]["SSIS-419"]["current"]["hash"], "replacement-task")
        self.assertEqual(records["sub"]["SSIS-419"]["current"]["state"], "downloading")
        self.assertFalse(records["sub"]["SSIS-419"].get("retry_after"))

    def test_qbittorrent_missing_files_task_is_rechecked_instead_of_removed(self):
        class MissingFilesDownloader(FakeDownloader):
            def __init__(self):
                super().__init__()
                self.rechecked = []

            def list_tasks(self, hashes=None):
                return {"existinghash": {"progress": 1, "complete": True, "failed": True, "state": "missingFiles"}}

            def recheck(self, info_hash, download_dir=""):
                self.rechecked.append(info_hash)

        downloader = MissingFilesDownloader()
        records = {"SSIS-419": {"code": "SSIS-419", "current": {
            "hash": "existinghash", "state": "downloading", "title": "SSIS-419", "progress": 100,
        }}}
        results = []
        app.poll_downloader_records(downloader, records, results)
        self.assertEqual(downloader.rechecked, ["existinghash"])
        self.assertEqual(downloader.removed, [])
        self.assertEqual(records["SSIS-419"]["current"]["state"], "downloading")
        self.assertIn("补下载缺失数据", records["SSIS-419"]["current"]["message"])

    def test_completed_owned_task_missing_local_video_is_rechecked(self):
        class ExistingCompletedDownloader(FakeDownloader):
            def __init__(self):
                super().__init__()
                self.rechecked = []

            def list_tasks(self, hashes=None):
                return {"old-task": {"progress": 1, "complete": True, "state": "uploading"}}

            def recheck(self, info_hash, download_dir=""):
                self.rechecked.append((info_hash, download_dir))
                return "/mapped/subscription/path"

        root = Path(self.directory.name) / "empty-video-dir"
        root.mkdir()
        downloader = ExistingCompletedDownloader()
        records = {"SSIS-419": {"code": "SSIS-419", "current": {
            "hash": "old-task", "id": "1287499", "state": "completed", "owns_task": True,
            "title": "SSIS-419 Actress", "progress": 100, "download_dir": str(root),
        }}}
        results = []

        app.poll_downloader_records(downloader, records, results)

        self.assertEqual(downloader.rechecked, [("old-task", str(root))])
        self.assertEqual(records["SSIS-419"]["current"]["state"], "downloading")
        self.assertTrue(records["SSIS-419"]["current"]["completed_missing_recheck_requested"])
        self.assertEqual(results[-1]["state"], "downloading")

    def test_completed_owned_task_missing_local_video_is_rechecked_only_once(self):
        class ExistingCompletedDownloader(FakeDownloader):
            def __init__(self):
                super().__init__()
                self.rechecked = []

            def list_tasks(self, hashes=None):
                return {"old-task": {"progress": 1, "complete": True, "state": "uploading"}}

            def recheck(self, info_hash, download_dir=""):
                self.rechecked.append(info_hash)

        root = Path(self.directory.name) / "empty-video-dir-once"
        root.mkdir()
        downloader = ExistingCompletedDownloader()
        records = {"SSIS-419": {"code": "SSIS-419", "current": {
            "hash": "old-task", "id": "1287499", "state": "completed", "owns_task": True,
            "title": "SSIS-419 Actress", "progress": 100, "download_dir": str(root),
            "completed_missing_recheck_requested": True,
        }}}

        app.poll_downloader_records(downloader, records, [])

        self.assertEqual(downloader.rechecked, [])
        self.assertEqual(records["SSIS-419"]["current"]["state"], "downloading")

    def test_completed_record_without_local_video_is_resubmitted(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        candidate = {"id": "1287499", "code": "SSIS-419", "title": "SSIS-419 Actress", "rank": [1, 0, 1], "seeders": 10, "created": "2026", "metadata_complete": True}
        records = {"sub": {"SSIS-419": {"code": "SSIS-419", "current": {
            "id": "1287499", "code": "SSIS-419", "title": candidate["title"], "rank": [1, 0, 1],
            "hash": "old-task", "state": "completed", "progress": 100, "download_dir": str(root),
        }}}}
        downloader = FakeDownloader("replacement-task")
        results = []
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "Actress"}), patch.object(app, "fetch_torrent", return_value=torrent_bytes()):
            app.poll_downloader_records(downloader, records["sub"], results)
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)
        self.assertEqual(len(downloader.added), 1)
        self.assertEqual(records["sub"]["SSIS-419"]["current"]["hash"], "replacement-task")
        self.assertEqual(records["sub"]["SSIS-419"]["current"]["state"], "downloading")
        self.assertTrue(any(item["state"] == "missing" for item in results))

    def test_debug_mode_logs_per_number_skip_reason(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        candidate = {"id": "1287499", "code": "SSIS-419", "title": "SSIS-419 Actress", "rank": [1, 0, 1], "seeders": 10, "created": "2026", "metadata_complete": True}
        records = {"sub": {"SSIS-419": {"code": "SSIS-419", "current": {"id": "1287499", "hash": "old-task", "state": "downloading"}}}}
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token", "debug_mode": True}, FakeDownloader(), [])
        messages = [entry["message"] for entry in app.get_logs() if entry["event"] == "debug_decision"]
        self.assertTrue(any("与当前记录相同" in message for message in messages))

    def test_manual_poll_can_check_only_the_requested_subscription(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080"}})
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "one", "performer": "One", "keywords": "", "download_dir": "/video/one", "wash_enabled": True},
            {"id": "two", "performer": "Two", "keywords": "", "download_dir": "/video/two", "wash_enabled": True},
        ])
        with patch.object(app, "Downloader", return_value=FakeDownloader()), patch.object(app, "mteam_search", return_value=[]) as search, patch.object(app, "scan_local_releases", return_value={}):
            result = app.run_poll("manual", "two")
        self.assertEqual(result["state"], "completed")
        search.assert_called_once_with("Two", "token")

    def test_rate_limit_waits_with_countdown_then_retries_poll(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080"}})
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "one", "performer": "One", "keywords": "", "download_dir": "/video/one", "wash_enabled": True},
            {"id": "two", "performer": "Two", "keywords": "", "download_dir": "/video/two", "wash_enabled": True},
        ])
        clock = [1000.0]
        countdown_states = []
        def fake_sleep(seconds):
            countdown_states.append(app.get_status())
            clock[0] += seconds
        with patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=fake_sleep), patch.object(app, "Downloader", return_value=FakeDownloader()), patch.object(app, "mteam_search", side_effect=[[], app.MTeamRateLimitError("限流", 60), []]) as search, patch.object(app, "scan_local_releases", return_value={}):
            status = app.run_poll("manual")
        self.assertEqual(status["state"], "completed")
        self.assertEqual(search.call_count, 3)
        self.assertEqual([call.args[0] for call in search.call_args_list], ["One", "Two", "Two"])
        self.assertTrue(countdown_states)
        waiting_states = [item for item in countdown_states if item["state"] == "waiting_retry"]
        self.assertTrue(waiting_states)
        self.assertGreater(waiting_states[0]["retry_in"], 0)
        self.assertFalse(app.MTEAM_LIMIT_FILE.exists())
        self.assertTrue(any(item["event"] == "retry_resuming" for item in app.get_logs()))

    def test_poll_preserves_progress_across_repeated_rate_limits(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080"}})
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "one", "performer": "One", "keywords": "", "download_dir": "/video/one", "wash_enabled": True},
            {"id": "two", "performer": "Two", "keywords": "", "download_dir": "/video/two", "wash_enabled": True},
        ])
        clock = [1000.0]
        observed_limit_counts = set()

        def fake_sleep(seconds):
            observed_limit_counts.add(app.read_json(app.MTEAM_LIMIT_FILE, {}).get("consecutive_rate_limits"))
            clock[0] += seconds

        limited = app.MTeamRateLimitError("限流", 60)
        with patch.object(app, "MTEAM_DEFAULT_COOLDOWN", 60), patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=fake_sleep), patch.object(app, "Downloader", return_value=FakeDownloader()), patch.object(app, "mteam_search", side_effect=[[], limited, limited, []]) as search, patch.object(app, "scan_local_releases", return_value={}):
            status = app.run_poll("manual")

        self.assertEqual(status["state"], "completed")
        self.assertEqual([call.args[0] for call in search.call_args_list], ["One", "Two", "Two", "Two"])
        self.assertEqual(observed_limit_counts, {1, 2})
        self.assertEqual(len([item for item in app.get_logs() if item["event"] == "rate_limited"]), 2)
        self.assertFalse(app.MTEAM_LIMIT_FILE.exists())
        separators = [item for item in app.get_logs() if item["event"] == "run_separator"]
        self.assertEqual(len(separators), 4)
        self.assertEqual({item["subscription"] for item in separators}, {"One", "Two"})

    def test_mteam_search_retries_the_limited_request_in_place(self):
        clock = [1000.0]
        waiting_states = []
        limited = json.dumps({"code": 429, "message": "請求過於頻繁"}).encode()
        success = json.dumps({"code": 0, "data": []}).encode()
        app.write_json(app.STATUS_FILE, {"state": "running", "results": []})

        def fake_sleep(seconds):
            waiting_states.append(app.get_status())
            clock[0] += seconds

        app.MTEAM_POLL_CACHE.active = {"details": {}, "options": {}}
        app.MTEAM_POLL_CACHE.performer = "Actress"
        try:
            with patch.object(app, "MTEAM_DEFAULT_COOLDOWN", 60), patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=fake_sleep), patch.object(app, "mteam_request", side_effect=[(limited, {}), (success, {})]) as request:
                self.assertEqual(app.mteam_search("Actress ltx", "token"), [])
        finally:
            app.MTEAM_POLL_CACHE.active = None
            app.MTEAM_POLL_CACHE.performer = ""

        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])
        self.assertTrue(waiting_states)
        self.assertTrue(all(item["state"] == "waiting_retry" for item in waiting_states))
        self.assertFalse(app.MTEAM_LIMIT_FILE.exists())
        events = [item["event"] for item in app.get_logs()]
        self.assertIn("retry_waiting", events)
        self.assertIn("retry_resuming", events)

    def test_scheduler_stop_during_rate_wait_does_not_start_next_subscription(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080"}})
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "first", "performer": "桃乃木香奈", "keywords": "", "download_dir": "/video/first", "wash_enabled": True},
            {"id": "second", "performer": "香水纯", "keywords": "", "download_dir": "/video/second", "wash_enabled": True},
        ])
        clock = [1000.0]
        limited = json.dumps({"code": 429, "message": "請求過於頻繁"}).encode()

        def stop_scheduler(seconds):
            clock[0] += seconds
            app.STOP_SCHEDULER = True

        app.STOP_SCHEDULER = False
        try:
            with patch.object(app, "MTEAM_DEFAULT_COOLDOWN", 60), patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=stop_scheduler), patch.object(app, "Downloader", return_value=FakeDownloader()), patch.object(app, "mteam_request", return_value=(limited, {})) as request, patch.object(app, "scan_local_releases", return_value={}):
                status = app.run_poll("scheduled")
        finally:
            app.STOP_SCHEDULER = False

        self.assertEqual(status["state"], "waiting_retry")
        self.assertEqual(request.call_count, 1)
        started = [item["subscription"] for item in app.get_logs() if item["event"] == "subscription_started"]
        self.assertEqual(started, ["桃乃木香奈"])

    def test_poll_removes_stale_episode_number_failures_without_touching_downloads(self):
        app.write_json(app.SETTINGS_FILE, {"mteam_token": "token", "downloader": {"type": "qbittorrent", "url": "http://nas:8080"}, "poll_interval_minutes": 60, "auto_scan": True})
        app.write_json(app.SUBSCRIPTIONS_FILE, [{"id": "sub", "performer": "三上悠亚", "keywords": "", "download_dir": "/video", "wash_enabled": True}])
        app.write_json(app.RECORDS_FILE, {"sub": {
            "S01E41-2160": {"code": "S01E41-2160", "last_error": "參數錯誤"},
            "S03E11-1080": {"code": "S03E11-1080", "last_error": "參數錯誤"},
            "IPX-811": {"code": "IPX-811", "current": {"hash": "owned", "state": "downloading"}},
        }})
        with patch.object(app, "Downloader", return_value=FakeDownloader()), patch.object(app, "mteam_search", return_value=[]), patch.object(app, "scan_local_releases", return_value={}):
            app.run_poll("manual")
        saved = app.read_json(app.RECORDS_FILE, {})["sub"]
        self.assertEqual(set(saved), {"IPX-811"})

    def test_higher_release_is_added_before_old_data_is_removed(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        old = {"id": "old", "code": "IPX-811", "title": "IPX-811 1080p x264", "rank": [1, 0, 1], "hash": "oldhash", "state": "completed", "progress": 100}
        candidate = {"id": "new", "code": "IPX-811", "title": "IPX-811 4K 中文 x265", "rank": [2, 1, 2], "seeders": 8, "created": "2026"}
        records = {"sub": {"IPX-811": {"code": "IPX-811", "current": old}}}
        downloader = FakeDownloader("newhash")
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            results = []
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)
        self.assertEqual(records["sub"]["IPX-811"]["pending"]["state"], "upgrading")
        self.assertIn("分辨率：1080p → 4K", records["sub"]["IPX-811"]["pending"]["message"])
        self.assertIn("中文等级：非中文 → 中文字幕", records["sub"]["IPX-811"]["pending"]["message"])
        self.assertEqual(results[-1]["message"], records["sub"]["IPX-811"]["pending"]["message"])
        self.assertEqual(downloader.removed, [])
        downloader.complete = True
        app.poll_downloader_records(downloader, records["sub"], [])
        self.assertEqual(downloader.removed, [("oldhash", True)])
        self.assertEqual(records["sub"]["IPX-811"]["current"]["hash"], "newhash")
        self.assertNotIn("pending", records["sub"]["IPX-811"])

    def test_same_pending_mteam_release_is_not_resubmitted_when_rank_improves(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        pending = {"id": "same-id", "code": "IPX-811", "title": "IPX-811 4K 中文 x265", "rank": [1, 0, 1], "hash": "same-hash", "state": "upgrading", "progress": 45}
        current = {"id": "old-id", "code": "IPX-811", "title": "IPX-811 1080p x264", "rank": [1, 0, 1], "hash": "old-hash", "state": "completed", "progress": 100}
        records = {"sub": {"IPX-811": {"code": "IPX-811", "current": current, "pending": pending}}}
        candidate = {"id": "same-id", "code": "IPX-811", "title": "IPX-811 4K 内嵌中文 x265", "rank": [2, 2, 2], "seeders": 10, "created": "2026"}
        downloader = FakeDownloader()
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent") as fetch, patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        fetch.assert_not_called()
        self.assertEqual(downloader.added, [])
        self.assertEqual(downloader.removed, [])
        self.assertEqual(records["sub"]["IPX-811"]["pending"]["hash"], "same-hash")
        self.assertEqual(records["sub"]["IPX-811"]["pending"]["rank"], [2, 2, 2])

    def test_upgrade_reason_describes_each_rank_dimension(self):
        self.assertEqual(app.upgrade_reason([1, 0, 1], [2, 2, 2]), "分辨率：1080p → 4K；中文等级：非中文 → 内嵌中文")

    def test_codec_only_improvement_does_not_trigger_wash(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        current = {"id": "old", "code": "IPX-811", "title": "IPX-811 1080p x264", "rank": [1, 1, 1], "hash": "old-hash", "state": "completed", "progress": 100}
        candidate = {"id": "new", "code": "IPX-811", "title": "IPX-811 1080p x265", "rank": [1, 1, 2], "seeders": 8, "created": "2026"}
        records = {"sub": {"IPX-811": {"code": "IPX-811", "current": current}}}
        downloader = FakeDownloader("new-hash")
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent") as fetch, patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        fetch.assert_not_called()
        self.assertEqual(downloader.added, [])
        self.assertNotIn("pending", records["sub"]["IPX-811"])
        self.assertEqual(records["sub"]["IPX-811"]["current"]["hash"], "old-hash")

    def test_existing_local_number_skips_duplicate_and_is_removed_after_upgrade(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        old_path = root / "IPX-811 1080p x264.mkv"
        old_path.write_bytes(b"old video")
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        candidate = {"id": "new", "code": "IPX-811", "title": "IPX-811 4K 中文 x265", "rank": [2, 1, 2], "seeders": 8, "created": "2026"}
        records = {}
        downloader = FakeDownloader("newhash")
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        self.assertTrue(old_path.exists())
        self.assertIn("pending", records["sub"]["IPX-811"])
        downloader.complete = True
        app.poll_downloader_records(downloader, records["sub"], [])
        self.assertFalse(old_path.exists())
        self.assertEqual(records["sub"]["IPX-811"]["current"]["hash"], "newhash")

    def test_existing_local_number_is_not_downloaded_twice(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        (root / "IPX-811 4K 中文 x265.mkv").write_bytes(b"already downloaded")
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        candidate = {"id": "new", "code": "IPX-811", "title": "IPX-811 1080p x264", "rank": [1, 0, 1], "seeders": 8, "created": "2026"}
        records = {}
        downloader = FakeDownloader()
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", return_value=torrent_bytes()):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        self.assertEqual(downloader.added, [])
        self.assertEqual(records["sub"]["IPX-811"]["current"]["state"], "completed")

    def test_local_duplicate_wash_recurses_within_subscription_directory(self):
        root = Path(self.directory.name) / "三上悠亚"
        nested = root / "SSNI-229 4K 内嵌中文 x265"
        nested.mkdir(parents=True)
        better = nested / "SSNI-229-U.ltx.mp4"
        lower = root / "SSNI-229 1080p 中文字幕 x264-U.ltx.mp4"
        better.write_bytes(b"better")
        lower.write_bytes(b"lower")
        subscription = {"id": "sub", "performer": "三上悠亚", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        with patch.object(app, "mteam_search", return_value=[]):
            app.process_subscription(subscription, {}, {"mteam_token": "token", "local_multi_disk_wash": True}, FakeDownloader(), [])
        self.assertTrue(better.exists())
        self.assertFalse(lower.exists())
        local_wash = next(item for item in app.get_logs() if item["event"] == "local_wash_completed")
        self.assertIn("分辨率：1080p → 4K", local_wash["message"])
        self.assertEqual(local_wash["subscription"], "三上悠亚")

    def test_local_duplicate_wash_removes_matching_downloader_task_and_sidecars(self):
        root = Path(self.directory.name) / "三上悠亚"
        root.mkdir()
        better = root / "SSNI-229 4K 内嵌中文 x265.mp4"
        lower = root / "SSNI-229 1080p 中文字幕 x264.mp4"
        better.write_bytes(b"better")
        lower.write_bytes(b"lower")
        nfo = lower.with_suffix(".nfo")
        subtitle = lower.with_name(lower.stem + ".zh.srt")
        nfo.write_text("metadata", encoding="utf-8")
        subtitle.write_text("subtitle", encoding="utf-8")

        class MatchingDownloader(FakeDownloader):
            def list_tasks(self, hashes=None):
                return {"oldhash": {"complete": True, "content_path": str(lower)}}

        records = {"sub": {"SSNI-229": {"current": {"hash": "oldhash"}}}}
        downloader = MatchingDownloader()
        releases = app.scan_local_releases(str(root))
        remaining = app.wash_local_duplicate_releases(str(root), releases, "三上悠亚", downloader, downloader.list_tasks(), records)

        self.assertEqual(remaining["SSNI-229"], [next(item for item in releases["SSNI-229"] if item["path"] == str(better))])
        self.assertTrue(better.exists())
        self.assertFalse(lower.exists())
        self.assertFalse(nfo.exists())
        self.assertFalse(subtitle.exists())
        self.assertEqual(downloader.removed, [("oldhash", False)])
        self.assertNotIn("current", records["sub"]["SSNI-229"])

    def test_local_duplicate_wash_keeps_files_when_downloader_task_is_shared(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        better = root / "SSNI-229 4K 内嵌中文 x265.mp4"
        lower = root / "SSNI-229 1080p 中文字幕 x264.mp4"
        better.write_bytes(b"better")
        lower.write_bytes(b"lower")

        class MatchingDownloader(FakeDownloader):
            def list_tasks(self, hashes=None):
                return {"sharedhash": {"complete": True, "content_path": str(lower)}}

        records = {
            "one": {"SSNI-229": {"current": {"hash": "sharedhash"}}},
            "two": {"SSNI-229": {"current": {"hash": "sharedhash"}}},
        }
        downloader = MatchingDownloader()
        releases = app.scan_local_releases(str(root))
        remaining = app.wash_local_duplicate_releases(str(root), releases, "Actress", downloader, downloader.list_tasks(), records)

        self.assertTrue(lower.exists())
        self.assertEqual(len(remaining["SSNI-229"]), 2)
        self.assertEqual(downloader.removed, [])
        failure = next(item for item in app.get_logs() if item["event"] == "local_wash_failed")
        self.assertIn("多个订阅记录共用", failure["message"])

    def test_local_duplicate_wash_keeps_files_when_downloader_removal_fails(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        better = root / "SSNI-229 4K 内嵌中文 x265.mp4"
        lower = root / "SSNI-229 1080p 中文字幕 x264.mp4"
        better.write_bytes(b"better")
        lower.write_bytes(b"lower")

        class FailingDownloader(FakeDownloader):
            def list_tasks(self, hashes=None):
                return {"oldhash": {"complete": True, "content_path": str(lower)}}

            def remove(self, info_hash, delete_data=True):
                raise app.AppError("downloader unavailable")

        downloader = FailingDownloader()
        releases = app.scan_local_releases(str(root))
        remaining = app.wash_local_duplicate_releases(str(root), releases, "Actress", downloader, downloader.list_tasks(), {})

        self.assertTrue(lower.exists())
        self.assertEqual(len(remaining["SSNI-229"]), 2)

    def test_local_duplicate_wash_prefers_x265_over_x264(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        first = root / "SSNI-229 1080p 中文字幕 x264-A.mp4"
        second = root / "SSNI-229 1080p 中文字幕 x265-B.mp4"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        with patch.object(app, "mteam_search", return_value=[]):
            app.process_subscription(subscription, {}, {"mteam_token": "token", "local_multi_disk_wash": False}, FakeDownloader(), [])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        with patch.object(app, "mteam_search", return_value=[]):
            app.process_subscription(subscription, {}, {"mteam_token": "token", "local_multi_disk_wash": True}, FakeDownloader(), [])
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_local_duplicate_wash_removes_one_file_when_all_rank_dimensions_match(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        first = root / "SSNI-229 1080p 中文字幕 x265-A.mp4"
        second = root / "SSNI-229 1080p 中文字幕 x265-B.mp4"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        sidecar = first.with_suffix(".srt")
        sidecar.write_text("subtitle", encoding="utf-8")
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        with patch.object(app, "mteam_search", return_value=[]):
            app.process_subscription(subscription, {}, {"mteam_token": "token", "local_multi_disk_wash": True}, FakeDownloader(), [])
        self.assertFalse(first.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(second.exists())
        log = next(item for item in app.get_logs() if item["event"] == "local_wash_completed")
        self.assertIn("版本等级相同", log["message"])

    def test_other_subscription_local_number_is_hardlinked_without_downloading(self):
        root = Path(self.directory.name)
        source_dir = root / "source-subscription"
        target_dir = root / "target-subscription"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "IPX-811 1080p 中文字幕 x264.mkv"
        subtitle = source.with_suffix(".srt")
        nfo = source.with_suffix(".nfo")
        source.write_bytes(b"video bytes")
        subtitle.write_text("subtitle", encoding="utf-8")
        nfo.write_text("<movie><title>IPX-811</title><fileinfo><streamdetails><video><width>1920</width><height>1080</height><codec>x264</codec></video></streamdetails></fileinfo></movie>", encoding="utf-8")
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "source", "performer": "Co-star A", "download_dir": str(source_dir)},
            {"id": "target", "performer": "Co-star B", "download_dir": str(target_dir)},
        ])
        app.index_subscription_videos("source", app.scan_local_releases(str(source_dir)), {})
        candidate = {"id": "1289514", "code": "IPX-811", "title": "IPX-811 4K 内嵌中文 x265", "rank": [2, 2, 2], "seeders": 20, "created": "2026", "metadata_complete": True}
        downloader = FakeDownloader()
        records = {}
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "Co-star B"}), patch.object(app, "fetch_torrent") as fetch:
            app.process_subscription(app.list_subscriptions()[1], records, {"mteam_token": "token"}, downloader, [])
        fetch.assert_not_called()
        self.assertEqual(downloader.added, [])
        target_video = target_dir / source.name
        self.assertTrue(target_video.exists())
        self.assertEqual(source.stat().st_ino, target_video.stat().st_ino)
        self.assertEqual((target_dir / subtitle.name).stat().st_ino, subtitle.stat().st_ino)
        self.assertEqual((target_dir / nfo.name).stat().st_ino, nfo.stat().st_ino)
        target_record = records["target"]["IPX-811"]
        self.assertTrue(target_record["shared_local"])
        self.assertTrue(target_record["current"]["local_existing"])
        self.assertEqual(target_record["current"]["local_paths"], [str(target_video)])
        self.assertEqual(target_record["current"]["rank"], [1, 1, 1])
        app.remove_local_releases([str(target_video)], str(target_dir), "IPX-811")
        self.assertTrue(source.exists())
        self.assertTrue(subtitle.exists())
        self.assertTrue(nfo.exists())
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(database.execute("SELECT subscription_id, code FROM local_videos ORDER BY subscription_id").fetchall(), [("source", "IPX-811"), ("target", "IPX-811")])

    def test_other_subscription_indexed_number_with_missing_file_is_not_reused(self):
        root = Path(self.directory.name)
        source_dir = root / "source"
        target_dir = root / "target"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "IPX-811 1080p x264.mkv"
        source.write_bytes(b"gone before reuse")
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "source", "performer": "Co-star A", "download_dir": str(source_dir)},
            {"id": "target", "performer": "Co-star B", "download_dir": str(target_dir)},
        ])
        app.index_subscription_videos("source", app.scan_local_releases(str(source_dir)), {})
        source.unlink()
        candidate = {"id": "1289514", "code": "IPX-811", "title": "IPX-811 1080p x264", "rank": [1, 0, 1], "seeders": 20, "created": "2026", "metadata_complete": True}
        downloader = FakeDownloader()
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "Co-star B"}), patch.object(app, "fetch_torrent", return_value=torrent_bytes()):
            app.process_subscription(app.list_subscriptions()[1], {}, {"mteam_token": "token"}, downloader, [])
        self.assertEqual(len(downloader.added), 1)
        self.assertFalse((target_dir / source.name).exists())
        with closing(sqlite3.connect(app.LOCAL_VIDEO_DATABASE)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM local_video_files WHERE subscription_id = 'source'").fetchone()[0], 0)

    def test_other_subscription_reuse_still_validates_mteam_performer(self):
        root = Path(self.directory.name)
        source_dir = root / "source"
        target_dir = root / "target"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "IPX-811 1080p x264.mkv"
        source.write_bytes(b"video")
        app.write_json(app.SUBSCRIPTIONS_FILE, [
            {"id": "source", "performer": "Co-star A", "download_dir": str(source_dir)},
            {"id": "target", "performer": "Co-star B", "download_dir": str(target_dir)},
        ])
        app.index_subscription_videos("source", app.scan_local_releases(str(source_dir)), {})
        candidate = {"id": "1289514", "code": "IPX-811", "title": "IPX-811 1080p x264", "rank": [1, 0, 1], "seeders": 20, "created": "2026", "metadata_complete": True}
        downloader = FakeDownloader()
        results = []
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "Unrelated Actress"}), patch.object(app, "fetch_torrent") as fetch:
            app.process_subscription(app.list_subscriptions()[1], {}, {"mteam_token": "token"}, downloader, results)
        fetch.assert_not_called()
        self.assertEqual(downloader.added, [])
        self.assertFalse((target_dir / source.name).exists())
        self.assertEqual(results[0]["state"], "skipped")

    def test_other_subscription_reuse_refuses_cross_filesystem_copy(self):
        root = Path(self.directory.name)
        source_dir = root / "source"
        target_dir = root / "target"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "IPX-811 1080p x264.mkv"
        source.write_bytes(b"video")
        with patch.object(app.os, "link", side_effect=OSError(app.errno.EXDEV, "cross-device link")):
            with self.assertRaisesRegex(app.AppError, "为避免整文件复制"):
                app.link_local_release_to_subscription(str(source), str(target_dir), "IPX-811")
        target = target_dir / source.name
        self.assertFalse(target.exists())
        self.assertTrue(source.exists())

    def test_failed_upgrade_submission_keeps_existing_release(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        old = {"id": "old", "code": "IPX-811", "title": "IPX-811 1080p x264", "rank": [1, 0, 1], "hash": "oldhash", "state": "completed"}
        candidate = {"id": "new", "code": "IPX-811", "title": "IPX-811 4K 中文 x265", "rank": [2, 1, 2], "seeders": 8, "created": "2026"}
        records = {"sub": {"IPX-811": {"code": "IPX-811", "current": old}}}
        downloader = FakeDownloader()
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "fetch_torrent", side_effect=app.AppError("temporary failure")), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        self.assertIs(records["sub"]["IPX-811"]["current"], old)
        self.assertNotIn("pending", records["sub"]["IPX-811"])
        self.assertEqual(downloader.removed, [])

    def test_unknown_existing_rank_never_triggers_automatic_replacement(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        old = {"id": "old", "code": "SSIS-181", "title": "SSIS-181", "rank": [0, 0, 0], "hash": "oldhash", "state": "completed"}
        candidate = {"id": "new", "code": "SSIS-181", "title": "SSIS-181 外挂中字", "rank": [2, 1, 2], "seeders": 8, "created": "2026"}
        records = {"sub": {"SSIS-181": {"code": "SSIS-181", "current": old}}}
        downloader = FakeDownloader()
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "scan_local_releases", return_value={}):
            results = []
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)
        self.assertEqual(downloader.added, [])
        self.assertNotIn("pending", records["sub"]["SSIS-181"])
        self.assertIn("等级未知", results[0]["message"])

    def test_unknown_existing_rank_is_fetched_from_stored_mteam_id_before_washing(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        old = {"id": "1289514", "code": "SSIS-181", "title": "SSIS-181", "rank": [0, 0, 0], "hash": "oldhash", "state": "completed"}
        cached = {"id": "1289514", "title": "SSIS-181", "rank": [0, 0, 0], "metadata_complete": False}
        candidate = {"id": "1287499", "code": "SSIS-181", "title": "SSIS-181 1080p 中文字幕", "rank": [1, 1, 1], "seeders": 8, "created": "2026", "metadata_complete": True}
        records = {"sub": {"SSIS-181": {"code": "SSIS-181", "current": old, "mteam_metadata": cached}}}
        detail_data = {"performer": "Actress", "labelsNew": [{"name": "中字", "color": "red"}], "resolution": "2160p", "videoCodec": "H.265(x265/HEVC)"}
        downloader = FakeDownloader()

        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value=detail_data) as detail, patch.object(app, "fetch_torrent") as fetch, patch.object(app, "scan_local_releases", return_value={}):
            results = []
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)

        detail.assert_called_once_with("1289514", "token")
        fetch.assert_not_called()
        self.assertEqual(downloader.added, [])
        self.assertEqual(old["rank"], [2, 2, 2])
        self.assertEqual(records["sub"]["SSIS-181"]["mteam_metadata"]["rank"], [2, 2, 2])
        self.assertFalse(any("等级未知" in result.get("message", "") for result in results))

    def test_unknown_local_release_is_removed_after_known_upgrade_completes(self):
        root = Path(self.directory.name) / "videos"
        root.mkdir()
        old_path = root / "SSIS-419.mkv"
        old_path.write_bytes(b"unknown old video")
        old_nfo = old_path.with_suffix(".nfo")
        old_subtitle = root / "SSIS-419.zh.srt"
        old_nfo.write_text("unknown metadata", encoding="utf-8")
        old_subtitle.write_text("subtitle", encoding="utf-8")
        subscription = {"id": "sub", "performer": "三上悠亚", "keywords": "", "download_dir": str(root), "wash_enabled": True}
        old = {"id": "", "code": "SSIS-419", "title": old_path.name, "rank": [0, 0, 0], "hash": "", "state": "completed", "local_existing": True, "local_paths": [str(old_path)], "local_roots": [str(root)], "download_dir": str(root)}
        records = {"sub": {"SSIS-419": {"code": "SSIS-419", "current": old}}}
        candidate = {"id": "1287499", "code": "SSIS-419", "title": "SSIS-419 1080p 中文字幕", "rank": [1, 1, 1], "seeders": 8, "created": "2026", "metadata_complete": True}
        downloader = FakeDownloader("betterhash")
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "三上悠亚"}), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={"SSIS-419": [{"path": str(old_path), "title": old_path.name, "rank": [0, 0, 0], "metadata": {}}]}):
            results = []
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)
            self.assertIn("pending", records["sub"]["SSIS-419"])
            self.assertTrue(old_path.exists())
            downloader.complete = True
            app.poll_downloader_records(downloader, records["sub"], [])

        self.assertFalse(old_path.exists())
        self.assertFalse(old_nfo.exists())
        self.assertFalse(old_subtitle.exists())
        self.assertEqual(records["sub"]["SSIS-419"]["current"]["hash"], "betterhash")

    def test_local_higher_rank_prevents_washing_to_lower_candidate(self):
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": "/video", "wash_enabled": True}
        old = {"id": "old", "code": "SSIS-181", "title": "SSIS-181", "rank": [0, 0, 0], "hash": "oldhash", "state": "completed"}
        candidate = {"id": "new", "code": "SSIS-181", "title": "SSIS-181 外挂中字", "rank": [2, 1, 2], "seeders": 8, "created": "2026"}
        records = {"sub": {"SSIS-181": {"code": "SSIS-181", "current": old}}}
        local = {"SSIS-181": [{"path": "/video/SSIS-181.mkv", "title": "SSIS-181 内嵌中字", "rank": [2, 2, 2], "metadata": {}}]}
        downloader = FakeDownloader()
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "scan_local_releases", return_value=local):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, [])
        self.assertEqual(downloader.added, [])
        self.assertEqual(old["rank"], [2, 2, 2])
        self.assertNotIn("pending", records["sub"]["SSIS-181"])

    def test_shared_old_task_is_not_removed_until_other_subscription_replaces_it(self):
        old = {"hash": "shared", "rank": [1, 0, 1], "title": "old", "state": "completed", "owns_task": True}
        shared = {"hash": "shared", "rank": [1, 0, 1], "title": "old", "state": "completed", "owns_task": False}
        first = {"current": old, "pending": {"hash": "new-a", "rank": [2, 1, 2], "title": "new A", "state": "downloading"}}
        second = {"current": shared}
        records = {"one": {"IPX-811": first}, "two": {"IPX-811": second}}

        class CompleteDownloader(FakeDownloader):
            def list_tasks(self, hashes=None):
                return {"new-a": {"progress": 1, "complete": True}}

        downloader = CompleteDownloader()
        app.poll_downloader_records(downloader, records["one"], [], records)
        self.assertEqual(downloader.removed, [])
        self.assertTrue(shared["owns_task"])

        second["pending"] = {"hash": "new-b", "rank": [2, 1, 2], "title": "new B", "state": "downloading"}
        class CompleteSecondDownloader(FakeDownloader):
            def list_tasks(self, hashes=None):
                return {"new-b": {"progress": 1, "complete": True}}

        second_downloader = CompleteSecondDownloader()
        app.poll_downloader_records(second_downloader, records["two"], [], records)
        self.assertEqual(second_downloader.removed, [("shared", True)])

    def test_shared_completed_torrent_is_hardlinked_into_subscription_folder(self):
        source_root = Path(self.directory.name) / "source"
        source = source_root / "release"
        source.mkdir(parents=True)
        source_video = source / "video.mkv"
        source_video.write_bytes(b"video")
        target = Path(self.directory.name) / "second-subscription"
        app.copy_release_to_subscription(str(source), str(target))
        target_video = target / "release" / "video.mkv"
        self.assertEqual(target_video.read_bytes(), b"video")
        self.assertEqual(target_video.stat().st_ino, source_video.stat().st_ino)

    def test_existing_downloader_task_warns_without_linking_files(self):
        root = Path(self.directory.name)
        source = root / "existing-task" / "SSNI-229.mp4"
        source.parent.mkdir()
        source.write_bytes(b"video")
        target = root / "actress-subscription"
        subscription = {"id": "sub", "performer": "Actress", "keywords": "", "download_dir": str(target), "wash_enabled": True}
        candidate = {"id": "1289514", "code": "SSNI-229", "title": "SSNI-229 4K", "rank": [2, 0, 1], "seeders": 5, "created": "2026", "metadata_complete": True}

        class ExistingTaskDownloader(FakeDownloader):
            def add(self, data, title, download_dir):
                self.added.append((data, title, download_dir))
                return {"hash": "existinghash", "owned": False, "existing_task": {"state": "uploading", "content_path": "/downloads/SSNI-229.mp4"}}

        downloader = ExistingTaskDownloader()
        records = {}
        results = []
        with patch.object(app, "mteam_search", return_value=[candidate]), patch.object(app, "mteam_detail", return_value={"performer": "Actress"}), patch.object(app, "fetch_torrent", return_value=torrent_bytes()), patch.object(app, "scan_local_releases", return_value={}):
            app.process_subscription(subscription, records, {"mteam_token": "token"}, downloader, results)

        self.assertEqual(results[0]["state"], "downloading")
        self.assertIn("已存在相同种子", results[0]["message"])
        self.assertIn("/downloads/SSNI-229.mp4", results[0]["message"])
        self.assertIn("不会自动硬链接", results[0]["message"])
        release = records["sub"]["SSNI-229"]["current"]
        self.assertIsNotNone(release)
        self.assertFalse(release["copy_required"])
        self.assertFalse(target.exists())

    def test_completed_existing_downloader_task_warns_when_subscription_file_is_missing(self):
        root = Path(self.directory.name)
        source = root / "downloader" / "SSNI-229.mp4"
        source.parent.mkdir()
        source.write_bytes(b"source video")
        target = root / "subscription"
        release = {
            "hash": "existinghash", "state": "downloading", "owns_task": False,
            "copy_required": True, "download_dir": str(target), "code": "SSNI-229", "title": "SSNI-229",
            "directory_warning_logged": True,
        }
        records = {"SSNI-229": {"current": release}}

        class CompletedDownloader(FakeDownloader):
            def __init__(self):
                super().__init__()
                self.rechecked = []
                self.progress_callback = None

            def _progress(self, message, event):
                if self.progress_callback:
                    self.progress_callback(message, event)

            def list_tasks(self, hashes=None):
                return {"existinghash": {"complete": True, "progress": 1, "content_path": str(source)}}

            def recheck(self, info_hash, download_dir=""):
                self.rechecked.append(info_hash)

        results = []
        downloader = CompletedDownloader()
        downloader.progress_callback = lambda message, event: app.log_event(message, event=event, subscription="Actress")
        app.poll_downloader_records(downloader, records, results)
        self.assertEqual(downloader.rechecked, ["existinghash"])
        target.mkdir()
        target_video = target / "SSNI-229.mp4"
        target_video.write_bytes(b"restored video")
        app.poll_downloader_records(downloader, records, results)
        self.assertNotIn("directory_recheck_requested", release)
        target_video.unlink()
        app.poll_downloader_records(downloader, records, results)

        self.assertTrue(target.exists())
        self.assertFalse(target_video.exists())
        self.assertNotIn("copy_required", release)
        self.assertTrue(release["directory_warning_logged"])
        self.assertEqual(downloader.rechecked, ["existinghash", "existinghash"])
        self.assertTrue(release["directory_recheck_requested"])
        warnings = [item for item in app.get_logs() if item["event"] == "existing_task_directory_recheck"]
        self.assertEqual(len(warnings), 2)
        self.assertIn("未找到 SSNI-229", warnings[0]["message"])

    def test_shared_completed_torrent_refuses_copy_across_filesystems(self):
        source_root = Path(self.directory.name) / "source"
        source_root.mkdir()
        source = source_root / "video.mkv"
        source.write_bytes(b"video")
        target = Path(self.directory.name) / "second-subscription"
        with patch.object(app.os, "link", side_effect=OSError(app.errno.EXDEV, "cross-device link")):
            with self.assertRaisesRegex(app.AppError, "未复制文件"):
                app.copy_release_to_subscription(str(source), str(target))
        target_video = target / source.name
        self.assertFalse(target_video.exists())
        self.assertTrue(source.exists())

    def test_shared_directory_cross_filesystem_failure_removes_partial_links(self):
        source = Path(self.directory.name) / "source"
        source.mkdir()
        (source / "first.mkv").write_bytes(b"first")
        (source / "second.mkv").write_bytes(b"second")
        target = Path(self.directory.name) / "second-subscription"
        original_link = app.os.link

        def fail_second_link(source_file, target_file):
            if Path(source_file).name == "second.mkv":
                raise OSError(app.errno.EXDEV, "cross-device link")
            return original_link(source_file, target_file)

        with patch.object(app.os, "link", side_effect=fail_second_link):
            with self.assertRaisesRegex(app.AppError, "未复制文件"):
                app.copy_release_to_subscription(str(source), str(target))
        self.assertFalse((target / "source" / "first.mkv").exists())
        self.assertTrue((source / "first.mkv").exists())


if __name__ == "__main__":
    unittest.main()
