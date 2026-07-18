"""
FreedomNAS Installer — Boot chain installation.

Steps: install_bootloader (step 15).

Every machine gets a single dracut-built Unified Kernel Image (UKI) —
kernel + initramfs + cmdline bundled into one PE binary — written to the
EFI System Partition. How it gets loaded then splits by firmware:

  UEFI  — registered directly via efibootmgr. No third-party bootloader.
  BIOS  — Clover (legacy-BIOS-to-UEFI-emulation, boot7/BiosBlockIO DUET
          variant) bridges BIOS -> UEFI emulation -> UKI. Validated on
          real hardware; see assets/clover/README.md for provenance and
          why the vendored release must not be bumped without re-validating.

The two OS commands this needs (dracut, efibootmgr) are resolved through
freedomnas.engine.get_command() against freedomnas-profiles/debian.json's
"bootloader" category — never hardcoded here (Golden Rule) — then executed
via this module's own run_in_chroot(), since engine.execute() has no
chroot concept.

Deploying the Clover binaries themselves (boot0/boot1 boot sectors, the
CLOVERX64.efi/BOOTX64.efi payload, config.plist) is pure file I/O, not a
shell command, so it needs no profile entry — same precedent as
write_file() for GRUB's own config previously.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

from installer_modules import (
    Logger, DialogManager, Configuration,
    run_command, run_in_chroot, write_file,
)

_LIB_DIR = Path(__file__).parent
_ASSETS_DIR = _LIB_DIR.parent / "assets"
_CLOVER_DIR = _ASSETS_DIR / "clover"
_ZZ_DRACUT_UKI = _ASSETS_DIR / "zz-dracut-uki"

_ROOT_DATASET = "rpool/ROOT/debian"

# Host-side mountpoint of the ESP during install (also relied on by
# setup.py's cleanup, which unmounts this same path).
_ESP_MOUNT = "/mnt/boot/efi"

# Chroot-relative paths (as dracut/efibootmgr see them once inside /mnt).
_UKI_CHROOT_PATH = "/boot/efi/EFI/Linux/debian-uki.efi"
_UKI_UEFI_LOADER_PATH = "/EFI/Linux/debian-uki.efi"


class BootloaderInstaller:
    """Builds the UKI and installs the appropriate boot chain (efibootmgr or Clover)."""

    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration) -> None:
        self.logger = logger
        self.dialog = dialog
        self.config = config

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def install_bootloader(self) -> None:
        """Build the UKI and wire up the boot chain for the detected firmware."""
        Logger.info(f"Installing boot chain ({self.config.boot_mode} mode)...")

        self._mount_esp()
        self._build_uki()
        self._deploy_kernel_hook()

        if self.config.boot_mode == "UEFI":
            self._register_efi_boot_entry()
        else:
            self._deploy_clover()

        Logger.info("Boot chain installed")

    # ------------------------------------------------------------------
    # UKI build (both boot modes)
    # ------------------------------------------------------------------

    def _mount_esp(self) -> None:
        run_command(["mkdir", "-p", f"{_ESP_MOUNT}/EFI/Linux"])
        if self.config.part1:
            run_command(["mount", self.config.part1, _ESP_MOUNT])

    def _detect_kernel_version(self) -> str:
        """Read the installed kernel version from the chroot's /lib/modules."""
        modules_dir = Path("/mnt/lib/modules")
        versions = sorted(p.name for p in modules_dir.iterdir()) if modules_dir.is_dir() else []
        if not versions:
            raise RuntimeError(
                "No kernel found under /mnt/lib/modules — was linux-image-amd64 installed?"
            )
        return versions[-1]

    def _build_uki(self) -> None:
        from freedomnas.engine import get_engine

        kernel_version = self._detect_kernel_version()
        engine = get_engine()
        cmd = engine.get_command(
            "bootloader", "build_uki",
            dataset=_ROOT_DATASET,
            output_path=_UKI_CHROOT_PATH,
            kernel_version=kernel_version,
        )
        Logger.info(f"Building UKI for kernel {kernel_version}...")
        run_in_chroot(shlex.split(cmd))

    def _deploy_kernel_hook(self) -> None:
        """Deploy zz-dracut-uki so future kernel updates rebuild the UKI automatically."""
        dest = Path("/mnt/etc/kernel/postinst.d/zz-dracut-uki")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_ZZ_DRACUT_UKI, dest)
        os.chmod(dest, 0o755)

    # ------------------------------------------------------------------
    # UEFI boot chain — efibootmgr only
    # ------------------------------------------------------------------

    def _register_efi_boot_entry(self) -> None:
        from freedomnas.engine import get_engine

        engine = get_engine()
        cmd = engine.get_command(
            "bootloader", "efi_boot_entry",
            disk=self.config.disk,
            part_num=1,
            loader_path=_UKI_UEFI_LOADER_PATH,
            label="FreedomNAS",
        )
        Logger.info("Registering UEFI boot entry via efibootmgr...")
        run_in_chroot(shlex.split(cmd))

    # ------------------------------------------------------------------
    # BIOS boot chain — Clover (boot0/boot1/boot7 + CLOVERX64.efi)
    # ------------------------------------------------------------------

    def _deploy_clover(self) -> None:
        """
        Deploy the validated Clover boot chain onto the disk and ESP.
        Pure file I/O — no shell commands, no profile entry needed.
        """
        Logger.info("Deploying Clover boot chain (BIOS mode)...")
        self._write_boot0()
        self._write_boot1()
        self._copy_clover_payload()
        self._write_clover_config()
        Logger.info("Clover boot chain deployed")

    def _write_boot0(self) -> None:
        """
        Write boot0's first 440 bytes to the MBR — the bootstrap code area
        only, preserving the disk's existing signature, reserved bytes,
        partition table, and boot signature (bytes 440-511).
        """
        if not self.config.disk:
            raise RuntimeError("config.disk not set")
        boot0_data = (_CLOVER_DIR / "boot0ss").read_bytes()
        with open(self.config.disk, "r+b") as disk:
            disk.write(boot0_data[:440])

    def _write_boot1(self) -> None:
        """
        Write boot1f32alt to the ESP's own boot sector, merging in the
        original FAT32 BIOS Parameter Block (bytes 3-89) so the volume
        stays correctly described after the boot code is replaced.
        """
        if not self.config.part1:
            raise RuntimeError("config.part1 not set")
        boot1_data = bytearray((_CLOVER_DIR / "boot1f32alt").read_bytes())
        with open(self.config.part1, "r+b") as esp:
            original_sector = esp.read(512)
            boot1_data[3:90] = original_sector[3:90]
            esp.seek(0)
            esp.write(bytes(boot1_data))

    def _copy_clover_payload(self) -> None:
        boot_dir = Path(_ESP_MOUNT) / "boot"
        clover_dir = Path(_ESP_MOUNT) / "EFI" / "CLOVER"
        bootx64_dir = Path(_ESP_MOUNT) / "EFI" / "BOOT"
        for d in (boot_dir, clover_dir, bootx64_dir):
            d.mkdir(parents=True, exist_ok=True)

        shutil.copy2(_CLOVER_DIR / "boot7", boot_dir / "boot7")
        shutil.copy2(_CLOVER_DIR / "CLOVERX64.efi", clover_dir / "CLOVERX64.efi")
        shutil.copy2(_CLOVER_DIR / "BOOTX64.efi", bootx64_dir / "BOOTX64.efi")

    def _write_clover_config(self) -> None:
        """
        config.plist validated on real hardware: DefaultVolume/DefaultLoader/
        IgnoreNVRAMBoot (plus Scan->Entries=true) rather than relying solely
        on Custom->Entries, to work around a real boot7 bug where a
        Custom-only entry didn't reliably auto-load on boot.
        """
        content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Boot</key>
    <dict>
        <key>Timeout</key>
        <integer>0</integer>
        <key>Fast</key>
        <true/>
        <key>DefaultVolume</key>
        <string>EFI</string>
        <key>DefaultLoader</key>
        <string>\\EFI\\Linux\\debian-uki.efi</string>
        <key>IgnoreNVRAMBoot</key>
        <true/>
    </dict>
    <key>GUI</key>
    <dict>
        <key>Scan</key>
        <dict>
            <key>Entries</key>
            <true/>
            <key>Legacy</key>
            <false/>
            <key>Tool</key>
            <false/>
        </dict>
        <key>Custom</key>
        <dict>
            <key>Entries</key>
            <array>
                <dict>
                    <key>Volume</key>
                    <string>EFI</string>
                    <key>Path</key>
                    <string>\\EFI\\Linux\\debian-uki.efi</string>
                    <key>Type</key>
                    <string>Linux</string>
                    <key>Title</key>
                    <string>FreedomNAS</string>
                </dict>
            </array>
        </dict>
    </dict>
    <key>Debug</key>
    <false/>
</dict>
</plist>
"""
        write_file(f"{_ESP_MOUNT}/EFI/CLOVER/config.plist", content)
