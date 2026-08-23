# Hands-on Labs Module Switcher (prototype)

A PySide6 rewrite of the original `hol_modswitcher` tool. Students pick a
lab SKU, then pick a module (a "fast-forward point"), and the app runs the
associated Ansible playbook with live streaming output.

This directory is still a prototype: it proves out the UI, the manifest
format, and the launcher/provisioning approach. Before deploying to a real
LMC image, see "Moving to production" below for the placeholder values that
still need updating.

## Files

- **`app.py`** — the GUI application. Reads `manifest.yaml`, shows the SKU
  list, then the module list for the chosen SKU, and runs `ansible-playbook`
  against the selected module's playbook in a dialog that streams output in
  real time. Calls `app.setDesktopFileName("modswitcher")` on startup —
  without this, GNOME can't match the *running* window back to the pinned
  dock icon (it matches by window class, not by which script launched it),
  and clicking the dock icon appears to open a second, generic, unrelated
  icon instead of the window merging into the same dock slot. The run
  dialog's output console also explicitly re-scrolls to the bottom after
  each chunk of output — `QPlainTextEdit.appendPlainText()` only follows
  new text if the scrollbar happens to already be exactly at the bottom at
  that instant, which flakes under bursty output (like verbose
  `ansible-playbook -v` output). The fix only snaps to the bottom if the
  view was already there before the append, so manually scrolling up to
  re-read earlier output during a run doesn't get fought — new output
  won't yank you back down until you scroll to the bottom yourself again
  (standard `tail -f`-style behavior).
- **`manifest.yaml`** — all configuration: which SKUs exist, which modules
  each one has, and where each module's playbook lives. Edit this file to
  add/remove/reorder SKUs and modules — no code changes needed.
- **`create_launcher.py`** — provisioning script. Installs an
  application-menu launcher (`~/.local/share/applications/modswitcher.desktop`)
  and pins it to the GNOME dash. Also checks that the target venv has the
  packages the app needs before doing anything else. Run once per LMC image
  during provisioning; safe to re-run.
- **`demo_module_fail.yaml`** — a tiny Ansible playbook that always fails
  (`hosts: localhost`, single `fail` task). It exists only so the manifest
  has an example of what the "failed" state looks like in the run dialog.
  It is not real lab content — remove the manifest entry that points to it
  once you have real modules to show a failure case with, or leave it as a
  smoke test.

## System prerequisites

Beyond the venv having `PySide6` and `PyYAML` (see "Running it" below), the
OS itself needs **`libxcb-cursor0`** installed (`sudo apt install -y
libxcb-cursor0`). Without it, Qt's `xcb` platform plugin fails to load and
the app can't open any window at all. This bit us once already — it's not
optional, and it won't show up as a Python error since it fails before any
Python-level code runs.

## Configuring SKUs and modules (`manifest.yaml`)

The manifest has two top-level sections:

```yaml
config:
  python_bin: null   # see "python_bin" below

skus:
  - id: HOL-2671-01
    title: "HOL-2671-01 - Load balancing VCF apps with Avi"
    modules:
      - name: "Module 1 - Environment Setup"
        description: "Deploys the base Avi Controller and initial cloud connector configuration."
        playbook: ../module-scripts/2671-01-module_1.yaml
      - name: "Module 2 - Configure a Virtual Service"
        description: "Creates the first virtual service and pool fronting the demo app."
        playbook: ../module-scripts/2671-01-module_2.yaml
```

**To add a SKU:** add a new entry under `skus:` with a unique `id`, a
`title` (shown as the card title on the first screen), and a `modules` list.

**To add a module to a SKU:** add an entry to that SKU's `modules:` list
with:

- `name` — shown as the module's title on the second screen.
- `description` — shown as the subtitle/explanation under the name.
- `playbook` — path to the Ansible playbook to run, resolved **relative to
  the manifest file's own directory** (so `../module-scripts/foo.yaml`
  works the same way it does in the sample manifest, no matter where the
  app is installed).

SKUs and modules are shown in the exact order they appear in the file —
there's no sorting, and no filename convention to follow. You can point
`playbook` at any real Ansible playbook; it doesn't need to follow the old
`{sku}-module_{n}.yaml` naming pattern, and it doesn't need a shell-script
wrapper.

### `python_bin`

```yaml
config:
  python_bin: /home/holuser/py312venv/bin/python
```

