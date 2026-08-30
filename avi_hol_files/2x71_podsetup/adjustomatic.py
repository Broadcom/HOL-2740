import contextlib  # needed at module load time for the @contextlib.contextmanager
                    # decorator below (track_step) -- everything else in this file
                    # imports locally per-function, but a decorator runs at def-time,
                    # too early for a local import inside that same function's body.
import re  # needed at module load time for the compiled LABSTARTUP_LOG_TIMESTAMP_RE
           # constant below -- same reasoning as contextlib above.


def retry_io(fn, *args, retries=3, delay=5, **kwargs):
    """Retry fn on transient I/O errors (errno 5). Re-raises immediately on any other error."""
    import time
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except OSError as e:
            if e.errno == 5 and attempt < retries:
                time.sleep(delay)
                continue
            raise


# manager.site-a.vcf.lab (where adjustomatic runs) does NOT have the
# Supervisor / workload-cluster-* kubectl contexts configured -- verified
# 2026-07-24. console.site-a.vcf.lab does (same host used throughout the
# rest of this codebase -- see lsfunctions.py's own console_host-taking
# helpers, and prelim.py's CONSOLE_HOST usage). Same "SSH to whichever host
# actually has the right kubeconfig/context, run kubectl there" pattern
# used everywhere else (kube-fix.py, vsp-health-monitor.py, VCFfinal.py's
# VSP/VCFA checks) -- just targeting console instead of a VSP/VCFA node.
VKS_KUBECTL_HOST = 'holuser@console.site-a.vcf.lab'
VKS_SUPERVISOR_NS = 'acme-east-prod-wrp4h'
# Scoped down to just workload-cluster-1 (2026-07-27) -- workload-cluster-2
# isn't expected to need the extra capacity, and every worker added here is
# another SubnetPort NSX has to realize during an already-slow scale-out
# (one 2026-07-27 run took 74-101 minutes for 4 new nodes across both
# clusters -- see wait_for_vks_nodepool_scaleup()'s docstring). Halving the
# node count halves that realization work.
VKS_WORKLOAD_CLUSTERS = ('workload-cluster-1',)


def add_cidr_to_ipspace(lsf, ipspace_name, cidr_to_add):
    """
    Add a CIDR to a VCF Automation external IP address space via the provider API.
    Uses VCFA's cloudapi session auth with 'system' org (provider management context).
    Endpoint: /cloudapi/v1/ipSpaces with internalScopeCidrBlocks array.
    Idempotent: no-ops if the CIDR already exists in the space.
    """
    import base64
    import requests

    VCFA_HOST = 'auto-a.site-a.vcf.lab'
    VCFA_ORG = 'system'  # Provider management uses 'system' org
    VCFA_USERNAME = f'admin@{VCFA_ORG}'
    admin_password = lsf.get_password()

    try:
        # Get VCFA provider management access token
        creds = base64.b64encode(f'{VCFA_USERNAME}:{admin_password}'.encode()).decode()
        resp = requests.post(
            f'https://{VCFA_HOST}/cloudapi/1.0.0/sessions/provider',
            headers={
                'Authorization': f'Basic {creds}',
                'Accept': 'application/json;version=9.0.0',
                'Content-Type': 'application/json;version=9.0.0'
            },
            verify=False, timeout=15,
        )
        if resp.status_code != 200:
            lsf.write_output(f'  WARNING: VCFA provider login failed (HTTP {resp.status_code}): {resp.text[:200]}')
            return False

        access_token = resp.headers.get('x-vmware-vcloud-access-token')
        if not access_token:
            lsf.write_output('  WARNING: VCFA login succeeded but no x-vmware-vcloud-access-token header')
            return False

        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json;version=9.0.0',
            'Content-Type': 'application/json'
        }

        # Get all IP spaces to find the one we need
        url = f'https://{VCFA_HOST}/cloudapi/v1/ipSpaces?page=1&pageSize=100'
        resp = requests.get(url, headers=headers, verify=False, timeout=15)

        if resp.status_code != 200:
            lsf.write_output(f'  WARNING: could not list IP spaces (HTTP {resp.status_code}): {resp.text[:200]}')
            return False

        ipspaces_response = resp.json()
        ipspace_list = ipspaces_response.get('values', [])
        ipspace_obj = next((ip for ip in ipspace_list if ip.get('name') == ipspace_name), None)

        if not ipspace_obj:
            lsf.write_output(f'  WARNING: IP space {ipspace_name} not found')
            return False

        # Check if CIDR already exists in internalScopeCidrBlocks
        cidr_blocks = ipspace_obj.get('internalScopeCidrBlocks', []) or []
        if any(block.get('cidr') == cidr_to_add for block in cidr_blocks):
            lsf.write_output(f'  {ipspace_name}: CIDR {cidr_to_add} already present -- no-op')
            return True

        # Add new CIDR block
        import uuid
        new_block = {
            'id': ipspace_name,
            'name': f'{ipspace_name}-{uuid.uuid4().hex[:5]}',
            'cidr': cidr_to_add
        }
        cidr_blocks.append(new_block)
        ipspace_obj['internalScopeCidrBlocks'] = cidr_blocks

        # Update IP space via PUT
        ipspace_id = ipspace_obj.get('id')
        url = f'https://{VCFA_HOST}/cloudapi/v1/ipSpaces/{ipspace_id}'
        resp = requests.put(url, json=ipspace_obj, headers=headers, verify=False, timeout=15)

        if resp.status_code not in (200, 201, 204):
            lsf.write_output(f'  WARNING: failed to update {ipspace_name} (HTTP {resp.status_code}): {resp.text[:200]}')
            return False

        lsf.write_output(f'  {ipspace_name}: added CIDR {cidr_to_add}')
        return True

    except Exception as e:
        lsf.write_output(f'  WARNING: error updating IP space: {e}')
        return False


def scale_vks_worker_nodepools(lsf, target_replicas=3):
    """
    Scale the worker node pool of each cluster in VKS_WORKLOAD_CLUSTERS up
    to target_replicas, via a JSON patch on the Supervisor Cluster resource's
    spec.topology.workers.machineDeployments[0].replicas field --
    equivalent to `kubectl edit cluster <name>`, just non-interactive.
    Idempotent: no-ops any cluster already at or above target_replicas.

    Call this FIRST, at the very start of adjustomatic's main(), so the
    scale-up has the maximum amount of time to converge in the background
    while everything else in this script runs. Pair with
    wait_for_vks_nodepool_scaleup() at the very end to confirm it actually
    finished before the lab is declared ready.

    Non-fatal here: a failure to even issue the scale request is logged as
    a warning, not a lab failure (lsf.labfail is never called from this
    function -- that only happens in the wait function below, if the
    scale-up doesn't converge in time).
    """
    lsf.write_output(f'Scaling VKS worker node pools to {target_replicas} replicas...')
    password = lsf.get_password()

    for cluster_name in VKS_WORKLOAD_CLUSTERS:
        try:
            get_cmd = (
                f"kubectl --context Supervisor -n {VKS_SUPERVISOR_NS} get cluster {cluster_name} "
                f"-o jsonpath='{{.spec.topology.workers.machineDeployments[0].replicas}}'"
            )
            result = lsf.ssh(get_cmd, VKS_KUBECTL_HOST, password)
            current = (getattr(result, 'stdout', '') or '').strip()

            if not current.isdigit():
                lsf.write_output(
                    f'  {cluster_name}: could not read current replica count '
                    f'(got: {current!r}) -- skipping'
                )
                continue

            current_replicas = int(current)
            if current_replicas >= target_replicas:
                lsf.write_output(f'  {cluster_name}: already at {current_replicas} replicas -- no-op')
                continue

            # Ship the JSON patch body base64-encoded rather than embedding
            # it as a literal '-p '...'' argument. lsf.ssh() wraps whatever
            # command string we pass it in its OWN outer double quotes; a
            # raw JSON payload contains unescaped double quotes, which
            # terminates that outer wrapping early and silently mangles the
            # command. (Confirmed live 2026-07-24: the patch call returned
            # rc=0/no-stdout looking like success, but never actually
            # patched anything -- replicas stayed at 1.) Base64 contains no
            # shell-special characters at all, so this is safe regardless
            # of how many quoting layers wrap around it -- same pattern
            # already used for the kube-vip DaemonSet patch above.
            import base64
            patch_json = (
                '[{"op":"replace",'
                '"path":"/spec/topology/workers/machineDeployments/0/replicas",'
                f'"value":{target_replicas}}}]'
            )
            patch_b64 = base64.b64encode(patch_json.encode()).decode()
            patch_cmd = (
                f"echo {patch_b64} | base64 -d | "
                f"kubectl --context Supervisor -n {VKS_SUPERVISOR_NS} patch cluster {cluster_name} "
                f"--type=json --patch-file=/dev/stdin"
            )
            patch_result = lsf.ssh(patch_cmd, VKS_KUBECTL_HOST, password)
            rc = getattr(patch_result, 'returncode', None)
            patch_out = (getattr(patch_result, 'stdout', '') or '').strip()
            patch_err = (getattr(patch_result, 'stderr', '') or '').strip()

            if rc not in (0, None):
                lsf.write_output(
                    f'  WARNING: {cluster_name} scale patch failed (rc={rc}): '
                    f'{patch_err or patch_out or "(no output)"}'
                )
                continue

            lsf.write_output(
                f'  {cluster_name}: scale requested {current_replicas} -> {target_replicas} '
                f'({patch_out or "no output"})'
            )

        except Exception as e:
            lsf.write_output(f'  WARNING: could not scale {cluster_name}: {e}')


def wait_for_vks_nodepool_scaleup(lsf, start_time, target_replicas=3,
                                   timeout_seconds=600, poll_interval=30):
    """
    Poll every cluster in VKS_WORKLOAD_CLUSTERS until each has
    target_replicas worker (non-control-plane) nodes in Ready state, or
    timeout_seconds elapses.

    timeout_seconds=600 (10 min) history -- convergence time has been very
    inconsistent across pod deploys, likely tracking how backlogged NSX
    Manager's realization queue is on a given busy fresh boot (see
    resync_nsxt_alb_cloud_connector_credentials()'s docstring for the same
    root cause showing up as a slow ROTATE task):
      - 2026-07-24: 352s for both clusters (1->3 workers each, one cluster
        had a pre-existing worker briefly flap NotReady mid-scale-up).
      - 2026-07-25: one cluster's replacement nodes never converged at all
        (stuck in an endless CAPI MachineHealthCheck remediation loop all
        night); a second attempt on a fresh pod that same day took
        74-101 minutes across the 4 new nodes, succeeding well past this
        timeout (that run had a 3-minute pre-scale-out settling delay --
        see main() -- and still ran long).
    VKS_WORKLOAD_CLUSTERS was scoped down to just workload-cluster-1 on
    2026-07-27 specifically to cut this realization workload in half and
    see if that materially improves convergence time -- still an open
    question as of this writing.

    Fails the lab (lsf.labfail) if the timeout is hit without every
    cluster reaching target_replicas Ready workers.
    """
    import json
    import time

    lsf.write_output(
        f'Waiting for VKS worker node pools to reach {target_replicas} Ready workers each '
        f'(timeout={timeout_seconds}s)...'
    )
    password = lsf.get_password()
    pending = set(VKS_WORKLOAD_CLUSTERS)

    while pending and (time.time() - start_time) < timeout_seconds:
        for cluster_name in list(pending):
            try:
                cmd = f"kubectl --context {cluster_name} get nodes -o json"
                result = lsf.ssh(cmd, VKS_KUBECTL_HOST, password)
                stdout = getattr(result, 'stdout', '') or ''
                data = json.loads(stdout)

                ready_workers = 0
                for node in data.get('items', []):
                    labels = node.get('metadata', {}).get('labels', {}) or {}
                    if 'node-role.kubernetes.io/control-plane' in labels:
                        continue
                    conditions = node.get('status', {}).get('conditions', []) or []
                    is_ready = any(
                        c.get('type') == 'Ready' and c.get('status') == 'True'
                        for c in conditions
                    )
                    if is_ready:
                        ready_workers += 1

                if ready_workers >= target_replicas:
                    elapsed = time.time() - start_time
                    lsf.write_output(
                        f'  {cluster_name}: reached {ready_workers} Ready workers '
                        f'after {elapsed:.1f}s'
                    )
                    pending.discard(cluster_name)

            except Exception as e:
                lsf.write_output(f'  {cluster_name}: check failed ({e}), will retry')

        if pending:
            time.sleep(poll_interval)

    elapsed = time.time() - start_time
    if not pending:
        lsf.write_output(
            f'VKS worker node pool scale-up: ALL clusters reached {target_replicas} '
            f'Ready workers, total elapsed {elapsed:.1f}s'
        )
    else:
        lsf.write_output(
            f'WARNING: VKS worker node pool scale-up timed out after {elapsed:.1f}s '
            f'(timeout was {timeout_seconds}s) -- still waiting on: {sorted(pending)}'
        )
        lsf.labfail(
            f'VKS worker node pool scale-up did not reach {target_replicas} Ready workers '
            f'within {timeout_seconds}s (still waiting on: {sorted(pending)})'
        )


def fix_vmsp_gateway_kubevip(lsf):
    """
    Harden the VSP cluster's vmsp-platform kube-vip DaemonSet.

    Root cause: this DaemonSet (3 replicas, one per VSP worker; fronts the
    vmsp-gateway Service-type-LoadBalancer VIPs -- fleet-01a among them)
    ships at chart defaults (vip_leaseduration=15, vip_renewdeadline=10,
    vip_retryperiod=2, vip_preserve_on_leadership_loss=false, liveness
    timeoutSeconds=5/failureThreshold=3) that are too tight for a
    resource-constrained nested lab: a brief CPU-scheduling delay makes the
    healthz probe miss its deadline, kubelet kills the pod, and on every
    restart kube-vip drops leader election and releases the gateway VIPs
    for 15-45s -- the "fleet/depot refused, a retry fixes it" symptom.

    This is a DIFFERENT kube-vip instance from the one Tools/vsp-health/
    kube-fix.py and vsp-health-monitor.py's kvip_manifest check already
    harden (that one is the static-pod kube-vip managing the K8s API-server
    VIP, 10.1.1.142). Neither existing tool touches this DaemonSet. The fix
    below mirrors the identical, already-proven hardening applied to
    auto-platform-a's own vmsp-platform kube-vip (see vcfa-stabilizer.sh
    step "6/6: harden vmsp-platform kube-vip DaemonSet"), applied here
    against the real VSP cluster instead.

    UPDATE (2026-07-24): a one-shot patch of the DaemonSet/HelmRelease alone
    does NOT hold -- verified empirically to revert within about 3 minutes,
    and a recurring holuser crontab (vmsp_kvip_keeper.py, installed below)
    was added to keep re-catching that drift every minute. Confirmed live
    that keeper had to re-patch on *every single* 1-minute cycle for 3+
    hours straight with zero steady-state gaps -- not occasional drift, a
    continuous fight.

    UPDATE (2026-07-25): root cause found via the vcf/vmsp GitHub repo
    (vmsp/apps/upstream/kube-vip/releases/kube-vip/release-template.yaml).
    This DaemonSet/HelmRelease pair is itself continuously regenerated by
    vmsp-operator FROM a live ReleaseTemplate custom resource
    (releases.vmsp.vmware.com/v1alpha1, one per installed VSP package,
    named e.g. "kube-vip-v1.0.2-2" in the vmsp-platform namespace) --
    that CR, not the DaemonSet or HelmRelease, is the actual desired-state
    object driving the reconcile loop the keeper was fighting every minute.
    Patching *that* object's spec.helm.values.env (scoped merge-patch --
    only touches the 4 keys below, leaves lb_class_name/svc_election/etc.
    and the unrendered "<% ... %>" template fields alone) makes
    vmsp-operator converge to OUR values instead of the chart's fragile
    defaults, eliminating the fight at the source. Verified live: after
    patching the ReleaseTemplate CR and forcing a Flux reconcile, the
    keeper's log went completely silent (zero PATCHED entries) for 6+
    consecutive 1-minute cycles, vs. 195 consecutive PATCHED cycles
    beforehand.

    This function now does both: patches the live ReleaseTemplate CR first
    (the durable fix -- see UPDATE above), then still directly patches the
    DaemonSet/HelmRelease and forces a Flux reconcile so the running pods
    are actually correct by the time this labstartup run finishes, rather
    than waiting on Flux's 10m chart reconcile interval. Each pod deploy
    gets a fresh VSP instance with the chart's fragile defaults, so this
    needs to run at startup every time -- it isn't a one-time fix to this
    pod.

    We don't have write access to Tools/vsp-health/vsp-health-monitor.py to
    add this as a proper check on the manager VM the way that tool would
    normally handle ongoing drift protection, so this function also
    installs (idempotently) a holuser crontab entry that re-runs the
    DaemonSet/HelmRelease patch every minute via vmsp_kvip_keeper.py -- see
    install_vmsp_kvip_keeper_cron() below. With the ReleaseTemplate fix in
    place, this keeper should normally stay silent (nothing left to catch)
    -- it's kept installed as defense-in-depth in case a future VSP package
    upgrade mid-session re-installs a fresh ReleaseTemplate with the
    fragile defaults. Plain crontab, not systemd: same constraint
    vsp-health-monitor.py documents (holuser's sudoers cannot install
    systemd units) and the same reason that tool's own recurring schedule
    is a crontab entry too. It runs on the manager VM, not any VSP node, so
    it isn't lost when CAPI rolling-replaces a VSP control-plane node --
    vmsp_kvip_keeper.py re-resolves the current CP IP every cycle.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup (lsf.labfail is never called).
    """
    import base64
    import re

    lsf.write_output('Checking VSP vmsp-platform kube-vip DaemonSet hardening...')
    password = lsf.get_password()
    vsp_user = 'vmware-system-user'
    vsp_worker_fqdn = 'vsp-01a.site-a.vcf.lab'
    vsp_vip = '10.1.1.142'

    try:
        # ---- Resolve the real control-plane node ----
        # vsp-01a.site-a.vcf.lab is a floating gateway VIP hostname and can
        # land on any worker currently holding it, not necessarily a node
        # with a populated admin.conf. Discover the real CP IP the same way
        # kube-fix.py's resolve_cp_host() does: try the CP VIP directly
        # first, then fall back to reading a worker's own node-agent.conf.
        cp_ip = vsp_vip if lsf.test_tcp_port(vsp_vip, 22, timeout=5) else None
        if not cp_ip:
            result = lsf.ssh(
                f"echo '{password}' | sudo -S grep server: /etc/kubernetes/node-agent.conf",
                f'{vsp_user}@{vsp_worker_fqdn}'
            )
            stdout = getattr(result, 'stdout', '') or ''
            match = re.search(r'https?://([0-9.]+):', stdout)
            cp_ip = match.group(1) if match else None

        if not cp_ip:
            lsf.write_output(
                '  WARNING: could not resolve VSP control-plane IP -- '
                'skipping vmsp-platform kube-vip hardening'
            )
            return

        lsf.write_output(f'  VSP control-plane IP: {cp_ip}')
        cp_ssh_target = f'{vsp_user}@{cp_ip}'

        # ---- Patch script: run kubectl remotely against admin.conf ----
        # Built as a standalone python3 script and shipped base64-encoded
        # so no shell-quoting of the remote command is required at all.
        patch_py = r"""
import json, subprocess, sys
from datetime import datetime, timezone

K = ['kubectl', '--kubeconfig=/etc/kubernetes/admin.conf']
WANT_ENV = {
    'vip_leaseduration': '120',
    'vip_renewdeadline': '90',
    'vip_retryperiod': '10',
    'vip_preserve_on_leadership_loss': 'true',
}

# ---- 0. Patch the live ReleaseTemplate CR -- the actual desired-state ----
# object vmsp-operator continuously reconciles the HelmRelease from (see
# fix_vmsp_gateway_kubevip()'s docstring). This is the durable fix: without
# it, steps 1/2 below get regenerated back to chart defaults on whatever
# cadence vmsp-operator re-ticks (empirically <=60s). Scoped merge-patch on
# just spec.helm.values.env -- leaves every other field (including the
# unrendered "<% ... %>" template placeholders elsewhere in this object)
# untouched.
rt_list = subprocess.run(
    K + ['-n', 'vmsp-platform', 'get', 'releasetemplate', '-o', 'json'],
    capture_output=True, text=True,
)
rt_name = None
if rt_list.returncode == 0:
    for item in json.loads(rt_list.stdout).get('items', []):
        labels = item.get('metadata', {}).get('labels', {}) or {}
        if labels.get('packages.vcf.vmware.com/name') == 'kube-vip':
            rt_name = item['metadata']['name']
            rt_env = (item.get('spec', {}).get('helm', {})
                          .get('values', {}).get('env', {}) or {})
            break

if not rt_name:
    print('RELEASETEMPLATE_NOT_FOUND')
elif all(rt_env.get(k) == v for k, v in WANT_ENV.items()):
    print('RT_ALREADY_HARDENED')
else:
    rt_patch = json.dumps({'spec': {'helm': {'values': {'env': WANT_ENV}}}})
    rt_res = subprocess.run(
        K + ['-n', 'vmsp-platform', 'patch', 'releasetemplate', rt_name,
             '--type=merge', '-p', rt_patch],
        capture_output=True, text=True,
    )
    print('RT_PATCHED' if rt_res.returncode == 0 else f'RT_PATCH_FAILED: {rt_res.stderr.strip()}')

# ---- 1. Patch the live DaemonSet directly for immediate effect ----
# (Flux's own chart reconcile interval is 10m -- too slow to rely on for
# "fixed by the time this labstartup run finishes"; step 0 above is what
# keeps this from drifting back afterward instead of a one-shot patch.)
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

# ---- 2. Patch the HelmRelease directly too (belt-and-suspenders in case ----
# this races with an in-progress vmsp-operator reconcile of step 0 above),
# then force Flux to reconcile it right now instead of waiting up to its
# own 10m interval -- so the DaemonSet doesn't have to rely solely on step
# 1's direct patch to already be correct by the time labstartup finishes.
hr = subprocess.run(
    K + ['-n', 'vmsp-platform', 'get', 'helmrelease', 'kube-vip'],
    capture_output=True, text=True,
)
if hr.returncode == 0:
    hr_patch = json.dumps({'spec': {'values': {'env': WANT_ENV}}})
    hr_res = subprocess.run(
        K + ['-n', 'vmsp-platform', 'patch', 'helmrelease', 'kube-vip', '--type=merge', '-p', hr_patch],
        capture_output=True, text=True,
    )
    print('HELMRELEASE_PATCHED' if hr_res.returncode == 0 else f'HELMRELEASE_PATCH_FAILED: {hr_res.stderr.strip()}')

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    reconcile_res = subprocess.run(
        K + ['-n', 'vmsp-platform', 'annotate', 'helmrelease', 'kube-vip',
             f'reconcile.fluxcd.io/requestedAt={now}', '--overwrite'],
        capture_output=True, text=True,
    )
    print('FLUX_RECONCILE_REQUESTED' if reconcile_res.returncode == 0 else f'FLUX_RECONCILE_FAILED: {reconcile_res.stderr.strip()}')
else:
    print('NO_HELMRELEASE')
"""
        patch_b64 = base64.b64encode(patch_py.encode()).decode()
        remote_cmd = (
            f"echo {patch_b64} | base64 -d > /tmp/vmsp_kvip_fix.py && "
            f"echo '{password}' | sudo -S python3 /tmp/vmsp_kvip_fix.py; "
            f"rm -f /tmp/vmsp_kvip_fix.py"
        )
        result = lsf.ssh(remote_cmd, cp_ssh_target)
        out_text = (getattr(result, 'stdout', '') or '').strip()
        lsf.write_output(f'  vmsp-platform kube-vip hardening result: {out_text or "(no output)"}')

    except Exception as e:
        lsf.write_output(f'  WARNING: vmsp-platform kube-vip hardening failed: {e}')

    install_vmsp_kvip_keeper_cron(lsf)


