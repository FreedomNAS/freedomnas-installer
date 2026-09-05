"""
FreedomNAS Installer — deploying FreedomNAS itself into the target.

This is the step that makes the installer follow the same philosophy as the
rest of the project: it does not "install a Debian and then some scripts",
it installs *the core* — engine, profiles, WebUI — into the target using the
exact layout ARCHITECTURE.md defines and freedomnas-image-builder's
common/install-core.sh produces:

    /usr/lib/freedomnas/core/freedomnas/    engine, config, reconciler, ...
    /usr/lib/freedomnas/profiles/           the JSON profiles
    /usr/lib/freedomnas/webui/              FastAPI app + bin/
    /usr/lib/python3/dist-packages/freedomnas-core.pth

The source is whatever this machine already has: the installed packages
(/usr/lib/freedomnas/...) when the installer runs from its .deb, or the
sibling repo checkouts in a development tree. Copying is plain file I/O —
no rsync dependency, nothing to translate per OS.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, Tuple

from context import ExecutionContext
from installer_modules import Logger, Configuration

_INSTALL_ROOT = "/usr/lib/freedomnas"
_PTH_PATH = "/usr/lib/python3/dist-packages/freedomnas-core.pth"

_CORE_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", "tests", "conftest.py", ".gitignore",
    "debian", "PKGBUILD", "*.egg-info",
)
_PROFILES_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "debian", "PKGBUILD", "*.md", ".gitignore", ".forgejo",
)
_WEBUI_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".gitignore", "debian", "PKGBUILD",
)


class CoreInstaller:
    """Copies freedomnas-core/profiles/webui into the target and wires first boot."""

    def __init__(self, config: Configuration, ctx: ExecutionContext) -> None:
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def install_freedomnas(self) -> None:
        """Deploy core, profiles and webui into the target root."""
        if self.ctx.target_root == "/":
            Logger.info("In-place mode — FreedomNAS is already installed on this system")
            return

        core_src, profiles_src, webui_src = self._sources()

        Logger.info(f"Installing freedomnas-core from {core_src}")
        self._copy(core_src, self.ctx.target_path(f"{_INSTALL_ROOT}/core/freedomnas"), _CORE_IGNORE)

        Logger.info(f"Installing freedomnas-profiles from {profiles_src}")
        self._copy(profiles_src, self.ctx.target_path(f"{_INSTALL_ROOT}/profiles"), _PROFILES_IGNORE)

        if webui_src is not None:
            Logger.info(f"Installing freedomnas-webui from {webui_src}")
            self._copy(webui_src, self.ctx.target_path(f"{_INSTALL_ROOT}/webui"), _WEBUI_IGNORE)
        else:
            Logger.warn("freedomnas-webui not found — the WebUI will have to be installed later")

        self._write_pth()
        Logger.info("FreedomNAS installed into the target")

    def install_firstboot(self) -> None:
        """
        Deploy the boot-time entry point and its supervisor, then register
        the cron @reboot hook declared by the profile.

        firstboot.sh is the ADR-002 decision point — import pools, unlock
        encrypted ones, reconcile if a .freedomnas dataset exists, otherwise
        let the WebUI's first-run wizard take over — and is shared, verbatim,
        with the images freedomnas-image-builder produces.
        """
        assets = self.ctx.assets_dir
        bin_dir = self.ctx.ensure_target_dir(f"{_INSTALL_ROOT}/webui/bin")

        for script in ("firstboot.sh", "supervisor.sh"):
            source = assets / script
            if not source.is_file():
                raise RuntimeError(f"Missing asset: {source}")
            Logger.command(f"[file] {bin_dir / script}")
            if not self.ctx.dry_run:
                shutil.copy2(source, bin_dir / script)
                (bin_dir / script).chmod(0o755)

        self.ctx.write_target_file("webui_cron")
        Logger.info("First-boot service wired (cron @reboot -> firstboot.sh)")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sources(self) -> Tuple[Path, Path, Optional[Path]]:
        """
        Resolve where core/profiles/webui are on this machine: the installed
        layout first, the sibling development checkouts second.
        """
        repo_parent = Path(__file__).resolve().parent.parent.parent
        installed = Path(_INSTALL_ROOT)

        core = installed / "core" / "freedomnas"
        if not core.is_dir():
            core = repo_parent / "freedomnas-core"

        profiles = self.ctx.profiles

        webui: Optional[Path] = installed / "webui"
        if not webui.is_dir():
            webui = repo_parent / "freedomnas-webui"
        if not webui.is_dir():
            webui = None

        if not core.is_dir():
            raise RuntimeError(
                "freedomnas-core not found on this system — install the "
                "freedomnas-core package before running the installer"
            )
        return core, profiles, webui

    def _copy(self, source: Path, dest: Path, ignore) -> None:
        Logger.command(f"[file] {source} -> {dest}")
        if self.ctx.dry_run:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, ignore=ignore, dirs_exist_ok=True, symlinks=True)

    def _write_pth(self) -> None:
        """Put /usr/lib/freedomnas/core on the target's Python path."""
        pth = self.ctx.target_path(_PTH_PATH)
        Logger.command(f"[file] {pth}")
        if self.ctx.dry_run:
            return
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(f"{_INSTALL_ROOT}/core\n")
