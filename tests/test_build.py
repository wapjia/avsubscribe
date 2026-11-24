import unittest

from scripts.build_fpk import bump_version, png_icon, versioned_app_asset
from pathlib import Path


class BuildVersionTests(unittest.TestCase):
    def test_build_increments_only_patch_version(self):
        self.assertEqual(bump_version("1.0.0"), "1.0.1")
        self.assertEqual(bump_version("2.4.19"), "2.4.20")

    def test_build_carries_each_hundred_patch_versions_into_minor(self):
        self.assertEqual(bump_version("1.0.99"), "1.1.0")
        self.assertEqual(bump_version("1.0.100"), "1.1.1")
        self.assertEqual(bump_version("1.0.115"), "1.1.16")
        self.assertEqual(bump_version("1.1.199"), "1.3.0")

    def test_build_rejects_non_semantic_versions(self):
        with self.assertRaises(ValueError):
            bump_version("1.0")

    def test_build_injects_package_version_into_frontend_asset(self):
        source = b'export const APP_VERSION = "__APP_VERSION__";'
        self.assertEqual(
            versioned_app_asset("www/version.js", source, "1.0.101"),
            b'export const APP_VERSION = "1.0.101";',
        )
        self.assertEqual(versioned_app_asset("www/app.js", source, "1.0.101"), source)

    def test_cgi_allows_loading_version_module_imported_by_app(self):
        cgi = (Path(__file__).resolve().parents[1] / "app/ui/index.cgi").read_text(encoding="utf-8")
        self.assertIn("/version.js) FILE=\"version.js\" ;;", cgi)

    def test_cgi_serves_round_log_module_as_javascript(self):
        cgi = (Path(__file__).resolve().parents[1] / "app/ui/index.cgi").read_text(encoding="utf-8")
        self.assertIn("/log-utils.mjs) FILE=\"log-utils.mjs\" ;;", cgi)
        self.assertIn('*.js|*.mjs) CONTENT_TYPE="application/javascript; charset=utf-8" ;;', cgi)

    def test_cgi_serves_repair_status_module(self):
        cgi = (Path(__file__).resolve().parents[1] / "app/ui/index.cgi").read_text(encoding="utf-8")
        self.assertIn('/repair-status.mjs) FILE="repair-status.mjs" ;;', cgi)

    def test_actress_workspace_is_navigable_and_posts_manual_names(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "app/www/index.html").read_text(encoding="utf-8")
        script = (root / "app/www/app.js").read_text(encoding="utf-8")
        dashboard = html.index('data-view="all"')
        actresses = html.index('data-view="actresses"')
        self.assertLess(dashboard, actresses)
        self.assertIn('id="actresses-view"', html)
        self.assertIn('id="actress-body"', html)
        self.assertIn('api("/api/actresses")', script)
        self.assertIn('api("/api/actresses/add"', script)
        self.assertIn('api("/api/actresses/merge"', script)
        self.assertIn("actress-mteam-search", script)
        self.assertIn("browse/adult?keyword=", script)
        self.assertIn('search_keywords: keywordsToSave', script)
        self.assertIn('event.key === "Enter" || event.key === ","', script)
        self.assertIn("${actress.source_name}（${chineseName}）", script)
        self.assertIn('if (activeView === "actresses") renderActresses();', script)
        self.assertIn('method: "POST"', script)
        self.assertIn('source_name: sourceName, chinese_name: chineseName', script)

    def test_actress_input_drafts_survive_refresh_and_polling_is_throttled(self):
        script = (Path(__file__).resolve().parents[1] / "app/www/app.js").read_text(encoding="utf-8")
        self.assertIn("const actressDrafts = new Map();", script)
        self.assertIn("actressDrafts.set(actress.source_name, input.value)", script)
        self.assertIn("actressDrafts.has(actress.source_name)", script)
        self.assertIn("Date.now() - lastActressesRefresh >= 60000", script)

    def test_local_video_rows_render_hardlink_badge(self):
        script = (Path(__file__).resolve().parents[1] / "app/www/app.js").read_text(encoding="utf-8")
        self.assertIn('file.hardlink_count > 1', script)
        self.assertIn('marker.textContent = "硬链接"', script)
        self.assertIn('hardlinks`', script)
        self.assertIn("positionHardlinkPreview", script)
        css = (Path(__file__).resolve().parents[1] / "app/www/style.css").read_text(encoding="utf-8")
        self.assertIn(".version-markers .video-refresh, .version-markers .subtitle-marker", css)
        html = (Path(__file__).resolve().parents[1] / "app/www/index.html").read_text(encoding="utf-8")
        self.assertIn('id="log-category-filter"', html)
        self.assertIn('id="log-keyword"', html)
        self.assertIn('id="clear-log-keyword"', html)
        self.assertIn('id="existing-keyword"', html)
        self.assertIn('id="clear-existing-keyword"', html)

    def test_mobile_local_video_cards_use_compact_content_sized_rows(self):
        css = (Path(__file__).resolve().parents[1] / "app/www/style.css").read_text(encoding="utf-8")
        self.assertIn("grid-auto-rows: min-content; align-content: start", css)
        self.assertIn("max-height: clamp(280px, 68dvh, 680px)", css)
        self.assertIn("grid-row: calc(2 * var(--file-index) + 2)", css)
        self.assertIn("grid-row: calc(2 * var(--file-index) + 1)", css)
        self.assertIn("grid-column: 1; grid-row: 1 / span 2", css)

    def test_mobile_subscription_actions_use_two_columns(self):
        css = (Path(__file__).resolve().parents[1] / "app/www/style.css").read_text(encoding="utf-8")
        self.assertIn(".subscription-view #selected-scan { grid-column: auto; order: 1", css)

    def test_subscription_search_links_are_rendered_per_keyword_below_title(self):
        script = (Path(__file__).resolve().parents[1] / "app/www/app.js").read_text(encoding="utf-8")
        self.assertIn("function subscriptionSearchKeywords(subscription)", script)
        self.assertIn('const details = [["搜索关键字", subscriptionSearchKeywords(selected)]', script)
        self.assertIn('value.className = "search-keyword-links"', script)
        self.assertNotIn('description.append(searchLink)', script)

    def test_pc_cover_hover_preloads_uncropped_cover(self):
        script = (Path(__file__).resolve().parents[1] / "app/www/app.js").read_text(encoding="utf-8")
        self.assertIn("const coverPreloads = new Map();", script)
        self.assertIn("preloadCover(covers[0])", script)
        self.assertIn('window.matchMedia("(max-width: 600px)")', script)
        self.assertIn('thumbnail.querySelector("img")', script)
        self.assertIn('currentImage.cloneNode(true)', script)

    def test_gallery_opens_the_currently_displayed_thumbnail_first(self):
        script = (Path(__file__).resolve().parents[1] / "app/www/app.js").read_text(encoding="utf-8")
        self.assertIn("const coverUrl = (displayed && !failed.has(displayed))", script)
        self.assertIn("showCoverHoverPreview(thumbnail, [displayed, ...covers, poster]", script)

    def test_packaged_icons_use_supplied_png_assets_at_expected_sizes(self):
        for size in (64, 256):
            data = png_icon(size)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(int.from_bytes(data[16:20], "big"), size)
            self.assertEqual(int.from_bytes(data[20:24], "big"), size)


if __name__ == "__main__":
    unittest.main()
