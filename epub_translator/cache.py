"""JSON cache for translation blocks with resume, atomic save, thread-safe."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("epub_translator")

CACHE_VERSION = 1


def key_for(
    text: str,
    marker_text: str,
    target: str,
    model: str,
    source: str,
    glossary_hash: str,
    context: str,
    file_path: str = "",
) -> str:
    """Deterministic SHA-256 key from translation inputs.

    Concatenates fields with '\\x00' separator so the same inputs always
    produce the same key, regardless of ordering of internal structures.

    ``file_path`` é o caminho do arquivo DENTRO do EPUB (ex.:
    ``OEBPS/Text/section-0005.html``). Ele é parte da chave porque o índice
    do bloco reinicia a cada arquivo: sem ele, dois arquivos com blocos de
    mesmo texto (ex.: o parágrafo de copyright repetido em cada capítulo)
    colidirem na chave e um receberia a tradução do outro.
    """
    payload = "\x00".join(
        [
            text,
            marker_text,
            target,
            model,
            source,
            glossary_hash,
            context,
            file_path,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def glossary_fingerprint(lines: list[str]) -> str:
    """SHA-256 fingerprint of a glossary list.

    Normalises the glossary by joining with '\\n' so that the same
    glossary always yields the same hash.
    """
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BlockCache:
    """Thread-safe JSON-backed cache for translated blocks.

    Parameters
    ----------
    path : str | Path
        Filesystem path to the cache JSON file.  A ``.tmp`` sibling is
        used for atomic writes; on corruption it is renamed ``.corrupt``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries: Dict[str, str] = {}
        # Garante que a pasta do cache exista (ex.: "saida/").
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Return the cached marker_text for *key*, or ``None``."""
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, marker_text: str) -> None:
        """Store *marker_text* under *key* and persist atomically."""
        with self._lock:
            self._entries[key] = marker_text
            self._save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        """Remove all entries and persist the empty state."""
        with self._lock:
            self._entries.clear()
            self._save()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load cache from disk.  Missing or corrupt files start empty."""
        with self._lock:
            if not self._path.exists():
                return
            try:
                raw = self._path.read_text(encoding="utf-8")
                data: Dict[str, Any] = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("not a dict")
                if data.get("version") != CACHE_VERSION:
                    raise ValueError(f"version {data.get('version')} != {CACHE_VERSION}")
                entries = data.get("entries")
                if not isinstance(entries, dict):
                    raise ValueError("missing 'entries'")
                self._entries = entries
            except (json.JSONDecodeError, ValueError, OSError):
                logger.warning(
                    "Cache file %s is corrupt or unreadable; starting fresh.",
                    self._path,
                )
                self._backup_corrupt()
                self._entries = {}

    def _save(self) -> None:
        """Write entries to disk atomically via tmp + os.replace.

        On OSError the tmp file is left behind (no data loss); the
        in-memory state remains correct.
        """
        data = {
            "version": CACHE_VERSION,
            "entries": dict(self._entries),
        }
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.replace(str(tmp_path), str(self._path))
        except OSError:
            logger.warning("Atomic save failed for %s; in-memory state preserved.", self._path)

    def _backup_corrupt(self) -> None:
        """Rename the corrupt file to ``.corrupt``."""
        try:
            corrupt_path = self._path.with_suffix(".corrupt")
            os.replace(str(self._path), str(corrupt_path))
        except OSError:
            pass  # best effort
