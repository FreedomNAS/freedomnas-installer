# FreedomNAS Installer

Profile-driven installer for FreedomNAS. It provisions a chosen edition onto
a dedicated disk, **or** turns the system it is running on into a FreedomNAS
— and in both cases every OS command it issues comes from
[freedomnas-profiles](../freedomnas-profiles/) through
[freedomnas-core](../freedomnas-core/)'s engine.

> **Status: active** (dev branch). This supersedes the "parked" state
> recorded in ADR-002's 2026-07-25 follow-up: pre-built images from
> [`freedomnas-image-builder`](../freedomnas-image-builder/) remain the
> *primary* install path, and this installer is the path for machines that
> cannot be flashed, for installs onto an already-running system, and for
> ISO/console installs.

## The two modes

| Mode | What it does | What it touches |
|------|--------------|-----------------|
| **clean** | Partition, root pool, base system, users, packages, FreedomNAS, boot chain, `config.yaml` | The selected disk — erased completely |
| **in-place** | Installs the missing packages, enables their services, wires the WebUI, records the configuration | The running system's package set and config — no disks, no boot chain |

In-place mode is the one that makes the installer follow the same philosophy
as the rest of the project: you install `freedomnas-installer` and
`freedomnas-core` on a machine that already runs a supported OS, and the
installer completes it into a NAS. Package installation goes through the
target's own package manager and is idempotent, so only what is actually
missing gets pulled in.

A third entry, **recovery**, appears when an importable pool already carries
a `.freedomnas` dataset: it hands the saved `config.yaml` to
freedomnas-core's `ReconciliationEngine` instead of installing anything.

```sh
sudo freedomnas-install                       # menu-driven
sudo freedomnas-install --mode inplace        # skip the mode menu
sudo freedomnas-install --mode clean --target void
sudo freedomnas-install --dry-run             # resolve and print, execute nothing
```

`--dry-run` needs no root and touches nothing: it prints every command the
run would execute, resolved against the real profiles. It is the fastest way
to see what a given edition's install actually consists of.

## The one real limitation

Bootstrapping an OS means running that OS's own tooling. `debootstrap`,
`pacstrap`, `xbps-install`, `gpart`/`pkg` — none of them exist across the
Linux/BSD divide. So the installer offers only the editions whose
`os_family` matches its host's:

- a **Linux** installer environment installs Debian, Gentoo, Void, Artix, Arch
- installing **FreeBSD** requires a **FreeBSD** installer environment

That check is data, not code: `os_family` in `profiles/os/os-{edition}.json`.
Within a family, an edition whose native bootstrapper is missing on the host
falls back to the **tarball** method — which is why that method exists.

## How an install is assembled

`ExecutionContext` (`lib/context.py`) owns three engine views, and every step
picks the right one:

| View | Profile | Runs on | Used for |
|------|---------|---------|----------|
| `host_run()` | the host's | the live system | partitioning, root pool, chroot mounts, ESP mount |
| `stage_run()` | the target's | the live system | bootstrap (the target root is still empty) |
| `target_run()` | the target's | inside the target | packages, users, services, boot chain |

`target_run()` is transparently prefixed with the profile's
`chroot.exec_prefix`, so the same call works against `/mnt` during a clean
install and against `/` during an in-place install. That prefix comes from
`FreedomNASEngine.for_target(os_type, root=...)` in freedomnas-core — the
installer never writes `chroot` itself.

### Clean install order

| # | Step | Where it comes from |
|---|------|---------------------|
| 1 | Interview — disk, bootstrap method, mirror, hostname, timezone, features, users | `installer_meta`, `packages/MANIFEST.json` |
| 2 | One confirmation — nothing destructive has happened yet | — |
| 3 | Partition + format ESP | `partitioning.*` |
| 4 | Root pool + datasets | `rootpool.*` |
| 5 | Bootstrap the base system | `bootstrap.<method>` |
| 6 | Write the OS-specific config files | `files.*` |
| 7 | Bind-mount the chroot | `chroot.mount` |
| 8 | Create users (**before** packages, so PAM/Samba find them) | `users.*` |
| 9 | Install packages, generate locales, enable services | `packages/*.json`, `bootstrap.install_base`, `services.enable` |
| 10 | Install core/profiles/webui + first-boot hook | file I/O + `files.webui_cron` |
| 11 | Boot chain | `bootloader.*` |
| 12 | Save `config.yaml` to `.freedomnas` | `ConfigurationManager` |
| 13 | Unmount, export the pool | `chroot.unmount`, `rootpool.export` |

