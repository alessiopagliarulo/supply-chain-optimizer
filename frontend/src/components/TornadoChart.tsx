import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  LabelList,
  type LabelProps,
} from 'recharts';
import { type TornadoBar } from '../services/api';

interface TornadoChartProps {
  baselineOutput: number;
  metric: string;
  bars: TornadoBar[];
}

function formatValue(value: number, metric: string): string {
  if (metric === 'cost') {
    return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }
  // cvar (or any other metric) is a unitless multiplier
  return value.toFixed(3);
}

interface ChartRow {
  lever: string;
  base: number;
  span: number;
  startLabel: string;
  startValue: number;
  endLabel: string;
  endValue: number;
  spread: number;
}

// Custom label placed at the boundary between the transparent "base" segment
// and the visible "span" segment (i.e. the low end of the bar's real range).
// Reads the pre-computed label/value off the row by index (not the raw
// stacked dataKey) so it is correct regardless of which side — low or high —
// landed at the start of the visible span.
function makeStartLabel(rows: ChartRow[], metric: string) {
  return function StartLabel(props: LabelProps) {
    const x = Number(props.x);
    const y = Number(props.y);
    const width = Number(props.width);
    const height = Number(props.height);
    const row = props.index === undefined ? undefined : rows[props.index];
    if (!row) return null;
    return (
      <text x={x + width - 6} y={y + height / 2} dy={4} fontSize={11} fill="#94a3b8" textAnchor="end">
        {row.startLabel}: {formatValue(row.startValue, metric)}
      </text>
    );
  };
}

// Custom label at the far (high) end of the visible span.
function makeEndLabel(rows: ChartRow[], metric: string) {
  return function EndLabel(props: LabelProps) {
    const x = Number(props.x);
    const y = Number(props.y);
    const width = Number(props.width);
    const height = Number(props.height);
    const row = props.index === undefined ? undefined : rows[props.index];
    if (!row) return null;
    return (
      <text x={x + width + 6} y={y + height / 2} dy={4} fontSize={11} fill="#cbd5e1" textAnchor="start">
        {row.endLabel}: {formatValue(row.endValue, metric)}
      </text>
    );
  };
}

export function TornadoChart({ baselineOutput, metric, bars }: TornadoChartProps) {
  const chartData: ChartRow[] = bars.map((b) => {
    const segStart = Math.min(b.low_output, b.high_output);
    const segEnd = Math.max(b.low_output, b.high_output);
    const lowIsStart = b.low_output <= b.high_output;
    return {
      lever: b.lever,
      base: segStart,
      span: segEnd - segStart,
      startLabel: lowIsStart ? b.low_label : b.high_label,
      startValue: segStart,
      endLabel: lowIsStart ? b.high_label : b.low_label,
      endValue: segEnd,
      spread: b.spread,
    };
  });

  const allValues = bars.flatMap((b) => [b.low_output, b.high_output]).concat([baselineOutput]);
  const dataMin = Math.min(...allValues);
  const dataMax = Math.max(...allValues);
  const range = dataMax - dataMin;
  const pad = range > 0 ? range * 0.25 : Math.max(Math.abs(dataMax), 1) * 0.1;
  const domain: [number, number] = [dataMin - pad, dataMax + pad];

  const metricLabel = metric === 'cost' ? 'Landed Cost' : metric === 'cvar' ? 'Tail-Risk CVaR-95' : metric;

  // A tornado where every bar has zero width is not a chart — it is a row of
  // invisible slivers next to the baseline line, which reads as "the chart is
  // broken" rather than "no lever moves this BOM". Say the latter explicitly.
  const allFlat = chartData.length > 0 && chartData.every((r) => r.span === 0);

  return (
    <div className="bg-slate-800/70 border border-slate-700 rounded-xl p-6 backdrop-blur-sm">
      <h3 className="text-lg font-semibold text-white mb-1">Sensitivity Tornado</h3>
      <p className="text-sm text-slate-400 mb-4">
        One-way sensitivity of {metricLabel} to each model lever, holding all others at baseline.
      </p>
      {chartData.length === 0 ? (
        <div className="py-8 text-center text-slate-500 text-sm">No levers available for this BOM.</div>
      ) : allFlat ? (
        <div className="py-8 px-4 text-center text-sm">
          <div className="text-slate-300 font-medium mb-1">
            All {chartData.length} levers are flat on this BOM.
          </div>
          <div className="text-slate-500">
            Sweeping every lever across its full range leaves {metricLabel} unchanged at{' '}
            {formatValue(baselineOutput, metric)}. That is a real (and uninteresting) result,
            not a rendering failure — this BOM is too small or too well-sourced for any single
            lever to bite. Try a larger BOM.
          </div>
          <ul className="mt-3 text-xs text-slate-500 flex flex-wrap gap-x-3 gap-y-1 justify-center">
            {chartData.map((r) => (
              <li key={r.lever} className="px-2 py-0.5 rounded bg-slate-900/60 border border-slate-700">
                {r.lever}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={Math.max(240, chartData.length * 56)}>
          <BarChart
            data={chartData}
            layout="vertical"
            margin={{ top: 10, right: 140, left: 100, bottom: 10 }}
            barCategoryGap={18}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#334155" horizontal={false} />
            <XAxis
              type="number"
              domain={domain}
              stroke="#94a3b8"
              tickFormatter={(v) => formatValue(v, metric)}
            />
            <YAxis type="category" dataKey="lever" stroke="#94a3b8" width={100} />
            <Tooltip
              contentStyle={{
                backgroundColor: '#1e293b',
                border: '1px solid #475569',
                borderRadius: '8px',
              }}
              formatter={(_value, _name, props) => {
                const row: ChartRow | undefined = props?.payload;
                if (!row) return ['', ''];
                return [
                  `${row.startLabel}: ${formatValue(row.startValue, metric)}  →  ${row.endLabel}: ${formatValue(row.endValue, metric)}`,
                  'Range',
                ];
              }}
              labelFormatter={(label) => `Lever: ${label}`}
            />
            <ReferenceLine
              x={baselineOutput}
              stroke="#facc15"
              strokeDasharray="4 4"
              label={{ value: 'Baseline', position: 'top', fill: '#facc15', fontSize: 11 }}
            />
            <Bar dataKey="base" stackId="tornado" fill="transparent" isAnimationActive={false}>
              <LabelList dataKey="base" content={makeStartLabel(chartData, metric)} />
            </Bar>
            <Bar
              dataKey="span"
              stackId="tornado"
              fill="#60a5fa"
              radius={[4, 4, 4, 4]}
              isAnimationActive={false}
            >
              <LabelList dataKey="span" content={makeEndLabel(chartData, metric)} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
