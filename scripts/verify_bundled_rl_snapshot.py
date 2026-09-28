#!/usr/bin/env python3
"""Capture and verify the dirty worktree used by the bundled RL migration."""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = 1
EXCLUDED_CAPTURE_NAMES = frozenset(
    {
        ".agents/logs/bundled-rl-default-strategy-baseline-files.tar",
        ".agents/logs/bundled-rl-default-strategy-worktree-baseline.json",
        ".agents/logs/bundled-rl-default-strategy-retained-releases.json",
    }
)


class SnapshotError(RuntimeError):
    """Raised when a snapshot contract is violated."""


@dataclass(frozen=True)
class Entry:
    """Stable metadata for one filesystem entry."""

    path: str
    kind: str
    size: int | None = None
    sha256: str | None = None
    target: str | None = None
    children: tuple[str, ...] | None = None

    def to_json(self) -> dict[str, Any]:
        """Return JSON-ready metadata without null optional fields."""
        value: dict[str, Any] = {"path": self.path, "type": self.kind}
        for key in ("size", "sha256", "target", "children"):
            field = getattr(self, key)
            if field is not None:
                value[key] = list(field) if key == "children" else field
        return value


def fail(message: str, *, details: Any = None, code: int = 2) -> NoReturn:
    """Emit one bounded JSON failure and exit."""
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details is not None:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    raise SystemExit(code)


