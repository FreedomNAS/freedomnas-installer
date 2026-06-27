"""
FreedomNAS Installer — Package installation and first-boot service.

Steps: install_packages (step 14), create_firstboot_service (step 17).

The first-boot service is a cron @reboot entry that starts the WebUI
supervisor wrapper — init-system-agnostic per spec §9.2.
"""

from __future__ import annotations

from installer_modules import (
    Logger, DialogManager, Configuration,
    run_command, run_in_chroot, write_file,
)


# Core packages required on every FreedomNAS Debian install
_BASE_PACKAGES = [
    "linux-image-amd64",
    "linux-headers-amd64",
    "zfsutils-linux",
    "zfs-initramfs",
    "zfs-zed",
    "grub-efi-amd64",       # pulled in only on UEFI; harmless on BIOS
    "grub-pc",               # BIOS GRUB (apt will choose correctly)
    "dosfstools",            # mkfs.vfat for EFI
    "python3",
    "python3-pip",
    "python3-fastapi",
    "python3-uvicorn",
    "python3-yaml",
    "python3-pam",
    "samba",
    "nfs-kernel-server",
    "topgrade",
    "curl",
    "sudo",
    "locales",
]


class PackagesInstaller:
    """Installs required packages inside the chroot and sets up the first-boot service."""

    def __init__(self, logger: Logger, dialog: DialogManager, config: Configuration) -> None:
        self.logger = logger
        self.dialog = dialog
        self.config = config

    # ------------------------------------------------------------------
    # Public steps
    # ------------------------------------------------------------------

    def install_packages(self) -> None:
        """Install core packages inside the chroot (step 14)."""
        Logger.info("Updating package cache inside chroot...")
        run_in_chroot(["apt-get", "update", "-qq"])

        Logger.info(f"Installing {len(_BASE_PACKAGES)} packages...")
        run_in_chroot(
            ["apt-get", "install", "-y", "--no-install-recommends"] + _BASE_PACKAGES,
            timeout=1800,
        )

        # Install freedomnas-core Python package if available on the mirror
        self._install_freedomnas_core()
        Logger.info("Packages installed successfully")

    def create_firstboot_service(self) -> None:
        """
        Create a cron @reboot entry that starts the WebUI supervisor wrapper.
        This is the init-agnostic approach defined in spec §9.2.
        """
        Logger.info("Creating first-boot service (cron @reboot)...")

        supervisor = "/usr/lib/freedomnas/webui/bin/supervisor.sh"
        cron_entry = f"@reboot root {supervisor} >> /var/log/freedomnas-webui.log 2>&1\n"
        write_file("/mnt/etc/cron.d/freedomnas-webui", cron_entry, mode=0o644)

        # Write the supervisor wrapper script (spec §9.2: circuit breaker after 5 failures)
        supervisor_script = (
            "#!/bin/sh\n"
            "# FreedomNAS WebUI supervisor — init-agnostic (spec §9.2)\n"
            "FAILURES=0\n"
            "MAX_FAILURES=5\n"
            "WEBUI_CMD=\"python3 -m uvicorn freedomnas_webui.api.main:app "
            "--host 0.0.0.0 --port 8080\"\n\n"
            "while true; do\n"
            "    $WEBUI_CMD\n"
            "    EXIT=$?\n"
            "    if [ $EXIT -ne 0 ]; then\n"
            "        FAILURES=$((FAILURES + 1))\n"
            "        echo \"[$(date)] WebUI exited with code $EXIT "
            "(failure $FAILURES/$MAX_FAILURES)\"\n"
            "        if [ $FAILURES -ge $MAX_FAILURES ]; then\n"
            "            echo \"[$(date)] Circuit breaker OPEN — stopping supervisor\"\n"
            "            exit 1\n"
            "        fi\n"
            "        sleep 5\n"
            "    else\n"
            "        FAILURES=0\n"
            "    fi\n"
            "done\n"
        )
        write_file(
            "/mnt/usr/lib/freedomnas/webui/bin/supervisor.sh",
            supervisor_script,
            mode=0o755,
        )
        Logger.info("First-boot service created")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _install_freedomnas_core(self) -> None:
        """
        Install freedomnas-core from PyPI or the local .deb if available.
        Non-fatal: if not available, the user installs it separately.
        """
        result = run_in_chroot(
            ["pip3", "install", "--quiet", "freedomnas-core"],
            check=False,
        )
        if result.returncode == 0:
            Logger.info("freedomnas-core installed via pip")
        else:
            Logger.warn("freedomnas-core not available via pip — install manually")
