# FreedomNAS Installer

> **STATUS: PARKED.** Superseded by
> [`freedomnas-image-builder`](../freedomnas-image-builder/) for new
> installs — pre-built, flashable images per ADR-002
> ([IMAGE-BASED-INSTALLATION.md](../IMAGE-BASED-INSTALLATION.md)),
> bootable on both UEFI and BIOS from the same file. This installer is
> kept for reference and possible future refactor (see the ADR's
> follow-up decision) but is **not actively maintained**. It remains
> functional and prints a warning on startup pointing here.

Interactive TUI installer that provisions a FreedomNAS ZFS-on-root system on
Debian. It partitions the disk, creates the ZFS pool and datasets, bootstraps
the base OS, sets up the boot chain, creates users, installs packages, and
leaves the machine ready to boot straight into the FreedomNAS WebUI.

## What it does

- Detects UEFI vs legacy BIOS firmware and adapts the boot chain accordingly
- Partitions the target disk into an EFI System Partition + a full-featured
  ZFS root pool — no separate boot pool, no GRUB
- Installs a minimal Debian base system via `debootstrap`
- Builds a single dracut **Unified Kernel Image (UKI)** — kernel + initramfs
  + cmdline in one PE binary — and wires it into the firmware:
  - **UEFI**: registered directly with `efibootmgr`, no third-party bootloader
  - **BIOS**: bridged to UEFI-mode boot via a vendored, hardware-validated
    build of **Clover** (`boot0`/`boot1`/`boot7`, the BiosBlockIO DUET
    variant)
- Deploys a kernel postinst hook (`zz-dracut-uki`) so every future kernel
  update automatically rebuilds the UKI and rotates a rollback copy
- Creates the admin user (plus any additional users) before packages are
  installed, so PAM/Samba authentication is ready when those services start
- Installs the FreedomNAS package set and sets up the WebUI as an
  init-agnostic first-boot service (cron `@reboot`, with a circuit breaker
  after 5 consecutive failures)
- Persists the installation to `rpool/.freedomnas` as `config.yaml`, and can
  detect that dataset on a later boot to offer **disaster recovery** instead
  of a fresh install

## Requirements

