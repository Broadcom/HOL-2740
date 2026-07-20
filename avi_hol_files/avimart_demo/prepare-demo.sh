#!/usr/bin/env bash
# Prepares a shell + cluster for the AviMart demo: prompts for Avi/NSX
# credentials, exports them into THIS shell so avi-demo-*.sh / nsx-demo-*.sh
# pick them up with no further prompting, creates/updates the "nsx-dfw-creds"
# K8s Secret that the attack-lab app's network page reads at runtime (never
# committed to git), and resets both Avi WAF and vDefend DFW to "Act 0" —
# wide open, no protection at all — so the demo always starts from the same
# known state: app works, every attack succeeds, including the east-west ones
# Avi/WAF structurally can't see. From there the live demo narrative is:
#   Act 0 (this script) -> Act 1: enable Avi WAF -> Act 2: attack-lab's
#   Network page, "Deploy Baseline" (vDefend microsegmentation) -> Act 3
#   (optional): "Lockdown" then "Restore", to show the override survives.
# nsx-demo-dfw.sh also bootstraps the underlying groups/services/cluster-span
# on first run (idempotent, create-if-missing) — so after this script, the
# Network page's buttons work standalone for the rest of the demo with no
# further CLI needed.
#
# Must be SOURCED, not executed, so the exports land in your shell:
#   source ./prepare-demo.sh
#   . ./prepare-demo.sh
#
# Prompts for (skipped if already set in your shell):
#   AVI_HOST, AVI_USER, AVI_PASSWORD
#   NSX_HOST, NSX_USER, NSX_PASSWORD
#
# Also creates/updates:
#   kubectl Secret "nsx-dfw-creds" in namespace avimart (user/password keys)
#   Avi WAF-avimart-policy -> detection-only (via avi-demo-reset.sh)
#   NSX "Antrea" security policy -> empty rule set (via nsx-demo-dfw.sh DFW_STAGE=none)

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "ERROR: this script must be sourced, not executed, so the exports reach your shell." >&2
  echo "Run:  source ${BASH_SOURCE[0]}" >&2
  exit 1
fi

_prepare_demo() {
  local NAMESPACE="avimart"
  local SECRET_NAME="nsx-dfw-creds"
  local _avi_host _avi_user _avi_password _nsx_host _nsx_user _nsx_password

  read -r -p "Avi host [${AVI_HOST:-sfo-w01-avilb01.sfo.rainpole.io}]: " _avi_host
  export AVI_HOST="${_avi_host:-${AVI_HOST:-sfo-w01-avilb01.sfo.rainpole.io}}"
  read -r -p "Avi username [${AVI_USER:-admin}]: " _avi_user
  export AVI_USER="${_avi_user:-${AVI_USER:-admin}}"
  if [[ -z "${AVI_PASSWORD:-}" ]]; then
    read -r -s -p "Avi password: " _avi_password; echo
    export AVI_PASSWORD="$_avi_password"
  else
    echo "AVI_PASSWORD already set, keeping it."
  fi

  read -r -p "NSX host [${NSX_HOST:-sfo-w01-nsx01.sfo.rainpole.io}]: " _nsx_host
  export NSX_HOST="${_nsx_host:-${NSX_HOST:-sfo-w01-nsx01.sfo.rainpole.io}}"
  read -r -p "NSX username [${NSX_USER:-admin}]: " _nsx_user
  export NSX_USER="${_nsx_user:-${NSX_USER:-admin}}"
  if [[ -z "${NSX_PASSWORD:-}" ]]; then
    read -r -s -p "NSX password: " _nsx_password; echo
    export NSX_PASSWORD="$_nsx_password"
  else
    echo "NSX_PASSWORD already set, keeping it."
  fi

  echo "→ Creating/updating Secret ${SECRET_NAME} in namespace ${NAMESPACE} ..."
  if ! kubectl create secret generic "$SECRET_NAME" \
      --namespace "$NAMESPACE" \
      --from-literal=user="$NSX_USER" \
      --from-literal=password="$NSX_PASSWORD" \
      --dry-run=client -o yaml | kubectl apply -f - > /dev/null; then
    echo "ERROR: failed to create/update the ${SECRET_NAME} secret." >&2
    return 1
  fi

  local SCRIPT_DIR
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  echo "→ Resetting Avi WAF to detection-only (Act 0: wide open) ..."
  if ! bash "${SCRIPT_DIR}/avi-demo-reset.sh"; then
    echo "WARNING: avi-demo-reset.sh failed — WAF may still be in blocking mode. Fix and re-run it manually." >&2
  fi

  echo "→ Clearing vDefend DFW rules (Act 0: wide open) ..."
  if ! DFW_STAGE=none bash "${SCRIPT_DIR}/nsx-demo-dfw.sh"; then
    echo "WARNING: nsx-demo-dfw.sh failed — vDefend rules may not be cleared. Fix and re-run manually: DFW_STAGE=none ./nsx-demo-dfw.sh" >&2
  fi

  echo "✔ Ready. AVI_*/NSX_* are exported in this shell; ${SECRET_NAME} is set in the cluster; Act 0 (wide open) is live."
}

_prepare_demo
unset -f _prepare_demo
