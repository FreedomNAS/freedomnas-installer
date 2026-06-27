"""
FreedomNAS Installer — GRUB bootloader installation.

Steps: install_bootloader (step 15), configure_bpool_import (step 16).

UEFI: grub-install --target=x86_64-efi
BIOS: grub-install --target=i386-pc

GRUB configuration is generated with grub-mkconfig inside the chroot.
The bpool import at boot is handled by ZFSInstaller.configure_bpool_import().
"""

from __future__ import annotations

from installer_modules import (
    Logger, DialogManager, Configuration,
    run_command, run_in_chroot, write_file,
)


class BootloaderInstaller:
    """Installs and configures GRUB inside the chroot."""

    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration) -> None:
        self.logger = logger
        self.dialog = dialog
        self.config = config

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def install_bootloader(self) -> None:
        """Install GRUB for UEFI or BIOS depending on detected boot mode."""
        Logger.info(f"Installing GRUB ({self.config.boot_mode} mode)...")

        self._write_grub_defaults()

        if self.config.boot_mode == "UEFI":
            self._install_uefi()
        else:
            self._install_bios()

        Logger.info("Generating GRUB configuration...")
        run_in_chroot(["update-grub"])
        Logger.info("GRUB installed and configured")

    def configure_bpool_import(self) -> None:
        """
        Delegate to ZFSInstaller — kept here as a named step so setup.py
        can call it at the correct position in the installation sequence.
        """
        from zfs_installer import ZFSInstaller
        zfs = ZFSInstaller(self.logger, self.dialog, self.config)
        zfs.configure_bpool_import()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_grub_defaults(self) -> None:
        """Write /etc/default/grub optimised for ZFS root."""
        content = (
            "GRUB_DEFAULT=0\n"
            "GRUB_TIMEOUT=5\n"
            'GRUB_DISTRIBUTOR="FreedomNAS"\n'
            'GRUB_CMDLINE_LINUX_DEFAULT="quiet"\n'
            'GRUB_CMDLINE_LINUX="root=ZFS=rpool/ROOT/debian"\n'
            "GRUB_TERMINAL=console\n"
        )
        write_file("/mnt/etc/default/grub", content)

    def _install_uefi(self) -> None:
        # Mount EFI partition
        run_command(["mkdir", "-p", "/mnt/boot/efi"])
        if self.config.part2:
            run_command(["mount", self.config.part2, "/mnt/boot/efi"])

        run_in_chroot([
            "grub-install",
            "--target=x86_64-efi",
            "--efi-directory=/boot/efi",
            "--bootloader-id=FreedomNAS",
            "--recheck",
            "--no-floppy",
        ])

    def _install_bios(self) -> None:
        run_in_chroot([
            "grub-install",
            "--target=i386-pc",
            "--recheck",
            "--no-floppy",
            self.config.disk,
        ])
