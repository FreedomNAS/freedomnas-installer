# =============================================================================
# FreedomNAS ZFS-on-Root Installer (Python Port)
# Main entry point - modular architecture
# =============================================================================

import sys
import os
import signal
import atexit

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

from installer_modules import (
    Logger, DialogManager, Configuration, check_root
)
from system_installer import SystemInstaller
from disk_installer import DiskInstaller
from zfs_installer import ZFSInstaller
from bootloader_installer import BootloaderInstaller
from users_installer import UsersInstaller
from packages_installer import PackagesInstaller


def _cleanup_mounts(logger=None):
    """Comprehensive cleanup of mounted pools and partitions"""
    if logger is None:
        logger = Logger()

    try:
        logger.info("Cleaning up mounted filesystems...")

        # Unmount everything under /mnt in reverse order
        try:
            import subprocess
            result = subprocess.run(
                "mount | grep '/mnt' | awk '{print $3}' | sort -r",
                shell=True,
                capture_output=True,
                text=True,
                check=False
            )
            for mount_point in result.stdout.strip().split('\n'):
                if mount_point:
                    logger.info(f"Unmounting {mount_point}")
                    subprocess.run(f"umount '{mount_point}' 2>/dev/null", shell=True, check=False)
        except Exception as e:
            logger.warn(f"Error unmounting /mnt filesystems: {e}")

        # Export all ZFS pools
        try:
            import subprocess
            subprocess.run("zpool export -a 2>/dev/null || true", shell=True, check=False)
            logger.info("Exported all ZFS pools")
        except Exception as e:
            logger.warn(f"Error exporting ZFS pools: {e}")

    except Exception as e:
        logger.warn(f"Cleanup error: {e}")


