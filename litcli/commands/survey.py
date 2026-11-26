"""``litcli survey new|run|status|stop`` — the survey lifecycle.

``new`` is the only command that sets constraints; it writes the project and its seeds and
hands back nothing but the project's name, so a caller (an agent) gets an address, not a
report. Without ``--seed`` it runs one opening search and takes the first candidates as the
initial cluster; an opening search that finds nothing is an error rather than an empty survey,
because a survey with no seed has nowhere to walk from.

``run`` walks the graph one hop at a time. The frontier is rebuilt from ``trace.jsonl`` on
every start — admitted works whose hop never ended — so a run killed halfway is resumed by
the same command without expanding anything twice. Every step is written to the trace before
the next begins, so the checkpoint is never ahead of the work.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import cache, effort, expansion, fulltext, paths, project, scoring, survey_events, unpaywall
from ..admission import decide, rank
from ..aggregate import identity
from ..backends._entitled import EntitledBackend
from ..backends.base import GraphSource
from ..backends.http import RETRY_CODES
from ..browser import PlaywrightTimeoutError, session_from_config
from ..config import get_all_backends
from ..errors import BadInput, BotWall, LitcliError, Unavailable
from ..models import AcademicItem
from ..pdf import download as download_bytes
from ..provenance import stamp
from ..transport import HttpError
from .keys import graph_sources
from .search import relevance

#: Candidates the opening search may supply when the user named no seeds.
OPENING_SEEDS = 3
#: The lock and stop files inside a project directory (``$`` sorts after the data files).
LOCK_NAME = "$.lock"
STOP_NAME = "$.stop"


def add_new_parser(p: argparse.ArgumentParser) -> None:
    """Register the arguments of ``survey new`` (also used by its tests)."""
    p.add_argument("question", nargs="+", help="the survey question (quote it or not)")
    p.add_argument("--seed", action="append", default=[], metavar="DOI",
                   help="a work to start from, repeatable (default: one opening search)")
    p.add_argument("--year", default="", help="year window A-B (A- and -B also accepted)")
    p.add_argument("--type", action="append", default=[],
                   help="prefer this OpenAlex article type, repeatable (weight 2.0)")
    p.add_argument("--container", default="", help="require this journal/conference substring")
    p.add_argument("--threshold", type=float, default=None,
                   help=f"admission score floor (default {project.DEFAULT_THRESHOLD})")
    p.add_argument("--effort", default=effort.DEFAULT,
                   choices=effort.LEVELS,
                   help=f"how wide to walk (default {effort.DEFAULT})")
    p.add_argument("--root", default=None, help="project root (default: the user data dir)")


def _split_year(text: str) -> tuple[str, str]:
    """``"2019-2024"`` -> both ends; ``"2019-"`` / ``"-2024"`` -> one end."""
    left, sep, right = (text or "").partition("-")
    if not sep:
        return left.strip(), ""
    return left.strip(), right.strip()


def _seed_identity(seed: str) -> str:
    """The identity a user-supplied seed is recorded under (a DOI unless it says otherwise)."""
    if seed.startswith(("doi:", "isbn:", "title:")):
        return seed
    return "doi:" + seed if seed.startswith(("10.", "https://doi.org/")) else "title:" + seed


def _opening_seeds(cfg: dict, question: str) -> list[AcademicItem]:
    """Ask every source once and keep the closest titles, deduplicated.

    Stopping at the first source with enough hits let that one source's own order decide the
    whole walk (a live run followed lattice-Boltzmann work on a reduced-order-model question
    because an earlier source answered first); the sources are merged and ranked by the same
    relevance ``search`` uses instead.
    """
    found: list[AcademicItem] = []
    for name, backend in get_all_backends(cfg, corpora=True).items():
        try:
            found.extend(stamp(backend.search(question, limit=OPENING_SEEDS), backend))
        except Exception as e:  # one dead source must not sink the opening
            print(f"[skipped] {name}: {e}", file=sys.stderr)
    seen: set[str] = set()
    unique = []
    for item in found:
        if identity(item) in seen:
            continue
        seen.add(identity(item))
        unique.append(item)
    unique.sort(key=lambda item: relevance(item, question))
    return unique[:OPENING_SEEDS]


def _expandable(key: str) -> bool:
    """A key the walk's ``/works/<id>`` accepts: an OpenAlex work id or a DOI."""
    return (key.startswith("W") and key[1:].isdigit()) or key.startswith("10.")


def _bare_id(work_id: str) -> str:
    """A work id without its URL or source prefix (``https://openalex.org/W1`` -> ``W1``)."""
    tail = str(work_id).strip().rstrip("/").rsplit("/", 1)[-1]
    return tail.partition(":")[2] or tail


