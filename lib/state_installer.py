"""
FreedomNAS Installer — persisting the installation to config.yaml.

Both install modes end here, and neither creates a `.freedomnas` dataset.
The installer's answers (hostname, users, pool, features) go to config.yaml
in the state directory on the root filesystem, not marked initialized, and
a setup-pending file says the system still has to be set up.

On the first boot `freedomnas state-boot` creates the root pool's
`.freedomnas`, moves this config.yaml into it and looks for a configuration
on every other pool: one found — a data pool that outlived the old system —
is restored into it. None found, the setup wizard runs and copies the
configuration to the pool the administrator picks. A data pool's copy has
a mountpoint of its own, so it never competes with the root pool's.

This is the "dual write" rule seen from the installer's side: the OS was
changed first, and now the intent is recorded.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import corelink
from context import ExecutionContext
from installer_modules import Logger, Configuration

STATE_INSTALL = "install"
_PROBE_PREFIX = "fnasprobe"


class StateWriter:
    """Writes the installation's desired state to the FreedomNAS config file."""

    def __init__(self, config: Configuration, ctx: ExecutionContext) -> None:
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def save(self, state_path: Optional[str] = None, initialized: bool = True) -> Optional[Path]:
        """
        Write hostname, users, pool and features, and the initialized marker
        unless the setup wizard is still to run (`initialized=False`).
        """
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

            if initialized:
                manager.mark_initialized(
                    edition=self.config.target_os,
                    initialized_by="installer",
                )
            Logger.info("Configuration saved")
            return path
        except Exception as exc:
            Logger.warn(f"Could not save installation config: {exc}")
            return None

    def mark_setup_pending(self, kind: str = "") -> None:
        """
        Leave the file that keeps the setup wizard in front of everything
        until someone has set the system up: a disk image's first boot can
        restore another system's configuration, which must not stand in for
        that while the image's shipped password still works.

        `kind` is written into it. An image leaves it empty — its password is
        public and the wizard insists on a new one. An installation writes
        STATE_INSTALL: its administrator chose the password in the installer.
        """
        path = self.ctx.target_path(f"{self.ctx.config_dir}/setup-pending")
        Logger.command(f"[file] {path}")
        if self.ctx.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{kind}\n" if kind else "")

    def find_restorable(self, skip_disk: str = "") -> List[Dict[str, str]]:
        """
        Pools on this machine whose `.freedomnas` dataset holds a FreedomNAS
        configuration: what the first boot of the installed system will
        restore. Each pool is imported read-only under a name of our own and
        a temporary root, so neither its name (another `rpool`) nor its
        mountpoints can reach the running system, and exported again at once.
        The pool on `skip_disk`, about to be erased, is left out.
        """
        if self.ctx.dry_run or not self.ctx.has("rootpool", "probe_import", target=False):
            return []
        try:
            listing, _ = self.ctx.host_run("rootpool", "list_importable", check=False, timeout=60)
        except Exception as exc:
            Logger.warn(f"Could not look for existing FreedomNAS configurations: {exc}")
            return []

        skip = Path(skip_disk).name if skip_disk else ""
        found = []
        for index, (name, pool_id, devices) in enumerate(_importable_pools(listing)):
            if skip and skip in devices:
                continue
            temp_name = f"{_PROBE_PREFIX}{index}"
            altroot = tempfile.mkdtemp(prefix="fnas-probe-")
            try:
                self.ctx.host_run(
                    "rootpool", "probe_import", timeout=120,
                    altroot=altroot, pool_id=pool_id, temp_name=temp_name,
                )
            except Exception as exc:
                Logger.warn(f"Could not look into pool {name}: {exc}")
                _remove_empty_dir(altroot)
                continue
            try:
                state = self._read_probed_state(f"{temp_name}/.freedomnas")
                if state is not None:
                    state["pool"] = name
                    found.append(state)
            finally:
                self.ctx.host_run("rootpool", "export", check=False, pool_name=temp_name)
                _remove_empty_dir(altroot)
        return found

    def _read_probed_state(self, dataset: str) -> Optional[Dict[str, str]]:
        try:
            self.ctx.host_run("rootpool", "dataset_exists", dataset=dataset, timeout=30)
        except Exception:
            return None
        self.ctx.host_run("rootpool", "mount_dataset", check=False, dataset=dataset)
        mountpoint, _ = self.ctx.host_run(
            "rootpool", "dataset_mountpoint", check=False, dataset=dataset
        )
        config_file = Path(mountpoint.strip()) / "config.yaml"
        if not config_file.is_file():
            return None
        try:
            import yaml
            data = yaml.safe_load(config_file.read_text()) or {}
        except Exception:
            data = {}
        system = data.get("system") or {}
        return {
            "hostname": str(system.get("hostname") or "unknown"),
            "edition": str(system.get("edition") or "unknown"),
        }

    def state_dir(self) -> str:
        """Where the target edition mounts the .freedomnas dataset."""
        return f"{self.ctx.config_dir}/state"

    def fallback_path(self) -> str:
        """Where state goes when no pool can host the .freedomnas dataset."""
        return f"{self.ctx.config_dir}/config.yaml"


def _importable_pools(listing: str) -> List[tuple]:
    """(name, id, the rest of its block) for each pool `zpool import` lists."""
    pools = []
    for block in re.split(r"(?m)^\s*pool:\s*", listing)[1:]:
        name = block.split(None, 1)[0] if block.strip() else ""
        match = re.search(r"(?m)^\s*id:\s*(\d+)", block)
        if name and match:
            pools.append((name, match.group(1), block))
    return pools


def _remove_empty_dir(path: str) -> None:
    try:
        Path(path).rmdir()
    except OSError:
        pass
