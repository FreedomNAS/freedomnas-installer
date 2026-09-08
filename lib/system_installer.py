"""
FreedomNAS Installer — base system provisioning.

Covers firmware detection, the hostname/timezone/mirror prompts, the
bootstrap of the target root, the OS-specific configuration files, and the
chroot bind mounts.

Two bootstrap methods, both declared per edition in
`installer_meta.bootstrap_methods`:

  debootstrap  — the distro's own bootstrapper (debootstrap, pacstrap,
                 basestrap, xbps-install -r ...) named by the profile
  tarball      — download and unpack a rootfs tarball (the distro's own
                 rootfs.tar.gz, or an exported container rootfs)

The tarball path is what makes a new edition cheap: a distro that ships a
rootfs tarball needs no bootstrapper on the installer host at all.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Set

from context import ExecutionContext
from installer_modules import (
    Logger, DialogManager, Configuration, directory_exists,
)

# files.webui_cron is deployed by CoreInstaller, together with the scripts
# it points at, so configure_system() leaves it alone.
_FILES_OWNED_ELSEWHERE = {"webui_cron"}

_UTC = "UTC"


def _available_timezones() -> Set[str]:
    """
    The tz database as the host carries it: plain data, not an OS command,
    and the same names every edition writes into /etc/localtime.
    """
    try:
        import zoneinfo
        return {z for z in zoneinfo.available_timezones() if z != "localtime"}
    except Exception:
        return set()


def _time_in(zone: str) -> str:
    """The clock the admin is about to choose, so the confirmation means something."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(zone)).strftime("%H:%M on %a %d %b %Y")
    except Exception:
        return ""


