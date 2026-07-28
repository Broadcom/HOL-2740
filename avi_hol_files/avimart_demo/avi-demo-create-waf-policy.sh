#!/usr/bin/env bash
# Creates WAF-avimart-policy from scratch, including the "virtualpatch"
# pre-CRS group (id:9001 — X-Forwarded-IP SQLi patch), disabled by default.
# Idempotent: if the policy already exists, prints its uuid and exits 0
# without modifying it (use avi-demo-reset.sh to reset an existing policy).
#
# Required env vars:
#   AVI_HOST     controller hostname/IP  (default: sfo-w01-avilb01.sfo.rainpole.io)
#   AVI_USER     admin username          (default: admin)
#   AVI_PASSWORD admin password          (required — no default)
#   AVI_TENANT   tenant name             (default: chrisblog)
#   AVI_VERSION  API version             (default: 32.1.1)
#
# Optional env vars (names of objects that must already exist on the controller):
#   WAF_PROFILE_NAME       (default: System-WAF-Profile)
#   WAF_CRS_NAME           (default: CRS-2025-2)
#   WAF_APP_SIG_PROVIDER   (default: System-WafApplicationSignatures-Trustwave)
set -euo pipefail
AVI_DEBUG="${AVI_DEBUG:-0}"
dbg() { [[ "$AVI_DEBUG" == "1" ]] && echo "$@" >&2 || true; }

AVI_HOST="${AVI_HOST:-alb-a.site-a.vcf.lab}"
AVI_USER="${AVI_USER:-admin}"
AVI_VERSION="${AVI_VERSION:-32.1.1}"
AVI_TENANT="${AVI_TENANT:-Acme-East-A}"
POLICY_NAME="WAF-avimart-policy"

WAF_PROFILE_NAME="${WAF_PROFILE_NAME:-System-WAF-Profile}"
WAF_CRS_NAME="${WAF_CRS_NAME:-CRS-2025-2}"
WAF_APP_SIG_PROVIDER="${WAF_APP_SIG_PROVIDER:-System-WafApplicationSignatures-Trustwave}"

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

