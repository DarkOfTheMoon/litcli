"""Item-key normalization shared by ``export`` and the graph edges.

Both commands accept the key forms the CLI itself emits (``source/key`` table keys,
``extra["openalex_id"]`` URLs, arXiv ids), and both need those narrowed to what the
backend can resolve.
"""
from __future__ import annotations

import re

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
