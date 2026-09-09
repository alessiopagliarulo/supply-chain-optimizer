"""Nobody may rewrite the cart every demo visitor starts from.

`_ensure_demo_template` (app/api/auth.py) documents the template row as one that
"is never issued as a session". That was true of the intent and false of the
behaviour: `POST /api/v1/auth/login` with the seeded `demo` / `demo` password
returned 200 and a bearer token for it. Confirmed against PRODUCTION on
2026-09-08:

    curl -X POST https://supply-chain-api-qy8x.onrender.com/api/v1/auth/login \\
         -H 'Content-Type: application/json' \\
         -d '{"email":"demo@example.com","password":"demo"}'
    -> HTTP 200 {"access_token":"...","token_type":"bearer"}

Since `_clone_cart` copies that user's lines into every new demo session, anyone
on the internet could edit the starting cart a recruiter is about to be shown.

The fix keeps login working (QUICK_START documents it, and browsing as the
template harms nothing) and refuses only the WRITES. These tests pin both halves:
the template cannot mutate, and an ordinary demo session still can. Drop
`_refuse_template_writes` from cart.py and the first three go red.
"""

from app.api.auth import DEMO_TEMPLATE_EMAIL
from app.models.component import Component, DistributorOffer
from app.models.distributor import Distributor
from app.models.order import CartItem
from app.models.user import User


def _seed(db_session):
    db_session.add(
        Distributor(
            id=1, name="DigiKey", latitude=48.1, longitude=-96.2,
            city="Thief River Falls", state="MN", country="USA", is_domestic=True,
        )
    )
    db_session.add(
        Component(
            id=1, mpn="TMPL-001", manufacturer="TestCo", category="Microcontrollers",
            description="x", risk_score=0.3,
        )
    )
    db_session.add(
        DistributorOffer(
            id=1, component_id=1, distributor_id=1, price=1.25, stock=1000,
            moq=1, sku="SKU-1", currency="USD",
        )
    )
    db_session.commit()


def _template_headers(client, db_session):
    """A real bearer token for the template account, via the documented login."""
    # `/auth/demo` creates the template as a side effect, then hands back a
    # SESSION user — so hit it first to get the row, then log in as the template.
    client.post("/api/v1/auth/demo")
    template = db_session.query(User).filter(User.email == DEMO_TEMPLATE_EMAIL).one()
    assert template is not None

    r = client.post(
        "/api/v1/auth/login",
        json={"email": DEMO_TEMPLATE_EMAIL, "password": "demo"},
    )
    assert r.status_code == 200, (
        "the documented demo/demo login stopped working — this test is about "
        "write access, not about removing the login"
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, template


def test_the_template_cannot_add_to_its_own_cart(client, db_session):
    _seed(db_session)
    headers, template = _template_headers(client, db_session)

    r = client.post(
        "/api/v1/cart",
        json={"component_id": 1, "distributor_id": 1, "quantity": 5},
        headers=headers,
    )

    assert r.status_code == 403, (
        f"anyone with the published demo/demo password just edited the cart that "
        f"every visitor's session is cloned from (status {r.status_code})"
    )
    assert db_session.query(CartItem).filter(CartItem.user_id == template.id).count() == 0


def test_the_template_cannot_clear_its_own_cart(client, db_session):
    """The destructive direction matters more: an empty template means every
    later visitor gets an empty demo."""
    _seed(db_session)
    headers, template = _template_headers(client, db_session)
    db_session.add(
        CartItem(user_id=template.id, component_id=1, distributor_id=1,
                 quantity=7, unit_price=1.25)
    )
    db_session.commit()

    r = client.delete("/api/v1/cart", headers=headers)

    assert r.status_code == 403, (
        f"the template cart was wiped (status {r.status_code}); every subsequent "
        f"demo session would clone an empty cart"
    )
    assert db_session.query(CartItem).filter(CartItem.user_id == template.id).count() == 1


def test_the_template_cannot_delete_a_single_line(client, db_session):
    _seed(db_session)
    headers, template = _template_headers(client, db_session)
    line = CartItem(user_id=template.id, component_id=1, distributor_id=1,
                    quantity=7, unit_price=1.25)
    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)

    r = client.delete(f"/api/v1/cart/{line.id}", headers=headers)

    assert r.status_code == 403, f"a template cart line was deleted (status {r.status_code})"
    assert db_session.query(CartItem).filter(CartItem.id == line.id).count() == 1


def test_an_ordinary_demo_session_can_still_edit_its_cart(client, db_session):
    """The guard must protect the template, not break the product. Without this
    the tests above are satisfied by refusing every cart write."""
    _seed(db_session)
    token = client.post("/api/v1/auth/demo").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    added = client.post(
        "/api/v1/cart",
        json={"component_id": 1, "distributor_id": 1, "quantity": 5},
        headers=headers,
    )
    assert added.status_code == 201, (
        f"a normal demo visitor can no longer add to their cart (status "
        f"{added.status_code}) — the guard is over-broad"
    )
    assert client.delete("/api/v1/cart", headers=headers).status_code == 204
    assert client.get("/api/v1/cart", headers=headers).json() == []


def test_the_template_can_still_be_read(client, db_session):
    """Login and read access are deliberately preserved."""
    _seed(db_session)
    headers, _ = _template_headers(client, db_session)

    assert client.get("/api/v1/cart", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
