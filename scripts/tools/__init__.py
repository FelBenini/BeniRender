"""Shared utilities for BeniRender scripts."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "ansible" / "inventory.yml"


def run(command: list[str], *, timeout: int | None = None) -> str:
    """Run a command with live output; raise if it fails."""
    print(f"\n$ {shlex.join(command)}", flush=True)

    result = subprocess.run(
        command,
        text=True,
        stdout=None,
        stderr=None,
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{command[0]}"
        )

    return ""


def capture(command: list[str], *, timeout: int | None = None) -> str:
    """Run a command and return its captured stdout."""
    print(f"\n$ {shlex.join(command)}", flush=True)

    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{command[0]}"
        )

    return result.stdout.strip()


def require_executables(*executables: str) -> None:
    """Raise if any executable is missing from PATH."""
    for executable in executables:
        if not shutil.which(executable):
            raise RuntimeError(
                f"Required executable not found: {executable}"
            )
