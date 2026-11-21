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

    def test_packaged_icons_use_supplied_png_assets_at_expected_sizes(self):
        for size in (64, 256):
            data = png_icon(size)
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(int.from_bytes(data[16:20], "big"), size)
            self.assertEqual(int.from_bytes(data[20:24], "big"), size)


if __name__ == "__main__":
    unittest.main()
