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
  real time.
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