def install_vmsp_kvip_keeper_cron(lsf):
    """
    Install/refresh a holuser crontab entry that re-runs
    vmsp_kvip_keeper.py (the standalone, recurring version of the patch
    above) every minute. See fix_vmsp_gateway_kubevip()'s docstring for why
    this is necessary -- the one-shot patch alone was verified not to hold.

    Idempotent: replaces any prior copy of our own cron line (matched via
    CRON_MARKER) rather than appending a duplicate every boot. Mirrors
    Tools/vsp-health/vsp-health-monitor.py's own install_timer() pattern,
    but as a completely independent cron line/marker so this never
    conflicts with or requires editing that file.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup.
    """
    import os
    import subprocess

    CRON_MARKER = '# vmsp-gateway-kvip-keeper (adjustomatic-installed)'
    keeper_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'vmsp_kvip_keeper.py'
    )

    if not os.path.isfile(keeper_script):
        lsf.write_output(
            f'  WARNING: {keeper_script} not found -- skipping kvip-keeper cron install'
        )
        return

    try:
        cron_line = f"* * * * * /usr/bin/python3 {keeper_script} {CRON_MARKER}"

        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        existing = r.stdout.splitlines() if r.returncode == 0 else []
        lines = [l for l in existing if 'vmsp_kvip_keeper.py' not in l]
        lines.append(cron_line)
        payload = '\n'.join(lines).rstrip('\n') + '\n'

        wr = subprocess.run(['crontab', '-'], input=payload, capture_output=True, text=True)
        if wr.returncode == 0:
            lsf.write_output('  vmsp-gateway-kvip-keeper cron job installed/refreshed (every 1 min)')
        else:
            lsf.write_output(
                f'  WARNING: could not install kvip-keeper cron job (rc={wr.returncode}): '
                f'{wr.stderr.strip()[:200]}'
            )
    except Exception as e:
        lsf.write_output(f'  WARNING: could not install kvip-keeper cron job: {e}')


# SSH target for the VCFA (VMware Cloud Foundation Automation) appliance's
# own single-node control-plane VM -- a completely separate kube-vip
# instance from the vmsp-platform gateway one hardened above. VCFA's own
# "vcfa-stabilizer.sh" (not part of this repo -- lives only on the live
# appliance/console, found and read live during the 2026-07-30 session)
# already hardens this node's kube-vip *lease/renew/retry timing* via its
# own "Phase 1.5" marker, but that hardening only ever bumped
# vip_renewdeadline (default 10s -> 90s) without correspondingly raising
# vip_leaseduration (left at its default 60s) -- an invalid combination
# per Kubernetes' own leaderelection invariant (leaseDuration must exceed
# renewDeadline, which must exceed retryPeriod). kube-vip panics on
# startup rather than merely misbehaving when this is violated, so the
# static pod (kubelet-managed, not a Deployment) crash-loops forever with
# no auto-recovery. Confirmed live: 111 restarts over 22h before this was
# found and fixed by hand.
VCFA_SSH_HOST = 'vmware-system-user@10.1.1.72'
VCFA_KUBE_VIP_MANIFEST = '/etc/kubernetes/manifests/kube-vip.yaml'


def fix_vcfa_kube_vip_lease_invariant(lsf):
    """
    Idempotently ensure the VCFA appliance's own control-plane kube-vip
    static pod manifest satisfies vip_leaseduration > vip_renewdeadline >
    vip_retryperiod. See VCFA_SSH_HOST's comment above for the full root
    cause: this doesn't just correct the one 60/90 combination found live
    on 2026-07-30 -- it re-derives a safe leaseduration from whatever
    renewdeadline/retryperiod are *currently* set to, so it stays correct
    even if vcfa-stabilizer.sh's own values change in a future version.

    This is a static pod (kubelet watches the manifest file directly, no
    Deployment/DaemonSet to patch or rollout-restart) -- rewriting the file
    is the fix; kubelet picks up the change and restarts the pod itself,
    typically within a few seconds.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup.
    """
    import base64

    lsf.write_output('Checking VCFA kube-vip lease/renew/retry invariant...')
    password = lsf.get_password()

    fix_py = f"""
import re, sys

MANIFEST = '{VCFA_KUBE_VIP_MANIFEST}'

def get_val(content, name):
    m = re.search(r'- name: ' + name + r'\\s*\\n\\s*value: "(\\d+)"', content)
    return int(m.group(1)) if m else None

with open(MANIFEST) as f:
    content = f.read()

lease = get_val(content, 'vip_leaseduration')
renew = get_val(content, 'vip_renewdeadline')
retry = get_val(content, 'vip_retryperiod')

if lease is None or renew is None:
    print(f'FIELDS_NOT_FOUND lease={{lease}} renew={{renew}} retry={{retry}}')
    sys.exit(0)

if lease > renew > (retry or 0):
    print(f'ALREADY_VALID lease={{lease}} renew={{renew}} retry={{retry}}')
    sys.exit(0)

new_lease = renew + 30
new_content = re.sub(
    r'(- name: vip_leaseduration\\s*\\n\\s*value: )"\\d+"',
    lambda m: m.group(1) + f'"{{new_lease}}"',
    content,
)
with open(MANIFEST, 'w') as f:
    f.write(new_content)
print(f'FIXED lease={{lease}}->{{new_lease}} renew={{renew}} retry={{retry}}')
"""
    fix_b64 = base64.b64encode(fix_py.encode()).decode()
    remote_cmd = (
        f"echo {fix_b64} | base64 -d > /tmp/vcfa_kvip_fix.py && "
        f"echo '{password}' | sudo -S python3 /tmp/vcfa_kvip_fix.py; "
        f"rm -f /tmp/vcfa_kvip_fix.py"
    )
    try:
        result = lsf.ssh(remote_cmd, VCFA_SSH_HOST, password)
        out_text = (getattr(result, 'stdout', '') or '').strip()
        lsf.write_output(f'  {out_text or "(no output)"}')
    except Exception as e:
        lsf.write_output(f'  WARNING: could not check/fix VCFA kube-vip lease invariant: {e}')


def fix_firefox_remote_settings_bypass(lsf):
    """
    Disable Firefox's Remote Settings network calls on the console jump
    host, so opening the "Not Secure" identity panel (and any other UI
    path that triggers a Remote Settings freshness check) doesn't hang.

    Root cause (found 2026-07-27 via live tcpdump on a pod reporting
    "Firefox is extremely slow to load pages / open modals"):
    /etc/firefox/policies/policies.json already locks down the classic
    causes of that symptom -- Safe Browsing, telemetry, DoH, and prefetch
    are all disabled there, and the configured HTTP/SSL proxy
    (proxy.site-a.vcf.lab:3128) answers every blocked external
    destination with an instant 403, so it can't be the source of a
    multi-second hang either. But Remote Settings
    (firefox.settings.services.mozilla.com -- syncs Nimbus/tracking-
    protection config, polled when the identity panel opens) resolves
    fine via the internal DNS resolver, then opens a TCP connection
    *directly* to the real Fastly IP it resolved to -- bypassing the
    configured proxy entirely. This pod has no route to the real
    internet on that direct path, so the SYN is silently dropped (no
    SYN-ACK, no RST) and Firefox sits retrying with exponential backoff
    for 60+ seconds before giving up and rendering -- the "stalls, then
    pops open" symptom.

    Fix attempted here: add "services.settings.server": "" to the
    policy's existing Preferences block -- the same mechanism already
    used there for Safe Browsing/telemetry/etc.

    UPDATE (2026-07-27): verified via live tcpdump immediately after a
    full Firefox restart that this fix is NOT sufficient on its own --
    the identical DNS query + direct-to-Fastly SYN still fired. Root
    cause of *that*: Mozilla deliberately ignores any override of
    services.settings.server on Release/ESR channel builds (this
    pod runs Release) unless Firefox is launched with the
    MOZ_REMOTE_SETTINGS_DEVTOOLS=1 environment variable -- see
    services/settings/Utils.sys.mjs's SERVER_URL getter (falls back to
    the hardcoded AppConstants.REMOTE_SETTINGS_SERVER_URLS[0] whenever
    the override isn't "allowed") and Mozilla bug 1598562,
    https://bugzilla.mozilla.org/show_bug.cgi?id=1598562, "Prevent
    Remote Settings server URL to be modified in release". This is
    intentional hardening on Mozilla's part -- Remote Settings delivers
    security-relevant data (cert revocation, malicious-extension kill
    switches), so they don't want a compromised/malicious policy able to
    silently redirect or disable it. There's no supported pref/policy
    path around this from our side; see
    fix_firefox_remote_settings_dns_block() below for the actual fix
    (block the domain at the pod's own DNS server instead, where
    Firefox has no equivalent override to fall back from).

    Left in place anyway as harmless defense-in-depth (e.g. in case a
    future Firefox update on this image ships on a channel where the
    override *is* honored) -- it just doesn't fully solve this on its
    own on the current Release build.

    Idempotent: no-ops (ALREADY_APPLIED) once the pref is already set to
    the desired value, so safe to run on every pod boot even though the
    saved vApp template should already have it applied -- this also
    self-heals it if a future template rebuild drops it. Writes via a
    temp file + atomic replace on the remote host so a mid-write failure
    can't leave policies.json truncated/invalid (which would silently
    disable every other policy in the file, including the proxy lock).

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup (lsf.labfail is never called) -- this is a UX papercut
    fix, not something worth blocking the lab on.
    """
    import base64

    lsf.write_output('Checking Firefox Remote Settings proxy-bypass fix on console...')
    password = lsf.get_password()
    console_host = 'holuser@console.site-a.vcf.lab'

    patch_py = r"""
import json, os

PATH = '/etc/firefox/policies/policies.json'
KEY = 'services.settings.server'
WANT = ''

try:
    with open(PATH) as f:
        data = json.load(f)

    prefs = data.setdefault('policies', {}).setdefault('Preferences', {})

    if prefs.get(KEY) == WANT:
        print('ALREADY_APPLIED')
    else:
        prefs[KEY] = WANT
        tmp_path = PATH + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
        os.replace(tmp_path, PATH)
        print('PATCHED')
except Exception as e:
    print(f'FAILED: {e}')
"""
    try:
        patch_b64 = base64.b64encode(patch_py.encode()).decode()
        remote_cmd = (
            f"echo {patch_b64} | base64 -d > /tmp/firefox_policy_fix.py && "
            f"echo '{password}' | sudo -S python3 /tmp/firefox_policy_fix.py; "
            f"rm -f /tmp/firefox_policy_fix.py"
        )
        result = lsf.ssh(remote_cmd, console_host)
        out_text = (getattr(result, 'stdout', '') or '').strip()
        lsf.write_output(f'  firefox remote-settings proxy-bypass fix result: {out_text or "(no output)"}')
    except Exception as e:
        lsf.write_output(f'  WARNING: could not apply firefox remote-settings proxy-bypass fix: {e}')


def fix_firefox_remote_settings_dns_block(lsf):
    """
    Block firefox.settings.services.mozilla.com at the pod's own
    Technitium DNS server (holorouter, 10.1.10.129) so Firefox's Remote
    Settings sync fails instantly (NXDOMAIN) instead of hanging for 60+
    seconds. This is the actual fix for the identity-panel/page-load
    stall fix_firefox_remote_settings_bypass() above documents and
    attempts (and, per that function's 2026-07-27 UPDATE, does not fully
    solve on its own on Release-channel Firefox).

    Verified live 2026-07-27 via Technitium's REST API on the reported
    pod:
      - Technitium's zone-type API rejects "Blocked" as a zone type in
        this version (14.3) -- blocking domains is a separate feature
        with its own API namespace, /api/blocked/*, distinct from
        /api/zones/*.
      - /api/blocked/add?domain=<fqdn> is idempotent by itself (re-adding
        an already-blocked domain returns a plain {"status":"ok"}, no
        error, no duplicate) -- confirmed live by calling it twice.
      - The block is scoped to the exact FQDN, not its parent domain:
        after blocking firefox.settings.services.mozilla.com,
        addons.mozilla.org (a different mozilla.com subdomain) still
        resolved normally. Technitium's /api/blocked/list groups its
        summary view by registrable domain (e.g. shows a "mozilla.com"
        bucket), which looks like a whole-domain block at a glance but
        isn't one -- drilling in with /api/blocked/list?domain=<fqdn>
        confirms the NS/SOA records Technitium synthesizes for the block
        live exactly at the blocked FQDN, not at the registrable-domain
        apex.
      - Firefox has no equivalent override to fall back from here the
        way it does for the app-level services.settings.server pref
        (see fix_firefox_remote_settings_bypass()'s UPDATE) -- DNS is
        DNS regardless of which internal Firefox subsystem is asking, so
        this fix doesn't care whether a future Firefox version answers
        this particular request from the JS Remote Settings client, a
        Rust-based one, or something else entirely.

    Deliberately narrow: only blocks this one FQDN, confirmed via live
    packet capture to be the actual cause of this pod's reported hang --
    not a speculative list of other Mozilla background-service domains
    (push/contile/pocket/merino/etc.) that haven't actually been observed
    causing a problem here. Add more entries the same way if a future
    investigation turns up another offender.

    Idempotent: checks /api/blocked/list for this exact domain first and
    no-ops (logs "already blocked") if present, so safe to run on every
    pod boot -- also self-heals it if Technitium's config is ever reset.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup (lsf.labfail is never called) -- this is a UX papercut
    fix, not something worth blocking the lab on.
    """
    import requests

    lsf.write_output('Checking Technitium DNS block for Firefox Remote Settings...')
    technitium_url = 'http://10.1.10.129:5380'
    domain = 'firefox.settings.services.mozilla.com'
    password = lsf.get_password()

    try:
        login = requests.get(
            f'{technitium_url}/api/user/login',
            params={'user': 'admin', 'pass': password},
            timeout=10,
        ).json()
        if login.get('status') != 'ok':
            lsf.write_output(
                f'  WARNING: could not log into Technitium DNS server -- {login}'
            )
            return
        token = login['token']

        check = requests.get(
            f'{technitium_url}/api/blocked/list',
            params={'token': token, 'domain': domain},
            timeout=10,
        ).json()
        already_blocked = bool(check.get('response', {}).get('records'))

        if already_blocked:
            lsf.write_output(f'  {domain}: already blocked -- no-op')
            return

        add = requests.get(
            f'{technitium_url}/api/blocked/add',
            params={'token': token, 'domain': domain},
            timeout=10,
        ).json()
        if add.get('status') == 'ok':
            lsf.write_output(f'  {domain}: blocked (NXDOMAIN) at pod DNS server')
        else:
            lsf.write_output(f'  WARNING: could not block {domain}: {add}')
    except Exception as e:
        lsf.write_output(f'  WARNING: could not apply firefox remote-settings DNS block: {e}')


# global.vcf.lab's NS delegation was found pointing at a stale GSLB DNS VS
# name/IP pair (dns-vs-01a.site-a.vcf.lab/10.1.13.135,
# dns-vs-01b.site-b.vcf.lab/10.1.14.135) that no longer matches the pod's
# actual Avi GSLB DNS Virtual Service deployment -- confirmed live
# 2026-07-27 that the correct VSes now answer on 10.1.13.137 (site-a) and
# 10.1.14.137 (site-b) under the renamed hostnames below (verified each
# answers authoritatively, `aa` flag set, for a direct SOA query at its new
# IP). Fixed manually once already on the live Technitium server (same
# pattern documented in fix_firefox_remote_settings_dns_block() above,
# just against /api/zones/records/* instead of /api/blocked/*); this
# function makes that fix durable and self-healing across pod
# save/resume, the same reasoning as resync_nsxt_alb_enforcement_point_tokens()
# above.
GLOBAL_DNS_NS_DELEGATIONS = (
    {
        'old_nameserver': 'dns-vs-01a.site-a.vcf.lab',
        'new_nameserver': 'global-dns-vs-01a.site-a.vcf.lab',
        'glue_ip': '10.1.13.137',
        'a_record_zone': 'site-a.vcf.lab',
    },
    {
        'old_nameserver': 'dns-vs-01b.site-b.vcf.lab',
        'new_nameserver': 'global-dns-vs-01b.site-b.vcf.lab',
        'glue_ip': '10.1.14.137',
        'a_record_zone': 'site-b.vcf.lab',
    },
)
GLOBAL_DNS_NS_OWNER = 'global.vcf.lab'
GLOBAL_DNS_NS_ZONE = 'vcf.lab'


def fix_global_dns_ns_delegation(lsf):
    """
    Ensure global.vcf.lab's NS delegation (used for Avi GSLB) points at the
    current GSLB DNS VS hostnames/glue IPs, per GLOBAL_DNS_NS_DELEGATIONS
    above -- see that constant's comment for the root cause this fixes.

    For each delegation: first ensures the new glue-target A record exists
    with the desired IP (creates/overwrites it in a_record_zone), then
    ensures the global.vcf.lab NS record for that delegation points at the
    new hostname with the new glue IP -- updating the stale record in
    place if found under old_nameserver, or adding a fresh NS record if
    no matching old record exists (e.g. after a from-scratch Technitium
    reset).

    Idempotent: no-ops each delegation already found with the desired NS
    hostname and glue IP, so safe to run on every pod boot.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup (matches every other Technitium-touching fix in this
    file).
    """
    import requests

    lsf.write_output('Checking global.vcf.lab NS delegation (GSLB DNS VS glue records)...')
    technitium_url = 'http://10.1.10.129:5380'
    password = lsf.get_password()

    try:
        login = requests.get(
            f'{technitium_url}/api/user/login',
            params={'user': 'admin', 'pass': password},
            timeout=10,
        ).json()
        if login.get('status') != 'ok':
            lsf.write_output(
                f'  WARNING: could not log into Technitium DNS server -- {login}'
            )
            return
        token = login['token']

        ns_recs = requests.get(
            f'{technitium_url}/api/zones/records/get',
            params={'token': token, 'domain': GLOBAL_DNS_NS_OWNER, 'zone': GLOBAL_DNS_NS_ZONE},
            timeout=10,
        ).json()
        current_ns_records = [
            r for r in ns_recs.get('response', {}).get('records', [])
            if r.get('type') == 'NS' and r.get('name', '').lower() == GLOBAL_DNS_NS_OWNER.lower()
        ]

        for deleg in GLOBAL_DNS_NS_DELEGATIONS:
            # ---- ensure the new glue-target A record exists ----
            a_recs = requests.get(
                f'{technitium_url}/api/zones/records/get',
                params={'token': token, 'domain': deleg['new_nameserver'], 'zone': deleg['a_record_zone']},
                timeout=10,
            ).json()
            existing_a = next(
                (r for r in a_recs.get('response', {}).get('records', [])
                 if r.get('type') == 'A' and r.get('name', '').lower() == deleg['new_nameserver'].lower()),
                None,
            )
            if existing_a and existing_a.get('rData', {}).get('ipAddress') == deleg['glue_ip']:
                lsf.write_output(f"  {deleg['new_nameserver']}: A record already {deleg['glue_ip']} -- no-op")
            else:
                add = requests.get(
                    f'{technitium_url}/api/zones/records/add',
                    params={
                        'token': token,
                        'domain': deleg['new_nameserver'],
                        'zone': deleg['a_record_zone'],
                        'type': 'A',
                        'ipAddress': deleg['glue_ip'],
                        'ttl': 3600,
                        'overwrite': 'true',
                    },
                    timeout=10,
                ).json()
                if add.get('status') == 'ok':
                    lsf.write_output(f"  {deleg['new_nameserver']}: A record created -> {deleg['glue_ip']}")
                else:
                    lsf.write_output(f"  WARNING: could not create A record {deleg['new_nameserver']}: {add}")
                    continue

            # ---- ensure the global.vcf.lab NS record delegates to it ----
            already_correct = any(
                r.get('rData', {}).get('nameServer', '').lower() == deleg['new_nameserver'].lower()
                and r.get('glueRecords') == [deleg['glue_ip']]
                for r in current_ns_records
            )
            if already_correct:
                lsf.write_output(
                    f"  {GLOBAL_DNS_NS_OWNER}: NS already delegates to "
                    f"{deleg['new_nameserver']} ({deleg['glue_ip']}) -- no-op"
                )
                continue

            stale = next(
                (r for r in current_ns_records
                 if r.get('rData', {}).get('nameServer', '').lower() == deleg['old_nameserver'].lower()),
                None,
            )
            if stale:
                result = requests.get(
                    f'{technitium_url}/api/zones/records/update',
                    params={
                        'token': token,
                        'domain': GLOBAL_DNS_NS_OWNER,
                        'zone': GLOBAL_DNS_NS_ZONE,
                        'type': 'NS',
                        'nameServer': deleg['old_nameserver'],
                        'newNameServer': deleg['new_nameserver'],
                        'glue': deleg['glue_ip'],
                    },
                    timeout=10,
                ).json()
                verb = 'updated'
            else:
                result = requests.get(
                    f'{technitium_url}/api/zones/records/add',
                    params={
                        'token': token,
                        'domain': GLOBAL_DNS_NS_OWNER,
                        'zone': GLOBAL_DNS_NS_ZONE,
                        'type': 'NS',
                        'nameServer': deleg['new_nameserver'],
                        'glue': deleg['glue_ip'],
                    },
                    timeout=10,
                ).json()
                verb = 'added'

            if result.get('status') == 'ok':
                lsf.write_output(
                    f"  {GLOBAL_DNS_NS_OWNER}: NS record {verb} -> "
                    f"{deleg['new_nameserver']} (glue {deleg['glue_ip']})"
                )
            else:
                lsf.write_output(
                    f"  WARNING: could not {verb} NS record for {GLOBAL_DNS_NS_OWNER} "
                    f"-> {deleg['new_nameserver']}: {result}"
                )

    except Exception as e:
        lsf.write_output(f'  WARNING: could not fix global.vcf.lab NS delegation: {e}')


