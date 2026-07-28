#!/usr/bin/env bash
set -euo pipefail

HARBOR_REPO="harbor.site-a.vcf.lab/library"

IMAGES=(
  "chrismentjox/avimart:admin-svc-v1"
  "chrismentjox/avimart:attack-lab-v1"
  "chrismentjox/avimart:auth-svc-v1"
  "chrismentjox/avimart:frontend-v1"
  "chrismentjox/avimart:order-svc-v1"
  "chrismentjox/avimart:product-svc-v1"
  "chrismentjox/avimart:vuln-svc-v1"
  "postgres:15-alpine"
)

for src in "${IMAGES[@]}"; do
  name_tag="${src##*/}"
  dest="${HARBOR_REPO}/${name_tag}"

  echo "==> ${src} -> ${dest}"
  docker pull "${src}"
  docker tag "${src}" "${dest}"
  docker push "${dest}"
done

echo "Done."
