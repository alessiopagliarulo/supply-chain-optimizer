from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from app.core.database import get_db
from app.models.order import CartItem
from app.models.component import Component, DistributorOffer
from app.models.distributor import Distributor
from app.api.auth import DEMO_TEMPLATE_EMAIL, get_current_user
from app.models.user import User

router = APIRouter(prefix="/cart", tags=["cart"])


# A cart line feeds straight into the optimizer's demand constraint. A negative or
# zero quantity there is not a harmless bad row: it flips the sign of a line's demand
# and silently corrupts every downstream cost, ETA and CO2 figure. The API used to
# accept quantity=-5 with a 201 Created. It is a client error, and it is rejected at
# the schema so it can never reach the model layer.
MAX_CART_QUANTITY = 1_000_000


def _refuse_template_writes(user: User) -> None:
    """The demo TEMPLATE cart is what every visitor's session starts from.

    `_ensure_demo_template` documents this row as one that "is never issued as a
    session" — but `POST /auth/login` with the seeded `demo`/`demo` password
    returns 200 and a bearer token for it (confirmed against production
    2026-09-08). So the docstring was true of the intent and false of the
    behaviour, and anyone on the internet could edit the starting cart that every
    recruiter, interviewer and visitor then clones via `_clone_cart`.

    Login is left working — QUICK_START documents it and browsing as the template
    is harmless. Only the WRITES are refused, which is the whole exposure: read
    the demo, do not rewrite everyone else's starting point.
    """
    if (user.email or "").strip().lower() == DEMO_TEMPLATE_EMAIL:
        raise HTTPException(
            status_code=403,
            detail=(
                "The demo template cart is read-only: every new demo session is "
                "cloned from it. Use the Demo Login button, which issues your own "
                "private session, and edit that cart instead."
            ),
        )


class CartItemAdd(BaseModel):
    component_id: int = Field(..., ge=1, description="Component id (positive)")
    distributor_id: int = Field(..., ge=1, description="Distributor id (positive)")
    quantity: float = Field(
        ...,
        gt=0,
        le=MAX_CART_QUANTITY,
        description="Units to order. Must be strictly positive — a non-positive "
                    "quantity would invert the optimizer's demand constraint.",
    )
    unit_price: Optional[float] = Field(
        None,
        ge=0,
        description="Auto-looked-up from the offer when omitted. Never negative.",
    )


class CartItemResponse(BaseModel):
    id: int
    component_id: int
    distributor_id: int
    quantity: float
    unit_price: Optional[float]
    mpn: Optional[str] = None
    manufacturer: Optional[str] = None
    category: Optional[str] = None
    distributor_name: Optional[str] = None
    distributor_city: Optional[str] = None
    distributor_state: Optional[str] = None
    distributor_country: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


@router.get("", response_model=List[CartItemResponse])
async def get_cart(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Single-query cart retrieval via manual 3-way join (D-18)."""
    items_raw = (
        db.query(CartItem, Component, Distributor)
        .join(Component, CartItem.component_id == Component.id)
        .join(Distributor, CartItem.distributor_id == Distributor.id)
        .filter(CartItem.user_id == current_user.id)
        .all()
    )
    return [
        CartItemResponse(
            id=item.id,
            component_id=item.component_id,
            distributor_id=item.distributor_id,
            quantity=item.quantity,
            unit_price=item.unit_price,
            mpn=comp.mpn,
            manufacturer=comp.manufacturer,
            category=comp.category,
            distributor_name=dist.name,
            distributor_city=dist.city,
            distributor_state=dist.state,
            distributor_country=dist.country,
            created_at=item.created_at,
        )
        for item, comp, dist in items_raw
    ]


@router.post("", response_model=CartItemResponse, status_code=201)
async def add_to_cart(
    body: CartItemAdd,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _refuse_template_writes(current_user)

    comp = db.query(Component).filter(Component.id == body.component_id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="Component not found")

    dist = db.query(Distributor).filter(Distributor.id == body.distributor_id).first()
    if not dist:
        raise HTTPException(status_code=404, detail="Distributor not found")

    # Prevent duplicate (user, component, distributor) rows in the cart
    existing_item = db.query(CartItem).filter(
        CartItem.user_id == current_user.id,
        CartItem.component_id == body.component_id,
        CartItem.distributor_id == body.distributor_id,
    ).first()
    if existing_item:
        raise HTTPException(
            status_code=409,
            detail="This component/distributor combination is already in your cart. Remove it first or update the quantity.",
        )

    # Look up real price and MOQ from offer
    unit_price = body.unit_price
    offer = db.query(DistributorOffer).filter(
        DistributorOffer.component_id == body.component_id,
        DistributorOffer.distributor_id == body.distributor_id,
    ).first()
    if offer:
        # `cart_items` has no currency column (id, user_id, component_id,
        # distributor_id, quantity, unit_price, created_at) and every downstream
        # consumer — /cart totals, /optimize/*, /resilience/* — reads
        # `unit_price` as USD per unit. Booking a non-USD offer here would
        # silently relabel a EUR/GBP/SGD figure as dollars. The identical write
        # is already refused on the live-price path (see live_prices.py: "a
        # non-USD offer must not be written here at all"); that guard sat on a
        # path a visitor cannot reach while this one, which they can, had none.
        # Served catalogue: 8,152 USD / 17 EUR / 4 GBP / 3 SGD.
        offer_currency = (offer.currency or "USD").strip().upper()
        if offer_currency != "USD":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"This offer is priced in {offer_currency}. The cart books every "
                    f"line in USD and no FX conversion is available here, so adding "
                    f"it would misstate the cost. Pick a USD offer for this part."
                ),
            )
        if unit_price is None:
            unit_price = offer.price
        # Enforce MOQ: prevent adding quantities that can't satisfy the solver
        moq = int(offer.moq or 1)
        if moq > 1 and body.quantity < moq:
            raise HTTPException(
                status_code=422,
                detail=f"Minimum order quantity for this offer is {moq} units "
                       f"(requested: {int(body.quantity)})",
            )
        # Warn if quantity exceeds available stock
        if offer.stock is not None and body.quantity > offer.stock:
            raise HTTPException(
                status_code=422,
                detail=f"Requested quantity {int(body.quantity)} exceeds available "
                       f"stock of {offer.stock} units at {dist.name}",
            )

    item = CartItem(
        user_id=current_user.id,
        component_id=body.component_id,
        distributor_id=body.distributor_id,
        quantity=body.quantity,
        unit_price=unit_price,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    return CartItemResponse(
        id=item.id,
        component_id=item.component_id,
        distributor_id=item.distributor_id,
        quantity=item.quantity,
        unit_price=item.unit_price,
        mpn=comp.mpn,
        manufacturer=comp.manufacturer,
        category=comp.category,
        distributor_name=dist.name,
        distributor_city=dist.city,
        distributor_state=dist.state,
        distributor_country=dist.country,
        created_at=item.created_at,
    )


@router.delete("/{item_id}", status_code=204)
async def remove_from_cart(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _refuse_template_writes(current_user)

    item = db.query(CartItem).filter(
        CartItem.id == item_id, CartItem.user_id == current_user.id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Cart item not found")
    db.delete(item)
    db.commit()


@router.delete("", status_code=204)
async def clear_cart(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _refuse_template_writes(current_user)

    db.query(CartItem).filter(CartItem.user_id == current_user.id).delete()
    db.commit()
