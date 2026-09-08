"""
FreedomNAS Installer — root pool and dataset creation.

The dataset tree is the same one freedomnas-image-builder lays down
(common/zfs-layout.sh), so a system installed here and a system flashed
from an image expose an identical layout to the reconciler:

  rpool/ROOT                 (container)
  rpool/ROOT/<edition>       OS root, mountpoint=/
  rpool/home
  rpool/var, rpool/var/log
  rpool/.freedomnas          canmount=noauto, /etc/freedomnas/state

A single, full-featured pool — no GRUB-compatible bpool, because nothing at
boot time reads ZFS directly (the UKI's dracut initramfs does, with the full
kernel driver; FreeBSD's loader reads the pool natively).

Commands come from the host profile's `rootpool` category: pool creation is
the host's job, since the target system does not exist yet.
"""

from __future__ import annotations

import os
import re
import time

from context import ExecutionContext
from installer_modules import Logger, DialogManager, Configuration, directory_exists

# ZFS pool names: a letter, then letters, digits and _ - . :
_POOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")

# The chroot is torn down with lazy unmounts, which complete a moment later.
_EXPORT_SETTLE_SECONDS = 3


class ZFSInstaller:
    """Creates and mounts the FreedomNAS root pool."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx
        self.export_failed = False

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def prompt_pool_name(self) -> None:
        """
        Ask what the root pool should be called, and refuse a name already in
        use.

        The name used to be an unasked constant. On a machine that already had
        a pool by that name — a second NAS, a disk from another system, a
        dual-boot install — creation collided, and what the installer then
        recorded in config.yaml named someone else's pool.
        """
        existing = self._existing_pools()

        while True:
            text = "Name for the FreedomNAS root pool:"
            if existing:
                text += "\n\nAlready on this machine: " + ", ".join(existing)

            name = self.dialog.inputbox(
                "Root Pool Name", text,
                init_text=self.config.pool_name, height=12, width=70,
            )
            if not name or not name.strip():
                continue
            name = name.strip()

            if not _POOL_NAME.match(name):
                self.dialog.msgbox(
                    "Invalid Name",
                    "A pool name starts with a letter and may contain letters,\n"
                    "digits, and _ - . :",
                    height=10, width=62,
                )
                continue

            if name in existing:
                self.dialog.msgbox(
                    "Name In Use",
                    f"A pool named '{name}' is already imported on this machine.\n"
                    "Choose another name, or the two would be indistinguishable.",
                    height=10, width=66,
                )
                continue

            self.config.pool_name = name
            Logger.info(f"Root pool: {name}")
            return

    def _existing_pools(self) -> list:
        try:
            stdout, _ = self.ctx.host_run(
                "rootpool", "list_pools", check=False, timeout=60
            )
        except Exception as exc:
            Logger.warn(f"Could not list existing pools: {exc}")
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def create_root_pool(self) -> None:
        """Create the pool, its datasets, and mount them under the target root."""
        device = self._pool_device()
        Logger.info(f"Creating pool '{self.config.pool_name}' on {device}...")

        self.ctx.host_run(
            "rootpool", "create",
            timeout=120,
            ashift=self.config.ashift,
            compression=self.config.compression,
            altroot=self.config.target_root,
            pool_name=self.config.pool_name,
            device=device,
        )

        Logger.info("Creating datasets...")
        self.ctx.host_run(
            "rootpool", "create_datasets",
            timeout=120,
            pool_name=self.config.pool_name,
            edition=self.config.target_os,
        )
        self.ctx.host_run(
            "rootpool", "set_bootfs",
            pool_name=self.config.pool_name,
            edition=self.config.target_os,
        )
        self.ctx.host_run(
            "rootpool", "mount_root",
            pool_name=self.config.pool_name,
            edition=self.config.target_os,
        )
        self.ctx.host_run("rootpool", "mount_all")
        Logger.info("Root pool ready")

    def export_pool(self) -> None:
        """
        Export the pool (end of installation).

        This is not a formality. A pool that is still imported keeps the
        installer host's hostid in its label, and the machine that was just
        installed then refuses to import its own root pool at boot: "pool was
        previously in use from another system".

        The exit code is not proof — the chroot's lazy unmounts may still be
        settling when the export runs — so the pool list decides.
        """
        pool = self.config.pool_name
        for action in ("export", "export_force"):
            self.ctx.host_run("rootpool", action, check=False, pool_name=pool)
            if not self._pool_is_imported(pool):
                Logger.info(f"Pool {pool} exported")
                self.export_failed = False
                return
            Logger.warn(f"{pool} is still imported after '{action}' — waiting")
            time.sleep(_EXPORT_SETTLE_SECONDS)

        self.export_failed = True
        Logger.error(
            f"The pool {pool} could not be exported and is still imported.\n"
            f"The installed system will refuse to import its own root pool at "
            f"boot. Run 'zpool export {pool}' before rebooting."
        )

    def _pool_is_imported(self, pool: str) -> bool:
        """Cannot tell counts as imported: a false alarm is cheaper than a silent failure."""
        if self.ctx.dry_run:
            return False
        try:
            stdout, _ = self.ctx.host_run("rootpool", "list_pools", check=False)
        except Exception:
            return True
        return pool in stdout.split()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pool_device(self) -> str:
        """
        Prefer a stable by-id path for the pool member, exactly as the pool
        will be imported at boot.
        """
        part = self.config.part2
        if not part:
            raise RuntimeError("config.part2 not set — partition_disk() must run first")

        by_id_dir = "/dev/disk/by-id"
        if directory_exists(by_id_dir):
            for name in os.listdir(by_id_dir):
                link = os.path.join(by_id_dir, name)
                try:
                    if os.path.realpath(link) == os.path.realpath(part):
                        return link
                except OSError:
                    continue
        return part
