#!/usr/bin/env bash
# Validate .github/loop-config.json - the settings every loop workflow reads.
#
# Every workflow reads it with `jq ... // default` and falls back to the default when
# jq fails or a value is missing. That is right for a missing KEY, but it also
# swallows a malformed FILE or a wrong TYPE: a stray comma turns the Scout's
# tailoring off, `"prCap": 3.7` silently disables the Builder's queue cap, and every
# run stays green. This script is where that becomes a red tick instead.
#
# Usage: scripts/validate-loop-config.sh [path]   (default .github/loop-config.json)
# Exit 0 = valid (unknown top-level keys only warn), 1 = invalid.
# Run by .github/workflows/loop-config.yml; tested by
# backend/tests/test_loop_workflow_gates.py.
set -uo pipefail

CFG=${1:-.github/loop-config.json}

fail() {
  echo "::error file=$CFG::$1"
  exit 1
}

[ -f "$CFG" ] || fail "$CFG is missing. Every loop workflow reads it; without it they all fall back to template defaults."
# `jq empty` accepts a zero-byte file, so check for content first.
grep -q '[^[:space:]]' "$CFG" || fail "$CFG is empty. The loop workflows would silently fall back to template defaults."
jq empty "$CFG" 2>/dev/null || fail "$CFG is not valid JSON. The loop workflows would silently ignore it and fall back to template defaults."

# One line per problem. A key may be absent (the workflow default applies), but when
# it is present it must have the type the workflows expect - an explicit null
# included, since `.key // default` reads null exactly like a missing key.
problems=$(jq -r '
  def whole: type == "number" and . == floor and . >= 0;
  def cap: whole or . == "unlimited";
  def strings: type == "array" and all(.[]; type == "string");
  # Present (even as null) but not `ok` -> one problem line.
  def check($path; ok; $want):
    if (getpath($path[:-1]) | has($path[-1])) and ((getpath($path) | ok) | not)
    then "\($path | join(".")): must be \($want) (got \(getpath($path) | tojson | .[:80]))"
    else empty end;
  if type != "object" then "the top level must be a JSON object"
  else
    check(["version"]; type == "number"; "a number"),
    check(["autonomousBuildEnabled"]; type == "boolean"; "true or false"),
    check(["prCap"]; cap; "a whole number or \"unlimited\""),
    check(["ideaQueueCap"]; cap; "a whole number or \"unlimited\""),
    check(["demoPort"]; whole and . > 0 and . < 65536; "a port number"),
    check(["aiProvider"]; . == "subscription" or . == "bedrock"; "\"subscription\" or \"bedrock\""),
    check(["bedrockRegion"]; type == "string" and length > 0; "a region name"),
    check(["inFlight"]; type == "object"; "an object"),
    (if (.inFlight | type) == "object" then
      check(["inFlight", "lookbackDays"]; whole and . >= 1 and . <= 90; "a whole number from 1 to 90")
    else empty end),
    check(["scout"]; type == "object"; "an object"),
    (if (.scout | type) == "object" then
      check(["scout", "productSummary"]; type == "string"; "a string"),
      check(["scout", "currentGoals"]; strings; "a list of strings"),
      check(["scout", "offLimits"]; strings; "a list of strings"),
      check(["scout", "lenses"]; strings; "a list of strings"),
      check(["scout", "maxPerRun"]; whole and . >= 1; "a whole number of at least 1"),
      check(["scout", "aiProvider"]; . == "subscription" or . == "bedrock"; "\"subscription\" or \"bedrock\""),
      check(["scout", "staleCheck"]; type == "object"; "an object"),
      (if (.scout.staleCheck | type) == "object" then
        check(["scout", "staleCheck", "enabled"]; type == "boolean"; "true or false"),
        check(["scout", "staleCheck", "intervalHours"]; type == "number" and . > 0; "a positive number of hours")
      else empty end)
    else empty end)
  end
' "$CFG")

if [ -n "$problems" ]; then
  while IFS= read -r line; do
    echo "::error file=$CFG::$line"
  done <<<"$problems"
  echo "$CFG parses, but the values above have the wrong type or range. The workflows would read each one as its default without saying so."
  exit 1
fi

# A key no workflow reads is most often a typo (`autonomusBuildEnabled`), which means
# the setting the owner meant to change silently is not changed. Warn, do not fail:
# the dashboard may add a new key before this list learns about it.
known='["version","autonomousBuildEnabled","prCap","ideaQueueCap","demoPort","aiProvider","bedrockRegion","inFlight","scout"]'
unknown=$(jq -r --argjson known "$known" 'keys - $known | join(", ")' "$CFG")
if [ -n "$unknown" ]; then
  echo "::warning file=$CFG::Unknown top-level key(s): $unknown. No loop workflow reads them - check for a typo."
fi

echo "$CFG is valid. autonomousBuildEnabled=$(jq -r '.autonomousBuildEnabled // false' "$CFG") prCap=$(jq -r '.prCap // 3' "$CFG")"
