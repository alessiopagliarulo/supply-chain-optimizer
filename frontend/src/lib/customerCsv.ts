import type { PlanNode } from '../services/api';

/**
 * The customer CSV the Route Plan page accepts.
 *
 * A header row naming these columns (any order, case-insensitive; extra columns such
 * as an id or name are ignored), then one row per stop. The FIRST data row is the
 * depot: its demand and service_time must be 0, and its ready/due window is the
 * planning horizon (vehicles leave no earlier than ready and are back by due).
 * Time windows are on service start, in the same units as the x/y distances.
 */
export const CSV_COLUMNS = ['x', 'y', 'demand', 'ready', 'due', 'service_time'] as const;

type Column = (typeof CSV_COLUMNS)[number];

export const CSV_COLUMN_HELP: Record<Column, string> = {
  x: 'x coordinate (any number)',
  y: 'y coordinate (any number)',
  demand: 'units to deliver (whole number, none at the depot)',
  ready: 'earliest service start (whole number)',
  due: 'latest service start (whole number, not before ready)',
  service_time: 'time spent at the stop (whole number, none at the depot)',
};

const INTEGER_COLUMNS: readonly Column[] = ['demand', 'ready', 'due', 'service_time'];

export type CsvParseResult =
  | { ok: true; nodes: PlanNode[] }
  | { ok: false; errors: string[] };

/** Most errors a reader needs to see at once; the rest are counted, not listed. */
const MAX_ERRORS_SHOWN = 8;

function splitRow(line: string): string[] {
  return line.split(',').map((cell) => cell.trim().replace(/^"(.*)"$/, '$1').trim());
}

export function parseCustomerCsv(text: string, maxCustomers: number): CsvParseResult {
  const lines = text
    .replace(/^\uFEFF/, '')
    .split(/\r?\n/)
    .map((line, i) => ({ line: line.trim(), number: i + 1 }))
    .filter(({ line }) => line.length > 0);

  if (lines.length === 0) return { ok: false, errors: ['The file is empty.'] };

  const header = splitRow(lines[0].line).map((h) => h.toLowerCase());
  const missing = CSV_COLUMNS.filter((c) => !header.includes(c));
  if (missing.length) {
    return {
      ok: false,
      errors: [
        `The header row is missing ${missing.join(', ')}. Expected columns: ${CSV_COLUMNS.join(', ')}.`,
      ],
    };
  }
  const index = Object.fromEntries(CSV_COLUMNS.map((c) => [c, header.indexOf(c)])) as Record<Column, number>;

  const rows = lines.slice(1);
  const errors: string[] = [];
  if (rows.length < 2) errors.push('Give a depot row and at least one customer row after the header.');
  if (rows.length - 1 > maxCustomers) {
    errors.push(`The file has ${rows.length - 1} customers; the solver accepts at most ${maxCustomers}.`);
  }

  const nodes: PlanNode[] = [];
  rows.forEach(({ line, number }, rowIndex) => {
    const cells = splitRow(line);
    const values = {} as Record<Column, number>;
    let rowOk = true;
    for (const col of CSV_COLUMNS) {
      const raw = cells[index[col]] ?? '';
      const value = raw === '' ? Number.NaN : Number(raw);
      if (!Number.isFinite(value)) {
        errors.push(`Line ${number}: ${col} is ${raw === '' ? 'empty' : `"${raw}", not a number`}.`);
        rowOk = false;
        continue;
      }
      if (INTEGER_COLUMNS.includes(col) && (!Number.isInteger(value) || value < 0)) {
        errors.push(`Line ${number}: ${col} must be a non-negative whole number, got ${raw}.`);
        rowOk = false;
        continue;
      }
      values[col] = value;
    }
    if (!rowOk) return;
    if (values.ready > values.due) {
      errors.push(`Line ${number}: ready (${values.ready}) is after due (${values.due}).`);
    }
    if (rowIndex === 0 && (values.demand !== 0 || values.service_time !== 0)) {
      errors.push(`Line ${number}: the first row is the depot, so its demand and service_time must be 0.`);
    }
    nodes.push(values);
  });

  if (errors.length) {
    const shown = errors.slice(0, MAX_ERRORS_SHOWN);
    if (errors.length > shown.length) shown.push(`…and ${errors.length - shown.length} more.`);
    return { ok: false, errors: shown };
  }
  return { ok: true, nodes };
}

/** The same format back out, so any loaded instance doubles as a template. */
export function toCustomerCsv(nodes: PlanNode[]): string {
  const rows = nodes.map((n, i) => [i, n.x, n.y, n.demand, n.ready, n.due, n.service_time].join(','));
  return [['id', ...CSV_COLUMNS].join(','), ...rows].join('\n') + '\n';
}
