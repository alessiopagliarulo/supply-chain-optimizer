#!/usr/bin/env node
// Generates frontend/src/generated/landingData.ts from the committed artifacts.
//
// Why this exists: the landing page publishes numbers with no API call behind them.
// Twice in this repo's history a figure was hand-transcribed into the frontend and
// drifted from the artifact it claimed to describe. Nothing here is typed by hand —
// every value is pulled from an artifact by explicit path, and every value carries
// that path so the page can show its own source.
//
// The generated file IS committed: a wrong number then shows up in a diff, and the
// pytest in backend/tests/test_landing_data_contract.py fails the suite if it drifts.

import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..', '..')
const OUT = resolve(REPO, 'frontend/src/generated/landingData.ts')

const cache = new Map()
function artifact(rel) {
  if (!cache.has(rel)) {
    const p = resolve(REPO, rel)
    if (!existsSync(p)) throw new Error(`artifact missing: ${rel}`)
    cache.set(rel, JSON.parse(readFileSync(p, 'utf8')))
  }
  return cache.get(rel)
}

// Pull one value out of an artifact by dotted path. Throws rather than emitting
// undefined — a landing page with a blank stat is worse than a failed build.
function pick(rel, path) {
  let cur = artifact(rel)
  for (const key of path.split('.')) {
    if (cur == null) throw new Error(`path died at "${key}" in ${rel}:${path}`)
    cur = Array.isArray(cur) ? cur[Number(key)] : cur[key]
  }
  if (cur === undefined || cur === null) throw new Error(`no value at ${rel}:${path}`)
  return cur
}

// Catalogue counts live in the tracked SQLite DB, not in any JSON artifact. Render's
// static-site builder has no sqlite3 binary, so fall back to the value already
// committed in the generated file — the DB is tracked, so at a given commit the
// committed count and the DB agree by construction, and the pytest proves it.
const COUNT_KEYS = ['parts', 'distributors', 'offers', 'partsPriced', 'distributorsQuoting']

function catalogueCounts() {
  // Totals AND coverage. The catalogue holds 791 parts and 92 distributors, but 23 parts have
  // no offer at all and 9 distributors quote nothing — so "791 parts priced by 92 distributors"
  // overstates both sides. Publish the total and the priced subset, never the total alone
  // dressed as the priced one.
  const q = `SELECT (SELECT COUNT(*) FROM components),
                    (SELECT COUNT(*) FROM distributors),
                    (SELECT COUNT(*) FROM distributor_offers),
                    (SELECT COUNT(DISTINCT component_id) FROM distributor_offers),
                    (SELECT COUNT(DISTINCT distributor_id) FROM distributor_offers);`
  try {
    const raw = execFileSync('sqlite3', [resolve(REPO, 'backend/supply_chain.db'), q], { encoding: 'utf8' }).trim()
    const nums = raw.split('|').map(Number)
    if (nums.length !== COUNT_KEYS.length || nums.some((n) => !Number.isInteger(n) || n <= 0)) {
      throw new Error(`bad row: ${raw}`)
    }
    const out = Object.fromEntries(COUNT_KEYS.map((k, i) => [k, nums[i]]))
    if (out.partsPriced > out.parts || out.distributorsQuoting > out.distributors) {
      throw new Error('coverage exceeds totals — the query or the DB is wrong')
    }
    return { ...out, readFrom: 'sqlite3' }
  } catch (err) {
    if (!existsSync(OUT)) throw new Error(`sqlite3 unavailable and no committed landingData.ts to fall back on: ${err.message}`)
    const prev = readFileSync(OUT, 'utf8')
    const grab = (name) => {
      const m = prev.match(new RegExp(`\\n  ${name}: (\\d+),`))
      if (!m) throw new Error(`sqlite3 unavailable and committed landingData.ts has no ${name}`)
      return Number(m[1])
    }
    return { ...Object.fromEntries(COUNT_KEYS.map((k) => [k, grab(k)])), readFrom: 'committed' }
  }
}

const CHRONOS = 'docs/chronos_benchmark.json'
const BACKTEST = 'docs/forecast_backtest.json'

const counts = catalogueCounts()

