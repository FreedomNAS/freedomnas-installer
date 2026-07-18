"""
FreedomNAS Installer — ZFS pool and dataset creation.

Always uses /dev/disk/by-id paths.

A single, full-featured rpool — no separate bpool. That split only ever
existed because GRUB's built-in ZFS reader can only parse a restricted
feature set; nothing at boot time reads ZFS directly anymore (the UKI's
own dracut-built initramfs mounts root via the kernel's full-featured ZFS
driver), so there's nothing left that needs a feature-limited pool.

Datasets created under rpool:
  rpool/ROOT
  rpool/ROOT/debian          ← OS root (mountpoint /), /boot is a plain
                               directory here (the kernel+initramfs live
                               in the UKI on the ESP, not in ZFS)
  rpool/home                 ← Home directories
  rpool/var
  rpool/var/log
  rpool/.freedomnas          ← FreedomNAS config dataset (canmount=noauto)
"""

from __future__ import annotations

from installer_modules import Logger, DialogManager, Configuration, run_command


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
        """Create rpool and its datasets on the selected partition."""
        self._create_rpool()
        self._create_datasets()
        Logger.info("ZFS pool and datasets created successfully")

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

    def _create_rpool(self) -> None:
        rpart = self._pool_partition("part2")
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
        Logger.info("Creating ZFS datasets...")

        datasets = [
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

        # Set bootfs (metadata only — no boot-time consumer reads it anymore,
        # but it's a cheap, standard way to record the intended boot dataset)
        run_command(["zpool", "set", f"bootfs={pool}/ROOT/debian", pool])

        # Mount root dataset explicitly
        run_command(["zfs", "mount", f"{pool}/ROOT/debian"])
        run_command(["zfs", "mount", "--all"])

        Logger.info("ZFS datasets created and mounted")
