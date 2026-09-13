"""Status-only containment check for Bash bootstraps (Python >= 3.9).

Usage: python3 -I path_safety.py ABSOLUTE_ROOT ABSOLUTE_CANDIDATE
No stdout. Only ENOENT permits a missing component. This is a preflight check,
not protection against concurrent renames.

Exit codes:
  0  strictly inside the root
  1  root itself or outside
  2  invalid input, unsupported runtime, or resolution failure
"""

from __future__ import annotations

import errno
import os
import stat
import sys

MAX_SYMLINKS = 40


def resolve_missing(path: str) -> str:
    """Resolve links component by component without swallowing filesystem errors."""
    if not os.path.isabs(path) or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError("absolute paths without control characters are required")
    pending = path.split("/")[1:]
    resolved: list[str] = []
    traversals = 0
    while pending:
        component = pending.pop(0)
        if component in ("", "."):
            continue
        if component == "..":
            resolved = resolved[:-1]
            continue
        candidate = "/" + "/".join([*resolved, component])
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            resolved.append(component)
            continue
        if stat.S_ISLNK(metadata.st_mode):
            traversals += 1
            if traversals > MAX_SYMLINKS:
                raise OSError(errno.ELOOP, "symlink traversal limit exceeded")
            target = os.readlink(candidate)
            if any(ord(c) < 32 or ord(c) == 127 for c in target):
                raise ValueError("symlink target contains control characters")
            if os.path.isabs(target):
                resolved = []
            pending = target.split("/") + pending
            continue
        if pending and not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "non-directory path component")
        resolved.append(component)
    return "/" + "/".join(resolved)


def main(args: list[str]) -> int:
    if sys.version_info < (3, 9) or len(args) != 2:
        return 2
    try:
        root, candidate = (resolve_missing(path) for path in args)
        if not stat.S_ISDIR(os.stat(root).st_mode):
            return 2
        if candidate == root or os.path.commonpath([root, candidate]) != root:
            return 1
        return 0
    except (OSError, ValueError):
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
