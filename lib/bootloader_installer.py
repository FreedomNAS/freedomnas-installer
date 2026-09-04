"""
FreedomNAS Installer — boot chain.

Which boot chain gets installed is decided by what the target profile
declares, not by an `if edition ==` in Python:

  bootloader.build_uki        → Linux: one dracut-built Unified Kernel Image
                                (kernel + initramfs + cmdline in a single PE
                                binary) on the ESP, plus the zz-dracut-uki
                                hook so future kernel updates rebuild it
  bootloader.install_efi_loader → FreeBSD: the native loader, which reads
                                ZFS directly and needs no UKI at all

On top of that, firmware decides how the ESP gets entered:

  UEFI  — `bootloader.efi_boot_entry` (efibootmgr) registers the loader in
          NVRAM
  BIOS  — the vendored, hardware-validated Clover chain (boot0/boot1/boot7)
          bridges legacy BIOS into UEFI emulation. This is pure byte I/O on
          the MBR and the ESP's boot sector, so it has no profile entry —
          same precedent as writing /etc/fstab.

The Clover binaries and zz-dracut-uki live in freedomnas-profiles/assets/,
shared with freedomnas-image-builder rather than vendored twice. Do not bump
the Clover release without re-validating on hardware (see its README).
"""

from __future__ import annotations

import shutil
from pathlib import Path
import corelink
from context import ExecutionContext
from installer_modules import Logger, DialogManager, Configuration, write_file

_ESP_IN_TARGET = "/boot/efi"
_UKI_DIR = "/EFI/Linux"


