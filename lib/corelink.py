"""
FreedomNAS Installer — freedomnas-core import shim.

freedomnas-core is a hard runtime dependency of the installer: every OS
command comes from the engine, every persisted state change from
ConfigurationManager, every package list from PackageManager. The two ways
it can be present are:

  1. installed  — /usr/lib/freedomnas/core/freedomnas/ (the .deb/PKGBUILD
                  layout, on sys.path via freedomnas-core.pth), imported as
                  the `freedomnas` package
  2. dev tree   — checked out as a sibling directory of this repo, where the
                  modules are importable as plain top-level names (`engine`,
                  `config`, ...), the same way freedomnas-core's own tests
                  import them

This module hides that difference so no installer module has to care.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_REPO_DIR = Path(__file__).resolve().parent.parent

_SEARCH_PATHS = [
    Path("/usr/lib/freedomnas/core"),          # system install (package layout)
    _REPO_DIR.parent / "freedomnas-core",      # dev sibling repo (flat layout)
]


class CoreNotAvailable(RuntimeError):
    """freedomnas-core could not be imported."""


def _load(module_name: str) -> Any:
    """
    Import one freedomnas-core module, trying the package layout first and
    the flat dev layout second.
    """
    try:
        return importlib.import_module(f"freedomnas.{module_name}")
    except ImportError:
        pass

    for path in _SEARCH_PATHS:
        if not path.is_dir():
            continue
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
        try:
            return importlib.import_module(f"freedomnas.{module_name}")
        except ImportError:
            pass
        try:
            return importlib.import_module(module_name)
        except ImportError:
            continue

    raise CoreNotAvailable(
        f"freedomnas-core is required but '{module_name}' could not be imported.\n"
        f"Searched: {', '.join(str(p) for p in _SEARCH_PATHS)}\n"
        "Install the freedomnas-core package, or check it out as a sibling "
        "directory of freedomnas-installer."
    )


def engine_module() -> Any:
    return _load("engine")


def config_module() -> Any:
    return _load("config")


def package_manager_module() -> Any:
    return _load("package_manager")


def reconcile_module() -> Any:
    return _load("reconcile")


def find_profiles_dir() -> Path:
    """
    Resolve the freedomnas-profiles directory, using the same search order
    engine.py uses so the installer and the engine never disagree about
    which profiles are in play.
    """
    candidates = [
        Path("/usr/lib/freedomnas/profiles"),
        _REPO_DIR.parent / "freedomnas-profiles",
    ]
    for candidate in candidates:
        if (candidate / "os").is_dir():
            return candidate
    raise CoreNotAvailable(
        "Could not locate freedomnas-profiles. Checked: "
        + ", ".join(str(c) for c in candidates)
    )


def find_assets_dir() -> Path:
    """Resolve freedomnas-profiles/assets/ (Clover, zz-dracut-uki, firstboot)."""
    assets = find_profiles_dir() / "assets"
    if not assets.is_dir():
        raise CoreNotAvailable(f"freedomnas-profiles/assets not found at {assets}")
    return assets
