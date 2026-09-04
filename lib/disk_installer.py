"""
FreedomNAS Installer — Disk detection, partitioning, and formatting.

Every command in this module comes from the host profile's `partitioning`
category; nothing about sgdisk, gpart, mkfs or lsblk is written here. The
layout itself is the one the image builder produces, so an installed system
and a flashed image are indistinguishable:

  Part 1 — ESP        (EFI System, FAT32) — UKI + Clover payload
  Part 2 — root pool  (ZFS)

`partitioning.list_disks` is the one command whose *output* this module
reads, so its contract is fixed by PROFILE_SPECIFICATION_v1: one disk per
line, `<name> <size-in-bytes> <model...>`. Every edition's profile is
responsible for producing that shape with its own native tooling.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from context import ExecutionContext
from installer_modules import Logger, DialogManager, Configuration, directory_exists

_MIN_DISK_BYTES = 4 * 1024 ** 3


class DiskInstaller:
    """Handles disk selection, partitioning, and partition formatting."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def select_disk(self) -> None:
        """Present a menu of block devices; store the choice in the config."""
        Logger.info("Detecting available disks...")
        disks = self._list_disks()

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
        """Wipe the disk and create the ESP + root-pool layout."""
        disk = self.config.disk
        if not disk:
            raise RuntimeError("config.disk not set — select_disk() must run first")

        Logger.info(f"Partitioning disk: {disk}")
        esp_size = self.ctx.meta("esp_size", "512MiB")

        self.ctx.host_run("partitioning", "wipe", disk=disk, check=False)
        self.ctx.host_run("partitioning", "clear", disk=disk)
        self.ctx.host_run("partitioning", "create_esp", disk=disk, esp_size=esp_size)
        self.ctx.host_run("partitioning", "create_root", disk=disk)

        if self.ctx.has("partitioning", "probe", target=False):
            self.ctx.host_run("partitioning", "probe", disk=disk, check=False)
        if self.ctx.has("partitioning", "settle", target=False):
            self.ctx.host_run("partitioning", "settle", check=False)

        self.config.part1 = self._partition_path(disk, 1)
        self.config.part2 = self._partition_path(disk, 2)
        Logger.info(f"Partitions: ESP={self.config.part1} pool={self.config.part2}")

    def format_partitions(self) -> None:
        """Format the ESP. The pool partition is handled by ZFSInstaller."""
        if not self.config.part1:
            raise RuntimeError("config.part1 not set — partition_disk() must run first")
        Logger.info(f"Formatting ESP: {self.config.part1}")
        self.ctx.host_run("partitioning", "format_esp", device=self.config.part1)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_disks(self) -> List[dict]:
        """
        Parse `partitioning.list_disks` output: one disk per line, as
        `<name> <size-in-bytes> <model...>`.
        """
        try:
            stdout, _ = self.ctx.host_run("partitioning", "list_disks", check=False, timeout=60)
        except Exception as exc:
            Logger.warn(f"Disk listing failed: {exc}")
            return []

        disks = []
        for line in stdout.strip().splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            name, size = parts[0], parts[1]
            model = parts[2].strip() if len(parts) > 2 else "Unknown"
            if name.startswith(("loop", "sr", "zram", "cd")):
                continue
            try:
                if int(size) < _MIN_DISK_BYTES:
                    continue
            except ValueError:
                continue
            disks.append({
                "device": f"/dev/{name}",
                "model": model or "Unknown",
                "size": self._human_size(size),
            })
        return disks

    @staticmethod
    def _human_size(size_bytes: str) -> str:
        try:
            value = float(size_bytes)
        except (ValueError, TypeError):
            return size_bytes
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PiB"

    @staticmethod
    def _partition_path(disk: str, number: int) -> str:
        """
        Resolve partition N of a disk.

        Naming is a kernel/device convention, not a command: /dev/sda1 but
        /dev/nvme0n1p1 and /dev/ada0p1. Prefer whatever actually appeared on
        the filesystem, and fall back to the "digit-terminated name gets a p"
        rule that every one of those conventions follows.
        """
        for candidate in (f"{disk}p{number}", f"{disk}{number}"):
            if Path(candidate).exists():
                return candidate
        return f"{disk}p{number}" if disk[-1:].isdigit() else f"{disk}{number}"

    @staticmethod
    def _resolve_by_id(device: str) -> str:
        """
        Resolve a device to a stable /dev/disk/by-id path where the platform
        provides one (Linux); elsewhere the device path is already stable.
        """
        by_id_dir = "/dev/disk/by-id"
        if not directory_exists(by_id_dir):
            return device
        for name in os.listdir(by_id_dir):
            link = os.path.join(by_id_dir, name)
            try:
                if os.path.realpath(link) == os.path.realpath(device):
                    return link
            except OSError:
                continue
        Logger.warn(f"No stable by-id path for {device}; using it directly")
        return device
