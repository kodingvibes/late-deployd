#!/usr/bin/env bash
# Smoke test for a live late-deployd instance.
#
# Confirms the running daemon is healthy without running the full
# test suite. Hits real HTTP endpoints. Exits 0 on success, non-zero
# on the first failure.
#
# Usage:   scripts/smoke_test.sh [BASE_URL]
# default: http://127.0.0.1:9200
set -euo pipefail

BASE="${1:-${DEPLOYD_URL:-http://127.0.0.1:9200}}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
pass() { printf "  ✓ %s\n" "$*"; }
fail() { printf "  ✗ %s\n" "$*" >&2; exit 1; }

bold "smoke: $BASE"

bold "[1/5] GET /health"
body="$(curl -fsS -m 5 "$BASE/health")" || fail "/health did not respond"
echo "$body" | grep -q '"ok":true' || fail "/health missing ok:true"
echo "$body" | grep -q '"repos":' || fail "/health missing repos"
pass "/health OK"

bold "[2/5] GET /logs"
body="$(curl -fsS -m 5 "$BASE/logs")" || fail "/logs did not respond"
echo "$body" | grep -q '\[' || fail "/logs not a JSON array"
pass "/logs OK"

bold "[3/5] GET /api/deployd/events"
body="$(curl -fsS -m 5 "$BASE/api/deployd/events?limit=3")" || fail "/api/deployd/events did not respond"
echo "$body" | grep -q '\[' || fail "/api/deployd/events not a JSON array"
pass "/api/deployd/events OK"

bold "[4/5] GET /api/dashboard/state (no auth -> should 401 or 503)"
code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$BASE/api/dashboard/state")"
case "$code" in
  401|503) pass "/api/dashboard/state returns $code without auth (expected)" ;;
  *)       fail "/api/dashboard/state returned $code without auth (expected 401 or 503)" ;;
esac

bold "[5/5] POST /deploy-webhook with bad signature -> 401"
code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST \
  -H 'x-github-event: push' \
  -H 'x-hub-signature-256: sha256=deadbeef' \
  -H 'Content-Type: application/json' \
  -d '{"ref":"refs/heads/main","after":"abc1234567","repository":{"full_name":"kodingvibes/late.kodingvibes.com"}}' \
  "$BASE/deploy-webhook")"
case "$code" in
  401) pass "/deploy-webhook rejects bad signature with 401" ;;
  *)   fail "/deploy-webhook returned $code (expected 401)" ;;
esac

echo
bold "all 5 smoke checks passed."
