#!/usr/bin/env python3
"""
vmsp_kvip_keeper.py

Recurring drift-watcher for the VSP cluster's vmsp-platform kube-vip
DaemonSet (fronts the vmsp-gateway Service-type-LoadBalancer VIPs --
fleet-01a among them). See fix_vmsp_gateway_kubevip() in adjustomatic.py
for the one-shot version of this same patch and the full root-cause
writeup.

Why this exists as a separate recurring job: empirically observed that a
one-shot patch of both the live DaemonSet and its HelmRelease does NOT
hold -- something (most likely vmsp-operator regenerating the HelmRelease
from its own source of truth, then Flux re-syncing the DaemonSet to match)
reverted both back to chart defaults within about 3 minutes of patching.
This mirrors the exact class of problem vcfa-stabilizer.sh already solved
for auto-platform-a's own vmsp-platform kube-vip via a 60s drift-watcher
(vcfa-vmsp-kube-vip-keeper.sh) -- this script is the same fix, applied to
the real VSP cluster, installed via our own holuser crontab entry instead
(see install_vmsp_kvip_keeper_cron() in adjustomatic.py) since we don't
have write access to Tools/vsp-health/vsp-health-monitor.py to add this as
a proper check there.

Self-contained by design (no lsfunctions import) so it behaves correctly
when invoked from cron, outside the labstartup lifecycle -- same
convention Tools/vsp-health/*.py already uses and documents.

Logs only when something actually changed (or failed) -- not every cycle
-- to avoid an unbounded, mostly-empty log file from a job that runs every
60 seconds indefinitely.
"""
import base64
import re
import subprocess
import sys
from datetime import datetime

CREDS_FILE = '/home/holuser/creds.txt'
VSP_USER = 'vmware-system-user'
VSP_WORKER_FQDN = 'vsp-01a.site-a.vcf.lab'
VSP_VIP = '10.1.1.142'
LOG_FILE = '/home/holuser/hol/vmsp_kvip_keeper.log'


def log(msg):
    line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def get_password():
    with open(CREDS_FILE) as f:
        return f.read().strip()


