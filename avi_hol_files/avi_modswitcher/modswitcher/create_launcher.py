#!/usr/bin/env python3
"""Replacement for the old createlaunch.py. Installs an application-menu
launcher (not a raw Desktop-folder icon) and pins it to the GNOME dash,
so there is no untrusted-launcher trust gate to fight with.

Run this once as the holuser account during lab provisioning. Safe to
re-run - both the launcher install and the pin step are idempotent.
"""
import ast
import subprocess
import sys
from pathlib import Path

APP_ID = "modswitcher"
APP_NAME = "Hands-on Labs Module Switcher"
APP_DIR = Path("/hol/hol-2740/avi_modswitcher/modswitcher")
PYTHON_BIN = "/home/holuser/py312venv/bin/python3"
ENTRYPOINT = APP_DIR / "app.py"
ICON_PATH = APP_DIR / "hol-logo.png"

REQUIRED_PACKAGES = [
    ("PySide6", "PySide6"),
    ("PyYAML", "yaml"),
]
PROXY_INDEX_URL = "https://packages.vcfd.broadcom.net/artifactory/api/pypi/pypi-remote/simple"

DESKTOP_FILE_NAME = f"{APP_ID}.desktop"
APPLICATIONS_DIR = Path.home() / ".local" / "share" / "applications"

DESKTOP_ENTRY = f"""[Desktop Entry]
Version=1.0
Type=Application
Terminal=false
Name={APP_NAME}
Comment=Fast-forward through Hands-on Labs modules
Exec={PYTHON_BIN} {ENTRYPOINT}
Icon={ICON_PATH}
Categories=Education;
"""


def check_python_bin():
    if not Path(PYTHON_BIN).exists():
        print(f"Configured python binary not found: {PYTHON_BIN}")
        print("Create the virtual environment before running this script.")
        sys.exit(1)


def missing_packages():
    missing = []
    for pip_name, import_name in REQUIRED_PACKAGES:
        result = subprocess.run(
            [PYTHON_BIN, "-c", f"import {import_name}"],
            capture_output=True,
        )
        if result.returncode != 0:
            missing.append(pip_name)
    return missing


def install_desktop_entry():
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    desktop_path = APPLICATIONS_DIR / DESKTOP_FILE_NAME
    desktop_path.write_text(DESKTOP_ENTRY)
    # 644, not +x: entries under ~/.local/share/applications are discovered
    # and spawned by GNOME Shell via GDesktopAppInfo, not exec'd directly,
    # so they don't need the executable bit or the GVFS metadata::trusted
    # flag that Desktop-folder files require.
    desktop_path.chmod(0o644)
    return desktop_path


def pin_to_favorites():
    result = subprocess.run(
        ["gsettings", "get", "org.gnome.shell", "favorite-apps"],
        capture_output=True, text=True, check=True,
    )
    raw = result.stdout.strip()
    if raw.startswith("@as "):
        raw = raw[len("@as "):]
    current = ast.literal_eval(raw)

    if DESKTOP_FILE_NAME in current:
        return

    current.append(DESKTOP_FILE_NAME)
    new_value = "[" + ", ".join(f"'{item}'" for item in current) + "]"
    subprocess.run(
        ["gsettings", "set", "org.gnome.shell", "favorite-apps", new_value],
        check=True,
    )


if __name__ == "__main__":
    check_python_bin()

    missing = missing_packages()
    if missing:
        print(f"Missing required packages in {PYTHON_BIN}: {', '.join(missing)}")
        print("Install them via the corporate package proxy cache, then re-run this script:")
        print(f"  {PYTHON_BIN} -m pip install --index-url {PROXY_INDEX_URL} {' '.join(missing)}")
        sys.exit(1)

    desktop_path = install_desktop_entry()
    print(f"Installed launcher at {desktop_path}")
    pin_to_favorites()
    print("Pinned to the dash")
