"""
FreedomNAS Installer — what the machine looked like when something failed.

A failed step is cleaned up right after (mounts undone, pool exported), and
with it goes the state that would explain the failure. collect() runs first:
the traceback, then every command in the host profile's `diagnostics`
section (processes, what still runs inside the target, mounts, pools, kernel
messages, free space), each with its output, into the installer log, on
screen, and into a file of its own beside the log.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Optional

from installer_modules import Logger

_DIAGNOSTICS_DIR = Path("/var/log")
_COMMAND_TIMEOUT = 30


def collect(ctx, reason: str, error: Optional[BaseException] = None) -> Optional[Path]:
    """Gather the diagnostics for `reason`; returns the file they were saved to."""
    if ctx.dry_run:
        return None
    sections = [("Reason", reason)]
    if error is not None:
        sections.append(("Traceback", "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ).rstrip()))

    commands = ctx.host.profile.get("diagnostics", {})
    if not commands:
        sections.append(("Diagnostics", "the host profile declares no diagnostics commands"))
    verbose, Logger.verbose = Logger.verbose, False
    try:
        for name in commands:
            try:
                out, err = ctx.host_run(
                    "diagnostics", name, check=False, timeout=_COMMAND_TIMEOUT,
                    root=ctx.target_root,
                )
                body = "\n".join(part for part in (out, err) if part) or "(no output)"
            except Exception as exc:
                body = f"(could not run: {exc})"
            sections.append((name, body))
    finally:
        Logger.verbose = verbose

    text = "".join(f"=== {title} ===\n{body}\n\n" for title, body in sections)
    for title, body in sections:
        Logger.section(f"diagnostics: {title}", body)

    path = _DIAGNOSTICS_DIR / f"freedomnas-install-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    try:
        path.write_text(text)
    except OSError as exc:
        Logger.warn(f"Diagnostics not saved to {path}: {exc}")
        return None
    Logger.warn(f"Diagnostics saved to {path}")
    return path