def _graph_id(item, backend) -> str:
    """The id a seed is walked by: its openalex work id, looked up when only a DOI is known.

    The seed-citer bonus compares a seed against ``referenced_works``, which are bare ``W…``
    ids, so a seed left as a DOI could never match. Where the id is already known it is used;
    where only a DOI is at hand one lookup fetches it, and a source that cannot answer leaves
    the spelling we have (a DOI still walks, as ``/works/doi:…``).
    """
    key = str(getattr(item, "key", item) or "").strip()
    known = str((getattr(item, "extra", None) or {}).get("openalex_id") or "")
    if known:
        return _bare_id(known)
    return _graph_id_of(_unprefixed(key), backend)


def _unprefixed(key: str) -> str:
    """A typed seed without its ``source:`` prefix or ``https://doi.org/`` wrapper."""
    key = key.strip()
    for prefix in ("openalex:", "doi:", "https://doi.org/"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    return key


def _graph_id_of(key: str, backend) -> str:
    """``_graph_id`` for a bare spelling: a W id as is, a DOI through one lookup."""
    if key.startswith("W") and key[1:].isdigit():
        return key
    getter = getattr(backend, "get", None)
    if key.startswith("10.") and getter is not None:
        try:
            found = getter(key)
            return _bare_id(str(found.extra.get("openalex_id") or found.key or key))
        except Exception as e:  # a lookup is an improvement, never a requirement
            print(f"[skipped] {key}: {e}", file=sys.stderr)
    return key


def _seed_row(typed: str, backend) -> dict:
    """How a typed seed is recorded: the id it is walked by, plus what its download needs.

    A seed is admitted without ever being scored, and an admitted work is downloaded, so the
    row has to carry a DOI — which a ``W…`` typed seed only has after one lookup. The identity
    stays what the user typed.
    """
    key = _unprefixed(typed)
    row = {"identity": _seed_identity(typed), "admit": True, "reason": "seed", "seed": "flag",
           "key": key, "doi": key if key.startswith("10.") else "", "title": "",
           "score": 0.0, "parts": {}, "download": ""}
    getter = getattr(backend, "get", None)
    if getter is not None and (key.startswith("W") or key.startswith("10.")):
        found, error = None, None
        for _attempt in range(2):  # a burst 429 here would cost the seed its work id
            try:
                found = getter(key)
                break
            except Exception as e:
                error = e
        if found is None:  # the walk can still go ahead on the bare id
            print(f"[skipped] {key}: {error}", file=sys.stderr)
        else:
            row.update(key=_bare_id(str(found.extra.get("openalex_id") or found.key or key)),
                       doi=found.doi, title=found.title)
    return row


def _addressable(item: AcademicItem, sources) -> bool:
    """Whether some graph source can be asked about ``item`` in the spelling it has."""
    return any(expansion.target_for(source, item) for source in sources)


def _resolvable_seeds(found: list[AcademicItem], cfg: dict) -> list[AcademicItem]:
    """The seeds the walk can actually place, dropping the ones no source can address.

    An opening search fans out over every source, and the walk asks each source in its own
    spelling — an OpenAlex W id, a DOI, an S2 paperId, a PMID. A seed spelled for any of them
    walks as it stands. One spelled for none of them (an arXiv id) is converted through a title
    lookup, which also brings the work id the seed-citer bonus needs; where that fails, only
    the sources that can still address the row keep it. A seed that stays unplaceable costs a
    note here, not a dead project later.
    """
    backend = get_all_backends(cfg).get("openalex")
    if backend is None:
        return found
    sources = graph_sources(cfg)
    kept: list[AcademicItem] = []
    for item in found:
        if _expandable(str(item.key)):  # already a work id or a DOI: nothing to convert
            kept.append(item)
            continue
        hits = []
        if item.title:
            try:
                hits = backend.search(item.title, limit=1,
                                      filters={"title": str(item.title).replace('"', "")})
            except Exception as e:  # one unplaceable seed must not sink the opening
                print(f"[skipped] {item.key}: {e}", file=sys.stderr)
        if hits:
            kept.append(stamp([hits[0]], backend)[0])
        elif _addressable(item, sources):
            kept.append(item)
        elif not item.title:  # a title lookup needs a title
            print(f"[skipped] {item.key}: no title to look it up by", file=sys.stderr)
        else:
            print(f"[skipped] {item.key}: not in openalex under its title, and no source can "
                  "address it", file=sys.stderr)
    return kept


def cmd_survey_new(cfg, args) -> int:
    """Create the project, record its seeds, print its directory name."""
    question = " ".join(args.question).strip()
    if not question:
        raise BadInput("a survey needs a question")
    year_from, year_to = _split_year(args.year)
    fields = {
        "year_from": year_from,
        "year_to": year_to,
        "container": args.container or "",
        "type_weight": {kind: 2.0 for kind in args.type},
        "threshold": args.threshold if args.threshold is not None else project.DEFAULT_THRESHOLD,
        "effort": args.effort,
    }
    typed = [s.strip() for s in args.seed if s.strip()]
    backend = get_all_backends(cfg).get("openalex")
    found: list[AcademicItem] = []
    flags: list[tuple[str, str]] = []  # (what the user typed, the id the graph walks)
    if typed:
        flags = [_seed_row(seed, backend) for seed in typed]
        seeds = [row["key"] for row in flags]
    else:
        found = _resolvable_seeds(_opening_seeds(cfg, question), cfg)
        if not found:
            print("[error] the opening search found nothing; pass --seed to start from a work "
                  "you already know", file=sys.stderr)
            return 2
        # The work id, not the DOI: the graph hands references back as work ids, so a seed has
        # to be spelled the same way for the seed-citer bonus to ever match.
        seeds = [_graph_id(item, backend) for item in found]

    created = project.create(Path(args.root).expanduser() if args.root else paths.project_root(),
                             question, seeds=seeds, **fields)
    for item in found:
        record = {"identity": identity(item), "admit": True, "reason": "seed", "seed": "search",
                  "source": item.source, "key": _graph_id(item, backend), "title": item.title,
                  "doi": item.doi, "year": item.year,
                  "score": 0.0, "parts": {}}
        project.append_seen(created, record)
    for row in flags:
        project.append_seen(created, row)
    print(created.root.name)
    return 0


@dataclass
class Deps:
    """What the loop touches outside its checkpoint, injected so a test needs no network.

    ``resolve`` is the fulltext chain already bound to the config; ``publishers`` are the
    session-carrying backends it may need bytes from; ``session_alive`` answers whether the
    shared browser session still lives (the loop checks it at a hop's start and again at each
    download, so a session that dies mid-hop is caught and that download takes the cookie path).
    ``stream`` is where JSONL events go (``None`` keeps the loop silent for tests).
    """

    backend: Any
    resolve: Callable[[str], dict]
    #: Graph sources in preference order. ``expand`` walks the axes over all of them; ``backend``
    #: stays the OpenAlex one (or None) for the id lookups only it can answer.
    graph: list[GraphSource] = field(default_factory=list)
    #: ``name -> backend`` for stamping a candidate with the source that actually yielded it.
    by_name: dict[str, Any] = field(default_factory=dict)
    publishers: dict = field(default_factory=dict)
    session_alive: Callable[[], bool] = lambda: True
    browser_fetch: Callable[[str], bytes] | None = None
    close: Callable[[], None] = lambda: None
    stream: Any = None

    def sources(self) -> list[GraphSource]:
        """The graph sources to walk (the single ``backend`` when a test built one directly)."""
        if self.graph:
            return list(self.graph)
        return [self.backend] if self.backend is not None else []


def deps_from_config(cfg: dict, survey_project: project.Project) -> Deps:
    """The real dependencies: configured backends, the fulltext chain, the live session."""
    backends = get_all_backends(cfg)
    graph = graph_sources(cfg, backends)
    # An unconfigured openalex is still added by `keys` as a keyless fallback; the OA lookup
    # and the fulltext chain hang off this, so it must be that instance rather than nothing.
    backend = backends.get("openalex") or next((s for s in graph if s.name == "openalex"), None)
    if not graph:
        raise BadInput("a survey walks the citation graph and no source supplies one; enable "
                       "[backends.openalex], [backends.semanticscholar], [backends.opencitations],"
                       " [backends.crossref] or [backends.europepmc]")
    publishers = {name: b for name, b in backends.items() if isinstance(b, EntitledBackend)}
    session = next((b.session for b in publishers.values() if b.session is not None), None)
    if session is None:
        # The browser is what gets past a gated OA link, and that has nothing to do with a
        # subscription: with `[browser] enabled` the survey carries one even when no publisher
        # is configured. `session_from_config` answers None when the section is off.
        session = session_from_config(cfg)
    email = ((cfg.get("unpaywall") or {}).get("email") or os.environ.get("UNPAYWALL_EMAIL")
             or unpaywall.DEFAULT_EMAIL)

    def resolve(doi: str) -> dict:
        return fulltext.resolve_fulltext(doi, email, backends=backends, openalex_backend=backend,
                                         europepmc_backend=backends.get("europepmc"),
                                         zenodo_backend=backends.get("zenodo"),
                                         publishers=publishers,
                                         scihub_backend=backends.get("scihub"))

    return Deps(backend=backend, resolve=resolve, publishers=publishers,
                graph=graph, by_name={b.name: b for b in graph},
                session_alive=_session_alive(session) if session is not None else (lambda: True),
                browser_fetch=session.download if session is not None else None,
                # `terminate`, not `close`: a run that started a browser has to stop it, or
                # every run of many leaves one behind. A browser the user opened is untouched.
                close=session.terminate if session is not None else (lambda: None))


def _session_alive(session) -> Callable[[], bool]:
    """``session.alive``, starting the browser once if it is not up yet.

    Nothing has started it for a survey (``fetch`` does that itself), and a session that was
    never started cannot answer, so the first check is also the launch.
    """
    def alive() -> bool:
        if session.alive():
            return True
        try:
            session.start()
        except (LitcliError, OSError, RuntimeError):
            return False
        return True
    return alive


def _detach_sessions(publishers: dict) -> None:
    """Drop the dead session from every publisher so this download takes the cookie path."""
    for backend in publishers.values():
        backend.session = None


def _write_file(path: Path, data: bytes) -> None:
    """Write ``data`` through a sibling temp file: a kill never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _land(item: AcademicItem, deps: Deps, survey_project: project.Project) -> str:
    """Get the candidate's fulltext into ``corpus/``; returns ``ok`` or why there is no copy."""
    if not item.doi:
        return "no-doi"
    try:
        info = deps.resolve(item.doi)
    except (LitcliError, OSError, RuntimeError, PlaywrightTimeoutError):
        return "unavailable"
    if not info or not info.get("has_fulltext"):
        return "unavailable"
    raw = info.get("data")
    if raw is None:
        raw = _fetch_info(info, item.doi, deps)
    fmt = str(info.get("format") or "pdf")
    if not raw or (fmt == "pdf" and not bytes(raw).startswith(b"%PDF")):
        return "unavailable"
    _write_file(project.corpus_dir(survey_project) / paths.safe_filename(item.doi, fmt),
                bytes(raw))
    return "ok"


def _fetch_info(info: dict, doi: str, deps: Deps) -> bytes | None:
    """The bytes for a resolved answer, cheapest route first.

    Order: the publisher's own route (it can hand over an entitled copy), then each link it
    has — plain HTTP first (most OA hosts), the live session second (a gated link answers
    only a real browser). :func:`_oa_links` puts the work's own OA copies behind the answer
    the chain gave. Each attempt is tried on its own, so one failure falls through.
    """
    url = str(info.get("url") or "")
    publisher = deps.publishers.get(str(info.get("kind") or ""))
    if publisher is None:
        publisher = next((b for b in deps.publishers.values()
                          if getattr(b, "matches_url", None) and b.matches_url(url)), None)
    routes = []
    if publisher is not None:
        routes.append(lambda: publisher.get_attachment(doi))
    links = [url, *_oa_links(doi, deps)] if url else _oa_links(doi, deps)
    for link in filter(None, links):
        routes.append(lambda link=link: download_bytes(link))
        if deps.browser_fetch is not None:
            routes.append(lambda link=link: deps.browser_fetch(link))
    for route in routes:
        try:
            data = route()
        except (LitcliError, OSError, RuntimeError, PlaywrightTimeoutError):
            # A page that accepts the request and then never answers raises playwright's own
            # TimeoutError, which is not an OSError: caught here it costs the route, like the
            # rest, instead of unwinding the whole walk through `_land`.
            continue
        if data:
            return bytes(data)
    return None


def _oa_links(doi: str, deps: Deps) -> list[str]:
    """The work's own open-access links from the graph source, as a last resort.

    The chain can answer a DOI with a publisher url that only a browser gets past (Unpaywall
    hands out plenty of those on link.aps.org), and having answered, it never asks OpenAlex.
    The work's own OA locations — usually an arXiv copy — are still one lookup away.
    """
    getter = getattr(deps.backend, "get", None)
    if getter is None:
        return []
    try:
        return [str(link) for link in (getter(doi).attachments or [])]
    except (LitcliError, KeyError, ValueError, OSError, RuntimeError):
        return []


def _write_meta(survey_project: project.Project, item: AcademicItem, status: str) -> None:
    """Keep a metadata sidecar beside the copy — a missing copy must not lose the record."""
    record = {"identity": identity(item), "doi": item.doi, "key": item.key, "source": item.source,
              "title": item.title, "year": item.year, "access": getattr(item, "access", ""),
              "download": status}
    name = paths.safe_filename(item.doi or identity(item), "json")
    _write_file(project.corpus_dir(survey_project) / name,
                json.dumps(record, ensure_ascii=False).encode("utf-8"))


def _download(item: AcademicItem, deps: Deps, survey_project: project.Project,
              session_was_alive: bool) -> str:
    """Download one admitted candidate, retrying through the cookie path if the session died.

    The session is checked at the hop start and again here, but the window can also close
    while the bytes are in flight: that failure belongs to the browser, so the one download
    goes out again cookie-only instead of being written off as the site's.
    """
    if not (session_was_alive and deps.publishers):
        return _land(item, deps, survey_project)
    if deps.session_alive():
        status = _land(item, deps, survey_project)
        if status == "ok" or deps.session_alive():
            return status
    _detach_sessions(deps.publishers)
    status = _land(item, deps, survey_project)
    return "cookie" if status == "ok" else status


def _download_kept(item: AcademicItem, deps: Deps, survey_project: project.Project,
                   session_was_alive: bool) -> str:
    """``_download`` with the failure contained: a dead or slow link costs this copy, not the walk.

    A document store that accepts the request and then hangs (a handle that 302s to a repository
    which never answers) raises out of the fetch after the navigation timeout, and an uncaught
    raise ended the whole run — at hop 0, on a seed, before anything was walked. The status is
    recorded as ``error:<type>`` so a resume skips the copy and the trace says what happened.
    """
    try:
        return _download(item, deps, survey_project, session_was_alive)
    except Exception as e:  # one link must not sink a walk
        return "error:" + type(e).__name__


def _row_item(row: dict) -> AcademicItem:
    """The item a checkpoint row stands for (what an expansion or a download works on).

    A DOI is read back from the key when the row predates the ``doi`` field: projects made
    before it was recorded carry their DOI there, and only there.
    """
    key = str(row.get("key") or "")
    doi = str(row.get("doi") or (key if key.startswith("10.") else ""))
    return AcademicItem(source=str(row.get("source") or "openalex"), key=key, doi=doi,
                        title=str(row.get("title") or ""), year=row.get("year"),
                        extra={"seed": row.get("seed")})


def _landed(seen_rows: list[dict]) -> dict[str, str]:
    """``identity`` -> the download outcome already recorded for it, per admitted row."""
    return {str(row.get("identity")): str(row.get("download")) for row in seen_rows
            if row.get("admit") and row.get("download")}


def _raiser(signum: int) -> Callable:
    """A handler that unwinds the stack, for a signal whose default action would just exit."""
    def raised(_signum, _frame) -> None:
        raise SystemExit(128 + signum)
    return raised


def _askable(item, sources) -> bool:
    """Whether some source declares an axis and has a spelling for ``item`` at all.

    A row no source can address (a Crossref reference that kept its text as the key) can never
    be expanded; putting it back on the frontier every hop would only churn.
    """
    for source in sources:
        if not getattr(source, "capabilities", frozenset()) & set(expansion.AXES):
            continue
        if expansion.target_for(source, item):
            return True
    return False


def _again_later(exc: BaseException) -> bool:
    """Whether the failure says "not now" rather than "not this one".

    A 429 burst or an unreachable host is the source's mood and the row deserves every retry
    the envelope allows; a 404 (or an answer that is not JSON at all) is the row itself, and one
    retry is the whole of what another hop can learn about it. The status is read first:
    ``HttpError`` is itself an :class:`Unavailable`, so a plain ``isinstance`` would call its
    404 "not now" and retry a dead work every hop to `max_hops`.
    """
    if isinstance(exc, HttpError):
        return exc.status in RETRY_CODES
    # A bot wall is a challenge page: the most retryable failure there is, not the row's.
    return isinstance(exc, (Unavailable, BotWall))


def _consumed(trace: list[dict]) -> set[str]:
    """Identities a hop has taken off the frontier for good.

    An ``error`` event speaks for its own hop: an identity that hop lists in ``expanded`` but
    also errors on was not really expanded, and the next run has to try it again (a 429 burst
    used to be written down as an expansion, so those traces stop on `saturated` at hop 0 with
    no request at all). Tracing that is what makes an old project resumable without hand edits.
    ``abandoned`` is the deliberate opposite: a row retried once and written off for good.
    """
    errored = {(str(e.get("identity")), e.get("hop"))
               for e in trace if e.get("event") == "error"}
    done = {str(w) for e in trace if e.get("event") == "hop_end"
            for w in e.get("abandoned") or []}
    done.update(str(w) for e in trace if e.get("event") == "hop_end"
                for w in e.get("expanded") or [] if (str(w), e.get("hop")) not in errored)
    return done


def _frontier(seen_rows: list[dict], trace: list[dict]) -> tuple[list[dict], set[str]]:
    """Admitted works never expanded, one row per identity, and everything already consumed.

    An identity gets several admitted rows over its life — the download pass writes the row
    back to record the copy — so the first row is kept and the rest are the same work. Handing
    one work to a hop twice reads its whole bibliography twice.
    """
    expanded = _consumed(trace)
    rows: dict[str, dict] = {}
    for row in seen_rows:
        identity_now = str(row.get("identity"))
        if row.get("admit") and identity_now not in expanded:
            rows.setdefault(identity_now, row)
    return list(rows.values()), expanded


def run_loop(survey_project: project.Project, deps: Deps) -> dict:
    """Walk the graph hop by hop from the last checkpoint; returns ``{"stop", "admitted"}``.

    Stop reasons: ``saturated`` (no new high for the level's streak, or nothing left to
    expand — the normal ending), ``budget`` (the discovery source's envelope is spent),
    ``max_hops``, ``stopped`` (a ``$.stop`` sentinel written by ``survey stop``) and
    ``interrupted`` (SIGINT, state left resumable).
    """
    trace = project.read_trace(survey_project)
    seen_rows = project.read_seen(survey_project)
    seen = {str(row.get("identity")) for row in seen_rows}
    frontier, _expanded = _frontier(seen_rows, trace)
    landed = _landed(seen_rows)
    collected = {str(work) for row in seen_rows if row.get("admit")
                 for work in row.get("referenced_works") or []}
    envelope = effort.resolve(survey_project.effort)
    #: Every graph source is its own site with its own allowance: a fallback answering for a
    #: throttled first source must not be paid for out of that first source's budget.
    sites = list(dict.fromkeys(expansion._site(source) for source in deps.sources()))
    counters = {key: int(envelope[key]) for key in ("refs", "citing", "related")}
    counters.update({key: int(envelope[f"{key}_cap"]) for key in ("arxiv", "publisher")})
    counters.update({site: int(envelope["graph_cap"]) for site in sites})
    hop =sum(1 for event in trace if event.get("event") == "hop_end")
    best = max((float(row.get("score") or 0.0) for row in seen_rows if row.get("admit")),
               default=0.0)
    streak = admitted_total = landed_total = 0
    retried: set[str] = set()  # identities an error already cost once (a dead row gets no more)
    interrupted: list[int] = []

    def emit(event: str, **fields) -> None:
        record = {"event": event, **fields}
        trace.append(record)  # scoring's sparsity reads it, so it must grow with the run
        project.append_trace(survey_project, record)
        if deps.stream is not None:
            print(survey_events.line(event, **fields), file=deps.stream)

    def on_signal(signum, _frame) -> None:
        # The first strike only ends the hop (the state stays resumable). The handler is put
        # back after it, because a stuck network call cannot be waited out and the second
        # strike has to raise. SIGTERM gets a raiser when it has no handler of its own: its
        # default action kills the process without the `finally` that removes the lock.
        interrupted.append(signum)
        with contextlib.suppress(ValueError):
            signal.signal(signum, previous.get(signum) or _raiser(signum))

    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):  # not the main thread: no handler to install
            previous[signum] = signal.signal(signum, on_signal)
    stop = ""
    emit("start", hop=hop, question=survey_project.question, effort=survey_project.effort)
    try:
        while not stop:
            sentinel = survey_project.root / STOP_NAME
            if sentinel.exists():
                with contextlib.suppress(OSError):
                    sentinel.unlink()
                stop = "stopped"
            elif interrupted:
                stop = "interrupted"
            elif hop >= int(envelope["max_hops"]):
                stop = "max_hops"
            elif all(counters.get(site, 0) <= 0 for site in sites):
                stop = "budget"
            elif not frontier or streak >= int(envelope["streak"]):
                stop = "saturated"
            if stop:
                break
            hop += 1
            high_before = best
            charged = 0
            session_was_alive = deps.session_alive()
            # A row can be admitted and never handed to the download path: a seed is, since it
            # is never seen as a fresh candidate, and so is a row whose run was killed between
            # its admission and its copy. Land those before expanding, once each: the row's
            # own `download` records it, so a resume reads it back and skips the second try.
            for row in frontier:
                identity_now = str(row.get("identity"))
                if identity_now in landed or counters["publisher"] <= 0:
                    continue
                counters["publisher"] -= 1
                item = _row_item(row)
                status = _download_kept(item, deps, survey_project, session_was_alive)
                landed[identity_now] = status
                landed_total += status in ("ok", "cookie")
                _write_meta(survey_project, item, status)
                project.append_seen(survey_project, {**row, "download": status})
                emit("download", hop=hop, identity=identity_now, key=item.key, status=status)
            cache.reset_stats()
            batch, frontier = frontier[: int(envelope["frontier"])], frontier[int(envelope["frontier"]):]
            fresh = errored = 0
            expanded_now = []
            abandoned_now = []
            #: This hop's judged candidates. They are kept or dropped together once the hop is
            #: over: how many may enter is the envelope's call and which of them is the score's,
            #: not the order the sources happened to answer in.
            pending: list[dict] = []
            #: The collection as it stood when the hop opened. Frozen for the whole hop, so two
            #: candidates of the same hop are scored against the same thing.
            collected_at_hop_start = set(collected)
            #: What the whole hop charged, per site, for the refund below: a warm cache over a
            #: batch refunds every row that hit it, not only the last one.
            charged_by: dict[str, int] = {}
            for row in batch:
                item = _row_item(row)
                # The axis cells are edges read from EACH work, so a row starts with its full
                # allowance every time; only the per-site caps are carried across the run.
                for axis in expansion.AXES:
                    counters[axis] = int(envelope[axis])
                attempts: dict[str, int] = {}
                pairs = []
                spent_before = cache.stats()["by"]
                try:
                    # Pulled eagerly: a failing axis must keep the candidates already handed
                    # back, and one unexpandable row must not take the rest of the hop with it.
                    for pair in expansion.expand(item, deps.sources(), envelope, counters,
                                                 attempts):
                        pairs.append(pair)
                except (LitcliError, KeyError, ValueError, TypeError) as e:
                    # KeyError is a 200 with no body, ValueError a 200 that is not JSON at all;
                    # both are how the backends report one unreadable answer.
                    errored += 1
                    identity_now = str(row.get("identity"))
                    # A row that failed was not really expanded, so the failure must not cost
                    # it: it goes back on the frontier for the next hop, and stays out of
                    # `expanded` so an interruption does not lose it either. A row the graph
                    # cannot resolve at all (a 404, an id the source does not know) gets one
                    # retry and is then written off into `abandoned`, or it would run the topic
                    # to `max_hops` alone. `errored` keeps either case off the streak below.
                    if identity_now not in retried or _again_later(e):
                        retried.add(identity_now)
                        frontier.append(row)
                    else:
                        abandoned_now.append(identity_now)
                    emit("error", hop=hop, identity=identity_now, reason=str(e))
                else:
                    # Nobody looked (every site was spent before this row's turn) is not the
                    # same as nothing found: the row stays unexpanded, or a resume skips it.
                    if pairs or attempts or not _askable(item, deps.sources()):
                        expanded_now.append(str(row.get("identity")))
                # One `references` call is several requests behind a batch, so the attempt
                # charge is only the floor: the site pays for what was really spent, on the
                # site the requests went to.
                row_charge = 0
                for site, made in attempts.items():
                    charged_by[site] = charged_by.get(site, 0) + made
                    row_charge += made
                spent_after = cache.stats()["by"]
                for site in sites:
                    over = max(0, int((spent_after.get(site) or {}).get("misses", 0))
                               - int((spent_before.get(site) or {}).get("misses", 0))
                               - attempts.get(site, 0))
                    if over:
                        counters[site] = int(counters.get(site, 0)) - over
                        charged_by[site] = charged_by.get(site, 0) + over
                        row_charge += over
                charged += row_charge
                for candidate, axis in pairs:
                    stamp([candidate], deps.by_name.get(candidate.source) or deps.sources()[0])
                    total, parts = scoring.score(candidate, survey_project,
                                                 collected_at_hop_start, trace)
                    candidate.extra["score"] = total
                    verdict = decide(candidate, survey_project, seen)
                    seen.add(verdict["identity"])
                    pending.append({
                        "identity": verdict["identity"], "admit": verdict["admit"],
                        "reason": verdict["reason"], "hop": hop, "axis": axis,
                        "source": candidate.source, "key": candidate.key,
                        "title": candidate.title, "doi": candidate.doi, "year": candidate.year,
                        "score": total, "parts": parts,
                        "referenced_works": candidate.extra.get("referenced_works") or [],
                        "item": candidate})
            # The hop is judged; now spend it. Best first, and only as many as the envelope
            # allows — a demoted row still gets its record and its reason, but no copy and no
            # place in the frontier.
            rank(pending, int(envelope["admit"]))
            for row in pending:
                item = row.pop("item")
                status = ""
                if row["admit"]:
                    fresh += 1
                    admitted_total += 1
                    # Admitted here, expanded by a later hop of THIS run; a kill before that
                    # hop leaves it unexpanded in `seen`, which resume picks up.
                    frontier.append({"identity": row["identity"], "key": row["key"],
                                     "doi": row["doi"], "source": row["source"],
                                     "title": row["title"], "year": row["year"]})
                    collected.update(str(w) for w in row["referenced_works"])
                    best = max(best, float(row["score"]))
                    if counters["publisher"] > 0:
                        counters["publisher"] -= 1
                        status = _download_kept(item, deps, survey_project, session_was_alive)
                    else:
                        status = "unavailable"
                    landed_total += status in ("ok", "cookie")
                    landed[row["identity"]] = status
                    _write_meta(survey_project, item, status)
                project.append_seen(survey_project, {**row, "download": status})
                emit("candidate", hop=hop, key=row["key"], doi=row["doi"],
                     title=row["title"], year=row["year"], source=row["source"],
                     score=row["score"], admit=row["admit"], reason=row["reason"])
                if row["admit"]:
                    emit("admit", hop=hop, identity=row["identity"], key=row["key"],
                         score=row["score"])
                    emit("download", hop=hop, identity=row["identity"],
                         key=row["key"], status=status)
            hits_by = cache.stats()["by"]
            # A cached answer costs no request, so it refunds its charge — never more than the
            # site was actually charged, or the quota would drift above its cap on a hot cache.
            refund = hits = 0
            for site, spent in charged_by.items():
                back = min(int((hits_by.get(site) or {}).get("hits", 0)), spent)
                hits += int((hits_by.get(site) or {}).get("hits", 0))
                counters[site] = int(counters.get(site, 0)) + back
                refund += back
            # A hop whose expansions errored (a 429 burst, an unreadable answer) found nothing
            # because it could not look, not because there was nothing: it must not spend the
            # saturation streak, or two rate-limited hops end a run that never got started.
            if best > high_before:
                streak = 0
            elif not errored:
                streak += 1
            emit("hop_end", hop=hop, new=fresh, expanded=expanded_now, abandoned=abandoned_now,
                 hits=hits, charged=charged, refund=refund, errored=errored,
                 remaining={**{site: counters.get(site, 0) for site in sites},
                            **{key: counters[key] for key in ("arxiv", "publisher")}})
            project.save(survey_project)
        emit("stop", reason=stop, admitted=admitted_total)
        return {"stop": stop, "admitted": admitted_total, "landed": landed_total}
    finally:
        for signum, handler in previous.items():
            with contextlib.suppress(ValueError):
                signal.signal(signum, handler)


