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

            patch_body = {
                '_revision': ep['_revision'],
                'connection_info': {
                    'resource_type': 'AviConnectionInfo',
                    'enforcement_point_address': d['avi_ip'],
                    'username': 'nsxt-alb',
                    'password': token_resp['token'],
                    'expires_at': token_resp['expires_at'],
                    'tenant': 'admin',
                },
            }
            patch_result = requests.patch(
                ep_url, auth=('admin', admin_password), verify=False, timeout=15, json=patch_body,
            )
            lsf.write_output(
                f"  {d['domain']}: enforcement-point token refreshed, new expiry {token_resp['expires_at']} "
                f"(PATCH {patch_result.status_code})"
            )

        except Exception as e:
            lsf.write_output(f"  WARNING: could not resync {d['domain']} enforcement-point token: {e}")


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
    _time.sleep(180)

    # Kick off VKS worker node pool scale-up (1 -> 3) right away (after the
    # settling buffer above), before anything else in this script. This just
    # issues the Supervisor Cluster patch and returns immediately -- the
    # actual node provisioning happens in the background over the next
    # several minutes, in parallel with everything else adjustomatic does
    # below. wait_for_vks_nodepool_scaleup() at the very end of main()
    # confirms it actually finished (and fails the lab if it doesn't) before
    # returning control to the rest of startup.
    _vks_scale_start = _time.time()
    scale_vks_worker_nodepools(lsf, target_replicas=3)

    # VSP vmsp-platform kube-vip DaemonSet hardening (fleet-01a/vmsp-gateway
    # VIP flap fix). Independent of the Avi playbooks below; run first so a
    # flapping gateway VIP doesn't intermittently affect anything later in
    # this script that happens to depend on fleet/depot reachability.
    # See fix_vmsp_gateway_kubevip() docstring for full root-cause detail.
    fix_vmsp_gateway_kubevip(lsf)

    # Console Firefox Remote Settings proxy-bypass fix (identity-panel /
    # page-load hang). Independent of everything else here; see
    # fix_firefox_remote_settings_bypass() docstring for full root-cause
    # detail -- and see fix_firefox_remote_settings_dns_block() for why
    # that fix alone isn't sufficient and what actually closes the gap.
    fix_firefox_remote_settings_bypass(lsf)
    fix_firefox_remote_settings_dns_block(lsf)

    # global.vcf.lab GSLB NS delegation -- keeps the NS/glue records pointed
    # at the pod's actual Avi GSLB DNS VS hostnames/IPs. See
    # fix_global_dns_ns_delegation() and GLOBAL_DNS_NS_DELEGATIONS' comment
    # for full root-cause detail.
    fix_global_dns_ns_delegation(lsf)

    # NSX-T LB app profiles (custom-fast-tcp/custom-fast-udp) + HTTP
    # monitor (http-30001). See configure_nsxt_app_profiles() docstring --
    # this used to be inline here, PUT unconditionally, and errored out on
    # every run after the pod's first, since these objects persist across
    # the pod's lifetime. Now idempotent (GET-before-PUT), safe to run
    # every time.
    configure_nsxt_app_profiles(lsf)

    # NSX-ALB credential/lockout durability -- see resync_nsxt_alb_*()
    # docstrings. This vApp is a saved/suspended VCD template that can sit
    # powered off for up to ~18 months (or more) between power-ons, so
    # anything with a calendar-based expiration needs re-checking (and,
    # if needed, re-extending) on every single boot rather than trusting
    # a one-time manual fix to hold. Both functions are fast in the common
    # (already-healthy) case; only resync_nsxt_alb_cloud_connector_credentials
    # pays a slower (~1min) cost, and only on the rare boot where it finds
    # an actual broken credential to rotate.
    resync_nsxt_alb_enforcement_point_tokens(lsf)
    resync_nsxt_alb_cloud_connector_credentials(lsf)
    resync_sso_password_policy(lsf)

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

    try:
        lsf.write_output("Running avi configuration playbook - workload domain")
        result = subprocess.run(["/usr/bin/ansible-playbook", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/avi_configs/fy27-updates/avi_config_wld_a.yml",
            "-i", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/avi_configs/fy27-updates/inv_wld_a.yml", "--vault-password-file",
            "/home/holuser/vaultsecret.txt"], capture_output=True, text=True, check=True)
        # Playbook already succeeded at this point - don't let a transient I/O error while
        # logging its output turn a successful run into a lab failure.
        try:
            retry_io(lsf.write_output, result, console=False)
            retry_io(lsf.write_output, result.stdout, console=False)
        except OSError as log_err:
            try:
                lsf.write_output(f"avi workload-domain configuration succeeded, but logging its output failed: {log_err}")
            except OSError:
                pass
    except Exception as e:
        lsf.write_output(e)
        try:
            lsf.write_output(e.stdout)
            lsf.write_output(e.stderr)
        except:
            pass
        lsf.write_output('Adjustomatic failed at avitweaker - avi workload-domain configuration step')
        lsf.labfail('Adjustomatic failed at avitweaker - avi workload-domain configuration step')

    try:
        lsf.write_output("Running avi configuration playbook - management domain")
        result = subprocess.run(["/usr/bin/ansible-playbook", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/avi_configs/fy27-updates/avi_config_mgmt_a.yml",
            "-i", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/avi_configs/fy27-updates/inv_mgmt_a.yml", "--vault-password-file",
            "/home/holuser/vaultsecret.txt"], capture_output=True, text=True, check=True)
        # Playbook already succeeded at this point - don't let a transient I/O error while
        # logging its output turn a successful run into a lab failure.
        try:
            retry_io(lsf.write_output, result, console=False)
            retry_io(lsf.write_output, result.stdout, console=False)
        except OSError as log_err:
            try:
                lsf.write_output(f"avi management-domain configuration succeeded, but logging its output failed: {log_err}")
            except OSError:
                pass
    except Exception as e:
        lsf.write_output(e)
        try:
            lsf.write_output(e.stdout)
            lsf.write_output(e.stderr)
        except:
            pass
        lsf.write_output('Adjustomatic failed at avitweaker - avi management-domain configuration step')
        lsf.labfail('Adjustomatic failed at avitweaker - avi management-domain configuration step')

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

    # Confirm the VKS worker node pool scale-up kicked off at the very top
    # of this function actually finished (fails the lab if it didn't) --
    # run last so it's had the full runtime of everything above to converge
    # in the background first.
    wait_for_vks_nodepool_scaleup(lsf, _vks_scale_start, target_replicas=3, timeout_seconds=600)

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