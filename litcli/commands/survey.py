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
from ..admission import decide
from ..aggregate import identity
from ..backends._entitled import EntitledBackend
from ..config import get_all_backends
from ..errors import BadInput, LitcliError
from ..models import AcademicItem
from ..pdf import download as download_bytes
from ..provenance import stamp

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
    """One search across the configured sources, first hits only."""
    found: list[AcademicItem] = []
    for name, backend in get_all_backends(cfg, corpora=True).items():
        try:
            found.extend(stamp(backend.search(question, limit=OPENING_SEEDS), backend))
        except Exception as e:  # one dead source must not sink the opening
            print(f"[skipped] {name}: {e}", file=sys.stderr)
        if len(found) >= OPENING_SEEDS:
            break
    return found[:OPENING_SEEDS]


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
    seeds = [s.strip() for s in args.seed if s.strip()]
    found: list[AcademicItem] = []
    if not seeds:
        found = _opening_seeds(cfg, question)
        if not found:
            print("[error] the opening search found nothing; pass --seed to start from a work "
                  "you already know", file=sys.stderr)
            return 2
        seeds = [item.doi or identity(item) for item in found]

    created = project.create(Path(args.root).expanduser() if args.root else paths.project_root(),
                             question, seeds=seeds, **fields)
    for item in found:
        record = {"identity": identity(item), "admit": True, "reason": "seed", "seed": "search",
                  "source": item.source, "key": item.key, "title": item.title,
                  "doi": item.doi, "year": item.year,
                  "score": 0.0, "parts": {}}
        project.append_seen(created, record)
    for seed in (() if found else seeds):
        project.append_seen(created, {"identity": _seed_identity(seed), "admit": True,
                                      "reason": "seed", "seed": "flag", "key": seed,
                                      "score": 0.0, "parts": {}})
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
    publishers: dict = field(default_factory=dict)
    session_alive: Callable[[], bool] = lambda: True
    stream: Any = None


def deps_from_config(cfg: dict, survey_project: project.Project) -> Deps:
    """The real dependencies: configured backends, the fulltext chain, the live session."""
    backends = get_all_backends(cfg)
    backend = backends.get("openalex")
    if backend is None:
        raise BadInput("a survey walks the citation graph, which only openalex supplies; "
                       "configure [backends.openalex]")
    publishers = {name: b for name, b in backends.items() if isinstance(b, EntitledBackend)}
    session = next((b.session for b in publishers.values() if b.session is not None), None)
    email = ((cfg.get("unpaywall") or {}).get("email") or os.environ.get("UNPAYWALL_EMAIL")
             or unpaywall.DEFAULT_EMAIL)

    def resolve(doi: str) -> dict:
        return fulltext.resolve_fulltext(doi, email, backends=backends, openalex_backend=backend,
                                         europepmc_backend=backends.get("europepmc"),
                                         zenodo_backend=backends.get("zenodo"),
                                         publishers=publishers,
                                         scihub_backend=backends.get("scihub"))

    return Deps(backend=backend, resolve=resolve, publishers=publishers,
                session_alive=session.alive if session is not None else (lambda: True))


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
    except (LitcliError, OSError, RuntimeError):
        return "unavailable"
    if not info or not info.get("has_fulltext"):
        return "unavailable"
    raw = info.get("data")
    if raw is None:
        url = str(info.get("url") or "")
        publisher = deps.publishers.get(str(info.get("kind") or ""))
        if publisher is None:
            publisher = next((b for b in deps.publishers.values()
                              if getattr(b, "matches_url", None) and b.matches_url(url)), None)
        try:
            raw = publisher.get_attachment(item.doi) if publisher is not None else (
                download_bytes(url) if url else None)
        except (LitcliError, OSError, RuntimeError):
            return "unavailable"
    fmt = str(info.get("format") or "pdf")
    if not raw or (fmt == "pdf" and not bytes(raw).startswith(b"%PDF")):
        return "unavailable"
    _write_file(project.corpus_dir(survey_project) / f"{paths.safe_filename(item.doi, '')}.{fmt}",
                bytes(raw))
    return "ok"


def _write_meta(survey_project: project.Project, item: AcademicItem, status: str) -> None:
    """Keep a metadata sidecar beside the copy — a missing copy must not lose the record."""
    record = {"identity": identity(item), "doi": item.doi, "key": item.key, "source": item.source,
              "title": item.title, "year": item.year, "access": getattr(item, "access", ""),
              "download": status}
    stem = paths.safe_filename(item.doi or identity(item), "")
    _write_file(project.corpus_dir(survey_project) / f"{stem}.json",
                json.dumps(record, ensure_ascii=False).encode("utf-8"))


def _download(item: AcademicItem, deps: Deps, survey_project: project.Project,
              session_was_alive: bool) -> str:
    """Download one admitted candidate, retrying through the cookie path if the session died."""
    if session_was_alive and deps.publishers and not deps.session_alive():
        _detach_sessions(deps.publishers)
        status = _land(item, deps, survey_project)
        return "cookie" if status == "ok" else status
    return _land(item, deps, survey_project)