def add_run_parser(p: argparse.ArgumentParser) -> None:
    """Register the arguments of ``survey run`` (also used by its tests)."""
    p.add_argument("project", help="project directory, or a name under the project root")
    p.add_argument("--effort", default=None, choices=effort.LEVELS,
                   help="reset the effort level before walking")
    p.add_argument("--format", default="jsonl", choices=("jsonl", "md"),
                   help="stdout events as JSONL (default) or one markdown table per hop")
    p.add_argument("--root", default=None, help="project root (default: the user data dir)")


def _project_dir(name: str, root: str | None) -> Path:
    """An existing path as given; otherwise a project name under the root."""
    path = Path(name).expanduser()
    if path.is_dir():
        return path
    return (Path(root).expanduser() if root else paths.project_root()) / name


def cmd_survey_run(cfg, args) -> int:
    """Take the lock, walk until a stop reason, and render; 1 when no copy landed at all."""
    directory = _project_dir(args.project, getattr(args, "root", None))
    try:
        survey_project = project.load(directory)
    except BadInput as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    if args.effort:
        survey_project.effort = args.effort
        project.save(survey_project)
    lock = survey_project.root / LOCK_NAME
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        print(f"[error] {survey_project.root} is already being walked "
              f"(remove {LOCK_NAME} if it is not)", file=sys.stderr)
        return 2
    try:
        with os.fdopen(handle, "w") as f:
            f.write(str(os.getpid()))
        try:
            deps = deps_from_config(cfg, survey_project)
        except BadInput as e:
            print(f"[error] {e}", file=sys.stderr)
            return 2
        deps.stream = sys.stdout if args.format == "jsonl" else None
        result = run_loop(survey_project, deps)
        if args.format == "md":
            sys.stdout.write(survey_events.render_md(project.read_trace(survey_project)))
        survey_project.status = "done" if result["stop"] == "saturated" else "stopped"
        project.save(survey_project)
        print(f"stop={result['stop']} admitted={result['admitted']}", file=sys.stderr)
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()
        deps.close()  # the engine is per run; a detached browser stays up behind it
    # `landed`, not "corpus/ is non-empty": every admitted candidate writes a metadata sidecar
    # there before its download is tried, so the directory says nothing about copies.
    return 0 if result["landed"] else 1


