#!/usr/bin/env python3
"""Build a small, reviewable context pack for an SDD task.

The repository is large enough that handing an AI assistant the whole tree is
both expensive and unsafe.  This tool treats ``.sdd`` markdown files as the
source of truth, drops superseded decisions, and then ranks source and test
files against the task text.  The output is deterministic apart from the
generation timestamp, so it is suitable for a handoff or a CI check.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


ACTIVE_STATUSES = {"active", "current", "accepted", "approved"}
INACTIVE_STATUSES = {"deprecated", "expired", "rejected", "superseded", "obsolete"}
TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".ipp", ".py", ".rs",
    ".sh", ".sql", ".test", ".md", ".rst", ".txt", ".yaml", ".yml", ".json",
}
SKIP_PARTS = {".git", "build", "build_debug", "cmake-build-debug", "node_modules"}


def _split_value(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
        value = value[1:-1]
    return [item.strip().strip("'\"") for item in value.split(",") if item.strip()]


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Parse the deliberately small YAML subset used by SDD markdown files."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}, text
    metadata: dict[str, object] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in {"paths", "tests", "tags", "scope", "supersedes"}:
            metadata[key] = _split_value(value)
        else:
            metadata[key] = value.strip("'\"")
    return metadata, "\n".join(lines[end + 1 :]).lstrip("\n")


@dataclass
class Document:
    path: Path
    metadata: dict[str, object]
    body: str
    kind: str
    score: int = 0

    @property
    def doc_id(self) -> str:
        return str(self.metadata.get("id", self.path.stem))

    @property
    def status(self) -> str:
        return str(self.metadata.get("status", "active")).lower()

    @property
    def active(self) -> bool:
        if self.status in INACTIVE_STATUSES or self.status not in ACTIVE_STATUSES:
            return False
        until = str(self.metadata.get("valid_until", "")).strip()
        if until:
            try:
                return dt.date.today() <= dt.date.fromisoformat(until)
            except ValueError:
                return False
        if self.metadata.get("superseded_by"):
            return False
        return True

    @property
    def paths(self) -> list[str]:
        value = self.metadata.get("paths", [])
        return value if isinstance(value, list) else _split_value(str(value))

    @property
    def tests(self) -> list[str]:
        value = self.metadata.get("tests", [])
        return value if isinstance(value, list) else _split_value(str(value))

    @property
    def summary(self) -> str:
        summary = str(self.metadata.get("summary", "")).strip()
        if summary:
            return summary
        for line in self.body.splitlines():
            if line.strip() and not line.startswith("#"):
                return line.strip()
        return ""


def _kind(path: Path, metadata: dict[str, object]) -> str:
    declared = str(metadata.get("kind", "")).strip().lower()
    if declared:
        return declared
    name = path.as_posix().lower()
    if "/decision" in name:
        return "decision"
    if "/spec" in name:
        return "spec"
    if "/task" in name:
        return "task"
    if "/handoff" in name:
        return "handoff"
    return "context"


def load_documents(root: Path) -> list[Document]:
    sdd_root = root / ".sdd"
    if not sdd_root.exists():
        return []
    documents: list[Document] = []
    for path in sorted(sdd_root.rglob("*.md")):
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        documents.append(Document(path.relative_to(root), metadata, body, _kind(path, metadata)))
    return documents


def task_tokens(task: str) -> list[str]:
    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", task.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            tokens.append(word)
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            tokens.extend(part for part in re.split(r"[_-]+", word) if part)
    return sorted(set(token for token in tokens if len(token) > 1 or token.isascii()))


def _contains(path: str, token: str) -> int:
    return path.lower().count(token.lower())


def rank_documents(documents: Iterable[Document], task: str) -> list[Document]:
    tokens = task_tokens(task)
    ranked: list[Document] = []
    for document in documents:
        if not document.active:
            continue
        haystack = f"{document.path} {document.summary} {document.body}".lower()
        document.score = sum(_contains(str(document.path), token) * 8 for token in tokens)
        document.score += sum(haystack.count(token) for token in tokens)
        document.score += {"spec": 12, "decision": 9, "task": 7, "handoff": 5}.get(document.kind, 1)
        if document.score:
            ranked.append(document)
    return sorted(ranked, key=lambda doc: (-doc.score, doc.path.as_posix()))


def _iter_code_files(root: Path) -> Iterable[Path]:
    for base in ("src", "include", "tools", "unittest", "test", "tests", "docs"):
        directory = root / base
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_PARTS or part.startswith(".") for part in path.relative_to(root).parts):
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            yield path


def rank_code_files(root: Path, task: str, documents: Sequence[Document], max_files: int) -> tuple[list[dict[str, object]], list[str]]:
    tokens = task_tokens(task)
    explicit_paths = [
        pattern
        for document in documents
        if document.active
        for pattern in (document.paths + document.tests)
    ]
    scored: list[tuple[int, Path, str]] = []
    for path in _iter_code_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        score = sum(_contains(relative, token) * 10 for token in tokens)
        # Repeated boilerplate (for example generated RPC headers) must not
        # drown out a small, task-specific file. Cap each token contribution.
        score += sum(min(text.lower().count(token), 20) for token in tokens)
        explicit = any(fnmatch.fnmatch(relative, pattern) for pattern in explicit_paths)
        if explicit:
            score += 30
        elif explicit_paths:
            # Active SDD artifacts are the authoritative boundary. Once they
            # name evidence paths, do not widen the pack with weak keyword
            # matches from an unrelated subsystem in a large repository.
            continue
        path_hits = sum(1 for token in tokens if token in relative.lower())
        content_hits = sum(1 for token in tokens if token in text.lower())
        # A single generic word in a filename (for example ``context`` in a
        # transport helper) is not enough evidence. Require either two path
        # terms or two independent terms in the file body.
        if not explicit and path_hits < 2 and content_hits < 2:
            continue
        lower = relative.lower()
        if any(part in lower for part in ("test", "unittest", ".test", "_test")):
            score += 3
        if "generated by" in text[:500].lower():
            score = min(score, 4)
        if score:
            scored.append((score, path, text))
    scored.sort(key=lambda item: (-item[0], item[1].as_posix()))
    selected: list[dict[str, object]] = []
    tests: list[str] = []
    for score, path, text in scored[:max_files]:
        relative = path.relative_to(root).as_posix()
        lines = text.splitlines()
        selected.append({"path": relative, "score": score, "lines": len(lines), "excerpt": "\n".join(lines[:20])})
        if "test" in relative.lower() or "/t/" in relative.lower():
            tests.append(relative)
    return selected, tests


def git_value(root: Path, args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _document_json(root: Path, document: Document) -> dict[str, object]:
    return {
        "path": document.path.as_posix(),
        "id": document.doc_id,
        "kind": document.kind,
        "status": document.status,
        "score": document.score,
        "summary": document.summary,
        "paths": document.paths,
        "tests": document.tests,
    }


def build_pack(root: Path, task: str, budget: int, max_files: int, handoff: bool = False) -> dict[str, object]:
    all_documents = load_documents(root)
    documents = rank_documents(all_documents, task)
    code_files, discovered_tests = rank_code_files(root, task, documents, max_files)
    tests = sorted(set(discovered_tests + [test for document in documents for test in document.tests]))
    omissions: list[str] = []
    selected_documents: list[dict[str, object]] = []
    used = 0
    for document in documents:
        item = _document_json(root, document)
        item_size = len(json.dumps(item, ensure_ascii=False))
        if used + item_size > budget:
            omissions.append(document.path.as_posix())
            continue
        selected_documents.append(item)
        used += item_size
    pack: dict[str, object] = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "repository": root.name,
        "revision": git_value(root, ["rev-parse", "HEAD"]),
        "task": task,
        "budget_chars": budget,
        "documents": selected_documents,
        "files": code_files,
        "tests": tests,
        "omissions": omissions,
    }
    if handoff:
        pack["handoff"] = {
            "resume_from": git_value(root, ["branch", "--show-current"]),
            "working_tree": git_value(root, ["status", "--short"]) or "clean",
            "next_step": "Review the active documents, run the listed tests, then regenerate this pack after changes.",
        }
    return pack


def render_markdown(pack: dict[str, object]) -> str:
    lines = [
        "# SDD Context Pack",
        "",
        f"- Task: {pack['task']}",
        f"- Repository: `{pack['repository']}` @ `{pack['revision']}`",
        f"- Generated: {pack['generated_at']}",
        "",
        "## Active specifications and decisions",
        "",
    ]
    documents = pack.get("documents", [])
    if documents:
        for document in documents:
            lines.append(f"- **{document['kind']}** `{document['path']}` — {document['summary'] or 'no summary'} (score {document['score']})")
    else:
        lines.append("- No active SDD documents matched; add a task or spec before coding.")
    lines += ["", "## Related code and tests", ""]
    for item in pack.get("files", []):
        lines.append(f"- `{item['path']}` (score {item['score']}, {item['lines']} lines)")
    if pack.get("tests"):
        lines += ["", "Tests to run:", ""]
        lines.extend(f"- `{path}`" for path in pack["tests"])
    if pack.get("omissions"):
        lines += ["", "## Omitted for budget", ""]
        lines.extend(f"- `{path}`" for path in pack["omissions"])
    if pack.get("handoff"):
        handoff = pack["handoff"]
        lines += ["", "## Handoff", "", f"- Branch: `{handoff['resume_from']}`", f"- Working tree: `{handoff['working_tree']}`", f"- Next step: {handoff['next_step']}"]
    lines += ["", "## Contract", "", "Only documents with an active status are included. Superseded or expired decisions must not be used without an explicit review.", ""]
    return "\n".join(lines)


def check_repository(root: Path) -> list[str]:
    errors: list[str] = []
    documents = load_documents(root)
    ids: dict[str, Path] = {}
    for document in documents:
        if document.doc_id in ids:
            errors.append(f"duplicate id {document.doc_id}: {ids[document.doc_id]} and {document.path}")
        ids[document.doc_id] = document.path
        if "status" not in document.metadata:
            errors.append(f"missing status: {document.path}")
        until = document.metadata.get("valid_until")
        if until:
            try:
                dt.date.fromisoformat(str(until))
            except ValueError:
                errors.append(f"invalid valid_until on {document.path}: {until}")
        for pattern in document.paths + document.tests:
            if not any(root.glob(pattern)):
                errors.append(f"missing referenced path {pattern} in {document.path}")
        for superseded in document.metadata.get("supersedes", []) if isinstance(document.metadata.get("supersedes", []), list) else []:
            if superseded not in ids and not any(other.doc_id == superseded for other in documents):
                errors.append(f"unknown superseded document {superseded} in {document.path}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pack", "handoff", "check"))
    parser.add_argument("--task", default="", help="task description used for ranking")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--budget", type=int, default=12000)
    parser.add_argument("--max-files", type=int, default=12)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "check":
        errors = check_repository(root)
        if errors:
            print("SDD check failed:")
            print("\n".join(f"- {error}" for error in errors))
            return 1
        print(f"SDD check passed ({len(load_documents(root))} documents)")
        return 0
    if not args.task.strip():
        parser.error("--task is required for pack and handoff")
    pack = build_pack(root, args.task, max(1, args.budget), max(1, args.max_files), handoff=args.command == "handoff")
    rendered = json.dumps(pack, ensure_ascii=False, indent=2) if args.format == "json" else render_markdown(pack)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
