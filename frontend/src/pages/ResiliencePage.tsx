import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ShieldAlert } from 'lucide-react';
import {
  type AffectedComponentDetail,
  type BomLine,
  type ScenarioResponse,
  type DeliveryTargetResponse,
  type CriticalitySweepResponse,
  type DualSourcingResponse,
  type SensitivityResponse,
  resilienceAPI,
  distributorsAPI,
  cartAPI,
  componentsAPI,
} from '../services/api';
import { ScenarioCard } from '../components/ScenarioCard';
import { DeltaCard } from '../components/DeltaCard';
import { DistributorSelector, GeopoliticalRiskSelector, DeliveryTargetSelector } from '../components/DistributorSelector';
import { MonteCarloChart } from '../components/MonteCarloChart';
import { BOMImpactTable } from '../components/BOMImpactTable';
import { CriticalitySweepTable } from '../components/CriticalitySweepTable';
import { DualSourcingTable } from '../components/DualSourcingTable';
import { TornadoChart } from '../components/TornadoChart';
import { BomCostBreakdownTable } from '../components/BomCostBreakdownTable';
import WakeNotice from '../components/WakeNotice';
import { useElapsedSeconds } from '../services/warmup';

const usd = (n: number) =>
  `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// Served prose, rendered verbatim, needs the site's own dash. The API composes some
// of its sentences with an ASCII double hyphen ("does not fall -- it RISES"), which
// lands on screen as two hyphens beside em dashes everywhere else on the page. This
// is a RENDERING normalisation only: it changes no word, no number and no claim, and
// it deliberately matches the spaced form ` -- ` so a hyphenated identifier or a
// range inside a served string is left alone.
const servedProse = (s: string) => s.replace(/ -- /g, ' — ');

// Single source of truth for the simulation count quoted in the UI. This must track
// `N_SCENARIOS` in backend/app/graph/simulation.py — the API does not return the count,
// so this is the one place it is written down rather than being retyped in prose.
const MC_SCENARIOS = 1000;
const mcLabel = `${MC_SCENARIOS.toLocaleString()} Monte Carlo scenarios`;

// Fulfilment is a SERVED pair of fields (`baseline_fulfillment_p50` /
// `scenario_fulfillment_p50`). EVERY figure and sentence this page prints about
// fulfilment is derived here, from those two fields, so no prose anywhere on the
// page can drift away from the response that produced it.
//
// This exists because the page shipped the opposite: the API's own
// `hedging.statement` asserts "Zero fulfillment impact is the correct answer"
// whenever no BOM line is structurally orphaned — and the SAME response carried
// baseline 1.0 / scenario 0.8, a real 20-point drop. Structural hedging (every
// line still has a supplier) and modelled fulfilment (what the Monte Carlo
// cascade actually delivers) are different questions with different answers.
export type FulfilmentImpact = {
  baselinePct: number;
  scenarioPct: number;
  /** Negative when fulfilment falls — this is a delta, not a magnitude. */
  deltaPts: number;
  /** "100% → 80%", rendered at the same size as the claim it qualifies. */
  headline: string;
  /** "−20 pts" */
  deltaLabel: string;
};

function fulfilmentImpact(r: ScenarioResponse): FulfilmentImpact | null {
  const b = r.baseline_fulfillment_p50;
  const s = r.scenario_fulfillment_p50;
  if (typeof b !== 'number' || typeof s !== 'number') return null;
  if (b - s <= 0.001) return null;
  const baselinePct = b * 100;
  const scenarioPct = s * 100;
  const deltaPts = scenarioPct - baselinePct;
  return {
    baselinePct,
    scenarioPct,
    deltaPts,
    headline: `${baselinePct.toFixed(0)}% → ${scenarioPct.toFixed(0)}%`,
    deltaLabel: `−${Math.abs(deltaPts).toFixed(0)} pts`,
  };
}

// A cost delta says nothing useful on its own when the scenario also destroys
// fulfilment: the cheapest possible supply chain is one that ships nothing. This is
// the same trap that made the deleted Digital Twin page paint a −100% cost delta
// green. Where fulfilment falls, the cost card must still carry the warning that its
// own number understates the damage.
//
// It does NOT restate the figures. This line used to read "fulfilment falls
// 100% → 80%", which put the same pair in small amber print immediately beside the
// Fulfilment (P50) card that publishes it at 2xl — the fifth printing of one number
// on one screen. The warning is the part that does work here; the number belongs to
// the card the line points at, which sits directly next to this one.
function fulfilmentCaveat(r: ScenarioResponse): string | undefined {
  const impact = fulfilmentImpact(r);
  if (!impact) return undefined;
  return (
    'Read with care: this cost covers only what can still be sourced, so it ' +
    'understates the true impact — see Fulfilment (P50).'
  );
}

// The BOM impact table's count line. Zero orphaned lines is a real and correct
// answer, but it is NOT "no impact" — so where the served fulfilment fields show a
// drop, the count line says both things at the same size instead of the bare
// "No components affected" the table used to print on its own.
function affectedEmptyLabel(r: ScenarioResponse): ReactNode | undefined {
  const impact = fulfilmentImpact(r);
  if (!impact) return undefined;
  return (
    <>
      No BOM line loses every supplier —{' '}
      <span className="text-red-300 font-medium">
        but modelled fulfilment (P50) still falls {impact.headline} ({impact.deltaLabel})
      </span>
      .
    </>
  );
}

// The fulfilment figure, set at the SAME size and weight as the dollar figure it
// sits beside — not underneath it in small print. A qualifier is never smaller
// than the claim it qualifies; that rule is why this page publishes the drop as a
// headline rather than as a footnote.
function FulfilmentHeadline({ impact }: { impact: FulfilmentImpact }) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase tracking-wider text-red-300">
        Modelled fulfilment (P50)
      </div>
      <div className="text-2xl font-bold text-red-300 tabular-nums">
        {impact.headline}
        <span className="text-sm font-semibold text-red-300/90 ml-2">
          ({impact.deltaLabel})
        </span>
      </div>
    </div>
  );
}

// Shared $-framing for the "Total Cost" delta card across all three scenarios.
const COST_TOOLTIP =
  'Real dollars: each BOM line valued at the average of its real distributor offer ' +
  `prices, then inflated by the Monte Carlo emergency-procurement model (${mcLabel}). ` +
  'The delta is the extra spend the disruption forces.';

// Shared framing for the "Delivery ETA" card. Needed because a FALLING ETA under a
// failure scenario looks like a defect at a glance — it is not. The cheapest supplier
// is often also the most distant one, so dropping it can pull delivery in while
// pushing cost up. The backend ships this same sentence as `eta_basis`.
const ETA_TOOLTIP =
  'The slowest line of the plan priced beside it — a geography-derived lead-time ' +
  'estimate (distance at a fixed ground speed, plus fixed processing and customs ' +
  'allowances) for the distributor each line is actually bought from, not an observed ' +
  'delivery time and not the fastest supplier in the catalogue. A cheap distant ' +
  'supplier is also a slow one, so dropping it can improve delivery while raising cost.';

// Translates the CVaR-95 cost multiplier into a concrete dollar figure: the extra
// procurement spend exposed in the worst-5% of disruption scenarios. Fully derived
// from real data — baseline BOM spend × (CVaR-95 − 1).
//
// A BOM that is fully hedged against the scenario (every line still has a surviving
// supplier) genuinely prices out to $0 here — CVaR-95 only measures the emergency
// premium for a line that becomes completely unavailable, and no line does. That is
// a correct finding, not a broken computation, but leading the tile with $0.00 reads
// as one. When the API's own `hedging` block confirms zero lines were orphaned AND
// carries a `cost_substitution` figure (the real cost: re-sourcing to the
// next-cheapest surviving offer), the tile leads with THAT number instead and states
// plainly that zero tail exposure is the correct answer for a hedged BOM. The CVaR
// figure and formula stay visible underneath — this reframes which number is the
// headline, it does not suppress either one. Any real tail exposure (a line actually
// orphaned) keeps the original CVaR-led framing untouched.
function SpendAtRiskBanner({ result }: { result: ScenarioResponse }) {
  const hedging = result.hedging;
  const substitution = result.cost_substitution;
  // Derived from the SERVED fulfilment fields, never from prose. When this is
  // non-null the response contradicts any "zero fulfillment impact" claim.
  const impact = fulfilmentImpact(result);
  const isFullyHedged =
    hedging != null &&
    hedging.fully_hedged === true &&
    hedging.n_lines_orphaned === 0 &&
    substitution != null;

  const quantityNote = (
    <>
      {result.quantity_source === 'explicit' && result.total_units != null && (
        <> Priced on the real build: {result.total_units.toLocaleString()} units.</>
      )}
      {result.quantity_source === 'assumed_one_unit_per_line' && (
        <> Priced at one unit per line — quantities were not supplied, so this is a
        prototype figure, not a build.</>
      )}
    </>
  );

  if (isFullyHedged && substitution) {
    const pct = substitution.baseline_component_cost_usd
      ? (substitution.substitution_delta_usd / substitution.baseline_component_cost_usd) * 100
      : 0;
    const lines = `${substitution.n_lines_repriced} line${substitution.n_lines_repriced === 1 ? '' : 's'}`;
    return (
      <div
        className={`bg-amber-500/5 border rounded-xl p-4 flex items-start gap-4 ${
          impact ? 'border-red-500/40' : 'border-amber-500/30'
        }`}
      >
        <div className="p-2 rounded-lg bg-amber-500/10 shrink-0">
          <ShieldAlert className="w-5 h-5 text-amber-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap gap-x-10 gap-y-3">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wider text-amber-400">
                Substitution Cost · No BOM Line Orphaned
              </div>
              <div className="text-2xl font-bold text-white tabular-nums">
                {usd(substitution.substitution_delta_usd)}
                <span className="text-sm font-semibold text-slate-400 ml-2">
                  ({pct >= 0 ? '+' : ''}{pct.toFixed(1)}%)
                </span>
              </div>
            </div>
            {/* The response's own refutation of "zero fulfillment impact", set at
                the same size as the dollar figure rather than under it. */}
            {impact && <FulfilmentHeadline impact={impact} />}
          </div>

          {/*
            This paragraph used to render `hedging.statement` verbatim. That string
            asserts "Zero fulfillment impact is the correct answer, not a missing
            computation" whenever no line is structurally orphaned — while the SAME
            response reported baseline_fulfillment_p50 = 1.0 against
            scenario_fulfillment_p50 = 0.8. Three confident claims sat 12px above the
            line that refuted them.

            The served statement is now echoed ONLY when the served fulfilment fields
            agree with it. Where they disagree, the page composes the sentence from
            the fields themselves, so it can never again publish a claim its own
            adjacent numbers contradict. (The backend string is being audited
            separately; the display must not depend on that landing.)
          */}
          {impact ? (
            <p className="text-sm text-slate-300 mt-2 leading-relaxed">
              All {hedging.n_bom_lines} of {hedging.n_bom_lines} BOM lines keep at least
              one supplier under this scenario, so no line is orphaned. Procurement spend
              at risk (CVaR-95) is {usd(result.procurement_spend_at_risk_usd)} — baseline
              BOM spend × (CVaR-95 {result.baseline_cvar_95.toFixed(3)} − 1)
              {result.procurement_spend_at_risk_usd === 0
                ? ', which is zero here because no line becomes unavailable'
                : ', the extra emergency-procurement spend in the worst 5% of scenarios'}
              .{' '}
              {/* The ARGUMENT, not the arithmetic. This sentence used to repeat
                  "{impact.headline} ({impact.deltaLabel})" ~12px below the headline
                  that already states it at 2xl. The claim a reader needs here is that
                  a structurally hedged BOM can still lose fulfilment; the figure is
                  directly above, in red, at the size of the dollar number. */}
              <span className="text-red-300 font-medium">
                Zero orphaned lines is not the same as zero impact.
              </span>{' '}
              The Monte Carlo cascade in this same response is what moves median
              fulfilment to the figure above, and re-sourcing {lines} to the
              next-cheapest surviving offer costs the{' '}
              {usd(substitution.substitution_delta_usd)} beside it, broken out below.
              {quantityNote}
            </p>
          ) : (
            <p className="text-sm text-slate-300 mt-2 leading-relaxed">
              {servedProse(hedging.statement)} Procurement spend at risk (CVaR-95) is{' '}
              {usd(result.procurement_spend_at_risk_usd)} — baseline BOM spend × (CVaR-95{' '}
              {result.baseline_cvar_95.toFixed(3)} − 1)
              {result.procurement_spend_at_risk_usd === 0
                ? ', which is zero here because no line becomes unavailable'
                : ', the extra emergency-procurement spend in the worst 5% of scenarios'}
              ; the cost of this outage is re-sourcing {lines} to the next-cheapest
              surviving offer, shown above and broken out below.
              {quantityNote}
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div
      className={`bg-amber-500/5 border rounded-xl p-4 flex items-start gap-4 ${
        impact ? 'border-red-500/40' : 'border-amber-500/30'
      }`}
    >
      <div className="p-2 rounded-lg bg-amber-500/10 shrink-0">
        <ShieldAlert className="w-5 h-5 text-amber-400" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap gap-x-10 gap-y-3">
          <div>
            <div className="text-xs font-semibold uppercase tracking-wider text-amber-400">
              Procurement Spend at Risk · CVaR-95
            </div>
            <div className="text-2xl font-bold text-white tabular-nums">
              {usd(result.procurement_spend_at_risk_usd)}
            </div>
          </div>
          {impact && <FulfilmentHeadline impact={impact} />}
        </div>
        <p className="text-sm text-slate-300 mt-2 leading-relaxed">
          {/*
            The served hedging statement, which this branch never rendered at all.
            `isFullyHedged` above additionally requires `cost_substitution`, and the
            geopolitical endpoint carries no substitution block (it removes no
            supplier, so there is nothing to re-source), so EVERY geopolitical
            response fell through to here and its `hedging.statement` — "A 3.0x
            geopolitical risk spike does not remove any supplier from the catalogue,
            so no line is orphaned by it. Structurally, 1 of 5 lines are
            single-sourced…" — was composed by the API and then thrown away.

            Echoed only where the page is NOT composing its own fulfilment sentence.
            The API appends its own fulfilment clause to this string; printing both
            would put the same pair on screen twice inside one paragraph, which is
            the redundancy this page was just trimmed of.
          */}
          {hedging && !impact && <>{servedProse(hedging.statement)}{' '}</>}
          Extra emergency-procurement spend in the worst-5% of {mcLabel} = baseline BOM
          spend × (CVaR-95 {result.baseline_cvar_95.toFixed(3)} − 1).
          {impact && (
            <> Read it beside the fulfilment figure above: this scenario also moves
            median modelled fulfilment, so the dollar figure prices only what can still
            be sourced.</>
          )}
          {quantityNote}
        </p>
      </div>
    </div>
  );
}

// The three delta cards every scenario tab publishes, plus a FOURTH card whenever
// the served fulfilment fields show a real drop. Fulfilment then gets the same
// visual register as cost, ETA and risk — a headline number with its own delta
// badge — and it is the ONLY place in this block that prints the figure: the cost
// card carries the warning, this card carries the number.
//
// Factored out of the three tabs so the four scenarios can never disagree about
// how the same response is presented.
function ScenarioDeltas({
  result,
  explainEtaImprovement = true,
}: {
  result: ScenarioResponse;
  /** The delivery-target scenario EXISTS to pull the ETA in, so "why did the ETA
   *  improve" is not a surprise there and the note would be noise. */
  explainEtaImprovement?: boolean;
}) {
  const impact = fulfilmentImpact(result);
  // Prefer the API's own `eta_basis` over this file's copy of the same sentence.
  const etaTooltip =
    result.eta_basis && result.eta_basis.trim() ? servedProse(result.eta_basis) : ETA_TOOLTIP;
  const etaImproved = result.eta_delta_days < -0.05;

  return (
    <div className="space-y-3">
      {/* Two-up when the fulfilment card is present, NOT four-up. Measured at 1280
          and 1440: four DeltaCards across gives each ~275-320px of inner width, and
          this card does not survive it — the Total Cost caveat wrapped to five lines,
          "167.61 USD" broke across two, and the ETA badge split "↓ 3.2" from its "d".
          A qualifier is never set smaller than the claim it qualifies, and it is not
          set in a column too narrow to read either. Two-up gives ~630px per card. */}
      <div
        className={`grid grid-cols-1 gap-4 ${
          impact ? 'md:grid-cols-2' : 'md:grid-cols-3'
        }`}
      >
        <DeltaCard
          label="Total Cost"
          baseline={result.baseline_cost_usd}
          scenario={result.scenario_cost_usd}
          delta={result.cost_delta_pct}
          deltaUnit="%"
          unit=" USD"
          decimals={2}
          isBad={true}
          tooltip={COST_TOOLTIP}
          subline={fulfilmentCaveat(result)}
        />
        {/* SECOND, not last. Cost and fulfilment are the pair that has to be read
            together — "this cost covers only what can still be sourced" is unreadable
            if the fulfilment figure it points at is three cards away. At md+ this puts
            the two side by side in the top row; at 390px, where the grid collapses to
            one column, it moves the drop from the fourth card to the second, so a
            reader who never scrolls past the fold still cannot miss it. */}
        {impact && (
          <DeltaCard
            label="Fulfilment (P50)"
            baseline={impact.baselinePct}
            scenario={impact.scenarioPct}
            delta={impact.deltaPts}
            deltaUnit=" pts"
            deltaDecimals={0}
            decimals={0}
            unit="%"
            // A FALLING fulfilment is the bad direction, so the sign convention
            // inverts relative to the cost card.
            isBad={false}
            accent="border-red-500/60"
            tooltip={
              `Median (P50) fulfilment across the ${mcLabel}, straight from ` +
              `baseline_fulfillment_p50 and scenario_fulfillment_p50 in this response. ` +
              `A BOM can be fully hedged — every line still has a supplier — and still ` +
              `lose fulfilment here, because the surviving plan leans on fewer suppliers.`
            }
          />
        )}
        <DeltaCard
          label="Delivery ETA"
          baseline={result.baseline_eta_days}
          scenario={result.scenario_eta_days}
          // eta_delta_days is DAYS, not a percentage.
          delta={result.eta_delta_days}
          deltaUnit=" d"
          unit=" days"
          isBad={true}
          tooltip={etaTooltip}
        />
        <DeltaCard
          label="Risk Score"
          baseline={result.baseline_risk_score}
          scenario={result.scenario_risk_score}
          // risk_delta is a raw 0–1 score difference, not a percentage.
          delta={result.risk_delta}
          deltaUnit=""
          deltaDecimals={3}
          decimals={3}
          unit=""
          isBad={true}
        />
      </div>

      {/* A shorter ETA under a FAILURE scenario reads as a defect at a glance. It
          is not, and the mechanism is in the code: the plan's ETA is the slowest
          line of the plan being priced beside it (`_plan_eta_days` takes the max
          lead time over the distributors the priced plan actually buys from), so
          dropping a cheap-but-distant supplier can pull the whole BOM in while
          pushing the bill up. Both numbers are served; the sentence is composed
          from them. */}
      {explainEtaImprovement && etaImproved && (
        <p className="text-sm text-slate-300 leading-relaxed bg-slate-800/50 border border-slate-700 rounded-lg p-3">
          <span className="font-semibold text-white">
            Delivery gets faster here, and that is the model working, not an error.
          </span>{' '}
          The ETA is the slowest line of the plan priced beside it — a geography-derived
          lead-time estimate for the distributor each line is actually bought from, not an
          observed delivery time. When this scenario forces
          those lines off their cheapest supplier onto the next-cheapest surviving one,
          the replacement is often nearer, so the BOM lands in{' '}
          {result.scenario_eta_days.toFixed(1)} days instead of{' '}
          {result.baseline_eta_days.toFixed(1)} ({Math.abs(result.eta_delta_days).toFixed(1)} days
          sooner) while the cost goes up. Cheap and distant travel together.
        </p>
      )}
    </div>
  );
}

// Rows for the BOM impact table. The API answers per component — real MPN, the
// distributor the line is actually sourced from, and the alternatives that can still
// serve THAT line under the scenario. The page used to build these itself from
// `affected_bom_ids`, stamping the literal string "Primary" on every row and hanging
// the BOM-WIDE alternative list off each one, so a line the outage orphans was shown
// offering ten reroute options it does not have.
function impactRows(
  result: ScenarioResponse,
  mpnById: Record<number, string>,
): AffectedComponentDetail[] {
  if (result.affected_components) return result.affected_components;
  // Only reachable for a response cached before the API carried per-line detail.
  // Say nothing rather than something invented.
  return result.affected_bom_ids.map((id) => ({
    component_id: id,
    mpn: mpnById[id] || `Component ${id}`,
    current_supplier: null,
    alternative_suppliers: [],
  }));
}

export default function ResiliencePage() {
  const [activeTab, setActiveTab] = useState<'distributor' | 'geopolitical' | 'delivery' | 'recommendations'>('distributor');

  // Abort controllers for cancelling in-flight requests
  const abortControllerRef = useRef<AbortController>(new AbortController());

  // Global state: BOM from cart (fetch on mount)
  const [bomComponentIds, setBomComponentIds] = useState<number[]>([]);
  const [mpnById, setMpnById] = useState<Record<number, string>>({});
  const [quantityById, setQuantityById] = useState<Record<number, number>>({});
  const [usingDefaultBom, setUsingDefaultBom] = useState(false);
  const [distributors, setDistributors] = useState<Array<{ id: number; name: string }>>([]);

  // The BOM every scenario on this page runs against has three distinct states, and
  // the page used to render only one of them: a literal, static "Loading the BOM these
  // scenarios run on…" keyed on `bomComponentIds.length === 0`. When the fetch failed
  // that line never changed — no spinner, no error, no retry — so a cold-start failure
  // was indistinguishable from a slow success, forever.
  const [bomLoading, setBomLoading] = useState(true);
  const [bomError, setBomError] = useState<string | null>(null);
  const [bomStartedAt, setBomStartedAt] = useState<number | null>(null);
  const bomSec = useElapsedSeconds(bomStartedAt);

  // Scenario 1: Distributor Failure
  const [selectedDistributorId, setSelectedDistributorId] = useState<number | null>(null);
  const [dfLoading, setDfLoading] = useState(false);
  const [dfError, setDfError] = useState<string | null>(null);
  const [dfResult, setDfResult] = useState<ScenarioResponse | null>(null);

  // Scenario 2: Geopolitical Risk
  const [riskMultiplier, setRiskMultiplier] = useState(1.0);
  const [grLoading, setGrLoading] = useState(false);
  const [grError, setGrError] = useState<string | null>(null);
  const [grResult, setGrResult] = useState<ScenarioResponse | null>(null);

  // Scenario 3: Delivery Target
  //
  // This default has been wrong twice, in opposite directions, for the same reason:
  // the page used to publish a baseline ETA of ~2.8 days that described a DIFFERENT
  // plan from the one it priced. `_bom_eta_days` took the fastest supplier in the
  // catalogue per line; `_price_bom` bought the cheapest. Four of five lines on the
  // demo cart price to Singapore, so the real baseline is 26.6 days.
  //
  // Against the false 2.8 a 14-day target looked non-binding, so it was lowered to 2.
  // Against the true 26.6 a 2-day target is unreachable. Measured on the demo cart,
  // 14 days is the value that shows the trade honestly: ETA 26.6 -> 9.2 (-17.4 d) for
  // +94.7% cost. Do not re-tune this without re-measuring the baseline it implies.
  const [targetDeliveryDays, setTargetDeliveryDays] = useState(14);
  const [dtLoading, setDtLoading] = useState(false);
  const [dtError, setDtError] = useState<string | null>(null);
  const [dtResult, setDtResult] = useState<DeliveryTargetResponse | null>(null);

  // Scenario 4: Recommendations (criticality sweep + dual-sourcing + tornado)
  const [recLoading, setRecLoading] = useState(false);
  const [recError, setRecError] = useState<string | null>(null);
  const [recHasRun, setRecHasRun] = useState(false);
  const [sweepResult, setSweepResult] = useState<CriticalitySweepResponse | null>(null);
  const [dualSourcingResult, setDualSourcingResult] = useState<DualSourcingResponse | null>(null);
  const [tornadoResult, setTornadoResult] = useState<SensitivityResponse | null>(null);

  // Load initial data (BOM from cart, distributors list)
  const loadInitialData = useCallback(async () => {
    setBomLoading(true);
    setBomError(null);
    setBomStartedAt(performance.now());
    async function load() {
      try {
        // Fetch distributors list
        const response = await distributorsAPI.list();
        const dists = response.data || [];
        setDistributors(
          dists.map((d: any) => ({
            id: d.id,
            name: d.name,
          }))
        );
      } catch (e) {
        console.error("Failed to load distributors:", e);
      }

      // Build the BOM the scenarios run against. Prefer the user's cart; if it is
      // empty or unauthenticated, fall back to a DEMONSTRATIVE default BOM.
      //
      // The old fallback took the first 6 components by id — ~$25 of total spend,
      // all multi-sourced — so the flagship scenario returned 0.0% cost delta,
      // 0.0-day ETA delta, 0.000 risk delta and 100% fulfilment in every case. The
      // page's headline feature demoed as a wall of zeros.
      //
      // Instead: seed from the network's real single-source exposure — the components
      // where losing one distributor actually breaks something — plus the highest-value
      // multi-sourced parts for contrast. Nothing is hardcoded; the ids are derived at
      // runtime from the catalogue's own offer counts (see below for why not from
      // /benchmark/single-source-components).
      try {
        const ids: number[] = [];
        const mpnMap: Record<number, string> = {};
        const qtyMap: Record<number, number> = {};
        let catalogueFailed = false;
        const cartDistributorCounts = new Map<number, number>();
        try {
          const cart = await cartAPI.get();
          for (const item of cart.data || []) {
            ids.push(item.component_id);
            if (item.mpn) mpnMap[item.component_id] = item.mpn;
            qtyMap[item.component_id] = item.quantity ?? 1;
            if (item.distributor_id != null) {
              cartDistributorCounts.set(
                item.distributor_id,
                (cartDistributorCounts.get(item.distributor_id) ?? 0) + 1
              );
            }
          }
        } catch {
          // not logged in / empty cart — fall through to default BOM
        }

        // Pre-select the distributor this cart leans on hardest, so "Simulate
        // Failure" is enabled on arrival instead of sitting greyed out until the
        // user guesses which of 92 distributors is worth failing.
        if (cartDistributorCounts.size > 0) {
          let topId: number | null = null;
          let topCount = 0;
          for (const [distId, count] of cartDistributorCounts) {
            if (count > topCount) {
              topCount = count;
              topId = distId;
            }
          }
          if (topId != null) setSelectedDistributorId(topId);
        }

        if (ids.length === 0) {
          try {
            const comps = await componentsAPI.list();
            const list: Array<{
              id: number;
              mpn?: string;
              num_offers?: number;
              min_price?: number | null;
            }> = comps.data?.items || comps.data || [];

            // NOTE: we deliberately do NOT use GET /benchmark/single-source-components
            // here. That endpoint currently over-reports — it lists components such as
            // 170/174/180/183/186 as single-source when each in fact has 3–4 distributor
            // offers, so seeding from it produced a BOM whose "sole supplier" failure
            // changed nothing (risk delta 0.000, fulfilment flat at 100%). The
            // catalogue's own `num_offers` agrees with GET /components/{id}/offers, so
            // we trust that instead. (Backend defect, out of scope for this pass.)
            const trulySingleSource = list
              .filter((c) => c.num_offers === 1)
              .sort((a, b) => (b.min_price ?? 0) - (a.min_price ?? 0))
              .slice(0, 16);

            // Resolve each one's sole distributor, then fail the distributor that owns
            // the most of them — the most instructive single failure in the network.
            const sole = await Promise.all(
              trulySingleSource.map(async (c) => {
                try {
                  const res = await componentsAPI.offers(c.id);
                  const offers = Array.isArray(res.data) ? res.data : [];
                  return offers.length === 1
                    ? { component: c, distributorId: offers[0].distributor_id as number }
                    : null;
                } catch {
                  return null;
                }
              })
            );

            const byDistributor = new Map<number, typeof trulySingleSource>();
            for (const s of sole) {
              if (!s) continue;
              const bucket = byDistributor.get(s.distributorId) || [];
              bucket.push(s.component);
              byDistributor.set(s.distributorId, bucket);
            }

            let seededDistributorId: number | null = null;
            let best: typeof trulySingleSource = [];
            for (const [distId, bucket] of byDistributor) {
              if (bucket.length > best.length) {
                best = bucket;
                seededDistributorId = distId;
              }
            }

            for (const c of best.slice(0, 5)) {
              ids.push(c.id);
              if (c.mpn) mpnMap[c.id] = c.mpn;
              qtyMap[c.id] = 1;
            }

            // Add the highest-value multi-sourced parts for contrast: the BOM should
            // not be uniformly fragile, and the re-sourcing story needs lines that
            // actually survive and move to another distributor.
            const multiSource = list
              .filter((c) => (c.num_offers ?? 0) > 1)
              .sort((a, b) => (b.min_price ?? 0) - (a.min_price ?? 0));
            for (const c of multiSource) {
              if (ids.length >= 8) break;
              if (ids.includes(c.id)) continue;
              ids.push(c.id);
              if (c.mpn) mpnMap[c.id] = c.mpn;
              qtyMap[c.id] = 1;
            }

            // Pre-select the distributor whose failure this BOM is built to expose, so
            // "Simulate Failure" returns a real result on the first click.
            if (seededDistributorId != null) setSelectedDistributorId(seededDistributorId);
          } catch (e) {
            // catalogue unavailable — leave the BOM empty rather than inventing one,
            // and remember WHY it is empty so the page can say so.
            console.error("Failed to load the catalogue for the default BOM:", e);
            catalogueFailed = true;
          }
          setUsingDefaultBom(true);
        }

        setBomComponentIds(ids);
        setMpnById(mpnMap);
        setQuantityById(qtyMap);
        // An empty BOM is never a working page — but "the request failed" and "the
        // catalogue is genuinely empty" are different sentences and get different ones.
        if (ids.length === 0) {
          setBomError(
            catalogueFailed
              ? "The component catalogue could not be loaded, so there is no BOM for these scenarios to run on. The API runs on a free tier and can take up to ~2 minutes to wake from sleep."
              : "The cart is empty and the catalogue returned no components, so there is no BOM for these scenarios to run on."
          );
        }
      } catch (e) {
        console.error("Failed to load BOM:", e);
        setBomError(
          "Loading the BOM failed, so no scenario can run yet. The API runs on a free tier and can take up to ~2 minutes to wake from sleep."
        );
      } finally {
        setBomLoading(false);
        setBomStartedAt(null);
      }
    }
    await load();
  }, []);

  useEffect(() => {
    void loadInitialData();
  }, [loadInitialData]);

  // Cleanup: cancel pending requests on unmount
  useEffect(() => {
    return () => {
      abortControllerRef.current.abort();
    };
  }, []);

  // The BOM these scenarios are priced on, WITH its quantities. Posting bare
  // `bom_component_ids` makes the API price one unit per line, which is why the
  // summary tiles read $4.04 above a $166.94 line-by-line table for the same cart.
  const bomItems: BomLine[] = useMemo(
    () => bomComponentIds.map((id) => ({ component_id: id, quantity: quantityById[id] ?? 1 })),
    [bomComponentIds, quantityById],
  );

  const onSimulateDistributorFailure = useCallback(async () => {
    if (!selectedDistributorId) {
      setDfError("Please select a distributor");
      return;
    }
    setDfLoading(true);
    setDfError(null);

    // Reset abort controller for new request
    abortControllerRef.current = new AbortController();

    try {
      const result = await resilienceAPI.distributorFailure(
        {
          distributor_id: selectedDistributorId,
          items: bomItems,
        },
        abortControllerRef.current.signal
      );
      setDfResult(result);
    } catch (e) {
      const message = (e as Error).message;
      if (message.includes("timeout")) {
        setDfError("Simulation took too long (>30s). Try a smaller BOM or try again.");
      } else if (message.includes("API error")) {
        setDfError("Backend error. Check that the server is running.");
      } else if (message.includes("aborted")) {
        setDfError(null); // Silently clear if aborted (unmount)
      } else {
        setDfError(message || "Unknown error");
      }
      // Still clear result to reset state
      setDfResult(null);
    } finally {
      setDfLoading(false);
    }
  }, [selectedDistributorId, bomItems]);

  const onSimulateGeopoliticalRisk = async () => {
    setGrLoading(true);
    setGrError(null);

    // Reset abort controller for new request
    abortControllerRef.current = new AbortController();

    try {
      const result = await resilienceAPI.geopoliticalRisk(
        {
          risk_multiplier: riskMultiplier,
          items: bomItems,
        },
        abortControllerRef.current.signal
      );
      setGrResult(result);
    } catch (e) {
      const message = (e as Error).message;
      if (message.includes("timeout")) {
        setGrError("Simulation took too long (>30s). Try again.");
      } else if (message.includes("aborted")) {
        setGrError(null); // Silently clear if aborted
      } else {
        setGrError(message || "Unknown error");
      }
      setGrResult(null);
    } finally {
      setGrLoading(false);
    }
  };

  const onSimulateDeliveryTarget = async () => {
    setDtLoading(true);
    setDtError(null);

    // Reset abort controller for new request
    abortControllerRef.current = new AbortController();

    try {
      const result = await resilienceAPI.deliveryTarget(
        {
          target_delivery_days: targetDeliveryDays,
          items: bomItems,
        },
        abortControllerRef.current.signal
      );
      setDtResult(result);
    } catch (e) {
      const message = (e as Error).message;
      if (message.includes("timeout")) {
        setDtError("Simulation took too long (>30s). Try again.");
      } else if (message.includes("aborted")) {
        setDtError(null); // Silently clear if aborted
      } else {
        setDtError(message || "Unknown error");
      }
      setDtResult(null);
    } finally {
      setDtLoading(false);
    }
  };

  const onAnalyzeRecommendations = async () => {
    setRecLoading(true);
    setRecError(null);

    // Reset abort controller for new request
    abortControllerRef.current = new AbortController();
    const { signal } = abortControllerRef.current;

    try {
      const [sweep, dualSourcing, tornado] = await Promise.all([
        resilienceAPI.criticalitySweep({}, signal), // network-wide by default
        resilienceAPI.dualSourcingPlan({}, signal), // network-wide by default
        resilienceAPI.sensitivity({ bom_component_ids: bomComponentIds, metric: 'cost' }, signal),
      ]);
      setSweepResult(sweep);
      setDualSourcingResult(dualSourcing);
      setTornadoResult(tornado);
      setRecHasRun(true);
    } catch (e) {
      const message = (e as Error).message;
      if (message.includes('timeout')) {
        setRecError('Analysis took too long (>30s). Try again.');
      } else if (message.includes('aborted')) {
        setRecError(null); // Silently clear if aborted
      } else {
        setRecError(message || 'Unknown error');
      }
    } finally {
      setRecLoading(false);
    }
  };

  // Auto-run the flagship scenario on mount, so /resilience never lands as an empty
  // page behind a button the visitor has to find. Same pattern as FrontierPage: the
  // first result arrives on its own, and the loading state is what a *changed* input
  // looks like afterwards. It waits for both inputs, and both are derived from real
  // data — the BOM from the cart (or the catalogue's own single-source exposure) and
  // the distributor the BOM leans on hardest — so nothing is fabricated to make the
  // page fill up.
  const autoRanRef = useRef(false);
  useEffect(() => {
    if (autoRanRef.current) return;
    if (bomComponentIds.length === 0 || selectedDistributorId == null) return;
    autoRanRef.current = true;
    void onSimulateDistributorFailure();
  }, [bomComponentIds, selectedDistributorId, onSimulateDistributorFailure]);

  // Auto-trigger the recommendations analysis the first time the tab is opened,
  // once the BOM (needed for the tornado's bom_component_ids) has loaded.
  useEffect(() => {
    if (activeTab === 'recommendations' && !recHasRun && !recLoading && bomComponentIds.length > 0) {
      onAnalyzeRecommendations();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, bomComponentIds]);

  return (
    <div className="container mx-auto px-6 py-8 overflow-y-auto h-full">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6 }}
        className="mb-8"
      >
        <h1 className="text-4xl font-bold text-white mb-2">Resilience Scenarios</h1>
        <p className="text-slate-400">
          Explore supply chain trade-offs: what happens if a key distributor fails, risk spikes, or delivery accelerates?
        </p>
        {/* Be explicit about which BOM the numbers below describe — an interviewer
            should never have to guess whether they're looking at their own cart. */}
        {bomComponentIds.length > 0 && (
          <p className="text-xs text-slate-400 mt-2">
            {usingDefaultBom ? (
              <>
                Running on a demo BOM of {bomComponentIds.length} components, seeded from the
                network's real single-source exposure so a distributor failure has a visible
                effect. Add items to your cart to run these scenarios on your own BOM.
              </>
            ) : (
              <>Running on your cart: {bomComponentIds.length} components.</>
            )}
          </p>
        )}
      </motion.div>

      {/* Tab Navigation. flex-wrap: at 390px this row was 560px wide inside a 342px
          box, panning the whole page 194px sideways and pushing "Recommendations"
          entirely off-screen. Wrapping to two rows at narrow widths costs nothing at
          >=768px, where all four already fit on one line. */}
      <div className="flex flex-wrap gap-2 mb-6 border-b border-slate-700">
        <button
          onClick={() => setActiveTab('distributor')}
          className={`px-4 py-2 min-h-[44px] font-semibold border-b-2 transition ${
            activeTab === 'distributor'
              ? 'text-white border-blue-500'
              : 'text-slate-400 border-transparent hover:text-white hover:border-blue-500'
          }`}
        >
          Distributor Failure
        </button>
        <button
          onClick={() => setActiveTab('geopolitical')}
          className={`px-4 py-2 min-h-[44px] font-semibold border-b-2 transition ${
            activeTab === 'geopolitical'
              ? 'text-white border-blue-500'
              : 'text-slate-400 border-transparent hover:text-white hover:border-blue-500'
          }`}
        >
          Geopolitical Risk
        </button>
        <button
          onClick={() => setActiveTab('delivery')}
          className={`px-4 py-2 min-h-[44px] font-semibold border-b-2 transition ${
            activeTab === 'delivery'
              ? 'text-white border-blue-500'
              : 'text-slate-400 border-transparent hover:text-white hover:border-blue-500'
          }`}
        >
          Delivery Acceleration
        </button>
        <button
          onClick={() => setActiveTab('recommendations')}
          className={`px-4 py-2 min-h-[44px] font-semibold border-b-2 transition ${
            activeTab === 'recommendations'
              ? 'text-white border-blue-500'
              : 'text-slate-400 border-transparent hover:text-white hover:border-blue-500'
          }`}
        >
          Recommendations
        </button>
      </div>

      {/* Tab Content */}
      <div>
        {/* Scenario 1: Distributor Failure */}
        {activeTab === 'distributor' && (
          <div className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <ScenarioCard title="Simulate Failure" loading={dfLoading} error={dfError}>
                <DistributorSelector
                  distributors={distributors}
                  selectedDistributorId={selectedDistributorId}
                  onSelect={setSelectedDistributorId}
                  onSimulate={onSimulateDistributorFailure}
                  loading={dfLoading}
                />
              </ScenarioCard>
            </div>

            {/* Skeleton while the very first (auto-run) scenario is solving. */}
            {!dfResult && dfLoading && (
              <div className="flex flex-col items-center justify-center h-56 gap-4">
                <div className="w-8 h-8 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
                <p className="text-sm text-slate-500">
                  Running the Monte Carlo cascade on this BOM…
                </p>
              </div>
            )}

            {/* Nothing to show and not loading: the BOM is still loading, the BOM
                failed to load, or we are waiting on a click (the first solve's own
                error is rendered in the card above). Three states, three messages —
                the old single line claimed "loading" in all of them, permanently. */}
            {!dfResult && !dfLoading && !dfError && (
              bomLoading ? (
                <div className="flex flex-col items-center gap-3 py-4">
                  <div className="flex items-center gap-2 text-sm text-slate-500">
                    <div className="w-4 h-4 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
                    Loading the BOM these scenarios run on{bomSec > 0 ? ` — ${bomSec}s` : '…'}
                  </div>
                  {bomSec >= 3 && (
                    <div className="w-full max-w-xl">
                      <WakeNotice requestSec={bomSec} />
                    </div>
                  )}
                </div>
              ) : bomError ? (
                <div
                  className="max-w-xl bg-red-900/20 border border-red-700/50 rounded-lg p-4"
                  data-testid="bom-error"
                >
                  <div className="text-sm font-semibold text-red-300">No BOM to run these scenarios on</div>
                  <div className="text-xs text-red-200/80 mt-1.5">{bomError}</div>
                  <button
                    onClick={() => { void loadInitialData(); }}
                    className="mt-3 inline-flex items-center gap-1.5 bg-red-600/80 hover:bg-red-500 text-white text-xs font-medium px-3 py-1.5 rounded transition-colors"
                  >
                    Retry
                  </button>
                </div>
              ) : (
                <div className="text-sm text-slate-500">
                  Pick a distributor to fail and run the scenario.
                </div>
              )
            )}

            <AnimatePresence>
              {dfResult && (
                <motion.div
                  initial={{ opacity: 0, y: 16 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.4 }}
                  className="space-y-6"
                >
                  {/* Procurement spend at risk (CVaR-95 → $) */}
                  <SpendAtRiskBanner result={dfResult} />

                  {/* Delta cards — cost / ETA / risk, plus fulfilment whenever the
                      served fulfilment fields show a real drop. */}
                  <ScenarioDeltas result={dfResult} />

                  {/* Monte Carlo Chart */}
                  <MonteCarloChart
                    baselineP10={dfResult.baseline_fulfillment_p10}
                    baselineP50={dfResult.baseline_fulfillment_p50}
                    baselineP90={dfResult.baseline_fulfillment_p90}
                    scenarioP10={dfResult.scenario_fulfillment_p10}
                    scenarioP50={dfResult.scenario_fulfillment_p50}
                    scenarioP90={dfResult.scenario_fulfillment_p90}
                    title="Fulfillment Rate (P10/P50/P90)"
                  />

                  {/* BOM Impact Table */}
                  <BOMImpactTable
                    affectedComponents={impactRows(dfResult, mpnById)}
                    emptyLabel={affectedEmptyLabel(dfResult)}
                    title="Affected Components & Rerouting Options"
                    emptyMessage={
                      'No BOM line loses every supplier when this distributor goes dark. ' +
                      'The cost impact is the substitution to the next-cheapest surviving ' +
                      'offer, priced line by line below.'
                    }
                  />

                  {/* Per-line base-vs-scenario cost breakdown — the one idea worth
                      keeping from the deleted Digital Twin page, rebuilt on real
                      offer prices with actual re-sourcing. */}
                  {selectedDistributorId != null && (
                    <BomCostBreakdownTable
                      bomComponentIds={bomComponentIds}
                      mpnById={mpnById}
                      quantityById={quantityById}
                      failedDistributorId={selectedDistributorId}
                      failedDistributorName={
                        distributors.find((d) => d.id === selectedDistributorId)?.name ??
                        `Distributor ${selectedDistributorId}`
                      }
                    />
                  )}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* Scenario 2: Geopolitical Risk */}
        {activeTab === 'geopolitical' && (
          <div className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <ScenarioCard title="Risk Settings" loading={grLoading} error={grError}>
                <GeopoliticalRiskSelector
                  riskMultiplier={riskMultiplier}
                  onRiskChange={setRiskMultiplier}
                  onSimulate={onSimulateGeopoliticalRisk}
                  loading={grLoading}
                />
              </ScenarioCard>
            </div>

            <AnimatePresence>
              {grResult && (
                <motion.div
                  initial={{ opacity: 0, y: 16 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.4 }}
                  className="space-y-6"
                >
                  {/* Procurement spend at risk (CVaR-95 → $) */}
                  <SpendAtRiskBanner result={grResult} />

                  {/* Delta cards — cost / ETA / risk, plus fulfilment whenever the
                      served fulfilment fields show a real drop. */}
                  <ScenarioDeltas result={grResult} />

                  {/* Monte Carlo Chart */}
                  <MonteCarloChart
                    baselineP10={grResult.baseline_fulfillment_p10}
                    baselineP50={grResult.baseline_fulfillment_p50}
                    baselineP90={grResult.baseline_fulfillment_p90}
                    scenarioP10={grResult.scenario_fulfillment_p10}
                    scenarioP50={grResult.scenario_fulfillment_p50}
                    scenarioP90={grResult.scenario_fulfillment_p90}
                    title="Fulfillment Rate (P10/P50/P90)"
                  />

                  {/* BOM Impact Table */}
                  <BOMImpactTable
                    affectedComponents={impactRows(grResult, mpnById)}
                    emptyLabel={affectedEmptyLabel(grResult)}
                    title="Affected Components & Rerouting Options"
                    emptyMessage={
                      'No BOM line crosses into a higher risk tier at this multiplier. ' +
                      'The spike still flows through the emergency-procurement premium ' +
                      'in the cards above.'
                    }
                  />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* Scenario 3: Delivery Target */}
        {activeTab === 'delivery' && (
          <div className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <ScenarioCard title="Delivery Target" loading={dtLoading} error={dtError}>
                <DeliveryTargetSelector
                  targetDeliveryDays={targetDeliveryDays}
                  onTargetChange={setTargetDeliveryDays}
                  onSimulate={onSimulateDeliveryTarget}
                  loading={dtLoading}
                />
              </ScenarioCard>
            </div>

            <AnimatePresence>
              {dtResult && (
                <motion.div
                  initial={{ opacity: 0, y: 16 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.4 }}
                  className="space-y-6"
                >
                  {/* Procurement spend at risk (CVaR-95 → $) */}
                  <SpendAtRiskBanner result={dtResult} />

                  {/* Delta cards — cost / ETA / risk, plus fulfilment whenever the
                      served fulfilment fields show a real drop. */}
                  <ScenarioDeltas result={dtResult}
                    explainEtaImprovement={false} />

                  {/* Monte Carlo Chart */}
                  <MonteCarloChart
                    baselineP10={dtResult.baseline_fulfillment_p10}
                    baselineP50={dtResult.baseline_fulfillment_p50}
                    baselineP90={dtResult.baseline_fulfillment_p90}
                    scenarioP10={dtResult.scenario_fulfillment_p10}
                    scenarioP50={dtResult.scenario_fulfillment_p50}
                    scenarioP90={dtResult.scenario_fulfillment_p90}
                    title="Fulfillment Rate (P10/P50/P90)"
                  />

                  {/* BOM Impact Table + Suppliers capable/cannot meet */}
                  <BOMImpactTable
                    affectedComponents={impactRows(dtResult, mpnById)}
                    emptyLabel={affectedEmptyLabel(dtResult)}
                    title="Lines That Miss the Delivery Window"
                    emptyMessage={
                      `Every BOM line has at least one supplier that can deliver inside ` +
                      `the ${targetDeliveryDays}-day window.`
                    }
                  />

                  {/* Suppliers capable and cannot meet */}
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    {/* Suppliers capable */}
                    <div className="bg-slate-800/70 border border-slate-700 rounded-xl p-6 backdrop-blur-sm">
                      <h3 className="text-lg font-semibold text-white mb-1">
                        Suppliers Capable
                        <span className="ml-2 text-sm font-normal text-slate-500">
                          {dtResult.suppliers_capable.length}
                        </span>
                      </h3>
                      <p className="text-xs text-slate-500 mb-4">
                        Can hit the {targetDeliveryDays}-day window. Lead time is derived from real
                        distributor geography; the premium is the expedite surcharge required, 0%
                        where the supplier already meets the window.
                      </p>
                      <div className="space-y-3 max-h-[420px] overflow-y-auto">
                        {dtResult.suppliers_capable.length === 0 && (
                          <div className="text-sm text-slate-400">
                            No supplier in the catalogue can meet a {targetDeliveryDays}-day window
                            for this BOM.
                          </div>
                        )}
                        {dtResult.suppliers_capable.map((sup, idx) => (
                          <div
                            key={idx}
                            className="bg-slate-800/50 border border-green-700 rounded p-3 flex justify-between items-center gap-3"
                          >
                            <div className="min-w-0">
                              {/* Was `truncate` — clipped to "Component ..." / "Weyland
                                  Electronics ..." for real distributor names this dataset
                                  actually has. The supplier's identity is the whole point
                                  of the row, so it wraps instead of clipping; `title` is a
                                  hover bonus on desktop, not the only way to read it. */}
                              <div className="text-white font-medium break-words" title={sup.name}>{sup.name}</div>
                              {/* The API returns {name, lead_time_days, cost_adjustment_pct}.
                                  It has never returned a per-component average cost — reading
                                  `cost_per_component_avg` here white-screened the whole app. */}
                              <div className="text-sm text-slate-400">
                                Lead time: {sup.lead_time_days.toFixed(1)} days
                                {' | '}
                                {sup.cost_adjustment_pct > 0
                                  ? `Expedite premium: +${sup.cost_adjustment_pct.toFixed(1)}%`
                                  : 'No expedite premium'}
                              </div>
                            </div>
                            <div className="bg-green-500/20 text-green-300 px-3 py-1 rounded text-sm font-semibold shrink-0">
                              Viable
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Suppliers cannot meet */}
                    <div className="bg-slate-800/70 border border-slate-700 rounded-xl p-6 backdrop-blur-sm">
                      <h3 className="text-lg font-semibold text-white mb-1">
                        Cannot Meet Target
                        <span className="ml-2 text-sm font-normal text-slate-500">
                          {dtResult.suppliers_cannot_meet.length}
                        </span>
                      </h3>
                      <p className="text-xs text-slate-500 mb-4">
                        Ruled out for the {targetDeliveryDays}-day window.
                      </p>
                      <div className="space-y-3 max-h-[420px] overflow-y-auto">
                        {dtResult.suppliers_cannot_meet.length === 0 && (
                          <div className="text-sm text-slate-400">
                            Every supplier in the catalogue can meet this window.
                          </div>
                        )}
                        {dtResult.suppliers_cannot_meet.map((sup, idx) => (
                          <div
                            key={idx}
                            className="bg-slate-800/50 border border-red-700 rounded p-3 flex justify-between items-center gap-3"
                          >
                            <div className="min-w-0">
                              <div className="text-white font-medium break-words" title={sup.name}>{sup.name}</div>
                              <div className="text-sm text-slate-400">
                                Min lead time: {sup.min_lead_time_days.toFixed(1)} days |{' '}
                                {sup.reason.replace(/_/g, ' ')}
                              </div>
                            </div>
                            <div className="bg-red-500/20 text-red-300 px-3 py-1 rounded text-sm font-semibold shrink-0">
                              Not Viable
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* Scenario 4: Recommendations */}
        {activeTab === 'recommendations' && (
          <div className="space-y-6">
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <ScenarioCard title="Recommendation Engine" loading={recLoading} error={recError}>
                <div className="space-y-4">
                  <p className="text-sm text-slate-400">
                    Ranks network-wide single-source exposure, dual-sourcing payoff, and this
                    BOM's cost sensitivity to the key model levers.
                  </p>
                  <button
                    onClick={onAnalyzeRecommendations}
                    disabled={recLoading}
                    className="w-full px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-500 text-white font-semibold transition"
                  >
                    {recLoading ? 'Analyzing...' : recHasRun ? 'Re-analyze' : 'Analyze'}
                  </button>
                </div>
              </ScenarioCard>
            </div>

            <AnimatePresence>
              {sweepResult && dualSourcingResult && tornadoResult && (
                <motion.div
                  initial={{ opacity: 0, y: 16 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.4 }}
                  className="space-y-6"
                >
                  <CriticalitySweepTable
                    entries={sweepResult.entries}
                    maxSpendAtRisk={sweepResult.max_spend_at_risk_usd}
                    networkWide={sweepResult.network_wide}
                  />

                  <DualSourcingTable
                    entries={dualSourcingResult.entries}
                    noRegretCount={dualSourcingResult.no_regret_count}
                    hedgeCount={dualSourcingResult.hedge_count}
                    supplierDevelopmentCount={dualSourcingResult.supplier_development_count}
                  />

                  <TornadoChart
                    baselineOutput={tornadoResult.baseline_output}
                    metric={tornadoResult.metric}
                    bars={tornadoResult.bars}
                  />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}
      </div>
    </div>
  );
}