The resulting layout — ESP + `rpool/ROOT/<edition>` + `rpool/.freedomnas` —
is the same one `freedomnas-image-builder` produces, deliberately: an
installed machine and a flashed image must look identical to the reconciler
and to the WebUI.

## Bootstrap methods

Declared per edition in `installer_meta.bootstrap_methods`, filtered at
runtime by whether the host actually has the tool
(`installer_meta.bootstrap_tools`):

- **native** — `debootstrap`, `pacstrap`, `basestrap`, `xbps-install -r`
- **tarball** — download and unpack a rootfs tarball: the distro's own
  `rootfs.tar.gz`/`base.txz`, or an exported container rootfs. Needs nothing
  but `curl`/`fetch` and `tar` on the host, which is what makes adding an
  edition cheap.

## Module layout

```
freedomnas-installer/
├── setup.py                  entry point: mode/edition selection, recovery
├── lib/
│   ├── corelink.py           freedomnas-core import shim (installed vs dev tree)
│   ├── context.py            ExecutionContext — host/stage/target engines
│   ├── installer_modules.py  Logger, DialogManager, Configuration, file I/O
│   ├── disk_installer.py     disk selection, partitioning, ESP format
│   ├── zfs_installer.py      root pool + dataset layout
│   ├── system_installer.py   firmware, prompts, bootstrap, config files, chroot
│   ├── users_installer.py    accounts, passwords via stdin
│   ├── packages_installer.py category selection, install, service enable
│   ├── core_installer.py     core/profiles/webui + first-boot wiring
│   ├── bootloader_installer.py  UKI/Clover or native loader
│   ├── state_installer.py    config.yaml shadow copy
│   ├── target_installer.py   clean-install orchestration
│   └── inplace_installer.py  in-place orchestration
└── tests/dryrun_smoke.py     end-to-end dry run of both modes, no root needed
```

## The Golden Rule

> Python code must never contain a native system command, hardcoded.

What that means here, concretely:

- **Commands** — every one of them lives in a profile, including the chroot
  invocation, the partitioning tools, and the bootstrapper.
- **OS-specific file contents** — `sources.list`, `fstab`, `hosts`,
  `rc.conf`, `loader.conf` — live in the profile's `files` section too. A
  file's text is as distro-specific as a command; keeping it in the profile
  is what stops `if edition == "debian"` from reappearing in Python.
- **Pure byte/file I/O** is not a system command: copying the Clover payload,
  writing boot sectors, copying core/profiles/webui into the target. That
  stays ordinary Python, as it always has.

Because of this, **freedomnas-core and freedomnas-profiles must be present**
before the installer runs — they are hard dependencies, not optional extras.

## Adding an edition

1. `profiles/os/os-<edition>.json` and the per-module fragments (as for any
   edition)
2. `profiles/installer/installer-<edition>.json` — `installer_meta`,
   `chroot`, `partitioning`, `rootpool`, `bootstrap`, `files`
3. `profiles/packages/*.json` — the per-edition package lists
4. `python3 tests/dryrun_smoke.py` to see the whole install resolve

No Python changes. If one is needed, the profile schema is missing something
— fix that instead.

## Requirements

Root, a TUI (`dialog` or `whiptail`), the host-side tools the edition's
`partitioning`/`bootstrap` sections name (`gdisk`, `dosfstools`,
`zfsutils-freedomnas` (or `zfsutils-linux`), `curl`, optionally `debootstrap`), plus **freedomnas-core**
and **freedomnas-profiles**. `--dry-run` requires none of them.

## Packaging

Debian (`debian/`) and Arch (the PKGBUILD in freedomnas-arch), installing to
`/usr/lib/freedomnas/installer/{setup.py,lib/}` with a
`/usr/bin/freedomnas-install` symlink; FreeBSD's package (freedomnas-freebsd)
uses `/usr/local/lib/freedomnas/installer`. The installer finds the core and
the profiles relative to its own location, and writes into a target where
that target's OS profile (`paths.install_root`, `paths.config_dir`) says. Boot-chain assets are **not** vendored
here — they come from `freedomnas-profiles/assets/` (Clover,
`zz-dracut-uki`, `firstboot.sh`, `supervisor.sh`), shared with
freedomnas-image-builder so there is one copy, not two.
