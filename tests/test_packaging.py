"""The binary installers' build inputs.

Cheap structural checks: the payload markers the build relies on, the shell syntax of the
installer preamble, and that the release script keeps attaching installers. The real
install/run/uninstall cycle lives in ``scripts/build_binary.sh`` and CI.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.shell import BASH

ROOT = os.path.join(os.path.dirname(__file__), "..")
HEADER = os.path.join(ROOT, "packaging", "installer", "linux-header.sh")
SPEC = os.path.join(ROOT, "packaging", "litcli.spec")
RELEASE = os.path.join(ROOT, "scripts", "release.sh")
VERIFY = os.path.join(ROOT, "scripts", "verify_release.sh")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class InstallerTemplateTest(unittest.TestCase):
    def test_header_carries_the_payload_marker_and_version_placeholder(self):
        text = _read(HEADER)
        self.assertTrue(text.rstrip().endswith("__PAYLOAD__"))
        self.assertIn("@VERSION@", text)

    def test_marker_is_the_last_line_so_extraction_starts_right_after_it(self):
        lines = _read(HEADER).splitlines()
        self.assertEqual(lines[-1], "__PAYLOAD__")

    @unittest.skipUnless(BASH, "no POSIX shell")
    def test_header_parses_as_a_shell_script(self):
        rc = subprocess.run([BASH, "-n", HEADER], capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)

    def test_uninstall_paths_are_covered(self):
        text = _read(HEADER)
        self.assertIn("--uninstall", text)
        self.assertIn("opt/litcli-", text)  # the versioned tree, removed on uninstall


class SpecTest(unittest.TestCase):
    def test_spec_freezes_the_package_entry_and_its_optional_deps(self):
        text = _read(SPEC)
        self.assertIn('"entry.py"', text)
        for package in ("playwright", "jieba"):
            self.assertIn(f'"{package}"', text)


class ReleaseWiringTest(unittest.TestCase):
    def test_binary_build_is_opt_in_and_installers_get_attached(self):
        text = _read(RELEASE)
        self.assertIn("--binary) DO_BINARY=1", text)
        self.assertIn("dist/* dist/bin/*", text)  # both are uploaded

    def test_a_binary_release_needs_both_platforms(self):
        text = _read(RELEASE)
        # one platform builds locally; the other is fetched from CI or supplied by hand
        self.assertIn("*-linux-*.run", text)
        self.assertIn("*-windows-*-setup.exe", text)
        self.assertIn("--installer) DO_BINARY=1", text)
        self.assertIn("fetch_ci_installer", text)

    def test_consumer_check_requires_the_windows_installer(self):
        text = _read(VERIFY)
        self.assertIn("-windows-", text)
        self.assertIn("SHA256SUMS", text)


class CiWiringTest(unittest.TestCase):
    def test_the_frozen_build_downloads_and_launches_chromium(self):
        text = _read(CI)
        self.assertIn("backends install-browser", text)
        self.assertIn("chromium", text)
        self.assertIn("install-browser --check", text)
        self.assertIn("--with-deps", text)
        self.assertIn("windows-latest", text)  # the browser path is exercised on both platforms

    @unittest.skipUnless(BASH, "no POSIX shell")
    def test_release_script_parses(self):
        rc = subprocess.run([BASH, "-n", RELEASE], capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)

    @unittest.skipUnless(BASH, "no POSIX shell")
    def test_checksum_step_survives_the_bin_subdirectory(self):
        # `sha256sum ./* bin/*` also gets dist/bin (a directory) and fails under pipefail.
        import re
        import shutil
        import tempfile
        helper = re.search(r"^write_sums\(\) \{.*?^\}", _read(RELEASE), re.S | re.M).group()
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        os.makedirs(os.path.join(work, "dist", "bin"))
        for rel in ("dist/litcli-1.0.tar.gz", "dist/bin/litcli-1.0.run"):
            with open(os.path.join(work, rel), "w", encoding="utf-8") as f:
                f.write("x")
        script = f"set -euo pipefail; cd {work!r}\n{helper}\nwrite_sums"
        rc = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        sums = _read(os.path.join(work, "dist", "SHA256SUMS"))
        self.assertIn("  litcli-1.0.tar.gz", sums)
        self.assertNotIn("bin/", sums)  # attachments download side by side, names must be flat
        # ...and the format must stay one `sha256sum -c` accepts, from a flat download dir
        flat = os.path.join(work, "flat")
        os.makedirs(flat)
        for rel in ("dist/SHA256SUMS", "dist/litcli-1.0.tar.gz", "dist/bin/litcli-1.0.run"):
            shutil.copy(os.path.join(work, rel), flat)
        rc = subprocess.run([BASH, "-c", f"cd {flat!r} && sha256sum -c --quiet SHA256SUMS"],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)


if __name__ == "__main__":
    unittest.main()
