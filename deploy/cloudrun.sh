#!/usr/bin/env bash
# Deploy the BSE + Meltwater MIRA MCP server to Google Cloud Run (Mumbai).
#
# Run from the repo root in an authenticated gcloud shell (e.g. GCP Cloud Shell):
#   PROJECT_ID=my-gcp-project bash deploy/cloudrun.sh
#
# Cloud Build builds the Dockerfile for you — no local Docker needed.
# Secrets go into Secret Manager; the endpoint is public but gated by MCP_API_KEYS.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
REGION="${REGION:-asia-south1}"     # Mumbai — closest region to BSE
SERVICE="${SERVICE:-bse-mcp}"

gcloud config set project "$PROJECT_ID"

echo ">> Enabling required APIs (first run can take a minute)..."
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

# --- Secrets -------------------------------------------------------------
create_or_update_secret () {
  local name="$1" value="$2"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=-
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic
  fi
}

# Meltwater key: use $MELTWATER_API_KEY if exported, else prompt (input hidden).
if [ -z "${MELTWATER_API_KEY:-}" ]; then
  read -rsp "Meltwater API key: " MELTWATER_API_KEY; echo
fi
create_or_update_secret meltwater-api-key "$MELTWATER_API_KEY"

# Client API key for the MCP endpoint: use $MCP_API_KEYS if exported, else generate.
if [ -z "${MCP_API_KEYS:-}" ]; then
  MCP_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  echo ">> Generated MCP client API key (SAVE THIS — clients send it as a bearer token):"
  echo "   $MCP_API_KEYS"
fi
create_or_update_secret mcp-api-keys "$MCP_API_KEYS"

# --- Allow the Cloud Run runtime SA to read the secrets ------------------
PROJNUM="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${PROJNUM}-compute@developer.gserviceaccount.com"
for s in meltwater-api-key mcp-api-keys; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

# --- Deploy --------------------------------------------------------------
echo ">> Building and deploying to Cloud Run ($REGION)..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --min-instances=1 \
  --timeout=300 \
  --set-secrets="MELTWATER_API_KEY=meltwater-api-key:latest,MCP_API_KEYS=mcp-api-keys:latest"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "=============================================================="
echo " Deployed."
echo "   MCP endpoint : ${URL}/mcp"
echo "   Health check : ${URL}/healthz"
echo "   Auth header  : Authorization: Bearer <your MCP_API_KEYS value>"
echo "=============================================================="
