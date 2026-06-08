"""Agent identity primitives: path canonicalization, ULIDs, timestamps.

The MCP server cannot derive the calling agent's working directory from its
own process state (inside a plugin-launched stdio server, ``os.getcwd()`` is
the plugin install dir, and the Copilot CLI advertises no ``roots`` capability
— see ``docs/spike-roots.md``). Every agent-scoped tool therefore takes an
explicit ``agent_cwd: str`` which is canonicalized here and looked up in
``agent_aliases``.

Identity invariant (per ``docs/PLAN.md``): **one canonical directory == one
agent.** Symlink resolution is intentional: a OneDrive-symlinked path and its
real target collapse to the same canonical path and therefore the same agent.
Canonicalization is best-effort on Windows (``subst`` drives, UNC aliases, and
extended-length prefixes may still diverge); ``agent_add_alias`` exists to
merge any residual path variants onto a single agent.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

# Upper bound on an accepted path length. Guards against a pathological caller
# bloating the DB / tool responses with an enormous ``agent_cwd``. Comfortably
# above any real repo root (Windows MAX_PATH is 260; extended-length is 32767).
_MAX_PATH_LEN = 4096


def canonicalize_agent_cwd(path: str, *, require_exists: bool = True) -> str:
    """Canonicalize an agent-supplied working directory for alias storage.

    Pipeline: validate non-empty + absolute -> resolve (symlinks, ``..``) ->
    normalize case (Windows) -> forward-slash separators -> strip a trailing
    separator unless the path is a filesystem anchor (e.g. ``c:/``).

    When ``require_exists`` is True (the default) the path must already exist
    and be a directory: resolving a non-existent path would silently mint a
    phantom agent identity from a typo. Lookups that resolve an *already
    stored* alias (e.g. the source side of :func:`add_alias` after a repo has
    moved and the old path is gone) pass ``require_exists=False`` — they cannot
    mint a phantom because the canonical result must still match an existing
    ``agent_aliases`` row.

    Args:
        path: Absolute filesystem path the agent considers its root (typically
            ``git rev-parse --show-toplevel``).
        require_exists: Require the path to exist and be a directory. Use the
            default for establishing identity; pass False only to canonicalize
            a stored alias key for lookup.

    Returns:
        The canonical alias-path string stored in ``agent_aliases.alias_path``.

    Raises:
        ValueError: If ``path`` is empty, relative, too long, unresolvable, or
            (when ``require_exists``) missing or not a directory.
    """
    if not path or not path.strip():
        raise ValueError("agent_cwd must be a non-empty path.")

    if len(path) > _MAX_PATH_LEN:
        raise ValueError(
            f"agent_cwd is too long ({len(path)} chars, max {_MAX_PATH_LEN})."
        )

    # Reject relative paths up front: Path.resolve() would resolve them against
    # the server's own cwd (the plugin install dir), producing a wrong identity.
    if not os.path.isabs(path):
        raise ValueError(
            f"agent_cwd must be an absolute path, got: {path!r}. "
            f"Pass your repository root (e.g. `git rev-parse --show-toplevel`)."
        )

    # resolve()/exists()/is_dir() touch the filesystem and can raise OSError on
    # unreachable UNC shares, permission errors, or malformed paths; surface a
    # clean validation error instead of leaking the OS-level traceback.
    try:
        resolved = Path(path).resolve()
        if require_exists:
            exists = resolved.exists()
            is_dir = resolved.is_dir() if exists else False
        else:
            exists = is_dir = True  # not checked in lookup mode
    except OSError as exc:
        raise ValueError(f"agent_cwd could not be resolved: {path!r} ({exc}).") from exc

    if require_exists:
        if not exists:
            raise ValueError(
                f"agent_cwd does not exist: {path!r} (resolved to {resolved}). "
                f"Pass an existing repository root."
            )
        if not is_dir:
            raise ValueError(f"agent_cwd must be a directory, got a file: {path!r}.")

    canonical = os.path.normcase(str(resolved)).replace("\\", "/")
    anchor = os.path.normcase(resolved.anchor).replace("\\", "/")

    # Strip a trailing separator only when it is not the bare filesystem anchor
    # (``c:/`` must keep its slash; ``c:/repos/x/`` becomes ``c:/repos/x``).
    if canonical != anchor and canonical.endswith("/"):
        canonical = canonical.rstrip("/")

    return canonical


def new_ulid() -> str:
    """Return a new ULID string used as an internal agent primary key."""
    return str(ULID())


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()
