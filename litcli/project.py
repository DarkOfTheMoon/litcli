"""One survey project on disk: a question, its seeds, and an append-only history.

A survey is long-running — it is resumed tomorrow, by another command, after the process that
started it is gone — so every piece of it is a file under its own directory: the question and
its settings as ``project.toml``, every candidate ever judged in ``seen.jsonl``, every step in
``trace.jsonl`` and the downloaded copies in ``corpus/``. The two histories are append-only
JSONL because a survey only ever adds to them; a torn last line is reported with its file and
line number rather than silently dropped.

Every write lands through a sibling ``.tmp`` file and :func:`os.replace`, which is atomic on
the same filesystem: an interrupted write leaves the previous content intact, never a half
line.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .errors import BadInput
from .paths import safe_filename

#: How many leading words of the question name the directory.
_SLUG_WORDS = 6
#: The admission score a candidate needs unless the project says otherwise.
DEFAULT_THRESHOLD = 2.5
DEFAULT_EFFORT = "medium"

#: TOML key order, fixed so two saves of the same project diff cleanly.
_FIELDS = ("question", "seeds", "year_from", "year_to", "container", "type_weight",
           "threshold", "effort", "status", "created")


@dataclass
class Project:
    """A survey question and the settings the loop reads on every step."""

    root: Path
    question: str
    seeds: list[str] = field(default_factory=list)
    year_from: str = ""
    year_to: str = ""
    container: str = ""
    type_weight: dict[str, float] = field(default_factory=dict)
    threshold: float = DEFAULT_THRESHOLD
    effort: str = DEFAULT_EFFORT
    status: str = "running"
    created: str = ""

    @property
    def seen_file(self) -> Path:
        return self.root / "seen.jsonl"

    @property
    def trace_file(self) -> Path:
        return self.root / "trace.jsonl"


def slugify(question: str) -> str:
    """The directory name for ``question``: its first words, made file-name safe."""
    words = [w for w in (question or "").replace("　", " ").split() if w.strip("?!.,;:()[]")]
    if not words:
        return "survey"
    stem = "_".join("".join(ch for ch in w if ch.isalnum() or ch in "_-") for w in words[:_SLUG_WORDS])
    return safe_filename(stem, "").rstrip(".") or "survey"


def _toml_value(value) -> str:
    """One value as TOML; JSON string quoting is also valid TOML basic-string quoting."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_value(k)} = {_toml_value(v)}"
                                for k, v in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` through a sibling temp file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def create(root: Path, question: str, **fields) -> Project:
    """Make a new project under ``root``; a taken name gets ``-2``, ``-3``, …"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = slugify(question)
    target, n = root / base, 1
    while target.exists():
        n += 1
        target = root / f"{base}-{n}"
    target.mkdir()
    project = Project(root=target, question=question,
                      created=datetime.now(UTC).isoformat(timespec="seconds"), **fields)
    save(project)
    return project


def save(project: Project) -> None:
    """Write ``project.toml`` atomically."""
    lines = [f"{key} = {_toml_value(getattr(project, key))}" for key in _FIELDS]
    _write_atomic(project.root / "project.toml", "\n".join(lines) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    """Parse an append-only record file, naming the file and line of a torn row."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise BadInput(f"{path}:{number}: not a JSON record ({e.msg})") from e
    return records


def load(root: Path) -> Project:
    """Read a project and check both histories are intact.

    The check is part of loading on purpose: every step reads the project first, so a torn
    file is caught before a loop runs on top of half an answer.
    """
    root = Path(root)
    path = root / "project.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise BadInput(f"no survey project at {root} (missing {path.name})") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise BadInput(f"{path}: {e}") from e
    if not data.get("question"):
        raise BadInput(f"{path}: no question")
    project = Project(root=root, question=str(data["question"]))
    for key in ("seeds", "year_from", "year_to", "container", "type_weight", "effort", "status",
                "created"):
        if key in data:
            setattr(project, key, data[key])
    project.threshold = float(data.get("threshold", DEFAULT_THRESHOLD))
    _read_jsonl(project.seen_file)
    _read_jsonl(project.trace_file)
    return project


def _append(path: Path, record: dict) -> None:
    """Append one JSON line by rewriting the whole file atomically (the histories stay small)."""
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    _write_atomic(path, existing + json.dumps(record, ensure_ascii=False) + "\n")


def append_seen(project: Project, record: dict) -> None:
    """Record that a candidate was judged (whatever the verdict)."""
    _append(project.seen_file, record)


def append_trace(project: Project, record: dict) -> None:
    """Record one step of the loop."""
    _append(project.trace_file, record)


def read_seen(project: Project) -> list[dict]:
    return _read_jsonl(project.seen_file)


def read_trace(project: Project) -> list[dict]:
    return _read_jsonl(project.trace_file)


def corpus_dir(project: Project) -> Path:
    """The directory downloaded copies live in; created on demand."""
    path = project.root / "corpus"
    path.mkdir(parents=True, exist_ok=True)
    return path
