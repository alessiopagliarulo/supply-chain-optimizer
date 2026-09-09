"""The cart books every line in USD, so a non-USD offer must never enter it.

`cart_items` is `(id, user_id, component_id, distributor_id, quantity,
unit_price, created_at)` — there is no currency column. Every downstream
consumer (`/cart` totals, `/optimize/*`, `/resilience/*`) reads `unit_price` as
USD per unit. So accepting a EUR/GBP/SGD offer here silently relabels a foreign
figure as dollars.

The identical write was already refused on the live-price ingest path
(`app/api/live_prices.py`: "a non-USD offer must not be written here at all;
doing so would silently relabel a EUR/GBP price as USD in the catalog") — but
that path is unreachable for a visitor, while `POST /api/v1/cart`, which had no
such guard, is the one they actually click. Served catalogue as of 2026-09-08,
queried from `backend/supply_chain.db`:

    SELECT COALESCE(UPPER(TRIM(currency)),'USD'), COUNT(*)
      FROM distributor_offers GROUP BY 1;
    USD|8152   EUR|17   GBP|4   SGD|3

These tests are written so they FAIL if the guard is removed: the EUR/GBP/SGD
cases assert a 422 *and* that no row was booked, and the USD case pins the
happy path so the guard cannot be "fixed" by refusing everything.
"""

import pytest

from app.models.component import Component, DistributorOffer
from app.models.distributor import Distributor


def _seed(db_session, currency: str, price: float = 5.188):
    """One component with one offer in `currency`, plus a USD control offer."""
    db_session.add(
        Distributor(
            id=1, name="Weyland Electronics", latitude=1.29, longitude=103.85,
            city="Singapore", state=None, country="Singapore", is_domestic=False,
        )
    )
    db_session.add(
        Distributor(
            id=2, name="Texas Instruments", latitude=32.78, longitude=-96.80,
            city="Dallas", state="TX", country="USA", is_domestic=True,
        )
    )
    db_session.add(
        Component(
            id=1, mpn="INA2126U", manufacturer="Texas Instruments",
            category="Amplifiers", description="Instrumentation amplifier",
            risk_score=0.3,
        )
    )
    # The foreign offer carries the SMALLER raw number — the exact shape that
    # made the live site badge it "Best Price" above a dearer USD listing.
    db_session.add(
        DistributorOffer(
            id=1, component_id=1, distributor_id=1, price=price, stock=1000,
            moq=1, sku="EU-1", currency=currency,
        )
    )
    db_session.add(
        DistributorOffer(
            id=2, component_id=1, distributor_id=2, price=5.464, stock=1000,
            moq=1, sku="US-1", currency="USD",
        )
    )
    db_session.commit()


def _demo_headers(client):
    token = client.post("/api/v1/auth/demo").json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("currency", ["EUR", "GBP", "SGD"])
def test_a_non_usd_offer_is_refused_by_the_cart(client, db_session, currency):
    """422, and nothing booked. Remove the guard in cart.py and this goes red."""
    _seed(db_session, currency)
    headers = _demo_headers(client)

    r = client.post(
        "/api/v1/cart",
        json={"component_id": 1, "distributor_id": 1, "quantity": 10},
        headers=headers,
    )

    assert r.status_code == 422, (
        f"a {currency} offer was accepted into a USD-only cart "
        f"(status {r.status_code}) — its price is now booked as dollars"
    )
    assert currency in r.json()["detail"], (
        "the refusal must name the currency, so the visitor knows why"
    )
    assert client.get("/api/v1/cart", headers=headers).json() == [], (
        f"the {currency} line was refused with 422 but still written to the cart"
    )


def test_a_non_usd_price_is_never_relabelled_as_usd(client, db_session):
    """The consequence, stated as its own assertion: no foreign figure lands in
    `unit_price`. This is the assertion that actually encodes the defect."""
    _seed(db_session, "EUR", price=5.188)
    headers = _demo_headers(client)

    client.post(
        "/api/v1/cart",
        json={"component_id": 1, "distributor_id": 1, "quantity": 10},
        headers=headers,
    )

    booked = [line["unit_price"] for line in client.get("/api/v1/cart", headers=headers).json()]
    assert 5.188 not in booked, (
        "EUR 5.188 is sitting in unit_price, which every consumer reads as USD "
        "(~$5.45-5.60 at any recent rate) — the cost is understated"
    )


def test_the_usd_offer_on_the_same_part_still_works(client, db_session):
    """The guard must refuse foreign currency, not refuse the cart. Without this
    the test above could be satisfied by breaking the endpoint outright."""
    _seed(db_session, "EUR")
    headers = _demo_headers(client)

    r = client.post(
        "/api/v1/cart",
        json={"component_id": 1, "distributor_id": 2, "quantity": 10},
        headers=headers,
    )

    assert r.status_code == 201, (
        f"the USD offer was rejected too (status {r.status_code}) — the guard is "
        f"over-broad and has broken the normal path"
    )
    assert r.json()["unit_price"] == pytest.approx(5.464)


def test_a_missing_currency_is_treated_as_usd(client, db_session):
    """Legacy rows predate the column's default. NULL must mean USD, not refuse
    — 8,152 of 8,176 served offers are USD and the column is nullable."""
    _seed(db_session, "USD")
    offer = db_session.query(DistributorOffer).filter(DistributorOffer.id == 1).one()
    offer.currency = None
    db_session.commit()

    r = client.post(
        "/api/v1/cart",
        json={"component_id": 1, "distributor_id": 1, "quantity": 10},
        headers=_demo_headers(client),
    )

    assert r.status_code == 201, (
        f"a NULL-currency legacy offer was refused (status {r.status_code}); "
        f"NULL is documented as USD by the column default"
    )