def configure_nsxt_app_profiles(lsf):
    """
    Ensure the custom NSX-T LB app profiles/monitor this pod relies on
    exist with the desired config: two LB fast-path profiles
    (custom-fast-tcp/custom-fast-udp) and an HTTP monitor on port 30001.

    Idempotent: GETs each object first and only PUTs when it's missing or
    an existing object's tracked fields differ from the desired values.
    This used to PUT unconditionally on every run, which errored out from
    the second adjustomatic run onward, since these NSX Policy objects
    persist across the whole pod's lifetime (only the very first run
    ever needed to create them). GET-before-PUT makes every subsequent
    run a no-op instead of an error.

    Also ensures the hol-wildcard service certificate is imported into
    NSX, from the cert/key files staged on the manager at
    /lmchol/home/holuser/certificates (wildcard_cert.pem/wildcard_key.pem).
    Idempotency here is presence-only (GET 200 -> skip) rather than a
    field diff like the profiles above, since NSX never reads back a
    private_key in plaintext to compare against.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup (matches this block's original behavior -- lsf.labfail
    was never called here).
    """
    import os
    import requests

    lsf.write_output('Configuring NSX-T LB app profiles...')
    session = requests.Session()
    session.verify = False
    session.auth = ('admin', os.environ['AVICTRL_PASSWORD'])
    nsx_mgr = 'https://nsx-wld01-a.site-a.vcf.lab'

    profiles = [
        ('lb-app-profiles', 'custom-fast-tcp', {
            'display_name': 'custom-fast-tcp',
            'idle_timeout': '1700',
            'close_timeout': '8',
            'resource_type': 'LBFastTcpProfile',
        }),
        ('lb-app-profiles', 'custom-fast-udp', {
            'display_name': 'custom-fast-udp',
            'idle_timeout': '330',
            'resource_type': 'LBFastUdpProfile',
        }),
        ('lb-monitor-profiles', 'http-30001', {
            'display_name': 'http-30001',
            'resource_type': 'LBHttpMonitorProfile',
            'monitor_port': 30001,
        }),
    ]

    for category, name, desired in profiles:
        url = f'{nsx_mgr}/policy/api/v1/infra/{category}/{name}'
        try:
            get_result = session.get(url, timeout=15)
            if get_result.status_code == 200:
                current = get_result.json()
                if all(current.get(k) == v for k, v in desired.items()):
                    lsf.write_output(f'  {name}: already present with desired config -- no-op')
                    continue
                lsf.write_output(f'  {name}: exists but differs from desired config -- updating')
            elif get_result.status_code == 404:
                lsf.write_output(f'  {name}: not found -- creating')
            else:
                lsf.write_output(
                    f'  {name}: GET returned {get_result.status_code} '
                    f'({get_result.text[:200]}) -- attempting PUT anyway'
                )

            put_result = session.put(url, json=desired, timeout=15)
            lsf.write_output(f'  {name}: PUT result {put_result.status_code} - {put_result.text[:200]}')

        except Exception as e:
            lsf.write_output(f'  WARNING: could not ensure {name}: {e}')

    cert_name = 'hol-wildcard'
    cert_dir = '/lmchol/home/holuser/certificates'
    cert_url = f'{nsx_mgr}/policy/api/v1/infra/certificates/{cert_name}'
    try:
        get_result = session.get(cert_url, timeout=15)
        if get_result.status_code == 200:
            lsf.write_output(f'  {cert_name}: certificate already present -- no-op')
        else:
            if get_result.status_code != 404:
                lsf.write_output(
                    f'  {cert_name}: GET returned {get_result.status_code} '
                    f'({get_result.text[:200]}) -- attempting import anyway'
                )
            lsf.write_output(f'  {cert_name}: not found -- importing from {cert_dir}')
            with open(f'{cert_dir}/wildcard_cert.pem', 'r') as cert_file:
                cert_pem = cert_file.read()
            with open(f'{cert_dir}/wildcard_key.pem', 'r') as key_file:
                cert_key = key_file.read()
            cert_data = {
                'display_name': cert_name,
                'resource_type': 'Certificate',
                'pem_encoded': cert_pem,
                'private_key': cert_key,
            }
            put_result = session.put(cert_url, json=cert_data, timeout=15)
            lsf.write_output(f'  {cert_name}: PUT result {put_result.status_code} - {put_result.text[:200]}')
    except Exception as e:
        lsf.write_output(f'  WARNING: could not ensure {cert_name} certificate: {e}')


# This vApp is a saved/suspended VCD template that can sit powered off for
# up to ~18 months (or more) between power-ons -- real wall-clock time
# elapses regardless of power state, so anything with a calendar-based
# expiration (auth tokens, password-aging policy) needs to already be
# valid far enough out, AND be re-checked/re-extended on every boot,
# rather than assumed-fixed-forever from a single manual remediation.
# The whole NSX<->Avi credential/lockout saga (2026-07-25) traces back to
# exactly this: an NSX Policy "Enforcement Point" (alb-endpoint) holding a
# short-lived Avi API token for the core NSX-ALB integration, plus a
# separate Avi cloud-connector -> NSX credential that SDDC Manager's
# auto-rotation updates on NSX but never pushes into Avi (a known,
# documented VCF gap) -- see the nsx-lockout incident doc for the fuller
# writeup. Both directions are handled below; kept as plain direct HTTPS
# calls to NSX/Avi/SDDC Manager (same reachability already proven by
# configure_nsxt_app_profiles()'s direct calls to nsx-wld01-a above) so
# nothing here needs an SSH hop.
NSXT_ALB_DOMAINS = (
    {
        'domain': 'wld01-a',
        'nsx_host': 'nsx-wld01-a.site-a.vcf.lab',
        'nsx_resource_name': 'nsx-wld01-a.site-a.vcf.lab',
        'avi_host': 'alb-a.site-a.vcf.lab',
        'avi_ip': '10.1.1.90',
        'nsx_svc_user': 'svc-alb-a-nsx-wld01-a',
        'immune_addresses': ['10.1.1.90', '10.1.1.91', '10.1.1.166'],  # Avi VIP+node, Supervisor NCP
    },
    {
        'domain': 'mgmt-a',
        'nsx_host': 'nsx-mgmt-a.site-a.vcf.lab',
        'nsx_resource_name': 'nsx-mgmt-a.site-a.vcf.lab',
        'avi_host': 'alb-b.site-a.vcf.lab',
        'avi_ip': '10.1.1.92',
        'nsx_svc_user': 'svc-alb-b-nsx-mgmt-a',
        'immune_addresses': ['10.1.1.92', '10.1.1.93'],  # no known Supervisor/NCP on this domain
    },
)
NSXT_ALB_TOKEN_HOURS = 17520  # 730 days -- max allowed is capped by the Avi
                              # account's own password_expiration_days (see
                              # _ensure_avi_password_expiration below), which
                              # this raises to the same 730 days first.
NSXT_ALB_TOKEN_MIN_DAYS_REMAINING = 90  # refresh proactively well before expiry
NSXT_ALB_PASSWORD_EXPIRATION_DAYS = 730  # ~2yr: 18mo target + margin


def _avi_login(admin_password, avi_host, timeout=15):
    """Return a requests.Session already carrying Avi's csrftoken/cookie."""
    import requests
    session = requests.Session()
    session.verify = False
    resp = session.post(
        f'https://{avi_host}/login', timeout=timeout,
        json={'username': 'admin', 'password': admin_password},
    )
    resp.raise_for_status()
    session.headers.update({
        'X-CSRFToken': session.cookies.get('csrftoken'),
        'Referer': f'https://{avi_host}',
        'X-Avi-Version': '32.1.1',
    })
    return session


def _sddc_login(admin_password, timeout=15):
    """Return a bearer token for SDDC Manager's REST API."""
    import requests
    resp = requests.post(
        'https://sddcmanager-a.site-a.vcf.lab/v1/tokens', timeout=timeout, verify=False,
        json={'username': 'administrator@vsphere.local', 'password': admin_password},
    )
    resp.raise_for_status()
    return resp.json()['accessToken']


def _ensure_avi_password_expiration(avi_session, avi_host, lsf, min_days=NSXT_ALB_PASSWORD_EXPIRATION_DAYS):
    """
    Idempotently ensure the No-Lockout-User-Account-Profile (used by
    nsxt-alb/nsxt-ako) allows at least min_days of password validity --
    this is what caps how long an Avi auth token can be issued for (Avi
    itself enforces "token expiry cannot exceed password expiration
    period"). No-op if already >= min_days.
    """
    result = avi_session.get(f'https://{avi_host}/api/useraccountprofile', timeout=15).json()
    no_lockout = next((p for p in result.get('results', []) if p.get('name') == 'No-Lockout-User-Account-Profile'), None)
    if not no_lockout:
        lsf.write_output(f'    WARNING: No-Lockout-User-Account-Profile not found on {avi_host}')
        return
    uuid = no_lockout['uuid']
    current = avi_session.get(f'https://{avi_host}/api/useraccountprofile/{uuid}', timeout=15).json()
    current_days = current.get('expiration_constraint', {}).get('password_expiration_days', 0)
    if current_days >= min_days:
        return
    avi_session.patch(
        f'https://{avi_host}/api/useraccountprofile/{uuid}', timeout=15,
        json={'replace': {'expiration_constraint': {'password_expiration_days': min_days}}},
    )
    lsf.write_output(f'    {avi_host}: bumped password_expiration_days {current_days} -> {min_days}')


def _ensure_avi_user_no_lockout(avi_session, avi_host, username, no_lockout_ref, lsf):
    """Idempotently move a local Avi user onto the no-lockout profile."""
    result = avi_session.get(f'https://{avi_host}/api/user?username={username}', timeout=15).json()
    users = result.get('results', [])
    if not users:
        return
    user = users[0]
    if user.get('user_profile_ref', '').split('#')[0] == no_lockout_ref:
        return
    avi_session.patch(
        f'https://{avi_host}/api/user/{user["uuid"]}', timeout=15,
        json={'user_profile_ref': no_lockout_ref},
    )
    lsf.write_output(f'    {avi_host}: moved {username} onto no-lockout profile')


def resync_nsxt_alb_enforcement_point_tokens(lsf):
    """
    Keep the NSX Policy "alb-endpoint" Enforcement Point's Avi API token
    (used for the core NSX<->Avi LB-config-realization integration, incl.
    what AKO/NCP ultimately depend on) from ever going stale across this
    vApp's save/resume lifecycle. Refreshes proactively (>=90 days before
    expiry) rather than reactively, since a powered-off vApp can't refresh
    itself and might not be powered on again until well past a short
    token's expiry.

    Also ensures nsxt-alb/nsxt-ako sit on Avi's no-lockout profile with
    >=730 days of password validity (raises the ceiling on how long a
    token this function issues can be) -- fast, idempotent, safe to run
    every boot.

    Root cause / history: see the 2026-07-25 nsx-lockout session notes --
    this Enforcement Point was found in NSX-reported state
    DEACTIVATE_PROVIDER with a corrupted (null-byte) username, matching
    JIRA VKAL-36952 / KB "AKO Pod in CrashLoopBackOff Caused by Locked
    NSX-ALB Account". Fixed manually once already; this is that fix made
    durable and self-renewing.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup.
    """
    import datetime
    import os
    import requests

    lsf.write_output('Checking NSX-ALB Enforcement Point token freshness...')
    admin_password = os.environ['AVICTRL_PASSWORD']
    now = datetime.datetime.now(datetime.timezone.utc)

    for d in NSXT_ALB_DOMAINS:
        try:
            avi_session = _avi_login(admin_password, d['avi_host'])
            _ensure_avi_password_expiration(avi_session, d['avi_host'], lsf)

            no_lockout_url = avi_session.get(f"https://{d['avi_host']}/api/useraccountprofile", timeout=15).json()
            no_lockout = next((p for p in no_lockout_url.get('results', []) if p.get('name') == 'No-Lockout-User-Account-Profile'), None)
            if no_lockout:
                for u in ('nsxt-alb', 'nsxt-ako'):
                    _ensure_avi_user_no_lockout(avi_session, d['avi_host'], u, no_lockout['url'], lsf)

            ep_url = f"https://{d['nsx_host']}/policy/api/v1/infra/sites/default/enforcement-points/alb-endpoint"
            ep = requests.get(ep_url, auth=('admin', admin_password), verify=False, timeout=15).json()
            expires_at = ep.get('connection_info', {}).get('expires_at')

            needs_refresh = True
            if expires_at:
                try:
                    remaining = datetime.datetime.fromisoformat(expires_at) - now
                    needs_refresh = remaining.days < NSXT_ALB_TOKEN_MIN_DAYS_REMAINING
                except ValueError:
                    needs_refresh = True  # unparseable -- treat as stale

            if not needs_refresh:
                lsf.write_output(f"  {d['domain']}: enforcement-point token valid until {expires_at} -- no-op")
                continue

            lsf.write_output(f"  {d['domain']}: enforcement-point token missing/expiring ({expires_at}) -- refreshing")
            token_resp = avi_session.post(
                f"https://{d['avi_host']}/api/authtoken", timeout=15,
                json={'username': 'nsxt-alb', 'hours': NSXT_ALB_TOKEN_HOURS},
            ).json()
            if 'token' not in token_resp:
                lsf.write_output(f"  WARNING: {d['domain']}: could not generate Avi token: {token_resp}")
                continue

            # 'status' is a REQUIRED field on AviConnectionInfo, and NSX
            # Policy does NOT field-merge this nested object on PATCH -- it
            # replaces connection_info wholesale, filling in the schema
            # default for anything omitted. That default is literally
            # "DEACTIVATE_API" (confirmed via
            # GET /policy/api/v1/infra/sites/default/enforcement-points --
            # the healthy resting state for this integration is
            # "DEACTIVATE_PROVIDER", NOT "DEACTIVATE_API"). An earlier
            # version of this function omitted 'status' here and silently
            # deactivated NSX<->Avi on every boot that happened to need a
            # token refresh -- see the 2026-07-30 avi-secret/AKO
            # CrashLoopBackOff incident, a second occurrence of the same
            # underlying issue as the 2026-07-25 nsx-lockout incident this
            # function was originally written to guard against. Always
            # carry the existing status forward explicitly rather than
            # trusting NSX to preserve it.
            existing_status = ep.get('connection_info', {}).get('status') or 'DEACTIVATE_PROVIDER'
            # Same wholesale-replace hazard applies to every other optional
            # field on connection_info -- 'managed_by' (VCF/LCM) was found
            # missing entirely during the 2026-07-30 incident, almost
            # certainly wiped by this same function on an earlier run.
            # Losing it doesn't break token refresh directly, but it does
            # break NSX's own /infra/alb-onboarding-workflow/{managed-by}
            # de-registration API (throws a server-side NullPointerException
            # on a null managed_by) -- carry it forward too so a future
            # manual re-registration isn't blocked by our own omission.
            existing_managed_by = ep.get('connection_info', {}).get('managed_by') or 'VCF'
            patch_body = {
                '_revision': ep['_revision'],
                'connection_info': {
                    'resource_type': 'AviConnectionInfo',
                    'enforcement_point_address': d['avi_ip'],
                    'username': 'nsxt-alb',
                    'password': token_resp['token'],
                    'expires_at': token_resp['expires_at'],
                    'tenant': 'admin',
                    'status': existing_status,
                    'managed_by': existing_managed_by,
                },
            }
            patch_result = requests.patch(
                ep_url, auth=('admin', admin_password), verify=False, timeout=15, json=patch_body,
            )
            lsf.write_output(
                f"  {d['domain']}: enforcement-point token refreshed, new expiry {token_resp['expires_at']} "
                f"(PATCH {patch_result.status_code})"
            )

            # Verify the PATCH actually left the endpoint in the state we
            # asked for -- don't just trust a 200. This is exactly the
            # check that would have caught the missing-'status' bug
            # immediately instead of it silently breaking AKO hours later.
            verify = requests.get(ep_url, auth=('admin', admin_password), verify=False, timeout=15).json()
            verify_status = verify.get('connection_info', {}).get('status')
            if verify_status != existing_status:
                lsf.write_output(
                    f"  WARNING: {d['domain']}: enforcement-point status is '{verify_status}' "
                    f"after patch, expected '{existing_status}' -- NSX<->Avi integration may be "
                    f"broken; AKO/NCP on this domain will likely fail to get an avi-secret"
                )

        except Exception as e:
            lsf.write_output(f"  WARNING: could not resync {d['domain']} enforcement-point token: {e}")


# wld01-a is the only domain with a Supervisor cluster / AKO on it (see
# NSXT_ALB_DOMAINS' own immune_addresses comment above) -- mgmt-a has no
# equivalent secret chain to check here, so this all runs against the
# Supervisor context directly rather than looping over NSXT_ALB_DOMAINS.
AKO_NAMESPACE = 'vmware-system-ako'
NCP_NAMESPACE = 'vmware-system-nsx'
NCP_DEPLOYMENT = 'nsx-ncp'
NETOP_NAMESPACE = 'vmware-system-netop'
NETOP_DEPLOYMENT = 'vmware-system-netop-controller-manager'


def ensure_ako_avi_secret_healthy(lsf, timeout_per_step=90, poll_interval=10):
    """
    Detect and repair the 2026-07-30 avi-secret/AKO CrashLoopBackOff class of
    failure: AKO's controller-manager/crd-operator pods in vmware-system-ako
    crash-loop because the 'avi-secret' Kubernetes Secret they depend on was
    never created. That secret is derived through a chain of components,
    none of which retries automatically or promptly on every failure mode:

        NSX alb-endpoint EnforcementPoint (fixed durably by
        resync_nsxt_alb_enforcement_point_tokens() above, if that was the
        problem)
          -> nsx-ncp's AviSecretController creates 'avi-init-secret' in
             vmware-system-ako (namespace vmware-system-ako)
          -> vmware-system-netop-controller-manager's loadbalancerconfig
             controller reads avi-init-secret and creates 'avi-secret'
          -> AKO consumes avi-secret

    Root-caused live on 2026-07-30: even with a healthy alb-endpoint, this
    chain does not reliably self-heal promptly -- nsx-ncp's AviSecretController
    can be stuck retrying against a stale connection to NSX, and
    vmware-system-netop-controller-manager's own AviLoadBalancerConfig
    provider-sync only re-runs on its own resync cadence (empirically tens
    of minutes) rather than the instant avi-init-secret appears. A plain
    `rollout restart` of each -- in that order, since the second depends on
    output from the first -- reliably unstuck both in every case tested
    that day. This function automates exactly that, bounded and non-fatal.

    Deliberately NOT automated here: deleting/recreating the NSX
    EnforcementPoint itself. That was also needed once, live, to clear an
    NSX<->Avi TLS handshake fault (BouncyCastle "certificate_unknown(46)")
    that resync_nsxt_alb_enforcement_point_tokens() cannot fix (that
    function only ever PATCHes an existing EnforcementPoint's
    connection_info; it never touches the TLS layer). The exact mechanism
    by which delete+recreate cleared that fault was never conclusively
    pinned down (possibly a stale connection/session cache on NSX Manager
    keyed to the EnforcementPoint's own identity, possibly coincidental
    timing) -- automating a blind delete of a live, shared NSX object on
    every boot on an unconfirmed hypothesis is a worse trade than leaving
    this one case to a human, so this function stops short of that and
    just logs a clear pointer to it instead.

    Non-fatal: any failure or unresolved state here is logged as a warning
    and does not fail lab startup.
    """
    import time

    lsf.write_output('Checking AKO / avi-secret health on Supervisor...')
    password = lsf.get_password()

    def _ako_pods_ready():
        cmd = (
            f"kubectl --context Supervisor -n {AKO_NAMESPACE} get pods "
            f"-o jsonpath='{{range .items[*]}}{{.metadata.name}}{{\" \"}}"
            f"{{.status.containerStatuses[*].ready}}{{\"\\n\"}}{{end}}'"
        )
        result = lsf.ssh(cmd, VKS_KUBECTL_HOST, password)
        out = (getattr(result, 'stdout', '') or '').strip()
        if not out:
            return False
        for line in out.splitlines():
            if 'false' in line.split():
                return False
        return True

    def _secret_exists(namespace, name):
        cmd = f"kubectl --context Supervisor -n {namespace} get secret {name} --ignore-not-found -o name"
        result = lsf.ssh(cmd, VKS_KUBECTL_HOST, password)
        return bool((getattr(result, 'stdout', '') or '').strip())

    def _wait_for(predicate, description):
        deadline = time.time() + timeout_per_step
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(poll_interval)
        lsf.write_output(f'  WARNING: timed out waiting for {description}')
        return False

    try:
        if _ako_pods_ready():
            lsf.write_output('  AKO pods are healthy -- no-op')
            return

        lsf.write_output('  AKO pods are not all Ready -- checking avi-secret chain')

        if not _secret_exists(AKO_NAMESPACE, 'avi-secret'):
            if not _secret_exists(AKO_NAMESPACE, 'avi-init-secret'):
                lsf.write_output(
                    f"  avi-init-secret missing -- restarting {NCP_DEPLOYMENT} "
                    f"to force its AviSecretController to retry"
                )
                lsf.ssh(
                    f"kubectl --context Supervisor -n {NCP_NAMESPACE} rollout restart deployment {NCP_DEPLOYMENT}",
                    VKS_KUBECTL_HOST, password,
                )
                if not _wait_for(
                    lambda: _secret_exists(AKO_NAMESPACE, 'avi-init-secret'),
                    'avi-init-secret to appear after nsx-ncp restart',
                ):
                    lsf.write_output(
                        '  avi-init-secret still missing after restarting nsx-ncp. This can mean '
                        'the NSX<->Avi connection itself is broken beneath the object data (a TLS '
                        'handshake fault was the cause on 2026-07-30, invisible to a plain status '
                        'check) -- manually verify with: '
                        "curl -sk -u admin:<password> -X PUT "
                        "'https://nsx-<domain>.site-a.vcf.lab/policy/api/v1/infra/alb-auth-token' "
                        "-d '{\"username\":\"nsxt-alb\",\"hours\":\"5\"}' -- a "
                        "'Certificate validation failed' response confirms it. See the "
                        "nsx-lockout incident doc for the full delete/re-create remediation; "
                        "not attempted automatically here."
                    )
                    return

            lsf.write_output(
                f"  avi-init-secret present -- restarting {NETOP_DEPLOYMENT} "
                f"to force it to derive avi-secret"
            )
            lsf.ssh(
                f"kubectl --context Supervisor -n {NETOP_NAMESPACE} rollout restart deployment {NETOP_DEPLOYMENT}",
                VKS_KUBECTL_HOST, password,
            )
            if not _wait_for(
                lambda: _secret_exists(AKO_NAMESPACE, 'avi-secret'),
                'avi-secret to appear after netop-controller-manager restart',
            ):
                return

        lsf.write_output('  avi-secret present -- forcing AKO pods to restart rather than wait out backoff')
        lsf.ssh(
            f"kubectl --context Supervisor -n {AKO_NAMESPACE} delete pods --all --wait=false",
            VKS_KUBECTL_HOST, password,
        )
        if _wait_for(_ako_pods_ready, 'AKO pods to become Ready'):
            lsf.write_output('  AKO pods are healthy after remediation')

    except Exception as e:
        lsf.write_output(f'  WARNING: could not verify/repair AKO avi-secret health: {e}')


