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
from typing import Dict, List, Tuple

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
        """
        Present a menu of block devices; store the choice in the config.

        A disk that belongs to an imported pool is refused: it is in use, most
        likely by the system running the installer. A disk carrying the label
        of a pool that is not imported (exported, another system's, an older
        install) is only accepted after the admin confirms, by name, that the
        pool may be destroyed.
        """
        Logger.info("Detecting available disks...")
        disks = self._list_disks()
        imported = self._imported_pool_devices()

        items = []
        for d in disks:
            pools = sorted({pool for _, pool in self._disk_pools(d["device"])})
            label = f"{d['model']} ({d['size']})"
            if pools:
                label += f" [pool: {', '.join(pools)}]"
            items.append((d["device"], label))
        items.append(("CUSTOM", "Enter disk path manually"))

        while True:
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

            if self._accept_disk(choice, imported):
                break

        self.config.disk = choice
        self.config.disk_by_id = self._resolve_by_id(choice)
        Logger.info(f"Selected disk: {self.config.disk} ({self.config.disk_by_id})")

    def _accept_disk(self, disk: str, imported: Dict[str, str]) -> bool:
        # Both sides as real paths: zpool status -L resolves /dev/mapper/x to
        # /dev/dm-N, while lsblk keeps the mapper name.
        devices = {os.path.realpath(dev) for dev in self._disk_devices(disk)}
        busy = sorted({pool for dev, pool in imported.items() if os.path.realpath(dev) in devices})
        if busy:
            Logger.warn(f"{disk} belongs to imported pool(s): {', '.join(busy)}")
            self.dialog.msgbox(
                "Disk In Use",
                f"{disk} is part of the imported pool(s):\n\n    {', '.join(busy)}\n\n"
                "It is in use and cannot be erased. Export the pool first, "
                "or choose another disk.",
                13, 70,
            )
            return False

        foreign = self._disk_pools(disk)
        self.config.foreign_pool_devices = []
        if not foreign:
            return True

        listing = "\n".join(f"    {pool}  ({dev})" for dev, pool in foreign)
        if not self.dialog.defaultno(
            "Existing ZFS Pool",
            f"{disk} carries ZFS pool data that is not imported:\n\n{listing}\n\n"
            "Installing here destroys these pools for good.\n"
            "Use this disk anyway?",
            16, 74,
        ):
            return False

        Logger.warn(f"Destroying pool data on {disk}: {', '.join(p for _, p in foreign)}")
        self.config.foreign_pool_devices = [dev for dev, _ in foreign]
        return True

    def _disk_devices(self, disk: str) -> List[str]:
        """The disk and its partitions, as `partitioning.disk_pools` names them."""
        return [dev for dev, _, _ in self._disk_members(disk)] or [disk]

    def _disk_pools(self, disk: str) -> List[Tuple[str, str]]:
        """(device, pool name) for every device on the disk carrying a ZFS label."""
        return [(dev, label or "(unnamed)") for dev, fstype, label in self._disk_members(disk)
                if fstype == "zfs_member"]

    def _disk_members(self, disk: str) -> List[Tuple[str, str, str]]:
        """
        Parse `partitioning.disk_pools`: one device per line, `<device>
        [<fstype> [<label>]]`, with spaces inside a label escaped as \\x20
        (lsblk's raw format). A ZFS member's label is its pool's name.
        """
        if not self.ctx.has("partitioning", "disk_pools", target=False):
            return []
        try:
            stdout, _ = self.ctx.host_run(
                "partitioning", "disk_pools", check=False, timeout=60, disk=disk
            )
        except Exception as exc:
            Logger.warn(f"Could not read the filesystems on {disk}: {exc}")
            return []
        members = []
        for line in stdout.splitlines():
            fields = line.split(" ")
            if not fields or not fields[0]:
                continue
            fstype = fields[1] if len(fields) > 1 else ""
            label = fields[2] if len(fields) > 2 else ""
            label = label.encode().decode("unicode_escape") if "\\x" in label else label
            members.append((fields[0], fstype, label))
        return members

    def _imported_pool_devices(self) -> Dict[str, str]:
        """
        Map every device used by an imported pool to that pool's name, from
        `rootpool.imported_devices` (`zpool status -LP`: a `pool:` line, then
        the vdev tree with resolved /dev paths).
        """
        if not self.ctx.has("rootpool", "imported_devices", target=False):
            return {}
        try:
            stdout, _ = self.ctx.host_run("rootpool", "imported_devices", check=False, timeout=60)
        except Exception as exc:
            Logger.warn(f"Could not list the devices of imported pools: {exc}")
            return {}
        devices: Dict[str, str] = {}
        pool = ""
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pool:"):
                pool = stripped.split(":", 1)[1].strip()
            elif pool and stripped.startswith("/dev/"):
                devices[stripped.split()[0]] = pool
        return devices

    def partition_disk(self) -> None:
        """Wipe the disk and create the ESP + root-pool layout."""
        disk = self.config.disk
        if not disk:
            raise RuntimeError("config.disk not set — select_disk() must run first")

        Logger.info(f"Partitioning disk: {disk}")
        esp_size = self.ctx.meta("esp_size", "1GiB")

        # sgdisk --zap-all removes the partition table, not the ZFS labels
        # inside the old partitions: left there, they make zpool create refuse
        # the new partition and can surface as ghost pools in later imports.
        if self.ctx.has("partitioning", "labelclear", target=False):
            for device in self.config.foreign_pool_devices:
                Logger.info(f"Clearing the ZFS label on {device}")
                self.ctx.host_run("partitioning", "labelclear", device=device, check=False)

        self.ctx.host_run("partitioning", "wipe", disk=disk, check=False)
        self.ctx.host_run("partitioning", "clear", disk=disk)
        self.ctx.host_run("partitioning", "create_esp", disk=disk, esp_size=esp_size)
        # BIOS boot code, for editions whose firmware path needs its own
        # partition (FreeBSD's gptzfsboot). Before the root partition, which
        # takes the rest of the disk; the profile gives it an index after the
        # pool's, so the ESP stays partition 1 and the pool partition 2.
        if self.ctx.has("partitioning", "create_bios_boot", target=False):
            self.ctx.host_run("partitioning", "create_bios_boot", disk=disk)
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
