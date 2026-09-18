#!/usr/bin/env python3
"""Exercise every backend endpoint against a running deployment and report what
is actually WORKING -- not merely what returns HTTP 200.

The distinction matters here. A prior audit of this repo found endpoints that
answered 200 with structurally empty bodies (empty lists, all-null metrics),
which reads as healthy to any uptime check and as broken to a human. So every
check below carries a predicate over the response body, and an endpoint only
passes if the data is really there.

Usage:
    python scripts/verify_backend.py                    # live deployment
    python scripts/verify_backend.py --base http://localhost:8000/api/v1
    python scripts/verify_backend.py --json report.json

Exit code is 1 if anything FAILED, so this is CI-safe.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

import requests

LIVE_BASE = "https://supply-chain-api-qy8x.onrender.com/api/v1"

# Free-tier Render cold starts take ~100s. Be patient rather than reporting a
# sleeping service as a broken one.
TIMEOUT = 120


class Result:
    __slots__ = ("name", "method", "path", "status", "ok", "detail", "seconds")

    def __init__(
        self,
        name: str,
        method: str,
        path: str,
        status: int | None,
        ok: bool,
        detail: str,
        seconds: float,
    ) -> None:
        self.name = name
        self.method = method
        self.path = path
        self.status = status
        self.ok = ok
        self.detail = detail
        self.seconds = seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "endpoint": f"{self.method} {self.path}",
            "status": self.status,
            "ok": self.ok,
            "detail": self.detail,
            "seconds": round(self.seconds, 2),
        }


class Verifier:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.results: list[Result] = []
        self.token: str | None = None

    # -- plumbing ---------------------------------------------------------

    def check(
        self,
        name: str,
        method: str,
        path: str,
        predicate: Callable[[Any], str | None],
        payload: dict | None = None,
        expect_status: int = 200,
    ) -> Any:
        """Call one endpoint. `predicate` returns None if the body is good, or a
        string explaining what is missing. Returns the parsed body (or None)."""
        url = f"{self.base}{path}"
        started = time.time()
        try:
            resp = self.session.request(
                method, url, json=payload, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            self.results.append(
                Result(name, method, path, None, False, f"request failed: {exc}", time.time() - started)
            )
            return None

        elapsed = time.time() - started
        if resp.status_code != expect_status:
            body = resp.text[:160].replace("\n", " ")
            self.results.append(
                Result(name, method, path, resp.status_code, False, f"expected {expect_status}: {body}", elapsed)
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            self.results.append(
                Result(name, method, path, resp.status_code, False, "response was not JSON", elapsed)
            )
            return None

        problem = predicate(data)
        self.results.append(
            Result(name, method, path, resp.status_code, problem is None, problem or "ok", elapsed)
        )
        return data

    # -- predicates -------------------------------------------------------

    @staticmethod
    def nonempty_list(min_len: int = 1) -> Callable[[Any], str | None]:
        def _p(data: Any) -> str | None:
            items = data.get("items", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return f"expected a list, got {type(items).__name__}"
            if len(items) < min_len:
                return f"list has {len(items)} items, expected >= {min_len}"
            return None

        return _p

    @staticmethod
    def keys_present(*keys: str) -> Callable[[Any], str | None]:
        """Keys must exist AND be non-null. Catches the 200-with-null-metrics case."""

        def _p(data: Any) -> str | None:
            if not isinstance(data, dict):
                return f"expected an object, got {type(data).__name__}"
            missing = [k for k in keys if data.get(k) is None]
            if missing:
                return f"null or absent: {', '.join(missing)}"
            return None

        return _p

    @staticmethod
    def always_ok(_data: Any) -> str | None:
        return None

    # -- the run ----------------------------------------------------------

    def authenticate(self) -> bool:
        data = self.check(
            "demo login", "POST", "/auth/demo", self.keys_present("access_token")
        )
        if not data:
            return False
        self.token = data["access_token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        return True

    def run(self) -> None:
        if not self.authenticate():
            print("FATAL: demo login failed; everything downstream needs it.", file=sys.stderr)
            return

        self.check("current user", "GET", "/auth/me", self.keys_present("email"))

        # --- catalogue ---------------------------------------------------
        comps = self.check("component list", "GET", "/components", self.nonempty_list())
        self.check("categories", "GET", "/components/categories", self.nonempty_list())
        self.check("manufacturers", "GET", "/components/manufacturers", self.nonempty_list())
        self.check("catalogue stats", "GET", "/components/stats", self.keys_present("total_components"))
        dists = self.check("distributor list", "GET", "/distributors", self.nonempty_list())

        items = comps.get("items", comps) if isinstance(comps, dict) else comps
        ids = [c["id"] for c in (items or [])[:5] if isinstance(c, dict) and "id" in c]
        mpns = [c["mpn"] for c in (items or []) if isinstance(c, dict) and c.get("mpn")][:5]
        mpn = mpns[0] if mpns else None
        dl = dists.get("items", dists) if isinstance(dists, dict) else dists
        dist_id = next((d["id"] for d in (dl or []) if isinstance(d, dict) and "id" in d), None)

        if ids:
            self.check("component detail", "GET", f"/components/{ids[0]}", self.keys_present("id", "mpn"))
            self.check("component offers", "GET", f"/components/{ids[0]}/offers", self.nonempty_list())
        if dist_id:
            self.check("distributor detail", "GET", f"/distributors/{dist_id}", self.keys_present("id", "name"))

        # --- ML ----------------------------------------------------------
        # The headline check: model_source must not be "none". That single field
        # is what silently read as "no models trained" for weeks while the real
        # cause was an unpicklable artifact.
        def real_model(data: Any) -> str | None:
            src = data.get("model_source")
            if src in (None, "none"):
                return f"model_source={src!r} -- no model is being served"
            return None

        self.check("ML model info", "GET", "/ml/model-info", real_model)
        if ids:
            self.check(
                "ML lead-time", "GET", f"/ml/lead-time?component_id={ids[0]}",
                self.keys_present("predicted_factory_lead_time_days"),
            )
        self.check("ML macro stress", "GET", "/ml/stress", self.keys_present("stress_probability"))
        self.check("ML model comparison", "GET", "/ml/model-comparison", self.always_ok)

        # --- forecasting / benchmarks -------------------------------------
        self.check("demand benchmark", "GET", "/demand/benchmark", self.always_ok)
        self.check("fiedler curve", "GET", "/benchmark/fiedler-curve", self.always_ok)
        self.check("single-source parts", "GET", "/benchmark/single-source-components", self.always_ok)

        # --- graph --------------------------------------------------------
        self.check("graph metrics", "GET", "/graph/metrics", self.always_ok)
        if ids:
            self.check("graph simulate", "POST", "/graph/simulate", self.always_ok, {"bom_component_ids": ids})

        # --- resilience ------------------------------------------------------
        if ids and dist_id:
            self.check(
                "distributor failure", "POST", "/resilience/distributor-failure",
                self.always_ok, {"bom_component_ids": ids, "distributor_id": dist_id},
            )
        if ids:
            for name, path, body in [
                ("delivery target", "/resilience/delivery-target", {"bom_component_ids": ids, "target_delivery_days": 30}),
                ("geopolitical risk", "/resilience/geopolitical-risk", {"bom_component_ids": ids, "risk_multiplier": 1.5}),
                ("criticality sweep", "/resilience/criticality-sweep", {"bom_component_ids": ids, "top_n": 5}),
                ("dual sourcing", "/resilience/dual-sourcing-plan", {"bom_component_ids": ids, "top_n": 5}),
                ("sensitivity", "/resilience/sensitivity", {"bom_component_ids": ids, "metric": "cost"}),
            ]:
                self.check(name, "POST", path, self.always_ok, body)

        # --- live pricing -------------------------------------------------------
        if mpn:
            self.check("live price by MPN", "GET", f"/live-prices/{mpn}", self.always_ok)
        if mpns:
            self.check(
                "live price for BOM", "POST", "/live-prices/bom", self.always_ok,
                {"items": [{"mpn": m, "quantity": 100} for m in mpns[:3]]},
            )

        # --- cart round trip -------------------------------------------------------
        self.check("cart read", "GET", "/cart", self.always_ok)
        if ids and dist_id:
            self.check(
                "cart add", "POST", "/cart", self.always_ok,
                {"component_id": ids[0], "distributor_id": dist_id, "quantity": 10},
                expect_status=201,
            )

        # --- external feeds ---------------------------------------------------------
        # Feeds legitimately report "inactive" when a key is absent; that is an
        # honest state, not a fault. So we only assert the endpoint answers with
        # a list and surface each feed's status in the detail column.
        def feed_summary(data: Any) -> str | None:
            feeds = data.get("feeds", data) if isinstance(data, dict) else data
            if not isinstance(feeds, list) or not feeds:
                return "no feeds reported"
            live = [f.get("name") for f in feeds if f.get("status") == "live"]
            other = [f"{f.get('name')}={f.get('status')}" for f in feeds if f.get("status") != "live"]
            if not live:
                return f"NO live feeds; {', '.join(other)}"
            return None

        self.check("feed status", "GET", "/feeds/status", feed_summary)

        # --- market intelligence (SupplyMaven-gated) ------------------------------
        # REMOVED 2026-09-01. Six probes used to run here — /market/status,
        # /summary, /disruption-index, /alerts, /commodities, /trade-policy.
        # The routes themselves were removed from the API surface (see
        # docs/OUTSTANDING_WORK.md item 55): their upstream REST path 404s with
        # or without a token, so they had never once returned data, and no
        # frontend page called them. The committed
        # docs/backend_verification.json is a dated hand-run snapshot from
        # 2026-08-19 and still records those six as 200 — which they were, on
        # that day. It is deliberately NOT hand-edited; a fresh run of this
        # script simply reports six fewer checks.

    # -- reporting ---------------------------------------------------------

    def report(self) -> int:
        width = max((len(r.name) for r in self.results), default=10) + 2
        passed = [r for r in self.results if r.ok]
        failed = [r for r in self.results if not r.ok]

        print(f"\n{'ENDPOINT VERIFICATION':<{width}} {self.base}\n")
        for r in self.results:
            mark = "PASS" if r.ok else "FAIL"
            status = r.status if r.status is not None else "---"
            print(f"  [{mark}] {r.name:<{width}} {status}  {r.method:5} {r.path}")
            if not r.ok or r.detail != "ok":
                print(f"         {'':<{width}} -> {r.detail}")

        print(f"\n  {len(passed)} passed, {len(failed)} failed, of {len(self.results)} checks")
        if failed:
            print("\n  FAILED:")
            for r in failed:
                print(f"    - {r.name}: {r.detail}")
        return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=LIVE_BASE, help="API base URL including /api/v1")
    ap.add_argument("--json", help="also write a JSON report here")
    args = ap.parse_args()

    v = Verifier(args.base)
    v.run()
    code = v.report()

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(
                {"base": args.base, "checks": [r.as_dict() for r in v.results]}, fh, indent=2
            )
        print(f"\n  JSON report -> {args.json}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
