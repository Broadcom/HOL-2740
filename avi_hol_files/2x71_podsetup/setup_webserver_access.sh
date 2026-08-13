#!/bin/bash
# Run this FROM manager. Copies manager's SSH public key to each webserver
# (so manager can log in as holuser without a password) and configures
# passwordless sudo for holuser on each one.
#
# Requires sshpass (used only for the one-time initial password auth).
#
# The webserver password is NOT hardcoded here -- these 4 hosts authenticate
# with a different password than the rest of the lab, already stored as the
# ansible-vault secret `holuser_webserver_password` in secrets.yml alongside
# this script (same source inventory.yml uses). Resolution order:
#   1. HOLUSER_WEBSERVER_PASSWORD env var, if set
#   2. decrypted from secrets.yml via ansible-vault, using vaultsecret.txt

set -uo pipefail

HOSTS=(web1.site-a.vcf.lab web2.site-a.vcf.lab web1.site-b.vcf.lab web2.site-b.vcf.lab)
REMOTE_USER="holuser"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_FILE="${SCRIPT_DIR}/secrets.yml"
VAULT_PASSWORD_FILE="/home/holuser/vaultsecret.txt"

REMOTE_PASSWORD="${HOLUSER_WEBSERVER_PASSWORD:-}"

if [ -z "$REMOTE_PASSWORD" ] && [ -f "$SECRETS_FILE" ] && [ -f "$VAULT_PASSWORD_FILE" ] && command -v ansible-vault >/dev/null 2>&1; then
    REMOTE_PASSWORD="$(ansible-vault view --vault-password-file "$VAULT_PASSWORD_FILE" "$SECRETS_FILE" 2>/dev/null \
        | python3 -c 'import sys, yaml; print(yaml.safe_load(sys.stdin).get("holuser_webserver_password", ""))' 2>/dev/null)"
fi

if [ -z "$REMOTE_PASSWORD" ]; then
    echo "ERROR: could not determine the webserver password." >&2
    echo "       Export HOLUSER_WEBSERVER_PASSWORD, or run this where ${SECRETS_FILE}" >&2
    echo "       and ${VAULT_PASSWORD_FILE} are both reachable (e.g. on manager)." >&2
    exit 1
fi

if ! command -v sshpass >/dev/null 2>&1; then
    echo "ERROR: sshpass is required but not installed on this host." >&2
    exit 1
fi

if [ ! -f "$HOME/.ssh/id_ed25519.pub" ] && [ ! -f "$HOME/.ssh/id_rsa.pub" ]; then
    echo "No SSH key pair found on this host -- generating one (ed25519)..."
    ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519"
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)

for HOST in "${HOSTS[@]}"; do
    echo "=== $HOST ==="

    echo "  Copying SSH public key..."
    if ! sshpass -p "$REMOTE_PASSWORD" ssh-copy-id "${SSH_OPTS[@]}" "${REMOTE_USER}@${HOST}" >/tmp/ssh-copy-id.$$.log 2>&1; then
        echo "  FAILED to copy SSH key to $HOST -- skipping. See /tmp/ssh-copy-id.$$.log"
        continue
    fi
    rm -f /tmp/ssh-copy-id.$$.log

    if ! ssh -o BatchMode=yes "${SSH_OPTS[@]}" "${REMOTE_USER}@${HOST}" true 2>/dev/null; then
        echo "  FAILED: passwordless SSH login not working for $HOST -- skipping sudo setup."
        continue
    fi
    echo "  Passwordless SSH login confirmed."

    echo "  Configuring passwordless sudo for $REMOTE_USER..."
    ssh -o BatchMode=yes "${SSH_OPTS[@]}" "${REMOTE_USER}@${HOST}" \
        "SUDOPW='${REMOTE_PASSWORD}' bash -s" <<'REMOTE_SCRIPT'
set -uo pipefail
TMPFILE=$(mktemp)
echo "holuser ALL=(ALL) NOPASSWD: ALL" > "$TMPFILE"
chmod 0440 "$TMPFILE"
if echo "$SUDOPW" | sudo -S visudo -c -f "$TMPFILE" >/dev/null 2>&1; then
    echo "$SUDOPW" | sudo -S cp "$TMPFILE" /etc/sudoers.d/91-holuser-nopasswd
    echo "$SUDOPW" | sudo -S chown root:root /etc/sudoers.d/91-holuser-nopasswd
    echo "$SUDOPW" | sudo -S chmod 0440 /etc/sudoers.d/91-holuser-nopasswd
    rm -f "$TMPFILE"
    echo "SUDOERS_INSTALLED_OK"
else
    echo "SUDOERS_VALIDATION_FAILED -- leaving existing sudo config untouched"
    rm -f "$TMPFILE"
    exit 1
fi
REMOTE_SCRIPT

    if ssh -o BatchMode=yes "${SSH_OPTS[@]}" "${REMOTE_USER}@${HOST}" "sudo -n true" 2>/dev/null; then
        echo "  Confirmed: passwordless sudo works on $HOST."
    else
        echo "  WARNING: passwordless sudo verification failed on $HOST -- check manually."
    fi
    echo ""
done

echo "Done."
