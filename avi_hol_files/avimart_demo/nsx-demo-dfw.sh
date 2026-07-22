#!/usr/bin/env bash
# Locks down east-west traffic between the AviMart (avimart/) services on
# NSX 9.1's Antrea DFW integration: deny-by-default within the avimart
# namespace, with explicit ALLOW rules for the flows that are actually needed.
#
# Two layers, modeling two personas:
#   - Generic SecOps baseline: 3 "tier" groups (app/database/ai) matched by
#     K8s pod label, not by specific service name — any pod tagged
#     tier=<x> inherits the tier-based allow rules automatically, no script
#     change needed. This is what a security team would actually publish
#     cluster-wide on VKS/Antrea.
#   - App-owned specifics: external (Gateway) ingress per service, and the
#     vuln-svc -> admin-svc block — narrow, app-specific knowledge that
#     doesn't generalize by tag.
#
# Manages, in order:
#   1. Groups: one per K8s Service (still needed for the app-specific rules
#      above) plus a namespace-wide "avimart-ns" group (catch-all deny) plus
#      3 tier groups (Namespace + Pod-Tag criteria) — created only if
#      missing. Note: NSX Groups can't mix a Service criterion with a
#      Pod/Tag criterion in one expression, nor OR them at the top level
#      (confirmed against a live NSX 9.1 Manager — both rejected by
#      validation) — hence tier groups are always separate from the
#      per-service ones.
#   2. NSX Services (TCP port objects rules reference by path) — created
#      only if missing.
#   3. The "Antrea" SecurityPolicy and its full rule set — always PUT
#      (reconciled to match this script exactly on every run, since rules
#      need to pick up changes here, not just exist once).
#   4. The policy's container-cluster-span ("Applied To" → Antrea Container
#      Clusters), pointed at the live cluster matching CLUSTER_NAME,
#      discovered at runtime — created only if missing.
#
# Rule semantics (NSX Antrea DFW): "scope" (Applied To) always defines the
# enforcement point. Exactly ONE of source_groups/destination_groups is a
# real group per rule — the other stays "ANY". scope=X + source=Y +
# destination=ANY + direction=IN means "traffic flowing TO X, from Y".
#
# Rule ORDER matters more now than it used to: the generic tier-app ->
# tier-app allow is broad enough to also match vuln-svc -> admin-svc, so the
# specific block-vuln-svc-to-admin-svc DROP must have a lower sequence_number
# than that allow, or the broad rule would shadow it. See the `rules` list
# below — order there is preserved via ascending sequence_number.
#
# DFW_STAGE controls how much of the policy gets reconciled, for a staged
# live demo:
#   none     no rules at all — the "Antrea" policy is reconciled with an
#            empty rules array, so nothing it defines matches any traffic and
#            everything falls through to the ambient (allow) default. This is
#            the wide-open starting point for a demo: no vDefend protection,
#            no Avi WAF either (pair with avi-demo-reset.sh) — every attack,
#            including east-west ones Avi/WAF structurally can't see, should
#            succeed. See prepare-demo.sh, which sets this automatically.
#   full     (default) — deny-all + every allow rule (tier-based baseline,
#            external ingress, the specific override). The steady state.
#   lockdown — deny-all ONLY, no allow rules at all. Its source_groups AND
#            scope are both "avimart-ns", so it only matches traffic that's
#            both FROM and TO the namespace — i.e. it blocks east-west only.
#            Gateway/external ingress (source outside avimart-ns) never
#            matches this rule's source condition and falls through to the
#            ambient default (allow), so the site shell/WAF stay reachable —
#            DFW's job is east-west microsegmentation, not ingress, which is
#            Avi/WAF's job (demoed separately). What breaks under lockdown:
#            product browsing/checkout (product-svc/order-svc -> avimart-db),
#            the chatbot (mcp-svc -> its backends), and the attack-lab's own
#            probes against product-svc/vuln-svc (cluster-internal DNS, not
#            via the Gateway). Run with DFW_STAGE=lockdown first to show
#            that break, then re-run with the default stage to bring
#            east-west back via the tag-based baseline.
#
# Required env vars:
#   NSX_HOST     manager hostname/IP  (default: sfo-w01-nsx01.sfo.rainpole.io)
#   NSX_USER     admin username       (default: admin)
#   NSX_PASSWORD admin password       (required — no default)
# Optional env vars:
#   DFW_STAGE    "full" (default), "lockdown", or "none" — see above.
set -euo pipefail
NSX_DEBUG="${NSX_DEBUG:-0}"
dbg() { [[ "$NSX_DEBUG" == "1" ]] && echo "$@" >&2 || true; }

