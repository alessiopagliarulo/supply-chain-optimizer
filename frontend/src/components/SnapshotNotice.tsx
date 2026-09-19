import { Database } from 'lucide-react';
import type { CatalogueProvenance } from '../services/api';

/**
 * The label every view of catalogue data carries: a frozen snapshot, not a live feed.
 * Every figure comes from GET /catalogue/provenance, so the label cannot drift from
 * the data it describes.
 */
export default function SnapshotNotice({ provenance, compact = false }: { provenance: CatalogueProvenance; compact?: boolean }) {
  const { snapshot_year: year, counts, coordinates } = provenance;
  return (
    <div
      role="note"
      className="flex gap-2.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-sm text-amber-100"
    >
      <Database className="w-4 h-4 mt-0.5 shrink-0 text-amber-300" aria-hidden="true" />
      <p className="leading-relaxed min-w-0">
        <span className="font-semibold">{`Frozen ${year} snapshot, not live data.`}</span>{' '}
        {compact
          ? `Real distributors and locations from the ${year} catalogue; prices and stock are ${year} observations.`
          : `${counts.distributors} real distributors, ${counts.components} components and ${counts.offers.toLocaleString()} price offers collected in ${year} (${provenance.original_source}, ${provenance.license}). Locations are ${coordinates.precision}-level, so distributors in one city share one point; ${coordinates.unlocated_distributors} with no verifiable location is left off the map.`}{' '}
        <a
          href={provenance.documentation.url}
          target="_blank"
          rel="noreferrer"
          className="underline text-amber-200 hover:text-white"
        >
          Data provenance
        </a>
      </p>
    </div>
  );
}
