"""
FreedomNAS Installer — persisting the installation to config.yaml.

Both install modes end here. The shadow copy written to the `.freedomnas`
dataset is what makes a FreedomNAS reinstallable: on the next boot
firstboot.sh finds it and hands it to the reconciler
(`reconcile --mode declarative`), which makes the running system match
it: hostname, users, pools, shares and features. Without this step an
install is a one-off; with it, it is reproducible.

This is the "dual write" rule seen from the installer's side: the OS was
changed first, and now the intent is recorded.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import corelink
from context import ExecutionContext
from installer_modules import Logger, Configuration

class StateWriter:
    """Writes the installation's desired state to the FreedomNAS config file."""

    def __init__(self, config: Configuration, ctx: ExecutionContext) -> None:
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def mount_state_dataset(self) -> None:
        """
        Mount <pool>/.freedomnas — it is canmount=noauto, so `zfs mount --all`
        deliberately leaves it alone.

        The mount is checked, not assumed: save() writes config.yaml under
        that path, and if the dataset is not there the file lands on the root
        dataset instead. The next boot then mounts an empty .freedomnas over
        it, finds no configuration, and runs the first-run wizard on a system
        that was already configured.
        """
        dataset = f"{self.config.pool_name}/.freedomnas"
        self.ctx.host_run("rootpool", "mount_dataset", check=False, dataset=dataset)
        if self.ctx.dry_run:
            return
        mountpoint = self.ctx.target_path(self.state_dir())
        if not os.path.ismount(mountpoint):
            Logger.error(
                f"{dataset} is not mounted at {mountpoint}.\n"
                f"The installation's configuration would be written outside it "
                f"and lost at the next boot."
            )

    def ensure_state_dataset(self, pool_name: str) -> bool:
        """
        Make sure `<pool>/.freedomnas` exists and is mounted (in-place mode,
        where the pool already exists and was not created by us).

        Returns True if the dataset is available.
        """
        dataset = f"{pool_name}/.freedomnas"
        try:
            self.ctx.host_run("rootpool", "dataset_exists", dataset=dataset, timeout=30)
            exists = True
        except Exception:
            exists = False

        if not exists:
            Logger.info(f"Creating state dataset {dataset}...")
            try:
                self.ctx.host_run("rootpool", "create_state_dataset", pool_name=pool_name)
            except Exception as exc:
                Logger.warn(f"Could not create {dataset}: {exc}")
                return False

        self.ctx.host_run("rootpool", "mount_dataset", check=False, dataset=dataset)
        return True

    def save(self, state_path: Optional[str] = None) -> Optional[Path]:
        """Write hostname, users, pool, features and the initialized marker."""
        path = self.ctx.target_path(state_path or f"{self.state_dir()}/config.yaml")
        Logger.info(f"Saving installation configuration to {path}")

        if self.ctx.dry_run:
            return path

        try:
            config_module = corelink.config_module()
        except corelink.CoreNotAvailable as exc:
            Logger.warn(f"Cannot save configuration: {exc}")
            return None

        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            manager = config_module.ConfigurationManager(config_path=str(path))
            manager.set_hostname(self.config.hostname)
            manager.set_timezone(self.config.timezone)
            manager.set_root_filesystem("zfs")

            manager.add_user({
                "username": self.config.username,
                "groups": [self.ctx.target.get_profile_value("users", "sudo_group")],
                "admin": True,
            })
            for user in self.config.additional_users:
                manager.add_user({
                    "username": user["username"],
                    "groups": (
                        [self.ctx.target.get_profile_value("users", "sudo_group")]
                        if user.get("sudo") else []
                    ),
                    "admin": bool(user.get("sudo")),
                })

            if self.config.mode == "clean":
                manager.add_pool({
                    "name": self.config.pool_name,
                    "vdev_type": "single",
                    "ashift": self.config.ashift,
                    "compression": self.config.compression,
                    "devices": [self.config.disk_by_id or self.config.part2],
                })

            for category in self.config.package_categories:
                manager.add_installed_feature(category)

            manager.mark_initialized(
                edition=self.config.target_os,
                initialized_by="installer",
            )
            Logger.info("Configuration saved")
            return path
        except Exception as exc:
            Logger.warn(f"Could not save installation config: {exc}")
            return None

    def state_dir(self) -> str:
        """Where the target edition mounts the .freedomnas dataset."""
        return f"{self.ctx.config_dir}/state"

    def fallback_path(self) -> str:
        """Where state goes when no pool can host the .freedomnas dataset."""
        return f"{self.ctx.config_dir}/config.yaml"
