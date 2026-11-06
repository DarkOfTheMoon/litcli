"""Platform-location and save-name rules.

Both are pure and run everywhere, which is the point: the Windows behaviour is pinned from
a Linux CI run instead of only on the Windows job.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litcli.paths as paths
from litcli.paths import config_dir, safe_filename, target_path

#: A real DOI shape whose characters Windows rejects verbatim.
LEGACY_DOI = "10.1002/(SICI)1097-0258(19970815)16:15<1705::AID-SIM599>3.0.CO;2-H"


class SafeFilenameTest(unittest.TestCase):
    def test_plain_doi_keeps_its_shape(self):
        self.assertEqual(safe_filename("10.1038/s41598-024-80509-9", "pdf"),
                         "10.1038_s41598-024-80509-9.pdf")

    def test_windows_illegal_characters_are_replaced(self):
        name = safe_filename(LEGACY_DOI, "pdf")
        self.assertEqual(set(name) & set('<>:"/\\|?*'), set())
        self.assertTrue(name.endswith(".pdf"))
        self.assertIn("16_15_1705", name)

    def test_trailing_dots_and_spaces_are_trimmed(self):
        self.assertEqual(safe_filename("entry. ", "pdf"), "entry.pdf")
        self.assertEqual(safe_filename("book/", "pdf"), "book.pdf")

    def test_reserved_device_names_get_a_suffix(self):
        self.assertEqual(safe_filename("CON", "pdf"), "CON_.pdf")
        self.assertEqual(safe_filename("nul.2024", "pdf"), "nul_.2024.pdf")
        self.assertEqual(safe_filename("console", "pdf"), "console.pdf")

    def test_long_stem_is_truncated(self):
        name = safe_filename("x" * 400, "pdf")
        self.assertLessEqual(len(name), 124)

    def test_degenerate_stem_falls_back(self):
        self.assertEqual(safe_filename("///", "pdf"), "download.pdf")

    def test_unicode_survives(self):
        self.assertEqual(safe_filename("图书情报/2024", "pdf"), "图书情报_2024.pdf")


class TargetPathTest(unittest.TestCase):
    def test_no_out_is_the_bare_name(self):
        self.assertEqual(target_path(None, "10.1/x", "pdf"), "10.1_x.pdf")

    def test_out_directory_gets_the_default_name(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(target_path(d, "10.1/x", "pdf"),
                             os.path.join(d, "10.1_x.pdf"))

    def test_trailing_separator_counts_as_a_directory(self):
        self.assertEqual(target_path("papers/", "10.1/x", "pdf"),
                         os.path.join("papers", "10.1_x.pdf"))

    def test_out_file_path_is_kept_verbatim(self):
        self.assertEqual(target_path("mine.pdf", "10.1/x", "pdf"), "mine.pdf")


class ConfigDirTest(unittest.TestCase):
    def test_windows_uses_appdata(self):
        env = {"APPDATA": r"C:\Users\me\AppData\Roaming"}
        with mock.patch.object(paths, "_IS_WINDOWS", True), mock.patch.dict(os.environ, env):
            self.assertEqual(str(config_dir()),
                             os.path.join(r"C:\Users\me\AppData\Roaming", "litcli"))

    def test_windows_without_appdata_falls_back_to_the_profile(self):
        self.assertIn("litcli", str(config_dir()))

    def test_posix_honours_xdg_config_home(self):
        env = {"XDG_CONFIG_HOME": "/tmp/xdg"}
        with mock.patch.object(paths, "_IS_WINDOWS", False), mock.patch.dict(os.environ, env):
            self.assertEqual(config_dir(), __import__("pathlib").Path("/tmp/xdg") / "litcli")

    def test_posix_defaults_to_dot_config(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with mock.patch.object(paths, "_IS_WINDOWS", False), mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config_dir(), __import__("pathlib").Path.home() / ".config" / "litcli")


class BrowserCacheTest(unittest.TestCase):
    """A frozen Playwright would otherwise put browsers inside the wiped bundle."""

    def test_windows_uses_localappdata(self):
        env = {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
        with mock.patch.object(paths, "_IS_WINDOWS", True), mock.patch.dict(os.environ, env):
            self.assertEqual(paths.browser_cache_dir(),
                             paths.Path(env["LOCALAPPDATA"]) / "ms-playwright")

    def test_posix_honours_xdg_cache_home(self):
        env = {"XDG_CACHE_HOME": "/tmp/cache"}
        with mock.patch.object(paths, "_IS_WINDOWS", False), mock.patch.dict(os.environ, env):
            self.assertEqual(paths.browser_cache_dir(), paths.Path("/tmp/cache/ms-playwright"))

    def test_apply_does_not_override_an_explicit_setting(self):
        env = {"PLAYWRIGHT_BROWSERS_PATH": "/elsewhere"}
        with mock.patch.dict(os.environ, env):
            paths.apply_browser_cache()
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "/elsewhere")

    def test_apply_sets_the_cache_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "PLAYWRIGHT_BROWSERS_PATH"}
        with mock.patch.dict(os.environ, env, clear=True):
            paths.apply_browser_cache()
            self.assertTrue(os.environ["PLAYWRIGHT_BROWSERS_PATH"].endswith("ms-playwright"))


if __name__ == "__main__":
    unittest.main()
