#!/usr/bin/env python3
"""
FreedomNAS ZFS-on-Root Installer — Main entry point.

PARKED: superseded by freedomnas-image-builder's pre-built images
(ADR-002, IMAGE-BASED-INSTALLATION.md). This tool is kept functional
for reference and potential future refactor, but is not the recommended
or actively maintained install path — see prints_parked_warning() below
and freedomnas-installer/README.md.

Execution order follows spec §8.3 exactly:
  1.  check_root()
  2.  detect_boot_mode()
  3.  select_mirror()
  4.  prompt_hostname()
  5.  select_disk()
  6.  partition_disk()
  7.  format_partitions()
  8.  create_zfs_pool()
  9.  install_base_system()
  10. configure_system()
  11. setup_chroot()
  12. get_admin_password()         ← BEFORE packages
  13. prompt_additional_users()    ← BEFORE packages
  14. install_packages()
  15. install_bootloader()
  16. create_firstboot_service()
  17. save_installation_config()   ← writes config.yaml to ZFS
  18. cleanup()
  19. show_completion()

Recovery mode (spec §8.4):
  Triggered when an existing ZFS pool with .freedomnas dataset is detected.
  Offers to restore from saved configuration instead of fresh install.
"""

import atexit
import os
import signal
import subprocess
import sys

# Add lib/ to path so modules can import each other without package install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

from installer_modules import (
    Logger, DialogManager, Configuration, check_root, run_command,
)
from system_installer import SystemInstaller
from disk_installer import DiskInstaller
from zfs_installer import ZFSInstaller
from bootloader_installer import BootloaderInstaller
from users_installer import UsersInstaller
from packages_installer import PackagesInstaller


# ---------------------------------------------------------------------------
# Parked-tool warning
# ---------------------------------------------------------------------------