# vCenter host that owns the Supervisor-enabled cluster this shim checks.
# Only wld01-a has one -- see NSXT_ALB_DOMAINS' own comment on that.
SUPERVISOR_VC_HOST = 'vc-wld01-a.site-a.vcf.lab'


def check_supervisor_ako_health_early(lsf):
    """
    Early-boot, cause-discriminating check shimmed into Startup/VCF.py's
    CUSTOM section (this repo) so it runs BEFORE the generic VCFfinal.py
    module's Supervisor health polling.

    Root cause this guards against: VCFfinal.py's three Supervisor checks
    (confirmed by reading its source at
    github.com/Broadcom/HOLFY27-MGR-HOLUSER) never call lsf.labfail() --
    they only log WARNINGs and continue. But the main poll allows up to
    30 minutes (WCP_MAX_POLL_TIME) and its supervisor_stabilizer.py run
    allows up to another 31 minutes (WCP_SCRIPT_TIMEOUT), and neither has
    any ability to fix a broken avi-secret/alb-endpoint chain (see
    ensure_ako_avi_secret_healthy() and resync_nsxt_alb_enforcement_point_tokens()
    above). On a pod carrying that problem, VCFfinal would burn up to
    ~60 minutes retrying something it can't fix before ever reaching
    final.py, where adjustomatic.py (and this repo's real fix) actually
    runs. Running the same fix here first, before VCFfinal starts
    polling, means it just sees a healthy Supervisor and passes quickly.

    Deliberately narrow: only calls the AKO repair chain when vCenter's
    own kubernetes_status_messages specifically name a vmware-system-ako
    pod. If Supervisor is WARNING/ERROR for some other reason (e.g. other
    system pods still reconciling shortly after boot), this logs what it
    found and does nothing else -- supervisor_stabilizer.py (later, in
    VCFfinal) is the right place for those, and an unrelated intervention
    here could race with or duplicate its own remediation.

    Non-fatal: any failure here is logged as a warning. Never raises --
    safe to call from a CUSTOM section that isn't itself labfail-gated.
    """
    import requests
    requests.packages.urllib3.disable_warnings()

    lsf.write_output('Early check: Supervisor status ahead of VCFfinal (AKO shim)...')
    password = lsf.get_password()

    try:
        session_resp = requests.post(
            f'https://{SUPERVISOR_VC_HOST}/api/session',
            auth=('administrator@wld.sso', password), verify=False, timeout=15,
        )
        if session_resp.status_code != 201:
            lsf.write_output(
                f'  WARNING: could not create vCenter session (HTTP {session_resp.status_code}): '
                f'{session_resp.text[:200]} -- vCenter/wcp may still be starting or mid-shutdown, skipping'
            )
            return
        headers = {'vmware-api-session-id': session_resp.json()}

        clusters_resp = requests.get(
            f'https://{SUPERVISOR_VC_HOST}/api/vcenter/namespace-management/clusters',
            headers=headers, verify=False, timeout=15,
        )
        if clusters_resp.status_code != 200:
            lsf.write_output(
                f'  WARNING: namespace-management/clusters returned HTTP {clusters_resp.status_code}: '
                f'{clusters_resp.text[:200]} -- wcp service may still be starting, skipping'
            )
            return
        clusters = clusters_resp.json()
        if not clusters:
            lsf.write_output('  No Supervisor-enabled clusters found on this vCenter -- nothing to check')
            return

        for c in clusters:
            cluster_moid = c['cluster']
            status = c.get('kubernetes_status')
            label = c.get('cluster_name', cluster_moid)
            if status == 'READY':
                lsf.write_output(f'  {label}: kubernetes_status READY -- no-op')
                continue

            lsf.write_output(f'  {label}: kubernetes_status {status} -- inspecting cause')
            detail_resp = requests.get(
                f'https://{SUPERVISOR_VC_HOST}/api/vcenter/namespace-management/clusters/{cluster_moid}',
                headers=headers, verify=False, timeout=15,
            )
            if detail_resp.status_code != 200:
                lsf.write_output(
                    f'    WARNING: could not fetch cluster detail (HTTP {detail_resp.status_code}) -- skipping'
                )
                continue
            detail = detail_resp.json()
            messages = detail.get('kubernetes_status_messages', [])
            if not messages:
                lsf.write_output(
                    '    No status messages reported (likely a transient state right after boot) '
                    '-- deferring to VCFfinal\'s own polling'
                )
                continue

            ako_related, other = [], []
            for m in messages:
                text = m.get('details', {}).get('default_message', '') or ''
                (ako_related if AKO_NAMESPACE in text else other).append(text)

            for text in ako_related:
                lsf.write_output(f'    AKO-related: {text}')
            for text in other:
                lsf.write_output(f'    Non-AKO: {text}')

            if not ako_related:
                lsf.write_output(
                    '    Nothing AKO-related in the reported causes -- leaving as-is for '
                    'VCFfinal\'s own Supervisor stabilization; no action taken here'
                )
                continue

            if other:
                lsf.write_output(
                    '    Mixed causes -- running the avi-secret repair chain for the AKO part only; '
                    'leaving the rest for VCFfinal\'s supervisor_stabilizer.py'
                )
            else:
                lsf.write_output('    All reported causes are AKO-related -- running avi-secret repair chain now')

            resync_nsxt_alb_enforcement_point_tokens(lsf)
            ensure_ako_avi_secret_healthy(lsf)

    except Exception as e:
        lsf.write_output(f'  WARNING: early Supervisor/AKO check failed: {e}')


# The AKO deployed INSIDE each VKS guest cluster -- a different AKO from the
# Supervisor-side vmware-system-ako namespace checked by
# ensure_ako_avi_secret_healthy()/check_supervisor_ako_health_early() above.
# Confirmed live 2026-08-11 on this pod: each of workload-cluster-1/-2 runs
# its own 'avi-system' namespace with a single-replica StatefulSet 'ako'
# (pod ako-0, containers ako/ako-gateway-api/vmci-relay) plus a separate
# 'ako-crd-operator' Deployment. Checked on BOTH clusters here, unlike
# VKS_WORKLOAD_CLUSTERS above (deliberately scoped to just
# workload-cluster-1 for node-pool scaling only) -- AKO health isn't tied to
# that scoping decision.
WORKLOAD_CLUSTER_CONTEXTS = ('workload-cluster-1', 'workload-cluster-2')
WORKLOAD_CLUSTER_AKO_NAMESPACE = 'avi-system'

# Container waiting reasons that a plain pod delete actually fixes. All of
# these stem from kubelet resolving a pod's inputs (a referenced
# ConfigMap/Secret, an image reference) once at container creation; if that
# input was missing/wrong when the pod first started but is fine now,
# kubelet's own in-place retry can sit stuck far longer than a lab session
# should wait (this is the exact class of failure seen live on
# workload-cluster-1 2026-08-11: AKO's ako-0 pod stuck in
# CreateContainerConfigError). Deleting the pod lets its owning
# StatefulSet/Deployment recreate it and re-resolve those inputs
# immediately. Deliberately excludes reasons a delete can't fix (e.g.
# CrashLoopBackOff from an actual application bug) so this doesn't churn
# pods that would just fail the same way again.
AKO_FIXABLE_WAITING_REASONS = frozenset([
    'CreateContainerConfigError',
    'CreateContainerError',
    'InvalidImageName',
])


def ensure_workload_cluster_ako_healthy(lsf, timeout_seconds=180, poll_interval=15):
    """
    For each context in WORKLOAD_CLUSTER_CONTEXTS, check that every pod in
    the guest cluster's own avi-system namespace (AKO's ako-0 StatefulSet
    pod and the ako-crd-operator Deployment pod) is Ready. Any pod with a
    container stuck waiting on a reason in AKO_FIXABLE_WAITING_REASONS gets
    deleted so its controller recreates it; this function then polls up to
    timeout_seconds for the namespace to report all-Ready again.

    A pod that's simply still starting (e.g. right after a fresh guest
    cluster boot) is left alone -- only pods with a *waiting reason present*
    in the fixable set are touched, so this doesn't race a normal cold
    start.

    Non-fatal: any failure, or an unfixed/unfixable pod, is logged as a
    warning. lsf.labfail is never called here -- a stuck AKO pod degrades
    load-balancer provisioning for workloads on that guest cluster, but
    doesn't itself mean the lab can't be used.
    """
    import json
    import time

    lsf.write_output('Checking AKO pod health in each VKS workload cluster...')
    password = lsf.get_password()

    def _get_pods(context):
        cmd = f"kubectl --context {context} -n {WORKLOAD_CLUSTER_AKO_NAMESPACE} get pods -o json"
        result = lsf.ssh(cmd, VKS_KUBECTL_HOST, password)
        stdout = (getattr(result, 'stdout', '') or '').strip()
        if not stdout:
            return None
        return json.loads(stdout).get('items', [])

    def _all_ready(pods):
        return all(
            cs.get('ready')
            for pod in pods
            for cs in (pod.get('status', {}).get('containerStatuses', []) or [])
        )

    for context in WORKLOAD_CLUSTER_CONTEXTS:
        try:
            pods = _get_pods(context)
            if pods is None:
                lsf.write_output(
                    f'  {context}: no output listing pods in {WORKLOAD_CLUSTER_AKO_NAMESPACE} '
                    f'-- cluster may be unreachable, skipping'
                )
                continue
            if not pods:
                lsf.write_output(f'  {context}: no pods found in {WORKLOAD_CLUSTER_AKO_NAMESPACE} -- skipping')
                continue

            to_delete = []
            for pod in pods:
                name = pod.get('metadata', {}).get('name', '<unknown>')
                statuses = pod.get('status', {}).get('containerStatuses', []) or []
                bad_reasons = []
                for cs in statuses:
                    if cs.get('ready'):
                        continue
                    reason = (cs.get('state', {}).get('waiting', {}) or {}).get('reason')
                    if reason:
                        bad_reasons.append(reason)

                if not bad_reasons:
                    continue

                lsf.write_output(f'  {context}/{name}: not Ready ({", ".join(bad_reasons)})')
                if any(r in AKO_FIXABLE_WAITING_REASONS for r in bad_reasons):
                    to_delete.append(name)
                else:
                    lsf.write_output(
                        f'    {name}: reason(s) not in the known pod-delete-fixable set -- leaving alone'
                    )

            if not to_delete:
                lsf.write_output(f'  {context}: all AKO pods Ready (or not fixable by delete) -- no-op')
                continue

            for name in to_delete:
                lsf.write_output(f'  {context}/{name}: deleting to force recreation')
                lsf.ssh(
                    f"kubectl --context {context} -n {WORKLOAD_CLUSTER_AKO_NAMESPACE} "
                    f"delete pod {name} --wait=false",
                    VKS_KUBECTL_HOST, password,
                )

            deadline = time.time() + timeout_seconds
            recovered = False
            while time.time() < deadline:
                time.sleep(poll_interval)
                pods = _get_pods(context)
                if pods and _all_ready(pods):
                    recovered = True
                    break

            if recovered:
                lsf.write_output(f'  {context}: AKO pods Ready after recreation')
            else:
                lsf.write_output(
                    f'  WARNING: {context}: AKO pods still not all Ready {timeout_seconds}s '
                    f'after deleting {to_delete}'
                )

        except Exception as e:
            lsf.write_output(f'  WARNING: could not check/repair AKO health in {context}: {e}')


# The cert-manager Issuer used by the Automation lab's Supervisor namespace
# to sign certs via HashiCorp Vault's pki/sign/holodeck path. Same
# console-has-the-kubeconfig / manager-doesn't constraint as the VKS
# kubectl calls above -- see VKS_KUBECTL_HOST's comment.
VAULT_ISSUER_NAME = 'vault-issuer'
VAULT_SERVER_FQDN = 'vault.vcf.lab'
# Domains this pod's exercises actually request certs for via the
# holodeck PKI role -- vcf.lab plus each site-a subdomain fronted by Avi
# (the load-balancer VIP domain and both VKS guest-cluster domains).
VAULT_HOLODECK_REQUIRED_DOMAINS = (
    'vcf.lab', 'lb.site-a.vcf.lab', 'site-a.vcf.lab',
    'vks1.site-a.vcf.lab', 'vks2.site-a.vcf.lab',
)


def fix_vault_issuer_ca_trust(lsf):
    """
    Ensure cert-manager's vault-issuer Issuer (namespace VKS_SUPERVISOR_NS,
    Supervisor context) trusts the TLS certificate vault.vcf.lab's API
    listener presents.

    Root cause (confirmed live 2026-08-23): vault.vcf.lab's own root CA
    ("vcf.lab Root Authority") is regenerated per pod rebuild, but the
    Issuer object is provisioned with no spec.vault.caBundle at all --
    cert-manager then fails every health check against Vault with
    "x509: certificate signed by unknown authority" and the Issuer never
    reaches Ready, which means every cert request against
    pki/sign/holodeck in this namespace fails too.

    Fix: fetch the cert chain vault.vcf.lab actually presents (via
    `openssl s_client -showcerts`, run on console since that's the host
    with a network route + the Supervisor kubeconfig -- see
    VKS_KUBECTL_HOST), identify the self-signed root (subject == issuer),
    and merge-patch it into spec.vault.caBundle. Idempotent: skips the
    patch entirely if the currently-live root already matches what's on
    the Issuer (covers every run after the first on a given pod, and any
    run where this was already fixed by hand).

    Non-fatal: any failure here is logged as a warning. A broken
    vault-issuer degrades whatever exercise depends on Vault-issued certs
    in this namespace, but doesn't itself mean the rest of the lab is
    unusable.
    """
    import base64

    lsf.write_output('Checking cert-manager vault-issuer CA trust...')
    password = lsf.get_password()

    remote_py = r"""
import base64, json, re, subprocess, sys, time

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

K = ['kubectl', '--context', 'Supervisor', '-n', '__VKS_SUPERVISOR_NS__']

get = run(K + ['get', 'issuer', '__VAULT_ISSUER_NAME__', '-o', 'json'])
if get.returncode != 0:
    print('ISSUER_NOT_FOUND: ' + get.stderr.strip())
    sys.exit(0)

issuer = json.loads(get.stdout)
current_ca_b64 = issuer.get('spec', {}).get('vault', {}).get('caBundle', '')

chain = subprocess.run(
    ['openssl', 's_client', '-connect', '__VAULT_SERVER_FQDN__:443',
     '-servername', '__VAULT_SERVER_FQDN__', '-showcerts'],
    input='', capture_output=True, text=True, timeout=20,
).stdout

certs = re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', chain, re.S)
if not certs:
    print('NO_CERTS_RETRIEVED')
    sys.exit(0)

root_pem = None
for pem in certs:
    with open('/tmp/_vault_issuer_cert.pem', 'w') as f:
        f.write(pem)
    subj = run(['openssl', 'x509', '-in', '/tmp/_vault_issuer_cert.pem', '-noout', '-subject']).stdout.strip()
    iss = run(['openssl', 'x509', '-in', '/tmp/_vault_issuer_cert.pem', '-noout', '-issuer']).stdout.strip()
    if subj.replace('subject=', '', 1) == iss.replace('issuer=', '', 1):
        root_pem = pem
subprocess.run(['rm', '-f', '/tmp/_vault_issuer_cert.pem'])

if not root_pem:
    print('NO_SELF_SIGNED_ROOT_FOUND')
    sys.exit(0)

new_ca_b64 = base64.b64encode(root_pem.encode()).decode()
if new_ca_b64 == current_ca_b64:
    print('ALREADY_TRUSTED')
    sys.exit(0)

patch = json.dumps({'spec': {'vault': {'caBundle': new_ca_b64}}})
res = run(K + ['patch', 'issuer', '__VAULT_ISSUER_NAME__', '--type=merge', '-p', patch])
if res.returncode != 0:
    print('PATCH_FAILED: ' + res.stderr.strip())
    sys.exit(0)

for _ in range(6):
    time.sleep(5)
    st = run(K + ['get', 'issuer', '__VAULT_ISSUER_NAME__', '-o', 'json'])
    if st.returncode == 0:
        conds = json.loads(st.stdout).get('status', {}).get('conditions', [])
        ready = next((c for c in conds if c.get('type') == 'Ready'), None)
        if ready and ready.get('status') == 'True':
            print('PATCHED_AND_VERIFIED')
            sys.exit(0)
print('PATCHED_BUT_NOT_YET_READY')
"""
    remote_py = (
        remote_py
        .replace('__VKS_SUPERVISOR_NS__', VKS_SUPERVISOR_NS)
        .replace('__VAULT_ISSUER_NAME__', VAULT_ISSUER_NAME)
        .replace('__VAULT_SERVER_FQDN__', VAULT_SERVER_FQDN)
    )
    script_b64 = base64.b64encode(remote_py.encode()).decode()
    remote_cmd = (
        f"echo {script_b64} | base64 -d > /tmp/fix_vault_issuer.py && "
        f"python3 /tmp/fix_vault_issuer.py; rm -f /tmp/fix_vault_issuer.py"
    )

    try:
        result = lsf.ssh(remote_cmd, VKS_KUBECTL_HOST, password)
        out_text = (getattr(result, 'stdout', '') or '').strip()
        lsf.write_output(f'  vault-issuer CA trust result: {out_text or "(no output)"}')
        if 'FAILED' in out_text or 'NOT_YET_READY' in out_text or 'NOT_FOUND' in out_text:
            lsf.write_output('  WARNING: vault-issuer may still need manual attention')
    except Exception as e:
        lsf.write_output(f'  WARNING: vault-issuer CA trust check failed: {e}')


def ensure_vault_holodeck_role_domains(lsf):
    """
    Ensure Vault's pki/roles/holodeck role (the role vault-issuer signs
    through, via pki/sign/holodeck) has allowed_domains covering every
    domain this pod's exercises actually request certs for --
    VAULT_HOLODECK_REQUIRED_DOMAINS.

    Uses the same universal pod password as the Vault root token --
    confirmed live 2026-08-23 that vault.vcf.lab's root token equals the
    standard pod password, same as it authenticates essentially every
    other account across this lab (see CLAUDE.md's Credentials section).

    Read-modify-write, not a partial POST: Vault's pki/roles/:name write
    endpoint replaces the whole role, not just the fields supplied -- a
    request containing only allowed_domains would silently reset every
    other role parameter (key_type, ttl, allow_subdomains, etc.) to its
    default. This reads the full current role first and re-submits it
    verbatim with only allowed_domains extended, so nothing else about
    the role changes.

    Idempotent: no-ops (and only logs) if every required domain is
    already present -- true on any pod where this was already applied,
    confirmed live 2026-08-23.

    Non-fatal: any failure is logged as a warning, never fails the lab --
    a missing domain here breaks cert issuance for that one domain, not
    the rest of the pod.
    """
    import base64
    import json

    lsf.write_output('Checking Vault pki/roles/holodeck allowed_domains...')
    password = lsf.get_password()

    remote_py = r"""
import json, ssl, sys, urllib.request

VAULT_HOST = '__VAULT_SERVER_FQDN__'
ROLE = 'holodeck'
REQUIRED_DOMAINS = __REQUIRED_DOMAINS_JSON__
TOKEN = '__VAULT_TOKEN__'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def vault_request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f'https://{VAULT_HOST}{path}', data=data, method=method,
        headers={'X-Vault-Token': TOKEN, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}

try:
    current = vault_request('GET', f'/v1/pki/roles/{ROLE}')
except Exception as e:
    print(f'ROLE_READ_FAILED: {e}')
    sys.exit(0)

data = current.get('data', {})
existing = data.get('allowed_domains', []) or []
missing = [d for d in REQUIRED_DOMAINS if d not in existing]

if not missing:
    print('ALREADY_PRESENT')
    sys.exit(0)

data['allowed_domains'] = existing + missing
try:
    vault_request('POST', f'/v1/pki/roles/{ROLE}', data)
except Exception as e:
    print(f'ROLE_WRITE_FAILED: {e}')
    sys.exit(0)

print(f'ADDED: {missing}')
"""
    remote_py = (
        remote_py
        .replace('__VAULT_SERVER_FQDN__', VAULT_SERVER_FQDN)
        .replace('__REQUIRED_DOMAINS_JSON__', json.dumps(list(VAULT_HOLODECK_REQUIRED_DOMAINS)))
        .replace('__VAULT_TOKEN__', password)
    )
    script_b64 = base64.b64encode(remote_py.encode()).decode()
    remote_cmd = (
        f"echo {script_b64} | base64 -d > /tmp/fix_vault_holodeck_role.py && "
        f"python3 /tmp/fix_vault_holodeck_role.py; rm -f /tmp/fix_vault_holodeck_role.py"
    )

    try:
        result = lsf.ssh(remote_cmd, VKS_KUBECTL_HOST, password)
        out_text = (getattr(result, 'stdout', '') or '').strip()
        lsf.write_output(f'  holodeck role allowed_domains result: {out_text or "(no output)"}')
        if 'FAILED' in out_text:
            lsf.write_output('  WARNING: holodeck role allowed_domains may still need manual attention')
    except Exception as e:
        lsf.write_output(f'  WARNING: holodeck role allowed_domains check failed: {e}')