This tells the app which Python environment's `ansible-playbook` to run
modules with. The app looks for an `ansible-playbook` binary sitting next
to that Python interpreter (i.e. in the same `bin/` directory). This is
separate from whichever Python launches the GUI app itself — the GUI can
be launched by a totally different interpreter (see `create_launcher.py`),
and this setting only controls where `ansible-playbook` is found.

Set it to `null` (or omit it) to fall back to standard `PATH` resolution
(`ansible-playbook` found via `PATH` at the moment the app runs). This is
mainly useful for local testing outside the LMC image, since a
desktop-launched GUI app often has a leaner `PATH` than an interactive
shell — see the comments in `create_launcher.py` for the same concern
applied to the GUI's own interpreter.

## Running it

```
python app.py
```

The venv running `app.py` needs `PySide6` and `PyYAML` installed. It does
**not** need `ansible` installed unless `config.python_bin` (or `PATH`)
happens to point at that same venv — the app just needs to be able to find
an `ansible-playbook` binary somewhere, as described above.

## Provisioning the desktop launcher (`create_launcher.py`)

```
python create_launcher.py
```

Run this once per LMC image (as the `holuser` account) after the app and
its venv are in place. It will:

1. Check that the configured `PYTHON_BIN` exists, and that it has
   `PySide6`/`PyYAML` importable. If anything's missing, it prints the
   exact `pip install` command to run against the corporate package proxy
   cache and exits — **it never installs anything automatically**, since
   automatic installs are unreliable against the proxy cache.
2. Write `~/.local/share/applications/modswitcher.desktop`.
3. Pin it to the GNOME dash (`gsettings` `favorite-apps`).

It intentionally does **not** create an icon on `~/Desktop`. Files dropped
directly into the Desktop folder are subject to GNOME/Nautilus's
untrusted-launcher trust gate (the `chmod`/`gio set metadata::trusted`
dance that sometimes needs a logout/login to take effect). Installing into
the applications directory instead means the launcher is discovered and
run by GNOME Shell directly — no trust gate, no `chmod +x` needed on the
`.desktop` file itself.

Both steps 2 and 3 are idempotent — re-running the script after content
changes is safe and won't create duplicate dash icons.

**If you edit `create_launcher.py` (or `app.py`) on a pod that's already
running**, you have to `scp`/pull the change onto that pod and re-run
`create_launcher.py` yourself. The rsync-on-vApp-startup pipeline only runs
at boot — it will not retroactively update an already-running pod, and the
`.desktop` file on disk won't reflect a fix that only exists in git. This
cost real debugging time twice in a row: a fix looked "applied" because it
was committed, but the actual file on the test pod was still the old one.

## Troubleshooting: dock icon does nothing when clicked

The `.desktop` file has `Terminal=false`, so if the launched process
crashes on startup, you see **nothing** — no window, no error, no dialog.
Don't debug this by staring at the desktop; reproduce it with output you
can actually read:

```bash
# Run the exact Exec= line from the .desktop file directly:
/home/holuser/py312venv/bin/python3 /hol/hol-2740/avi_modswitcher/modswitcher/app.py
```

If that prints a traceback, you've found it. Two specific causes already
bit us on this image and are worth checking first:

1. **`libxcb-cursor0` missing** — see "System prerequisites" above. Error
   looks like `qt.qpa.plugin: Could not load the Qt platform plugin "xcb"`.
2. **`PyYAML` (or another dependency) missing from the venv itself** —
   `ModuleNotFoundError: No module named 'yaml'`.

For #2, **do not trust a normal interactive terminal to verify this** on
this LMC image. `~/.bashrc` and `~/.profile` both export:

```bash
PYTHONPATH=/usr/lib/python3/dist-packages:/hol:/hol/Tools
```

`PYTHONPATH` gets prepended to `sys.path` for *any* process launched from
an interactive shell, regardless of venv isolation (`pyvenv.cfg`'s
`include-system-site-packages = false` does not block it). This means:

- Running `pip install PyYAML` (or even `python3 -m pip install PyYAML`)
  from a normal terminal can report "Requirement already satisfied" by
  finding the **system's** copy in `/usr/lib/python3/dist-packages` —
  while the venv's own `site-packages` genuinely doesn't have it.
- `import yaml` tested interactively will "work" the same misleading way,
  because the leaked system path comes *before* the venv's own
  `site-packages` in `sys.path`.
- The `.desktop` launcher never sources `.bashrc`/`.profile`, so it never
  gets this leaked `PYTHONPATH` — meaning it can fail even when every
  terminal-based check said everything was fine.

