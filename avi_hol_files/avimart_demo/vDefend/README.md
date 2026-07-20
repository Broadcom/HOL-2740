# AviMart — vDefend Deployment Manifests

Kubernetes manifests for the full AviMart demo stack. ArgoCD watches this directory and syncs automatically on every push.

Application source code lives in [`ANS/avimart-chrism`](https://github-vcf.devops.broadcom.net/ANS/avimart-chrism).

---

## What's deployed

| Manifest | Service | Image |
|---|---|---|
| `namespace.yaml` | `avimart` namespace | — |
| `database.yaml` | PostgreSQL 15 | `postgres:15-alpine` |
| `frontend.yaml` | React shop UI | `chrismentjox/avimart:frontend` |
| `auth-svc.yaml` | JWT auth | `chrismentjox/avimart:auth-svc` |
| `order-svc.yaml` | Orders + virtual patching endpoint | `chrismentjox/avimart:order-svc` |
| `product-svc.yaml` | Product catalog | `chrismentjox/avimart:product-svc` |
| `admin-svc.yaml` | Unauthenticated admin endpoints | `chrismentjox/avimart:admin-svc` |
| `vuln-svc.yaml` | XSS / command injection / path traversal | `chrismentjox/avimart:vuln-svc` |
| `attack-lab.yaml` | Flask WAF testing console | `chrismentjox/avimart:attack-lab` |
| `chatbot-stack/chatbot.yaml` | Ollama-backed chatbot | `chrismentjox/avimart:chatbot` |
| `chatbot-stack/mcp.yaml` | MCP tool server | `chrismentjox/avimart:mcp` |
| `chatbot-stack/ollama.yaml` | Ollama LLM runtime (CPU) | `ollama/ollama:latest` |
| `gateway/gateway.yaml` | Avi Gateway (`chris-tkg-gateway`) | — |
| `httproutes.yaml` | All HTTPRoute rules + WAF bindings | — |

---

## Endpoints

| URL | Backend | WAF |
|---|---|---|
| `https://avimart.k8s.fqdn.nl` | frontend | yes |
| `https://avimart.k8s.fqdn.nl/api/products*` | product-svc | yes |
| `https://avimart.k8s.fqdn.nl/api/auth*` | auth-svc | yes |
| `https://avimart.k8s.fqdn.nl/api/cart*` | order-svc | yes |
| `https://avimart.k8s.fqdn.nl/api/orders/search` | order-svc | yes (virtual patch demo) |
| `https://avimart.k8s.fqdn.nl/api/orders*` | order-svc | no (IDOR demo) |
| `https://avimart.k8s.fqdn.nl/api/vuln*` | vuln-svc | no (raw attack surface) |
| `https://avimart.k8s.fqdn.nl/chat*` | chatbot-svc | yes |
| `https://avimart.k8s.fqdn.nl/mcp*` | mcp-svc | yes |
| `https://waflab.k8s.fqdn.nl` | attack-lab | no |

WAF is applied via `waf-avimart` L7Rule → `WAF-avimart-policy` on the Avi controller.  
Routes without WAF are intentionally unprotected for demo purposes.

---

## New deployment checklist

Run through these once, in order, on a fresh cluster/environment:

1. **Antrea ↔ NSX connectivity** (cluster-level, one-time infra step, outside this repo) — the VKS cluster must be registered with NSX Manager as an Antrea container cluster, via the cluster's addon-config, before any vDefend DFW rule can be applied. See [`../nsx-demo-dfw.md`](../nsx-demo-dfw.md#prerequisites).
2. **ArgoCD sync** — applying this directory deploys everything in the table above.
3. **Seed the database** (once):
   ```bash
   kubectl apply -f database-seed-job.yaml
   ```
   Creates users (alice, bob, charlie / `password123`), products, and orders 1–6 used in the IDOR demo.
4. **Ollama model** — the pod pulls `qwen2.5:1.5b` automatically on first start via an init container and caches it on a 5Gi PVC. No manual step needed.
5. **Run `prepare-demo.sh`** from the repo root (must be sourced, not executed):
   ```bash
   source ../prepare-demo.sh
   ```
   Prompts for Avi/NSX credentials, creates the `nsx-dfw-creds` Secret this namespace's `attack-lab.yaml` reads, resets Avi WAF to detection-only, and bootstraps + resets vDefend DFW to Act 0 (wide open) — see [`../nsx-demo-dfw.md`](../nsx-demo-dfw.md).
6. **Restart the attack-lab pod** so it picks up the Secret created in step 5:
   ```bash
   kubectl rollout restart deployment/waf-attack-lab -n avimart
   ```
7. **Verify**: open `https://waflab.k8s.fqdn.nl/network` — the stage badge should read `NONE (wide open)`.

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