Installed automatically as package dependencies (see [Packaging](#packaging)
below), but for a manual/dev run you need on the live/rescue environment:

- Python 3.7+
- `dialog` (or `whiptail`) for the TUI
- `gdisk`/`gptfdisk` (`sgdisk`), `dosfstools` (`mkfs.vfat`), `parted`
  (`partprobe`)
- `zfsutils-linux`
- `debootstrap` (auto-installed by the installer itself if missing)
- `dracut`, `efibootmgr`
- **`freedomnas-core`** and **`freedomnas-profiles`** — hard runtime
  dependencies. The installer never hardcodes OS commands (see
  [Golden Rule](#the-golden-rule-and-freedomnas-core) below); it resolves
  `dracut`/`efibootmgr` through `freedomnas.engine.get_command()`, which
  needs freedomnas-core installed and freedomnas-profiles' `debian.json` on
  disk.

Root privileges are required (`check_root()` aborts immediately otherwise).

## Usage

```sh
sudo freedomnas-install
```

(installed as `/usr/bin/freedomnas-install`, a symlink to
`/usr/lib/freedomnas/installer/setup.py`). In a development checkout:

```sh
sudo python3 setup.py
```

**Warning:** the installer wipes the selected disk completely — there is no
partition-preserving/dual-boot mode.

## Installation flow

`setup.py`'s `FreedomNASInstaller.run()` executes, in order:

| # | Step | Module |
|---|------|--------|
| 1 | `check_root()` | — |
| 2 | `detect_boot_mode()` — reads `/sys/firmware/efi` | `system_installer.py` |
| 3 | `select_mirror()` — pick a Debian mirror | `system_installer.py` |
| 4 | `prompt_hostname()` | `system_installer.py` |
| 5 | `select_disk()` | `disk_installer.py` |
| 6 | `partition_disk()` | `disk_installer.py` |
| 7 | `format_partitions()` | `disk_installer.py` |
| 8 | `create_zfs_pool()` | `zfs_installer.py` |
| 9 | `install_base_system()` — `debootstrap` | `system_installer.py` |
| 10 | `configure_system()` — hostname, hosts, fstab, sources.list, timezone | `system_installer.py` |
| 11 | `setup_chroot()` — bind-mount `/dev /dev/pts /proc /sys /run` | `system_installer.py` |
| 12 | `get_admin_password()` — before packages | `users_installer.py` |
| 13 | `prompt_additional_users()` — before packages | `users_installer.py` |
| 14 | `install_packages()` | `packages_installer.py` |
| 15 | `install_bootloader()` — UKI + efibootmgr/Clover | `bootloader_installer.py` |
| 16 | `create_firstboot_service()` — WebUI cron `@reboot` | `packages_installer.py` |
| 17 | `_save_installation_config()` — writes `config.yaml` to ZFS | `setup.py` |
| 18 | `cleanup()` — unmount chroot, export pools | `setup.py` |
| 19 | `show_completion()` — offer reboot | `setup.py` |

If a `.freedomnas` dataset is detected on an importable pool before step 2,
the installer instead offers **recovery mode**: it calls into
`freedomnas.reconcile.ReconciliationEngine` to restore the saved
configuration onto the running system instead of doing a fresh install.

Any exception during the flow triggers cleanup (unmount + pool export) and
an error dialog; nothing is left half-mounted.

## Disk and pool layout

Two partitions, identical regardless of firmware — there's no
GRUB-compatible boot pool to carve out and no separate BIOS-boot partition,
since Clover's `boot0`/`boot1` live in the MBR gap and the ESP's own boot
sector rather than a partition of their own:

| Partition | Type | Size | Purpose |
|---|---|---|---|
| 1 | `EF00` (EFI System), FAT32 | 512 MiB | UKI (`debian-uki.efi`), Clover payload on BIOS |
| 2 | `BF00` (Solaris root), ZFS | rest of disk | `rpool` |

`rpool` gets the **full ZFS feature set** — no `compatibility=grub2`
restriction, because nothing at boot time reads ZFS directly anymore (the
UKI's own dracut-built initramfs mounts root via the kernel's ZFS driver).

Datasets:

```
rpool/ROOT                                    (canmount=off)
rpool/ROOT/debian        mountpoint=/          (the OS root; /boot is a plain
                                                 directory here, not its own
                                                 dataset — kernel+initramfs
                                                 live in the UKI on the ESP)
rpool/home                setuid=off
rpool/var                 mountpoint=/var, canmount=off
rpool/var/log
rpool/.freedomnas          canmount=noauto, mountpoint=/etc/freedomnas/state
                            (config.yaml for disaster recovery)
```

`bootfs` is set to `rpool/ROOT/debian` (metadata only, kept for tooling
convention — no boot-time consumer reads it).

## Boot chain

Every install builds **one** UKI via `dracut --force --uefi`, embedding the
cmdline `root=ZFS=rpool/ROOT/debian rw`, written to
`/boot/efi/EFI/Linux/debian-uki.efi`. The command itself is never
hardcoded — it's resolved through
`freedomnas.engine.get_command("bootloader", "build_uki", ...)` against
freedomnas-profiles' `debian.json`, then executed via the installer's own
`run_in_chroot()` (see [Golden Rule](#the-golden-rule-and-freedomnas-core)).

**UEFI machines** — `_register_efi_boot_entry()` calls `efibootmgr --create`
(again resolved via the engine) to register the UKI directly as a boot
entry. No third-party bootloader is installed at all.

**BIOS-only machines** — `_deploy_clover()` deploys a
hardware-validated Clover build (vendored in `assets/clover/`, see its own
[README](assets/clover/README.md) for provenance and exact release/checksums
— **do not bump the version** without re-validating; a later release is a
known regression upstream, see CloverBootloader#740). This step is pure file
I/O, not shell commands, so it needs no profile entry:

1. `boot0` — writes the first 440 bytes of `boot0ss` to the disk's MBR
   (the bootstrap code area only; the disk signature, reserved bytes,
   partition table, and boot signature at bytes 440–511 are preserved)
2. `boot1` — writes `boot1f32alt` to the ESP's own boot sector, merging in
   the *original* FAT32 BIOS Parameter Block (bytes 3–89) so the volume
   stays correctly described
3. Copies `boot7` (the BiosBlockIO DUET firmware), `CLOVERX64.efi`, and
   `BOOTX64.efi` onto the ESP
4. Writes `config.plist`, using `DefaultVolume`/`DefaultLoader`/
   `IgnoreNVRAMBoot=true` (plus `Scan → Entries=true`) rather than relying
   solely on `Custom → Entries` — a real `boot7` bug on some hardware means
   a Custom-only entry doesn't reliably auto-load; this combination is the
   one validated end-to-end on real hardware.

**Both modes** get `assets/zz-dracut-uki` deployed to
`/etc/kernel/postinst.d/zz-dracut-uki` (`chmod +x`) inside the chroot, so
every subsequent kernel update:
- rotates the current UKI to `debian-uki-previous.efi`, paired with the most
  recent `pre-update-*` ZFS snapshot for combined kernel+filesystem rollback
- rebuilds the standard UKI and a `debian-recovery.efi` (single-user,
  read-only root) from the new kernel
- resets the boot-attempt counter in `EFI/FreedomNAS/boot.state`, if that
  directory exists

## The Golden Rule and freedomnas-core

> Python code must never contain a native system command, hardcoded.

This applies to the installer as much as to the core engine and WebUI. The
two genuinely new OS commands this boot chain needs — `dracut` and
`efibootmgr` — are template strings in freedomnas-profiles' `debian.json`
under the `bootloader` category, resolved at runtime via
`freedomnas.engine.get_command()` and executed through the installer's own
`run_in_chroot()` (the engine's own `execute()` has no chroot concept, so
this two-step pattern — resolve via the engine, execute via the installer —
is used instead of `engine.execute()` directly). Pure file/byte I/O (writing
boot sectors, copying the Clover payload, writing `config.plist`,
`/etc/fstab`, etc.) is not a "system command" under this rule and is
ordinary Python I/O, same as the previous GRUB config used `write_file()`.

Because of this, **freedomnas-core must be installed and importable** at
install time — `bootloader_installer.py` does `from freedomnas.engine import
get_engine` unconditionally (unlike the optional/best-effort recovery-mode
and config-save imports elsewhere in `setup.py`, which degrade gracefully if
freedomnas-core is missing).

## Module layout

```
freedomnas-installer/
├── setup.py                       ← entry point, orchestrates all steps
├── lib/
│   ├── installer_modules.py       ← Logger, DialogManager, Configuration,
│   │                                 run_command/run_in_chroot/write_file
│   ├── system_installer.py        ← boot mode, mirror, hostname, debootstrap,
│   │                                 fstab/hosts/sources.list, chroot bind-mounts
│   ├── disk_installer.py          ← disk selection, partitioning, formatting
│   ├── zfs_installer.py           ← rpool + dataset creation
│   ├── bootloader_installer.py    ← UKI build, efibootmgr/Clover boot chain
│   ├── users_installer.py         ← admin + additional user prompts/creation
│   └── packages_installer.py      ← apt package install, first-boot WebUI service
└── assets/
    ├── clover/                    ← vendored, validated Clover binaries + README
    └── zz-dracut-uki               ← /etc/kernel/postinst.d hook (rebuilds UKI
                                       on every kernel update)
```

`Configuration` (in `installer_modules.py`) is the mutable state bag passed
by reference into every module — disk/partition paths, boot mode, ZFS pool
name, hostname, users, etc.

## Packaging

Packaged for both Arch (`PKGBUILD`) and Debian (`debian/`), installing to
`/usr/lib/freedomnas/installer/{setup.py,lib/,assets/}` with a
`/usr/bin/freedomnas-install` entry-point symlink, per the layout documented
in `ARCHITECTURE.md`. Depends on `freedomnas-core`, `freedomnas-profiles`,
`dialog`, `gdisk`/`gptfdisk`, `dosfstools`, `zfsutils-linux`/`zfs-utils`,
`debootstrap`, `dracut`, `efibootmgr`, `parted`.

## Recovery mode

If `select_disk()` hasn't run yet and an importable ZFS pool with a
`.freedomnas` dataset is found, the installer offers to restore from it
instead of doing a fresh install: it loads `freedomnas.config
.ConfigurationManager` and `freedomnas.reconcile.ReconciliationEngine` and
runs `reconciler.reconcile(mode="disaster_recovery")`, applying the saved
`config.yaml` state (hostname, users, pools, ...) to the currently running
system.
