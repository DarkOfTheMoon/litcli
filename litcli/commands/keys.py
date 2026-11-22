"""Item keys: what a key points at, which backend serves it, and how to narrow it.

The CLI emits ``source/key`` (first table column) exactly so a key can be handed back
unchanged. Read commands resolve that prefix themselves, so ``data get openalex/W123`` works
without ``--backend`` and a key never silently lands on the default backend.
"""
from __future__ import annotations

import re

from ..config import _registry, get_backend

# Source prefixes the table output / export files put in front of a key.
_SOURCE_PREFIXES = ("openalex/", "arxiv/", "crossref/", "zotero/", "scihub/")


def arxiv_id_from_key(key: str) -> str | None:
    """Recognize an arXiv id in a key (e.g. ``arxiv/1806.09180`` or ``arXiv:1806.09180``)."""
    m = re.match(r"^\s*(?:arxiv[:/]|arXiv[:/]|arXiv)?\s*(\d{4}\.\d{4,5})(?:v\d+)?\s*$", key)
    if m:
        return m.group(1)
    return None


def normalize_export_key(key: str, backend_name: str) -> str:
    """Normalize an arXiv key to a form the backend can resolve before export.

    - arxiv backend: give the bare arXiv id (its get resolves via ``id:<key>`` search);
    - other backends (openalex/crossref): convert to DOI ``10.48550/arxiv.<id>``,
      otherwise openalex.get would treat ``arxiv/1806.09180`` as an OpenAlex id and
      return 404.
    """
    aid = arxiv_id_from_key(key)
    if not aid:
        return key
    if backend_name == "arxiv":
        return aid
    if backend_name not in ("openalex", "crossref"):
        # A local store resolves its own item keys; handing it a DOI would 404.
        return key
    return f"10.48550/arxiv.{aid}"


def normalize_related_key(key: str) -> str:
    """Normalize a graph-edge key: strip a source prefix and turn an arXiv id into its DOI.

    The openalex backend's W-id/DOI lookup can then resolve the key forms the CLI itself
    emits (``extra["openalex_id"]``, table keys, exported arXiv keys).
    """
    k = key.strip()
    for prefix in _SOURCE_PREFIXES:
        if k.startswith(prefix):
            k = k[len(prefix):]
            break
    aid = arxiv_id_from_key(k)
    if aid:
        return f"10.48550/arxiv.{aid}"
    return k


#: Backend types whose configured name differs from the registry key.
_SOURCE_ALIASES = {"zotero": "zotero_local"}


def known_sources() -> set[str]:
    """Every source name a key may carry a prefix of (registry types and their names)."""
    reg = _registry()
    return set(reg) | set(_SOURCE_ALIASES) | {cls.name for cls in reg.values() if cls.name}


def split_source(key: str) -> tuple[str, str]:
    """``"openalex/W123"`` -> ``("openalex", "W123")``; a bare key -> ``("", key)``."""
    head, sep, rest = key.partition("/")
    if sep and rest and head in known_sources():
        return head, rest
    return "", key


def resolve_backend(cfg, explicit: str | None, key: str, fallback: str):
    """The backend and key to use: an explicit ``--backend`` wins, then the key's own prefix,
    then ``fallback``. The prefix is stripped only when that is the source actually used."""
    source, bare = split_source(key)
    if explicit:
        return get_backend(explicit, cfg), bare if source and _is_source(explicit, source) else key
    if source:
        return get_backend(_SOURCE_ALIASES.get(source, source), cfg), bare
    return get_backend(fallback, cfg), key


def _is_source(name: str, source: str) -> bool:
    return name in (source, _SOURCE_ALIASES.get(source))
