#!/usr/bin/env sh
# End-to-end check against a running stack:  deploy/smoke.sh http://127.0.0.1:8080 [api-key]
# Uploads two files, waits until a worker has indexed them, then searches.
set -eu
BASE="${1:-http://127.0.0.1:8080}"
AUTH=""
[ -n "${2:-}" ] && AUTH="Authorization: Bearer $2"
curl_api() { if [ -n "$AUTH" ]; then curl -sf -H "$AUTH" "$@"; else curl -sf "$@"; fi; }

for i in $(seq 1 60); do curl -sf "$BASE/readyz" >/dev/null && break; sleep 1; done
curl -sf "$BASE/readyz" >/dev/null || { echo "service never became ready" >&2; exit 1; }

STAMP=$(date +%s)
printf 'glacier retreat measurements %s from the himalayan station' "$STAMP" > /tmp/smoke-a.txt
printf 'भारत की राजधानी नई दिल्ली है और किताबें %s' "$STAMP" > /tmp/smoke-b.txt
A=$(curl_api -F file=@/tmp/smoke-a.txt "$BASE/v1/documents" | sed 's/.*"asset_id":\([0-9]*\).*/\1/')
B=$(curl_api -F file=@/tmp/smoke-b.txt "$BASE/v1/documents" | sed 's/.*"asset_id":\([0-9]*\).*/\1/')
for id in "$A" "$B"; do
  for i in $(seq 1 60); do
    curl_api "$BASE/v1/assets/$id" | grep -q '"status":"indexed"' && break
    sleep 1
  done
  curl_api "$BASE/v1/assets/$id" | grep -q '"status":"indexed"' || { echo "asset $id was not indexed" >&2; exit 1; }
done
curl_api "$BASE/v1/search?q=glacier+$STAMP" | grep -q '"total":1' || { echo "english search failed" >&2; exit 1; }
# The query goes in through a file, not argv: some shells (Git Bash on Windows) mangle non-ASCII arguments.
printf 'किताब %s' "$STAMP" > .smoke-q.txt
curl_api -G --data-urlencode "q@.smoke-q.txt" "$BASE/v1/search" | grep -q '"total":1' || { echo "hindi search failed" >&2; exit 1; }
curl_api "$BASE/metrics" >/dev/null 2>&1 || true
rm -f .smoke-q.txt
echo "smoke test passed"
