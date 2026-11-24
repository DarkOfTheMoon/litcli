"""The one description of every entitled/guarded publisher, and of the order they are tried in.

Each entry says how to recognise a target that belongs to that publisher (its key normalizer,
the URL shapes and DOI prefixes it owns), which registered backend serves it, and what to tell
the reader when the site does not answer. ``commands.fetch``, ``fulltext``, ``config._registry``
and ``completions`` all derive from this table, so a reader cannot be registered, offered by
completion or put into the chain without a spec — and a spec cannot name a kind that does not
exist. ``PUBLISHER_ORDER`` is the chain order (a subscription copy outranks a landing page).

The hints are strings rather than callables, and the table holds no eager reader imports: this
module must not import ``commands`` (the command imports it), and one reader failing to import
must leave ``fulltext``, ``fetch`` and even ``litcli --help`` usable — the normalizers import
their module on first use and answer "" when it is broken.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from importlib import import_module
from typing import NamedTuple

from ..errors import LitcliError

#: Reader modules already imported (``None`` = it failed): one ``sys.modules`` lookup per use.
_modules: dict[str, object] = {}


def _module(name: str):
    """The reader module, imported on first use; ``None`` when it cannot be imported.

    A missing module raises ``ImportError``, but "not ready" also covers a syntax error, a bad
    constant and a renamed attribute — none of them an ``ImportError`` — so anything the import
    raises is caught (`KeyboardInterrupt`/`SystemExit` aside).
    """
    if name not in _modules:
        try:
            _modules[name] = import_module(f".{name}", __package__)
        except Exception:
            _modules[name] = None
    return _modules[name]


def _normalizer(name: str) -> Callable[[str], str]:
    """The named reader's key normalizer, resolved lazily (see :func:`_module`)."""
    def normalize(target: str) -> str:
        module = _module(name)
        return "" if module is None else module.normalize_key(target)
    return normalize

#: DOI prefixes owned by Elsevier (ScienceDirect, incl. the 10.1006/10.1053/10.1067 legacy ones).
ELSEVIER_DOI_RE = ("10.1016/", "10.1006/", "10.1053/", "10.1067/")

#: DOI prefixes owned by Wiley (the Online Library, incl. the 10.1046/10.1096 legacy ones and
#: the Physiological Society's 10.1113/10.1114).
WILEY_DOI_RE = ("10.1002/", "10.1111/", "10.1113/", "10.1114/", "10.1046/", "10.1096/")

#: DOI prefixes owned by Springer Nature (SpringerLink, BMC/SpringerOpen, EPJ, Nature-branded
#: content, and the Palgrave/Materials & Structures legacies).
SPRINGER_DOI_RE = ("10.1007/", "10.1186/", "10.1140/", "10.1038/", "10.1617/", "10.1057/")

#: DOI prefix owned by Cambridge University Press (Cambridge Core journals and books).
CAMBRIDGE_DOI_RE = ("10.1017/",)

#: DOI prefixes hosted on pubs.aip.org (AIP Publishing itself and the Acoustical Society of
#: America, whose journals AIP serves).
AIP_DOI_RE = ("10.1063/", "10.1121/")

#: DOI prefixes owned by the guarded publishers added in 2026-11 (ACM, APS, IOP, Taylor &
#: Francis, AIAA, IEEE Xplore). Each sits behind a bot challenge, so its step only starts for
#: its own prefixes or a pasted page URL.
ACM_DOI_RE = ("10.1145/",)
APS_DOI_RE = ("10.1103/",)
IOP_DOI_RE = ("10.1088/",)
TANDF_DOI_RE = ("10.1080/",)
AIAA_DOI_RE = ("10.2514/",)
IEEE_DOI_RE = ("10.1109/",)

#: Targets that are a given publisher by shape: its ``name:``/``name/`` key, or a page URL on
#: its own hosts. A pasted page URL must be recognised or it would drift down the chain.
_SCIENCEDIRECT_LIKE_RE = re.compile(
    r"^(?:sciencedirect[\:/]|https?://(?:[\w-]+\.)*sciencedirect\.com/)", re.I)
_WILEY_LIKE_RE = re.compile(r"^(?:wiley:|https?://(?:[\w-]+\.)*(?:wiley\.com|doi\.org)/)", re.I)
_SPRINGER_LIKE_RE = re.compile(
    r"^(?:springer[:\/]|https?://(?:[\w-]+\.)*(?:springer\.com|springernature\.com|nature\.com|"
    r"biomedcentral\.com|doi\.org)/)", re.I)
_CAMBRIDGE_LIKE_RE = re.compile(r"^(?:cambridge[:\/]|https?://(?:[\w-]+\.)*cambridge\.org/)", re.I)
_AIP_LIKE_RE = re.compile(
    r"^(?:aip[:/]|https?://(?:[\w-]+\.)*(?:pubs\.aip\.org|aip\.scitation\.org)/)", re.I)
_ACM_LIKE_RE = re.compile(r"^(?:acm[:\/]|https?://(?:[\w-]+\.)*dl\.acm\.org/)", re.I)
_APS_LIKE_RE = re.compile(r"^(?:aps[:\/]|https?://(?:[\w-]+\.)*link\.aps\.org/)", re.I)
_IOP_LIKE_RE = re.compile(r"^(?:iop[:\/]|https?://(?:[\w-]+\.)*iopscience\.iop\.org/)", re.I)
_TANDF_LIKE_RE = re.compile(r"^(?:tandf[:\/]|https?://(?:[\w-]+\.)*tandfonline\.com/)", re.I)
_AIAA_LIKE_RE = re.compile(r"^(?:aiaa[:\/]|https?://(?:[\w-]+\.)*arc\.aiaa\.org/)", re.I)
_IEEE_LIKE_RE = re.compile(r"^(?:ieee[:\/]|https?://(?:[\w-]+\.)*ieeexplore\.ieee\.org/)", re.I)