def ssh(command, target, password):
    cmd = [
        'sshpass', '-p', password, 'ssh',
        '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'ConnectTimeout=8',
        target, command,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_tcp_port(host, port, timeout=5):
    r = subprocess.run(
        ['bash', '-c', f'timeout {timeout} bash -c "echo > /dev/tcp/{host}/{port}"'],
        capture_output=True,
    )
    return r.returncode == 0


def resolve_cp_ip(password):
    """Same discovery order as fix_vmsp_gateway_kubevip() / kube-fix.py's
    resolve_cp_host(): try the CP VIP directly first (fast path, usually
    reachable), then fall back to reading a worker's own node-agent.conf,
    since vsp-01a.site-a.vcf.lab can land on any node currently holding a
    floating gateway VIP rather than the real control-plane node."""
    if test_tcp_port(VSP_VIP, 22, timeout=5):
        return VSP_VIP
    result = ssh(
        f"echo '{password}' | sudo -S grep server: /etc/kubernetes/node-agent.conf",
        f'{VSP_USER}@{VSP_WORKER_FQDN}',
        password,
    )
    match = re.search(r'https?://([0-9.]+):', result.stdout or '')
    return match.group(1) if match else None


# Shipped base64-encoded to the remote control-plane node and run there
# against admin.conf, so no shell-quoting of the remote command is needed.
PATCH_PY = r"""
import json, subprocess, sys

K = ['kubectl', '--kubeconfig=/etc/kubernetes/admin.conf']
WANT_ENV = {
    'vip_leaseduration': '120',
    'vip_renewdeadline': '90',
    'vip_retryperiod': '10',
    'vip_preserve_on_leadership_loss': 'true',
}

out = subprocess.run(
    K + ['-n', 'vmsp-platform', 'get', 'daemonset', 'kube-vip', '-o', 'json'],
    capture_output=True, text=True,
)
if out.returncode != 0:
    print('DAEMONSET_NOT_FOUND')
    sys.exit(0)

d = json.loads(out.stdout)
c = d['spec']['template']['spec']['containers'][0]
env = c.get('env', []) or []
changed = False
seen = set()
for e in env:
    n = e.get('name')
    if n in WANT_ENV:
        seen.add(n)
        if e.get('value') != WANT_ENV[n]:
            e['value'] = WANT_ENV[n]
            changed = True
for n, v in WANT_ENV.items():
    if n not in seen:
        env.append({'name': n, 'value': v})
        changed = True

lp = c.get('livenessProbe') or {}
if str(lp.get('timeoutSeconds')) != '10':
    lp['timeoutSeconds'] = 10
    changed = True
if str(lp.get('failureThreshold')) != '5':
    lp['failureThreshold'] = 5
    changed = True

if not changed:
    print('ALREADY_HARDENED')
else:
    patch = json.dumps([
        {'op': 'replace', 'path': '/spec/template/spec/containers/0/env', 'value': env},
        {'op': 'replace', 'path': '/spec/template/spec/containers/0/livenessProbe', 'value': lp},
    ])
    res = subprocess.run(
        K + ['-n', 'vmsp-platform', 'patch', 'daemonset', 'kube-vip', '--type=json', '-p', patch],
        capture_output=True, text=True,
    )
    print('PATCHED' if res.returncode == 0 else f'PATCH_FAILED: {res.stderr.strip()}')

hr = subprocess.run(
    K + ['-n', 'vmsp-platform', 'get', 'helmrelease', 'kube-vip'],
    capture_output=True, text=True,
)
if hr.returncode == 0:
    hr_cur = subprocess.run(
        K + ['-n', 'vmsp-platform', 'get', 'helmrelease', 'kube-vip', '-o',
             'jsonpath={.spec.values.env.vip_leaseduration},{.spec.values.env.vip_renewdeadline},'
             '{.spec.values.env.vip_retryperiod},{.spec.values.env.vip_preserve_on_leadership_loss}'],
        capture_output=True, text=True,
    ).stdout
    if hr_cur.strip() == '120,90,10,true':
        print('HELMRELEASE_ALREADY_HARDENED')
    else:
        hr_patch = json.dumps({'spec': {'values': {'env': WANT_ENV}}})
        hr_res = subprocess.run(
            K + ['-n', 'vmsp-platform', 'patch', 'helmrelease', 'kube-vip', '--type=merge', '-p', hr_patch],
            capture_output=True, text=True,
        )
        print('HELMRELEASE_PATCHED' if hr_res.returncode == 0 else f'HELMRELEASE_PATCH_FAILED: {hr_res.stderr.strip()}')
else:
    print('NO_HELMRELEASE')
"""


def main():
    password = get_password()
    cp_ip = resolve_cp_ip(password)
    if not cp_ip:
        log('WARNING: could not resolve VSP control-plane IP -- skipping this cycle')
        return 2

    patch_b64 = base64.b64encode(PATCH_PY.encode()).decode()
    remote_cmd = (
        f"echo {patch_b64} | base64 -d > /tmp/vmsp_kvip_fix.py && "
        f"echo '{password}' | sudo -S python3 /tmp/vmsp_kvip_fix.py; "
        f"rm -f /tmp/vmsp_kvip_fix.py"
    )
    result = ssh(remote_cmd, f'{VSP_USER}@{cp_ip}', password)
    out_text = (result.stdout or '').strip()

    # Steady state (nothing drifted since the last cycle) -- don't spam the
    # log every single minute when there's nothing to report.
    if 'ALREADY_HARDENED' in out_text and 'HELMRELEASE_ALREADY_HARDENED' in out_text:
        return 0

    log(f'cp={cp_ip} result: {out_text or "(no output)"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
