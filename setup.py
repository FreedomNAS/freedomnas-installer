#!/usr/bin/env python3
"""
FreedomNAS Installer — entry point.

Profile-driven, edition-agnostic, and built on freedomnas-core exactly like
the WebUI is: every OS command comes from freedomnas-profiles through the
engine, and the installer itself only decides *what* to do and *when*.

Three things it can do:

  clean     Install a chosen edition onto a dedicated disk — partition, root
            pool, bootstrap (native bootstrapper or rootfs tarball), users,
            packages, FreedomNAS, boot chain, config.yaml.
  in-place  Turn the system that is currently running into a FreedomNAS:
            install the missing packages, enable the services, wire the
            WebUI, record the configuration. No disks are touched.
  recovery  Reapply an existing config.yaml found on a `.freedomnas`
            dataset, through freedomnas-core's ReconciliationEngine.

One limitation, and it is a real one: bootstrapping an OS requires that OS's
own tooling, which exists only on a host of the same family. This installer
offers the editions its host can actually build — a Linux installer installs
Linux editions; installing FreeBSD needs a FreeBSD installer environment.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

# realpath, not abspath: the entry point is reached through the
# /usr/bin/freedomnas-install symlink, and abspath would leave it
# unresolved and look for lib/ next to the symlink.
_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_HERE, "lib"))

import corelink
from context import ExecutionContext
from installer_modules import (
    Logger, DialogManager, Configuration, check_root, MODE_CLEAN, MODE_INPLACE,
)
from inplace_installer import InPlaceInstaller
from target_installer import CleanInstaller


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _host_engine(profile_dir: str, assume_host_os: Optional[str]):
    engine_mod = corelink.engine_module()
    if assume_host_os:
        return engine_mod.FreedomNASEngine.for_target(assume_host_os, profile_dir=profile_dir)
    return engine_mod.get_engine(profile_dir)


def _installable_targets(profile_dir: str, host_family: str) -> List[Dict[str, str]]:
    return [
        target for target in ExecutionContext.available_targets(profile_dir)
        if target["os_family"] == host_family
    ]


def _make_target_root(explicit: Optional[str], dry_run: bool) -> Tuple[str, bool]:
    """
    Decide where the target system gets assembled.

    /mnt is shared ground: something may already be mounted there, and
    whatever is under it would be swallowed into the new system — or collide
    with it. A directory made for this run cannot. Returns the path and
    whether we created it, so cleanup knows what to take away again.
    """
    if explicit:
        return explicit, False

    if dry_run:
        return "/mnt/freedomnas-install.dry-run", False

    for parent in ("/mnt", None):
        try:
            return tempfile.mkdtemp(prefix="freedomnas-install.", dir=parent), True
        except OSError:
            continue

    raise RuntimeError("Could not create a directory to assemble the target in")


def _detect_existing_state(ctx: ExecutionContext) -> bool:
    """True if an importable pool carries a .freedomnas dataset."""
    try:
        stdout, _ = ctx.host_run("rootpool", "list_importable", check=False, timeout=60)
    except Exception:
        return False
    return ".freedomnas" in stdout


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def _run_recovery(dialog: DialogManager) -> int:
    """Reapply an existing configuration through the ReconciliationEngine."""
    Logger.info("Recovery mode — restoring from the saved configuration...")
    try:
        engine_mod = corelink.engine_module()
        config_mod = corelink.config_module()
        reconcile_mod = corelink.reconcile_module()
    except corelink.CoreNotAvailable as exc:
        dialog.msgbox("Recovery Failed", str(exc), 12, 70)
        return 1

    engine = engine_mod.get_engine()
    manager = config_mod.ConfigurationManager()
    reconciler = reconcile_mod.ReconciliationEngine(engine, manager)
    results = reconciler.reconcile(mode="disaster_recovery")

    applied = sum(1 for r in results if r.target_state == "applied")
    failed = sum(1 for r in results if r.target_state == "failed")

    dialog.msgbox(
        "Recovery Complete",
        f"Configuration reapplied from the FreedomNAS dataset.\n\n"
        f"  Applied : {applied}\n"
        f"  Failed  : {failed}\n\n"
        "Reboot to complete the recovery.",
        14, 60,
    )
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------

def _select_mode(dialog: DialogManager, recovery_available: bool) -> Optional[str]:
    items = [
        (MODE_CLEAN, "Clean install onto a disk (erases it)"),
        (MODE_INPLACE, "Install onto this running system"),
    ]
    if recovery_available:
        items.append(("recovery", "Restore an existing FreedomNAS configuration"))

    return dialog.menu(
        "FreedomNAS Installer",
        "What would you like to do?",
        items,
        height=14,
        width=70,
        list_height=4,
    )


def _select_target(dialog: DialogManager, targets: List[Dict[str, str]]) -> Optional[str]:
    if not targets:
        return None
    if len(targets) == 1:
        return targets[0]["edition"]

    items = []
    for target in targets:
        label = f"{target['os_name']} — init: {target['init_system']}"
        if target["community"]:
            label += " [community]"
        items.append((target["edition"], label))

    return dialog.menu(
        "Select Edition",
        "Which system should be installed?",
        items,
        height=18,
        width=72,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="freedomnas-install",
        description="Profile-driven FreedomNAS installer",
    )
    parser.add_argument(
        "--mode", choices=[MODE_CLEAN, MODE_INPLACE, "recovery"],
        help="Skip the mode menu",
    )
    parser.add_argument(
        "--target", help="Edition to install (skips the edition menu)",
    )
    parser.add_argument(
        "--target-root", default=None,
        help="Where the target system is assembled in clean mode "
             "(default: a directory made for this run, under /mnt)",
    )
    parser.add_argument(
        "--profiles", help="Path to freedomnas-profiles (default: the engine's search path)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve and print every command instead of running it",
    )
    parser.add_argument(
        "--assume-host-os",
        help="Treat the host as this edition instead of detecting it (testing)",
    )
    args = parser.parse_args()

    if not args.dry_run:
        check_root()

    dialog = DialogManager()
    config = Configuration()

    profile_dir = str(args.profiles or corelink.find_profiles_dir())
    host = _host_engine(profile_dir, args.assume_host_os)
    host_family = host.profile.get("os_family", "unknown")
    config.host_os = host.os_type
    Logger.info(f"Installer host: {host.get_os_info()['os_name']} ({host.os_type}/{host_family})")

    targets = _installable_targets(profile_dir, host_family)
    if not targets:
        Logger.error(
            f"No installable editions for a '{host_family}' host. "
            "Every edition needs an installer/installer-<edition>.json fragment."
        )
        return 1

    probe = ExecutionContext(
        target_os=host.os_type,
        target_root="/",
        host_os=args.assume_host_os,
        dry_run=args.dry_run,
        profile_dir=profile_dir,
    )
    recovery_available = _detect_existing_state(probe)

    mode = args.mode or _select_mode(dialog, recovery_available)
    if not mode:
        Logger.warn("Nothing selected — exiting")
        return 1

    if mode == "recovery":
        return _run_recovery(dialog)

    owns_root = False
    if mode == MODE_INPLACE:
        target_os = host.os_type
        if target_os not in [t["edition"] for t in targets]:
            Logger.error(
                f"No installer profile for '{target_os}'. "
                f"Add profiles/installer/installer-{target_os}.json to freedomnas-profiles."
            )
            return 1
        target_root = "/"
    else:
        target_os = args.target or _select_target(dialog, targets)
        if not target_os:
            Logger.warn("No edition selected — exiting")
            return 1
        target_root, owns_root = _make_target_root(args.target_root, args.dry_run)
        Logger.info(f"Assembling the target in {target_root}")

    config.target_os = target_os
    config.target_root = target_root

    ctx = ExecutionContext(
        target_os=target_os,
        target_root=target_root,
        host_os=args.assume_host_os,
        dry_run=args.dry_run,
        profile_dir=profile_dir,
    )

    if mode == MODE_INPLACE:
        installer = InPlaceInstaller(dialog, config, ctx)
    else:
        installer = CleanInstaller(dialog, config, ctx, owns_target_root=owns_root)

    def _on_interrupt(_signum, _frame):
        Logger.warn("Interrupted — cleaning up")
        if hasattr(installer, "cleanup"):
            installer.cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, _on_interrupt)

    try:
        completed = installer.run()
    except Exception as exc:
        Logger.error(f"Installation failed: {exc}")
        if hasattr(installer, "cleanup"):
            installer.cleanup()
        dialog.msgbox(
            "Installation Failed",
            f"{exc}\n\nSee /var/log/freedomnas-install.log for details.",
            14, 74,
        )
        return 1

    if not completed:
        return 1

    if mode == MODE_CLEAN:
        if dialog.yesno(
            "Installation Complete",
            f"FreedomNAS ({target_os}) has been installed.\n\n"
            f"  Hostname : {config.hostname}\n"
            f"  Admin    : {config.username}\n\n"
            "Reboot now?",
            14, 62,
        ) and not args.dry_run:
            ctx.host_run("system", "reboot", check=False)
    else:
        dialog.msgbox(
            "Installation Complete",
            "FreedomNAS has been installed on this system.\n\n"
            "The WebUI starts at the next boot (cron @reboot -> firstboot.sh),\n"
            "or immediately with:\n\n"
            "  /usr/lib/freedomnas/webui/bin/firstboot.sh &",
            14, 70,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
