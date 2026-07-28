# Avi config playbook (reconciled 2026-07-27)

Self-documenting Ansible playbooks for both Avi Controllers on this HOL-2740
pod. Started as a live-config snapshot (a passive reference of "what's
actually configured"), then reconciled task-by-task against the intent in
`../avi_config.yml`/`../avi_tweaks.yml`. **These are now meant to run every
time the pod starts**, same as the original template — not just documentation.

## Files

| File | Covers |
|---|---|
| `inv_wld_a.yml` | Vars for the workload-domain Avi (`alb-a.site-a.vcf.lab`, cloud `Cloud-nsx-wld01-a`) |
| `avi_config_wld_a.yml` | Controller properties, backup config, VIP network, IPAM/DNS profiles, SE groups (`Default-Group`, `gslb01a`), app profiles (`System-Secure-HTTP`, `System-HTTP`, `System-DNS`), SSL profile, certs, `demo_vs` + `dns-vs-01a` (pool/VsVip/VS), DNS VS registration |
| `inv_mgmt_a.yml` | Vars for the management-domain Avi (`alb-b.site-a.vcf.lab`, cluster `mgmt-avi`, cloud `Cloud-nsx-mgmt-a`) |
| `avi_config_mgmt_a.yml` | Same categories, scoped to what actually exists there: no `demo_vs`/pool, SE groups `Default-Group` + `gslb01b`, `dns-vs-01b` |

Every task uses a `vmware.alb` collection module (`avi_cloud`,
`avi_controllerproperties`, `avi_network`, `avi_ipamdnsproviderprofile`,
`avi_serviceenginegroup`, `avi_applicationprofile`, `avi_sslprofile`,
`avi_sslkeyandcertificate`, `avi_pool`, `avi_vsvip`, `avi_virtualservice`,
`avi_backupconfiguration`, `avi_systemconfiguration`) — no raw
`uri`/`avi_api_session` REST calls except the pre-login controller-readiness
poll, which is the same pattern the original `avi_config.yml` uses (there's
no dedicated module for an unauthenticated health check).

## Reconciliation: what changed from the original snapshot, and why

Comparing the raw snapshot against `avi_config.yml`/`avi_tweaks.yml` surfaced
several places where live state had drifted from template intent, plus one
whole SE group pair (`gslb01a`/`gslb01b`) the original template never covered
at all. Each was resolved deliberately, not defaulted to "whatever's live":

**Template intent restored (was missing/wrong live, now enforced every run):**
- Controller properties: `shared_ssl_certificates: true`, `api_idle_timeout: 1440` — was `false`/`15` on both controllers.
- True-Client-IP / HSTS tweak on `System-Secure-HTTP` and `System-HTTP` — was never applied on either controller.
- `self_se_election: true` on **all four** SE groups (`Default-Group` on both clouds, plus `gslb01a`/`gslb01b`) — was `false` everywhere.
- `dns_virtualservice_refs` registration on `systemconfiguration` — was set for `alb-a`→`dns-vs-01a` already (on the fixed pod), but never set for `alb-b`→`dns-vs-01b`. Now an explicit, scoped task on both.
- `demo_vs` (workload domain only) and `dns-vs-01a`/`dns-vs-01b` `analytics_policy` flags (`all_headers`, `full_client_logs.enabled`, `metrics_realtime_update.enabled`) — brought back in line with template values.

**Live/current value deliberately preserved over template's stale intent:**
- SE group memory/disk/`max_vs_per_se`/`se_deprovision_delay` sizing — the template's numbers (8GB/25GB/200 VSes) are stale; the pod runs smaller Default-Groups (2GB/15GB/100) plus dedicated `gslb01a`/`gslb01b` groups already correctly sized at 8GB. Only `self_se_election` was changed on any of them.
- `state_based_dns_registration: true` on both clouds — template wants `false`, but this is left as-is (not touched by the `avi_cloud` task at all).
- The portal's SSL certificate (`portal_configuration.sslkeyandcertificate_refs`, `secure_channel_configuration`) — **must not be overwritten**. On this VCF 9.1+ build, the portal cert is provisioned by VCF itself; the original template's certificate-creation flow predates that and no longer applies. The `dns_virtualservice_refs` task is deliberately scoped to that one field only, and there's no active `avi_systemconfiguration` task that touches `portal_configuration`.
- Backup configuration (SFTP to SDDC Manager's NFS share) — explicitly left untouched.
- `ProxyProtocol` L4 app profile — the original template creates this unconditionally but nothing in the live config references it by name; explicitly not added back.

**Provenance note:** most of this was originally captured from the pod
reached via jump host `10.138.170.47`. The `System-DNS` fix, the
`gslb01a`/`gslb01b` discovery, and the `dns_virtualservice_refs` values all
came from `10.138.170.13` instead, since that's the pod where the manual
DNS-delegation fix was actually applied. If a value here ever looks off
against a specific pod, that jump-host split is the most likely reason.

## Deliberately out of scope

- **Licensing.** Both controllers run on a Broadcom subscription license
  applied automatically by VCF Operations, not the static serial-key flow the
  original template uses. Reapplying a serial here would be wrong.
- **System-level bootstrap** (DNS/NTP resolvers, `welcome_workflow_complete`,
  `sddcmanager_fqdn`) and the **portal SSL cert** — see above.
- **Supervisor/VKS-managed LB objects** on the workload-domain cloud (pools,
  VsVips, VirtualServices named after content hashes, e.g.
  `5dcb84b1-dbd--kube-system-kube-apiserver-lb-svc...`). These are
  created/deleted automatically by the Supervisor cluster's own NSX/Avi
  integration — managing them here would fight the platform. They're listed
  in a comment at the bottom of `avi_config_wld_a.yml` for reference.
- **Cert private key material.** Avi never returns private keys via the API
  (`"<sensitive>"` on every read). The `sslkeyandcertificate` tasks read the
  vault-created wildcard cert/key and the root CA cert via `/lmchol/...`
  paths, because `adjustomatic.py` (which runs these playbooks) executes on
  the lab **manager**, not the jump host/console — `/lmchol` there is a bind
  mount of the console VM's real filesystem. (An earlier revision of this
  README claimed the opposite — that these ran on the console directly and
  `/lmchol` never applied — that was wrong; corrected after actually running
  `adjustomatic.py` end to end and watching the cert task fail without the
  prefix.) If those files are gone, this playbook can't recreate the certs
  from what's on the controller alone.

## Usage

```bash
cd avi_configs/live_config_snapshot
ansible-playbook --syntax-check avi_config_wld_a.yml   # validated against vmware.alb 32.1.1 on the jump host
ansible-playbook avi_config_wld_a.yml                  # run for real, from the jump host or lab manager
ansible-playbook avi_config_mgmt_a.yml
```

Both were syntax-checked against the `vmware.alb` collection already
installed on the jump host (`holuser@<jumpbox>`), which is where they're
expected to run from. They have not yet been run end-to-end against the live
controllers — given the intent is now every-boot automation, that first real
run deserves a careful watch, especially the controller-properties and
SE-group changes.