def emit(payload: dict[str, Any]) -> None:
    """Emit one stable JSON result."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run_git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    """Run Git without invoking a shell."""
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if result.returncode != 0:
        raise SnapshotError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout if binary else result.stdout.decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """Return a lowercase SHA-256 digest."""
    return hashlib.sha256(data).hexdigest()


def repo_relative(repo: Path, path: Path) -> str:
    """Return a normalized repository-relative POSIX path."""
    try:
        relative = path.relative_to(repo)
    except ValueError as error:
        raise SnapshotError(f"path is outside repository: {path}") from error
    return relative.as_posix()


def validate_relative_path(value: str) -> str:
    """Validate a repository-relative path without resolving symlinks."""
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise SnapshotError(f"unsafe repository path: {value!r}")
    return candidate.as_posix()


def entry_for(repo: Path, relative: str) -> Entry:
    """Read one path using lstat semantics."""
    relative = validate_relative_path(relative)
    path = repo / relative
    if path.is_symlink():
        return Entry(relative, "symlink", target=os.readlink(path))
    if path.is_file():
        data = path.read_bytes()
        return Entry(relative, "file", size=len(data), sha256=sha256_bytes(data))
    if path.is_dir():
        children = tuple(sorted(child.name for child in path.iterdir()))
        return Entry(relative, "directory", children=children)
    raise SnapshotError(f"unsupported or missing path: {relative}")


def porcelain_paths(repo: Path) -> set[str]:
    """Return tracked dirty and untracked leaf paths from porcelain v1."""
    raw = run_git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        binary=True,
    )
    assert isinstance(raw, bytes)
    fields = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        text = field.decode("utf-8", "surrogateescape")
        if len(text) < 4:
            raise SnapshotError("malformed git porcelain entry")
        status = text[:2]
        path = validate_relative_path(text[3:])
        paths.add(path)
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise SnapshotError("malformed renamed git porcelain entry")
            old_path = validate_relative_path(
                fields[index].decode("utf-8", "surrogateescape")
            )
            index += 1
            paths.add(old_path)
    return paths


def add_glob_paths(repo: Path, paths: set[str], patterns: list[str]) -> None:
    """Add matching ignored planning artifacts without following symlinks."""
    for pattern in patterns:
        for match in repo.glob(pattern):
            if match.is_dir() and not match.is_symlink():
                for child in match.rglob("*"):
                    if child.is_file() or child.is_symlink():
                        paths.add(repo_relative(repo, child))
            elif match.is_file() or match.is_symlink():
                paths.add(repo_relative(repo, match))


def parent_directories(paths: set[str]) -> set[str]:
    """Return existing ancestor directories for captured leaves."""
    parents: set[str] = set()
    for value in paths:
        parent = Path(value).parent
        while parent != Path("."):
            parents.add(parent.as_posix())
            parent = parent.parent
    return parents


def head_bytes(repo: Path, relative: str) -> bytes | None:
    """Return the HEAD blob for a path, or None for an untracked path."""
    result = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def change_tokens(before: bytes, after: bytes) -> Counter[str]:
    """Build a conservative multiset fingerprint of line-level changes."""
    before_lines = before.decode("utf-8", "surrogateescape").splitlines()
    after_lines = after.decode("utf-8", "surrogateescape").splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    tokens: Counter[str] = Counter()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = "\n".join(before_lines[i1:i2]).encode("utf-8", "surrogateescape")
        added = "\n".join(after_lines[j1:j2]).encode("utf-8", "surrogateescape")
        token = f"{tag}:{sha256_bytes(removed)}:{sha256_bytes(added)}"
        tokens[token] += 1
    return tokens


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write canonical JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_name = handle.name
    os.replace(temp_name, path)


def write_archive(repo: Path, archive: Path, leaves: list[str]) -> None:
    """Create a deterministic tar containing baseline regular files/symlinks."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=archive.parent, delete=False) as handle:
        temp_name = handle.name
    try:
        with tarfile.open(temp_name, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for relative in sorted(leaves):
                path = repo / relative
                if not path.exists() and not path.is_symlink():
                    continue
                info = tar.gettarinfo(str(path), arcname=relative)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if info.isfile():
                    with path.open("rb") as source:
                        tar.addfile(info, source)
                else:
                    tar.addfile(info)
        os.replace(temp_name, archive)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def walk_tree(repo: Path, root_relative: str) -> list[Entry]:
    """Capture an exact tree without following symlinked directories."""
    root = repo / validate_relative_path(root_relative)
    if not root.is_dir():
        raise SnapshotError(f"retained root is not a directory: {root_relative}")
    paths = [root, *sorted(root.rglob("*"))]
    return [entry_for(repo, repo_relative(repo, path)) for path in paths]


def command_capture(args: argparse.Namespace) -> None:
    """Capture the pre-implementation dirty worktree and retained releases."""
    repo = Path(args.repo).resolve()
    script_relative = repo_relative(repo, Path(__file__).resolve())
    actual_self = sha256_bytes(Path(__file__).read_bytes())
    if actual_self != args.self_sha256:
        raise SnapshotError("verifier self SHA-256 does not match")
    paths = porcelain_paths(repo)
    add_glob_paths(repo, paths, args.include_glob)
    paths.add(script_relative)
    paths.difference_update(EXCLUDED_CAPTURE_NAMES)
    leaves = sorted(
        path for path in paths if (repo / path).is_file() or (repo / path).is_symlink()
    )
    directories = sorted(parent_directories(set(leaves)))
    entries = [entry_for(repo, path) for path in [*directories, *leaves]]
    hunk_tokens: dict[str, dict[str, int]] = {}
    for relative in leaves:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            continue
        before = head_bytes(repo, relative) or b""
        tokens = change_tokens(before, path.read_bytes())
        if tokens:
            hunk_tokens[relative] = dict(sorted(tokens.items()))
    archive = repo / validate_relative_path(args.archive)
    snapshot = repo / validate_relative_path(args.snapshot)
    retained_snapshot = repo / validate_relative_path(args.retained_snapshot)
    write_archive(repo, archive, leaves)
    retained = walk_tree(repo, args.retained_root)
    snapshot_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "repo": str(repo),
        "self": {"path": script_relative, "sha256": actual_self},
        "archive": validate_relative_path(args.archive),
        "ownership": validate_relative_path(args.ownership),
        "entries": [entry.to_json() for entry in entries],
        "dirtyHunkTokens": hunk_tokens,
    }
    retained_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "root": validate_relative_path(args.retained_root),
        "entries": [entry.to_json() for entry in retained],
    }
    atomic_json(snapshot, snapshot_payload)
    atomic_json(retained_snapshot, retained_payload)
    emit(
        {
            "ok": True,
            "command": "capture",
            "leaves": len(leaves),
            "directories": len(directories),
            "retainedEntries": len(retained),
            "artifacts": [args.archive, args.snapshot, args.retained_snapshot],
        }
    )


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object or fail closed."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SnapshotError(f"cannot load {path}: {error}") from error
    if not isinstance(value, dict):
        raise SnapshotError(f"JSON root must be an object: {path}")
    return value


def owned_paths(ownership: dict[str, Any], key: str) -> set[str]:
    """Collect exact paths from every owner for one permission class."""
    owners = ownership.get("permissions")
    if not isinstance(owners, dict):
        raise SnapshotError("ownership permissions must be an object")
    result: set[str] = set()
    for owner, spec in owners.items():
        if not isinstance(owner, str) or not isinstance(spec, dict):
            raise SnapshotError("invalid ownership entry")
        values = spec.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(item, str) for item in values
        ):
            raise SnapshotError(f"invalid {key} for {owner}")
        result.update(validate_relative_path(item) for item in values)
    return result


def current_entry(repo: Path, relative: str) -> Entry | None:
    """Return current metadata, including broken symlinks, or None."""
    path = repo / relative
    if not path.exists() and not path.is_symlink():
        return None
    return entry_for(repo, relative)


