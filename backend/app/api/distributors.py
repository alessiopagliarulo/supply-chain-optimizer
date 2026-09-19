from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func as sqla_func
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel
from app.core.database import get_db
from app.models.distributor import Distributor
from app.models.component import DistributorOffer, Component

router = APIRouter(prefix="/distributors", tags=["distributors"])

# Every list and detail response here reads the frozen 2024 catalogue snapshot.
# GET /catalogue/provenance says so in full; never present these as live quotes.


class DistributorResponse(BaseModel):
    id: int
    name: str
    # NULL location fields mean "location unknown" (one distributor in the
    # snapshot); filter with located_only=true for a map.
    latitude: Optional[float]
    longitude: Optional[float]
    city: Optional[str]
    state: Optional[str]
    country: Optional[str]
    is_domestic: Optional[bool]
    total_offers: int
    total_stock: int
    # Coordinates are one point per city, so distributors in the same city stack
    # on a map. This says how many OTHER distributors sit on exactly this point,
    # so a map can cluster them instead of hiding all but the top marker.
    shares_location_with: int = 0

    class Config:
        from_attributes = True


class CarriedComponent(BaseModel):
    """One component a distributor carries, with that distributor's 2024 offer."""
    component_id: int
    mpn: str
    manufacturer: str
    category: str
    price: float
    currency: Optional[str]
    stock: int
    moq: int = 1
    sku: Optional[str]


class DistributorDetailResponse(DistributorResponse):
    top_components: List[CarriedComponent]  # top 20 by stock


def _colocation_counts(db: Session) -> Dict[Tuple[float, float], int]:
    rows = (
        db.query(Distributor.latitude, Distributor.longitude, sqla_func.count(Distributor.id))
        .filter(Distributor.latitude.isnot(None), Distributor.longitude.isnot(None))
        .group_by(Distributor.latitude, Distributor.longitude)
        .all()
    )
    return {(lat, lng): n for lat, lng, n in rows}


def _to_response(d: Distributor, colocated: Dict[Tuple[float, float], int]) -> DistributorResponse:
    return DistributorResponse(
        id=d.id,
        name=d.name,
        latitude=d.latitude,
        longitude=d.longitude,
        city=d.city,
        state=d.state,
        country=d.country,
        is_domestic=d.is_domestic,
        total_offers=d.total_offers or 0,
        total_stock=d.total_stock or 0,
        shares_location_with=max(colocated.get((d.latitude, d.longitude), 1) - 1, 0),
    )


def _carried(o: DistributorOffer, c: Component) -> CarriedComponent:
    return CarriedComponent(
        component_id=c.id,
        mpn=c.mpn,
        manufacturer=c.manufacturer,
        category=c.category,
        price=o.price,
        currency=o.currency,
        stock=o.stock or 0,
        moq=int(o.moq or 1),
        sku=o.sku,
    )


