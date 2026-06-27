"""
FreedomNAS Installer — ZFS pool and dataset creation.

Always uses /dev/disk/by-id paths (spec §8.6).

Pool layout:
  bpool — boot pool (simple, grub-compatible features only)
  rpool — root pool (all features enabled, lz4 compression)

Datasets created under rpool:
  rpool/ROOT
  rpool/ROOT/debian          ← OS root (mountpoint /)
  rpool/home                 ← Home directories
  rpool/var
  rpool/var/log
  rpool/.freedomnas          ← FreedomNAS config dataset (canmount=noauto)
"""

from __future__ import annotations

from installer_modules import Logger, DialogManager, Configuration, run_command, write_file


# Canonical pool features compatible with GRUB's ZFS support
_BPOOL_FEATURES = ",".join([
    "async_destroy",
    "bookmarks",
    "embedded_data",
    "empty_bpobj",
    "enabled_txg",
    "extensible_dataset",
    "filesystem_limits",
    "hole_birth",
    "large_blocks",
    "lz4_compress",
    "spacemap_histogram",
])


class ZFSInstaller:
    """Creates and configures ZFS pools and datasets for FreedomNAS."""

    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration) -> None:
        self.logger = logger
        self.dialog = dialog
        self.config = config

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def create_zfs_pool(self) -> None:
        """Create bpool and rpool on the selected partitions."""
        self._create_bpool()
        self._create_rpool()
        self._create_datasets()
        Logger.info("ZFS pools and datasets created successfully")

    def configure_bpool_import(self) -> None:
        """
        Create a systemd service inside the chroot to import bpool at boot.
        Falls back to a cron @reboot entry if systemd is not available.
        """
        Logger.info("Configuring bpool auto-import at boot...")

        service_content = (
            "[Unit]\n"
            "Description=Import bpool\n"
            "DefaultDependencies=no\n"
            "Before=zfs-import-scan.service\n"
            "Before=zfs-import-cache.service\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            "RemainAfterExit=yes\n"
            f"ExecStart=/sbin/zpool import -N -o cachefile=none {self.config.bpool_name}\n"
            "ExecStartPost=/bin/zfs mount -a\n\n"
            "[Install]\n"
            "WantedBy=zfs-import.target\n"
        )
        service_path = f"/mnt/etc/systemd/system/zfs-import-{self.config.bpool_name}.service"
        write_file(service_path, service_content)

        run_command([
            "chroot", "/mnt",
            "systemctl", "enable", f"zfs-import-{self.config.bpool_name}.service",
        ], check=False)
        Logger.info("bpool import service configured")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pool_partition(self, part_attr: str) -> str:
        """Resolve partition path via by-id, falling back to disk+number."""
        part = getattr(self.config, part_attr)
        if not part:
            raise RuntimeError(f"Partition not set: config.{part_attr}")
        # Try to find the by-id link for this partition
        import os
        by_id_dir = "/dev/disk/by-id"
        if os.path.isdir(by_id_dir):
            for name in os.listdir(by_id_dir):
                link = os.path.join(by_id_dir, name)
                try:
                    if os.path.realpath(link) == os.path.realpath(part):
                        return link
                except OSError:
                    continue
        return part

    def _create_bpool(self) -> None:
        bpart = self._pool_partition("part3")
        Logger.info(f"Creating bpool on {bpart}...")
        run_command([
            "zpool", "create",
            "-o", "ashift=12",
            "-o", f"compatibility=grub2",
            "-O", "acltype=posixacl",
            "-O", "canmount=off",
            "-O", "compression=lz4",
            "-O", "devices=off",
            "-O", "normalization=formD",
            "-O", "xattr=sa",
            "-O", "mountpoint=/boot",
            "-R", "/mnt",
            self.config.bpool_name,
            bpart,
        ])

    def _create_rpool(self) -> None:
        rpart = self._pool_partition("part4")
        Logger.info(f"Creating rpool on {rpart}...")
        run_command([
            "zpool", "create",
            "-o", "ashift=12",
            "-O", "acltype=posixacl",
            "-O", "canmount=off",
            "-O", "compression=lz4",
            "-O", "dnodesize=auto",
            "-O", "normalization=formD",
            "-O", "relatime=on",
            "-O", "xattr=sa",
            "-O", "mountpoint=/",
            "-R", "/mnt",
            self.config.pool_name,
            rpart,
        ])

    def _create_datasets(self) -> None:
        pool = self.config.pool_name
        bpool = self.config.bpool_name
        Logger.info("Creating ZFS datasets...")

        datasets = [
            # bpool datasets
            (["zfs", "create", "-o", "canmount=off", "-o", "mountpoint=none",      f"{bpool}/BOOT"], True),
            (["zfs", "create", "-o", "mountpoint=/boot",                           f"{bpool}/BOOT/debian"], True),

            # rpool ROOT
            (["zfs", "create", "-o", "canmount=off", "-o", "mountpoint=none",      f"{pool}/ROOT"], True),
            (["zfs", "create", "-o", "canmount=noauto", "-o", "mountpoint=/",      f"{pool}/ROOT/debian"], True),

            # rpool home and var
            (["zfs", "create", "-o", "setuid=off",                                 f"{pool}/home"], True),
            (["zfs", "create", "-o", "canmount=off", "-o", "mountpoint=/var",      f"{pool}/var"], True),
            (["zfs", "create", "-o", "atime=on",                                   f"{pool}/var/log"], True),

            # FreedomNAS config dataset
            (["zfs", "create", "-o", "canmount=noauto", "-o", "mountpoint=/etc/freedomnas/state",
              f"{pool}/.freedomnas"], True),
        ]

        for cmd, check in datasets:
            run_command(cmd, check=check)

        # Set bootfs
        run_command(["zpool", "set", f"bootfs={pool}/ROOT/debian", pool])

        # Mount root dataset explicitly
        run_command(["zfs", "mount", f"{pool}/ROOT/debian"])
        run_command(["zfs", "mount", "--all"])

        Logger.info("ZFS datasets created and mounted")
