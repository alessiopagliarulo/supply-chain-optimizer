"""GET /catalogue/provenance: what the component and distributor data actually is."""

from typing import Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import catalogue_provenance as prov
from app.core.database import get_db
from app.models.component import Component, DistributorOffer
from app.models.distributor import Distributor

router = APIRouter(prefix="/catalogue", tags=["catalogue"])


class CatalogueCounts(BaseModel):
    components: int
    distributors: int
    offers: int


class CoordinateProvenance(BaseModel):
    precision: str
    tolerance_km: float
    source: str
    distinct_points: int
    distributors_on_shared_points: int
    unlocated_distributors: int


class CatalogueProvenance(BaseModel):
    """How to describe the catalogue honestly. Read this before labelling it anywhere."""
    is_live: bool
    snapshot_year: int
    summary: str
    dataset: str
    dataset_url: str
    license: str
    uploader: str
    original_source: str
    collected: str
    published: str
    counts: CatalogueCounts
    coordinates: CoordinateProvenance
    documentation: Dict[str, str]


@router.get("/provenance", response_model=CatalogueProvenance)
async def catalogue_provenance(db: Session = Depends(get_db)):
    """Source, licence, freshness and coordinate precision of the served catalogue.

    Counts are read from the database on every call, never quoted from a constant.
    """
    points = db.query(Distributor.latitude, Distributor.longitude).all()
    per_point: Dict[tuple, int] = {}
    unlocated = 0
    for lat, lng in points:
        if lat is None or lng is None:
            unlocated += 1
            continue
        per_point[(lat, lng)] = per_point.get((lat, lng), 0) + 1
    counts = CatalogueCounts(
        components=db.query(Component).count(),
        distributors=len(points),
        offers=db.query(DistributorOffer).count(),
    )
    return CatalogueProvenance(
        is_live=prov.IS_LIVE,
        snapshot_year=prov.SNAPSHOT_YEAR,
        summary=(
            f"{counts.components} real components, {counts.distributors} distributors and "
            f"{counts.offers} price offers from a frozen {prov.SNAPSHOT_YEAR} snapshot "
            f"(Nexar/Octopart via HuggingFace, {prov.DATASET_LICENSE}). Not a live feed: "
            "prices and stock are 2024 observations, not current quotes."
        ),
        dataset=prov.DATASET_REPO,
        dataset_url=prov.DATASET_URL,
        license=prov.DATASET_LICENSE,
        uploader=prov.DATASET_UPLOADER,
        original_source=prov.DATASET_ORIGINAL_SOURCE,
        collected=prov.DATASET_COLLECTED,
        published=prov.DATASET_PUBLISHED,
        counts=counts,
        coordinates=CoordinateProvenance(
            precision=prov.COORDINATE_PRECISION,
            tolerance_km=prov.COORDINATE_TOLERANCE_KM,
            source=prov.COORDINATE_SOURCE,
            distinct_points=len(per_point),
            distributors_on_shared_points=sum(n for n in per_point.values() if n > 1),
            unlocated_distributors=unlocated,
        ),
        documentation={"path": prov.PROVENANCE_DOC, "url": prov.PROVENANCE_DOC_URL},
    )
