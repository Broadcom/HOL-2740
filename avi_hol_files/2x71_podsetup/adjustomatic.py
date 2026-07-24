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

    UPDATE: a one-shot patch alone does NOT hold. Verified empirically
    (2026-07-24): both the live DaemonSet and its HelmRelease reverted back
    to chart defaults within about 3 minutes of this patch running --
    something (most likely vmsp-operator regenerating the HelmRelease from
    its own source of truth, then Flux re-syncing the DaemonSet to match)
    keeps re-asserting the defaults, same as the drift Flux/vmsp-operator
    already causes on auto-platform-a's own vmsp-platform kube-vip (which
    is why vcfa-stabilizer.sh installs a 60s systemd drift-watcher there).
    We don't have write access to Tools/vsp-health/vsp-health-monitor.py to
    add this as a proper check on the manager VM the way that tool would
    normally handle ongoing drift protection, so instead this function also
    installs (idempotently) a holuser crontab entry that re-runs the same
    patch every minute via vmsp_kvip_keeper.py -- see
    install_vmsp_kvip_keeper_cron() below. Plain crontab, not systemd: same
    constraint vsp-health-monitor.py documents (holuser's sudoers cannot
    install systemd units) and the same reason that tool's own recurring
    schedule is a crontab entry too. It runs on the manager VM, not any VSP
    node, so it isn't lost when CAPI rolling-replaces a VSP control-plane
    node -- vmsp_kvip_keeper.py re-resolves the current CP IP every cycle.

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

# Durable channel: if this DaemonSet is Flux-managed, patch the
# HelmRelease values too so the next reconcile doesn't revert the
# live patch above back to chart defaults. No-op if not Flux-managed.
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

    # VSP vmsp-platform kube-vip DaemonSet hardening (fleet-01a/vmsp-gateway
    # VIP flap fix). Independent of the Avi playbooks below; run first so a
    # flapping gateway VIP doesn't intermittently affect anything later in
    # this script that happens to depend on fleet/depot reachability.
    # See fix_vmsp_gateway_kubevip() docstring for full root-cause detail.
    fix_vmsp_gateway_kubevip(lsf)

    # try:
    #     lsf.write_output("Configuring NSX T App profiles")   
    #     #add fast tcp and udp nsxt lb app profiles
    #     #prepare the http connection to NSX Manager
    #     print(os.environ)
    #     lsf.write_output(os.environ)
    #     session = requests.Session()
    #     session.verify = False
    #     session.auth = ('admin', os.environ['AVICTRL_PASSWORD'])
    #     nsx_mgr = 'https://nsx-wld01-a.site-a.vcf.lab'
    #     fast_tcp_data = {
    #         'display_name': 'custom-fast-tcp',
    #         'idle_timeout': '1700',
    #         'close_timeout': '8',
    #         'resource_type': 'LBFastTcpProfile'
    #         }
    #     fast_udp_data = {
    #         'display_name' : 'custom-fast-udp',
    #         'idle_timeout':  '330',
    #         'resource_type': 'LBFastUdpProfile'
    #         }
    #     hm_data = {
    #         'display_name' : 'http-30001',
    #         'resource_type' : 'LBHttpMonitorProfile',
    #         'monitor_port' : 30001
    #         }
    #     tcp_result = session.put(f"{nsx_mgr}/policy/api/v1/infra/lb-app-profiles/custom-fast-tcp", json=fast_tcp_data)
    #     lsf.write_output(f"Result code - {tcp_result.status_code}, Error text - {tcp_result.text}")
    #     udp_result = session.put(f"{nsx_mgr}/policy/api/v1/infra/lb-app-profiles/custom-fast-udp", json=fast_udp_data)
    #     lsf.write_output(f"Result code - {udp_result.status_code}, Error text - {udp_result.text}")
    #     mon_result = session.put(f"{nsx_mgr}/policy/api/v1/infra/lb-monitor-profiles/http-30001", json=hm_data)
    #     lsf.write_output(f"Result code - {mon_result.status_code}, Error text - {mon_result.text}")
      
    # except Exception as e:
    #     lsf.write_output(e)
    #     try:
    #         lsf.write_output(e.stdout)
    #         lsf.write_output(e.stderr)
    #     except:
    #         pass 
    #     lsf.write_output('Adjustomatic failed at nsxt app profile create')   
    #     #lsf.labfail('Adjustomatic failed at nsxt app profile create')

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

    # try:
    #     lsf.write_output("Running avi configuration playbook")   
    #     # Playbook to run final config steps
    #     result = subprocess.run(["/usr/bin/ansible-playbook", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/avi_configs/avi_config.yml", 
    #         "-i", "/vpodrepo/2027-labs/2740/avi_hol_files/2x71_podsetup/avi_configs/inv_sitea.yml", "--vault-password-file", 
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
    #     lsf.write_output('Adjustomatic failed at avitweaker - avi configuration step') 
    #     lsf.labfail('Adjustomatic failed at avitweaker - avi configuration step')

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