# Namespace name has a random per-pod suffix (e.g. svc-harbor-s8b1s) --
# discovered at runtime in ensure_harbor_healthy() rather than hardcoded.
HARBOR_NAMESPACE_PREFIX = 'svc-harbor-'
# Harbor is a wld01-a Supervisor Service, fronted by that domain's Avi --
# reuse the same avi_host already defined for the NSX<->Avi credential work
# above rather than duplicating it.
HARBOR_AVI_HOST = NSXT_ALB_DOMAINS[0]['avi_host']


def ensure_harbor_healthy(lsf, timeout_seconds=300, poll_interval=15, pending_stuck_seconds=300):
    """
    Verify the Harbor Supervisor Service (svc-harbor-<suffix> namespace) is
    actually serving, and self-remediate the stuck-pod failure mode
    root-caused live on 2026-08-20 (see the harbor-slow-start-podvm-
    annotation writeup): the vSphere CSI controller's AttachVolume can't
    find the "vmware-system-vm-uuid" annotation on a pod, either because
    spherelet explicitly reports pod status.reason ==
    "PodVMAnnotationsMissing", or -- the worse variant actually seen on
    harbor-core -- spherelet gets stuck in a local pod-cache desync (a
    tight "PodNotFound" retry loop) that never surfaces any reason at all
    and never self-heals on its own. Both look identical from the pod's own
    status: it just sits Pending indefinitely. Deleting the pod lets its
    controller (Deployment/StatefulSet) recreate it fresh, usually landing
    on a different node than whichever one had the desynced spherelet,
    which resolved every instance seen so far within seconds.

    This is deliberately more aggressive than
    ensure_workload_cluster_ako_healthy() above: rather than only touching
    pods with a *known* fixable waiting reason, any pod still Pending after
    pending_stuck_seconds gets deleted regardless of reason, since the
    worst-case variant (harbor-core) never reported one. A pod that's
    genuinely just slow to schedule/pull on a busy boot could in theory get
    touched by this before it would have recovered on its own -- accepted
    tradeoff given the real incident sat stuck for 35+ minutes with zero
    progress, and Harbor is a hard dependency for at least one lab module.

    Ruled out before writing this as the fix: cluster-wide CPU/memory
    *reservation* exhaustion (the most common documented root cause for
    this exact CSI error upstream) was checked live via PowerCLI during the
    2026-08-20 incident and was nowhere close to exhausted (~17% of CPU
    reservation used) -- so this function does not attempt any
    capacity-based diagnosis, only the pod-recreate remediation that was
    actually confirmed to work.

    Harbor isn't part of every pod build; if no svc-harbor-* namespace is
    found this is a silent no-op.

    FATAL: unlike most checks in this file, an unrecovered Harbor is treated
    as a lab-failing condition (lsf.labfail) rather than a warning, since
    Harbor is a hard dependency for at least one lab module -- callers
    should expect this function can end the process.
    """
    import json
    import time
    import datetime
    import os

    lsf.write_output('Checking Harbor Supervisor Service health...')
    password = lsf.get_password()

    def _find_namespace():
        result = lsf.ssh("kubectl --context Supervisor get ns -o name", VKS_KUBECTL_HOST, password)
        stdout = (getattr(result, 'stdout', '') or '')
        for line in stdout.splitlines():
            name = line.strip().split('/')[-1]
            if name.startswith(HARBOR_NAMESPACE_PREFIX):
                return name
        return None

    def _get_pods(namespace):
        cmd = f"kubectl --context Supervisor -n {namespace} get pods -o json"
        result = lsf.ssh(cmd, VKS_KUBECTL_HOST, password)
        stdout = (getattr(result, 'stdout', '') or '').strip()
        if not stdout:
            return None
        return json.loads(stdout).get('items', [])

    def _all_ready(pods):
        return bool(pods) and all(
            cs.get('ready')
            for pod in pods
            for cs in (pod.get('status', {}).get('containerStatuses', []) or [])
        )

    def _pod_age_seconds(pod):
        ts = pod.get('metadata', {}).get('creationTimestamp')
        if not ts:
            return 0.0
        created = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds()

    try:
        namespace = _find_namespace()
        if not namespace:
            lsf.write_output(f'  No {HARBOR_NAMESPACE_PREFIX}* namespace found -- Harbor not part of this build, skipping')
            return

        pods = _get_pods(namespace)
        if pods is None:
            lsf.write_output(f'  {namespace}: no output listing pods -- Supervisor may be unreachable, skipping')
            return

        to_delete = []
        for pod in pods:
            name = pod.get('metadata', {}).get('name', '<unknown>')
            status = pod.get('status', {})
            phase = status.get('phase')
            reason = status.get('reason')
            age = _pod_age_seconds(pod)

            if reason == 'PodVMAnnotationsMissing':
                lsf.write_output(f'  {namespace}/{name}: {reason} ({age:.0f}s old) -- deleting to force recreation')
                to_delete.append(name)
            elif phase == 'Pending' and age > pending_stuck_seconds:
                lsf.write_output(
                    f'  {namespace}/{name}: stuck Pending for {age:.0f}s (> {pending_stuck_seconds}s threshold) '
                    f'-- deleting to force recreation'
                )
                to_delete.append(name)

        for name in to_delete:
            lsf.ssh(
                f"kubectl --context Supervisor -n {namespace} delete pod {name} --wait=false",
                VKS_KUBECTL_HOST, password,
            )

        if to_delete:
            deadline = time.time() + timeout_seconds
            recovered = False
            while time.time() < deadline:
                time.sleep(poll_interval)
                pods = _get_pods(namespace)
                if _all_ready(pods):
                    recovered = True
                    break
            if recovered:
                lsf.write_output(f'  {namespace}: all pods Ready after recreating {to_delete}')
            else:
                lsf.labfail(
                    f'Harbor ({namespace}) still not healthy {timeout_seconds}s after recreating '
                    f'stuck pod(s) {to_delete}'
                )
        elif _all_ready(pods):
            lsf.write_output(f'  {namespace}: all pods already Ready -- no-op')
        else:
            not_ready = [
                p.get('metadata', {}).get('name')
                for p in pods
                if not all(cs.get('ready') for cs in (p.get('status', {}).get('containerStatuses', []) or []))
            ]
            lsf.write_output(
                f'  {namespace}: no pods stuck long enough to touch yet, but not all Ready: {not_ready} '
                f'-- leaving alone, may still be a normal cold start'
            )

        # End-to-end verification via Avi, independent of the pod-level view above --
        # this is what "Harbor is actually up" really means for a student.
        try:
            avi_session = _avi_login(os.environ['AVICTRL_PASSWORD'], HARBOR_AVI_HOST)
            # No server-side name filter here (deliberately) -- fetch the
            # full inventory and filter client-side, since this file has no
            # prior confirmed-working use of Avi's search-query syntax for
            # virtualservice-inventory and this list is small.
            resp = avi_session.get(f'https://{HARBOR_AVI_HOST}/api/virtualservice-inventory', timeout=15)
            resp.raise_for_status()
            results = resp.json().get('results', [])
            harbor_vs = next(
                (r for r in results if 'harbor-nginx' in r.get('config', {}).get('name', '')), None
            )
            if not harbor_vs:
                lsf.write_output('  WARNING: no Avi Virtual Service matching *harbor-nginx* found to verify')
            else:
                vs_name = harbor_vs['config']['name']
                vs_state = harbor_vs.get('runtime', {}).get('oper_status', {}).get('state')
                if vs_state == 'OPER_UP':
                    lsf.write_output(f'  Avi VS {vs_name}: OPER_UP -- Harbor confirmed reachable')
                else:
                    lsf.labfail(
                        f'Harbor Avi VS {vs_name} is {vs_state}, not OPER_UP, even after pod-level remediation'
                    )
        except Exception as e:
            lsf.write_output(f'  WARNING: could not verify Harbor via Avi API: {e}')

    except Exception as e:
        lsf.write_output(f'  WARNING: could not check/repair Harbor health: {e}')


def resync_nsxt_alb_cloud_connector_credentials(lsf):
    """
    Verify (fast path) that Avi's cloud-connector credential for each NSX
    domain still authenticates against that NSX Manager; if not, run the
    full fix (slow path, only on actual failure): rotate the credential
    on NSX via SDDC Manager (which also updates SDDC's own vault --
    ROTATE is the only SDDC operation that keeps both in sync for a
    SERVICE account), push the freshly rotated password into Avi's
    cloudconnectoruser object, and force a reconnect.

    Why "verify then conditionally fix" instead of always rotating: SDDC
    Manager's ROTATE is an async task that took ~50s round-trip when
    exercised manually (2026-07-25) -- fine as an occasional repair, too
    slow to pay unconditionally on every lab-startup run. The common case
    (credential already fine) is 2 cheap GETs per domain.

    Root cause: SDDC Manager's automated credential rotation for these
    NSX_ALB service accounts updates NSX + its own vault but never pushes
    the new value into Avi's cloud connector -- a known VCF gap (see the
    nsx-lockout incident doc). Auto-rotation is now disabled for these
    credentials (2026-07-25), but the same divergence could still recur
    from a manual rotation or a future product change, hence checking
    rather than assuming it's permanently fine.

    Also idempotently keeps password_change_frequency on the NSX side and
    autoRotatePolicy on the vCenter/Avi SSO credentials in their intended
    state, since both are cheap to verify.

    Known residual gap: the health check tests whether SDDC Manager's
    *currently vaulted* password authenticates to NSX -- it does not (and
    can't cheaply, since Avi always masks stored credentials as
    "<sensitive>") verify that Avi's cloud connector holds that same
    value. If _rotate_and_resync_cloud_connector's NSX-side ROTATE
    succeeds but the push into Avi doesn't complete in the same run (the
    exact race hit live on 2026-07-25, when one domain's ROTATE task took
    just over the polling window that existed at the time), a later boot's
    health check would see SDDC's password now matching NSX and
    conclude "healthy", never noticing Avi is still stale. The polling
    window below was widened specifically to make that race rare, but it
    isn't eliminated by construction.

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup.
    """
    import os
    import requests

    lsf.write_output('Checking Avi cloud-connector -> NSX credential health...')
    admin_password = os.environ['AVICTRL_PASSWORD']

    try:
        sddc_token = _sddc_login(admin_password)
    except Exception as e:
        lsf.write_output(f'  WARNING: could not log into SDDC Manager: {e}')
        return
    sddc_headers = {'Authorization': f'Bearer {sddc_token}'}

    for d in NSXT_ALB_DOMAINS:
        try:
            # ---- cheap health check: does NSX's current password (per SDDC's
            # own vault) actually authenticate? ----
            cred_resp = requests.get(
                'https://sddcmanager-a.site-a.vcf.lab/v1/system/credentials/service',
                params={'serviceType': 'NSX_ALB', 'targetType': 'NSXT_MANAGER'},
                headers=sddc_headers, verify=False, timeout=15,
            ).json()
            entry = next((c for c in cred_resp if c.get('username') == d['nsx_svc_user']), None)
            if not entry:
                lsf.write_output(f"  WARNING: {d['domain']}: no SDDC-held credential found for {d['nsx_svc_user']}")
                continue

            test = requests.get(
                f"https://{d['nsx_host']}/api/v1/node",
                auth=(d['nsx_svc_user'], entry['secret']), verify=False, timeout=15,
            )
            if test.status_code == 200:
                lsf.write_output(f"  {d['domain']}: {d['nsx_svc_user']} already authenticates to NSX -- no-op")
            else:
                lsf.write_output(
                    f"  {d['domain']}: {d['nsx_svc_user']} does NOT authenticate to NSX "
                    f"(HTTP {test.status_code}) -- rotating"
                )
                _rotate_and_resync_cloud_connector(lsf, d, sddc_headers, admin_password)

            # ---- cheap idempotent policy checks (always run, regardless of
            # the health check above) ----
            _ensure_nsx_password_frequency(d, admin_password, lsf)
            _ensure_nsx_lockout_immune(d, admin_password, lsf)

        except Exception as e:
            lsf.write_output(f"  WARNING: could not resync {d['domain']} cloud-connector credential: {e}")

    _ensure_sddc_auto_rotate_disabled(sddc_headers, lsf)


def _rotate_and_resync_cloud_connector(lsf, d, sddc_headers, admin_password):
    """Slow-path repair: ROTATE on NSX via SDDC Manager, then push the new password into Avi."""
    import requests
    import time

    rotate_resp = requests.patch(
        'https://sddcmanager-a.site-a.vcf.lab/v1/credentials',
        headers=sddc_headers, verify=False, timeout=15,
        json={
            'operationType': 'ROTATE',
            'elements': [{
                'resourceName': d['nsx_resource_name'],
                'resourceType': 'NSXT_MANAGER',
                'credentials': [{'credentialType': 'API', 'username': d['nsx_svc_user']}],
            }],
        },
    ).json()
    task_id = rotate_resp.get('id')
    if not task_id:
        lsf.write_output(f"    WARNING: rotate request rejected: {rotate_resp}")
        return

    # 36 x 5s = 180s. Manual testing (2026-07-24, quiet system) saw ROTATE
    # finish in ~50s; live on a freshly-booted pod (2026-07-25, 23 VCF
    # components initializing concurrently) one domain's ROTATE took just
    # over 60s and the original 12-attempt/60s window here missed it --
    # SDDC Manager's vault genuinely finished the rotation a bit later,
    # but this function had already given up and returned without ever
    # pushing the new password into Avi, leaving it stale until caught
    # manually. 180s gives real margin for a busy-boot scenario like that.
    for _ in range(36):
        status = requests.get(
            f'https://sddcmanager-a.site-a.vcf.lab/v1/tasks/{task_id}',
            headers=sddc_headers, verify=False, timeout=15,
        ).json().get('status')
        if status in ('SUCCESSFUL', 'FAILED'):
            break
        time.sleep(5)
    if status != 'SUCCESSFUL':
        lsf.write_output(f"    WARNING: rotate task ended in status {status}")
        return

    cred_resp = requests.get(
        'https://sddcmanager-a.site-a.vcf.lab/v1/system/credentials/service',
        params={'serviceType': 'NSX_ALB', 'targetType': 'NSXT_MANAGER'},
        headers=sddc_headers, verify=False, timeout=15,
    ).json()
    entry = next((c for c in cred_resp if c.get('username') == d['nsx_svc_user']), None)
    if not entry:
        lsf.write_output('    WARNING: rotated but could not retrieve new password')
        return
    new_password = entry['secret']

    avi_session = _avi_login(admin_password, d['avi_host'])
    cloud_list = avi_session.get(f"https://{d['avi_host']}/api/cloud", timeout=15).json()
    cloud = next((c for c in cloud_list.get('results', []) if c.get('nsxt_configuration')), None)
    if not cloud:
        lsf.write_output('    WARNING: rotated NSX password but could not find an NSX-T cloud on Avi to update')
        return
    cred_ref = cloud['nsxt_configuration']['nsxt_credentials_ref']

    avi_session.patch(
        cred_ref, timeout=15,
        json={'replace': {'nsxt_credentials': {'username': d['nsx_svc_user'], 'password': new_password}}},
    )
    # Bump then immediately restore metrics_polling_interval to force a
    # reconnect (a byte-identical PUT is deduped server-side -- Avi won't
    # re-attempt the connection at all without an actual field change).
    # Restoring the original value afterward matters: an earlier version
    # of this left it at current+1 permanently, which would silently
    # creep upward by 1 every time this repair path runs -- over this
    # vApp's 18-month save/resume lifecycle that's real, accumulating
    # drift in actual monitoring-poll behavior, not just a cosmetic
    # counter.
    cloud_url = f"https://{d['avi_host']}/api/cloud/{cloud['uuid']}"
    current_interval = cloud.get('metrics_polling_interval', 300)
    avi_session.patch(cloud_url, timeout=15, json={'replace': {'metrics_polling_interval': current_interval + 1}})
    avi_session.patch(cloud_url, timeout=15, json={'replace': {'metrics_polling_interval': current_interval}})
    lsf.write_output(f"    {d['domain']}: rotated on NSX and pushed new password into Avi cloud connector")


def _ensure_nsx_password_frequency(d, admin_password, lsf, min_days=NSXT_ALB_PASSWORD_EXPIRATION_DAYS):
    """Idempotently ensure the NSX service account's password_change_frequency is >= min_days."""
    import requests

    role_bindings = requests.get(
        f"https://{d['nsx_host']}/policy/api/v1/aaa/role-bindings",
        auth=('admin', admin_password), verify=False, timeout=15,
    ).json()
    binding = next((r for r in role_bindings.get('results', []) if r.get('name') == d['nsx_svc_user']), None)
    if not binding:
        return
    user_id = binding['user_id']
    url = f"https://{d['nsx_host']}/api/v1/node/users/{user_id}"
    current = requests.get(url, auth=('admin', admin_password), verify=False, timeout=15).json()
    if current.get('password_change_frequency', 0) >= min_days:
        return
    requests.put(
        url, auth=('admin', admin_password), verify=False, timeout=15,
        json={
            'userid': current['userid'], 'username': current['username'],
            'password_change_frequency': min_days,
            'password_change_warning': current.get('password_change_warning', 7),
            'password_reset_required': current.get('password_reset_required', False),
            'status': current.get('status', 'ACTIVE'),
        },
    )
    lsf.write_output(f"    {d['domain']}: bumped {d['nsx_svc_user']} password_change_frequency to {min_days}d")


def _ensure_nsx_lockout_immune(d, admin_password, lsf):
    """Idempotently ensure this domain's lockout_immune_addresses contains its expected entries."""
    import requests

    url = f"https://{d['nsx_host']}/api/v1/cluster/api-service"
    current = requests.get(url, auth=('admin', admin_password), verify=False, timeout=15).json()
    existing = current.get('lockout_immune_addresses', [])
    missing = [a for a in d['immune_addresses'] if a not in existing]
    if not missing:
        return
    current['lockout_immune_addresses'] = existing + missing
    requests.put(url, auth=('admin', admin_password), verify=False, timeout=15, json=current)
    lsf.write_output(f"    {d['domain']}: added {missing} to lockout_immune_addresses")


def _ensure_sddc_auto_rotate_disabled(sddc_headers, lsf):
    """Idempotently ensure the vCenter/Avi SSO credentials behind these NSX-ALB accounts have auto-rotate disabled."""
    import requests

    creds = requests.get(
        'https://sddcmanager-a.site-a.vcf.lab/v1/credentials',
        params={'accountType': 'SERVICE'}, headers=sddc_headers, verify=False, timeout=15,
    ).json()
    targets = [
        e for e in creds.get('elements', [])
        if e.get('credentialType') == 'SSO' and 'svc-alb-' in e.get('username', '')
        and e.get('autoRotatePolicy') is not None
    ]
    for e in targets:
        requests.patch(
            'https://sddcmanager-a.site-a.vcf.lab/v1/credentials',
            headers=sddc_headers, verify=False, timeout=15,
            json={
                'operationType': 'UPDATE_AUTO_ROTATE_POLICY',
                'elements': [{
                    'resourceName': e['resource']['resourceName'],
                    'resourceType': e['resource']['resourceType'],
                    'credentials': [{'credentialType': 'SSO', 'username': e['username']}],
                }],
                'autoRotatePolicy': {'frequencyInDays': 0, 'enableAutoRotatePolicy': False},
            },
        )
        lsf.write_output(f"    disabled auto-rotate on {e['username']} (was still enabled)")


def _request_with_retry(fn, *args, retries=5, delay=15, **kwargs):
    """
    Call fn(*args, **kwargs) (a requests-style call returning a
    Response), retrying on the two symptoms confirmed (2026-08-07) to be
    transient noise during a busy pod boot rather than real failures: a
    401 from License Hub's authserver component before it's fully warmed
    up, and a connection-level exception (timeout/refused) while
    NSX/Avi/LH are still coming up. Confirmed live: a License Hub upload
    hard-failed with a 401 during a fresh-boot run where NSX Manager
    calls were also timing out moments earlier in that same run; the
    identical call (same credentials, same file) succeeded immediately
    when run by hand a few minutes later against the same, by-then-
    settled pod.

    Any other status code (400/403/404/5xx) is returned immediately
    without retrying -- those are real errors, not boot-timing noise,
    and should fail fast rather than burn through all the retries first.

    Used throughout the License Hub functions below (upload, endpoint
    onboarding, mapping resync) plus their NSX-side calls, since all of
    them hit appliances that can still be initializing at the same point
    in adjustomatic's run.
    """
    import time

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            response = fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(delay)
                continue
            raise
        if response.status_code == 401 and attempt < retries:
            time.sleep(delay)
            continue
        return response
    raise last_exc


LICENSE_HUB_HOST = 'ssp.site-a.vcf.lab'
LICENSE_HUB_ZIP_PATH = (
    '/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/'
    'b3966fca-3ec0-4fe2-b628-ff186de53b19_1000001_1785992463049_v7.lic.zip'
)
# Expiration timestamp (epoch ms, as License Hub itself reports it) this
# specific v7.lic.zip bundle produces once imported -- confirmed live
# 2026-08-07 on both SKUs it carries (ANS-VMW-ALB, ANS-FW-ATP), works out
# to ~2027-07-10. Hardcoded deliberately, not derived generically: this
# lab's remaining life (through Aug/Sept 2027) is shorter than this one
# license's 360-day post-expiration grace period, so this bundle is
# expected to be the last one this lab ever needs -- there's no future
# replacement file to stay generic for. Update this if that ever changes
# (a newer replacement zip is checked in with a different expiration).
LICENSE_HUB_EXPECTED_EXPIRATION_MS = 1815237720365


