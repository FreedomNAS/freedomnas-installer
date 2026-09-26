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


def run_clean(target_os, target_root, host_os="debian", bootstrap=None, init=None):
    print(f"\n=== clean install: {target_os} (host: {host_os}) ===")
    FakeDialog.menu_answers = {"Select Installation Disk": "/dev/sda"}
    if bootstrap:
        FakeDialog.menu_answers["Bootstrap Method"] = bootstrap
    if init:
        FakeDialog.menu_answers["Init System"] = init
    FakeDialog.text_answers = {
        "Rootfs Tarball": f"https://example.invalid/{target_os}-rootfs.tar.xz",
        "Root Pool Name": "fnaspool",
        "Release": "15.1-RELEASE",
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
    if init:
        assert ctx.target.init_system == init, ctx.target.init_system
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


def run_disk_pool_checks():
    print("\n=== disk selection: ZFS pools already on the disks ===")
    from disk_installer import DiskInstaller
    from zfs_installer import ZFSInstaller

    listing = {
        ("partitioning", "list_disks"):
            "sda 64000000000 HostDisk\nsdb 64000000000 OldNAS\nsdc 64000000000 Blank\n",
        ("rootpool", "imported_devices"):
            "  pool: rpool\n state: ONLINE\nconfig:\n\n\tNAME         STATE\n"
            "\trpool        ONLINE\n\t  /dev/sda2  ONLINE\n",
    }
    members = {
        "/dev/sda": "/dev/sda\n/dev/sda1 vfat EFI\n/dev/sda2 zfs_member rpool\n",
        "/dev/sdb": "/dev/sdb\n/dev/sdb1 vfat\n/dev/sdb2 zfs_member old\\x20nas\n",
        "/dev/sdc": "/dev/sdc\n",
    }
    ctx = ExecutionContext(
        target_os="debian", target_root="/tmp/freedomnas-smoke-disks",
        host_os="debian", dry_run=True, profile_dir=PROFILES,
    )
    calls = []
    real_host_run = ctx.host_run

    def host_run(category, action, *args, **kwargs):
        calls.append((category, action, kwargs))
        if (category, action) == ("partitioning", "disk_pools"):
            return members[kwargs["disk"]], ""
        if (category, action) in listing:
            return listing[(category, action)], ""
        return real_host_run(category, action, *args, **kwargs)

    ctx.host_run = host_run

    class Picker(FakeDialog):
        choices = ["/dev/sda", "/dev/sdb"]
        menus = []
        boxes = []

        @classmethod
        def menu(cls, title, text, items, *args, **kwargs):
            if title == "Select Installation Disk":
                cls.menus.append(dict(items))
                return cls.choices.pop(0)
            return super().menu(title, text, items, *args, **kwargs)

        @classmethod
        def msgbox(cls, title, text, *args, **kwargs):
            cls.boxes.append(title)

    config = Configuration()
    config.target_os = "debian"
    config.target_root = "/tmp/freedomnas-smoke-disks"
    DiskInstaller(Picker, config, ctx).select_disk()

    labels = Picker.menus[0]
    assert labels["/dev/sda"].endswith("[pool: rpool]"), labels
    assert labels["/dev/sdb"].endswith("[pool: old nas]"), labels
    assert "pool" not in labels["/dev/sdc"], labels
    # The imported pool's disk is refused and the menu comes back.
    assert Picker.boxes == ["Disk In Use"], Picker.boxes
    assert len(Picker.menus) == 2
    assert config.disk == "/dev/sdb", config.disk
    assert config.foreign_pool_devices == ["/dev/sdb2"], config.foreign_pool_devices

    DiskInstaller(Picker, config, ctx).partition_disk()
    order = [(c, a) for c, a, _ in calls]
    assert order.index(("partitioning", "labelclear")) < order.index(("partitioning", "wipe")), order
    assert [k["device"] for c, a, k in calls if a == "labelclear"] == ["/dev/sdb2"]

    ZFSInstaller(Picker, config, ctx).create_root_pool()
    create = next(k for c, a, k in calls if (c, a) == ("rootpool", "create"))
    assert create["force"] == "-f", create
    assert ctx.host.get_command("rootpool", "create", **create).startswith("zpool create -f "), create


def main():
    targets = ExecutionContext.available_targets(PROFILES)
    print("Editions with an installer profile:")
    for target in targets:
        print(f"  {target['edition']:<8} {target['os_family']:<6} "
              f"{target['init_system']:<8} bootstrap={target['bootstrap_methods']}")

    run_disk_pool_checks()

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
    # Artix asks for its init; dinit swaps the bootstrap's init packages, the
    # service scripts (zfs-dinit, openssh-dinit, ...) and the enable command.
    run_clean("artix", "/tmp/freedomnas-smoke-artix-dinit", host_os="gentoo", bootstrap="pacstrap", init="dinit")
    # runit on Artix is not runit on Void: /etc/runit/sv and
    # /etc/runit/runsvdir/default, from init/init-runit-artix.json.
    artix_runit = ExecutionContext(
        target_os="artix", target_root="/tmp/freedomnas-smoke-artix-runit",
        host_os="gentoo", dry_run=True, profile_dir=PROFILES,
    )
    artix_runit.set_target_init("runit")
    enable = artix_runit.target.get_command("services", "enable", service_name="zed")
    assert enable == "ln -sf /etc/runit/sv/zed /etc/runit/runsvdir/default/zed", enable
    run_clean("artix", "/tmp/freedomnas-smoke-artix-runit", host_os="gentoo", bootstrap="pacstrap", init="runit")

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