def _frontier(seen_rows: list[dict], trace: list[dict]) -> tuple[list[dict], set[str]]:
    """Admitted works never expanded, and the identities of all expanded ones."""
    expanded = {str(work) for event in trace if event.get("event") == "hop_end"
                for work in event.get("expanded") or []}
    return [row for row in seen_rows
            if row.get("admit") and str(row.get("identity")) not in expanded], expanded


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
    collected = {str(work) for row in seen_rows if row.get("admit")
                 for work in row.get("referenced_works") or []}
    envelope = effort.resolve(survey_project.effort)
    counters = {key: int(envelope[key]) for key in ("refs", "citing", "related")}
    counters.update({key: int(envelope[f"{key}_cap"])
                     for key in ("openalex", "arxiv", "publisher")})
    caps = {"admit": int(envelope["admit"])}
    hop = sum(1 for event in trace if event.get("event") == "hop_end")
    best = max((float(row.get("score") or 0.0) for row in seen_rows if row.get("admit")),
               default=0.0)
    streak = admitted_total = 0
    interrupted = []

    def emit(event: str, **fields) -> None:
        project.append_trace(survey_project, {"event": event, **fields})
        if deps.stream is not None:
            print(survey_events.line(event, **fields), file=deps.stream)

    def on_sigint(*_args) -> None:
        interrupted.append(True)

    previous = None
    with contextlib.suppress(ValueError):  # not the main thread: no handler to install
        previous = signal.signal(signal.SIGINT, on_sigint)
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
            elif counters["openalex"] <= 0:
                stop = "budget"
            elif not frontier or streak >= int(envelope["streak"]):
                stop = "saturated"
            if stop:
                break
            hop += 1
            high_before = best
            session_was_alive = deps.session_alive()
            cache.reset_stats()
            batch, frontier = frontier[: int(envelope["frontier"])], frontier[int(envelope["frontier"]):]
            fresh = 0
            expanded_now = []
            for row in batch:
                expanded_now.append(str(row.get("identity")))
                item = AcademicItem(source=str(row.get("source") or "openalex"),
                                    key=str(row.get("key") or ""), doi=str(row.get("doi") or ""),
                                    title=str(row.get("title") or ""), year=row.get("year"),
                                    extra={"seed": row.get("seed")})
                for axis in expansion.AXES:
                    if counters[axis] > 0 and counters["openalex"] > 0:
                        counters["openalex"] -= 1
                for candidate, axis in expansion.expand(item, deps.backend, envelope, counters):
                    stamp([candidate], deps.backend)
                    total, parts = scoring.score(candidate, survey_project, collected, trace)
                    candidate.extra["score"] = total
                    verdict = decide(candidate, survey_project, seen, fresh, caps)
                    seen.add(verdict["identity"])
                    status = ""
                    if verdict["admit"]:
                        fresh += 1
                        admitted_total += 1
                        collected.update(str(w) for w in candidate.extra.get("referenced_works") or [])
                        if total > best:
                            best = total
                        if counters["publisher"] > 0:
                            counters["publisher"] -= 1
                            status = _download(candidate, deps, survey_project, session_was_alive)
                        else:
                            status = "unavailable"
                        _write_meta(survey_project, candidate, status)
                    project.append_seen(survey_project, {
                        "identity": verdict["identity"], "admit": verdict["admit"],
                        "reason": verdict["reason"], "hop": hop, "axis": axis,
                        "source": candidate.source, "key": candidate.key,
                        "title": candidate.title, "doi": candidate.doi, "year": candidate.year,
                        "score": total, "parts": parts,
                        "referenced_works": candidate.extra.get("referenced_works") or [],
                        "download": status})
                    emit("candidate", hop=hop, key=candidate.key, doi=candidate.doi,
                         title=candidate.title, year=candidate.year, source=candidate.source,
                         score=total, admit=verdict["admit"], reason=verdict["reason"])
                    if verdict["admit"]:
                        emit("admit", hop=hop, identity=verdict["identity"], key=candidate.key,
                             score=total)
                        emit("download", hop=hop, identity=verdict["identity"],
                             key=candidate.key, status=status)
            counters["openalex"] += cache.stats()["hits"]
            streak = 0 if best > high_before else streak + 1
            emit("hop_end", hop=hop, new=fresh, expanded=expanded_now,
                 remaining={key: counters[key] for key in ("openalex", "arxiv", "publisher")})
            project.save(survey_project)
        emit("stop", reason=stop, admitted=admitted_total)
        return {"stop": stop, "admitted": admitted_total}
    finally:
        if previous is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGINT, previous)


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
    corpus = survey_project.root / "corpus"
    return 0 if corpus.is_dir() and any(corpus.iterdir()) else 1
