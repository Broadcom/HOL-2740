# AviMart — vDefend Deployment Manifests

Kubernetes manifests for the full AviMart demo stack. Apply with `kubectl apply -f .` (or per-file).

Application source code lives in [`ANS/avimart-chrism`](https://github-vcf.devops.broadcom.net/ANS/avimart-chrism).

---

## What's deployed

| Manifest | Contents |
|---|---|
| `namespace.yaml` | `avimart` namespace |
| `gateway.yaml` | Avi Gateway (`hol-tkg-gateway`) |
| `deployment.yaml` | Everything else: database (PostgreSQL 15) + seed Job, frontend, auth-svc, order-svc, product-svc, admin-svc, vuln-svc, attack-lab, and all HTTPRoutes/L7Rules |

All service images are `harbor.site-a.vcf.lab/library/avimart:<service>-v1`.

---

## Endpoints

| URL | Backend | WAF |
|---|---|---|
| `https://avimart.site-a.vcf.lab` | frontend | yes |
| `https://avimart.site-a.vcf.lab/api/products*` | product-svc | yes |
| `https://avimart.site-a.vcf.lab/api/auth*` | auth-svc | yes |
| `https://avimart.site-a.vcf.lab/api/cart*` | order-svc | yes |
| `https://avimart.site-a.vcf.lab/api/orders/search` | order-svc | yes (virtual patch demo) |
| `https://avimart.site-a.vcf.lab/api/orders*` | order-svc | no (IDOR demo) |
| `https://avimart.site-a.vcf.lab/api/vuln*` | vuln-svc | no (raw attack surface) |
| `https://avimart.site-a.vcf.lab/chat*` | chatbot-svc | yes |
| `https://avimart.site-a.vcf.lab/mcp*` | mcp-svc | yes |
| `https://waflab.site-a.vcf.lab` | attack-lab | no |

WAF is applied via `waf-avimart` L7Rule → `WAF-avimart-policy` on the Avi controller.  
Routes without WAF are intentionally unprotected for demo purposes.

---

## New deployment checklist

Run through these once, in order, on a fresh cluster/environment:

1. **Antrea ↔ NSX connectivity** (cluster-level, one-time infra step, outside this repo) — the VKS cluster must be registered with NSX Manager as an Antrea container cluster, via the cluster's addon-config, before any vDefend DFW rule can be applied. See [`../nsx-demo-dfw.md`](../nsx-demo-dfw.md#prerequisites).
2. **Apply the manifests** — `kubectl apply -f namespace.yaml -f gateway.yaml -f deployment.yaml` deploys everything in the table above, including the seed Job.
3. **Wait for the seed Job** (runs automatically as part of step 2):
   ```bash
   kubectl wait --for=condition=complete job/avimart-db-seed -n avimart --timeout=60s
   ```
   Creates users (alice, bob, charlie / `password123`), products, and orders 1–6 used in the IDOR demo.
4. **Ollama model** — the pod pulls `qwen2.5:1.5b` automatically on first start via an init container and caches it on a 5Gi PVC. No manual step needed.
5. **Run `prepare-demo.sh`** from the repo root (must be sourced, not executed):
   ```bash
   source ../prepare-demo.sh
   ```
   Prompts for Avi/NSX credentials, creates the `nsx-dfw-creds` Secret this namespace's attack-lab deployment (in `deployment.yaml`) reads, resets Avi WAF to detection-only, and bootstraps + resets vDefend DFW to Act 0 (wide open) — see [`../nsx-demo-dfw.md`](../nsx-demo-dfw.md).
6. **Restart the attack-lab pod** so it picks up the Secret created in step 5:
   ```bash
   kubectl rollout restart deployment/waf-attack-lab -n avimart
   ```
7. **Verify**: open `https://waflab.site-a.vcf.lab/network` — the stage badge should read `NONE (wide open)`.

---

## Demo reset

Resets `WAF-avimart-policy` to detection-only and disables the virtual patch rule group:

```bash
./avi-demo-reset.sh
```

See [`avi-demo-reset.md`](../avi-demo-reset.md) for details. Set `AVI_DEBUG=1` to trace API calls.

`prepare-demo.sh` (step 5 above) runs this automatically as part of resetting to Act 0.

---

## vDefend DFW (east-west microsegmentation)

The live demo mechanism is the NSX-managed "Antrea" security policy, reconciled by [`../nsx-demo-dfw.sh`](../nsx-demo-dfw.sh) — see [`../nsx-demo-dfw.md`](../nsx-demo-dfw.md) for stages, prerequisites, and env vars. Once bootstrapped (step 5 above), it's controllable live from the attack-lab's `/network` page — no further CLI needed.

Four-act demo narrative:

0. **Wide open** (`prepare-demo.sh`, `DFW_STAGE=none`) — nothing enforced, every attack succeeds, including east-west ones Avi/WAF structurally can't see.
1. **Avi WAF blocking** — perimeter attacks (SQLi, etc.) get blocked at the Gateway; `vuln-svc → admin-svc` still succeeds since it never crosses the Gateway.
2. **vDefend baseline** (`/network` page → Deploy) — tier-based microsegmentation now blocks that lateral movement, while checkout and the chatbot keep working.
3. *(optional)* **Lockdown → Restore** (`/network` page) — shows the failure mode of over-rotating on zero trust (checkout/chatbot break), then that the `vuln-svc → admin-svc` block survives a full lockdown/restore cycle.

> **Legacy, do not apply**: `antrea-policies/antrea-policies.yaml` predates the above — it's a plain Antrea `ClusterNetworkPolicy` CRD approach, a different enforcement path from the NSX-managed policy above. Applying both risks conflicting/redundant rules. Kept for reference only.