def _sciencedirect_key(target: str) -> str:
    """``pii:<ID>`` when the target is a ScienceDirect article URL or bare PII, else ``""``."""
    module = _module("sciencedirect")
    if module is None:
        return ""
    try:
        normalized = module.normalize_key(target)
    except LitcliError:
        return ""
    return normalized if normalized.startswith("pii:") else ""


class _PublisherSpec(NamedTuple):
    """One guarded publisher step: how its targets are recognised and how it is reported."""

    name: str
    label: str
    #: The registered backend type and the class attribute ``config._registry`` imports.
    backend: str
    normalize: Callable[[str], str]
    like: re.Pattern
    doi_re: tuple[str, ...]
    #: What the reader needs to set, printed when that publisher is the reason no file came.
    hint: str
    #: The key kind a pasted page URL yields (``pii:`` on ScienceDirect, else ``url:``); the
    #: page step is what turns it into a DOI the rest of the chain can key on.
    page_prefix: str = "url:"

    def page_key(self, target: str) -> str:
        try:
            normalized = self.normalize(target)
        except LitcliError:
            return ""
        return normalized if normalized.startswith(self.page_prefix) else ""


#: Every guarded/entitled publisher, in chain order (a subscription copy outranks a page).
_PUBLISHER_SPECS: tuple[_PublisherSpec, ...] = (
    _PublisherSpec(
        "sciencedirect", "ScienceDirect", "ScienceDirectBackend", _sciencedirect_key,
        _SCIENCEDIRECT_LIKE_RE, ELSEVIER_DOI_RE,
        "  ScienceDirect needs a logged-in session: set [backends.sciencedirect] cookie"
        " (or cookie_file) from your browser's off-campus session",
        page_prefix="pii:"),
    _PublisherSpec(
        "wiley", "Wiley", "WileyBackend", _normalizer("wiley"), _WILEY_LIKE_RE, WILEY_DOI_RE,
        "  Wiley needs a logged-in session: set [backends.wiley] cookie"
        " (or cookie_file) and user_agent from your browser's off-campus session,"
        " or set a free tdm_token (its PDF route skips Cloudflare)"),
    _PublisherSpec(
        "springer", "SpringerLink", "SpringerBackend", _normalizer("springer"),
        _SPRINGER_LIKE_RE, SPRINGER_DOI_RE,
        "  SpringerLink PDFs need a session: set [backends.springer] cookie"
        " (or cookie_file) and user_agent from an authenticated browser tab"
        " (open-access articles download without one)"),
    _PublisherSpec(
        "cambridge", "Cambridge Core", "CambridgeBackend", _normalizer("cambridge"),
        _CAMBRIDGE_LIKE_RE, CAMBRIDGE_DOI_RE,
        "  Cambridge Core PDFs need a session: set [backends.cambridge] cookie"
        " (or cookie_file) and user_agent from an authenticated browser tab"
        " (open-access articles download without one)"),
    _PublisherSpec(
        "aip", "AIP", "AipBackend", _normalizer("aip"), _AIP_LIKE_RE, AIP_DOI_RE,
        "  AIP is behind a Cloudflare challenge: set [backends.aip] cookie"
        " (or cookie_file) and user_agent from a browser tab that cleared it"),
    _PublisherSpec(
        "acm", "ACM Digital Library", "AcmBackend", _normalizer("acm"), _ACM_LIKE_RE, ACM_DOI_RE,
        "  ACM is behind a Cloudflare challenge: set [backends.acm] cookie"
        " (or cookie_file) and user_agent from a browser tab that cleared it"),
    _PublisherSpec(
        "aps", "APS", "ApsBackend", _normalizer("aps"), _APS_LIKE_RE, APS_DOI_RE,
        "  APS is behind a Cloudflare challenge: set [backends.aps] cookie"
        " (or cookie_file) and user_agent from a browser tab that cleared it"),
    _PublisherSpec(
        "iop", "IOPscience", "IopBackend", _normalizer("iop"), _IOP_LIKE_RE, IOP_DOI_RE,
        "  IOPscience is behind a Cloudflare challenge: set [backends.iop] cookie"
        " (or cookie_file) and user_agent from a browser tab that cleared it"
        " (the gate covers open-access items too)"),
    _PublisherSpec(
        "tandf", "Taylor & Francis Online", "TandfBackend", _normalizer("tandf"),
        _TANDF_LIKE_RE, TANDF_DOI_RE,
        "  Taylor & Francis is behind a Cloudflare challenge: set [backends.tandf] cookie"
        " (or cookie_file) and user_agent from a browser tab that cleared it"),
    _PublisherSpec(
        "aiaa", "AIAA", "AiaaBackend", _normalizer("aiaa"), _AIAA_LIKE_RE, AIAA_DOI_RE,
        "  AIAA is behind a Cloudflare challenge: set [backends.aiaa] cookie"
        " (or cookie_file) and user_agent from a browser tab that cleared it"),
    _PublisherSpec(
        "ieee", "IEEE Xplore", "IeeeBackend", _normalizer("ieee"), _IEEE_LIKE_RE, IEEE_DOI_RE,
        "  IEEE Xplore is behind an AWS WAF challenge: set [backends.ieee] cookie"
        " (or cookie_file) and user_agent from a browser tab that passed it"),
)

#: The chain order, derived: a subscription copy outranks a landing page, and the free OA
#: corpora / landing page / Sci-Hub sit outside this table.
PUBLISHER_ORDER = tuple(spec.name for spec in _PUBLISHER_SPECS)
