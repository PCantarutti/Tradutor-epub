"""Tests for epub_translator.cache."""

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from epub_translator.cache import BlockCache, glossary_fingerprint, key_for


# ------------------------------------------------------------------
# key_for
# ------------------------------------------------------------------

class TestKeyFor:
    def test_deterministic(self, tmp_path: Path) -> None:
        """Same inputs -> same key (deterministic)."""
        k1 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        k2 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        assert k1 == k2

    def test_changes_on_target(self, tmp_path: Path) -> None:
        """Changing target changes the key."""
        k1 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        k2 = key_for("a", "[1]", "fr-FR", "llama", "en", "g1", "ctx")
        assert k1 != k2

    def test_changes_on_model(self, tmp_path: Path) -> None:
        """Changing model changes the key."""
        k1 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        k2 = key_for("a", "[1]", "pt-BR", "gpt", "en", "g1", "ctx")
        assert k1 != k2

    def test_changes_on_glossary(self, tmp_path: Path) -> None:
        """Changing glossary hash changes the key."""
        k1 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        k2 = key_for("a", "[1]", "pt-BR", "llama", "en", "g2", "ctx")
        assert k1 != k2

    def test_changes_on_context(self, tmp_path: Path) -> None:
        """Changing context changes the key."""
        k1 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        k2 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx2")
        assert k1 != k2

    def test_text_changes_key(self, tmp_path: Path) -> None:
        """Different source text -> different key."""
        k1 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        k2 = key_for("b", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        assert k1 != k2

    def test_marker_text_changes_key(self, tmp_path: Path) -> None:
        """Different marker text -> different key."""
        k1 = key_for("a", "[1]", "pt-BR", "llama", "en", "g1", "ctx")
        k2 = key_for("a", "[2]", "pt-BR", "llama", "en", "g1", "ctx")
        assert k1 != k2


# ------------------------------------------------------------------
# glossary_fingerprint
# ------------------------------------------------------------------

class TestGlossaryFingerprint:
    def test_deterministic(self) -> None:
        lines = ["apple -> maçã", "banana -> banana"]
        f1 = glossary_fingerprint(lines)
        f2 = glossary_fingerprint(lines)
        assert f1 == f2

    def test_order_matters(self) -> None:
        lines1 = ["apple -> maçã", "banana -> banana"]
        lines2 = ["banana -> banana", "apple -> maçã"]
        assert glossary_fingerprint(lines1) != glossary_fingerprint(lines2)

    def test_empty(self) -> None:
        f1 = glossary_fingerprint([])
        f2 = glossary_fingerprint([])
        assert f1 == f2


# ------------------------------------------------------------------
# BlockCache — round-trip & persistence
# ------------------------------------------------------------------

class TestBlockCachePersistence:
    def test_put_get_round_trip(self, tmp_path: Path) -> None:
        """put + get returns the stored value."""
        cache = BlockCache(tmp_path / "cache.json")
        cache.put("k1", "translated [[1]]")
        assert cache.get("k1") == "translated [[1]]"

    def test_persists_between_instances(self, tmp_path: Path) -> None:
        """A second instance sees the data written by the first."""
        path = tmp_path / "cache.json"
        c1 = BlockCache(path)
        c1.put("k1", "translated [[1]]")
        c1.put("k2", "translated [[2]]")
        del c1  # force __del__ if any (there isn't one)

        c2 = BlockCache(path)
        assert c2.get("k1") == "translated [[1]]"
        assert c2.get("k2") == "translated [[2]]"
        assert len(c2) == 2

    def test_missing_file_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        cache = BlockCache(path)
        assert len(cache) == 0
        assert cache.get("nope") is None

    def test_clear(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        c1 = BlockCache(path)
        c1.put("k1", "v1")
        c1.clear()
        assert len(c1) == 0
        assert c1.get("k1") is None

    def test_clear_persists(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        c1 = BlockCache(path)
        c1.put("k1", "v1")
        c1.clear()
        c2 = BlockCache(path)
        assert len(c2) == 0


# ------------------------------------------------------------------
# Atomicity — os.replace failure leaves original intact
# ------------------------------------------------------------------

class TestAtomicity:
    def test_os_replace_failure_preserves_original(self, tmp_path: Path) -> None:
        """When os.replace raises, the original file is not corrupted."""
        path = tmp_path / "cache.json"
        cache = BlockCache(path)
        cache.put("k1", "v1")

        # Verify file exists and is valid JSON
        raw = json.loads(path.read_text())
        assert raw["entries"]["k1"] == "v1"

        # Now corrupt os.replace
        with patch("os.replace", side_effect=OSError("disk full")):
            cache.put("k2", "v2")

        # Original should still be intact
        raw = json.loads(path.read_text())
        assert raw["entries"]["k1"] == "v1"
        assert "k2" not in raw["entries"]  # not persisted


# ------------------------------------------------------------------
# Thread safety
# ------------------------------------------------------------------

class TestThreadSafety:
    def test_20_threads_put_no_data_loss(self, tmp_path: Path) -> None:
        """20 threads writing concurrently should not lose data."""
        path = tmp_path / "cache.json"
        cache = BlockCache(path)
        n_threads = 20
        n_writes = 50

        def worker(i: int) -> None:
            for j in range(n_writes):
                cache.put(f"t{i}_k{j}", f"v{i}_{j}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(cache) == n_threads * n_writes

        # Verify every key is present
        for i in range(n_threads):
            for j in range(n_writes):
                assert cache.get(f"t{i}_k{j}") == f"v{i}_{j}"


# ------------------------------------------------------------------
# Corrupt file handling
# ------------------------------------------------------------------

class TestCorruptFile:
    def test_corrupt_json_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text("{'broken", encoding="utf-8")
        cache = BlockCache(path)
        assert len(cache) == 0

        # .corrupt file should exist
        corrupt = tmp_path / "cache.corrupt"
        assert corrupt.exists()

    def test_invalid_version_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        data = {"version": 99, "entries": {"k": "v"}}
        path.write_text(json.dumps(data), encoding="utf-8")
        cache = BlockCache(path)
        assert len(cache) == 0
        corrupt = tmp_path / "cache.corrupt"
        assert corrupt.exists()

    def test_missing_entries_key_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        data = {"version": 1}  # no 'entries'
        path.write_text(json.dumps(data), encoding="utf-8")
        cache = BlockCache(path)
        assert len(cache) == 0
        corrupt = tmp_path / "cache.corrupt"
        assert corrupt.exists()

    def test_not_a_dict_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text('["not", "a", "dict"]', encoding="utf-8")
        cache = BlockCache(path)
        assert len(cache) == 0
        corrupt = tmp_path / "cache.corrupt"
        assert corrupt.exists()
