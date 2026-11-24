"""``litcli survey new|run|status|stop`` — the survey lifecycle.

``new`` is the only command that sets constraints; it writes the project and its seeds and
hands back nothing but the project's name, so a caller (an agent) gets an address, not a
report. Without ``--seed`` it runs one opening search and takes the first candidates as the
initial cluster; an opening search that finds nothing is an error rather than an empty survey,
because a survey with no seed has nowhere to walk from.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import effort, paths, project
from ..aggregate import identity
from ..config import get_all_backends
from ..errors import BadInput
from ..models import AcademicItem
from ..provenance import stamp

#: Candidates the opening search may supply when the user named no seeds.
OPENING_SEEDS = 3


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