def add_status_parser(p: argparse.ArgumentParser) -> None:
    """Register the arguments of ``survey status`` (also used by its tests)."""
    p.add_argument("project", help="project directory, or a name under the project root")
    p.add_argument("--root", default=None, help="project root (default: the user data dir)")


def add_stop_parser(p: argparse.ArgumentParser) -> None:
    """Register the arguments of ``survey stop`` (also used by its tests)."""
    p.add_argument("project", help="project directory, or a name under the project root")
    p.add_argument("--root", default=None, help="project root (default: the user data dir)")


def _load_or_report(args) -> tuple[project.Project | None, int]:
    """The project, or ``(None, 2)`` with the reason already on stderr."""
    try:
        return project.load(_project_dir(args.project, getattr(args, "root", None))), 0
    except BadInput as e:
        print(f"[error] {e}", file=sys.stderr)
        return None, 2


def cmd_survey_status(cfg, args) -> int:
    """Report one project as JSON: question, status, hops, admitted, last stop, remaining."""
    survey_project, code = _load_or_report(args)
    if survey_project is None:
        return code
    trace = project.read_trace(survey_project)
    seen = project.read_seen(survey_project)
    envelope = effort.resolve(survey_project.effort)
    remaining = {key: int(envelope[f"{key}_cap"]) for key in ("arxiv", "publisher")}
    remaining.update({expansion._site(source): int(envelope["graph_cap"])
                      for source in graph_sources(cfg)})
    for event in reversed(trace):
        if event.get("event") == "hop_end" and isinstance(event.get("remaining"), dict):
            remaining.update({k: int(v) for k, v in event["remaining"].items()})
            break
    last_stop = next((str(e.get("reason") or "") for e in reversed(trace)
                      if e.get("event") == "stop"), "")
    report = {"question": survey_project.question, "status": survey_project.status,
              "effort": survey_project.effort,
              "hops": sum(1 for e in trace if e.get("event") == "hop_end"),
              "admitted": sum(1 for row in seen if row.get("admit")),
              "last_stop": last_stop, "remaining": remaining,
              "corpus": len(list((survey_project.root / "corpus").glob("*")))
              if (survey_project.root / "corpus").is_dir() else 0,
              "running": (survey_project.root / LOCK_NAME).exists()}
    print(json.dumps(report, ensure_ascii=False))
    return 0


def cmd_survey_stop(cfg, args) -> int:
    """Write the stop sentinel; the loop reads it at its next hop boundary."""
    survey_project, code = _load_or_report(args)
    if survey_project is None:
        return code
    sentinel = survey_project.root / STOP_NAME
    if sentinel.exists():
        print(f"already stopped: {survey_project.root} (the sentinel is waiting to be read)",
              file=sys.stderr)
        return 0
    sentinel.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    print(f"stop requested: {survey_project.root} — the walk ends at its next hop boundary")
    return 0
