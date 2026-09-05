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


class PackagesInstaller:
    """Selects package categories and installs them into the target."""

    def __init__(self, dialog: DialogManager, config: Configuration, ctx: ExecutionContext) -> None:
        self.dialog = dialog
        self.config = config
        self.ctx = ctx
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
            (c["id"], f"{c['name']} — {c['description'][:44]}", c["id"] in ("samba-cifs", "nfs"))
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
            for package in self.packages.get_packages_for_os(category, edition):
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

        Logger.info(f"Updating package cache ({self.config.target_os})...")
        if fresh_root and self.ctx.has("bootstrap", "update_cache"):
            self.ctx.target_run("bootstrap", "update_cache", timeout=600, check=False)
        else:
            self.ctx.target_run("packages", "update_cache", timeout=600, check=False)

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

    def enable_services(self) -> None:
        """
        Enable each selected category's services through the init profile, so
        this works the same on systemd, OpenRC, runit, s6, dinit and bsd_rc.
        """
        edition = self.config.target_os
        for category in self.config.package_categories:
            for service in self.packages.get_services_for_os(category, edition):
                Logger.info(f"Enabling service: {service}")
                self.ctx.target_run(
                    "services", "enable", check=False, service_name=service
                )

    def locale_setup(self) -> None:
        """Generate locales where the edition needs an explicit step."""
        if self.ctx.has("bootstrap", "locale_gen"):
            self.ctx.target_run("bootstrap", "locale_gen", check=False, timeout=300)