class FreedomNASInstaller:
    """Main installer orchestrator"""
    
    def __init__(self):
        self.logger = Logger()
        self.dialog = DialogManager()
        self.config = Configuration()
        self.config.os_edition = self._detect_os_edition()
        self.system_installer = None
        self.disk_installer = None
        self.zfs_installer = None
        self.bootloader_installer = None
        self.users_installer = None
        self.packages_installer = None
    
    def _detect_os_edition(self) -> str:
        """Auto-detect the OS edition"""
        try:
            import platform
            system = platform.system()
            if system == "FreeBSD":
                self.logger.info("Detected FreeBSD edition")
                return "freebsd"
            elif system == "Linux":
                # Check for Arch Linux
                if os.path.exists("/etc/arch-release"):
                    self.logger.info("Detected Arch Linux edition")
                    return "arch"
                else:
                    # Default to Debian-based
                    self.logger.info("Detected Debian-based Linux")
                    return "debian"
            else:
                self.logger.warn(f"Unknown OS: {system}, defaulting to Debian")
                return "debian"
        except Exception as e:
            self.logger.warn(f"Could not detect OS edition: {e}, defaulting to Debian")
            return "debian"

    def initialize_modules(self):
        """Initialize all installer modules"""
        self.system_installer = SystemInstaller(self.logger, self.dialog, self.config)
        self.disk_installer = DiskInstaller(self.logger, self.dialog, self.config)
        self.zfs_installer = ZFSInstaller(self.logger, self.dialog, self.config)
        self.bootloader_installer = BootloaderInstaller(self.logger, self.dialog, self.config)
        self.users_installer = UsersInstaller(self.logger, self.dialog, self.config)
        self.packages_installer = PackagesInstaller(self.logger, self.dialog, self.config)
    
    def signal_handler(self, sig, frame):
        """Handle Ctrl+C gracefully"""
        self.logger.warn("\nInstallation interrupted by user")
        _cleanup_mounts(self.logger)
        self.dialog.msgbox("Error", "Installation cancelled by user.", 7, 40)
        sys.exit(1)
    
    def show_welcome(self):
        """Show welcome screen"""
        self.logger.info("=" * 70)
        self.logger.info("  FreedomNAS ZFS-on-Root Installer")
        self.logger.info(f"  OS Edition: {self.config.os_edition.upper()}")
        self.logger.info("=" * 70)
        self.logger.info("")

        self.dialog.msgbox(
            "Welcome to FreedomNAS Installer",
            f"This installer will guide you through the process of "
            f"installing FreedomNAS with ZFS-on-root.\n\n"
            f"Detected OS Edition: {self.config.os_edition.upper()}\n\n"
            f"WARNING: This will completely erase the selected disk!\n\n"
            f"Make sure you have selected the correct disk before proceeding.",
            16, 70
        )
    
    def _detect_existing_pool(self) -> bool:
        """
        Check for an existing ZFS pool containing a .freedomnas dataset.
        Returns True if recovery mode should be offered (spec §8.4).
        """
        try:
            import subprocess
            result = subprocess.run(
                ["zpool", "import", "-d", "/dev/disk/by-id"],
                capture_output=True, text=True, check=False
            )
            return ".freedomnas" in result.stdout
        except Exception:
            return False

    def save_installation_config(self):
        """
        Persist the installation configuration to the ZFS dataset (spec §8.3 step 18).
        Writes config.yaml to /mnt/mnt/zpool/.freedomnas/config.yaml inside the chroot.
        """
        self.logger.info("Saving installation configuration to ZFS dataset...")
        try:
            from freedomnas.config import ConfigurationManager
        except ImportError:
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib', 'core'))
                from config import ConfigurationManager
            except ImportError:
                self.logger.warn("ConfigurationManager not available — skipping config save")
                return

        config_path = f"/mnt/mnt/{self.config.pool_name}/.freedomnas/config.yaml"
        try:
            cm = ConfigurationManager(config_path=config_path)
            cm.set_hostname(self.config.hostname)
            cm.add_user({
                "username": self.config.username,
                "groups": ["wheel", "samba"],
                "shell": "/bin/bash",
            })
            for user in self.config.additional_users:
                cm.add_user({
                    "username": user["username"],
                    "groups": ["wheel"] if user.get("sudo") else [],
                    "shell": "/bin/bash",
                })
            cm.add_pool({
                "name": self.config.pool_name,
                "vdev_type": "mirror",
                "ashift": 12,
                "compression": "lz4",
            })
            cm.mark_initialized(edition=self.config.os_edition, initialized_by="installer")
            self.logger.info(f"Configuration saved to: {config_path}")
        except Exception as e:
            self.logger.warn(f"Could not save installation config: {e}")

    def run(self):
        """Main installation workflow — follows spec §8.3 step order."""
        try:
            check_root()

            atexit.register(_cleanup_mounts, self.logger)
            signal.signal(signal.SIGINT, self.signal_handler)

            self.initialize_modules()

            # Step 1: check_root() — already called above

            # Recovery mode (spec §8.4): offer restore if existing pool found
            if self._detect_existing_pool():
                if self.dialog.yesno(
                    "Existing Pool Detected",
                    "An existing FreedomNAS ZFS pool was detected.\n\n"
                    "Do you want to restore from the saved configuration\n"
                    "instead of performing a fresh installation?",
                    12, 70,
                ):
                    self.logger.info("Recovery mode selected — restoring from existing config")
                    self._run_recovery_mode()
                    return

            self.show_welcome()
            self.logger.info("Starting installation process...")

            # Steps 2-3: boot mode and mirror
            self.system_installer.detect_boot_mode()
            self.system_installer.select_mirror()

            # Step 4: hostname (spec §8.3)
            self.system_installer.prompt_hostname()

            # Steps 5-7: disk
            self.disk_installer.select_disk()
            self.disk_installer.partition_disk()
            self.disk_installer.format_partitions()

            # Step 8: ZFS pool
            self.zfs_installer.create_zfs_pool()

            # Steps 9-11: base system
            self.system_installer.install_base_system()
            self.system_installer.configure_system()
            self.system_installer.setup_chroot()

            # Steps 12-13: users BEFORE packages (spec §8.3)
            self.users_installer.get_admin_password()
            self.users_installer.prompt_additional_users()

            # Step 14: packages
            self.packages_installer.install_packages()

            # Steps 15-16: bootloader
            self.bootloader_installer.install_bootloader()
            self.zfs_installer.configure_bpool_import()

            # Step 17: first-boot service (cron wrapper, spec §9.2)
            self.packages_installer.create_firstboot_service()

            # Step 18: save config.yaml to ZFS dataset (spec §8.3)
            self.save_installation_config()

            # Steps 19-20: cleanup and completion
            self.cleanup()
            self.show_completion()
            
        except Exception as e:
            self.logger.error(str(e))
            _cleanup_mounts(self.logger)
            self.dialog.msgbox("Error", f"Installation failed:\n{str(e)}", 10, 70)
            sys.exit(1)
    
    def _run_recovery_mode(self):
        """
        Import existing ZFS pools and apply config.yaml via the reconciler (spec §8.4).
        Order: hostname → pools → users → services (spec §6.4).
        """
        self.logger.info("Recovery mode: importing pools and restoring configuration...")
        try:
            from freedomnas.engine import get_engine
            from freedomnas.config import ConfigurationManager
            from freedomnas.reconcile import ReconciliationEngine
        except ImportError:
            self.logger.error("freedomnas-core not available — cannot run recovery mode")
            return

        engine = get_engine()
        cm = ConfigurationManager()
        reconciler = ReconciliationEngine(engine, cm)
        results = reconciler.reconcile(mode="disaster_recovery")

        for result in results:
            if result.target_state == "applied":
                self.logger.info(f"  OK  {result.action}")
            elif result.target_state == "failed":
                self.logger.warn(f"  FAIL {result.action}: {result.error}")
            else:
                self.logger.info(f"  SKIP {result.action}: {result.error}")

        self.dialog.msgbox(
            "Recovery Complete",
            f"Configuration restored.\n\n"
            f"Applied: {sum(1 for r in results if r.target_state == 'applied')}\n"
            f"Failed:  {sum(1 for r in results if r.target_state == 'failed')}\n\n"
            "The system is ready. You can reboot now.",
            14, 60,
        )

    def cleanup(self):
        """Clean up and unmount filesystems"""
        self.logger.info("Cleaning up...")

        try:
            # Unmount in reverse order
            for path in ['/mnt/run', '/mnt/dev/pts', '/mnt/dev', '/mnt/proc', '/mnt/sys']:
                os.system(f'umount {path} 2>/dev/null || true')

            # Unmount EFI if UEFI
            if self.config.boot_mode == "UEFI":
                os.system('umount /mnt/boot/efi 2>/dev/null || true')

            # Call comprehensive cleanup for remaining mounts
            _cleanup_mounts(self.logger)

            self.logger.info("Cleanup complete.")
        except Exception as e:
            self.logger.warn(f"Cleanup warning: {str(e)}")
    
    def show_completion(self):
        """Show installation completion message"""
        self.logger.info("=" * 70)
        self.logger.info("Installation complete!")
        self.logger.info("=" * 70)
        self.logger.info(f"Username: {self.config.username}")
        self.logger.info(f"Pool name: {self.config.pool_name}")
        self.logger.info("")
        
        message = (
            "Installation completed successfully!\n\n"
            f"Username: {self.config.username}\n"
            "Password: Set during installation\n\n"
            "You can now reboot into your new FreedomNAS system.\n\n"
            "Reboot now?"
        )
        
        if self.dialog.yesno(
            "Installation Complete",
            message,
            12, 70
        ):
            self.logger.info("Rebooting system...")
            os.system('reboot')
        else:
            self.logger.info("Reboot deferred. You can reboot manually whenever ready.")


if __name__ == '__main__':
    installer = FreedomNASInstaller()
    installer.run()
