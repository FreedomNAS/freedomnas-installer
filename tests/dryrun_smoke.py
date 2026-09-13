#!/usr/bin/env python3
"""
Dry-run smoke test for the profile-driven installer.

Runs both install modes end to end with every command resolved through the
real profiles but nothing executed (ExecutionContext(dry_run=True)), with the
TUI replaced by canned answers. It verifies the thing that matters most about
this installer: that every step resolves to a real command in
freedomnas-profiles, for a host and a target the test picks explicitly.

    python3 tests/dryrun_smoke.py
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import installer_modules
from context import ExecutionContext
from installer_modules import Configuration, DialogManager
from inplace_installer import InPlaceInstaller
from target_installer import CleanInstaller

PROFILES = str(REPO.parent / "freedomnas-profiles")


class FakeDialog(DialogManager):
    """Canned answers, so the flow runs without dialog(1) or a terminal."""

    menu_answers = {}
    text_answers = {}

    @classmethod
    def menu(cls, title, text, items, *args, **kwargs):
        if title in cls.menu_answers:
            return cls.menu_answers[title]
        return items[0][0]

    # containers is not preselected, but it is the one category that needs a
    # repository configured first, so the dry run has to walk that path.
    extra_categories = ["containers"]

    @classmethod
    def checklist(cls, title, text, items, *args, **kwargs):
        chosen = [tag for tag, _, on in items if on]
        return chosen + [t for t in cls.extra_categories if t not in chosen]

    @classmethod
    def inputbox(cls, title, text, init_text="", *args, **kwargs):
        return cls.text_answers.get(title, init_text or "smoke")

    @classmethod
    def passwordbox(cls, title, text, *args, **kwargs):
        return "smoketest-password"

    # Answering "yes" to everything would loop forever on "add another user?",
    # and answering "no" to the timezone would loop forever on its confirmation.
    yes_titles = {"Administrator Account", "Confirm Timezone"}

    @classmethod
    def yesno(cls, title, text, *args, **kwargs):
        return title in cls.yes_titles

    @classmethod
    def defaultno(cls, title, text, *args, **kwargs):
        return True

    @classmethod
    def msgbox(cls, title, text, *args, **kwargs):
        return None


def run_clean(target_os, target_root, host_os="debian", bootstrap=None):
    print(f"\n=== clean install: {target_os} (host: {host_os}) ===")
    FakeDialog.menu_answers = {"Select Installation Disk": "/dev/sda"}
    if bootstrap:
        FakeDialog.menu_answers["Bootstrap Method"] = bootstrap
    FakeDialog.text_answers = {
        "Rootfs Tarball": f"https://example.invalid/{target_os}-rootfs.tar.xz",
        "Root Pool Name": "fnaspool",
    }
    config = Configuration()
    config.target_os = target_os
    config.target_root = target_root
    ctx = ExecutionContext(
        target_os=target_os,
        target_root=target_root,
        host_os=host_os,
        dry_run=True,
        profile_dir=PROFILES,
    )
    installer = CleanInstaller(FakeDialog, config, ctx)
    assert installer.run() is True
    return config


def run_inplace(edition="debian"):
    print(f"\n=== in-place install: {edition} ===")
    FakeDialog.menu_answers = {"Configuration Storage": "NONE"}
    config = Configuration()
    config.target_os = edition
    ctx = ExecutionContext(
        target_os=edition,
        target_root="/",
        host_os=edition,
        dry_run=True,
        profile_dir=PROFILES,
    )
    installer = InPlaceInstaller(FakeDialog, config, ctx)
    assert installer.run() is True
    return config


def main():
    targets = ExecutionContext.available_targets(PROFILES)
    print("Editions with an installer profile:")
    for target in targets:
        print(f"  {target['edition']:<8} {target['os_family']:<6} "
              f"{target['init_system']:<8} bootstrap={target['bootstrap_methods']}")

    clean = run_clean("debian", "/tmp/freedomnas-smoke-root")
    assert clean.part1 == "/dev/sda1", clean.part1
    # The pool name is asked for now, not assumed
    assert clean.pool_name == "fnaspool", clean.pool_name
    assert clean.root_dataset == "fnaspool/ROOT/debian", clean.root_dataset
    assert "core" in clean.package_categories, clean.package_categories

    # Tarball bootstrap: xbps-install is absent on a Debian host, so the
    # tarball method is the only one left and must carry the whole install.
    void = run_clean("void", "/tmp/freedomnas-smoke-void", bootstrap="tarball")
    assert void.bootstrap_method == "tarball", void.bootstrap_method
    assert void.root_dataset == "fnaspool/ROOT/void", void.root_dataset

    # pacstrap from a host that is not Arch, as on the live ISO: pacman.conf
    # and the keyring come from bootstrap_files, not from the host's /etc.
    arch = run_clean("arch", "/tmp/freedomnas-smoke-arch", host_os="gentoo", bootstrap="pacstrap")
    assert arch.bootstrap_method == "pacstrap", arch.bootstrap_method
    artix = run_clean("artix", "/tmp/freedomnas-smoke-artix", host_os="gentoo", bootstrap="pacstrap")
    assert artix.bootstrap_method == "pacstrap", artix.bootstrap_method

    # Gentoo: stage3 tarball + portage + OpenRC, from a Gentoo host, which is
    # also the case where host and target edition are the same rolling distro.
    gentoo = run_clean("gentoo", "/tmp/freedomnas-smoke-gentoo", host_os="gentoo")
    assert gentoo.bootstrap_method == "tarball", gentoo.bootstrap_method
    assert gentoo.root_dataset == "fnaspool/ROOT/gentoo", gentoo.root_dataset

    # A BSD host sees only BSD targets, and installs them natively.
    freebsd = run_clean("freebsd", "/tmp/freedomnas-smoke-freebsd", host_os="freebsd")
    assert freebsd.bootstrap_method == "tarball", freebsd.bootstrap_method

    inplace = run_inplace()
    assert inplace.target_root == "/"
    assert "core" in inplace.package_categories

    # Gentoo in-place: the accept-keywords drop-in has to be written before
    # emerge runs, or the whole package set is refused on a stable system.
    run_inplace("gentoo")

    linux_host = [t["edition"] for t in targets if t["os_family"] == "linux"]
    bsd_host = [t["edition"] for t in targets if t["os_family"] == "bsd"]
    assert "freebsd" not in linux_host and "freebsd" in bsd_host

    print("\nOK — both modes resolved every command against the real profiles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