# --- Bail out early if the policy already exists ---
echo "→ Checking for existing ${POLICY_NAME} ..."
EXISTING=$(curl "${CURL_COMMON[@]}" -X GET "${BASE_URL}/api/wafpolicy?name=${POLICY_NAME}")
EXISTING_COUNT=$(echo "$EXISTING" | python3 -c "
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

if [[ "$EXISTING_COUNT" -gt 0 ]]; then
  EXISTING_UUID=$(echo "$EXISTING" | python3 -c "import sys,json; print(json.load(sys.stdin)['results'][0]['uuid'])")
  echo "   ${POLICY_NAME} already exists (uuid: ${EXISTING_UUID}) — nothing to do."
  echo "   Use avi-demo-reset.sh to reset it to the clean demo state instead."
  exit 0
fi

# --- Resolve object refs by name ---
resolve_ref() {
  local endpoint="$1" name="$2" resp
  resp=$(curl "${CURL_COMMON[@]}" -X GET "${BASE_URL}/api/${endpoint}?name=${name}")
  echo "$resp" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except json.JSONDecodeError:
    print('ERROR: non-JSON response resolving ${endpoint} \"${name}\"', file=sys.stderr)
    sys.exit(2)
if d.get('count', 0) != 1 or not d.get('results'):
    print('ERROR: expected 1 result for ${endpoint} named \"${name}\", got', d.get('count', 0), file=sys.stderr)
    sys.exit(3)
print(d['results'][0]['url'])
" || exit 1
}

echo "→ Resolving object refs ..."
TENANT_REF=$(resolve_ref tenant "${AVI_TENANT}")
WAF_PROFILE_REF=$(resolve_ref wafprofile "${WAF_PROFILE_NAME}")
WAF_CRS_REF=$(resolve_ref wafcrs "${WAF_CRS_NAME}")
APP_SIG_REF=$(resolve_ref wafapplicationsignatureprovider "${WAF_APP_SIG_PROVIDER}")
dbg "tenant_ref=${TENANT_REF}"
dbg "waf_profile_ref=${WAF_PROFILE_REF}"
dbg "waf_crs_ref=${WAF_CRS_REF}"
dbg "app_sig_ref=${APP_SIG_REF}"

# --- Build create payload (mirrors the live policy shape + virtualpatch group) ---
PAYLOAD=$(TENANT_REF="$TENANT_REF" WAF_PROFILE_REF="$WAF_PROFILE_REF" \
  WAF_CRS_REF="$WAF_CRS_REF" APP_SIG_REF="$APP_SIG_REF" \
  POLICY_NAME="$POLICY_NAME" python3 -c "
import json, os

data = {
    'name': os.environ['POLICY_NAME'],
    'tenant_ref': os.environ['TENANT_REF'],
    'waf_profile_ref': os.environ['WAF_PROFILE_REF'],
    'waf_crs_ref': os.environ['WAF_CRS_REF'],
    'mode': 'WAF_MODE_DETECTION_ONLY',
    'paranoia_level': 'WAF_PARANOIA_LEVEL_LOW',
    'allow_mode_delegation': True,
    'auto_update_crs': True,
    'bypass_static_extensions': True,
    'enable_streaming': False,
    'failure_mode': 'WAF_FAILURE_MODE_OPEN',
    'fixed_sampling_rate': 1,
    'sampling_mode': 'WAF_SAMPLING_MODE_NO_SAMPLING',
    'use_evaluation_mode_on_crs_update': True,
    'application_signatures': {
        'provider_ref': os.environ['APP_SIG_REF'],
    },
    'pre_crs_groups': [
        {
            'name': 'virtualpatch',
            'index': 0,
            'enable': False,
            'rules': [
                {
                    'name': 'protext xff',
                    'rule_id': '9001',
                    'index': 0,
                    'enable': True,
                    'is_sensitive': False,
                    'rule': 'SecRule REQUEST_HEADERS:X-Forwarded-IP \"@detectSQLi\" \"id:9001,phase:1,deny,status:403\"',
                },
            ],
        },
    ],
}
print(json.dumps(data))
")

dbg "POST payload: ${PAYLOAD}"

# --- Create ---
echo "→ Creating ${POLICY_NAME} ..."
CREATE_RESPONSE=$(curl "${CURL_COMMON[@]}" -X POST "${BASE_URL}/api/wafpolicy" -d "$PAYLOAD")
dbg "POST response: ${CREATE_RESPONSE}"

NEW_UUID=$(echo "$CREATE_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except json.JSONDecodeError:
    print('ERROR: non-JSON response on create:', file=sys.stderr)
    sys.exit(2)
uuid = d.get('uuid')
if not uuid:
    import pprint
    print('ERROR: create failed — no uuid in response:', file=sys.stderr)
    pprint.pprint(d, stream=sys.stderr)
    sys.exit(2)
print(uuid)
") || { echo "ERROR: failed to create ${POLICY_NAME}." >&2; exit 1; }

MODE=$(echo "$CREATE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mode',''))")
VP_ENABLED=$(echo "$CREATE_RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for g in data.get('pre_crs_groups', []):
    if g.get('name') == 'virtualpatch':
        print(g.get('enable'))
        sys.exit()
print('not found')
")

echo ""
echo "Done — created ${POLICY_NAME}."
echo "  uuid                = ${NEW_UUID}"
echo "  mode                = ${MODE}"
echo "  virtualpatch.enable = ${VP_ENABLED}"
echo ""
echo "Note: remember to point the waf-avimart L7Rule at this policy by name if it isn't already."

if [[ "$MODE" != "WAF_MODE_DETECTION_ONLY" || "$VP_ENABLED" != "False" ]]; then
  echo "WARNING: one or more values were not created as expected." >&2
  exit 1
fi