def archive_bytes(archive: Path, relative: str) -> bytes:
    """Read one regular-file member from the baseline archive."""
    with tarfile.open(archive, mode="r") as tar:
        member = tar.getmember(relative)
        handle = tar.extractfile(member)
        if handle is None:
            raise SnapshotError(f"baseline archive member is not a file: {relative}")
        return handle.read()


def pattern_matches(path: str, patterns: list[str]) -> bool:
    """Match an exact path or explicit glob."""
    return any(
        path == pattern or fnmatch.fnmatchcase(path, pattern) for pattern in patterns
    )


def command_verify(args: argparse.Namespace) -> None:
    """Verify ownership, baseline preservation, and dirty hunk retention."""
    repo = Path.cwd().resolve()
    snapshot = load_json(repo / args.snapshot)
    ownership = load_json(repo / args.ownership)
    baseline_entries = {
        item["path"]: item
        for item in snapshot.get("entries", [])
        if item["type"] != "directory"
    }
    allowed_modify = owned_paths(ownership, "allowed_modify")
    allowed_create = owned_paths(ownership, "allowed_create")
    allowed_delete = owned_paths(ownership, "allowed_delete")
    violations: list[str] = []
    for relative, expected in baseline_entries.items():
        actual = current_entry(repo, relative)
        if actual is None:
            if relative not in allowed_delete:
                violations.append(f"unexpected deletion: {relative}")
            continue
        if actual.to_json() != expected and relative not in allowed_modify:
            violations.append(f"unowned baseline modification: {relative}")
    current_dirty = porcelain_paths(repo)
    baseline_paths = set(baseline_entries)
    for relative in sorted(current_dirty - baseline_paths):
        if relative in EXCLUDED_CAPTURE_NAMES:
            continue
        if relative not in allowed_create and relative not in allowed_modify:
            violations.append(f"unowned new change: {relative}")
    policies = ownership.get("baseline_hunks", {})
    if not isinstance(policies, dict):
        raise SnapshotError("baseline_hunks must be an object")
    archive = repo / snapshot["archive"]
    token_map = snapshot.get("dirtyHunkTokens", {})
    for relative, baseline_tokens in token_map.items():
        if relative not in allowed_modify and relative not in allowed_delete:
            continue
        policy = policies.get(relative)
        if policy not in {
            "preserve_hunks",
            "preserve_hunks_with_release_pointer",
            "replace_obsolete_native_hunks",
            "replace_integrator_tool",
            "delete_obsolete_native_file",
        }:
            violations.append(f"unclassified dirty hunk path: {relative}")
            continue
        if policy not in {"preserve_hunks", "preserve_hunks_with_release_pointer"}:
            continue
        path = repo / relative
        if not path.is_file():
            violations.append(f"preserve_hunks path missing: {relative}")
            continue
        before = head_bytes(repo, relative) or b""
        current_tokens = change_tokens(before, path.read_bytes())
        required_tokens = Counter(baseline_tokens)
        if policy == "preserve_hunks_with_release_pointer":
            pointer_map = ownership.get("baseline_release_pointers", {})
            old_release_id = pointer_map.get(relative)
            new_release_id = args.expected_new_release_id
            if not isinstance(old_release_id, str) or not new_release_id:
                violations.append(f"release pointer identity missing: {relative}")
                continue
            baseline_bytes = archive_bytes(archive, relative)
            old_bytes = old_release_id.encode("ascii")
            if old_bytes not in baseline_bytes:
                violations.append(f"baseline release pointer missing: {relative}")
                continue
            adjusted = baseline_bytes.replace(old_bytes, new_release_id.encode("ascii"))
            required_tokens = change_tokens(before, adjusted)
        missing = required_tokens - current_tokens
        if missing:
            violations.append(f"pre-existing hunks disappeared: {relative}")
    patch_dir = repo / ".agents/logs/bundled-rl-snapshot-patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_file = patch_dir / f"{args.phase}.patch"
    chunks: list[str] = []
    for relative in sorted(set(baseline_entries) & allowed_modify):
        path = repo / relative
        if not path.is_file() or baseline_entries[relative]["type"] != "file":
            continue
        before = (
            archive_bytes(archive, relative).decode("utf-8", "replace").splitlines(True)
        )
        after = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        chunks.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"baseline/{relative}",
                tofile=f"current/{relative}",
            )
        )
    patch_file.write_text("".join(chunks), encoding="utf-8")
    if violations:
        fail("snapshot verification failed", details=violations)
    emit(
        {
            "ok": True,
            "command": "verify",
            "phase": args.phase,
            "owner": args.owner,
            "patch": repo_relative(repo, patch_file),
            "violations": [],
        }
    )


