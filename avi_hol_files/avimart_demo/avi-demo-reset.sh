#!/usr/bin/env bash
# Resets WAF-avimart-policy to a clean demo state:
#   - mode  → WAF_MODE_DETECTION
#   - pre_crs_groups[virtualpatch] → enable: false
#
# Required env vars:
#   AVI_HOST     controller hostname/IP  (default: sfo-w01-avilb01.sfo.rainpole.io)
#   AVI_USER     admin username          (default: admin)
#   AVI_PASSWORD admin password          (required — no default)
#   AVI_TENANT   tenant name             (default: chrisblog)
#   AVI_VERSION  API version             (default: 32.1.1)
set -euo pipefail
AVI_DEBUG="${AVI_DEBUG:-0}"
dbg() { [[ "$AVI_DEBUG" == "1" ]] && echo "$@" >&2 || true; }

AVI_HOST="${AVI_HOST:-alb-a.site-a.vcf.lab}"
AVI_USER="${AVI_USER:-admin}"
AVI_VERSION="${AVI_VERSION:-32.1.1}"
AVI_TENANT="${AVI_TENANT:-Acme-East-A}"
POLICY_NAME="WAF-avimart-policy"

if [[ -z "${AVI_PASSWORD:-}" ]]; then
  echo "ERROR: AVI_PASSWORD is not set." >&2
  exit 1
fi

BASE_URL="https://${AVI_HOST}"
COOKIE_JAR=$(mktemp)
trap 'rm -f "$COOKIE_JAR"' EXIT

# --- Login ---
echo "→ Logging in as ${AVI_USER} ..."
LOGIN_RESPONSE=$(curl -s -k \
  -c "$COOKIE_JAR" \
  -D - \
  -X POST "${BASE_URL}/login" \
  -F "username=${AVI_USER}" \
  -F "password=${AVI_PASSWORD}")

CSRF_TOKEN=$(awk '/csrftoken/{print $NF}'      "$COOKIE_JAR")
AVI_SESSION=$(awk '/avi-sessionid/{print $NF}' "$COOKIE_JAR")
SESSION_ID=$(awk '/\tsessionid\t/{print $NF}'  "$COOKIE_JAR")

if [[ -z "$CSRF_TOKEN" || -z "$AVI_SESSION" || "$AVI_SESSION" == "None" ]]; then
  echo "ERROR: login failed — missing session cookies." >&2
  cat "$COOKIE_JAR" >&2
  exit 1
fi
echo "   logged in (session: ${AVI_SESSION:0:8}...)"

COOKIE_HDR="accesstoken=None; refreshtoken=None; csrftoken=${CSRF_TOKEN}; avi-sessionid=${AVI_SESSION}; sessionid=${SESSION_ID}"

CURL_COMMON=(
  -s -k
  -H "accept: application/json"
  -H "Content-Type: application/json"
  -H "X-Avi-Tenant: ${AVI_TENANT}"
  -H "X-Avi-Version: ${AVI_VERSION}"
  -H "Referer: ${BASE_URL}"
  -H "x-csrftoken: ${CSRF_TOKEN}"
  -H "cookie: ${COOKIE_HDR}"
)

# --- Fetch policy ---
echo "→ Fetching ${POLICY_NAME} ..."
RESPONSE=$(curl "${CURL_COMMON[@]}" \
  -X GET \
  "${BASE_URL}/api/wafpolicy?name=${POLICY_NAME}")

COUNT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if 'count' not in d:
        import pprint
        print('ERROR: unexpected response:', file=sys.stderr)
        pprint.pprint(d, stream=sys.stderr)
        sys.exit(2)
    print(d['count'])
except json.JSONDecodeError:
    print('ERROR: non-JSON response:', file=sys.stderr)
    sys.exit(2)
") || exit 1

if [[ "$COUNT" -ne 1 ]]; then
  echo "ERROR: expected 1 result for ${POLICY_NAME}, got ${COUNT}." >&2
  exit 1
fi

UUID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['results'][0]['uuid'])")
echo "   uuid: ${UUID}"

# --- Patch ---
echo "→ Patching policy ..."
UPDATED=$(echo "$RESPONSE" | python3 -c "
import sys, json

data = json.load(sys.stdin)['results'][0]

data['mode'] = 'WAF_MODE_DETECTION_ONLY'

for group in data.get('pre_crs_groups', []):
    if group.get('name') == 'virtualpatch':
        group['enable'] = False

print(json.dumps(data))
")

PUT_RESPONSE=$(curl "${CURL_COMMON[@]}" \
  -X PUT \
  "${BASE_URL}/api/wafpolicy/${UUID}" \
  -d "$UPDATED")

dbg "PUT response: $PUT_RESPONSE"

# --- Verify ---
MODE=$(echo "$PUT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mode',''))")
VP_ENABLED=$(echo "$PUT_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for g in data.get('pre_crs_groups', []):
    if g.get('name') == 'virtualpatch':
        print(g.get('enable'))
        sys.exit()
print('not found')
")

echo ""
echo "Done."
echo "  mode                = ${MODE}"
echo "  virtualpatch.enable = ${VP_ENABLED}"

if [[ "$MODE" != "WAF_MODE_DETECTION_ONLY" || "$VP_ENABLED" != "False" ]]; then
  echo "WARNING: one or more values did not update as expected." >&2
  exit 1
fi
