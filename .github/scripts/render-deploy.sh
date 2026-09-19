#!/usr/bin/env bash
# Deploy exactly one gated commit to one Render service, and prove Render took it.
#
# Usage: render-deploy.sh <service-id> <label>
# Env:   SHA             the full 40-char commit the Model CI gate verified
#        RENDER_API_KEY  Render API key (never printed)
#        RENDER_API      optional base URL, default https://api.render.com/v1
#
# Called by both deploy steps in .github/workflows/deploy-render.yml, so the two
# services cannot drift apart. backend/tests/test_deploy_pins_the_gated_commit.py
# runs this file against a stubbed curl for every Render reply it handles.
#
# The rule throughout: the step is green only when Render positively names the
# gated commit. "Render said nothing about the commit" is a failure, not a pass
# (issue #5; the same bug class as the Model CI gate's "no run is not a pass").
#
#   POST body   {"commitId": "$SHA"}. Render's create-deploy API: commitId is
#               "The SHA of a specific Git commit to deploy for a service.
#               Defaults to the latest commit on the service's connected
#               branch." An empty body would ship main's tip, gated or not.
#   non-2xx     fail, printing Render's own error text.
#   201         the body is the deploy; its commit.id must equal $SHA.
#   202         Render queued it behind a deploy in progress and returns an
#               empty body, so there is nothing to compare yet. Look the deploy
#               up in the service's recent deploys instead; fail if no deploy of
#               $SHA created by this call shows up.
#   other 2xx   same rule: a body without the gated commit fails.
set -euo pipefail

service_id="${1:?usage: render-deploy.sh <service-id> <label>}"
label="${2:?usage: render-deploy.sh <service-id> <label>}"
api="${RENDER_API:-https://api.render.com/v1}"
: "${RENDER_API_KEY:?RENDER_API_KEY is not set}"

if ! [[ "${SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::Refusing to deploy $label: SHA '${SHA:-}' is not a full 40-character commit id."
  exit 1
fi

response="$(mktemp)"
requested_at="$(date -u +%s)"
code="$(curl -sS -o "$response" -w '%{http_code}' -X POST \
  "$api/services/$service_id/deploys" \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -d "$(jq -cn --arg sha "$SHA" '{commitId: $sha}')")"

if [ "$code" -lt 200 ] || [ "$code" -ge 300 ]; then
  echo "::error::Render refused the $label deploy ($service_id, HTTP $code): $(cat "$response")"
  exit 1
fi

# A 2xx that is not JSON (a proxy or gateway page) must still show what came back.
if [ -s "$response" ] && ! jq -e . "$response" >/dev/null 2>&1; then
  echo "::error::Render answered the $label deploy ($service_id) with HTTP $code but a body that is not JSON: $(cat "$response")"
  exit 1
fi

deploy_id=""
built=""
if [ -s "$response" ]; then
  deploy_id="$(jq -r '.id // empty' "$response")"
  built="$(jq -r '.commit.id // empty' "$response")"
fi

if [ -z "$built" ] && [ "$code" = "202" ]; then
  echo "Render queued the $label deploy of $SHA (HTTP 202, empty body); looking it up in the service's deploys"
  for attempt in 1 2 3 4 5 6; do
    listing="$(curl -sS -w '\n%{http_code}' \
      "$api/services/$service_id/deploys?limit=20" \
      -H "Authorization: Bearer $RENDER_API_KEY" \
      -H "Accept: application/json")"
    list_code="${listing##*$'\n'}"
    list_body="${listing%$'\n'*}"
    if [ "$list_code" -ge 200 ] && [ "$list_code" -lt 300 ]; then
      # Newest deploy of exactly $SHA created no earlier than a minute before
      # our request, so an older deploy of the same commit cannot vouch for this one.
      match="$(jq -r --arg sha "$SHA" --argjson since "$((requested_at - 60))" '
        [ .[].deploy
          | select(.commit.id == $sha)
          | select((.createdAt | sub("\\.[0-9]+"; "") | fromdateiso8601) >= $since) ]
        | first // empty | "\(.id) \(.status)"' <<<"$list_body" 2>/dev/null || true)"
      if [ -n "$match" ]; then
        deploy_id="${match%% *}"
        built="$SHA"
        echo "Found queued deploy ${deploy_id} (status ${match#* })"
        break
      fi
    else
      echo "Listing $label deploys returned HTTP $list_code: $list_body"
    fi
    if [ "$attempt" -lt 6 ]; then sleep "${RENDER_LOOKUP_SLEEP:-5}"; fi
  done
fi

echo "Render deploy ${deploy_id:-<no id>} for $label ($service_id): Render reports commit ${built:-<none>}, gated commit $SHA"

if [ -z "$built" ]; then
  echo "::error::Render accepted the $label deploy (HTTP $code) but never named the commit it is building, so there is no proof it is the gated commit $SHA. Response: $(cat "$response")"
  exit 1
fi
if [ "$built" != "$SHA" ]; then
  echo "::error::Render is building commit $built for $label, but the commit this run gated is $SHA. An unverified commit is being deployed."
  exit 1
fi