def compare_entry_maps(
    expected: list[dict[str, Any]], actual: list[Entry]
) -> list[str]:
    """Compare exact tree metadata maps."""
    expected_map = {entry["path"]: entry for entry in expected}
    actual_map = {entry.path: entry.to_json() for entry in actual}
    issues: list[str] = []
    for path in sorted(set(expected_map) | set(actual_map)):
        if expected_map.get(path) != actual_map.get(path):
            issues.append(path)
    return issues


def command_verify_retained(args: argparse.Namespace) -> None:
    """Verify retained release entries and optionally exactly one new ID."""
    repo = Path.cwd().resolve()
    snapshot = load_json(repo / args.snapshot)
    root = validate_relative_path(args.releases)
    actual = walk_tree(repo, root)
    expected_entries = snapshot.get("entries", [])
    expected_ids = {
        Path(entry["path"]).name
        for entry in expected_entries
        if entry["type"] == "directory"
        and Path(entry["path"]).parent.as_posix() == root
    }
    actual_ids = {
        child.name
        for child in (repo / root).iterdir()
        if child.is_dir() and not child.is_symlink()
    }
    new_ids = actual_ids - expected_ids
    expected_for_compare = expected_entries
    if args.expected_new_id:
        if new_ids != {args.expected_new_id}:
            fail(
                "retained release verification failed",
                details=[f"unexpected new release IDs: {sorted(new_ids)}"],
            )
        expected_for_compare = [
            {
                **entry,
                "children": sorted([*entry["children"], args.expected_new_id]),
            }
            if entry["path"] == root and entry["type"] == "directory"
            else entry
            for entry in expected_entries
        ]
    elif new_ids:
        fail(
            "retained release verification failed",
            details=[f"unexpected new release IDs: {sorted(new_ids)}"],
        )
    expected_paths = {entry["path"] for entry in expected_for_compare}
    filtered = [entry for entry in actual if entry.path in expected_paths]
    issues = compare_entry_maps(expected_for_compare, filtered)
    if issues:
        fail("retained release verification failed", details=issues)
    emit({"ok": True, "command": "verify-retained", "phase": args.phase})


def command_active_residue(args: argparse.Namespace) -> None:
    """Reject obsolete active contract strings outside excluded trees."""
    repo = Path(args.repo).resolve()
    excludes = [validate_relative_path(value).rstrip("/") for value in args.exclude]
    forbidden = list(args.forbid)
    if args.forbid_native_source_fields:
        forbidden.extend(["sourceHash", '"source"'])
    findings: list[dict[str, Any]] = []
    for path in repo.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = repo_relative(repo, path)
        if relative.startswith(".git/") or any(
            relative == prefix or relative.startswith(prefix + "/")
            for prefix in excludes
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for needle in forbidden:
            if needle in text:
                findings.append({"path": relative, "needle": needle})
    if findings:
        fail("obsolete active contract residue found", details=findings)
    emit({"ok": True, "command": "active-residue", "findings": []})


def parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--repo", required=True)
    capture.add_argument("--archive", required=True)
    capture.add_argument("--snapshot", required=True)
    capture.add_argument("--retained-root", required=True)
    capture.add_argument("--retained-snapshot", required=True)
    capture.add_argument("--ownership", required=True)
    capture.add_argument("--self-sha256", required=True)
    capture.add_argument(
        "--include-glob", action="append", default=[".agents/logs/codex/*bundled-rl*"]
    )
    capture.set_defaults(function=command_capture)
    verify = commands.add_parser("verify")
    verify.add_argument("--phase", required=True)
    verify.add_argument("--snapshot", required=True)
    verify.add_argument("--archive", required=True)
    verify.add_argument("--ownership", required=True)
    verify.add_argument("--owner")
    verify.add_argument("--all-owners", action="store_true")
    verify.add_argument("--expected-new-release-id")
    verify.set_defaults(function=command_verify)
    retained = commands.add_parser("verify-retained")
    retained.add_argument("--phase", required=True)
    retained.add_argument("--snapshot", required=True)
    retained.add_argument("--releases", required=True)
    retained.add_argument("--expected-new-id")
    retained.set_defaults(function=command_verify_retained)
    residue = commands.add_parser("active-residue")
    residue.add_argument("--repo", required=True)
    residue.add_argument("--exclude", action="append", default=[])
    residue.add_argument("--forbid", action="append", default=[])
    residue.add_argument("--forbid-native-source-fields", action="store_true")
    residue.set_defaults(function=command_active_residue)
    return root


def main() -> None:
    """Run the selected snapshot command."""
    args = parser().parse_args()
    try:
        args.function(args)
    except SnapshotError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
