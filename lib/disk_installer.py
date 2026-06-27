"""
FreedomNAS Installer — Disk detection, partitioning, and formatting.

Partitioning scheme (spec §8.6):
  UEFI:
    Part 1 — 1 MiB  — BIOS-boot  (sgdisk type EF02, for hybrid MBR)
    Part 2 — 512 MiB — EFI System (sgdisk type EF00, FAT32)
    Part 3 — 2 GiB  — bpool      (sgdisk type BF01, ZFS)
    Part 4 — rest   — rpool      (sgdisk type BF00, ZFS)

  BIOS:
    Part 1 — 1 MiB  — BIOS-boot  (sgdisk type EF02)
    Part 2 — 2 GiB  — bpool      (sgdisk type BF01)
    Part 3 — rest   — rpool      (sgdisk type BF00)

Always uses /dev/disk/by-id paths when creating ZFS pools (spec §8.6).
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional

from installer_modules import (
    Logger, DialogManager, Configuration,
    run_command, write_file, directory_exists,
)


class DiskInstaller:
    """Handles disk selection, partitioning, and partition formatting."""

    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration) -> None:
        self.logger = logger
        self.dialog = dialog
        self.config = config

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def select_disk(self) -> None:
        """
        Present a menu of available block devices and store the selected disk
        in config.disk and config.disk_by_id.
        """
        Logger.info("Detecting available disks...")
        disks = self._list_disks()

        if not disks:
            raise RuntimeError("No suitable block devices found")

        items = [(d["device"], f"{d['model']} ({d['size']})") for d in disks]
        items.append(("CUSTOM", "Enter disk path manually"))

        choice = self.dialog.menu(
            "Select Installation Disk",
            "WARNING: the selected disk will be COMPLETELY ERASED.\n"
            "Select the disk to install FreedomNAS on:",
            items,
            height=22,
            width=78,
        )

        if not choice:
            raise RuntimeError("No disk selected — installation aborted")

        if choice == "CUSTOM":
            custom = self.dialog.inputbox(
                "Custom Disk",
                "Enter the full device path (e.g. /dev/sda):",
                height=10,
                width=60,
            )
            if not custom:
                raise RuntimeError("No disk entered — installation aborted")
            choice = custom.strip()

        self.config.disk = choice
        self.config.disk_by_id = self._resolve_by_id(choice)
        Logger.info(f"Selected disk: {self.config.disk} ({self.config.disk_by_id})")

    def partition_disk(self) -> None:
        """Wipe and repartition the selected disk using sgdisk."""
        disk = self.config.disk
        Logger.info(f"Partitioning disk: {disk}")

        # Wipe existing partition tables
        run_command(["sgdisk", "--zap-all", disk])
        run_command(["sgdisk", "--clear", disk])

        if self.config.boot_mode == "UEFI":
            self._partition_uefi(disk)
        else:
            self._partition_bios(disk)

        # Force kernel to re-read partition table
        run_command(["partprobe", disk], check=False)
        run_command(["udevadm", "settle"])
        Logger.info("Disk partitioned successfully")

    def format_partitions(self) -> None:
        """Format the EFI partition (UEFI only). ZFS partitions are formatted by ZFSInstaller."""
        if self.config.boot_mode != "UEFI":
            Logger.info("BIOS mode — no EFI partition to format")
            return

        efi_part = self.config.part2
        Logger.info(f"Formatting EFI partition: {efi_part}")
        run_command(["mkfs.vfat", "-F", "32", "-n", "EFI", efi_part])
        Logger.info("EFI partition formatted as FAT32")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_disks(self) -> List[dict]:
        """Return a list of dicts with {device, model, size} for block devices."""
        try:
            result = run_command(
                ["lsblk", "-d", "-o", "NAME,SIZE,MODEL", "--noheadings", "--bytes"],
                check=False,
            )
            disks = []
            for line in result.stdout.strip().splitlines():
                parts = line.split(None, 2)
                if len(parts) < 2:
                    continue
                name = parts[0]
                size = parts[1]
                model = parts[2].strip() if len(parts) > 2 else "Unknown"
                device = f"/dev/{name}"
                # Skip loop devices, CDROMs, and very small disks (< 4 GiB)
                if name.startswith("loop") or name.startswith("sr"):
                    continue
                try:
                    if int(size) < 4 * 1024 ** 3:
                        continue
                except ValueError:
                    pass
                disks.append({"device": device, "model": model, "size": self._human_size(size)})
            return disks
        except Exception as exc:
            Logger.warn(f"lsblk failed: {exc}")
            return []

    def _human_size(self, size_bytes: str) -> str:
        """Convert bytes string to human-readable size."""
        try:
            b = int(size_bytes)
            for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
                if b < 1024:
                    return f"{b:.1f} {unit}"
                b //= 1024
            return f"{b:.1f} PiB"
        except (ValueError, TypeError):
            return size_bytes

    def _resolve_by_id(self, device: str) -> str:
        """
        Resolve a device path to its /dev/disk/by-id equivalent.
        Falls back to the original path if no by-id link is found.
        """
        by_id_dir = "/dev/disk/by-id"
        if not directory_exists(by_id_dir):
            return device
        for name in os.listdir(by_id_dir):
            link = os.path.join(by_id_dir, name)
            try:
                target = os.path.realpath(link)
                if target == os.path.realpath(device):
                    return link
            except OSError:
                continue
        Logger.warn(f"No /dev/disk/by-id symlink found for {device}, using path directly")
        return device

    def _partition_uefi(self, disk: str) -> None:
        """Create 4-partition UEFI layout."""
        # Part 1: BIOS-boot (1 MiB, EF02)
        run_command(["sgdisk", "-n", "1:1MiB:+1MiB", "-t", "1:EF02", disk])
        # Part 2: EFI System (512 MiB, EF00)
        run_command(["sgdisk", "-n", "2:0:+512MiB", "-t", "2:EF00", disk])
        # Part 3: bpool (2 GiB, BF01)
        run_command(["sgdisk", "-n", "3:0:+2GiB", "-t", "3:BF01", disk])
        # Part 4: rpool (rest, BF00)
        run_command(["sgdisk", "-n", "4:0:-0", "-t", "4:BF00", disk])

        self.config.part1 = f"{disk}1"
        self.config.part2 = f"{disk}2"
        self.config.part3 = f"{disk}3"
        self.config.part4 = f"{disk}4"

    def _partition_bios(self, disk: str) -> None:
        """Create 3-partition BIOS legacy layout."""
        # Part 1: BIOS-boot (1 MiB, EF02)
        run_command(["sgdisk", "-n", "1:1MiB:+1MiB", "-t", "1:EF02", disk])
        # Part 2: bpool (2 GiB, BF01)
        run_command(["sgdisk", "-n", "2:0:+2GiB", "-t", "2:BF01", disk])
        # Part 3: rpool (rest, BF00)
        run_command(["sgdisk", "-n", "3:0:-0", "-t", "3:BF00", disk])

        self.config.part1 = f"{disk}1"
        self.config.part2 = None
        self.config.part3 = f"{disk}2"
        self.config.part4 = f"{disk}3"
