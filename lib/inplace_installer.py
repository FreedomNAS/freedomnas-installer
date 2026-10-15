"""
FreedomNAS Installer — install onto the system that is already running.

This is the mode that makes the installer follow the same "install the
package, get the system" philosophy as the rest of FreedomNAS: the machine
already runs a supported OS and already has freedomnas-core; the installer
completes it into a NAS.

Nothing is partitioned, nothing is bootstrapped, no boot chain is touched.
What happens is:

  1. the package categories from freedomnas-profiles/packages/ are installed
     with the target's own package manager (idempotent — only what's missing
     actually gets pulled in)
  2. their services are enabled through the init profile, so this works the
     same under systemd, OpenRC, runit, s6, dinit or bsd_rc
  3. the WebUI first-boot hook is wired up
  4. the installation is recorded in config.yaml in the state directory,
     not marked initialized: the first boot moves it into the root pool's
     `.freedomnas` dataset and the setup wizard copies it to the pool the
     administrator picks (on a system with no pool it stays under the
     config directory, initialized)

Because the target *is* the host, the target engine runs without a chroot
prefix and every command lands directly on the running system.
"""

from __future__ import annotations

import socket
from typing import List

import diagnostics
from context import ExecutionContext
from core_installer import CoreInstaller
from installer_modules import Logger, DialogManager, Configuration, MODE_INPLACE
from packages_installer import PackagesInstaller
from state_installer import STATE_INSTALL, StateWriter
from users_installer import UsersInstaller


class InPlaceInstaller:
    """Turns the running system into a FreedomNAS."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx
        self.config.mode = MODE_INPLACE
        self.config.target_root = "/"

        self.packages = PackagesInstaller(dialog, config, ctx)
        self.users = UsersInstaller(dialog, config, ctx)
        self.core = CoreInstaller(config, ctx)
        self.state = StateWriter(config, ctx)

        self._create_admin = False
        self._has_pools = False

    # ------------------------------------------------------------------
    # Interview
    # ------------------------------------------------------------------

    def interview(self) -> None:
        notice = self.ctx.meta("install_notice")
        if notice:
            self.dialog.msgbox(f"Installing {self.ctx.os_info()['os_name']}", notice, 16, 72)
        self.config.hostname = socket.gethostname() or self.config.hostname
        self.config.timezone = self._current_timezone()
        self._detect_root_dataset()

        self.packages.select_categories()

        self._create_admin = self.dialog.yesno(
            "Administrator Account",
            "Create a dedicated FreedomNAS administrator account?\n\n"
            "Choose No to keep using the accounts this system already has.",
            height=11,
            width=64,
        )
        if self._create_admin:
            self.users.prompt_admin_user()

        self._has_pools = bool(self._list_pools())

    def _detect_root_dataset(self) -> None:
        """
        Ask the running system what its root dataset is.

        Composing one from a pool name and an edition guesses a layout this
        machine need not have: rpool/os/gentoo is as valid a root as
        rpool/ROOT/debian, and an in-place install has no business assuming.
        """
        try:
            dataset, _ = self.ctx.host_run(
                "rootpool", "detect_root_dataset", check=False, timeout=30
            )
        except Exception as exc:
            Logger.warn(f"Could not detect the root dataset: {exc}")
            return

        dataset = dataset.strip()
        if dataset and "/" in dataset:
            self.config.detected_root_dataset = dataset
            Logger.info(f"Root dataset: {dataset}")

    def _current_timezone(self) -> str:
        """Read /etc/timezone or the /etc/localtime symlink — plain file I/O."""
        from pathlib import Path

        tz_file = Path("/etc/timezone")
        if tz_file.is_file():
            value = tz_file.read_text().strip()
            if value:
                return value
        localtime = Path("/etc/localtime")
        if localtime.is_symlink():
            target = str(localtime.resolve())
            marker = "/zoneinfo/"
            if marker in target:
                return target.split(marker, 1)[1]
        return self.config.timezone

    def _list_pools(self) -> List[str]:
        try:
            stdout, _ = self.ctx.host_run("rootpool", "list_pools", check=False, timeout=60)
        except Exception as exc:
            Logger.warn(f"Could not list pools: {exc}")
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def confirm(self) -> bool:
        summary = (
            f"System     : {self.ctx.os_info()['os_name']} ({self.config.target_os})\n"
            f"Init       : {self.ctx.target.init_system}\n"
            f"Hostname   : {self.config.hostname}\n"
            f"Root       : {self.config.detected_root_dataset or 'not on ZFS'}\n"
            f"Features   : {', '.join(self.config.package_categories)}\n"
            f"Admin      : {self.config.username if self._create_admin else 'existing accounts'}\n"
            f"State      : {'chosen in the setup wizard' if self._has_pools else self.state.fallback_path()}\n\n"
            "FreedomNAS will be installed onto THIS running system.\n"
            "Packages will be installed and services enabled. Continue?"
        )
        return self.dialog.defaultno("Confirm In-Place Installation", summary, 20, 76)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def prepare_package_manager(self) -> None:
        """
        Write the profile files marked `inplace`, before anything is
        installed.

        A clean install writes every file the edition declares; a running
        system must not have its hostname or fstab rewritten under it. The
        exception is package-manager drop-ins — Gentoo needs the packages
        FreedomNAS depends on but that are still ~arch to be accepted, or
        emerge refuses the whole set. Those entries are additive files in a
        drop-in directory, named after us, so they add to the admin's
        configuration instead of replacing it.
        """
        variables = {
            "hostname": self.config.hostname,
            "fqdn": self.config.hostname,
            "domain": self.config.domain,
            "timezone": self.config.timezone,
            "mirror": self.config.mirror,
            "suite": self.config.suite,
            "arch": self.config.arch,
            "esp_device": "",
            "esp_spec": "",
            "pool_name": self.config.pool_name,
            "edition": self.config.target_os,
            "sudo_group": self.ctx.target.get_profile_value("users", "sudo_group"),
            "channel": self.config.channel,
        }

        for name, spec in sorted(self.ctx.target.profile.get("files", {}).items()):
            if not spec.get("inplace"):
                continue
            parent = self.ctx.target_path(spec["path"]).parent
            if parent.exists() and not parent.is_dir():
                Logger.warn(
                    f"{parent} is a file, not a drop-in directory — add the "
                    f"contents of files.{name} to it by hand"
                )
                continue
            self.ctx.write_target_file(name, **variables)

    def execute(self) -> None:
        Logger.info(f"Installing FreedomNAS onto the running {self.config.target_os} system")

        self.prepare_package_manager()
        self.packages.install_packages(fresh_root=False)
        self.packages.enable_services()

        if self._create_admin:
            self.users.create_users()
            self.users.set_samba_passwords()

        self.core.install_freedomnas()
        self.core.install_firstboot()

        if self._has_pools:
            self.state.save(initialized=False)
            self.state.mark_setup_pending(STATE_INSTALL)
        else:
            Logger.warn(f"No ZFS pools found — configuration stored under {self.ctx.config_dir}")
            self.state.save(self.state.fallback_path())

    def run(self) -> bool:
        self.interview()
        if not self.confirm():
            Logger.warn("Installation cancelled by the user")
            return False
        try:
            self.execute()
        except Exception as exc:
            diagnostics.collect(self.ctx, f"installation step failed: {exc}", exc)
            raise
        return True
