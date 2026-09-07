"""
FreedomNAS Installer — clean install onto a dedicated disk.

Provisions a target edition from nothing: partition, root pool, bootstrap,
configuration, users, packages, FreedomNAS itself, boot chain, and the
config.yaml shadow copy. The result is byte-for-byte the layout
freedomnas-image-builder produces, so an installed machine and a flashed
image are the same system as far as the reconciler and the WebUI are
concerned.

All questions are asked first and confirmed once; nothing destructive
happens before that confirmation.
"""

from __future__ import annotations

from context import ExecutionContext
from installer_modules import Logger, DialogManager, Configuration, MODE_CLEAN
from bootloader_installer import BootloaderInstaller
from core_installer import CoreInstaller
from disk_installer import DiskInstaller
from packages_installer import PackagesInstaller
from state_installer import StateWriter
from system_installer import SystemInstaller
from users_installer import UsersInstaller
from zfs_installer import ZFSInstaller


class CleanInstaller:
    """Full provisioning of a target disk."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx
        self.config.mode = MODE_CLEAN

        self.system = SystemInstaller(dialog, config, ctx)
        self.disk = DiskInstaller(dialog, config, ctx)
        self.zfs = ZFSInstaller(dialog, config, ctx)
        self.users = UsersInstaller(dialog, config, ctx)
        self.packages = PackagesInstaller(dialog, config, ctx)
        self.core = CoreInstaller(config, ctx)
        self.bootloader = BootloaderInstaller(dialog, config, ctx)
        self.state = StateWriter(config, ctx)

        self._chroot_ready = False
        self._esp_mounted = False
        self._pool_created = False

    # ------------------------------------------------------------------
    # Interview
    # ------------------------------------------------------------------

    def interview(self) -> None:
        """Ask everything up front; touch nothing."""
        self.system.detect_boot_mode()
        self.system.load_defaults()

        self.disk.select_disk()
        self.zfs.prompt_pool_name()
        self.system.select_bootstrap_method()
        self.system.select_mirror()
        self.system.prompt_rootfs_url()
        self.system.prompt_hostname()
        self.system.prompt_timezone()
        self.packages.select_categories()
        self.users.prompt_admin_user()
        self.users.prompt_additional_users()

    def confirm(self) -> bool:
        summary = (
            f"Edition    : {self.ctx.os_info()['os_name']} ({self.config.target_os})\n"
            f"Init       : {self.ctx.target.init_system}\n"
            f"Bootstrap  : {self.config.bootstrap_method}\n"
            f"Firmware   : {self.config.boot_mode}\n"
            f"Disk       : {self.config.disk}  (WILL BE ERASED)\n"
            f"Pool       : {self.config.pool_name} -> {self.config.root_dataset}\n"
            f"Hostname   : {self.config.hostname}\n"
            f"Timezone   : {self.config.timezone}\n"
            f"Admin      : {self.config.username}\n"
            f"Features   : {', '.join(self.config.package_categories)}\n\n"
            "Proceed with the installation? Everything on the disk will be lost."
        )
        return self.dialog.defaultno("Confirm Installation", summary, 20, 76)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self) -> None:
        Logger.info(f"Installing {self.config.target_os} on {self.config.disk}")

        self.disk.partition_disk()
        self.disk.format_partitions()

        self.zfs.create_root_pool()
        self._pool_created = True

        self.system.bootstrap_base_system()
        self.system.configure_system()
        self.system.setup_chroot()
        self._chroot_ready = True

        # Users before packages: PAM/Samba must find the accounts already
        # there when those services are first configured.
        self.users.create_users()

        self.packages.install_packages(fresh_root=True)
        self.packages.locale_setup()
        self.packages.enable_services()

        self.core.install_freedomnas()
        self.core.install_firstboot()

        self.bootloader.install_bootloader()
        self._esp_mounted = True

        self.state.mount_state_dataset()
        self.state.save()

    def cleanup(self) -> None:
        """Unwind mounts and export the pool. Safe to call at any point."""
        if self._esp_mounted:
            self.bootloader.unmount_esp()
            self._esp_mounted = False
        if self._chroot_ready:
            self.system.teardown_chroot()
            self._chroot_ready = False
        if self._pool_created:
            self.zfs.export_pool()
            self._pool_created = False

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> bool:
        self.interview()
        if not self.confirm():
            Logger.warn("Installation cancelled by the user")
            return False
        try:
            self.execute()
        finally:
            self.cleanup()
        return True