NSX_HOST="${NSX_HOST:-sfo-w01-nsx01.sfo.rainpole.io}"
NSX_USER="${NSX_USER:-admin}"
DFW_STAGE="${DFW_STAGE:-full}"
if [[ "$DFW_STAGE" != "full" && "$DFW_STAGE" != "lockdown" && "$DFW_STAGE" != "none" ]]; then
  echo "ERROR: DFW_STAGE must be 'full', 'lockdown', or 'none', got '${DFW_STAGE}'." >&2
  exit 1
fi

if [[ -z "${NSX_PASSWORD:-}" ]]; then
  echo "ERROR: NSX_PASSWORD is not set." >&2
  exit 1
fi

BASE_URL="https://${NSX_HOST}"
COOKIE_JAR=$(mktemp)
trap 'rm -f "$COOKIE_JAR"' EXIT

# --- Login ---
echo "→ Logging in as ${NSX_USER} ..."
LOGIN_HEADERS=$(curl -s -k \
  -c "$COOKIE_JAR" \
  -D - \
  -o /dev/null \
  -X POST "${BASE_URL}/api/session/create" \
  --data-urlencode "j_username=${NSX_USER}" \
  --data-urlencode "j_password=${NSX_PASSWORD}")

JSESSIONID=$(awk '/\tJSESSIONID\t/{print $NF}' "$COOKIE_JAR" | tr -d '\r')
XSRF_TOKEN=$(echo "$LOGIN_HEADERS" | awk -F': ' 'tolower($1)=="x-xsrf-token"{print $2}' | tr -d '\r')

if [[ -z "$JSESSIONID" || -z "$XSRF_TOKEN" ]]; then
  echo "ERROR: login failed — missing session cookie or XSRF token." >&2
  dbg "$LOGIN_HEADERS"
  cat "$COOKIE_JAR" >&2
  exit 1
fi
echo "   logged in (session: ${JSESSIONID:0:8}...)"

CURL_COMMON=(
  -s -k
  -H "accept: application/json"
  -H "Content-Type: application/json"
  -H "X-XSRF-TOKEN: ${XSRF_TOKEN}"
  -H "Cookie: JSESSIONID=${JSESSIONID}"
)

DOMAIN_ID="default"
POLICY_ID="Antrea"
SPAN_ID="antrea-cluster"
CLUSTER_NAME="chrisblog-ns01-6wcrt-kubernetes-cluster-fsrz"
NAMESPACE="avimart"
INFRA_URL="${BASE_URL}/policy/api/v1/infra/domains/${DOMAIN_ID}"
GLOBAL_URL="${BASE_URL}/policy/api/v1/infra"
SITE_URL="${BASE_URL}/policy/api/v1/infra/sites/default/enforcement-points/default"

# GET a full URL, echo the numeric HTTP status code.
get_status() {
  curl "${CURL_COMMON[@]}" -o /dev/null -w '%{http_code}' -X GET "$1"
}

# GET a full URL; if it's missing (404), PUT the given body to create it.
# Leaves existing objects untouched.
ensure_exists() {
  local label="$1" url="$2" body="$3"
  echo "→ Checking ${label} ..."
  local status
  status=$(get_status "$url")
  if [[ "$status" == "200" ]]; then
    echo "   ${label} already exists, skipping"
  elif [[ "$status" == "404" ]]; then
    echo "   ${label} missing — creating ..."
    local put_response
    put_response=$(curl "${CURL_COMMON[@]}" -X PUT "$url" -d "$body")
    dbg "PUT response: $put_response"
    echo "$put_response" | python3 -c "import sys,json; d=json.load(sys.stdin); print('   created:', d.get('path', d))" \
      || { echo "ERROR: failed to create ${label}:" >&2; echo "$put_response" >&2; exit 1; }
  else
    echo "ERROR: unexpected status ${status} checking ${label}." >&2
    exit 1
  fi
}