def print_parked_warning() -> None:
    """
    freedomnas-installer is parked, not removed (see setup.py's module
    docstring and README.md). This tool remains fully functional -- the
    warning is meant to discourage new usage, not block it.
    """
    print(
        "NOTE: freedomnas-installer is PARKED. New installs should use "
        "freedomnas-image-builder (pre-built images, ADR-002). This tool "
        "remains functional but is not actively maintained.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _cleanup(logger: Logger = None) -> None:
    """Unmount chroot bind-mounts and export ZFS pools."""
    if logger is None:
        logger = Logger()
    logger.info("Cleaning up mounts...")

    for mount in ["/mnt/run", "/mnt/dev/pts", "/mnt/dev", "/mnt/proc", "/mnt/sys"]:
        subprocess.run(["umount", "-l", mount], capture_output=True)

    subprocess.run(["umount", "-l", "/mnt/boot/efi"], capture_output=True)
    subprocess.run(["zpool", "export", "-a"], capture_output=True)


# ---------------------------------------------------------------------------
# Installer orchestrator
# ---------------------------------------------------------------------------

class FreedomNASInstaller:

    def __init__(self) -> None:
        self.dialog = DialogManager()
        self.config = Configuration()
        self.config.os_edition = self._detect_os_edition()

        self.system    = None
        self.disk      = None
        self.zfs       = None
        self.bootloader = None
        self.users     = None
        self.packages  = None

    # ------------------------------------------------------------------
    # OS edition detection
    # ------------------------------------------------------------------

    def _detect_os_edition(self) -> str:
        import platform
        if platform.system() == "FreeBSD":
            return "freebsd"
        if os.path.exists("/etc/arch-release"):
            return "arch"
        return "debian"

    # ------------------------------------------------------------------
    # Module initialization
    # ------------------------------------------------------------------

    def _init_modules(self) -> None:
        self.system     = SystemInstaller(Logger, self.dialog, self.config)
        self.disk       = DiskInstaller(Logger, self.dialog, self.config)
        self.zfs        = ZFSInstaller(Logger, self.dialog, self.config)
        self.bootloader = BootloaderInstaller(Logger, self.dialog, self.config)
        self.users      = UsersInstaller(Logger, self.dialog, self.config)
        self.packages   = PackagesInstaller(Logger, self.dialog, self.config)

    # ------------------------------------------------------------------
    # Signal handler
    # ------------------------------------------------------------------

    def _handle_interrupt(self, sig, frame) -> None:
        Logger.warn("Installation interrupted by user (Ctrl+C)")
        _cleanup()
        self.dialog.msgbox("Cancelled", "Installation was cancelled by the user.", 7, 50)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Recovery mode detection
    # ------------------------------------------------------------------

    def _detect_existing_pool(self) -> bool:
        """Return True if a .freedomnas dataset is found in importable pools."""
        try:
            result = run_command(
                ["zpool", "import", "-d", "/dev/disk/by-id"],
                check=False,
                timeout=30,
            )
            return ".freedomnas" in result.stdout
        except Exception:
            return False

    def _run_recovery_mode(self) -> None:
        """Restore system from existing config.yaml via ReconciliationEngine."""
        Logger.info("Recovery mode — restoring from existing configuration...")
        try:
            from freedomnas.engine import get_engine
            from freedomnas.config import ConfigurationManager
            from freedomnas.reconcile import ReconciliationEngine
        except ImportError:
            self.dialog.msgbox(
                "Recovery Failed",
                "freedomnas-core is not installed.\n"
                "Cannot run recovery mode without the core engine.",
                10, 60,
            )
            return

        engine = get_engine()
        cm = ConfigurationManager()
        reconciler = ReconciliationEngine(engine, cm)
        results = reconciler.reconcile(mode="disaster_recovery")

        applied = sum(1 for r in results if r.target_state == "applied")
        failed  = sum(1 for r in results if r.target_state == "failed")

        self.dialog.msgbox(
            "Recovery Complete",
            f"Configuration restored from ZFS dataset.\n\n"
            f"  Applied : {applied}\n"
            f"  Failed  : {failed}\n\n"
            "Reboot the system to complete recovery.",
            14, 60,
        )

    # ------------------------------------------------------------------
    # Post-install config save
    # ------------------------------------------------------------------

    def _save_installation_config(self) -> None:
        """Write config.yaml to the .freedomnas ZFS dataset (step 17)."""
        Logger.info("Saving installation configuration to ZFS dataset...")
        try:
            from freedomnas.config import ConfigurationManager
        except ImportError:
            Logger.warn("freedomnas-core not found — skipping config save")
            return

        config_path = f"/mnt/etc/freedomnas/state/config.yaml"
        try:
            cm = ConfigurationManager(config_path=config_path)
            cm.set_hostname(self.config.hostname)
            cm.set_timezone(self.config.timezone)
            cm.add_user({
                "username": self.config.username,
                "groups": ["sudo", "samba"],
                "shell": "/bin/bash",
            })
            for user in self.config.additional_users:
                cm.add_user({
                    "username": user["username"],
                    "groups": ["sudo"] if user.get("sudo") else [],
                    "shell": "/bin/bash",
                })
            cm.add_pool({
                "name": self.config.pool_name,
                "vdev_type": "single",
                "ashift": 12,
                "compression": "lz4",
            })
            cm.mark_initialized(
                edition=self.config.os_edition,
                initialized_by="installer",
            )
            Logger.info(f"Configuration saved: {config_path}")
        except Exception as exc:
            Logger.warn(f"Could not save installation config: {exc}")

    # ------------------------------------------------------------------
    # Cleanup and completion
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        Logger.info("Cleaning up...")
        _cleanup(Logger)

    def _show_welcome(self) -> None:
        self.dialog.msgbox(
            "Welcome to FreedomNAS Installer",
            f"This installer will provision a FreedomNAS ZFS-on-root system.\n\n"
            f"Edition  : {self.config.os_edition.upper()}\n\n"
            "WARNING: The selected disk will be completely erased!\n\n"
            "Press Enter to continue.",
            16, 70,
        )

    def _show_completion(self) -> None:
        if self.dialog.yesno(
            "Installation Complete",
            "FreedomNAS has been installed successfully.\n\n"
            f"  Username : {self.config.username}\n"
            f"  Hostname : {self.config.hostname}\n\n"
            "Reboot now?",
            12, 60,
        ):
            Logger.info("Rebooting...")
            subprocess.run(["reboot"])

    # ------------------------------------------------------------------
    # Main workflow
    # ------------------------------------------------------------------

    def run(self) -> None:
        check_root()
        atexit.register(_cleanup)
        signal.signal(signal.SIGINT, self._handle_interrupt)

        self._init_modules()

        # Recovery mode check (spec §8.4)
        if self._detect_existing_pool():
            if self.dialog.yesno(
                "Existing Pool Detected",
                "An existing FreedomNAS ZFS pool was found.\n\n"
                "Restore from the saved configuration\n"
                "instead of a fresh install?",
                12, 65,
            ):
                self._run_recovery_mode()
                return

        self._show_welcome()
        Logger.info(f"Starting installation — edition: {self.config.os_edition}")

        try:
            # Steps 2–3
            self.system.detect_boot_mode()
            self.system.select_mirror()

            # Step 4 — hostname
            self.system.prompt_hostname()

            # Steps 5–7 — disk
            self.disk.select_disk()
            self.disk.partition_disk()
            self.disk.format_partitions()

            # Step 8 — ZFS
            self.zfs.create_zfs_pool()

            # Steps 9–11 — base system
            self.system.install_base_system()
            self.system.configure_system()
            self.system.setup_chroot()

            # Steps 12–13 — users BEFORE packages (spec §8.3)
            self.users.get_admin_password()
            self.users.prompt_additional_users()
            self.users.create_users_in_chroot()

            # Step 14 — packages
            self.packages.install_packages()

            # Step 15 — bootloader
            self.bootloader.install_bootloader()

            # Step 16 — first-boot WebUI service
            self.packages.create_firstboot_service()

            # Step 17 — persist config to ZFS
            self._save_installation_config()

            # Steps 18–19
            self._cleanup()
            self._show_completion()

        except Exception as exc:
            Logger.error(f"Installation failed: {exc}")
            _cleanup()
            self.dialog.msgbox(
                "Installation Failed",
                f"An error occurred during installation:\n\n{exc}\n\n"
                "Check /var/log/freedomnas-install.log for details.",
                14, 70,
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print_parked_warning()
    FreedomNASInstaller().run()