def upload_license_hub_disconnected_license(lsf):
    """
    Upload the disconnected-mode license bundle (checked into this repo
    alongside adjustomatic.py) to License Hub via its Licensing Service
    API -- Workflow 4 ("Upload Avi Cloud Console License to LH") from the
    "License Hub API User Guide" Confluence doc.

    License Hub for this pod is hosted at ssp.site-a.vcf.lab -- confirmed
    live via its own UI (browser tab titled "License Hub" at that
    hostname). Note this is despite SSP (Security Services Platform) and
    License Hub being documented as two distinct appliances in the
    general Confluence guide (SSP is normally onboarded AS AN ENDPOINT
    INTO License Hub, not the LH appliance itself) -- this pod's actual
    deployment just runs both under the same hostname.

    Uses the same admin/AVICTRL_PASSWORD HTTP Basic-Auth session pattern
    as every other direct API call in this file (e.g.
    configure_nsxt_app_profiles above) -- no separate credential needed.
    Logs the license list before and after the upload so a no-op import
    (bundle already present) is visible in labstartup.log rather than
    silently indistinguishable from a real change.

    Skips the upload entirely if a license already on LH has the exact
    expiration_date this bundle is known to produce
    (LICENSE_HUB_EXPECTED_EXPIRATION_MS) -- i.e. it's already been
    imported -- rather than re-importing unconditionally every run. Purely
    an efficiency no-op, not a workaround for anything: License Hub
    tolerates re-importing the same bundle fine (confirmed live
    2026-08-07, back-to-back successful 204s importing the identical
    file).

    CORRECTION (2026-08-07): an earlier version of this docstring blamed
    the 400 ("unsupported content type") seen in testing on re-importing
    an already-known bundle -- that diagnosis was wrong. The 400 recurred
    on a completely fresh pod boot where LH still held an older, different
    license (not a re-import at all), which ruled that theory out. Root
    cause, confirmed via `curl --trace-ascii`: this multipart upload's
    file part must be sent as Content-Type application/octet-stream --
    curl's own default (no explicit -F ...;type=) sends exactly that and
    succeeds every time; explicitly forcing type=application/zip (what
    this code used to hardcode) reproduces the 400 on demand. License Hub
    apparently validates/whitelists the part's Content-Type string itself
    and does not accept "application/zip", regardless of the file's
    actual contents.

    Exact-match, not a generic "still has N days left" buffer, and
    deliberately hardcoded rather than derived from the zip's own
    contents (which don't expose anything reliably comparable -- it's a
    single opaque signed JWT asset-identity blob, not a plain
    license_id/expiration list) -- see LICENSE_HUB_EXPECTED_EXPIRATION_MS
    above for why hardcoding is the right call specifically for this
    bundle rather than a maintenance liability.

    Fatal: unlike most other functions in this file, a failed upload
    calls lsf.labfail(). Correct licensing is core lab functionality
    here, not a best-effort convenience fix -- a silent failure would
    leave the lab materially broken without flagging it.
    """
    import os
    import requests

    lsf.write_output('Uploading disconnected license file to License Hub...')
    lh_session = requests.Session()
    lh_session.verify = False
    lh_session.auth = ('admin', os.environ['AVICTRL_PASSWORD'])
    lh_mgr = f'https://{LICENSE_HUB_HOST}:443'

    try:
        before = _request_with_retry(lh_session.get, f'{lh_mgr}/licensing/views/licenses', timeout=15)
        before_licenses = before.json().get('results', [])
        lsf.write_output(f'  Licenses on License Hub before upload: {before.text[:500]}')

        if any(l.get('expiration_date') == LICENSE_HUB_EXPECTED_EXPIRATION_MS for l in before_licenses):
            lsf.write_output(
                f'  A license expiring {LICENSE_HUB_EXPECTED_EXPIRATION_MS} (this bundle\'s known '
                f'expiration) is already present on License Hub -- already imported, skipping re-upload'
            )
            return

        # Read the whole file into memory up front (it's ~20KB) rather than
        # passing a live file handle into files= -- a retried POST re-reads
        # whatever's in files= on each attempt, and a file handle would
        # already be at EOF after the first (failed) attempt, silently
        # sending an empty body on the retry. Bytes have no such state.
        with open(LICENSE_HUB_ZIP_PATH, 'rb') as license_file:
            license_bytes = license_file.read()
        upload_result = _request_with_retry(
            lh_session.post,
            f'{lh_mgr}/licensing/licenses?action=import',
            files={'file': (os.path.basename(LICENSE_HUB_ZIP_PATH), license_bytes, 'application/octet-stream')},
            timeout=60,
        )
        lsf.write_output(f'  Upload result {upload_result.status_code} - {upload_result.text[:500]}')
        upload_result.raise_for_status()

        after = _request_with_retry(lh_session.get, f'{lh_mgr}/licensing/views/licenses', timeout=15)
        lsf.write_output(f'  Licenses on License Hub after upload: {after.text[:500]}')

    except Exception as e:
        lsf.write_output(f'  License Hub upload failed: {e}')
        lsf.labfail(f'Adjustomatic failed at License Hub license upload: {e}')


def _ensure_license_hub_endpoint_onboarded(lh_session, lh_mgr, endpoint_type, host, username,
                                            admin_password, certificate, lsf,
                                            timeout_seconds=900, poll_interval=15):
    """
    Idempotently onboard a single host as a License Hub endpoint of the
    given type (NSX_MANAGER or AVI_CONTROLLER), then poll until READY.
    Matched by hostname -- no-ops (reuses the existing id) if already
    onboarded, mirroring the doc's own "already exists" success path.

    Shared by both halves of onboard_license_hub_endpoints() below since
    the onboard-then-poll logic is identical for NSX Manager and Avi
    Controller; only the cert-fetch mechanism differs between them.

    Returns the endpoint_id once READY, or None on failure/timeout --
    the caller decides whether that's fatal.
    """
    import time

    existing = _request_with_retry(
        lh_session.get, f'{lh_mgr}/licensing/views/endpoints', params={'endpoint_type': endpoint_type}, timeout=15
    ).json().get('results', [])
    match = next((e for e in existing if e.get('connection_info', {}).get('hostname') == host), None)

    if match:
        endpoint_id = match['id']
        if match.get('status') == 'READY':
            lsf.write_output(f'    {host}: already onboarded as {endpoint_type} endpoint and READY -- no-op')
            return endpoint_id
        lsf.write_output(f"    {host}: already onboarded as {endpoint_type} endpoint, status={match.get('status')} -- waiting for READY")
    else:
        create_result = _request_with_retry(
            lh_session.post,
            f'{lh_mgr}/licensing/endpoints', timeout=30,
            json={
                'endpoint_type': endpoint_type,
                'display_name': host.split('.')[0],
                'connection_info': {
                    'connection_type': 'DYNAMIC',
                    'hostname': host,
                    'username': username,
                    'password': admin_password,
                    'certificate': certificate,
                },
            },
        )
        lsf.write_output(f'    {host}: onboard result {create_result.status_code} - {create_result.text[:300]}')
        create_result.raise_for_status()
        endpoint_id = create_result.json().get('id')

    deadline = time.time() + timeout_seconds
    status = None
    while time.time() < deadline:
        try:
            endpoints = lh_session.get(
                f'{lh_mgr}/licensing/views/endpoints', params={'endpoint_type': endpoint_type}, timeout=15
            ).json().get('results', [])
            match = next((e for e in endpoints if e.get('id') == endpoint_id), None)
            status = match.get('status') if match else None
            if status == 'READY':
                lsf.write_output(f'    {host}: endpoint READY')
                return endpoint_id
        except Exception as e:
            # The outer while loop is already a poll-with-backoff (every
            # poll_interval up to timeout_seconds) -- treat a single bad
            # response (transient 401, non-JSON error body, timeout) the
            # same as "not ready yet" rather than crashing the whole poll.
            lsf.write_output(f'    {host}: poll check failed ({e}), will retry')
        time.sleep(poll_interval)

    lsf.write_output(f'    {host}: endpoint did not reach READY within {timeout_seconds}s (last status: {status})')
    return None


def onboard_license_hub_endpoints(lsf):
    """
    Onboard both NSX Managers (wld01-a, mgmt-a) and both Avi Controllers
    (alb-a, alb-b) as License Hub endpoints -- Workflows 5 and 7 from the
    "License Hub API User Guide" -- reusing NSXT_ALB_DOMAINS below for
    the host list. Run this BEFORE resync_license_hub_endpoint_mappings()
    so that function always has a real endpoint_id to map licenses onto,
    even after a pod rebuild -- License Hub is a separate appliance, not
    part of the saved vApp template, so it has no memory of previously-
    onboarded endpoints across a fresh deploy.

    Idempotent: matched by hostname, mirroring the doc's own "already
    exists -> skip, reuse existing id" semantics -- safe to run every
    boot (see _ensure_license_hub_endpoint_onboarded() above).

    NSX Manager cert: fetched live via
    GET https://<nsx_host>/api/v1/trust-management/certificates,
    filtered to the entry whose used_by[].service_types includes "API"
    (same call configure_nsxt_app_profiles's neighbors already use
    elsewhere in this file for NSX reachability).

    Avi Controller cert: there is no equivalent Avi REST API for this --
    per the "Steps to Retrieve and Upload Avi Controller Certificate in
    License Hub" KB, it's fetched by running fetch_cert.sh (checked into
    this same directory) against the controller's HTTPS port. That
    script is just a local openssl s_client TLS handshake (steered to
    prefer the controller's EC certificate, falling back to whatever it
    serves by default) -- it works identically run remotely as it does
    run locally on the controller, so no SSH hop onto the Avi Controller
    is needed; it's invoked here from wherever this script executes.

    Fatal: like the other License Hub functions in this file, failure to
    onboard (or reach READY) for any of the 4 endpoints calls
    lsf.labfail() -- but only after attempting all 4.
    """
    import os
    import subprocess
    import tempfile
    import requests

    lsf.write_output('Onboarding NSX Manager / Avi Controller endpoints to License Hub...')
    admin_password = os.environ['AVICTRL_PASSWORD']
    lh_session = requests.Session()
    lh_session.verify = False
    lh_session.auth = ('admin', admin_password)
    lh_mgr = f'https://{LICENSE_HUB_HOST}:443'
    fetch_cert_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fetch_cert.sh')

    failures = []
    for d in NSXT_ALB_DOMAINS:
        # ---- NSX Manager ----
        try:
            certs = _request_with_retry(
                requests.get, f"https://{d['nsx_host']}/api/v1/trust-management/certificates",
                auth=('admin', admin_password), verify=False, timeout=15,
            ).json()
            api_cert = next(
                (c for c in certs.get('results', [])
                 if any('API' in u.get('service_types', []) for u in c.get('used_by', []) or [])),
                None,
            )
            if not api_cert:
                lsf.write_output(f"  WARNING: {d['domain']}: no API-service certificate found on {d['nsx_host']}")
                failures.append(f"NSX Manager ({d['domain']})")
            else:
                endpoint_id = _ensure_license_hub_endpoint_onboarded(
                    lh_session, lh_mgr, 'NSX_MANAGER', d['nsx_host'], 'admin',
                    admin_password, api_cert['pem_encoded'], lsf,
                )
                if not endpoint_id:
                    failures.append(f"NSX Manager ({d['domain']})")
        except Exception as e:
            lsf.write_output(f"  WARNING: {d['domain']}: NSX Manager onboarding failed: {e}")
            failures.append(f"NSX Manager ({d['domain']})")

        # ---- Avi Controller ----
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                result = subprocess.run(
                    ['bash', fetch_cert_script, d['avi_host']],
                    cwd=tmpdir, capture_output=True, text=True, timeout=30,
                )
                cert_path = os.path.join(tmpdir, 'full_chain.pem')
                if result.returncode != 0 or not os.path.isfile(cert_path):
                    lsf.write_output(
                        f"  WARNING: {d['domain']}: fetch_cert.sh failed for {d['avi_host']}: "
                        f"{result.stdout} {result.stderr}"
                    )
                    failures.append(f"Avi Controller ({d['domain']})")
                    continue
                with open(cert_path, 'r') as f:
                    avi_cert = f.read()

            endpoint_id = _ensure_license_hub_endpoint_onboarded(
                lh_session, lh_mgr, 'AVI_CONTROLLER', d['avi_host'], 'admin',
                admin_password, avi_cert, lsf,
            )
            if not endpoint_id:
                failures.append(f"Avi Controller ({d['domain']})")
        except Exception as e:
            lsf.write_output(f"  WARNING: {d['domain']}: Avi Controller onboarding failed: {e}")
            failures.append(f"Avi Controller ({d['domain']})")

    if failures:
        lsf.labfail(f'Adjustomatic failed at License Hub endpoint onboarding for: {failures}')


def resync_license_hub_endpoint_mappings(lsf):
    """
    Ensure each of this pod's 4 already-onboarded License Hub endpoints
    (NSX Manager wld01-a/mgmt-a, Avi Controller alb-a/alb-b -- reusing
    NSXT_ALB_DOMAINS below) is mapped to the freshest non-expired license
    on LH for each SKU it currently holds, swapping out an older/soon-
    expiring mapping rather than leaving it in place alongside a new one.

    Run this AFTER upload_license_hub_disconnected_license() -- it only
    re-points EXISTING endpoint<->license mappings at whatever's newest
    on LH; it does not onboard endpoints (already onboarded in this pod,
    unlike a fresh pod following Workflows 5/7 in the License Hub API
    User Guide) or upload licenses itself.

    Per endpoint: list its current license-endpoint-mappings
    (GET /licensing/license-endpoint-mappings, filtered client-side by
    endpoint_id -- this API has no server-side filter param), and for
    each currently-mapped license's sku_code, pick the LH license
    (GET /licensing/views/licenses) with that sku_code and the latest
    expiration_date. If that differs from what's currently mapped,
    replace it with a single bulk DELETE-old/CREATE-new call to
    POST /licensing/bulk-license-endpoint-mappings -- atomic from the
    caller's perspective, so there's never a window where the endpoint
    has zero or two licenses of the same SKU mapped. No-op if the
    endpoint is already on the freshest available license for that SKU.

    NSX Manager endpoints get an explicit force-sync call afterward
    (POST <nsx_host>/policy/api/v1/licenses/action/async-query) so the
    swap is visible immediately rather than waiting on NSX's own
    15-minute license-refresh cycle. Avi Controller has no equivalent
    documented REST API for this (only a UI/CLI manual refresh, per the
    "Avi Controller 32.1.1 -- Licensing Configuration Guide" doc) -- Avi
    endpoints pick up the swap on their own next auto-sync (also ~15 min
    per that same doc).

    Fatal: like upload_license_hub_disconnected_license, this calls
    lsf.labfail() if any of the 4 endpoints couldn't be resynced --
    but only after attempting all 4, so one bad endpoint doesn't stop
    the other three from getting fixed. An endpoint left on an expiring
    license is a real lab-breaking condition, not cosmetic drift.
    """
    import os
    import requests

    lsf.write_output('Resyncing License Hub endpoint <-> license mappings...')
    admin_password = os.environ['AVICTRL_PASSWORD']
    lh_session = requests.Session()
    lh_session.verify = False
    lh_session.auth = ('admin', admin_password)
    lh_mgr = f'https://{LICENSE_HUB_HOST}:443'

    targets = []
    for d in NSXT_ALB_DOMAINS:
        targets.append({
            'label': f"NSX Manager ({d['domain']})",
            'host': d['nsx_host'],
            'endpoint_type': 'NSX_MANAGER',
            'force_sync_host': d['nsx_host'],
        })
        targets.append({
            'label': f"Avi Controller ({d['domain']})",
            'host': d['avi_host'],
            'endpoint_type': 'AVI_CONTROLLER',
            'force_sync_host': None,
        })

    try:
        all_licenses = _request_with_retry(
            lh_session.get, f'{lh_mgr}/licensing/views/licenses', timeout=15
        ).json().get('results', [])
    except Exception as e:
        lsf.write_output(f'  Could not list licenses on License Hub: {e}')
        lsf.labfail(f'Adjustomatic failed at License Hub endpoint resync: could not list licenses: {e}')
        return

    failures = []
    for t in targets:
        try:
            endpoints = _request_with_retry(
                lh_session.get, f"{lh_mgr}/licensing/views/endpoints", params={'endpoint_type': t['endpoint_type']}, timeout=15
            ).json().get('results', [])
            endpoint = next((e for e in endpoints if e.get('connection_info', {}).get('hostname') == t['host']), None)
            if not endpoint:
                lsf.write_output(f"  WARNING: {t['label']}: no License Hub endpoint found for hostname {t['host']} -- skipping")
                failures.append(t['label'])
                continue
            endpoint_id = endpoint['id']

            mappings = _request_with_retry(
                lh_session.get, f'{lh_mgr}/licensing/license-endpoint-mappings', timeout=15
            ).json().get('results', [])
            current_mappings = [m for m in mappings if m.get('endpoint_id') == endpoint_id]
            if not current_mappings:
                lsf.write_output(f"  {t['label']}: no current license mappings found -- nothing to swap")
                continue

            operations = []
            for m in current_mappings:
                current_license_id = m['license_id']
                current_license = next((l for l in all_licenses if l.get('license_id') == current_license_id), None)
                sku_code = current_license.get('sku_code') if current_license else None
                if not sku_code:
                    lsf.write_output(
                        f"  {t['label']}: could not determine sku_code for currently-mapped "
                        f"license {current_license_id} -- leaving as-is"
                    )
                    continue

                candidates = [l for l in all_licenses if l.get('sku_code') == sku_code]
                best = max(candidates, key=lambda l: l.get('expiration_date', 0), default=None)
                if not best or best['license_id'] == current_license_id:
                    lsf.write_output(f"  {t['label']}: {sku_code} already on the freshest available license -- no-op")
                    continue

                lsf.write_output(
                    f"  {t['label']}: {sku_code} swapping {current_license_id} "
                    f"(expires {current_license.get('expiration_date')}) -> {best['license_id']} "
                    f"(expires {best.get('expiration_date')})"
                )
                operations.append({'operation': 'DELETE', 'mapping': {'license_id': current_license_id, 'endpoint_id': endpoint_id}})
                operations.append({'operation': 'CREATE', 'mapping': {'license_id': best['license_id'], 'endpoint_id': endpoint_id}})

            if not operations:
                continue

            bulk_result = _request_with_retry(
                lh_session.post, f'{lh_mgr}/licensing/bulk-license-endpoint-mappings',
                json={'operations': operations}, timeout=30
            )
            lsf.write_output(f"  {t['label']}: bulk mapping result {bulk_result.status_code} - {bulk_result.text[:300]}")
            bulk_result.raise_for_status()

            if t['force_sync_host']:
                sync_result = _request_with_retry(
                    requests.post, f"https://{t['force_sync_host']}/policy/api/v1/licenses/action/async-query",
                    auth=('admin', admin_password), verify=False, timeout=15,
                )
                lsf.write_output(f"  {t['label']}: force-sync triggered ({sync_result.status_code})")
            else:
                lsf.write_output(f"  {t['label']}: no force-sync API available for Avi Controller -- will auto-sync within ~15 minutes")

        except Exception as e:
            lsf.write_output(f"  WARNING: {t['label']}: failed to resync license mapping: {e}")
            failures.append(t['label'])

    if failures:
        lsf.labfail(f'Adjustomatic failed at License Hub endpoint resync for: {failures}')


SSO_DOMAINS = (
    {'vc_host': 'vc-wld01-a.site-a.vcf.lab', 'sso_user': 'administrator@wld.sso'},
    {'vc_host': 'vc-mgmt-a.site-a.vcf.lab', 'sso_user': 'administrator@vsphere.local'},
)
SSO_PASSWORD_LIFETIME_DAYS = 730  # matches NSXT_ALB_PASSWORD_EXPIRATION_DAYS above


def resync_sso_password_policy(lsf):
    """
    Idempotently ensure each vCenter SSO domain's password-expiration
    policy allows at least SSO_PASSWORD_LIFETIME_DAYS (default 90).

    There is no REST API for this -- verified directly against this
    vCenter's own VAPI metadata (grepped /usr/lib/vmware-vapi/metadata/
    on the appliance: the only PasswordPolicy* type present is
    appliance-local-OS-account scoped, via
    com.vmware.appliance_metadata.json -- no identity-domain-scoped
    metadata file exists at all), and this version's sso-config.sh CLI
    has no password-policy sub-command either. The only way to fix it:
    PowerCLI's VMware.vSphere.SsoAdmin module's Get/Set-SsoPasswordPolicy
    cmdlets -- that module is NOT part of base PowerCLI and is not
    preinstalled on the jump host image (confirmed 2026-07-25). This
    function installs it itself (idempotent -- skipped if already
    present) rather than assuming a prior manual install persists, since
    this vApp can be rebuilt from a base template that never had it.
    Requires outbound proxy access to PowerShell Gallery at install time
    only (opened up for the jump host on 2026-07-25 specifically for
    this) -- once installed, the module stays on disk and no further
    network access is needed for the actual policy check/fix below.
    If that proxy access isn't available on some future rebuild, this
    logs a clear warning and skips the policy check entirely, rather
    than failing lab startup.

    Runs via lsf.ssh() to VKS_KUBECTL_HOST (console.site-a.vcf.lab) since
    that's where PowerCLI actually lives -- shipped as a single
    base64-encoded pwsh script covering both domains in one pwsh process
    (to amortize its ~5s startup/module-import cost once instead of
    paying it twice), same base64-over-SSH pattern used elsewhere in
    this file to dodge nested-quoting issues (pwsh's own quoting rules
    layered under lsf.ssh()'s outer wrapping).

    Non-fatal: any failure here is logged as a warning and does not fail
    lab startup.
    """
    import base64

    lsf.write_output('Checking vCenter SSO password-expiration policy...')
    password = lsf.get_password()

    domains_ps = ','.join(
        f"@{{Host='{d['vc_host']}';User='{d['sso_user']}'}}" for d in SSO_DOMAINS
    )
    pwsh_script = f"""
if (-not (Get-Module -ListAvailable VMware.vSphere.SsoAdmin)) {{
    Write-Output 'VMware.vSphere.SsoAdmin not installed -- installing...'
    try {{
        $env:http_proxy = 'http://proxy:3128'
        $env:https_proxy = 'http://proxy:3128'
        Install-Module -Name VMware.vSphere.SsoAdmin -Scope CurrentUser -Force -AllowClobber -Proxy 'http://proxy:3128' -ErrorAction Stop
        Write-Output 'VMware.vSphere.SsoAdmin installed successfully'
    }} catch {{
        Write-Output "ERROR: could not install VMware.vSphere.SsoAdmin: $_"
        exit 0
    }}
}}
Import-Module VMware.vSphere.SsoAdmin -ErrorAction Stop
$null = Set-PowerCLIConfiguration -InvalidCertificateAction Ignore -Confirm:$false
$domains = {domains_ps}
foreach ($d in $domains) {{
    $conn = $null
    try {{
        # Capture the connection object and explicitly disconnect it (in
        # the finally block below) before the next domain's iteration --
        # without this, a second Connect-SsoAdminServer call leaves BOTH
        # sessions active, and Get-SsoPasswordPolicy (which isn't scoped
        # to a single connection) returns one result per active
        # connection, collapsing into an array on assignment (surfaced as
        # a "Cannot convert System.Object[]" error / duplicated-looking
        # log line -- caught while testing this 2026-07-25). There's no
        # Get-SsoAdminServer listing cmdlet in this module, so the
        # connection object returned by Connect-SsoAdminServer itself is
        # the only handle available to disconnect cleanly.
        $conn = Connect-SsoAdminServer -Server $d.Host -User $d.User -Password '{password}' -SkipCertificateCheck
        $policy = Get-SsoPasswordPolicy
        $currentDays = [int]$policy.PasswordLifetimeDays
        if ($currentDays -ge {SSO_PASSWORD_LIFETIME_DAYS}) {{
            Write-Output "$($d.Host): already ${{currentDays}}d -- no-op"
        }} else {{
            $policy | Set-SsoPasswordPolicy -PasswordLifetimeDays {SSO_PASSWORD_LIFETIME_DAYS} | Out-Null
            Write-Output "$($d.Host): bumped ${{currentDays}}d -> {SSO_PASSWORD_LIFETIME_DAYS}d"
        }}
    }} catch {{
        Write-Output "$($d.Host): ERROR $_"
    }} finally {{
        if ($conn) {{ Disconnect-SsoAdminServer -Server $conn -ErrorAction SilentlyContinue }}
    }}
}}
"""
    script_b64 = base64.b64encode(pwsh_script.encode()).decode()
    remote_cmd = (
        f"echo {script_b64} | base64 -d > /tmp/sso_policy_check.ps1 && "
        f"pwsh -NoLogo -File /tmp/sso_policy_check.ps1; "
        f"rm -f /tmp/sso_policy_check.ps1"
    )
    try:
        result = lsf.ssh(remote_cmd, VKS_KUBECTL_HOST, password)
        out_text = (getattr(result, 'stdout', '') or '').strip()
        for line in out_text.splitlines():
            lsf.write_output(f'  {line}')
        if not out_text:
            lsf.write_output('  (no output)')
    except Exception as e:
        lsf.write_output(f'  WARNING: could not resync SSO password policy: {e}')


