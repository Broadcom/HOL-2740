# NSX vDefend DFW Demo

Reconciles the "Antrea" NSX security policy that microsegments AviMart's east-west traffic. This is the live mechanism behind the vDefend part of the demo — see the four-act narrative in [`avimart/README.md`](avimart/README.md).

## Prerequisites

- `curl` and `python3` available on the host running the script
- Network access to the NSX Manager
- **The VKS cluster must already be registered with NSX Manager as an Antrea container cluster.** This is a one-time, per-cluster infrastructure step. Without it, this script's cluster-span step fails with `ERROR: no Antrea cluster matching '<name>' found`.

  Enabled via an `AddonConfig` applied on the **supervisor cluster**, in the vSphere Namespace that owns the target VKS cluster (not inside the workload cluster itself):

  ```yaml
  apiVersion: addons.kubernetes.vmware.com/v1alpha1
  kind: AddonConfig
  metadata:
    annotations:
      clusteraddon.addons.kubernetes.vmware.com/owned-for-deletion: "true"
    labels:
      addon.kubernetes.vmware.com/addon-name: antrea
      cluster.x-k8s.io/cluster-name: <cluster-name>
    name: <cluster-name>-antrea
    namespace: <vsphere-namespace>
  spec:
    clusterName: <cluster-name>
    values:
      antreaNSX:
        enable: true
  ```

  `<vsphere-namespace>` and `<cluster-name>` together are exactly the two halves of `CLUSTER_NAME` in this script (e.g. namespace `chrisblog-ns01-6wcrt` + cluster `kubernetes-cluster-fsrz` → `chrisblog-ns01-6wcrt-kubernetes-cluster-fsrz`) — the same string NSX exposes when it registers the cluster. Confirm it worked with `nsx-get-antrea.sh`: a non-empty `cluster-control-planes` response means NSX can see the cluster.

## Stages (`DFW_STAGE`)

| Stage | What's applied | Demo act |
|---|---|---|
| `none` | Empty rule set — nothing enforced, everything falls through to ambient allow | Act 0 — wide open |
| `full` (default) | Tier-based baseline (app→database, app→ai, app→app) + external Gateway ingress + the `vuln-svc→admin-svc` override | Act 2 — steady state / Restore |
| `lockdown` | Deny-all east-west only, no allow rules | Act 3 — shows the failure mode of over-rotating on zero trust |

`lockdown`'s deny-all rule scopes `avimart-ns → avimart-ns` — it blocks east-west only. Gateway/external ingress is unaffected, so the site shell and Avi WAF stay reachable even under lockdown.

## Usage

```bash
export NSX_PASSWORD='your-password'
DFW_STAGE=full ./nsx-demo-dfw.sh
```

Rule order matters: the `vuln-svc→admin-svc` DROP is sequenced before the broad tier-app→tier-app ALLOW (which would otherwise shadow it, since `admin-svc` and `vuln-svc` are both tagged `tier: app`).

## What it bootstraps

Every run ensures these exist (create-if-missing, never modified once created):

- 15 NSX Groups (one per K8s Service, plus `avimart-ns` and the 3 tier groups)
- 7 NSX Services (TCP port objects: `tcp-3000`, `tcp-3001`, `tcp-5000`, `tcp-5432`, `tcp-8000`, `tcp-8080`, `tcp-11434`)
- The container-cluster-span, pointed at the live cluster matching `CLUSTER_NAME` in the script (discovered at runtime, not hardcoded)

The `Antrea` SecurityPolicy itself and its rules are always fully reconciled (PUT) to match the requested `DFW_STAGE`, since rules need to pick up script changes, not just exist once.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NSX_PASSWORD` | *(required)* | Admin password |
| `NSX_USER` | `admin` | Admin username |
| `NSX_HOST` | `sfo-w01-nsx01.sfo.rainpole.io` | NSX Manager hostname or IP |
| `DFW_STAGE` | `full` | `full`, `lockdown`, or `none` — see above |
| `NSX_DEBUG` | `0` | Set to `1` to print raw API responses for troubleshooting |

## Companion scripts

- **`nsx-demo-dfw-reset.sh`** — deletes everything this script manages (the container-cluster-span, the `Antrea` policy, and all 15 groups). Nuclear option, for recovering from a corrupted demo state (e.g. a group with a stray empty condition that silently breaks matching) rather than reconciling against possibly-stale state.
- **`prepare-demo.sh`** — one-shot environment setup: prompts for Avi/NSX credentials, creates the `nsx-dfw-creds` K8s Secret the attack-lab app reads, and runs this script with `DFW_STAGE=none` plus `avi-demo-reset.sh`, resetting the whole demo to Act 0 (wide open). Must be `source`d, not executed. This is also what bootstraps groups/services/cluster-span on a brand-new cluster — see [`avimart/README.md`](avimart/README.md) for the full new-deployment checklist.
- **`nsx-get-antrea.sh`** — read-only: dumps the current `Antrea` policy, its container-cluster-span, and the NSX enforcement point's cluster-control-planes list (see Prerequisites above).

## Live control (no CLI needed after bootstrap)

Once bootstrapped (via `prepare-demo.sh` or a manual run of this script), the attack-lab's `/network` page can toggle between `full` and `lockdown` — and shows a live traffic diagram derived from the actual rule set — with no further CLI access required. See `avimart/apps/attack-lab/app.py` (`_nsx_build_rules`, `nsx_reconcile_dfw`) for the Python port of this script's rule logic.
