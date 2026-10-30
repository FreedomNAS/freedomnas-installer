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

When the administrator chose to restore one of those copies, the part of
the restore a system can take before it runs happens here, in the chroot:
`freedomnas restore-offline` puts the NAS's identity back and installs the
modules the configuration uses with what they need. The first boot's
restore then finds them in place instead of downloading them.

This is the "dual write" rule seen from the installer's side: the OS was
changed first, and now the intent is recorded.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import corelink
from context import ExecutionContext
from installer_modules import Logger, Configuration

STATE_INSTALL = "install"
_PROBE_PREFIX = "fnasprobe"
RESTORE_DIR = "/var/tmp/freedomnas-restore"
_RELEASE_TRIES = 10
_RELEASE_SECONDS = 3
_KEPT = ("config.yaml", "identity")


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

            if self.config.restore:
                manager.set("system.restore_from", self.config.restore["pool"])
            elif self.config.restore_declined:
                manager.set("system.restore", False)

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

    def find_restorable(self, skip_disk: str = "") -> List[Dict]:
        """
        Pools on this machine whose `.freedomnas` dataset holds a FreedomNAS
        configuration, newest first: what the installed system can restore.
        Each pool is imported read-only under a name of our own and a
        temporary root, so neither its name (another `rpool`) nor its
        mountpoints can reach the running system, and exported again at once;
        its config.yaml and identity are kept in a directory of their own
        (`source`) for `restore_offline`. The pool on `skip_disk`, about to
        be erased, is left out.
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
                self._release(temp_name, name)
                _remove_empty_dir(altroot)
        found.sort(key=lambda state: state["saved_at"], reverse=True)
        return found

    def _release(self, temp_name: str, name: str) -> None:
        """
        Export a pool imported to look into it, until the pool list says it is
        gone: just after its dataset is unmounted the export can find it busy
        for a moment. One left imported would have every dataset mounted by
        the `zfs mount -a` that follows the root pool's creation, so the
        installation stops here instead, before any disk is touched.
        """
        actions = ["export"] * _RELEASE_TRIES + ["export_force"]
        for action in actions:
            self.ctx.host_run("rootpool", action, check=False, pool_name=temp_name)
            listed, _ = self.ctx.host_run("rootpool", "list_pools", check=False)
            if temp_name not in listed.split():
                return
            time.sleep(_RELEASE_SECONDS)
        raise RuntimeError(
            f"The pool {name}, imported as {temp_name} to look for a FreedomNAS configuration, "
            f"could not be exported again. Run 'zpool export {temp_name}' and start the installer again."
        )

    def _read_probed_state(self, dataset: str) -> Optional[Dict]:
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
        if not system.get("initialized"):
            return None
        source = tempfile.mkdtemp(prefix="fnas-restore-")
        for name in _KEPT:
            kept = config_file.parent / name
            if kept.is_dir():
                shutil.copytree(kept, Path(source) / name)
            elif kept.is_file():
                shutil.copy2(kept, Path(source) / name)
        return {
            "hostname": str(system.get("hostname") or "unknown"),
            "edition": str(system.get("edition") or "unknown"),
            "saved_at": str(system.get("saved_at") or ""),
            "timezone": str(system.get("timezone") or ""),
            "admins": _admins(data.get("users") or []),
            "features": [str(f.get("id")) for f in data.get("installed_features") or []
                         if isinstance(f, dict) and f.get("id") and f.get("enabled", True)],
            "modules": sorted(m for m, s in (data.get("modules") or {}).items() if isinstance(s, dict) and s),
            "source": source,
        }

    def restore_offline(self, state: Dict) -> None:
        """
        The chosen copy's identity and modules into the installed system,
        through freedomnas-core inside the chroot. What fails here is left to
        the first boot's restore, which does the same and more.
        """
        Logger.info(f"Restoring {state['hostname']}'s identity and modules from {state['pool']}")
        if self.ctx.dry_run:
            self.ctx.target_run("python", "restore_offline", check=False, source=RESTORE_DIR)
            return
        target = self.ctx.target_path(RESTORE_DIR)
        try:
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(state["source"], target)
            target.chmod(0o700)
            stdout, stderr = self.ctx.target_run(
                "python", "restore_offline", timeout=7200, check=False, source=RESTORE_DIR,
            )
            for line in (stdout + stderr).splitlines():
                if line.startswith(("FAIL", "ERROR")):
                    Logger.warn(f"{line} — the first boot tries again")
        except Exception as exc:
            Logger.warn(f"Could not restore before the first boot, the first boot will: {exc}")
        finally:
            shutil.rmtree(target, ignore_errors=True)

    @staticmethod
    def discard(states: List[Dict]) -> None:
        """Remove the copies find_restorable kept on the live system."""
        for state in states:
            shutil.rmtree(state.get("source") or "", ignore_errors=True)

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


def _admins(users: List) -> List[str]:
    """Administrators of a configuration, old ones that carry an admin group included."""
    try:
        normalize = corelink.config_module().normalize_user
    except (corelink.CoreNotAvailable, AttributeError):
        normalize = lambda user: user
    names = []
    for user in users:
        if isinstance(user, dict) and user.get("username") and normalize(dict(user)).get("admin"):
            names.append(str(user["username"]))
    return names


def _remove_empty_dir(path: str) -> None:
    try:
        Path(path).rmdir()
    except OSError:
        pass