# Startup telemetry: reports how long each adjustomatic run took and which
# steps were healthy, as one JSON object per run uploaded to a GCS bucket
# -- no Sheets, no Drive, no Apps Script, no BigQuery, no human OAuth
# consent. Deliberately best-effort -- see send_telemetry_summary()'s
# docstring for why this can never affect lab pass/fail.
#
# Auth: a personal OAuth refresh token (your own Google identity), not a
# service account. Four separate Google-side walls were hit reaching this
# point (all confirmed 2026-07-28): (1) a human "Sign in with Google"
# OAuth flow, under a custom consent screen created in this project, hit
# org_internal -- this project lives under the labs.broadcom.com Cloud
# org, a different domain than individual @broadcom.com accounts, so no
# human broadcom.com sign-in could pass that org-membership check for an
# app registered *in this project*; (2) a service account's own email
# (...iam.gserviceaccount.com) couldn't be shared a Sheet, blocked by a
# Workspace sharing-allowlist policy; (3) creating a BigQuery dataset was
# denied by project-level IAM (missing bigquery.datasets.create); (4)
# granting the service account IAM access to a GCS bucket was denied too
# (missing storage.buckets.setIamPolicy) -- but a manual Console upload
# confirmed the account's own human identity already has OWNER access to
# the bucket via GCS's legacy per-bucket ACL (automatic for the creating
# principal), with no extra grant needed at all.
#
# That's why this ended up as a personal credential: (1) was about a
# *custom* OAuth client's audience restriction -- it does NOT recur here
# because this uses gcloud's own pre-registered, globally-available OAuth
# client (the client_id/secret embedded in `gcloud auth
# application-default login`'s output) rather than a new consent screen
# registered under this project. (2)/(3)/(4) don't apply to begin with,
# since this never touches Sheets/Drive/BigQuery and needs no new IAM
# grant on the bucket. Just a plain OAuth refresh-token grant -- no JWT
# signing, no crypto library needed (contrast the service-account
# version this replaced, which needed pyjwt+cryptography for RS256
# signing).
#
# There's no live "dashboard" here -- summarize_telemetry.py (run
# locally, not on any pod) pulls every object down and renders a static
# HTML table on demand, which sidesteps needing any internal hosting too.
#
# TELEMETRY_VAULT_FILE points at this repo's existing shared secrets.yml
# (already loaded as vars_files by labconfig_finalstage.yaml/
# labconfig_registration.yaml/the avi_configs playbooks) rather than a
# separate dedicated file -- the telemetry credential was added there
# directly (2026-07-28) under telemetry_user_credentials_json. Ansible
# ignores vars a given playbook doesn't reference, so this extra key is
# harmless to everything else already loading that file.
TELEMETRY_POD_ID_FILE = '/home/holuser/pod_telemetry_id.txt'
TELEMETRY_VAULT_FILE = '/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/secrets.yml'
TELEMETRY_VAULT_PASSWORD_FILE = '/home/holuser/vaultsecret.txt'

# ~/hol/labstartup.log summary: the pod's overall boot-time log, shared
# across every startup-stage script (not just adjustomatic.py) -- see
# summarize_labstartup_log()'s docstring. Format verified 2026-07-29
# directly against the real HOLFY27-MGR-HOLUSER source (public repo,
# see CLAUDE.md): lsf.write_output() formats every line as
# '[YYYY-MM-DD HH:MM:SS] {msg}', and lsf.startup(module_name, ...) --
# what actually runs each named Startup/ module, including this lab's
# own Startup/final.py override that in turn calls adjustomatic.main()
# -- wraps each one with an exact 'Starting module: X from Y' /
# 'Completed module: X' / 'Module X reported failure' / 'Module X
# failed: <exc>' framing. That's real ground truth for section
# boundaries, not a guess -- see LABSTARTUP_MODULE_*_RE below.
LABSTARTUP_LOG_FILE = '/home/holuser/hol/labstartup.log'
LABSTARTUP_LOG_MAX_BYTES = 50 * 1024 * 1024  # skip rather than risk a slow/huge parse
LABSTARTUP_LOG_TIMESTAMP_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)$')
LABSTARTUP_MODULE_START_RE = re.compile(r'^Starting module: (\S+) from (.+)$')
LABSTARTUP_MODULE_COMPLETE_RE = re.compile(r'^Completed module: (\S+)$')
LABSTARTUP_MODULE_REPORTED_FAILURE_RE = re.compile(r'^Module (\S+) reported failure$')
LABSTARTUP_MODULE_FAILED_RE = re.compile(r'^Module (\S+) failed: (.*)$')
LABSTARTUP_LOG_NOTABLE_KEYWORDS = ('WARNING', 'ERROR', 'FAIL', 'TRACEBACK', 'EXCEPTION')
LABSTARTUP_LOG_TOP_GAPS = 5  # secondary/supplementary to module_sections below -- catches
                             # slow stretches not attributed to any named module (e.g. time
                             # between labstartup.py's own prelim steps, before the first
                             # 'Starting module:' line)
LABSTARTUP_LOG_TOP_NOTABLE = 30
TELEMETRY_GCS_BUCKET = 'lans001-avi-demo-labs-hol-telemetry'  # TODO: paste the bucket name here once created


def _get_or_create_pod_id():
    """
    Return a UUID identifying this pod, generating and caching one on
    first run so it's stable across reboots of the same pod. Needed
    because manager's own hostname is identical across every pod --
    there's no other existing identifier in this codebase that
    distinguishes one pod from another (checked: no pod/vpod-number
    convention exists anywhere in this repo).

    Best-effort: if the cache file can't be read or written, this still
    returns a usable (just not stable across reboots) UUID rather than
    raising -- telemetry identity is not worth risking lab startup over.
    """
    import os
    import uuid

    if os.path.isfile(TELEMETRY_POD_ID_FILE):
        try:
            cached = open(TELEMETRY_POD_ID_FILE).read().strip()
            if cached:
                return cached
        except OSError:
            pass

    pod_id = str(uuid.uuid4())
    try:
        with open(TELEMETRY_POD_ID_FILE, 'w') as f:
            f.write(pod_id + '\n')
    except OSError:
        pass
    return pod_id


@contextlib.contextmanager
def track_step(lsf, results, name, scan_for_warnings=True):
    """
    Time one main() step and classify it ok/degraded/failed for the
    end-of-run telemetry summary, WITHOUT changing any existing
    function's behavior or touching its internals -- it only observes
    what that function already does, by temporarily monkeypatching two
    things every step function in this file already calls by
    convention:
      - lsf.write_output(...) -- watched for the literal string
        'WARNING' every non-fatal failure path in this file already
        logs -> classified 'degraded'. Pass scan_for_warnings=False to
        skip this for steps that dump large blocks of *third-party* tool
        output through write_output (the three ansible-playbook blocks
        in main() do this via retry_io(lsf.write_output, result.stdout,
        ...)) -- Ansible itself routinely emits its own benign
        '[WARNING]:' lines (deprecations, module notices) on completely
        successful, unchanged runs, which would otherwise false-positive
        every single run regardless of whether anything was actually
        wrong. Their real status is already fully determined by whether
        lsf.labfail gets called, so nothing is lost by skipping the
        keyword scan for just those steps.
      - lsf.labfail(...) -- the one call every genuinely lab-failing
        path in this file already makes -> classified 'failed'.
    Both are restored in the finally block regardless of outcome.

    Never suppresses the wrapped block's own exception -- if something
    here ever needs to raise, it still propagates; this context manager
    only records, it never changes lab-startup control flow.
    """
    import time

    orig_write_output = lsf.write_output
    orig_labfail = lsf.labfail
    saw_warning = []
    saw_labfail = []

    def _tracking_write_output(msg, *args, **kwargs):
        if scan_for_warnings:
            try:
                if 'WARNING' in str(msg):
                    saw_warning.append(True)
            except Exception:
                pass
        return orig_write_output(msg, *args, **kwargs)

    def _tracking_labfail(msg, *args, **kwargs):
        saw_labfail.append(str(msg))
        return orig_labfail(msg, *args, **kwargs)

    lsf.write_output = _tracking_write_output
    lsf.labfail = _tracking_labfail
    start = time.time()
    exc_repr = None
    try:
        yield
    except Exception as e:
        exc_repr = str(e)
        raise
    finally:
        lsf.write_output = orig_write_output
        lsf.labfail = orig_labfail
        if exc_repr or saw_labfail:
            status = 'failed'
        elif saw_warning:
            status = 'degraded'
        else:
            status = 'ok'
        results.append({
            'name': name,
            'duration_s': round(time.time() - start, 1),
            'status': status,
            'detail': exc_repr or (saw_labfail[0] if saw_labfail else None),
        })


@contextlib.contextmanager
def _labfail_uploads_telemetry(lsf, telemetry_results, run_started_at_iso, run_start):
    """
    For the duration of this context, wrap lsf.labfail so that calling it
    also uploads whatever telemetry has been collected so far (forced
    overall_status='failed', via a synthetic 'labfail' entry) BEFORE
    delegating to the real labfail(), which calls sys.exit(1) and never
    returns control to its caller (confirmed against the real
    HOLFY27-MGR-HOLUSER source, 2026-07-29 -- see CLAUDE.md).

    Without this, send_telemetry_summary() at the end of main() would
    simply never be reached on any run that calls labfail() anywhere (the
    three ansible-playbook steps, or wait_for_vks_nodepool_scaleup's
    timeout) -- meaning the exact runs where telemetry matters most (a
    real failure) would silently produce zero telemetry.

    Restores the original lsf.labfail on exit (including on exception) --
    lsfunctions is a shared, process-wide singleton module (the
    lab-specific Startup/final.py that imports and calls
    adjustomatic.main() holds the SAME lsf module object, not a copy), so
    leaving this monkeypatch in place beyond adjustomatic's own run would
    make final.py's own later, unrelated labfail() calls (e.g. its
    lab-update.py failure handling, well after adjustomatic.main()
    returns) incorrectly re-upload adjustomatic's already-finished
    telemetry using stale closure state.

    track_step()'s own per-call monkeypatching of lsf.labfail layers on
    top of this and correctly restores back down to this wrapper (not
    further down to the true original) at the end of each `with
    track_step(...)` block, since it always saves/restores whatever
    lsf.labfail was at that block's own entry/exit.
    """
    import time

    orig_labfail = lsf.labfail

    def _wrapped(reason, *_a, **_kw):
        try:
            telemetry_results.append({
                'name': 'labfail', 'duration_s': round(time.time() - run_start, 1),
                'status': 'failed', 'detail': str(reason),
            })
            send_telemetry_summary(lsf, telemetry_results, run_started_at_iso, time.time() - run_start)
        except Exception:
            pass
        return orig_labfail(reason, *_a, **_kw)

    lsf.labfail = _wrapped
    try:
        yield
    finally:
        lsf.labfail = orig_labfail


def _get_telemetry_access_token():
    """
    Exchange the ansible-vault-stored personal OAuth refresh token for a
    short-lived access token. See the telemetry constants block above
    main() for the full history of why this is a personal credential
    (minted via `gcloud auth application-default login`, using gcloud's
    own pre-registered OAuth client) rather than a service account.

    Just a plain refresh-token grant -- no JWT signing, since this is an
    "authorized_user" style credential (client_id/client_secret/
    refresh_token), not a service-account key.

    Returns the access token on success, or None (never raises) if the
    vault file doesn't exist, decryption fails, the credential doesn't
    parse, or Google's token endpoint rejects the refresh token --
    send_telemetry_summary() treats that as "telemetry unavailable this
    run," never as a lab failure.
    """
    import json
    import os
    import subprocess
    import requests
    import yaml

    if not os.path.isfile(TELEMETRY_VAULT_FILE):
        return None

    try:
        result = subprocess.run(
            ['/usr/bin/ansible-vault', 'view', '--vault-password-file', TELEMETRY_VAULT_PASSWORD_FILE, TELEMETRY_VAULT_FILE],
            capture_output=True, text=True, timeout=15, check=True,
        )
        vault_contents = yaml.safe_load(result.stdout)
        creds = json.loads(vault_contents['telemetry_user_credentials_json'])
        resp = requests.post(
            'https://oauth2.googleapis.com/token', timeout=15,
            data={
                'client_id': creds['client_id'],
                'client_secret': creds['client_secret'],
                'refresh_token': creds['refresh_token'],
                'grant_type': 'refresh_token',
            },
        )
        resp.raise_for_status()
        return resp.json()['access_token']
    except Exception:
        return None


def summarize_labstartup_log(lsf):
    """
    Best-effort summary of ~/hol/labstartup.log (the pod's overall
    boot-time log, shared across every startup-stage script -- not just
    this one).

    Primary signal: real named sections. lsf.startup(module_name, ...) --
    what actually runs each Startup/<module>.py, including this lab's own
    Startup/final.py override that calls adjustomatic.main() -- wraps
    every module with an exact 'Starting module: X from Y' / 'Completed
    module: X' / 'Module X reported failure' / 'Module X failed: <exc>'
    framing (verified 2026-07-29 directly against the real
    HOLFY27-MGR-HOLUSER source -- see CLAUDE.md). This pairs those lines
    by module name to get a real per-section duration, not a guess.

    Secondary signal: the top LABSTARTUP_LOG_TOP_GAPS longest gaps between
    ANY two consecutive timestamped lines, for slowness not attributed to
    a named module (e.g. time spent before the very first module starts).

    Also collects notable WARNING/ERROR/FAIL/etc lines throughout,
    regardless of whether they fall inside a named module or not.

    Returns a dict with 'total_span_s', 'module_sections' (in
    chronological start order: name, status
    ok/reported_failure/failed/unclosed, duration_s, detail), 'slow_gaps'
    (top LABSTARTUP_LOG_TOP_GAPS gaps between consecutive lines),
    'notable_messages' (up to LABSTARTUP_LOG_TOP_NOTABLE most recent
    lines matching LABSTARTUP_LOG_NOTABLE_KEYWORDS), and
    'notable_messages_truncated' -- or None if the log is missing,
    unreadable, unexpectedly huge, or contains no parseable timestamped
    lines at all. Never raises.
    """
    import datetime
    import os

    try:
        if not os.path.isfile(LABSTARTUP_LOG_FILE):
            return None
        if os.path.getsize(LABSTARTUP_LOG_FILE) > LABSTARTUP_LOG_MAX_BYTES:
            lsf.write_output(
                f'  labstartup.log summary skipped -- file is over '
                f'{LABSTARTUP_LOG_MAX_BYTES // (1024 * 1024)}MB, too large to '
                f'safely parse inline'
            )
            return None

        entries = []
        notable = []
        module_starts = {}  # name -> (start_ts, start_ts_str)
        module_sections = []
        with open(LABSTARTUP_LOG_FILE, 'r', errors='replace') as f:
            for line in f:
                match = LABSTARTUP_LOG_TIMESTAMP_RE.match(line.rstrip('\n'))
                if not match:
                    continue
                ts_str, message = match.groups()
                try:
                    ts = datetime.datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
                entries.append((ts, message))
                if any(kw in message.upper() for kw in LABSTARTUP_LOG_NOTABLE_KEYWORDS):
                    notable.append(f'[{ts_str}] {message}')

                start_match = LABSTARTUP_MODULE_START_RE.match(message)
                if start_match:
                    module_starts[start_match.group(1)] = ts
                    continue
                complete_match = LABSTARTUP_MODULE_COMPLETE_RE.match(message)
                if complete_match:
                    name = complete_match.group(1)
                    if name in module_starts:
                        module_sections.append({
                            'name': name, 'status': 'ok',
                            'duration_s': round((ts - module_starts.pop(name)).total_seconds(), 1),
                            'detail': None,
                        })
                    continue
                reported_failure_match = LABSTARTUP_MODULE_REPORTED_FAILURE_RE.match(message)
                if reported_failure_match:
                    name = reported_failure_match.group(1)
                    if name in module_starts:
                        module_sections.append({
                            'name': name, 'status': 'reported_failure',
                            'duration_s': round((ts - module_starts.pop(name)).total_seconds(), 1),
                            'detail': None,
                        })
                    continue
                failed_match = LABSTARTUP_MODULE_FAILED_RE.match(message)
                if failed_match:
                    name, detail = failed_match.groups()
                    if name in module_starts:
                        module_sections.append({
                            'name': name, 'status': 'failed',
                            'duration_s': round((ts - module_starts.pop(name)).total_seconds(), 1),
                            'detail': detail,
                        })
                    continue

        if not entries:
            return None

        # Modules that started but never saw a matching completion line
        # (still running when this snapshot was taken, or the log was
        # truncated) -- approximate duration against the last timestamp
        # seen anywhere in the log, clearly marked as an approximation.
        last_ts = entries[-1][0]
        for name, start_ts in module_starts.items():
            module_sections.append({
                'name': name, 'status': 'unclosed',
                'duration_s': round((last_ts - start_ts).total_seconds(), 1),
                'detail': 'no matching Completed/failed line found (still running, or log truncated)',
            })

        gaps = []
        for (t1, m1), (t2, m2) in zip(entries, entries[1:]):
            gaps.append({'from': m1, 'to': m2, 'duration_s': round((t2 - t1).total_seconds(), 1)})
        gaps.sort(key=lambda g: g['duration_s'], reverse=True)

        total_span_s = (entries[-1][0] - entries[0][0]).total_seconds()

        return {
            'total_span_s': round(total_span_s, 1),
            'module_sections': module_sections,
            'slow_gaps': gaps[:LABSTARTUP_LOG_TOP_GAPS],
            'notable_messages': notable[-LABSTARTUP_LOG_TOP_NOTABLE:],
            'notable_messages_truncated': len(notable) > LABSTARTUP_LOG_TOP_NOTABLE,
        }
    except Exception:
        return None


def write_labstartup_log_summary(lsf):
    """
    Log a human-readable summary of ~/hol/labstartup.log via
    lsf.write_output -- see summarize_labstartup_log()'s docstring for
    what this covers. Returns the same dict summarize_labstartup_log()
    returns (or None), so main() can also fold a compact version into the
    uploaded telemetry JSON.

    Non-fatal: any failure here is logged as a warning and never calls
    lsf.labfail. This step is registered with track_step(...,
    scan_for_warnings=False) in main() -- it deliberately reproduces
    WARNING/ERROR/etc. substrings from elsewhere in the log as DATA, which
    would otherwise falsely mark this step itself 'degraded' on every run
    that has any notable message at all.
    """
    lsf.write_output('Summarizing ~/hol/labstartup.log...')
    try:
        summary = summarize_labstartup_log(lsf)
        if summary is None:
            lsf.write_output(
                f'  {LABSTARTUP_LOG_FILE}: not found, unreadable, too large, '
                f'or no parseable timestamped lines -- skipping'
            )
            return None

        hours, rem = divmod(summary['total_span_s'], 3600)
        minutes, seconds = divmod(rem, 60)
        lsf.write_output(
            f"  Total span: {int(hours)}h{int(minutes)}m{int(seconds)}s "
            f"({summary['total_span_s']}s)"
        )

        sections = summary['module_sections']
        if sections:
            lsf.write_output(f'  Startup module sections (in order, {len(sections)} found):')
            for sec in sections:
                detail_note = f" -- {sec['detail']}" if sec['detail'] else ''
                lsf.write_output(f"    {sec['name']}: {sec['status']} in {sec['duration_s']}s{detail_note}")
        else:
            lsf.write_output(
                "  No 'Starting module:'/'Completed module:' sections found -- either "
                "this ran outside the normal labstartup.py sequence, or the log doesn't "
                "cover a full run."
            )

        lsf.write_output(f"  Other slow gaps between log lines, not tied to a named module (top {len(summary['slow_gaps'])}):")
        for gap in summary['slow_gaps']:
            lsf.write_output(f"    {gap['duration_s']}s -- \"{gap['from'][:80]}\" -> \"{gap['to'][:80]}\"")

        notable = summary['notable_messages']
        if notable:
            trunc_note = ' (showing most recent -- log has more)' if summary['notable_messages_truncated'] else ''
            lsf.write_output(f'  Notable messages{trunc_note}:')
            for msg in notable:
                lsf.write_output(f'    {msg[:200]}')
        else:
            lsf.write_output('  No notable (WARNING/ERROR/FAIL/etc) messages found.')

        return summary
    except Exception as e:
        lsf.write_output(f'  WARNING: could not summarize labstartup.log (non-fatal): {e}')
        return None


