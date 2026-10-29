#!/usr/bin/env bash
# The API in six curl calls. Needs a running backend (make up) and jq.
#
#   ./examples/quickstart.sh                 # against http://localhost:8000
#   API=https://your-host ./examples/quickstart.sh
set -euo pipefail
API=${API:-http://localhost:8000}
USER_=${USER_:-admin}            # creating a cage needs the admin role
PASS=${PASS:-change-me-please}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "# 1. health"
curl -s "$API/health" | jq .

echo "# 2. login, keep the token"
TOKEN=$(curl -s -X POST "$API/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_\",\"password\":\"$PASS\"}" | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

echo "# 3. make a cage (409 if it already exists, that's fine)"
curl -s -X POST "$API/api/v1/cages" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"id":"cage-001","name":"Example cage","location":"bench","animal_strain":"C57BL/6","animal_age_days":70}' | jq .

echo "# 4. post one reading, the same packet the Arduino bridge sends"
sed "s/\"seq\": 1/\"seq\": $(date +%s)/; s/2026-10-29T20:14:31Z/$(date -u +%Y-%m-%dT%H:%M:%SZ)/" "$HERE/ingest_packet.json" \
  | curl -s -X POST "$API/api/v1/ingest" -H "$AUTH" -H 'Content-Type: application/json' -d @- | jq .

echo "# 5. read it back"
curl -s "$API/api/v1/cages/cage-001/readings?limit=3" -H "$AUTH" | jq '.[0]'

echo "# 6. export a day as CSV"
curl -s "$API/api/v1/cages/cage-001/export.csv?days=1" -H "$AUTH" | head -5
