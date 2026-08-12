#!/bin/bash
#
# fetch_cert.sh
#
# Purpose:
#   Fetches the TLS certificate chain presented by an Avi Controller over its
#   HTTPS interface and saves the full chain to 'full_chain.pem'.
#   Silently prefers fetching an EC certificate if available.
#
# Usage:
#   ./fetch_cert.sh <AVI_CONTROLLER_IP_or_FQDN> [ca_bundle_path]
#

# ==========================================
# 1. Define Usage and Validation
# ==========================================
usage() {
    echo "Usage: $0 <AVI_CONTROLLER_IP_or_FQDN> [ca_bundle_path]"
    echo ""
    echo "Arguments:"
    echo "  <AVI_CONTROLLER_IP_or_FQDN> : The Avi Controller IP address or hostname."
    echo "  [ca_bundle_path]            : Optional. Path to a CA bundle file to strictly validate the controller's certificate."
    echo ""
    echo "Examples:"
    echo "  $0 localhost                       # Local fetch, prefers EC silently"
    echo "  $0 avi.corp.com /etc/ca.pem        # Remote fetch, strictly validates the FQDN"
}

if [ -z "$1" ]; then
    usage
    exit 1
fi

AVI_CONTROLLER="$1"
HOST="${AVI_CONTROLLER%:*}" # Extract hostname for SNI

if [[ "$AVI_CONTROLLER" != *":"* ]]; then
    AVI_CONTROLLER="${AVI_CONTROLLER}:443"
fi
TARGET="$AVI_CONTROLLER"

CA_BUNDLE=""
if [ -n "$2" ]; then
    CA_BUNDLE="$2"
fi

# Validate CA Bundle if provided
if [ -n "$CA_BUNDLE" ]; then
    if [ ! -f "$CA_BUNDLE" ]; then
        echo "Error: CA bundle file not found at '$CA_BUNDLE'."
        exit 1
    fi
fi

# ==========================================
# 2. Remote Execution Warning Check
# ==========================================
if [ ! -f "/opt/avi/bin/controller" ]; then
    if [ -z "$CA_BUNDLE" ]; then
        echo "=========================================================================="
        echo " WARNING: Remote Environment Detected!"
        echo "          This script is not running directly on the Avi Controller."
        echo ""
        echo "          Fetching certificates over the network without a CA bundle"
        echo "          leaves this connection vulnerable to MITM attacks."
        echo ""
        echo "          Recommendation: Pass a CA bundle as an argument to strictly"
        echo "          validate the authenticity of the controller's certificate."
        echo "=========================================================================="
        echo ""
        sleep 3
    fi
fi

# ==========================================
# 3. Execution Logic
# ==========================================
cert=""

echo "Avi Controller     : $TARGET"
echo "SNI Hostname       : $HOST"

# Silently probe if TLS 1.2 is supported. If it fails, assume Strict TLS 1.3.
TLS_1_3_ONLY=false
if ! echo "Q" | openssl s_client -connect "$TARGET" -servername "$HOST" -tls1_2 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
    TLS_1_3_ONLY=true
fi

# Build the base OpenSSL arguments
OPENSSL_BASE_ARGS=("-showcerts" "-connect" "$TARGET" "-servername" "$HOST")

if [ -n "$CA_BUNDLE" ]; then
    echo "CA Validation      : ENABLED (Using $CA_BUNDLE)"
    OPENSSL_BASE_ARGS+=("-verify_hostname" "$HOST" "-CAfile" "$CA_BUNDLE" "-verify_return_error")
else
    echo "CA Validation      : DISABLED (Blind trust)"
fi
echo "----------------------------------------"
echo "Fetching certificate..."

# --- Certificate Retrieval Logic (Silently prefers EC) ---
if [ "$TLS_1_3_ONLY" = true ]; then
    # TLS 1.3 Mode: Use sigalgs to steer toward EC silently
    cert=$(openssl s_client "${OPENSSL_BASE_ARGS[@]}" -sigalgs "ecdsa_secp256r1_sha256:ecdsa_secp384r1_sha384" < /dev/null 2>/dev/null | sed -ne '/-BEGIN CERTIFICATE-/,/-END CERTIFICATE-/p')
else
    # Mixed/TLS 1.2 Mode: Use cipher and sigalgs to steer toward EC silently
    cert=$(openssl s_client "${OPENSSL_BASE_ARGS[@]}" -cipher "aECDSA" -sigalgs "ecdsa_secp256r1_sha256:ecdsa_secp384r1_sha384" < /dev/null 2>/dev/null | sed -ne '/-BEGIN CERTIFICATE-/,/-END CERTIFICATE-/p')
fi

# Fallback if the EC-preferred request failed (grabs the server default, usually RSA)
if [ -z "$cert" ]; then
    cert=$(openssl s_client "${OPENSSL_BASE_ARGS[@]}" < /dev/null 2>/dev/null | sed -ne '/-BEGIN CERTIFICATE-/,/-END CERTIFICATE-/p')
fi

# ==========================================
# 4. Save the Result
# ==========================================
if [ -n "$cert" ]; then
    echo "Successfully fetched certificate!"
    echo "$cert" > full_chain.pem
    echo "----------------------------------------"
    echo "Success: Certificate chain saved to 'full_chain.pem'."
else
    echo "----------------------------------------"
    echo "Error: Failed to fetch a valid certificate from $TARGET."
    if [ -n "$CA_BUNDLE" ]; then
        echo "(This is likely because the certificate failed strict CA/Hostname validation)."
    fi
    exit 1
fi