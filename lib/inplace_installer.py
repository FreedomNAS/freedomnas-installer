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
  4. the installation is recorded in config.yaml, on the `.freedomnas`
     dataset of an existing pool where there is one

Because the target *is* the host, the target engine runs without a chroot
prefix and every command lands directly on the running system.
"""

from __future__ import annotations

import socket
from typing import List, Optional

from context import ExecutionContext
from core_installer import CoreInstaller
from installer_modules import Logger, DialogManager, Configuration, MODE_INPLACE
from packages_installer import PackagesInstaller
from state_installer import StateWriter
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
        self._state_pool: Optional[str] = None

    # ------------------------------------------------------------------
    # Interview
    # ------------------------------------------------------------------

    def interview(self) -> None:
        self.config.hostname = socket.gethostname() or self.config.hostname
        self.config.timezone = self._current_timezone()

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

        self._state_pool = self._select_state_pool()

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

    def _select_state_pool(self) -> Optional[str]:
        """
        Ask which existing pool should hold the `.freedomnas` dataset. That
        dataset is what lets this machine be rebuilt later from its own
        configuration, so it belongs on a pool that survives a reinstall.
        """
        pools = self._list_pools()
        if not pools:
            Logger.warn("No ZFS pools found — configuration will be stored under /etc/freedomnas")
            return None

        items = [(pool, f"Store FreedomNAS state on {pool}") for pool in pools]
        items.append(("NONE", "Do not use a pool — store under /etc/freedomnas"))

        choice = self.dialog.menu(
            "Configuration Storage",
            "Where should the FreedomNAS configuration dataset live?\n"
            "(This is what disaster recovery reads on the next boot.)",
            items,
            height=18,
            width=72,
        )
        if not choice or choice == "NONE":
            return None
        return choice

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
            f"Features   : {', '.join(self.config.package_categories)}\n"
            f"Admin      : {self.config.username if self._create_admin else 'existing accounts'}\n"
            f"State      : {self._state_pool + '/.freedomnas' if self._state_pool else StateWriter.fallback_path()}\n\n"
            "FreedomNAS will be installed onto THIS running system.\n"
            "Packages will be installed and services enabled. Continue?"
        )
        return self.dialog.defaultno("Confirm In-Place Installation", summary, 20, 76)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self) -> None:
        Logger.info(f"Installing FreedomNAS onto the running {self.config.target_os} system")

        self.packages.install_packages(fresh_root=False)
        self.packages.enable_services()

        if self._create_admin:
            self.users.create_users()

        self.core.install_freedomnas()
        self.core.install_firstboot()

        state_path = None
        if self._state_pool:
            self.config.pool_name = self._state_pool
            if not self.state.ensure_state_dataset(self._state_pool):
                state_path = StateWriter.fallback_path()
        else:
            state_path = StateWriter.fallback_path()

        self.state.save(state_path)

    def run(self) -> bool:
        self.interview()
        if not self.confirm():
            Logger.warn("Installation cancelled by the user")
            return False
        self.execute()
        return True
