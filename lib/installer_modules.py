"""
FreedomNAS Installer — Base infrastructure.

Provides Logger, DialogManager, Configuration, and subprocess helpers
used by all installer modules.

All subprocess calls use shell=False. dialog output is captured from
stderr (where dialog writes the user's selection by default).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class Logger:
    """Colored console logger that also writes to /var/log/freedomnas-install.log."""

    _RED    = "\033[0;31m"
    _GREEN  = "\033[0;32m"
    _YELLOW = "\033[1;33m"
    _NC     = "\033[0m"

    _logger: Optional[logging.Logger] = None

    @classmethod
    def _get(cls) -> logging.Logger:
        if cls._logger is not None:
            return cls._logger

        cls._logger = logging.getLogger("freedomnas-installer")
        cls._logger.setLevel(logging.DEBUG)

        # File handler — try /var/log first, fall back to tempfile
        for path in ("/var/log/freedomnas-install.log", None):
            try:
                fh = logging.FileHandler(
                    path or tempfile.mkstemp(suffix=".log", prefix="fnas-install-")[1]
                )
                fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
                cls._logger.addHandler(fh)
                break
            except (OSError, PermissionError):
                continue

        return cls._logger

    @classmethod
    def info(cls, message: str) -> None:
        print(f"{cls._GREEN}[INFO]{cls._NC} {message}")
        cls._get().info(message)

    @classmethod
    def warn(cls, message: str) -> None:
        print(f"{cls._YELLOW}[WARN]{cls._NC} {message}")
        cls._get().warning(message)

    @classmethod
    def error(cls, message: str) -> None:
        print(f"{cls._RED}[ERROR]{cls._NC} {message}", file=sys.stderr)
        cls._get().error(message)


# ---------------------------------------------------------------------------
# DialogManager
# ---------------------------------------------------------------------------

class DialogManager:
    """
    TUI dialogs via `dialog` (preferred) or `whiptail` (fallback).

    dialog writes the user's selection to stderr, so all methods read from
    stderr. The display is drawn directly on /dev/tty, so stdin/stdout are
    unaffected.
    """

    _backend: Optional[str] = None

    @classmethod
    def _get_backend(cls) -> str:
        if cls._backend:
            return cls._backend
        for prog in ("dialog", "whiptail"):
            r = subprocess.run(["which", prog], capture_output=True)
            if r.returncode == 0:
                cls._backend = prog
                return prog
        raise RuntimeError("Neither dialog nor whiptail found on PATH")

    @classmethod
    def _run(cls, extra_args: List[str], capture: bool = False) -> Tuple[int, str]:
        """
        Run dialog with the given arguments.
        Returns (returncode, stderr_text).
        stderr_text is non-empty only when capture=True.
        """
        backend = cls._get_backend()
        cmd = [backend] + extra_args
        if capture:
            fd, tmp = tempfile.mkstemp()
            os.close(fd)
            try:
                with open(tmp, "w") as fh:
                    result = subprocess.run(cmd, stderr=fh)
                with open(tmp) as fh:
                    text = fh.read().strip()
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return result.returncode, text
        else:
            result = subprocess.run(cmd)
            return result.returncode, ""

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    @classmethod
    def msgbox(cls, title: str, text: str, height: int = 10, width: int = 70) -> None:
        cls._run(["--title", title, "--msgbox", text, str(height), str(width)])

    @classmethod
    def yesno(cls, title: str, text: str, height: int = 10, width: int = 70) -> bool:
        rc, _ = cls._run(["--title", title, "--yesno", text, str(height), str(width)])
        return rc == 0

    @classmethod
    def defaultno(cls, title: str, text: str, height: int = 10, width: int = 70) -> bool:
        rc, _ = cls._run(
            ["--title", title, "--defaultno", "--yesno", text, str(height), str(width)]
        )
        return rc == 0

    @classmethod
    def menu(
        cls,
        title: str,
        text: str,
        items: List[Tuple[str, str]],
        height: int = 20,
        width: int = 70,
        list_height: int = 12,
    ) -> Optional[str]:
        """Show a menu. Returns the selected tag string, or None if cancelled."""
        flat: List[str] = []
        for tag, label in items:
            flat += [tag, label]
        rc, choice = cls._run(
            ["--title", title, "--menu", text,
             str(height), str(width), str(list_height)] + flat,
            capture=True,
        )
        return choice if rc == 0 else None

    @classmethod
    def inputbox(
        cls,
        title: str,
        text: str,
        init_text: str = "",
        height: int = 10,
        width: int = 70,
    ) -> Optional[str]:
        """Show a text input box. Returns the entered string, or None if cancelled."""
        args = ["--title", title, "--inputbox", text, str(height), str(width)]
        if init_text:
            args.append(init_text)
        rc, value = cls._run(args, capture=True)
        return value if rc == 0 else None

    @classmethod
    def passwordbox(
        cls, title: str, text: str, height: int = 10, width: int = 70
    ) -> Optional[str]:
        """Show a password input box (characters are hidden). Returns the password or None."""
        rc, value = cls._run(
            ["--title", title, "--insecure", "--passwordbox", text,
             str(height), str(width)],
            capture=True,
        )
        return value if rc == 0 else None

    @classmethod
    def gauge(cls, title: str, text: str, percent: int, height: int = 7, width: int = 70) -> None:
        """Show a progress gauge (non-interactive, informational)."""
        cls._run(
            ["--title", title, "--gauge", text, str(height), str(width), str(percent)]
        )


# ---------------------------------------------------------------------------
# Installation configuration state
# ---------------------------------------------------------------------------

class Configuration:
    """Mutable state bag shared across all installer modules."""

    def __init__(self) -> None:
        # OS / edition
        self.os_edition: Optional[str] = None  # "debian" | "arch" | "freebsd"

        # Boot
        self.boot_mode: Optional[str] = None  # "UEFI" | "BIOS"

        # Disk layout (set by DiskInstaller)
        self.disk: Optional[str] = None            # e.g. /dev/sda
        self.disk_by_id: Optional[str] = None      # /dev/disk/by-id/...
        self.part1: Optional[str] = None           # BIOS-boot or ESP
        self.part2: Optional[str] = None           # EFI (UEFI mode only)
        self.part3: Optional[str] = None           # bpool partition
        self.part4: Optional[str] = None           # rpool partition

        # ZFS
        self.pool_name: str = "rpool"
        self.bpool_name: str = "bpool"

        # System
        self.hostname: str = "freedomnas"
        self.domain: str = "local"
        self.timezone: str = "UTC"
        self.debian_mirror: str = "http://deb.debian.org/debian"

        # Primary admin user
        self.username: str = "admin"
        self.user_password: Optional[str] = None

        # Extra users added during install
        self.additional_users: List[dict] = []


# ---------------------------------------------------------------------------
# Subprocess helpers — shell=False only
# ---------------------------------------------------------------------------

def check_root() -> None:
    """Abort if not running as root."""
    if os.geteuid() != 0:
        Logger.error("This installer must be run as root.")
        sys.exit(1)


def run_command(args: List[str], check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    """
    Run a command without a shell.

    Args:
        args:    Command + arguments as a list.
        check:   Raise CalledProcessError on non-zero exit.
        timeout: Seconds before killing the process (default 5 min).

    Returns:
        CompletedProcess with stdout/stderr captured as text.
    """
    Logger.info(f"RUN: {' '.join(args)}")
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        Logger.error(f"Command failed (exit {exc.returncode}): {' '.join(args)}")
        if exc.stderr:
            Logger.error(exc.stderr.strip())
        raise
    except subprocess.TimeoutExpired:
        Logger.error(f"Command timed out after {timeout}s: {' '.join(args)}")
        raise


def run_in_chroot(args: List[str], chroot_dir: str = "/mnt", check: bool = True) -> subprocess.CompletedProcess:
    """
    Run a command inside a chroot environment.

    Wraps the command as: chroot <chroot_dir> <args...>
    """
    return run_command(["chroot", chroot_dir] + args, check=check)


def write_file(path: str, content: str, mode: int = 0o644) -> None:
    """Write a file, creating parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    p.chmod(mode)


def directory_exists(path: str) -> bool:
    return Path(path).is_dir()


def file_exists(path: str) -> bool:
    return Path(path).is_file()