class BootloaderInstaller:
    """Builds the boot chain for the target edition and the detected firmware."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx
        self.assets = corelink.find_assets_dir()

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def install_bootloader(self) -> None:
        Logger.info(f"Installing boot chain ({self.config.boot_mode})...")
        self.mount_esp()

        if self.ctx.has("bootloader", "build_uki"):
            self._build_uki()
            self._deploy_kernel_hook()
            loader_path = f"{_UKI_DIR}/{self.ctx.meta('uki_name', 'freedomnas-uki.efi')}"
        else:
            self._install_native_loader()
            loader_path = "/EFI/BOOT/BOOTX64.efi"

        if self.config.boot_mode == "UEFI":
            self._register_efi_boot_entry(loader_path)
        else:
            self._deploy_bios_chain()

        Logger.info("Boot chain installed")

    def mount_esp(self) -> None:
        self.ctx.ensure_target_dir(f"{_ESP_IN_TARGET}{_UKI_DIR}")
        if self.config.part1:
            self.ctx.host_run(
                "partitioning", "mount_esp",
                device=self.config.part1,
                mountpoint=str(self.ctx.target_path(_ESP_IN_TARGET)),
            )

    def unmount_esp(self) -> None:
        self.ctx.host_run(
            "partitioning", "unmount", check=False,
            mountpoint=str(self.ctx.target_path(_ESP_IN_TARGET)),
        )

    # ------------------------------------------------------------------
    # Linux — UKI
    # ------------------------------------------------------------------

    def _detect_kernel_version(self) -> str:
        modules_dir = self.ctx.target_path("/lib/modules")
        versions = sorted(p.name for p in modules_dir.iterdir()) if modules_dir.is_dir() else []
        if not versions:
            if self.ctx.dry_run:
                return "0.0.0-dry-run"
            raise RuntimeError(
                f"No kernel found under {modules_dir} — was the kernel package installed?"
            )
        return versions[-1]

    def _build_uki(self) -> None:
        kernel_version = self._detect_kernel_version()
        uki_name = self.ctx.meta("uki_name", "freedomnas-uki.efi")
        Logger.info(f"Building UKI for kernel {kernel_version}...")
        self.ctx.target_run(
            "bootloader", "build_uki",
            timeout=900,
            dataset=self.config.root_dataset,
            output_path=f"{_ESP_IN_TARGET}{_UKI_DIR}/{uki_name}",
            kernel_version=kernel_version,
        )

        if self.ctx.has("bootloader", "build_uki_recovery"):
            recovery_name = self.ctx.meta("uki_recovery_name", "")
            if recovery_name:
                Logger.info("Building recovery UKI (read-only, single user)...")
                self.ctx.target_run(
                    "bootloader", "build_uki_recovery",
                    timeout=900, check=False,
                    dataset=self.config.root_dataset,
                    output_path=f"{_ESP_IN_TARGET}{_UKI_DIR}/{recovery_name}",
                    kernel_version=kernel_version,
                )

    def _deploy_kernel_hook(self) -> None:
        """Rebuild the UKI automatically on every future kernel update."""
        source = self.assets / "zz-dracut-uki"
        if not source.is_file():
            Logger.warn(f"Kernel hook not found at {source} — skipping")
            return
        dest = self.ctx.target_path("/etc/kernel/postinst.d/zz-dracut-uki")
        Logger.command(f"[file] {dest}")
        if self.ctx.dry_run:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        dest.chmod(0o755)

    # ------------------------------------------------------------------
    # FreeBSD — native loader
    # ------------------------------------------------------------------

    def _install_native_loader(self) -> None:
        """Editions whose loader reads ZFS directly need no UKI."""
        self.ctx.ensure_target_dir(f"{_ESP_IN_TARGET}/EFI/BOOT")
        if self.ctx.has("bootloader", "install_efi_loader"):
            self.ctx.target_run("bootloader", "install_efi_loader", esp_mount=_ESP_IN_TARGET)
        if self.ctx.has("bootloader", "install_gptzfsboot") and self.config.boot_mode == "BIOS":
            self.ctx.target_run(
                "bootloader", "install_gptzfsboot",
                part_num=2, disk=self.config.disk,
            )

    # ------------------------------------------------------------------
    # Firmware entry points
    # ------------------------------------------------------------------

    def _register_efi_boot_entry(self, loader_path: str) -> None:
        if not self.ctx.has("bootloader", "efi_boot_entry"):
            Logger.warn("Profile declares no efi_boot_entry — relying on the ESP fallback path")
            return
        Logger.info("Registering UEFI boot entry...")
        self.ctx.target_run(
            "bootloader", "efi_boot_entry",
            check=False,
            disk=self.config.disk,
            part_num=1,
            loader_path=loader_path,
            label="FreedomNAS",
        )

    def _deploy_bios_chain(self) -> None:
        """
        BIOS machines: deploy the validated Clover chain. Only meaningful for
        editions that boot through a UKI — an edition with a native
        BIOS-capable loader installs it in _install_native_loader() instead.
        """
        if not self.ctx.has("bootloader", "build_uki"):
            Logger.info("Native BIOS loader already installed — no Clover needed")
            return

        clover_dir = self.assets / "clover"
        if not clover_dir.is_dir():
            raise RuntimeError(f"Clover assets not found at {clover_dir}")

        Logger.info("Deploying Clover boot chain (BIOS mode)...")
        if self.ctx.dry_run:
            Logger.command(f"[file] boot0 -> {self.config.disk}, boot1 -> {self.config.part1}")
            return

        self._write_boot0(clover_dir)
        self._write_boot1(clover_dir)
        self._copy_clover_payload(clover_dir)
        self._write_clover_config()
        Logger.info("Clover boot chain deployed")

    def _write_boot0(self, clover_dir: Path) -> None:
        """
        Write boot0's first 440 bytes to the MBR — the bootstrap code area
        only, preserving the disk signature, partition table and boot
        signature at bytes 440-511.
        """
        if not self.config.disk:
            raise RuntimeError("config.disk not set")
        payload = (clover_dir / "boot0ss").read_bytes()
        with open(self.config.disk, "r+b") as disk:
            disk.write(payload[:440])

    def _write_boot1(self, clover_dir: Path) -> None:
        """
        Write boot1f32alt to the ESP's own boot sector, merging in the
        original FAT32 BIOS Parameter Block (bytes 3-89) so the volume stays
        correctly described.
        """
        if not self.config.part1:
            raise RuntimeError("config.part1 not set")
        payload = bytearray((clover_dir / "boot1f32alt").read_bytes())
        with open(self.config.part1, "r+b") as esp:
            original = esp.read(512)
            payload[3:90] = original[3:90]
            esp.seek(0)
            esp.write(bytes(payload))

    def _copy_clover_payload(self, clover_dir: Path) -> None:
        esp = self.ctx.target_path(_ESP_IN_TARGET)
        for subdir in ("boot", "EFI/CLOVER", "EFI/BOOT"):
            (esp / subdir).mkdir(parents=True, exist_ok=True)
        shutil.copy2(clover_dir / "boot7", esp / "boot" / "boot7")
        shutil.copy2(clover_dir / "CLOVERX64.efi", esp / "EFI" / "CLOVER" / "CLOVERX64.efi")
        shutil.copy2(clover_dir / "BOOTX64.efi", esp / "EFI" / "BOOT" / "BOOTX64.efi")

    def _write_clover_config(self) -> None:
        """
        config.plist validated on real hardware: DefaultVolume/DefaultLoader/
        IgnoreNVRAMBoot plus Scan->Entries=true, rather than relying solely on
        Custom->Entries — a boot7 bug makes a Custom-only entry fail to
        auto-load on some machines.
        """
        uki_name = self.ctx.meta("uki_name", "freedomnas-uki.efi")
        loader = f"\\EFI\\Linux\\{uki_name}"
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
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
        <string>{loader}</string>
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
                    <string>{loader}</string>
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
        write_file(str(self.ctx.target_path(f"{_ESP_IN_TARGET}/EFI/CLOVER/config.plist")), content)