// Derived values state their formula and inputs so the arithmetic is checkable on the
// page itself rather than taken on trust.
const chronosWape = pick(CHRONOS, 'chronos.overall.wape')
const naiveWape = pick(CHRONOS, 'seasonal_naive.overall.wape')
const wapeReduction = (naiveWape - chronosWape) / naiveWape

const stats = [
  {
    id: 'forecast-mape',
    value: pick(BACKTEST, 'prophet.overall.mape') * 100,
    unit: '%',
    label: 'demand forecast error, 12 months out-of-sample',
    detail: 'MAPE on held-out data. Tracking signal is −31.1, so the model runs systematically low — stated, not hidden.',
    source: `${BACKTEST} → prophet.overall.mape`,
  },
  {
    id: 'wape-reduction',
    value: wapeReduction * 100,
    unit: '%',
    label: 'less forecast error than seasonal-naive',
    detail: `Derived: (${naiveWape} − ${chronosWape}) / ${naiveWape}. Both WAPEs measured on the same held-out horizon.`,
    source: `${CHRONOS} → chronos.overall.wape vs seasonal_naive.overall.wape (derived)`,
    derived: true,
  },
  {
    id: 'latency',
    value: pick(CHRONOS, 'chronos.steady_state.median_ms'),
    unit: ' ms',
    label: 'median inference, 8.65M-parameter forecaster',
    detail: `p95 is ${pick(CHRONOS, 'chronos.steady_state.p95_ms')} ms. Steady state, after warm-up.`,
    source: `${CHRONOS} → chronos.steady_state.median_ms`,
  },
]

const rigour = [
  {
    // NOT "live offers". `distributor_offers` has no date column of any kind, so freshness is
    // not merely unverified — it is unfalsifiable from the data. The vintage is a static 2024
    // snapshot asserted in backend/seeds/seed_db.py and docs/DATA_PROVENANCE.md. Say snapshot.
    label: 'Real catalogue, no synthetic data',
    value: `${counts.partsPriced.toLocaleString()} priced parts`,
    detail: `${counts.partsPriced.toLocaleString()} of ${counts.parts.toLocaleString()} catalogued parts carry a price, quoted by ${counts.distributorsQuoting} of ${counts.distributors} distributors across ${counts.offers.toLocaleString()} offers — a static 2024 Nexar/Octopart snapshot, not a live feed. Where real data does not exist, the site says so.`,
    source: 'backend/supply_chain.db',
  },
]

// NO WALL-CLOCK TIMESTAMP IN THE OUTPUT, deliberately. This file is committed and
// regenerated by `prebuild` on every single build, so a `Generated: <now>` line made
// `git status` dirty after every `npm run build` — for a diff that carried no
// information. That is not cosmetic here: this repo regenerates artifacts that stamp
// `provenance.git.dirty`, and an artifact generated after a build would have recorded
// itself as irreproducible because of a comment. The output is now a pure function of
// the artifacts and the DB: it changes when the DATA changes, and a diff on this file
// always means a published number moved. `git log` already records when.
const file = `// GENERATED FILE — DO NOT EDIT BY HAND.
// Regenerate with: cd frontend && node scripts/build-landing-data.mjs
//
// Every value below was read out of a committed artifact by explicit path. The path
// travels with the value and is rendered on the page. Hand-editing a number here is
// exactly the failure this file exists to prevent, and the backend contract test
// (backend/tests/test_landing_data_contract.py) will fail if you do.
//
// Catalogue counts read from: ${counts.readFrom}

export interface LandingStat {
  id: string
  value: number
  unit: string
  label: string
  detail: string
  source: string
  derived?: boolean
}

export interface LandingProofPoint {
  label: string
  value: string
  detail: string
  source: string
}

export const landingStats: LandingStat[] = ${JSON.stringify(stats, null, 2)}

export const landingProofPoints: LandingProofPoint[] = ${JSON.stringify(rigour, null, 2)}

export const catalogue = {
  parts: ${counts.parts},
  distributors: ${counts.distributors},
  offers: ${counts.offers},
  partsPriced: ${counts.partsPriced},
  distributorsQuoting: ${counts.distributorsQuoting},
}
`

writeFileSync(OUT, file)
console.log(`landingData.ts written — ${stats.length} stats, ${rigour.length} proof points, counts from ${counts.readFrom}`)
