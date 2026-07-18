"""
FreedomNAS Installer — Base system setup.

Covers steps: detect_boot_mode, select_mirror, prompt_hostname,
install_base_system, configure_system, setup_chroot.
"""

from __future__ import annotations

import os
from typing import Optional

from installer_modules import (
    Logger, DialogManager, Configuration,
    run_command, run_in_chroot, write_file, directory_exists,
)


# Mirrors indexed by country code: (label, url)
_MIRRORS: dict[str, tuple[str, str]] = {
    "IT": ("Italy (debian.connesi.it)",       "http://debian.connesi.it/debian"),
    "DE": ("Germany (ftp.de.debian.org)",     "http://ftp.de.debian.org/debian"),
    "FR": ("France (ftp.fr.debian.org)",      "http://ftp.fr.debian.org/debian"),
    "US": ("United States (deb.debian.org)",  "http://deb.debian.org/debian"),
    "UK": ("United Kingdom (ftp.uk.debian.org)", "http://ftp.uk.debian.org/debian"),
    "NL": ("Netherlands (ftp.nl.debian.org)", "http://ftp.nl.debian.org/debian"),
    "CH": ("Switzerland (ftp.ch.debian.org)", "http://ftp.ch.debian.org/debian"),
    "PL": ("Poland (ftp.pl.debian.org)",      "http://ftp.pl.debian.org/debian"),
    "SE": ("Sweden (ftp.se.debian.org)",      "http://ftp.se.debian.org/debian"),
    "AU": ("Australia (ftp.au.debian.org)",   "http://ftp.au.debian.org/debian"),
    "JP": ("Japan (ftp.jp.debian.org)",       "http://ftp.jp.debian.org/debian"),
    "CA": ("Canada (ftp.ca.debian.org)",      "http://ftp.ca.debian.org/debian"),
    "BR": ("Brazil (ftp.br.debian.org)",      "http://ftp.br.debian.org/debian"),
    "ES": ("Spain (ftp.es.debian.org)",       "http://ftp.es.debian.org/debian"),
}


class SystemInstaller:
    """Handles OS detection, mirror selection, hostname prompt, and base system install."""

    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration) -> None:
        self.logger = logger
        self.dialog = dialog
        self.config = config

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def detect_boot_mode(self) -> None:
        """Detect UEFI or BIOS boot mode from /sys/firmware/efi."""
        if directory_exists("/sys/firmware/efi"):
            self.config.boot_mode = "UEFI"
        else:
            self.config.boot_mode = "BIOS"
        Logger.info(f"Boot mode detected: {self.config.boot_mode}")

    def select_mirror(self) -> None:
        """Let the user pick a Debian mirror (or enter a custom URL)."""
        Logger.info("Selecting Debian mirror...")

        items = [(code, label) for code, (label, _) in _MIRRORS.items()]
        items.append(("CUSTOM", "Custom — enter URL manually"))

        choice = self.dialog.menu(
            "Debian Mirror",
            "Select the Debian mirror closest to you:",
            items,
            height=24,
            width=78,
        )

        if not choice:
            Logger.warn("Mirror selection cancelled — using default (deb.debian.org)")
            return

        if choice == "CUSTOM":
            url = self.dialog.inputbox(
                "Custom Mirror",
                "Enter the full Debian mirror URL:",
                init_text="http://deb.debian.org/debian",
                height=10,
                width=70,
            )
            if url and url.strip():
                self.config.debian_mirror = url.strip()
            else:
                Logger.warn("Custom mirror empty — using default")
        elif choice in _MIRRORS:
            _, url = _MIRRORS[choice]
            self.config.debian_mirror = url
        else:
            Logger.warn("Unknown selection — using default mirror")

        Logger.info(f"Mirror: {self.config.debian_mirror}")

    def prompt_hostname(self) -> None:
        """Prompt the user for the system hostname (spec §8.3 step 4)."""
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

    def install_base_system(self) -> None:
        """Install Debian base system via debootstrap."""
        Logger.info("Installing Debian base system (debootstrap)...")
        self._ensure_debootstrap()
        run_command(
            [
                "debootstrap",
                "--arch=amd64",
                "trixie",
                "/mnt",
                self.config.debian_mirror,
            ],
            timeout=1800,
        )
        Logger.info("Base system installed")

    def configure_system(self) -> None:
        """Write hostname, /etc/hosts, fstab, timezone, and sources.list."""
        Logger.info("Configuring base system...")
        self._write_hostname()
        self._write_hosts()
        self._write_fstab()
        self._write_sources_list()
        self._write_timezone()
        Logger.info("Base system configured")

    def setup_chroot(self) -> None:
        """Bind-mount /dev, /dev/pts, /proc, /sys, /run into /mnt."""
        Logger.info("Setting up chroot environment...")
        mounts = [
            ("/dev",     "/mnt/dev"),
            ("/dev/pts", "/mnt/dev/pts"),
            ("/proc",    "/mnt/proc"),
            ("/sys",     "/mnt/sys"),
            ("/run",     "/mnt/run"),
        ]
        for src, dst in mounts:
            os.makedirs(dst, exist_ok=True)
            run_command(["mount", "--bind", src, dst])
        # Copy resolv.conf for DNS access inside chroot
        run_command(["cp", "/etc/resolv.conf", "/mnt/etc/resolv.conf"])
        Logger.info("Chroot ready")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_debootstrap(self) -> None:
        """Install debootstrap if not present."""
        r = run_command(["which", "debootstrap"], check=False)
        if r.returncode != 0:
            Logger.info("debootstrap not found — installing...")
            run_command(["apt-get", "update", "-qq"])
            run_command(["apt-get", "install", "-y", "debootstrap"])

    def _write_hostname(self) -> None:
        write_file("/mnt/etc/hostname", f"{self.config.hostname}\n")

    def _write_hosts(self) -> None:
        fqdn = (
            f"{self.config.hostname}.{self.config.domain}"
            if self.config.domain
            else self.config.hostname
        )
        content = (
            f"127.0.0.1  localhost\n"
            f"127.0.1.1  {fqdn} {self.config.hostname}\n\n"
            "::1        localhost ip6-localhost ip6-loopback\n"
            "ff02::1    ip6-allnodes\n"
            "ff02::2    ip6-allrouters\n"
        )
        write_file("/mnt/etc/hosts", content)

    def _write_fstab(self) -> None:
        # The ESP is used in both boot modes now (efibootmgr reads the UKI
        # from it directly on UEFI; Clover's boot7/CLOVERX64.efi and the
        # UKI both live on it on BIOS), so it's always mounted at /boot/efi.
        efi_line = f"{self.config.part1}  /boot/efi  vfat  defaults  0  2\n"
        content = (
            "# /etc/fstab — ZFS datasets are mounted by ZFS, not fstab\n"
            f"{efi_line}"
        )
        write_file("/mnt/etc/fstab", content)

    def _write_sources_list(self) -> None:
        content = (
            f"deb {self.config.debian_mirror} trixie main contrib non-free non-free-firmware\n"
            f"deb {self.config.debian_mirror} trixie-updates main contrib non-free non-free-firmware\n"
            "deb http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware\n"
        )
        write_file("/mnt/etc/apt/sources.list", content)

    def _write_timezone(self) -> None:
        write_file("/mnt/etc/timezone", f"{self.config.timezone}\n")
        run_in_chroot([
            "ln", "-sf",
            f"/usr/share/zoneinfo/{self.config.timezone}",
            "/etc/localtime",
        ], check=False)
