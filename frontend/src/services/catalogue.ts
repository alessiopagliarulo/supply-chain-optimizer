import { useEffect, useState } from 'react';
import { catalogueApi, errorMessage, type CatalogueProvenance, type LocatedDistributor } from './api';

export interface Catalogue {
  distributors: LocatedDistributor[];
  provenance: CatalogueProvenance;
}

/**
 * The located distributors and the provenance record, fetched once per page load and
 * shared by the Map and Route Plan pages. A failure clears the cache so the next
 * mount (or `retry`) asks again instead of replaying the error.
 */
let pending: Promise<Catalogue> | null = null;

function load(): Promise<Catalogue> {
  if (!pending) {
    pending = Promise.all([catalogueApi.distributors(), catalogueApi.provenance()]).then(
      ([distributors, provenance]) => ({ distributors, provenance }),
    );
    pending.catch(() => {
      pending = null;
    });
  }
  return pending;
}

export function useCatalogue() {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    load()
      .then((c) => live && setCatalogue(c))
      .catch((err: unknown) => live && setError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, [attempt]);
  const retry = () => {
    setError(null);
    setAttempt((n) => n + 1);
  };
  return { catalogue, error, retry };
}
