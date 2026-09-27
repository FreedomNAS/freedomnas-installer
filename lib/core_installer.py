"""
FreedomNAS Installer — deploying FreedomNAS itself into the target.

This is the step that makes the installer follow the same philosophy as the
rest of the project: it does not "install a Debian and then some scripts",
it installs *the core* — engine, profiles, WebUI — into the target using the
exact layout ARCHITECTURE.md defines and freedomnas-image-builder's
common/install-core.sh produces:

    <install_root>/core/freedomnas/    engine, config, reconciler, ...
    <install_root>/profiles/           the JSON profiles
    <install_root>/webui/              FastAPI app + bin/
    <install_root>/docs/               the documentation, built, for the WebUI
    <site-packages>/freedomnas-core.pth  wherever the target's python3 looks

<install_root> is the target edition's, from its OS profile's `paths`:
/usr/lib/freedomnas on Linux, /usr/local/lib/freedomnas on FreeBSD.

The source is whatever this machine already has: the installed packages
(this machine's own <install_root>) when the installer runs installed, or the
sibling repo checkouts in a development tree. Copying is plain file I/O —
no rsync dependency, nothing to translate per OS.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, Tuple

import corelink
from context import ExecutionContext
from installer_modules import Logger, Configuration

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
    """Copies freedomnas-core/profiles/webui/docs into the target and wires first boot."""

    def __init__(self, config: Configuration, ctx: ExecutionContext) -> None:
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def install_freedomnas(self) -> None:
        """Deploy core, profiles, webui and docs into the target root."""
        if self.ctx.target_root == "/":
            self._check_existing_install()
            return

        # An edition that packages FreedomNAS has already installed it with
        # everything else, from its own repository: copying the live image's
        # copy over the packaged one would leave apt owning files that no
        # longer match what it installed.
        if self.ctx.meta("core_packages"):
            self._check_existing_install()
            return

        core_src, profiles_src, webui_src, docs_src = self._sources()

        root = self.ctx.install_root

        Logger.info(f"Installing freedomnas-core from {core_src}")
        self._copy(core_src, self.ctx.target_path(f"{root}/core/freedomnas"), _CORE_IGNORE)

        Logger.info(f"Installing freedomnas-profiles from {profiles_src}")
        self._copy(profiles_src, self.ctx.target_path(f"{root}/profiles"), _PROFILES_IGNORE)

        if webui_src is not None:
            Logger.info(f"Installing freedomnas-webui from {webui_src}")
            self._copy(webui_src, self.ctx.target_path(f"{root}/webui"), _WEBUI_IGNORE)
        else:
            Logger.warn("freedomnas-webui not found — the WebUI will have to be installed later")

        if docs_src is not None:
            Logger.info(f"Installing freedomnas-docs from {docs_src}")
            self._copy(docs_src, self.ctx.target_path(f"{root}/docs"), None)
        else:
            Logger.warn("freedomnas-docs not found — the WebUI will have no offline documentation")

        self._write_pth()
        Logger.info("FreedomNAS installed into the target")

    def _check_existing_install(self) -> None:
        """
        Nothing to copy: FreedomNAS is expected to be there already, from the
        distribution's packages. That is the case in-place — the running
        system is one someone already installed the packages on — and on an
        edition whose `installer_meta.core_packages` put them in with the
        rest. Say which piece is missing instead of leaving the WebUI to fail
        at boot with an import error nobody can trace back to here.

        The core is checked by importing it rather than by looking for a
        directory: that is what proves the .pth file is in place too.
        """
        in_place = self.ctx.target_root == "/"
        root = self.ctx.install_root
        expected = {
            "freedomnas-profiles": Path(self.ctx.profiles) if in_place
            else self.ctx.target_path(f"{root}/profiles"),
            "freedomnas-webui": self.ctx.target_path(f"{root}/webui/api/main.py"),
            "freedomnas-docs": self.ctx.target_path(f"{root}/docs/index.html"),
        }
        missing = [name for name, path in expected.items() if not path.exists()]
        if not self._core_importable():
            missing.insert(0, "freedomnas-core")

        if not missing:
            Logger.info("FreedomNAS comes from the distribution's packages — nothing to copy")
            return

        Logger.warn("Not installed on this system: " + ", ".join(missing))
        Logger.warn("Install those packages too, or the WebUI will not start at boot")

    def _core_importable(self) -> bool:
        """True when the target's own python3 finds freedomnas."""
        if self.ctx.dry_run:
            return True
        if not self.ctx.has("python", "import_check"):
            # No way to ask: fall back to the layout the installer copies.
            return self.ctx.target_path(f"{self.ctx.install_root}/core/freedomnas").is_dir()
        stdout, _ = self.ctx.target_run("python", "import_check", check=False)
        return bool(stdout.strip())

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
        bin_dir = self.ctx.ensure_target_dir(f"{self.ctx.install_root}/webui/bin")

        for script in ("firstboot.sh", "supervisor.sh"):
            source = assets / script
            if not source.is_file():
                raise RuntimeError(f"Missing asset: {source}")
            Logger.command(f"[file] {bin_dir / script}")
            if not self.ctx.dry_run:
                shutil.copy2(source, bin_dir / script)
                (bin_dir / script).chmod(0o755)

        self.ctx.write_target_file("webui_cron")
        # Both install modes come through here, which is why the snapshot
        # schedule is written here too: an in-place install writes only the
        # profile files marked `inplace`, and would otherwise get the WebUI
        # without the snapshots.
        if self.ctx.has("files", "scheduled_jobs"):
            self.ctx.write_target_file("scheduled_jobs")
        Logger.info("First-boot service wired (cron @reboot -> firstboot.sh)")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sources(self) -> Tuple[Path, Path, Optional[Path], Optional[Path]]:
        """
        Resolve where core/profiles/webui/docs are on this machine: the
        installed layout first, the sibling development checkouts second. A
        docs checkout counts only once it is built (`mkdocs build` writes
        freedomnas-docs/site).

        A candidate counts only if it holds the file that proves it is a real
        copy, not just a directory: removing a package leaves its
        __pycache__ directories behind, and copying that skeleton gives a
        target whose WebUI cannot import api.main at boot.
        """
        repo_parent = Path(__file__).resolve().parent.parent.parent
        installed = corelink.INSTALL_ROOT

        core = installed / "core" / "freedomnas"
        if not (core / "__init__.py").is_file():
            core = repo_parent / "freedomnas-core"

        profiles = self.ctx.profiles

        webui: Optional[Path] = installed / "webui"
        if not (webui / "api" / "main.py").is_file():
            webui = repo_parent / "freedomnas-webui"
        if not (webui / "api" / "main.py").is_file():
            webui = None

        docs: Optional[Path] = installed / "docs"
        if not (docs / "index.html").is_file():
            docs = repo_parent / "freedomnas-docs" / "site"
        if not (docs / "index.html").is_file():
            docs = None

        if not (core / "__init__.py").is_file():
            raise RuntimeError(
                "freedomnas-core not found on this system — install the "
                "freedomnas-core package before running the installer"
            )
        return core, profiles, webui, docs

    def _copy(self, source: Path, dest: Path, ignore) -> None:
        Logger.command(f"[file] {source} -> {dest}")
        if self.ctx.dry_run:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, ignore=ignore, dirs_exist_ok=True, symlinks=True)

    def _write_pth(self) -> None:
        """
        Put <install_root>/core on the target's Python path, for running
        `python3 -m freedomnas` by hand.

        The directory is asked of the target's own interpreter
        (`python.site_dir`): it is /usr/lib/python3/dist-packages only on
        Debian, and elsewhere it carries the Python version, so an upgrade
        can leave this file behind. Nothing FreedomNAS starts depends on it —
        firstboot.sh, supervisor.sh and the scheduled jobs set PYTHONPATH.
        """
        if not self.ctx.has("python", "site_dir"):
            return
        try:
            stdout, _ = self.ctx.target_run("python", "site_dir", timeout=60)
        except Exception as exc:
            Logger.warn(f"Could not ask the target's python3 for its site-packages: {exc}")
            return
        site_dir = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        if not site_dir:
            if not self.ctx.dry_run:
                Logger.warn("The target's python3 reported no site-packages directory")
            return
        pth = self.ctx.target_path(f"{site_dir}/freedomnas-core.pth")
        Logger.command(f"[file] {pth}")
        if self.ctx.dry_run:
            return
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(f"{self.ctx.install_root}/core\n")