# PUT the given body, reconciling the object to match it. If the object
# already exists, its current "_revision" is fetched and merged into the
# body first — NSX's Policy API was observed rejecting a PUT with
# error_code 500127 ("already exists") on an object that had just been
# created/modified by a prior run, unless the request carries the object's
# current _revision (optimistic concurrency control). This applies not just
# to the top-level object (e.g. the SecurityPolicy) but to each entry of a
# nested array field too — NSX exposes each "rules" entry as its own child
# resource (.../security-policies/Antrea/rules/<id>) with its own
# _revision, so a rule that already exists needs its revision merged in by
# id or the same 500127 error fires on that specific rule's path.
reconcile() {
  local label="$1" url="$2" body="$3"
  echo "→ Reconciling ${label} ..."
  local status
  status=$(get_status "$url")
  if [[ "$status" == "200" ]]; then
    local existing
    existing=$(curl "${CURL_COMMON[@]}" -X GET "$url")
    body=$(python3 -c "
import sys, json
new_body = json.loads(sys.argv[1])
existing = json.loads(sys.argv[2])
if '_revision' in existing:
    new_body['_revision'] = existing['_revision']
existing_rule_revisions = {
    r['id']: r['_revision']
    for r in existing.get('rules', [])
    if 'id' in r and '_revision' in r
}
for rule in new_body.get('rules', []):
    rid = rule.get('id')
    if rid in existing_rule_revisions:
        rule['_revision'] = existing_rule_revisions[rid]
print(json.dumps(new_body))
" "$body" "$existing")
  fi
  local put_response
  put_response=$(curl "${CURL_COMMON[@]}" -X PUT "$url" -d "$body")
  dbg "PUT response: $put_response"
  echo "$put_response" | python3 -c "import sys,json; d=json.load(sys.stdin); print('   reconciled:', d.get('path', d))" \
    || { echo "ERROR: failed to reconcile ${label}:" >&2; echo "$put_response" >&2; exit 1; }
}

# Group body: avimart namespace AND the given K8s Service name.
service_group_body() {
  SVC="$1" NS="$NAMESPACE" python3 -c "
import json, os
print(json.dumps({
    'group_type': ['ANTREA'],
    'expression': [
        {
            'resource_type': 'NestedExpression',
            'expressions': [
                {'resource_type': 'Condition', 'member_type': 'Namespace', 'key': 'Name', 'operator': 'EQUALS', 'value': os.environ['NS']},
                {'resource_type': 'ConjunctionOperator', 'conjunction_operator': 'AND'},
                {'resource_type': 'Condition', 'member_type': 'Service', 'key': 'Name', 'operator': 'EQUALS', 'value': os.environ['SVC']},
            ],
        }
    ],
}))
"
}

# Group body: the whole avimart namespace, no service filter.
namespace_group_body() {
  NS="$NAMESPACE" python3 -c "
import json, os
print(json.dumps({
    'group_type': ['ANTREA'],
    'expression': [
        {'resource_type': 'Condition', 'member_type': 'Namespace', 'key': 'Name', 'operator': 'EQUALS', 'value': os.environ['NS']}
    ],
}))
"
}

# Group body: avimart namespace AND pods carrying the given K8s label
# (key=value, e.g. "tier=app"). NSX syncs K8s pod labels as Tags with
# scope "dis:k8s:<label-key>" and value "<label-value>"; the Condition's
# "value" field is that scope and value joined with "|". Note: a Service
# criterion (see service_group_body) cannot be combined with a Pod/Tag
# criterion in one NestedExpression, nor OR'd at the top level — confirmed
# against a live NSX 9.1 Manager, both rejected by validation — so tier
# groups are always Namespace+Pod, never mixed with the per-service groups.
tier_group_body() {
  LABEL_KEY="$1" LABEL_VALUE="$2" NS="$NAMESPACE" python3 -c "
import json, os
print(json.dumps({
    'group_type': ['ANTREA'],
    'expression': [
        {
            'resource_type': 'NestedExpression',
            'expressions': [
                {'resource_type': 'Condition', 'member_type': 'Namespace', 'key': 'Name', 'operator': 'EQUALS', 'value': os.environ['NS']},
                {'resource_type': 'ConjunctionOperator', 'conjunction_operator': 'AND'},
                {'resource_type': 'Condition', 'member_type': 'Pod', 'key': 'Tag', 'operator': 'EQUALS', 'value': f\"dis:k8s:{os.environ['LABEL_KEY']}|{os.environ['LABEL_VALUE']}\"},
            ],
        }
    ],
}))
"
}

# Service body: a single TCP port, referenced by rules via its path.
port_service_body() {
  ID="$1" PORT="$2" python3 -c "
import json, os
sid = os.environ['ID']
print(json.dumps({
    'resource_type': 'Service',
    'display_name': sid,
    'service_entries': [
        {
            'resource_type': 'L4PortSetServiceEntry',
            'id': sid + '-entry',
            'display_name': sid + '-entry',
            'l4_protocol': 'TCP',
            'destination_ports': [os.environ['PORT']],
        }
    ],
}))
"
}

# --- Groups: one per AviMart K8s Service, plus the namespace-wide catch-all ---
for SVC in vuln-svc admin-svc avimart-db auth-svc order-svc product-svc mcp-svc chatbot-svc ollama waf-attack-lab avimart-frontend; do
  ensure_exists "group '${SVC}'" "${INFRA_URL}/groups/${SVC}" "$(service_group_body "$SVC")"
done
ensure_exists "group 'avimart-ns'" "${INFRA_URL}/groups/avimart-ns" "$(namespace_group_body)"

# --- Groups: SecOps-style generic tiers, matched by K8s pod label (not by
# specific service name) — any pod carrying the right "tier" label inherits
# the baseline rules below without a script change. ---
ensure_exists "group 'tier-app'" "${INFRA_URL}/groups/tier-app" "$(tier_group_body tier app)"
ensure_exists "group 'tier-database'" "${INFRA_URL}/groups/tier-database" "$(tier_group_body tier database)"
ensure_exists "group 'tier-ai'" "${INFRA_URL}/groups/tier-ai" "$(tier_group_body tier ai)"

# --- NSX Services: the TCP ports referenced by the rules below ---
ensure_exists "service 'tcp-8080'" "${GLOBAL_URL}/services/tcp-8080" "$(port_service_body tcp-8080 8080)"
ensure_exists "service 'tcp-5432'" "${GLOBAL_URL}/services/tcp-5432" "$(port_service_body tcp-5432 5432)"
ensure_exists "service 'tcp-11434'" "${GLOBAL_URL}/services/tcp-11434" "$(port_service_body tcp-11434 11434)"
ensure_exists "service 'tcp-3000'" "${GLOBAL_URL}/services/tcp-3000" "$(port_service_body tcp-3000 3000)"
ensure_exists "service 'tcp-8000'" "${GLOBAL_URL}/services/tcp-8000" "$(port_service_body tcp-8000 8000)"
ensure_exists "service 'tcp-3001'" "${GLOBAL_URL}/services/tcp-3001" "$(port_service_body tcp-3001 3001)"
ensure_exists "service 'tcp-5000'" "${GLOBAL_URL}/services/tcp-5000" "$(port_service_body tcp-5000 5000)"

# --- The "Antrea" DFW security policy, fully reconciled every run ---
echo "→ Building policy for DFW_STAGE=${DFW_STAGE} ..."
ANTREA_POLICY_BODY=$(DFW_STAGE="$DFW_STAGE" python3 <<'PYEOF'
import json, os

def group_path(name):
    return f"/infra/domains/default/groups/{name}"

def svc_path(name):
    return f"/infra/services/{name}"

def allow_rule(rule_id, display, source, scope, service):
    # scope=callee, source=caller, destination=ANY, direction=IN:
    # "traffic flowing TO callee, from caller".
    return {
        "id": rule_id,
        "display_name": display,
        "action": "ALLOW",
        "direction": "IN",
        "ip_protocol": "IPV4_IPV6",
        "source_groups": [group_path(source)],
        "destination_groups": ["ANY"],
        "services": [svc_path(service)],
        "scope": [group_path(scope)],
    }

def allow_external_rule(rule_id, display, scope, service):
    # scope=X, source=ANY, destination=ANY, direction=IN: "traffic flowing
    # TO X, from anywhere" — needed for every service with a real Gateway
    # HTTPRoute, since selecting a pod as scope in ANY rule switches its
    # ingress to default-deny (Antrea/K8s NetworkPolicy semantics), which
    # would otherwise also block the Gateway's own traffic to that pod.
    return {
        "id": rule_id,
        "display_name": display,
        "action": "ALLOW",
        "direction": "IN",
        "ip_protocol": "IPV4_IPV6",
        "source_groups": ["ANY"],
        "destination_groups": ["ANY"],
        "services": [svc_path(service)],
        "scope": [group_path(scope)],
    }

# Specific, app-owned override, evaluated FIRST: must have a lower
# sequence_number than the generic tier-app -> tier-app allow, since that
# broad allow also matches vuln-svc -> admin-svc traffic. A narrower DROP
# only wins over a broader ALLOW if it's evaluated first.
#
# id is versioned (not just "block-vuln-svc-to-admin-svc") because NSX was
# observed keeping a *pre-existing* rule's sequence_number sticky across
# PUTs even when this script requests a different one — only newly-created
# rule ids get a fresh number computed from their array position.
block_vuln_to_admin_rule = {
    "id": "block-vuln-svc-to-admin-svc-v2",
    "display_name": "Block vln-svc - admin.svc",
    "action": "DROP",
    "direction": "IN",
    "ip_protocol": "IPV4_IPV6",
    "source_groups": [group_path("vuln-svc")],
    "destination_groups": ["ANY"],
    "services": ["ANY"],
    "scope": [group_path("admin-svc")],
}

# scope=avimart-ns + source=avimart-ns + destination=ANY: "traffic flowing
# TO any avimart-ns pod, from any avimart-ns pod" i.e. deny whatever
# intra-namespace traffic wasn't allowed above. The ONLY rule present in the
# "lockdown" stage — see DFW_STAGE note in the header comment.
deny_all_rule = {
    "id": "deny-all-avimart-east-west",
    "display_name": "Deny all other avimart east-west traffic",
    "action": "DROP",
    "direction": "IN",
    "ip_protocol": "IPV4_IPV6",
    "source_groups": [group_path("avimart-ns")],
    "destination_groups": ["ANY"],
    "services": ["ANY"],
    "scope": [group_path("avimart-ns")],
}

if os.environ["DFW_STAGE"] == "none":
    rules = []
elif os.environ["DFW_STAGE"] == "lockdown":
    rules = [deny_all_rule]
else:
    rules = [
        block_vuln_to_admin_rule,
        # External (Gateway) ingress — only for services that have a real
        # HTTPRoute. admin-svc, avimart-db, and ollama deliberately have
        # none of these. Kept per-service (not tag-based) since ports/routes
        # are inherently app-owned knowledge, unlike the generic tier
        # baseline below.
        allow_external_rule("allow-external-to-auth-svc", "Allow external -> auth-svc (8080)", "auth-svc", "tcp-8080"),
        allow_external_rule("allow-external-to-order-svc", "Allow external -> order-svc (8080)", "order-svc", "tcp-8080"),
        allow_external_rule("allow-external-to-product-svc", "Allow external -> product-svc (8080)", "product-svc", "tcp-8080"),
        allow_external_rule("allow-external-to-vuln-svc", "Allow external -> vuln-svc (8080)", "vuln-svc", "tcp-8080"),
        allow_external_rule("allow-external-to-chatbot-svc", "Allow external -> chatbot-svc (8000)", "chatbot-svc", "tcp-8000"),
        allow_external_rule("allow-external-to-mcp-svc", "Allow external -> mcp-svc (3001)", "mcp-svc", "tcp-3001"),
        allow_external_rule("allow-external-to-avimart-frontend", "Allow external -> avimart-frontend (3000)", "avimart-frontend", "tcp-3000"),
        allow_external_rule("allow-external-to-waf-attack-lab", "Allow external -> waf-attack-lab (5000)", "waf-attack-lab", "tcp-5000"),
        # Generic, SecOps-style baseline: allows keyed off the "tier" pod
        # label, not specific service names. Any pod tagged
        # tier=app/database/ai inherits these automatically — replaces what
        # used to be 9 separate per-service-pair rules (mcp-svc/waf-attack-lab
        # -> their peers, {auth,order,product,admin}-svc -> avimart-db,
        # chatbot-svc -> ollama).
        allow_rule("allow-tier-app-to-tier-app", "Allow tier:app -> tier:app (8080)", "tier-app", "tier-app", "tcp-8080"),
        allow_rule("allow-tier-app-to-tier-database", "Allow tier:app -> tier:database (5432)", "tier-app", "tier-database", "tcp-5432"),
        allow_rule("allow-tier-app-to-tier-ai", "Allow tier:app -> tier:ai (11434)", "tier-app", "tier-ai", "tcp-11434"),
        deny_all_rule,
    ]

# Evaluation order is NOT reliably array position — NSX's own docs say order
# is defined by each rule's "sequence_number", which must be set explicitly
# and consistently or the API auto-assigns its own (which came out scrambled
# here, putting the catch-all deny first). Number every rule ascending so
# ALLOWs are always evaluated before the DROPs that would otherwise shadow
# them.
for i, rule in enumerate(rules, start=1):
    rule["sequence_number"] = i

policy = {
    "resource_type": "SecurityPolicy",
    "display_name": "Antrea",
    "category": "Application",
    "target_type": "ANTREA",
    "stateful": True,
    "tcp_strict": True,
    "sequence_number": 499999,
    "scope": ["ANY"],
    "rules": rules,
}

print(json.dumps(policy))
PYEOF
)

reconcile "security policy '${POLICY_ID}'" "${INFRA_URL}/security-policies/${POLICY_ID}" "$ANTREA_POLICY_BODY"

# --- The policy's "Applied To" → Antrea Container Cluster association ---
echo "→ Looking for Antrea cluster matching '${CLUSTER_NAME}' ..."
CLUSTER_LIST=$(curl "${CURL_COMMON[@]}" -X GET "${SITE_URL}/cluster-control-planes")
CLUSTER_PATH=$(CLUSTER_NAME="$CLUSTER_NAME" python3 -c "
import sys, json, os
d = json.load(sys.stdin)
name = os.environ['CLUSTER_NAME']
for r in d.get('results', []):
    if r.get('node_type') == 'ANTREA_NODE' and name in r.get('id', ''):
        print(r['path'])
        break
" <<< "$CLUSTER_LIST")

if [[ -z "$CLUSTER_PATH" ]]; then
  echo "ERROR: no Antrea cluster matching '${CLUSTER_NAME}' found." >&2
  echo "Available Antrea clusters — update CLUSTER_NAME in this script to one of these:" >&2
  echo "$CLUSTER_LIST" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for r in d.get('results', []):
    if r.get('node_type') == 'ANTREA_NODE':
        print('  -', r.get('id', r.get('display_name', '?')))
" >&2
  exit 1
fi
echo "   found: ${CLUSTER_PATH}"

SPAN_BODY=$(CLUSTER_PATH="$CLUSTER_PATH" python3 -c "
import json, os
print(json.dumps({
    'resource_type': 'SecurityPolicyContainerCluster',
    'container_cluster_type': 'ANTREA',
    'container_cluster_path': os.environ['CLUSTER_PATH'],
}))
")

ensure_exists "container-cluster-span for '${POLICY_ID}'" \
  "${INFRA_URL}/security-policies/${POLICY_ID}/container-cluster-span/${SPAN_ID}" "$SPAN_BODY"
