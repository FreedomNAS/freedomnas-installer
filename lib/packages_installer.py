"""
FreedomNAS Installer — package selection and installation.

The package set is not a Python list any more: it comes from
freedomnas-profiles/packages/ through freedomnas-core's PackageManager, the
same source the WebUI and the reconciler read. The installer only decides
*which categories* to install; the profile decides what that means on each
edition.

Categories marked required in MANIFEST.json are always installed; the rest
are offered as a checklist and recorded in config.yaml so a later disaster
recovery reinstalls exactly the same set.
"""

from __future__ import annotations

from typing import Dict, List

import corelink
from context import ExecutionContext
from installer_modules import Logger, DialogManager, Configuration


# Preselected in the features checklist, and taken along when a configuration
# is restored: one restored from an image's wizard records no features.
DEFAULT_OPTIONAL = ("samba-cifs", "nfs")

class PackagesInstaller:
    """Selects package categories and installs them into the target."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx
        self.services_not_enabled = []
        pm_module = corelink.package_manager_module()
        self.packages = pm_module.PackageManager(packages_dir=str(ctx.packages_dir))

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select_categories(self) -> None:
        """Required categories are implicit; optional ones are a checklist."""
        required = self.packages.get_required_categories()
        optional = [
            c for c in self.packages.get_categories()
            if not c.get("required", False)
        ]

        items = [
            (c["id"], f"{c['name']} — {c['description'][:44]}", c["id"] in DEFAULT_OPTIONAL)
            for c in optional
        ]
        chosen = self.dialog.checklist(
            "Optional Features",
            "Select the features to install.\n"
            "(Required core packages are always installed.)",
            items,
            height=18,
            width=76,
            list_height=8,
        )
        if chosen is None:
            chosen = []

        self.config.package_categories = required + [
            c for c in chosen if self.packages.validate_category(c)
        ]
        Logger.info(f"Package categories: {', '.join(self.config.package_categories)}")

    def use_default_categories(self) -> None:
        """Non-interactive fallback: required categories only."""
        self.config.package_categories = self.packages.get_required_categories()

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def package_list(self) -> List[str]:
        """Flatten the selected categories into one deduplicated list."""
        edition = self.config.target_os
        seen: List[str] = []
        for category in self.config.package_categories:
            for package in self.packages.get_packages_for_os(
                category, edition, self.ctx.target.init_system
            ):
                if package not in seen:
                    seen.append(package)
        return seen

    def install_packages(self, fresh_root: bool) -> None:
        """
        Install the selected packages into the target.

        fresh_root=True uses `bootstrap.install_base` (the recommends-free
        variant a freshly bootstrapped root wants); in-place installs go
        through the ordinary `packages.install`, which is idempotent and
        therefore only pulls in what is actually missing.
        """
        packages = self.package_list()
        if not packages:
            Logger.warn("No packages selected — skipping package installation")
            return

        self._update_cache(fresh_root)

        if self.setup_repositories():
            # The new sources are only visible after a second refresh.
            self._update_cache(fresh_root)

        Logger.info(f"Installing {len(packages)} packages...")
        if fresh_root and self.ctx.has("bootstrap", "install_base"):
            self.ctx.target_run(
                "bootstrap", "install_base", timeout=3600, package_list=" ".join(packages)
            )
        else:
            self.ctx.target_run(
                "packages", "install", timeout=3600, package_name=" ".join(packages)
            )
        Logger.info("Packages installed")
        self.install_pkgbuilds()
        self.join_admin_groups()

    def join_admin_groups(self) -> None:
        """
        Put the administrator in the groups a selected category needs.

        Docker's socket belongs to the `docker` group: without it every
        update run ends with `permission denied while trying to connect to
        the docker API`, because topgrade's containers step talks to the
        daemon as the signed-in user. This is not a widening of what that
        account can do — `files.sudoers` already gives its group
        `NOPASSWD: ALL` so that topgrade can update the system at all.

        The groups are created by the packages that own them, so this comes
        after the installation, never before.
        """
        username = self.config.username
        if not username:
            return
        edition = self.config.target_os
        groups: List[str] = []
        for category in self.config.package_categories:
            for group in self.packages.get_admin_groups_for_os(
                category, edition, self.ctx.target.init_system
            ):
                if group not in groups:
                    groups.append(group)
        for group in groups:
            Logger.info(f"Adding {username} to the '{group}' group...")
            self.ctx.target_run(
                "users", "add_to_group", check=False, username=username, group=group
            )

    def install_pkgbuilds(self) -> None:
        """
        Clone, build and install the PKGBUILD repositories a category lists —
        the AUR or any other git host.

        They come after the repository packages because they usually depend
        on them — Artix's zfs-openrc and zfs-dinit need the ZFS they provide
        service scripts for — and before services are enabled, since those
        scripts are what the services are.
        """
        edition = self.config.target_os
        pkgbuilds: List[Dict[str, str]] = []
        for category in self.config.package_categories:
            for source in self.packages.get_pkgbuilds_for_os(
                category, edition, self.ctx.target.init_system
            ):
                if source not in pkgbuilds:
                    pkgbuilds.append(source)
        if not pkgbuilds:
            return
        if not self.ctx.has("pkgbuild", "install"):
            names = ", ".join(source["name"] for source in pkgbuilds)
            raise RuntimeError(
                f"{edition} lists PKGBUILD repositories ({names}) "
                "but its installer profile has no pkgbuild.install"
            )
        for source in pkgbuilds:
            Logger.info(f"Building {source['name']} from {source['git']}...")
            self.ctx.target_run(
                "pkgbuild", "install",
                timeout=1800,
                package_name=source["name"],
                git_url=source["git"],
            )

    def ensure_kernel_modules(self) -> None:
        """
        Check that ZFS is installed as a module for every kernel in the target,
        rebuild it where it is not, and refuse to go on if that fails.

        On the editions that build ZFS with DKMS, the package ships sources
        only and pacman's dkms hook compiles them. That hook reports a failed
        build as a warning and then exits 0, so pacman succeeds, the UKI is
        built without ZFS, and the machine drops into the initramfs shell
        looking for a root pool it cannot read — an unbootable system that
        installed without a single error.

        What fails is `make -C module/`, which reaches the top-level `gitrev`
        target and runs scripts/make_gitrev.sh — a file the package does not
        ship. The header that script writes is generated when the package is
        built and is already in place, so an empty script is enough to let the
        build finish.
        """
        if not (self.ctx.has("dkms", "module_present")
                and self.ctx.has("dkms", "fix_gitrev")
                and self.ctx.has("dkms", "autoinstall")):
            return

        for kernel_version in self._target_kernels():
            if self._zfs_module_installed(kernel_version):
                continue
            Logger.warn(
                f"No ZFS module for kernel {kernel_version} — the DKMS build "
                f"failed; rebuilding it"
            )
            self.ctx.target_run("dkms", "fix_gitrev", check=False)
            self.ctx.target_run(
                "dkms", "autoinstall",
                timeout=3600, check=False, kernel_version=kernel_version,
            )
            if not self._zfs_module_installed(kernel_version):
                raise RuntimeError(
                    f"ZFS is not installed as a module for kernel "
                    f"{kernel_version}. The build log is in the target under "
                    f"/var/lib/dkms/zfs/*/build/make.log. Installing further "
                    f"would produce a system that cannot boot."
                )
            Logger.info(f"ZFS module installed for kernel {kernel_version}")

    def _target_kernels(self) -> List[str]:
        modules_dir = self.ctx.target_path("/lib/modules")
        if not modules_dir.is_dir():
            return []
        return sorted(p.name for p in modules_dir.iterdir() if (p / "kernel").is_dir())

    def _zfs_module_installed(self, kernel_version: str) -> bool:
        if self.ctx.dry_run:
            return True
        stdout, _ = self.ctx.target_run(
            "dkms", "module_present", check=False, kernel_version=kernel_version
        )
        return bool(stdout.strip())

    def _update_cache(self, fresh_root: bool) -> None:
        Logger.info(f"Updating package cache ({self.config.target_os})...")
        if fresh_root and self.ctx.has("bootstrap", "update_cache"):
            self.ctx.target_run("bootstrap", "update_cache", timeout=600, check=False)
        else:
            self.ctx.target_run("packages", "update_cache", timeout=600, check=False)

    def setup_repositories(self) -> bool:
        """
        Configure any extra package repository a selected category needs,
        inside the target, before its packages are looked for.

        Debian's container packages come from Docker's own repository rather
        than from Debian, so asking apt for them without adding it first fails
        outright. The steps live in the profile as a command sequence — which
        repository, and how it is trusted, is not something to decide here.

        Returns True if anything was configured.
        """
        configured = False
        for category in self.config.package_categories:
            if not self.ctx.has("repositories", category):
                continue
            Logger.info(f"Configuring the '{category}' repository...")
            self.ctx.target_run(
                "repositories", category,
                timeout=900,
                suite=self.config.suite,
                arch=self.config.arch,
                channel=self.config.channel,
            )
            configured = True
        return configured

    def enable_services(self) -> None:
        """
        Enable each selected category's services through the init profile, so
        this works the same on systemd, OpenRC, runit, s6, dinit and bsd_rc.

        `services.enable_offline` wins when the init profile has it: dinit's
        plain `enable` asks the running daemon, and there is none inside the
        chroot of a system that has not booted yet.

        A service that fails to enable does not stop the installation, but it
        is not swallowed either: a NAS whose Samba never starts is a NAS that
        looks installed and shares nothing.
        """
        edition = self.config.target_os
        init_system = self.ctx.target.init_system
        action = "enable_offline" if self.ctx.has("services", "enable_offline") else "enable"
        early_action = "enable_early" if self.ctx.has("services", "enable_early") else action
        failed = []
        for category in self.config.package_categories:
            groups = (
                (early_action, self.packages.get_early_services_for_os(category, edition, init_system)),
                (action, self.packages.get_services_for_os(category, edition, init_system)),
            )
            for service_action, services in groups:
                for service in services:
                    Logger.info(f"Enabling service: {service}")
                    try:
                        self.ctx.target_run("services", service_action, service_name=service)
                    except Exception as exc:
                        Logger.warn(f"Could not enable {service}: {exc}")
                        failed.append(service)

        self.services_not_enabled = failed
        if failed:
            Logger.error(
                "These services were not enabled and will not start on their "
                f"own: {', '.join(failed)}"
            )

    def disable_ssh(self) -> None:
        """
        A fresh system starts with SSH off: its first account has a password
        someone chose at install time or, on an image, one printed in the
        documentation. It is turned on from the WebUI. Some packages enable
        their service on install (Debian's openssh-server), so it is disabled
        explicitly rather than just left out of the services.
        """
        try:
            service = self.ctx.target.get_profile_value("system", "ssh_service")
        except Exception:
            return
        action = "disable_offline" if self.ctx.has("services", "disable_offline") else "disable"
        Logger.info(f"Disabling service: {service}")
        self.ctx.target_run("services", action, check=False, service_name=service)

    def locale_setup(self) -> None:
        """Generate locales where the edition needs an explicit step."""
        if self.ctx.has("bootstrap", "locale_gen"):
            self.ctx.target_run("bootstrap", "locale_gen", check=False, timeout=300)