The reliable way to check or install, bypassing the leak entirely:

```bash
# Check what's REALLY in the venv:
ssh holuser@<pod> "/home/holuser/py312venv/bin/python3 -c 'import yaml; print(yaml.__file__)'"
# Should print .../py312venv/lib/python3.12/site-packages/yaml/__init__.py
# If it prints /usr/lib/python3/dist-packages/... instead, that's the leak.

# Install for real, with PYTHONPATH explicitly removed for the command:
env -u PYTHONPATH /home/holuser/py312venv/bin/python3 -m pip install PyYAML
```

Running the check via `ssh holuser@<pod> "command"` (rather than an
interactive shell) is itself a reliable way to sidestep `.bashrc` entirely,
since non-interactive SSH commands don't source it.

## Known issue (parked): app doesn't appear in the GNOME app grid

The app is correctly pinned to the GNOME dash and is findable by typing its
name into GNOME's search — both confirmed working. It has not been made to
appear when manually paging through the "Show Applications" grid, even
after a full GNOME Shell session restart (log out/in). This was
investigated thoroughly and ruled out as a config problem on our end:

- `.desktop` file has no `Categories=`, `NoDisplay`, or `Hidden` issues.
- `Gio.AppInfo`/`Gio.DesktopAppInfo` (the same library GNOME Shell itself
  uses) reports `should_show: True`, `NoDisplay: False`, `is_hidden: False`
  for this entry — by the standard freedesktop.org rules, it should render.
- Ruled out stale `gnome-shell` cache (full session restart didn't fix it).
- `org.gnome.shell app-picker-layout` is a curated pin-list of ~21 core
  GNOME apps only; everything else (including this app) is meant to
  auto-fill into remaining grid space, which does happen for other
  third-party apps on this same image — just not (yet) for this one.

Current read: likely a GNOME Shell quirk specific to this heavily
customized HOL image, not a bug in `modswitcher.desktop` or the app. Since
the dock pin and search both work, this was parked rather than chased
further. Revisit if it turns out to matter for students in practice.

## Moving to production

`create_launcher.py`'s constants are set to the real LMC deployment layout:

```python
APP_DIR = Path("/hol/hol-2740/avi_modswitcher/modswitcher")
PYTHON_BIN = "/home/holuser/py312venv/bin/python3"
ENTRYPOINT = APP_DIR / "app.py"
ICON_PATH = APP_DIR / "hol-logo.png"
```

This assumes `avi_modswitcher/` has two sibling folders — `modswitcher/`
(this app: `app.py`, `manifest.yaml`, `create_launcher.py`, etc.) and
`module-scripts/` (the Ansible playbooks) — which is what the manifest's
`../module-scripts/...` relative paths already expect. **`hol-logo.png`
needs to live inside `modswitcher/`, alongside `app.py`** — both the app's
own window/dock icon (`app.py`'s `LOGO_PATH`) and the desktop launcher's
`Icon=` (`create_launcher.py`'s `ICON_PATH`) resolve it from that same
directory.

If this path ever changes (e.g. a different lab number, or a directory
rename), update `APP_DIR` above and re-run `create_launcher.py` — it will
overwrite the existing `.desktop` file in place with corrected paths and
won't duplicate the dash pin.

Also worth checking against your actual LMC image before relying on it in
production:

- Whether `gsettings`/`dconf` writes succeed when `create_launcher.py` runs
  without an active desktop session (e.g. during headless provisioning). If
  not, wrap the `gsettings` calls in `dbus-run-session --`.
- The `demo_module_fail.yaml` manifest entry should be replaced with a real
  module once you have one, or removed.
- Real production playbooks will target actual lab infrastructure (Avi
  Controllers, etc.), not `hosts: localhost` — that's a property of the
  playbooks themselves and isn't something the app needs to know about.
- `libxcb-cursor0` and `PyYAML` (see "System prerequisites" and
  "Troubleshooting" above) were fixed by hand on individual test pods via
  SSH during development. Those fixes live on those specific pods only —
  they need to be folded into whatever builds the base OS packages and
  `py312venv` for the real vApp template, or every fresh clone will hit the
  exact same silent "icon does nothing" failure again.
- `manifest.yaml` and `module-scripts/` still contain the original sample
  HOL-2671 content used for development. Real HOL-2740 SKU/module data and
  the corresponding Ansible playbooks still need to be authored — that's
  the last remaining piece of actual feature work.
