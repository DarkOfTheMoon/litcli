"""The metadata response cache: opt-out, TTL, and that a hit really skips the network."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from litcli import cache, cli
from litcli.backends.openalex import OpenAlexBackend
from tests.http_stub import speed_up, stub


class _TmpCache(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="litcli-cache-test-")
        self.addCleanup(mock.patch.stopall)
        mock.patch("litcli.cache.cache_dir", return_value=Path(self._dir)).start()
        self._disabled = cache._DISABLED
        cache._DISABLED = False
        self.addCleanup(setattr, cache, "_DISABLED", self._disabled)


class RoundTripTest(_TmpCache):
    def test_put_then_get(self):
        cache.put("k", "body")
        self.assertEqual(cache.get("k"), "body")

    def test_a_missing_key_is_none(self):
        self.assertIsNone(cache.get("absent"))

    def test_an_expired_entry_is_none(self):
        cache.put("k", "body")
        with mock.patch.object(cache, "TTL", -1):
            self.assertIsNone(cache.get("k"))

    def test_a_corrupt_entry_is_none_not_an_error(self):
        path = cache._path("k")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertIsNone(cache.get("k"))

    def test_disabled_reads_and_writes_nothing(self):
        cache.disable()
        cache.put("k", "body")
        self.assertIsNone(cache.get("k"))

    def test_clear_removes_entries(self):
        cache.put("a", "x")
        cache.put("b", "y")
        self.assertEqual(cache.clear(), 2)
        self.assertEqual(cache.clear(), 0)


class PruneTest(_TmpCache):
    def _age(self, key, seconds):
        old = cache._path(key)
        stamp = old.stat().st_atime - seconds
        os.utime(old, (stamp, stamp))

    def _stamp(self, key, minutes_ago):
        old = cache._path(key)
        stamp = time.time() - minutes_ago * 60
        os.utime(old, (stamp, stamp))

    def test_expired_entries_go_before_fresh_ones(self):
        for i in range(2):
            cache.put(f"stale{i}", "x")
            self._age(f"stale{i}", cache.TTL + 60)
        with mock.patch.object(cache, "MAX_ENTRIES", 3):
            for i in range(5):
                cache.put(f"fresh{i}", "x")
                # The write clock is coarser than this loop (15.6 ms ticks on Windows), so the
                # fresh entries otherwise share one mtime and "oldest" falls back to the
                # directory listing order. Stamp their own order instead.
                self._stamp(f"fresh{i}", 10 - i)
        left = {p.name for p in Path(self._dir).rglob("*") if p.is_file()}
        self.assertEqual(left, {cache._path(f"fresh{i}").name for i in (2, 3, 4)})

    def test_prune_below_the_cap_removes_nothing(self):
        cache.put("a", "x")
        cache.put("b", "x")
        with mock.patch.object(cache, "MAX_ENTRIES", 10):
            cache._maybe_prune()
        self.assertEqual((cache.get("a"), cache.get("b")), ("x", "x"))
    def setUp(self):
        super().setUp()
        self.be = OpenAlexBackend(mailto="t@e.com")
        speed_up(self.be)

    def test_a_second_read_is_served_from_the_cache(self):
        rec = stub(self.be, (200, {"results": []}))
        self.be.cacheable = True
        self.be.fetch_json("https://api.openalex.org/works?q=x")
        self.be.fetch_json("https://api.openalex.org/works?q=x")
        self.assertEqual(len(rec.urls), 1)

    def test_a_non_cacheable_backend_always_asks(self):
        rec = stub(self.be, (200, {"results": []}))
        self.be.fetch_json("https://api.openalex.org/works?q=x")
        self.be.fetch_json("https://api.openalex.org/works?q=x")
        self.assertEqual(len(rec.urls), 2)

    def test_two_urls_do_not_share_an_entry(self):
        first = stub(self.be, (200, {"n": 1}))
        self.be.cacheable = True
        self.assertEqual(self.be.fetch_json("https://api.openalex.org/works?q=a"), {"n": 1})
        stub(self.be, (200, {"n": 2}))
        self.be.cacheable = True
        self.assertEqual(self.be.fetch_json("https://api.openalex.org/works?q=b"), {"n": 2})
        self.assertEqual(len(first.urls), 1)


class ConcurrencyTest(_TmpCache):
    """Writers fan out from several threads; a shared temp name would truncate entries."""

    def _hammer(self, keys, workers=8):
        import threading

        errors = []

        def work(slot):
            for i in keys:
                try:
                    cache.put(f"{slot}-{i}", f"body-{slot}-{i}")
                except Exception as e:  # pragma: no cover - surfaced through the assert
                    errors.append(e)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_distinct_keys_from_many_threads_all_stay_readable(self):
        self.assertEqual(self._hammer(range(5)), [])
        for slot in range(8):
            for i in range(5):
                self.assertEqual(cache.get(f"{slot}-{i}"), f"body-{slot}-{i}")

    def test_no_temp_files_are_left_behind(self):
        self._hammer(range(3), workers=4)
        self.assertEqual([p for p in Path(self._dir).rglob("*.tmp")], [])

    def test_the_same_key_written_by_many_threads_ends_valid(self):
        self._hammer([0], workers=8)
        self.assertEqual(cache.get("0-0"), "body-0-0")


class FlagTest(unittest.TestCase):
    def setUp(self):
        self._disabled = cache._DISABLED
        self.addCleanup(setattr, cache, "_DISABLED", self._disabled)
        cache._DISABLED = False

    def _main(self, argv):
        with mock.patch.dict(cli._TOP_LEVEL_COMMANDS, {"search": mock.Mock(return_value=0)}), \
                mock.patch.object(cli, "load_config", return_value={}), \
                mock.patch.object(cli, "_apply_proxy"), \
                mock.patch("builtins.print"):
            cli.main(argv)

    def test_the_flag_turns_it_off_wherever_it_is_written(self):
        for argv in (["--no-cache", "search", "x"], ["search", "x", "--no-cache"]):
            cache._DISABLED = False
            self._main(argv)
            self.assertTrue(cache._DISABLED, argv)

    def test_the_flag_is_not_required(self):
        self._main(["search", "x"])
        self.assertFalse(cache._DISABLED)


if __name__ == "__main__":
    unittest.main()
