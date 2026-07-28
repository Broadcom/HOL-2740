#!/usr/bin/env bash
# Deletes the "Antrea" DFW SecurityPolicy (its rules and container-cluster-span
# go with it) and every group nsx-demo-dfw.sh manages, restoring a clean slate
# so that script can recreate everything fresh in one shot.
#
# Why this exists: NSX's Policy API tracks a per-object "_revision" for
# optimistic concurrency, including per-rule on a SecurityPolicy. Deleting
# and recreating from scratch sidesteps ever having to reconcile revisions
# by hand, and also clears out any group whose membership criteria got left
# in a bad state (e.g. a stray empty condition row from manual UI edits).
#
# Does NOT touch the NSX Services (tcp-8080 etc.) — those are generic port
# objects, unrelated to policy/group state, safe to leave alone.
#
# Required env vars:
#   NSX_HOST     manager hostname/IP  (default: sfo-w01-nsx01.sfo.rainpole.io)
#   NSX_USER     admin username       (default: admin)
#   NSX_PASSWORD admin password       (required — no default)
set -euo pipefail
NSX_DEBUG="${NSX_DEBUG:-0}"
dbg() { [[ "$NSX_DEBUG" == "1" ]] && echo "$@" >&2 || true; }

NSX_HOST="${NSX_HOST:-sfo-w01-nsx01.sfo.rainpole.io}"
NSX_USER="${NSX_USER:-admin}"

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
INFRA_URL="${BASE_URL}/policy/api/v1/infra/domains/${DOMAIN_ID}"

# GET a full URL, echo the numeric HTTP status code.
get_status() {
  curl "${CURL_COMMON[@]}" -o /dev/null -w '%{http_code}' -X GET "$1"
}

# DELETE a full URL if it currently exists (200); skip quietly if already
# gone (404). Any other status is a hard error.
delete_if_exists() {
  local label="$1" url="$2"
  echo "→ Checking ${label} ..."
  local status
  status=$(get_status "$url")
  if [[ "$status" == "404" ]]; then
    echo "   ${label} already gone, skipping"
    return
  elif [[ "$status" != "200" ]]; then
    echo "ERROR: unexpected status ${status} checking ${label}." >&2
    exit 1
  fi
  echo "   deleting ${label} ..."
  local delete_status
  delete_status=$(curl "${CURL_COMMON[@]}" -o /dev/null -w '%{http_code}' -X DELETE "$url")
  if [[ "$delete_status" != "200" && "$delete_status" != "202" && "$delete_status" != "204" ]]; then
    echo "ERROR: failed to delete ${label} (status ${delete_status})." >&2
    exit 1
  fi
  echo "   deleted"
}

# --- Container-cluster-span first: it's a child of the SecurityPolicy, and
# deleting the parent doesn't reliably clean up this sub-resource. ---
delete_if_exists "container-cluster-span for '${POLICY_ID}'" \
  "${INFRA_URL}/security-policies/${POLICY_ID}/container-cluster-span/${SPAN_ID}"

# --- The SecurityPolicy itself — its inline rules go with it. ---
delete_if_exists "security policy '${POLICY_ID}'" "${INFRA_URL}/security-policies/${POLICY_ID}"

# --- Every group nsx-demo-dfw.sh manages. Deleted after the policy, since
# groups still referenced by a live rule can't be removed.
#
# NOTE: this is deliberately NOT named GROUPS — that's a bash builtin array
# variable (auto-populated with the current user's Unix group IDs, like
# `id -G`), and assigning to it doesn't reliably override its special
# behavior. Using that name here previously caused the loop to iterate over
# the shell's own GIDs instead of these NSX group names. ---
DFW_GROUPS=(
  vuln-svc admin-svc avimart-db auth-svc order-svc product-svc mcp-svc
  chatbot-svc ollama waf-attack-lab avimart-frontend avimart-ns
  tier-app tier-database tier-ai
)
for GROUP in "${DFW_GROUPS[@]}"; do
  delete_if_exists "group '${GROUP}'" "${INFRA_URL}/groups/${GROUP}"
done

echo ""
echo "Done. Re-run nsx-demo-dfw.sh to recreate everything fresh."
