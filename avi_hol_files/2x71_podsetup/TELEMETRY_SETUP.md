# adjustomatic.py startup telemetry

Reports how long each `adjustomatic.py` run took and which steps were
healthy, as one JSON object per run in a GCS bucket, so a fleet of
concurrently-running HOL pods can be checked from one place -- viewed by
regenerating a local HTML summary on demand, not a live-hosted dashboard.

**2026-07-29 additions:**
- **Critical fix:** `lsf.labfail()` was confirmed (against the real
  HOLFY27-MGR-HOLUSER source -- see this project's `CLAUDE.md`) to call
  `sys.exit(1)` and never return. Without `_labfail_uploads_telemetry()`
  wrapping it for the duration of `main()`, a mid-run failure would exit
  before ever reaching the upload at the end of the function -- silently
  producing zero telemetry on exactly the runs where it matters most.
  Fixed: any `labfail()` call now uploads whatever telemetry was
  collected so far (forced `overall_status='failed'`) before the real
  exit happens.
- `write_labstartup_log_summary()` / `summarize_labstartup_log()`: a new
  step reads `~/hol/labstartup.log` (the pod's overall boot log, shared
  across every startup-stage script, not just this one) and logs/uploads
  a real per-section breakdown, using the exact `Starting module: X` /
  `Completed module: X` / `Module X failed: ...` framing confirmed
  against the actual HOLFY27-MGR-HOLUSER `lsfunctions.py`/`labstartup.py`
  source (this lab's own `Startup/final.py` is what actually invokes
  `adjustomatic.main()` -- see `CLAUDE.md`'s new section on that
  framework for the full picture).

## Architecture

```
adjustomatic.py (on manager)
  -> track_step() wraps each main() step, times it, classifies
     ok / degraded / failed by watching for lsf.write_output(...WARNING...)
     and lsf.labfail(...) -- no existing function's internals are touched.
  -> send_telemetry_summary() uploads one JSON object per run to a GCS
     bucket (runs/<pod_id>/<timestamp>.json).
  -> _get_telemetry_access_token() gets the Bearer token for that upload
     by exchanging a personal OAuth refresh token (your own identity, not
     a service account) at Google's OAuth2 token endpoint.
       |
       v
GCS bucket -- one object per run, nothing else.
       |
       v (run manually, whenever you want to check status)
summarize_telemetry.py (on your own workstation, NOT a pod)
  -> lists every object under runs/, downloads each one, renders a
     static HTML table -- opened locally in a browser. Uses
     `gcloud auth print-access-token` for your already-logged-in
     identity -- no stored credential needed on this side at all.
     Regenerate whenever you want a fresh view; no live dashboard,
     no hosting.
```

### Why a personal credential, not a service account

Four separate walls were hit reaching this design (all confirmed
2026-07-28):

1. A human "Sign in with Google" OAuth flow, under a *custom* consent
   screen created in this project, hit `org_internal` -- the GCP project
   (`lans001-avi-demo-labs`) lives under the `labs.broadcom.com` Cloud
   org, a different domain than individual `@broadcom.com` accounts, so
   no human `broadcom.com` sign-in could pass that org-membership check
   for an app *registered in this project*.
2. Pivoting to a service account and sharing a Sheet directly with its
   own email (`...iam.gserviceaccount.com`) was blocked by a Workspace
   sharing-allowlist policy.
3. Pivoting to BigQuery, creating a dataset was denied by project-level
   IAM (`bigquery.datasets.create` missing).
4. Pivoting to a GCS bucket, granting the *service account* IAM access to
   it was denied too (`storage.buckets.setIamPolicy` missing) -- but a
   manual Console upload confirmed the human account that created the
   bucket already has OWNER access to it via GCS's legacy per-bucket ACL
   (automatic for the creating principal), no extra grant needed at all.

The fix that finally worked: authenticate as **yourself**, not a service
account -- but using **gcloud's own pre-registered, globally-available
OAuth client** (the client_id/secret embedded in `gcloud auth
application-default login`'s own output) rather than a *new* consent
screen registered under this project. Wall (1) was specifically about a
custom OAuth client's audience restriction inside this project -- it
doesn't recur here because there's no custom OAuth client at all this
time. Walls (2)/(3)/(4) don't apply either, since this never touches
Sheets/Drive/BigQuery and needs no new IAM grant on the bucket.

Bonus: this is simpler than the service-account version it replaced --
plain OAuth refresh-token grants need no JWT signing, so the
`pyjwt`/`cryptography` dependency is gone entirely.

## Files

| File | Purpose |
|---|---|
| `adjustomatic.py` | Instrumented with `track_step()` / `send_telemetry_summary()` / `_get_telemetry_access_token()`. `TELEMETRY_GCS_BUCKET` near the top needs the bucket name pasted in. |
| `summarize_telemetry.py` | Run locally (not on a pod) whenever you want to check fleet status -- lists/reads every run object in the bucket and writes a static HTML table. Uses your own already-authenticated `gcloud` session, nothing stored. |
| `secrets.yml` | This repo's existing shared ansible-vault file. Holds your personal OAuth credential (client_id/client_secret/refresh_token) under `telemetry_user_credentials_json` (added 2026-07-28) -- no separate dedicated vault file. |

Non-fatal by design: every telemetry code path in `adjustomatic.py`
(`track_step`, `send_telemetry_summary`, `_get_telemetry_access_token`)
catches its own exceptions and never calls `lsf.labfail` or raises past
`main()` -- an unconfigured bucket, a missing/invalid vault entry, a
network blip, or a bug in this code itself can only ever produce a
`WARNING:` log line, never a lab failure. (`summarize_telemetry.py` has
no such constraint -- it never runs during lab startup.)

## Setup (one-time)

1. **Mint your personal OAuth credential**, locally, on your own
   workstation (requires the `gcloud` CLI):
   ```
   gcloud auth application-default login
   ```
   Sign in as the same Broadcom account that already has access to the
   bucket (the one that created it). This writes
   `~/.config/gcloud/application_default_credentials.json` -- open it and
   copy its contents (an `authorized_user`-type JSON: `type`, `client_id`,
   `client_secret`, `refresh_token`).
2. **Add it to the vault**, using the *same* vault password that already
   protects this repo's other files (whatever's in
   `/home/holuser/vaultsecret.txt` on `manager`):
   ```
   cd avi_hol_files/2x71_podsetup
   ansible-vault edit secrets.yml
   ```
   Add a new key with that file's contents as a block scalar:
   ```yaml
   telemetry_user_credentials_json: |
     {"type": "authorized_user", "client_id": "...", "client_secret": "...", "refresh_token": "..."}
   ```
   (The `telemetry_service_account_json` key from the earlier,
   abandoned service-account attempt can stay -- it's unused now, but
   harmless.)
3. Paste the bucket name into `TELEMETRY_GCS_BUCKET` near the top of
   `adjustomatic.py`.
4. Nothing new to install -- `adjustomatic.py`'s telemetry code only
   needs `requests`/`pyyaml`, both already required by the rest of this
   file.
5. Commit and push (ask for this explicitly -- it hasn't been done yet).

## Viewing telemetry

Whenever you want to check fleet status, run this **locally** (requires
`gcloud auth login` once, if you haven't already, as the same account
that has bucket access):
```
python3 summarize_telemetry.py --bucket YOUR_BUCKET_NAME
```
This writes `telemetry_dashboard.html` -- open it in a browser. Re-run
any time for a fresh snapshot; nothing is cached or auto-refreshing.

## Troubleshooting

- **"TELEMETRY_GCS_BUCKET not configured":** step 3 above hasn't been
  done yet.
- **"could not obtain an access token":** either `secrets.yml` is
  missing `telemetry_user_credentials_json`, the vault password on
  `manager` doesn't match what it was encrypted with, or the refresh
  token was revoked (re-run `gcloud auth application-default login` and
  update the vault entry).
- **HTTP 403 on the upload itself (not the token exchange):** your
  account doesn't actually have write access to this bucket after all --
  double-check with a manual Console upload as yourself.
- **`summarize_telemetry.py`: "gcloud auth print-access-token failed":**
  run `gcloud auth login` locally first.
- **`summarize_telemetry.py` errors on individual objects:** it logs a
  warning per object and keeps going rather than aborting the whole run
  -- a single corrupted/partial upload won't block viewing everything
  else.
- Every `adjustomatic.py`-side failure above is logged as a `WARNING:`
  line in the normal adjustomatic output and never fails the lab.
