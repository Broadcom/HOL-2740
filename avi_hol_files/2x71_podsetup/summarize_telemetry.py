#!/usr/bin/env python3
"""
Run this LOCALLY, on your own workstation -- not on manager, not on any
pod. Pulls every run summary adjustomatic.py has uploaded to the GCS
bucket (see TELEMETRY_GCS_BUCKET in adjustomatic.py) and renders a static
HTML table you open in a browser. Regenerate on demand; there's no live
dashboard or hosting involved -- just a file you produce fresh whenever
you want to check fleet status.

You're already interactively present when running this (unlike
adjustomatic.py, which runs unattended on manager during pod boot), so
it just shells out to `gcloud auth print-access-token` for your own
already-logged-in identity -- no stored credential, no vault, no key
file needed on this side at all. Requires the `gcloud` CLI installed and
already authenticated (`gcloud auth login` once, if you haven't) as the
same account that has access to the bucket.

Usage:
    python3 summarize_telemetry.py \\
        --bucket YOUR_BUCKET_NAME \\
        [--output telemetry_dashboard.html]

Dependencies: pip install requests
"""
import argparse
import html
import subprocess
import sys
import urllib.parse

import requests

STATUS_COLORS = {"ok": "#2e7d32", "degraded": "#b8860b", "failed": "#c0392b"}


def get_access_token():
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except FileNotFoundError:
        sys.exit("ERROR: gcloud CLI not found -- install the Google Cloud SDK first.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: gcloud auth print-access-token failed -- run `gcloud auth login` first.\n{e.stderr}")
    return result.stdout.strip()


def list_run_objects(bucket, headers):
    names = []
    page_token = None
    while True:
        params = {"prefix": "runs/"}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o",
            headers=headers, params=params, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        names.extend(item["name"] for item in data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return names


def fetch_run(bucket, object_name, headers):
    encoded = urllib.parse.quote(object_name, safe="")
    resp = requests.get(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{encoded}",
        headers=headers, params={"alt": "media"}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def render_html(runs):
    counts = {"ok": 0, "degraded": 0, "failed": 0}
    for r in runs:
        counts[r.get("overall_status", "ok")] = counts.get(r.get("overall_status", "ok"), 0) + 1

    rows_html = []
    for r in runs:
        status = r.get("overall_status", "?")
        color = STATUS_COLORS.get(status, "#888")
        steps = r.get("steps", [])
        steps_html = "".join(
            f'<div><b>{html.escape(s.get("name", "?"))}</b> '
            f'[{html.escape(s.get("status", "?"))}] '
            f'{s.get("duration_s", "?")}s'
            + (f' -- {html.escape(str(s["detail"]))}' if s.get("detail") else '')
            + '</div>'
            for s in steps
        )
        rows_html.append(f"""
        <tr>
          <td>{html.escape(str(r.get('pod_id', '')))}</td>
          <td>{html.escape(str(r.get('started_at', '')))}</td>
          <td>{html.escape(str(r.get('finished_at', '')))}</td>
          <td>{r.get('total_duration_s', '')}</td>
          <td><span style="color:{color};font-weight:bold">{html.escape(status)}</span></td>
          <td><details><summary>{len(steps)} steps</summary>{steps_html}</details></td>
        </tr>""")

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>adjustomatic telemetry</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 2em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; position: sticky; top: 0; }}
  tr:nth-child(even) {{ background: #fafafa; }}
</style></head>
<body>
<h1>adjustomatic.py telemetry</h1>
<p>{len(runs)} runs total -- {counts.get('ok', 0)} ok, {counts.get('degraded', 0)} degraded, {counts.get('failed', 0)} failed</p>
<table>
<tr><th>pod_id</th><th>started_at</th><th>finished_at</th><th>total_duration_s</th><th>overall_status</th><th>steps</th></tr>
{"".join(rows_html)}
</table>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--output", default="telemetry_dashboard.html")
    args = parser.parse_args()

    access_token = get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}

    object_names = list_run_objects(args.bucket, headers)
    print(f"Found {len(object_names)} run objects, fetching...")

    runs = []
    for i, name in enumerate(object_names, 1):
        try:
            runs.append(fetch_run(args.bucket, name, headers))
        except Exception as e:
            print(f"  WARNING: could not fetch {name}: {e}", file=sys.stderr)
        if i % 25 == 0:
            print(f"  ...{i}/{len(object_names)}")

    runs.sort(key=lambda r: r.get("finished_at", ""), reverse=True)

    with open(args.output, "w") as f:
        f.write(render_html(runs))
    print(f"Wrote {args.output} ({len(runs)} runs)")


if __name__ == "__main__":
    main()