@router.get("", response_model=List[DistributorResponse])
async def list_distributors(
    domestic_only: bool = Query(False),
    country: Optional[str] = Query(None, description="Exact country, e.g. 'USA', 'China', 'Germany'."),
    component_id: Optional[int] = Query(None, description="Only distributors with an offer for this component."),
    category: Optional[str] = Query(None, description="Only distributors carrying at least one component in this category."),
    has_offers: bool = Query(False, description="Drop distributors with no priced offer in the snapshot."),
    located_only: bool = Query(False, description="Drop distributors whose location is unknown (NULL coordinates)."),
    min_lat: Optional[float] = Query(None, ge=-90, le=90),
    max_lat: Optional[float] = Query(None, ge=-90, le=90),
    min_lng: Optional[float] = Query(None, ge=-180, le=180),
    max_lng: Optional[float] = Query(None, ge=-180, le=180),
    db: Session = Depends(get_db),
):
    """List distributors with their locations and stats.

    Filters combine with AND. The bounding box (`min_lat`..`max_lng`) selects what
    is visible on a map viewport; any bound left out is open.
    """
    if component_id is not None and db.query(Component.id).filter(Component.id == component_id).first() is None:
        raise HTTPException(status_code=404, detail="Component not found")
    if min_lat is not None and max_lat is not None and min_lat > max_lat:
        raise HTTPException(status_code=422, detail="min_lat must not exceed max_lat")
    if min_lng is not None and max_lng is not None and min_lng > max_lng:
        raise HTTPException(status_code=422, detail="min_lng must not exceed max_lng")

    q = db.query(Distributor)
    if domestic_only:
        q = q.filter(Distributor.is_domestic.is_(True))
    if country:
        q = q.filter(Distributor.country == country)
    if has_offers:
        q = q.filter(Distributor.total_offers > 0)
    if located_only or any(b is not None for b in (min_lat, max_lat, min_lng, max_lng)):
        q = q.filter(Distributor.latitude.isnot(None), Distributor.longitude.isnot(None))
    if component_id is not None:
        carriers = db.query(DistributorOffer.distributor_id).filter(DistributorOffer.component_id == component_id)
        q = q.filter(Distributor.id.in_(carriers))
    if category:
        carriers = (
            db.query(DistributorOffer.distributor_id)
            .join(Component, DistributorOffer.component_id == Component.id)
            .filter(Component.category == category)
        )
        q = q.filter(Distributor.id.in_(carriers))
    if min_lat is not None:
        q = q.filter(Distributor.latitude >= min_lat)
    if max_lat is not None:
        q = q.filter(Distributor.latitude <= max_lat)
    if min_lng is not None:
        q = q.filter(Distributor.longitude >= min_lng)
    if max_lng is not None:
        q = q.filter(Distributor.longitude <= max_lng)

    colocated = _colocation_counts(db)
    return [
        _to_response(d, colocated)
        for d in q.order_by(Distributor.total_offers.desc(), Distributor.name.asc()).all()
    ]


@router.get("/{distributor_id}", response_model=DistributorDetailResponse)
async def get_distributor(distributor_id: int, db: Session = Depends(get_db)):
    """Get distributor detail with top components they carry."""
    d = db.query(Distributor).filter(Distributor.id == distributor_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Distributor not found")

    # Top 20 components by stock level at this distributor
    top = (
        db.query(DistributorOffer, Component)
        .join(Component, DistributorOffer.component_id == Component.id)
        .filter(DistributorOffer.distributor_id == distributor_id)
        .order_by(DistributorOffer.stock.desc(), DistributorOffer.id.asc())
        .limit(20)
        .all()
    )

    return DistributorDetailResponse(
        **_to_response(d, _colocation_counts(db)).model_dump(),
        top_components=[_carried(o, c) for o, c in top],
    )


@router.get("/{distributor_id}/components", response_model=List[CarriedComponent])
async def list_distributor_components(
    distributor_id: int,
    category: Optional[str] = Query(None),
    in_stock_only: bool = Query(False),
    sort_by: str = Query("stock", pattern="^(stock|price|mpn)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Every component this distributor carries, with its 2024 offer."""
    if db.query(Distributor.id).filter(Distributor.id == distributor_id).first() is None:
        raise HTTPException(status_code=404, detail="Distributor not found")

    q = (
        db.query(DistributorOffer, Component)
        .join(Component, DistributorOffer.component_id == Component.id)
        .filter(DistributorOffer.distributor_id == distributor_id)
    )
    if category:
        q = q.filter(Component.category == category)
    if in_stock_only:
        q = q.filter(DistributorOffer.stock > 0)
    if sort_by == "stock":
        q = q.order_by(DistributorOffer.stock.desc(), DistributorOffer.id.asc())
    elif sort_by == "price":
        q = q.order_by(DistributorOffer.price.asc(), DistributorOffer.id.asc())
    else:
        q = q.order_by(Component.mpn.asc(), DistributorOffer.id.asc())

    return [_carried(o, c) for o, c in q.offset(skip).limit(limit).all()]
