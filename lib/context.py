"""
FreedomNAS Installer — execution context.

This is the single place where the installer talks to the OS. It owns two
engines:

  host   — freedomnas-core's engine for the system the installer is running
           on (the live/rescue environment, or the running NAS in in-place
           mode). Partitioning, pool creation and bootstrapping happen here.
  target — a non-singleton engine built with FreedomNASEngine.for_target(),
           bound to the edition being installed and, in clean-install mode,
           to the target root. Every command it runs is transparently
           prefixed with the chroot invocation from the profile's
           `chroot.exec_prefix`.

No installer module builds a command string of its own: it names a
(category, action) pair and this context resolves it through the right
engine. That is the Golden Rule, applied to installation.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import corelink
from installer_modules import Logger


class ExecutionContext:
    """Owns the host and target engines and all command execution."""

    def __init__(
        self,
        target_os: str,
        target_root: Optional[str] = None,
        host_os: Optional[str] = None,
        dry_run: bool = False,
        profile_dir: Optional[str] = None,
    ) -> None:
        engine_mod = corelink.engine_module()
        self._engine_cls = engine_mod.FreedomNASEngine
        self.profile_dir = str(profile_dir or corelink.find_profiles_dir())
        # Every step reads profiles through these, so --profiles points the
        # command maps, the package lists and the assets at the same tree.
        self.profiles = Path(self.profile_dir)
        self.packages_dir = self.profiles / "packages"
        self.assets_dir = self.profiles / "assets"
        self.dry_run = dry_run

        if host_os:
            # Testing/dry-run escape hatch: pretend the installer runs on
            # `host_os` instead of probing the running system.
            self.host = self._engine_cls.for_target(host_os, profile_dir=self.profile_dir)
        else:
            self.host = engine_mod.get_engine(self.profile_dir)

        self.host_os = self.host.os_type
        self.target_os = target_os
        self.target_root = target_root or "/"
        # Installing onto the running system means installing for the init it
        # actually runs, not the one the edition's profile defaults to.
        running_init = self.host.init_system if self.target_root == "/" and not host_os else None
        self.set_target_init(running_init)

    def set_target_init(self, init_system: Optional[str]) -> None:
        """
        (Re)build the target engines for an init system, or for the edition's
        default when None — the target's service commands and package lists
        both depend on it.
        """
        self.target = self._engine_cls.for_target(
            self.target_os,
            root=None if self.target_root == "/" else self.target_root,
            init_system=init_system,
            profile_dir=self.profile_dir,
        )
        # Same profile as `target`, executed on the host without a chroot:
        # bootstrapping is described by the target edition but performed from
        # outside it, because the target root is still empty at that point.
        self.staging = self._engine_cls.for_target(
            self.target_os, init_system=init_system, profile_dir=self.profile_dir
        )

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def host_run(
        self,
        category: str,
        action: str,
        timeout: int = 300,
        check: bool = True,
        **kwargs: Any,
    ) -> Tuple[str, str]:
        """Run a profile command on the machine the installer runs on."""
        return self._run(self.host, "host", category, action, timeout, check, **kwargs)

    def target_run(
        self,
        category: str,
        action: str,
        timeout: int = 300,
        check: bool = True,
        **kwargs: Any,
    ) -> Tuple[str, str]:
        """Run a profile command inside the target system (chroot in clean mode)."""
        return self._run(self.target, "target", category, action, timeout, check, **kwargs)

    def stage_run(
        self,
        category: str,
        action: str,
        timeout: int = 300,
        check: bool = True,
        **kwargs: Any,
    ) -> Tuple[str, str]:
        """
        Run a command described by the *target's* profile but executed on the
        host, outside any chroot — bootstrap, and nothing else.
        """
        return self._run(self.staging, "stage", category, action, timeout, check, **kwargs)

    def target_run_stdin(
        self,
        category: str,
        action: str,
        stdin_data: str,
        timeout: int = 60,
        **kwargs: Any,
    ) -> Tuple[str, str]:
        """
        Run a target command with data piped to stdin — used for passwords, so
        they never appear in argv or in the log.
        """
        command = self.target.get_command(category, action, **kwargs)
        Logger.command(f"[target] {command} (stdin: {len(stdin_data)} bytes, not logged)")
        if self.dry_run:
            return "", ""
        return self.target.execute_with_stdin(
            category, action, stdin_data, timeout=timeout, **kwargs
        )

    def _run(
        self,
        engine: Any,
        label: str,
        category: str,
        action: str,
        timeout: int,
        check: bool,
        /,
        **kwargs: Any,
    ) -> Tuple[str, str]:
        # Positional-only: template variables land in **kwargs even when one
        # of them is named `label`, `check` or `action` (bootloader's
        # efi_boot_entry takes a `label`).
        command = engine.get_command(category, action, **kwargs)
        prefix = f"{engine.chroot_prefix} " if engine.chroot_prefix else ""
        for one in [command] if isinstance(command, str) else command:
            Logger.command(f"[{label}] {prefix}{one}")

        if self.dry_run:
            return "", ""

        return engine.execute(category, action, timeout=timeout, check=check, **kwargs)

    # ------------------------------------------------------------------
    # Profile access
    # ------------------------------------------------------------------

    def has(self, category: str, action: str, target: bool = True) -> bool:
        """True if the (target or host) profile defines this command."""
        engine = self.target if target else self.host
        return action in engine.profile.get(category, {})

    def meta(self, key: str, default: Any = None) -> Any:
        """Read a value from the target profile's installer_meta section."""
        return self.target.profile.get("installer_meta", {}).get(key, default)

    @property
    def install_root(self) -> str:
        """Where the target edition keeps FreedomNAS itself (its OS profile's paths)."""
        return self.target.profile["paths"]["install_root"]

    @property
    def config_dir(self) -> str:
        """Where the target edition keeps FreedomNAS's configuration."""
        return self.target.profile["paths"]["config_dir"]

    def host_meta(self, key: str, default: Any = None) -> Any:
        """Read a value from the host profile's installer_meta section."""
        return self.host.profile.get("installer_meta", {}).get(key, default)

    def os_info(self, target: bool = True) -> Dict[str, str]:
        engine = self.target if target else self.host
        return engine.get_os_info()

    def bootstrap_methods(self) -> List[str]:
        """
        Bootstrap methods the target declares, filtered down to those whose
        host-side tool is actually present on this machine.
        """
        declared = self.meta("bootstrap_methods", []) or []
        tools = self.meta("bootstrap_tools", {}) or {}
        available = []
        for method in declared:
            tool = tools.get(method)
            if tool is None or shutil.which(tool):
                available.append(method)
            elif self.dry_run:
                # A dry run should still be able to walk any declared path,
                # even one whose tool this machine does not have installed.
                available.append(method)
        return available

    # ------------------------------------------------------------------
    # Target filesystem
    # ------------------------------------------------------------------

    def target_path(self, path: str) -> Path:
        """Map an absolute in-target path to its path on the running system."""
        if self.target_root == "/":
            return Path(path)
        return Path(self.target_root) / path.lstrip("/")

    def write_target_file(self, name: str, **variables: Any) -> Path:
        """
        Render a file declared in the target profile's `files` section into
        the target root.

        Keeping the *content* of OS-specific files (sources.list, fstab,
        hosts, ...) in the profile is what stops distro-specific text from
        creeping back into Python — the same reasoning that keeps commands
        out of it.
        """
        spec = self.target.profile.get("files", {}).get(name)
        if spec is None:
            raise KeyError(
                f"Profile '{self.target_os}' declares no files.{name} entry"
            )

        variables = {"install_root": self.install_root, "config_dir": self.config_dir, **variables}
        dest = self.target_path(spec["path"].format(**variables))
        Logger.command(f"[file] {dest}")

        if self.dry_run:
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)

        if "symlink_to" in spec:
            link_target = spec["symlink_to"].format(**variables)
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            dest.symlink_to(link_target)
            return dest

        content = spec["content"].format(**variables)
        if spec.get("append"):
            # Some editions have no drop-in directory for a given file (no
            # /etc/cron.d on FreeBSD, for instance) and the entry has to go
            # into the system-wide file instead.
            existing = dest.read_text() if dest.exists() else ""
            if content not in existing:
                dest.write_text(existing + content)
        else:
            # A previous run may have left the file read-only (sudoers is
            # 0440), and writing over it would fail: replace it outright.
            dest.unlink(missing_ok=True)
            dest.write_text(content)
            dest.chmod(spec.get("mode", 0o644))
        return dest

    def write_bootstrap_files(self, directory: Path, **variables: Any) -> None:
        """
        Render the target profile's `bootstrap_files` section into a directory
        on the host: configuration the bootstrapper needs before the target
        root holds anything, such as the pacman.conf pacstrap is pointed at on
        a host that is not Arch.
        """
        for spec in self.target.profile.get("bootstrap_files", {}).values():
            dest = directory / spec["path"]
            Logger.command(f"[file] {dest}")
            if self.dry_run:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(spec["content"].format(**variables))

    def ensure_target_dir(self, path: str, mode: int = 0o755) -> Path:
        dest = self.target_path(path)
        if not self.dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            dest.chmod(mode)
        return dest

    # ------------------------------------------------------------------
    # Target discovery
    # ------------------------------------------------------------------

    @staticmethod
    def available_targets(profile_dir: str) -> List[Dict[str, str]]:
        """
        Every edition that has both an OS profile and an installer profile
        fragment, with the metadata needed to decide whether this host can
        install it: os_family, os_name and community_maintained.
        """
        base = Path(profile_dir)
        targets = []
        for fragment in sorted((base / "installer").glob("installer-*.json")):
            edition = fragment.stem.replace("installer-", "")
            os_profile = base / "os" / f"os-{edition}.json"
            if not os_profile.exists():
                continue
            with open(os_profile) as fh:
                data = json.load(fh)
            with open(fragment) as fh:
                installer_data = json.load(fh)
            targets.append({
                "edition": edition,
                "os_name": data.get("os_name", edition),
                "os_family": data.get("os_family", "unknown"),
                "init_system": data.get("init_system", "unknown"),
                "community": bool(data.get("community_maintained")),
                "bootstrap_methods": installer_data.get(
                    "installer_meta", {}
                ).get("bootstrap_methods", []),
            })
        return targets

    def installable_targets(self) -> List[Dict[str, str]]:
        """
        Targets this host can actually provision.

        The hard limitation, stated once, here: bootstrapping a system means
        running that system's own tooling (debootstrap, pacstrap, xbps,
        FreeBSD's base.txz + chroot), which only exists on a host of the same
        OS family. A Linux installer installs Linux editions; installing
        FreeBSD needs a FreeBSD installer environment.
        """
        host_family = self.host.profile.get("os_family", "unknown")
        return [
            t for t in self.available_targets(self.profile_dir)
            if t["os_family"] == host_family
        ]