def send_telemetry_summary(lsf, results, run_started_at_iso, total_duration_s, labstartup_log_summary=None):
    """
    Best-effort upload of this run's timing/health summary as one JSON
    object per run to a GCS bucket (no Sheets/Drive/Apps Script/BigQuery
    involved -- see the telemetry constants block above main() for why).
    This must NEVER affect lab pass/fail or block/slow down startup beyond
    a short, bounded timeout: every failure mode -- unconfigured bucket,
    vault/key problems, DNS/network failure, timeout, a non-2xx response,
    even a bug in this function itself -- is caught here and only logged
    as a warning. lsf.labfail is never called from this function, on
    purpose. summarize_telemetry.py (run locally, not on any pod) is what
    later reads all these objects back and renders a viewable summary.
    """
    if not TELEMETRY_GCS_BUCKET:
        lsf.write_output('  Telemetry: TELEMETRY_GCS_BUCKET not configured -- skipping upload')
        return

    try:
        import datetime
        import json
        import requests

        if any(r['status'] == 'failed' for r in results):
            overall_status = 'failed'
        elif any(r['status'] == 'degraded' for r in results):
            overall_status = 'degraded'
        else:
            overall_status = 'ok'

        access_token = _get_telemetry_access_token()
        if not access_token:
            lsf.write_output(
                '  WARNING: telemetry upload skipped -- could not obtain an '
                'access token (vault missing, decrypt failed, credential '
                'invalid, or token exchange failed)'
            )
            return
        headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}

        pod_id = _get_or_create_pod_id()
        finished_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        summary = {
            'received_at': finished_at_iso,
            'pod_id': pod_id,
            'started_at': run_started_at_iso,
            'finished_at': finished_at_iso,
            'total_duration_s': round(total_duration_s, 1),
            'overall_status': overall_status,
            'steps': results,
            'labstartup_log': labstartup_log_summary,
        }
        # One object per run, grouped by pod under runs/ -- object names
        # can't collide across concurrent pods (pod_id) or across repeated
        # runs of the same pod (timestamp has second resolution and this
        # only runs once per boot anyway).
        object_name = f'runs/{pod_id}/{finished_at_iso}.json'
        resp = requests.post(
            f'https://storage.googleapis.com/upload/storage/v1/b/{TELEMETRY_GCS_BUCKET}/o',
            headers=headers, timeout=15,
            params={'uploadType': 'media', 'name': object_name},
            data=json.dumps(summary).encode(),
        )
        resp.raise_for_status()
        lsf.write_output(f'  Telemetry: uploaded run summary (HTTP {resp.status_code}, overall={overall_status})')
    except Exception as e:
        lsf.write_output(f'  WARNING: telemetry upload failed (non-fatal, lab startup unaffected): {e}')


def main():
    # install forgotten pip package
    import subprocess
    import sys
    sys.path.append('/hol')
    #sys.path.append('/home/holuser/py312venv/lib')
    import lsfunctions as lsf
    import os
    import requests
    import json
  
    os.umask(0o0000)  ## lsfunctions sets a umask without execute, need to override or script-running things will die
    password_file = "/home/holuser/creds.txt"
    os.environ["http_proxy"] = "http://proxy:3128"
    os.environ["https_proxy"] = "http://proxy:3128"
    os.environ["no_proxy"] = "localhost,127.0.0.0/8,::1,site-a.vcf.lab,10.1.1.90,10.0.0.0/8"
    os.environ["HTTP_PROXY"] = "http://proxy:3128"
    os.environ["HTTPS_PROXY"] = "http://proxy:3128"
    os.environ["NO_PROXY"] = "localhost,127.0.0.0/8,::1,site-a.vcf.lab,10.1.1.90,10.0.0.0/8"  
    os.environ["AVICTRL_PASSWORD"] = open(password_file, 'r').read().strip("\n")
    os.environ["TF_VAR_nsxt_password"] = open(password_file, 'r').read().strip("\n")

    # One-time settling buffer before the VKS scale-out request below (added
    # 2026-07-25). On a fresh/busy pod boot, NSX Manager can still be working
    # through a backlog from bringing up the rest of the infrastructure right
    # as adjustomatic starts -- hitting it immediately with a new scale-out
    # (which needs fresh SubnetPort realization per new node) risks landing
    # in the worst possible window. Confirmed once already: a scale-out that
    # hit this exact situation left 4 worker VMs permanently stuck
    # (SubnetPortReady=True but kubelet never registered) and cycling through
    # CAPI's MachineHealthCheck all night without ever succeeding. This delay
    # is a simple mitigation for that one risky window, not a real readiness
    # check -- fine since scale-out to 3 workers is only needed once, to be
    # captured into the saved vApp template; ok to remove once that's done.
    import time as _time
    import datetime as _datetime

    # Telemetry: overall run clock + per-step results list, both started
    # here (before the settling sleep below) so the reported total
    # duration covers the whole run, not just the part after it. See
    # track_step()/send_telemetry_summary() above main().
    _run_start = _time.time()
    _run_started_at_iso = _datetime.datetime.now(_datetime.timezone.utc).isoformat()
    _telemetry_results = []

    # CRITICAL: lsf.labfail() calls sys.exit(1) (confirmed against the real
    # HOLFY27-MGR-HOLUSER source, 2026-07-29 -- see CLAUDE.md) -- it does NOT
    # return control to the caller. _labfail_uploads_telemetry() wraps
    # lsf.labfail for the rest of this function so a mid-run failure still
    # uploads whatever telemetry was collected before the process exits --
    # see that context manager's docstring for why this is essential (without
    # it, the exact runs where telemetry matters most -- a real failure --
    # would silently produce zero telemetry) and why it must be restored
    # afterward (lsfunctions is a shared, process-wide module).
    with _labfail_uploads_telemetry(lsf, _telemetry_results, _run_started_at_iso, _run_start):
        # TEMPORARILY DISABLED (2026-08-02): VKS worker node pool scale-out
        # (1 -> 3) is now baked into the saved template itself, so this
        # one-time provisioning step is no longer needed on boot. The
        # 180s settling_sleep existed solely to protect this scale-out
        # request from a risky NSX-realization timing window (see its
        # original comment) -- with the scale-out gone, it has no purpose,
        # so it's disabled along with it. The matching
        # wait_for_vks_nodepool_scaleup() confirm step at the end of
        # main() is also disabled below, since it depends on
        # _vks_scale_start being set here.
        # with track_step(lsf, _telemetry_results, 'settling_sleep'):
        #     _time.sleep(180)
        #
        # # Kick off VKS worker node pool scale-up (1 -> 3) right away (after the
        # # settling buffer above), before anything else in this script. This just
        # # issues the Supervisor Cluster patch and returns immediately -- the
        # # actual node provisioning happens in the background over the next
        # # several minutes, in parallel with everything else adjustomatic does
        # # below. wait_for_vks_nodepool_scaleup() at the very end of main()
        # # confirms it actually finished (and fails the lab if it doesn't) before
        # # returning control to the rest of startup.
        # _vks_scale_start = _time.time()
        # with track_step(lsf, _telemetry_results, 'vks_scale_request'):
        #     scale_vks_worker_nodepools(lsf, target_replicas=3)

        # VSP vmsp-platform kube-vip DaemonSet hardening (fleet-01a/vmsp-gateway
        # VIP flap fix). Independent of the Avi playbooks below; run first so a
        # flapping gateway VIP doesn't intermittently affect anything later in
        # this script that happens to depend on fleet/depot reachability.
        # See fix_vmsp_gateway_kubevip() docstring for full root-cause detail.
        with track_step(lsf, _telemetry_results, 'vmsp_kubevip_hardening'):
            fix_vmsp_gateway_kubevip(lsf)

        # VCFA's own control-plane kube-vip (a different instance from the
        # vmsp-platform one above) crash-loops forever if its own
        # stabilizer script's lease/renew timing hardening leaves an
        # invalid leaseDuration/renewDeadline combination in place -- see
        # fix_vcfa_kube_vip_lease_invariant() docstring for the 2026-07-30
        # incident this guards against.
        with track_step(lsf, _telemetry_results, 'vcfa_kubevip_lease_invariant'):
            fix_vcfa_kube_vip_lease_invariant(lsf)

        # AKO pod health inside each VKS workload guest cluster (avi-system
        # namespace) -- distinct from the Supervisor-side vmware-system-ako
        # check above (currently disabled). See
        # ensure_workload_cluster_ako_healthy()'s docstring for the
        # 2026-08-11 workload-cluster-1 CreateContainerConfigError incident
        # this guards against.
        with track_step(lsf, _telemetry_results, 'workload_cluster_ako_health'):
            ensure_workload_cluster_ako_healthy(lsf)

        # cert-manager vault-issuer CA trust (Supervisor namespace
        # VKS_SUPERVISOR_NS) -- see fix_vault_issuer_ca_trust()'s docstring
        # for the 2026-08-23 x509-unknown-authority incident this guards
        # against. Independent of everything else here; run alongside the
        # other Supervisor/VKS-namespace checks above.
        with track_step(lsf, _telemetry_results, 'vault_issuer_ca_trust'):
            fix_vault_issuer_ca_trust(lsf)

        # Vault pki/roles/holodeck allowed_domains -- see
        # ensure_vault_holodeck_role_domains()'s docstring. Run right after
        # the CA trust fix above since both gate the same vault-issuer /
        # pki/sign/holodeck cert-issuance path.
        with track_step(lsf, _telemetry_results, 'vault_holodeck_role_domains'):
            ensure_vault_holodeck_role_domains(lsf)

        # Console Firefox Remote Settings proxy-bypass fix (identity-panel /
        # page-load hang). Independent of everything else here; see
        # fix_firefox_remote_settings_bypass() docstring for full root-cause
        # detail -- and see fix_firefox_remote_settings_dns_block() for why
        # that fix alone isn't sufficient and what actually closes the gap.
        with track_step(lsf, _telemetry_results, 'firefox_remote_settings_fix'):
            fix_firefox_remote_settings_bypass(lsf)
            fix_firefox_remote_settings_dns_block(lsf)

        # global.vcf.lab GSLB NS delegation -- keeps the NS/glue records pointed
        # at the pod's actual Avi GSLB DNS VS hostnames/IPs. See
        # fix_global_dns_ns_delegation() and GLOBAL_DNS_NS_DELEGATIONS' comment
        # for full root-cause detail.
        with track_step(lsf, _telemetry_results, 'global_dns_ns_delegation'):
            fix_global_dns_ns_delegation(lsf)

        # NSX-T LB app profiles (custom-fast-tcp/custom-fast-udp) + HTTP
        # monitor (http-30001). See configure_nsxt_app_profiles() docstring --
        # this used to be inline here, PUT unconditionally, and errored out on
        # every run after the pod's first, since these objects persist across
        # the pod's lifetime. Now idempotent (GET-before-PUT), safe to run
        # every time.
        with track_step(lsf, _telemetry_results, 'nsxt_app_profiles'):
            configure_nsxt_app_profiles(lsf)

        # Disconnected-mode License Hub upload -- see
        # upload_license_hub_disconnected_license() docstring. Independent
        # of the NSX-ALB credential steps below; run here (right after the
        # other direct-API NSX/Avi step above) so a licensing failure is
        # caught early rather than after several minutes of unrelated work.
        with track_step(lsf, _telemetry_results, 'license_hub_upload'):
            upload_license_hub_disconnected_license(lsf)

        # TEMPORARILY DISABLED (2026-08-07): confirmed live on the test pod
        # that uploading a replacement license to License Hub updates it
        # in place -- already-assigned endpoints stay mapped to it without
        # needing onboarding to be (re-)run or an explicit endpoint<->
        # license mapping swap. Neither of these has actually been
        # exercised by a real adjustomatic.py run yet -- the one real run
        # hard-failed at the upload step itself (transient 401, since
        # fixed with retry) before either got a chance to execute. Leaving
        # onboard_license_hub_endpoints() / resync_license_hub_endpoint_
        # mappings() defined but unused until both are validated on
        # another pod (e.g. one where endpoints aren't already onboarded,
        # or where the in-place-update behavior doesn't hold).
        # with track_step(lsf, _telemetry_results, 'license_hub_endpoint_onboarding'):
        #     onboard_license_hub_endpoints(lsf)
        # with track_step(lsf, _telemetry_results, 'license_hub_mapping_resync'):
        #     resync_license_hub_endpoint_mappings(lsf)

        # NSX-ALB credential/lockout durability -- see resync_nsxt_alb_*()
        # docstrings. This vApp is a saved/suspended VCD template that can sit
        # powered off for up to ~18 months (or more) between power-ons, so
        # anything with a calendar-based expiration needs re-checking (and,
        # if needed, re-extending) on every single boot rather than trusting
        # a one-time manual fix to hold. Both functions are fast in the common
        # (already-healthy) case; only resync_nsxt_alb_cloud_connector_credentials
        # pays a slower (~1min) cost, and only on the rare boot where it finds
        # an actual broken credential to rotate.
        # TEMPORARILY DISABLED (2026-08-02): both of these mutate the
        # NSX<->Avi trust relationship on every single boot -- confirmed via
        # the adjustomatic-disabled-corruption-test experiment to be the
        # actual source of the NSX-Avi TLS cert-chain corruption baked into
        # recent templates (resync_nsxt_alb_enforcement_point_tokens()
        # wholesale-PATCHes NSX's alb-endpoint EnforcementPoint
        # connection_info; resync_nsxt_alb_cloud_connector_credentials()
        # rotates the NSX service-account password and pushes it into Avi's
        # cloud connector). Leave disabled until the underlying wholesale-
        # PATCH/rotation hazard in those two functions is actually fixed --
        # do not just uncomment these without a real fix first.
        # with track_step(lsf, _telemetry_results, 'nsxt_alb_enforcement_point_tokens'):
        #     resync_nsxt_alb_enforcement_point_tokens(lsf)
        # with track_step(lsf, _telemetry_results, 'nsxt_alb_cloud_connector_credentials'):
        #     resync_nsxt_alb_cloud_connector_credentials(lsf)
        # TEMPORARILY DISABLED (2026-08-07): root-caused live that this
        # function's remediation (restart nsx-ncp / netop-controller-manager
        # / AKO pods) cannot fix the failure actually occurring right now --
        # nsx-ncp's own AviSecretController is healthy and already retrying
        # on its own built-in schedule, but every attempt fails with "Failed
        # to get Avi auth token ... No route to host" / "502 BAD_GATEWAY"
        # hitting the Avi Controller (10.1.1.90) from NSX Manager. That's a
        # real network/gateway problem between NSX and Avi, not a missing-
        # secret or stale-object condition -- no amount of restarting AKO,
        # netop-controller-manager, or nsx-ncp itself addresses it, so this
        # function's checks were correctly reporting "not Ready" every run
        # while its fix loop just added restart churn on top of a problem
        # it can't touch. Leaving ensure_ako_avi_secret_healthy() defined
        # for when it once again matches an actual secret-chain failure
        # (its original 2026-07-30 target) rather than this network issue.
        # with track_step(lsf, _telemetry_results, 'ako_avi_secret_health'):
        #     ensure_ako_avi_secret_healthy(lsf)
        with track_step(lsf, _telemetry_results, 'sso_password_policy'):
            resync_sso_password_policy(lsf)

        # Pre-populate the Automation lab module's VCF Automation blueprint
        # catalog (org acme-east-a / project default-project) so students
        # don't have to paste each one in through the UI. See
        # install_vcfa_blueprints.py's module docstring for the VCD-style
        # cloudapi/sessions login this uses -- non-fatal, logs and skips on
        # any failure rather than failing the lab.
        with track_step(lsf, _telemetry_results, 'vcfa_blueprint_install'):
            import install_vcfa_blueprints
            install_vcfa_blueprints.install_vcfa_blueprints(lsf)

        # Storage-class quota bump for the Automation lab's Supervisor
        # namespace -- must run after the blueprint install above so a
        # fresh blueprint deployment doesn't land in a namespace still at
        # the un-bumped default quota. See
        # install_vcfa_blueprints.patch_supervisor_namespace_storage_quota()'s
        # docstring for the CCI merge-patch detail -- non-fatal, logs and
        # skips on any failure rather than failing the lab.
        with track_step(lsf, _telemetry_results, 'vcfa_namespace_storage_quota'):
            install_vcfa_blueprints.patch_supervisor_namespace_storage_quota(lsf)

        # VCF Automation external IP address space configuration -- add
        # required CIDR blocks for workload domain external IP pool.
        # Non-fatal; logs and skips on any failure rather than failing the lab.
        with track_step(lsf, _telemetry_results, 'vcfa_ipspace_cidrs'):
            lsf.write_output('Configuring VCF Automation IP address spaces')
            add_cidr_to_ipspace(lsf, 'ipspace-wld-a', '10.150.6.0/23')

        # Harbor Supervisor Service health + stuck-pod self-remediation --
        # see ensure_harbor_healthy()'s docstring for the 2026-08-20
        # incident this guards against. Run last, same reasoning as the VKS
        # scale-up wait above: Harbor's pods have had the full runtime of
        # everything else in this function to converge on their own first,
        # so anything still stuck by now is a real candidate for the
        # delete-and-recreate remediation rather than a normal cold start.
        # FATAL unlike most steps here -- see docstring.
        with track_step(lsf, _telemetry_results, 'harbor_health'):
            ensure_harbor_healthy(lsf)

        # try:
        #     lsf.write_output("Running first stages playbook")
        #     # Playbook to run final config steps
        #     result = subprocess.run(["/usr/bin/ansible-playbook", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/labconfig_firststage.yaml", 
        #         "-i", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/inventory.yml", "--vault-password-file", 
        #         "/home/holuser/vaultsecret.txt"], capture_output=True, text=True, check=True)
        #     lsf.write_output(result)
        #     try:
        #         lsf.write_output(result.stdout)
        #     except:
        #         pass
        # except Exception as e:
        #     lsf.write_output(e)
        #     try:
        #         lsf.write_output(e.stdout)
        #         lsf.write_output(e.stderr)
        #     except:
        #         pass   
        #     lsf.write_output('Adjustomatic failed at avitweaker - first stage playbook step') 
        #     lsf.labfail('Adjustomatic failed at avitweaker - first stage playbook step')

        # The two "avi configuration playbook" steps (workload-domain and
        # mgmt-domain avi_config_*.yml) that used to run here were moved to
        # VCF.py's CUSTOM section (tail of main()), replacing the Supervisor-
        # unpause shim that used to live there. See VCF.py's changelog for
        # the date/reason.

        with track_step(lsf, _telemetry_results, 'final_stage_playbook', scan_for_warnings=False):
            try:
                lsf.write_output("Running final stages playbook")
                # Playbook to run final config steps
                result = subprocess.run(["/usr/bin/ansible-playbook", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/labconfig_finalstage.yaml",
                    "-i", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/inventory.yml", "--vault-password-file",
                    "/home/holuser/vaultsecret.txt"], capture_output=True, text=True, check=True)
                # Playbook already succeeded at this point - don't let a transient I/O error while
                # logging its output turn a successful run into a lab failure.
                try:
                    retry_io(lsf.write_output, result, console=False)
                    retry_io(lsf.write_output, result.stdout, console=False)
                except OSError as log_err:
                    try:
                        lsf.write_output(f"final stage playbook succeeded, but logging its output failed: {log_err}")
                    except OSError:
                        pass
            except Exception as e:
                lsf.write_output(e)
                try:
                    lsf.write_output(e.stdout)
                    lsf.write_output(e.stderr)
                except:
                    pass
                lsf.write_output('Adjustomatic failed at avitweaker - final stage playbook step')
                lsf.labfail('Adjustomatic failed at avitweaker - final stage playbook step')

        # TEMPORARILY DISABLED (2026-08-02): paired with the scale-out
        # request disabled at the top of this function -- the worker node
        # pool is now provisioned at target replica count in the saved
        # template itself, and this step also depends on _vks_scale_start,
        # which is no longer set. Confirm the VKS worker node pool scale-up
        # kicked off at the very top of this function actually finished
        # (fails the lab if it didn't) -- run last so it's had the full
        # runtime of everything above to converge in the background first.
        # with track_step(lsf, _telemetry_results, 'vks_nodepool_scaleup_wait'):
        #     wait_for_vks_nodepool_scaleup(lsf, _vks_scale_start, target_replicas=3, timeout_seconds=600)



        # Summarize ~/hol/labstartup.log (the pod's overall boot-time log,
        # not just this script's own output) -- see
        # write_labstartup_log_summary()'s docstring. scan_for_warnings=False
        # for the same reason the ansible-playbook step above uses it:
        # this step deliberately reproduces WARNING/ERROR/etc. substrings
        # from elsewhere in the log as data, which would otherwise falsely
        # mark this step itself 'degraded' on every run that has any notable
        # message at all.
        _labstartup_log_summary = None
        with track_step(lsf, _telemetry_results, 'labstartup_log_summary', scan_for_warnings=False):
            _labstartup_log_summary = write_labstartup_log_summary(lsf)

        # Telemetry upload -- always last, always best-effort. See
        # send_telemetry_summary()'s docstring: no failure mode here can fail
        # the lab or affect anything above: the try/except here is deliberate
        # defense-in-depth on top of that function's own internal try/except,
        # specifically so that even an unanticipated bug in the telemetry code
        # itself still can't take startup down with it.
        try:
            send_telemetry_summary(
                lsf, _telemetry_results, _run_started_at_iso, _time.time() - _run_start,
                labstartup_log_summary=_labstartup_log_summary,
            )
        except Exception as _telemetry_err:
            try:
                lsf.write_output(f'  WARNING: telemetry summary step itself raised (non-fatal, lab startup unaffected): {_telemetry_err}')
            except Exception:
                pass

    # try:
    #     stdout = subprocess.run(["/usr/bin/rm", "-rf", "/home/holuser/vaultsecret.txt"], text=True, check=True)
    #     lsf.write_output(result)
    #     try:
    #         lsf.write_output(result.stdout)
    #     except:
    #         pass
    # except Exception as e:
    #     lsf.write_output(e)
    #     lsf.labfail('Adjustomatic failed at secret deletion')
    #     try:
    #         lsf.write_output(e.stderr)
    #     except:
    #         pass

if __name__ == "__main__":
    main()