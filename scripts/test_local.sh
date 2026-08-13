#!/usr/bin/env bash
# Run a local research job against the running server.
# Usage: ./scripts/test_local.sh [HOST]

set -euo pipefail

HOST="${1:-http://localhost:8080}"
AUTH_ARGS=()
if [[ -n "${API_KEY:-}" ]]; then
  AUTH_ARGS=(-H "X-API-Key: $API_KEY")
fi

echo "==> Submitting research job..."
RESPONSE=$(curl -sf -X POST "$HOST/api/v1/research" \
  "${AUTH_ARGS[@]}" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Analyse NVIDIA for long-term investment in the AI infrastructure cycle",
    "tickers": ["NVDA"],
    "research_depth": "standard"
  }')

JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "==> Job submitted: $JOB_ID"

echo "==> Polling for result..."
for i in $(seq 1 30); do
  sleep 5
  STATUS=$(curl -sf "${AUTH_ARGS[@]}" "$HOST/api/v1/research/$JOB_ID" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d['status'])")
  echo "    [attempt $i] status=$STATUS"
  if [[ "$STATUS" == "completed" || "$STATUS" == "failed" ]]; then
    break
  fi
done

echo ""
echo "==> Final result:"
curl -sf "${AUTH_ARGS[@]}" "$HOST/api/v1/research/$JOB_ID" | python3 -m json.tool