class SystemInstaller:
    """Firmware detection, system prompts, bootstrap and base configuration."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Detection and prompts
    # ------------------------------------------------------------------

    def detect_boot_mode(self) -> None:
        """UEFI if the firmware exposes efivars, BIOS otherwise."""
        self.config.boot_mode = "UEFI" if directory_exists("/sys/firmware/efi") else "BIOS"
        Logger.info(f"Boot mode detected: {self.config.boot_mode}")

    def load_defaults(self) -> None:
        """Seed the configuration from the target profile's installer_meta."""
        self.config.arch = self.ctx.meta("arch", self.config.arch)
        self.config.suite = self.ctx.meta("suite", "")
        self.config.mirror = self.ctx.meta("mirror", "")
        self.config.rootfs_url = self.ctx.meta("rootfs_tarball_url", "")

    def select_bootstrap_method(self) -> None:
        """Pick how the target root gets populated."""
        methods = self.ctx.bootstrap_methods()
        if not methods:
            raise RuntimeError(
                f"No usable bootstrap method for '{self.config.target_os}' on this host. "
                f"Declared: {self.ctx.meta('bootstrap_methods', [])}"
            )

        if len(methods) == 1:
            self.config.bootstrap_method = methods[0]
            Logger.info(f"Bootstrap method: {methods[0]}")
            return

        labels = {
            "debootstrap": "Native bootstrapper (recommended)",
            "pacstrap": "Native bootstrapper (recommended)",
            "basestrap": "Native bootstrapper (recommended)",
            "xbps": "Native bootstrapper (recommended)",
            "tarball": "Download and unpack a rootfs tarball",
        }
        choice = self.dialog.menu(
            "Bootstrap Method",
            f"How should the {self.config.target_os} base system be installed?",
            [(m, labels.get(m, m)) for m in methods],
            height=14,
            width=68,
            list_height=6,
        )
        if not choice:
            raise RuntimeError("No bootstrap method selected — installation aborted")
        self.config.bootstrap_method = choice
        Logger.info(f"Bootstrap method: {choice}")

    def select_mirror(self) -> None:
        """Pick a package mirror from the list the profile declares."""
        mirrors = self.ctx.meta("mirrors", []) or []
        if not mirrors:
            return

        items = [(m["code"], m["label"]) for m in mirrors]
        items.append(("CUSTOM", "Custom — enter URL manually"))

        choice = self.dialog.menu(
            "Package Mirror",
            "Select the mirror closest to you:",
            items,
            height=24,
            width=78,
        )
        if not choice:
            Logger.warn(f"Mirror selection cancelled — using {self.config.mirror}")
            return

        if choice == "CUSTOM":
            url = self.dialog.inputbox(
                "Custom Mirror",
                "Enter the full mirror URL:",
                init_text=self.config.mirror,
                height=10,
                width=70,
            )
            if url and url.strip():
                self.config.mirror = url.strip()
        else:
            for mirror in mirrors:
                if mirror["code"] == choice:
                    self.config.mirror = mirror["url"]
                    break

        Logger.info(f"Mirror: {self.config.mirror}")

    def prompt_rootfs_url(self) -> None:
        """Ask for the rootfs tarball URL when the tarball method is used."""
        if self.config.bootstrap_method != "tarball":
            return
        url = self.dialog.inputbox(
            "Rootfs Tarball",
            f"URL of the {self.config.target_os} rootfs tarball\n"
            "(the distro's own rootfs.tar.gz, or an exported container rootfs):",
            init_text=self.config.rootfs_url,
            height=12,
            width=76,
        )
        if not url or not url.strip():
            raise RuntimeError("A rootfs tarball URL is required — installation aborted")
        self.config.rootfs_url = url.strip()

    def prompt_hostname(self) -> None:
        hostname = self.dialog.inputbox(
            "System Hostname",
            "Enter the hostname for this FreedomNAS system:\n"
            "(letters, digits, hyphens — no dots or spaces)",
            init_text=self.config.hostname,
            height=12,
            width=60,
        )
        if hostname and hostname.strip():
            self.config.hostname = hostname.strip()
        Logger.info(f"Hostname: {self.config.hostname}")

    def prompt_timezone(self) -> None:
        """
        Pick from the host's tz database: region, then zone, then confirm.

        Typed by hand, a name that does not exist reaches the target as an
        /etc/localtime symlink pointing at nothing, and the first sign of it
        is a clock that is wrong.
        """
        zones = _available_timezones()
        if not zones:
            Logger.warn("No tz database on this host — asking for the name instead")
            self._prompt_timezone_by_name()
            return

        regions = sorted({z.split("/", 1)[0] for z in zones if "/" in z})
        while True:
            region = self.dialog.menu(
                "Timezone — Region",
                "Select the region:",
                [(_UTC, "Coordinated Universal Time")] + [(r, "") for r in regions],
                list_height=15,
            )
            if region is None:
                Logger.warn(f"Timezone unchanged: {self.config.timezone}")
                return

            if region == _UTC:
                candidate = _UTC
            else:
                zone = self.dialog.menu(
                    f"Timezone — {region}",
                    "Select the zone:",
                    [(z.split("/", 1)[1], "") for z in sorted(zones) if z.startswith(f"{region}/")],
                    list_height=15,
                )
                if zone is None:
                    continue      # back to the region list, not out of the prompt
                candidate = f"{region}/{zone}"

            now = _time_in(candidate)
            if self.dialog.yesno(
                "Confirm Timezone",
                f"The system clock will use:\n\n    {candidate}\n"
                + (f"\nThe time there is now {now}.\n" if now else "")
                + "\nIs that correct?",
                12, 62,
            ):
                self.config.timezone = candidate
                Logger.info(f"Timezone: {candidate}")
                return

    def _prompt_timezone_by_name(self) -> None:
        timezone = self.dialog.inputbox(
            "Timezone",
            "Enter the system timezone (e.g. Europe/Rome):",
            init_text=self.config.timezone,
            height=10,
            width=60,
        )
        if timezone and timezone.strip():
            self.config.timezone = timezone.strip()
        Logger.info(f"Timezone: {self.config.timezone}")

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap_base_system(self) -> None:
        """Populate the target root using the selected bootstrap method."""
        method = self.config.bootstrap_method
        Logger.info(f"Bootstrapping {self.config.target_os} into {self.config.target_root}...")

        if method == "tarball":
            self._bootstrap_tarball()
        else:
            self.ctx.stage_run(
                "bootstrap", method,
                timeout=3600,
                arch=self.config.arch,
                suite=self.config.suite,
                root=self.config.target_root,
                mirror=self.config.mirror,
            )
        Logger.info("Base system installed")

    def _bootstrap_tarball(self) -> None:
        tarball = Path(tempfile.gettempdir()) / f"freedomnas-{self.config.target_os}-rootfs.tar"
        Logger.info(f"Fetching rootfs tarball: {self.config.rootfs_url}")
        self.ctx.stage_run(
            "bootstrap", "fetch_tarball",
            timeout=3600,
            url=self.config.rootfs_url,
            output=str(tarball),
        )
        Logger.info("Unpacking rootfs tarball...")
        self.ctx.stage_run(
            "bootstrap", "unpack_tarball",
            timeout=3600,
            tarball=str(tarball),
            root=self.config.target_root,
        )
        if not self.ctx.dry_run:
            tarball.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Configuration and chroot
    # ------------------------------------------------------------------

    def configure_system(self) -> None:
        """Write the target's OS-specific configuration files from the profile."""
        Logger.info("Writing base configuration...")
        fqdn = (
            f"{self.config.hostname}.{self.config.domain}"
            if self.config.domain else self.config.hostname
        )
        variables = {
            "hostname": self.config.hostname,
            "fqdn": fqdn,
            "domain": self.config.domain,
            "timezone": self.config.timezone,
            "mirror": self.config.mirror,
            "suite": self.config.suite,
            "arch": self.config.arch,
            "esp_device": self.config.part1 or "",
            "esp_spec": self._esp_fstab_spec(),
            "pool_name": self.config.pool_name,
            "edition": self.config.target_os,
            "sudo_group": self.ctx.target.get_profile_value("users", "sudo_group"),
        }

        # Every file the edition declares, except the ones another step owns.
        for name in sorted(self.ctx.target.profile.get("files", {})):
            if name in _FILES_OWNED_ELSEWHERE:
                continue
            self.ctx.write_target_file(name, **variables)

        Logger.info("Base configuration written")

    def _esp_fstab_spec(self) -> str:
        """
        Mount the ESP by UUID when the platform can report one — a NAS grows
        disks, and /dev/sdX is not a stable name across reboots.
        """
        if not self.config.part1:
            return ""
        if not self.ctx.has("partitioning", "esp_uuid", target=False):
            return self.config.part1
        try:
            uuid, _ = self.ctx.host_run(
                "partitioning", "esp_uuid", check=False, timeout=30, device=self.config.part1
            )
        except Exception:
            uuid = ""
        uuid = uuid.strip()
        return f"UUID={uuid}" if uuid else self.config.part1

    def setup_chroot(self) -> None:
        """Create the pseudo-filesystem mountpoints and bind-mount them."""
        Logger.info("Preparing chroot environment...")
        for directory in self.ctx.meta("chroot_dirs", []) or []:
            self.ctx.ensure_target_dir(directory)

        self.ctx.host_run("chroot", "mount", root=self.config.target_root)
        self._copy_resolv_conf()
        Logger.info("Chroot ready")

    def teardown_chroot(self) -> None:
        """
        Undo what setup_chroot did, plus whatever the edition has to take back
        out of the target before it boots on its own — Debian's policy-rc.d,
        which keeps package scripts from starting daemons inside the chroot.
        """
        if self.ctx.has("chroot", "finalize"):
            self.ctx.target_run("chroot", "finalize", check=False)
        self.ctx.host_run("chroot", "unmount", check=False, root=self.config.target_root)

    def _copy_resolv_conf(self) -> None:
        """
        Give the chroot working DNS. Plain file I/O — /etc/resolv.conf is a
        POSIX-wide convention, not an OS command.
        """
        source = Path("/etc/resolv.conf")
        if not source.exists() or self.ctx.dry_run:
            return
        dest = self.ctx.target_path("/etc/resolv.conf")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        shutil.copy2(source, dest)
