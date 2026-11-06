"""Shell-completion generator offline tests: spec introspection + static renderers.

Fully offline: builds the spec from ``build_parser()`` (no network/backends) and checks the
emitted scripts' structure. One subprocess test sources the generated bash script and exercises
its completion engine, so the most important behavior (the emitted script actually completes) is
covered without a live shell.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli.commands.parser import build_parser
from litcli.completions import BACKENDS, SHELL_CHOICES, TYPES, build_spec, render
from tests.shell import BASH


def _spec():
    return build_spec(build_parser())


class BuildSpecTest(unittest.TestCase):
    def test_top_includes_all_commands(self):
        top = _spec()["top"]
        for cmd in ("search", "refs", "citing", "related", "export", "completions",
                    "backends", "data", "fetch"):
            self.assertIn(cmd, top)

    def test_subchild_maps(self):
        spec = _spec()
        self.assertEqual(spec["subs"][("data",)],
                         ["get", "add", "set", "del", "attach", "tree", "members"])
        self.assertIn("list", spec["subs"][("backends",)])
        self.assertEqual(spec["subs"][("completions",)], SHELL_CHOICES)

    def test_search_flags_and_choices(self):
        opts = _spec()["opts"][("search",)]
        for flag in ("--format", "--sort", "--title", "--author", "--year", "--container",
                     "--backend", "--type", "--limit", "--all", "--no-cite"):
            self.assertIn(flag, opts)
        self.assertEqual(opts["--format"], ["table", "json", "csl"])
        self.assertEqual(opts["--sort"], ["cited"])
        self.assertEqual(opts["--backend"], BACKENDS)
        self.assertEqual(opts["--type"], TYPES)

    def test_data_add_pdf_is_file_flag(self):
        spec = _spec()
        self.assertIn("--pdf", spec["opts"][("data", "add")])
        self.assertIn("--csl", spec["opts"][("data", "add")])
        self.assertIn("--pdf", spec["files"])
        self.assertIn("--csl", spec["files"])

    def test_fetch_flags(self):
        opts = _spec()["opts"][("fetch",)]
        for flag in ("--out", "--book", "--no-download", "--scihub"):
            self.assertIn(flag, opts)
        self.assertIn("--out", _spec()["files"])

    def test_export_format_choices(self):
        opts = _spec()["opts"][("export",)]
        self.assertEqual(opts["--format"], ["csljson", "bibtex", "ris", "text"])

    def test_file_positional_attach_src(self):
        spec = _spec()
        self.assertEqual(spec["file_positionals"][("data", "attach")], {2: "src"})

    def test_root_version_ok(self):
        # Root options (``--version``) are stored keyed at ``()`` so they complete.
        self.assertIn("--version", _spec()["opts"][()])


class RenderTest(unittest.TestCase):
    def test_bash_skeleton(self):
        out = render("bash", _spec())
        self.assertTrue(out.startswith("#!/usr/bin/env bash"))
        self.assertIn("complete -F _litcli_complete litcli", out)
        self.assertIn('echo "search refs citing related export completions backends data fetch"', out)
        self.assertIn('grep -qx -- "$2"', out)

    def test_zsh_skeleton(self):
        out = render("zsh", _spec())
        self.assertTrue(out.startswith("#compdef litcli"))
        self.assertIn("compdef _litcli litcli", out)
        self.assertIn("compadd --", out)

    def test_fish_skeleton(self):
        out = render("fish", _spec())
        self.assertIn("complete -c litcli", out)
        self.assertIn("__fish_use_subcommand", out)
        self.assertIn("-l backend -a 'zotero arxiv crossref openalex openlibrary uspto cnki douban scihub "
                      "zlibrary doaj europepmc doab zenodo'", out)




@unittest.skipUnless(BASH, "no usable bash on this host")
class BashEngineTest(unittest.TestCase):
    def test_engine_resolves_candidates(self):
        """Source the generated bash script and check it resolves several tab contexts."""
        spec = build_spec(build_parser())
        script = render("bash", spec)
        with tempfile.NamedTemporaryFile("w", suffix=".bash", newline="\n", delete=False) as f:
            f.write(script)
            # Git Bash on Windows cannot read a ``C:\...`` argument (backslashes are escapes).
            path = pathlib.Path(f.name).as_posix()
        try:
            probe = r'''
source "$1"
run() { COMP_WORDS=("$@"); COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 )); _litcli_complete; printf '%s\n' "${COMPREPLY[*]}"; }
run litcli comp
run litcli completions ""
run litcli search --backend ""
run litcli data add --p
'''
            proc = subprocess.run([BASH, "-c", probe, "_", path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)  # keep the reason in the message
            out = proc.stdout.splitlines()
            self.assertEqual(out[0].strip(), "completions")
            self.assertEqual(set(out[1].split()), set(SHELL_CHOICES))
            self.assertIn("openalex", out[2].split())
            self.assertIn("--pdf", out[3].split())
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
