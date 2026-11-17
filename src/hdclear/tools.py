"""Locating and running the external system tools hdclear relies on.

hdclear is usually started via ``sudo``, whose ``secure_path`` drops the
Homebrew prefix from PATH. So besides PATH we also search the classic sbin
directories and the Homebrew prefix this Python interpreter lives in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import HdclearError

_STANDARD_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def homebrew_prefix(executable: str | None = None) -> Path | None:
    """Return the Homebrew prefix if ``executable`` lives in a Homebrew Cellar."""
    exe = executable if executable is not None else sys.executable
    parts = Path(exe).parts
    if "Cellar" in parts:
        return Path(*parts[: parts.index("Cellar")])
    env = os.environ.get("HOMEBREW_PREFIX")
    return Path(env) if env else None


def search_path(executable: str | None = None) -> list[str]:
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    prefix = homebrew_prefix(executable)
    if prefix is not None:
        dirs += [str(prefix / "sbin"), str(prefix / "bin")]
        # e2fsprogs is keg-only on some platforms.
        dirs += [str(prefix / "opt" / "e2fsprogs" / "sbin")]
    dirs += list(_STANDARD_DIRS)
    seen: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def find_tool(name: str) -> str | None:
    return shutil.which(name, path=os.pathsep.join(search_path()))


def require_tool(name: str) -> str:
    path = find_tool(name)
    if path is None:
        raise HdclearError(
            f"required tool '{name}' not found (searched PATH, sbin dirs and the Homebrew prefix)"
        )
    return path


def run(
    args: list[str], *, input: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output. Raises HdclearError if ``check`` and it fails."""
    try:
        proc = subprocess.run(args, input=input, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise HdclearError(f"cannot run {args[0]}: {e}") from e
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise HdclearError(f"{' '.join(args)} failed (exit {proc.returncode}): {detail}")
    return proc
