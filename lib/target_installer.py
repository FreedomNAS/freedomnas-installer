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

import os

import diagnostics
from context import ExecutionContext
from installer_modules import Logger, DialogManager, Configuration, MODE_CLEAN
from bootloader_installer import BootloaderInstaller
from core_installer import CoreInstaller
from disk_installer import DiskInstaller
from packages_installer import DEFAULT_OPTIONAL, PackagesInstaller
from state_installer import STATE_INSTALL, StateWriter
from system_installer import SystemInstaller
from users_installer import UsersInstaller
from zfs_installer import ZFSInstaller



def _when(saved_at: str) -> str:
    """A copy's saved_at (UTC, ISO) as a date and time a person reads."""
    return saved_at[:16].replace("T", " ") + " UTC" if saved_at else "at an unknown time"


class CleanInstaller:
    """Full provisioning of a target disk."""

    def __init__(
        self,
        dialog: DialogManager,
        config: Configuration,
        ctx: ExecutionContext,
        owns_target_root: bool = False,
    ) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx
        self.config.mode = MODE_CLEAN
        # True when the directory was made for this run, and so is ours to
        # remove once everything is unmounted.
        self._owns_target_root = owns_target_root

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
        notice = self.ctx.meta("install_notice")
        if notice:
            self.dialog.msgbox(f"Installing {self.ctx.os_info()['os_name']}", notice, 16, 72)
        self.system.detect_boot_mode()
        self.system.load_defaults()
        self.system.select_init_system()

        self.disk.select_disk()
        self._choose_restore()
        self.zfs.prompt_pool_name()
        self.system.select_bootstrap_method()
        self.system.select_mirror()
        self.system.prompt_rootfs_url()
        restore = self.config.restore
        if restore:
            self._take_from_restore(restore)
            self.users.prompt_restored_admin(restore["admins"])
            return
        self.system.prompt_hostname()
        self.system.prompt_timezone()
        self.packages.select_categories()
        self.users.prompt_admin_user()
        self.users.prompt_additional_users()

    def _choose_restore(self) -> None:
        """
        A data pool that holds a FreedomNAS configuration can be restored
        into the new system, the newest copy first. The answers it already
        has are not asked again, and its identity and modules go in before
        the first boot, which restores the rest as for any reinstall.
        """
        found = self.state.find_restorable(self.config.disk_by_id or self.config.disk or "")
        if not found:
            Logger.info("No FreedomNAS configuration found on other pools: the setup wizard will create one")
            return
        Logger.info(f"FreedomNAS configuration found on: {', '.join(s['pool'] for s in found)}")
        items = [
            (str(index), f"{s['hostname']} ({s['edition']}) on {s['pool']}, saved {_when(s['saved_at'])}")
            for index, s in enumerate(found, 1)
        ] + [("new", "A new NAS: ignore these, the setup wizard runs")]
        choice = self.dialog.menu(
            "Existing FreedomNAS Configuration",
            "These pools hold a FreedomNAS configuration. Restore one into the new\n"
            "system? Users, shares, network, apps and settings come back; you set\n"
            "only the administrator's password. Leave the pool's disks attached.",
            items, height=18, width=76, list_height=6,
        )
        chosen = found[int(choice) - 1] if choice and choice.isdigit() else None
        if chosen and not chosen["admins"]:
            Logger.warn(f"The configuration on {chosen['pool']} has no administrator: not restored")
            chosen = None
        self.state.discard([s for s in found if s is not chosen])
        self.config.restore = chosen
        self.config.restore_declined = chosen is None
        if chosen:
            Logger.info(f"Restoring {chosen['hostname']} from {chosen['pool']}")
        else:
            Logger.info("A new NAS: the configurations found stay on their pools, untouched")

    def _take_from_restore(self, restore: dict) -> None:
        self.config.hostname = restore["hostname"]
        if restore["timezone"]:
            self.config.timezone = restore["timezone"]
        categories = self.packages.packages.get_required_categories()
        for feature in [*restore["features"], *DEFAULT_OPTIONAL]:
            if feature not in categories and self.packages.packages.validate_category(feature):
                categories.append(feature)
        self.config.package_categories = categories
        Logger.info(f"From the restored configuration: hostname {self.config.hostname}, "
                    f"timezone {self.config.timezone}, features {', '.join(self.config.package_categories)}")

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
            f"Features   : {', '.join(self.config.package_categories)}\n"
            + (f"Restore    : {self.config.restore['hostname']} from {self.config.restore['pool']}"
               f" (modules: {', '.join(self.config.restore['modules']) or 'none'})\n"
               if self.config.restore else "")
            + "\n"
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

        # Claim each of these before doing it, not after: a failure partway
        # through still leaves a pool imported or a filesystem mounted, and
        # cleanup has to know to undo it. Every undo runs with check=False,
        # so claiming one that never happened costs nothing.
        self._pool_created = True
        self.zfs.create_root_pool()

        self.system.bootstrap_base_system()
        self.system.configure_system()
        if self.ctx.has("bootloader", "build_uki"):
            self.bootloader.deploy_dracut_modules()
        self._chroot_ready = True
        self.system.setup_chroot()

        # Users before packages: PAM/Samba must find the accounts already
        # there when those services are first configured.
        self.users.create_users()
        self.users.lock_root()

        self.packages.install_packages(fresh_root=True)
        self.packages.ensure_kernel_modules()
        self.packages.locale_setup()
        self.packages.enable_services()
        self.packages.disable_ssh()
        self.users.set_samba_passwords()

        self.core.install_freedomnas()
        self.core.install_firstboot()
        if self.config.restore:
            self.state.restore_offline(self.config.restore)
        self.zfs.install_hostid()
        self.zfs.install_cachefile()

        self._esp_mounted = True
        self.bootloader.install_bootloader()

        if self.config.image_mode:
            Logger.info("Disk image: no .freedomnas and no configuration, the first boot runs the setup wizard")
            self.state.mark_setup_pending()
            return
        Logger.info(
            "The first boot creates the root pool's .freedomnas and restores a "
            "configuration found on another pool, or the setup wizard runs"
        )
        self.state.save(initialized=False)
        self.state.mark_setup_pending(STATE_INSTALL)

    def cleanup(self) -> None:
        """Unwind mounts and export the pool. Safe to call at any point."""
        if self.config.restore:
            self.state.discard([self.config.restore])
        if self._esp_mounted:
            self.bootloader.unmount_esp()
            self._esp_mounted = False
        if self._chroot_ready:
            self.system.teardown_chroot()
            self._chroot_ready = False
        if self._pool_created:
            self.zfs.export_pool()
            if self.zfs.export_failed:
                diagnostics.collect(self.ctx, f"the pool {self.config.pool_name} could not be exported")
            self._pool_created = False
        self._remove_target_root()

    def _remove_target_root(self) -> None:
        """Take away the directory we made, if nothing is left inside it."""
        if not self._owns_target_root:
            return
        try:
            os.rmdir(self.config.target_root)
        except OSError:
            Logger.warn(f"Left {self.config.target_root} in place — not empty")
            return
        self._owns_target_root = False

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
        except Exception as exc:
            diagnostics.collect(self.ctx, f"installation step failed: {exc}", exc)
            raise
        finally:
            self.cleanup()
        return True
