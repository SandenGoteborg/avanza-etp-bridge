#!/usr/bin/env python3
"""Fetch the most traded Swedish ETPs from Avanza's public web endpoints.

This uses undocumented endpoints used by Avanza's public website. They may
change without notice. No login or Avanza account is used.

Outputs:
- data/latest.json
- data/snapshots/YYYY-MM-DDTHH-MM-SSZ.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

BASE_URL = "https://www.avanza.se"
WARRANT_ENDPOINT = "/_api/market-warrant-filter/"
CERTIFICATE_ENDPOINT = "/_api/market-certificate-filter/"

TOP_N = int(os.getenv("TOP_N", "200"))
PAGE_SIZE = min(int(os.getenv("PAGE_SIZE", "100")), 100)
TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))

# Relevant certificate issuers selected by the user.
CERTIFICATE_ISSUERS = [
    "Vontobel",
    "Societe Generale",
    "Nordea",
    "Morgan Stanley",
    "J.P. Morgan SE",
    "Handelsbanken",
    "BNP Paribas",
    "Morgan Stanley BV",
]

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.avanza.se",
    "Referer": "https://www.avanza.se/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


class AvanzaFetchError(RuntimeError):
    """Raised when the Avanza response cannot be fetched or parsed."""


def _post_json(session: requests.Session, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{BASE_URL}{endpoint}"
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            response = session.post(
                url,
                headers=HEADERS,
                json=payload,
                timeout=TIMEOUT_SECONDS,
            )
            if response.status_code == 429:
                time.sleep(2**attempt)
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise AvanzaFetchError(f"Unexpected JSON type from {url}: {type(data).__name__}")
            return data
        except (requests.RequestException, ValueError, AvanzaFetchError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2**attempt)

    raise AvanzaFetchError(f"Failed to fetch {url}: {last_error}")


def _pages(total: int, size: int) -> Iterable[tuple[int, int]]:
    for offset in range(0, total, size):
        yield offset, min(size, total - offset)


def _warrant_payload(offset: int, limit: int) -> dict[str, Any]:
    return {
        "filter": {
            "directions": [],
            "subTypes": [],
            "issuers": [],
            "underlyingInstruments": [],
        },
        "offset": offset,
        "limit": limit,
        "sortBy": {"field": "totalValueTraded", "order": "desc"},
    }


def _certificate_payload(offset: int, limit: int) -> dict[str, Any]:
    return {
        "filter": {
            "directions": [],
            "leverages": [],
            "underlyingInstruments": [],
            "categories": [],
            "exposures": [],
            "issuers": CERTIFICATE_ISSUERS,
        },
        "offset": offset,
        "limit": limit,
        "sortBy": {"field": "totalValueTraded", "order": "desc"},
    }


def _extract_items(response: dict[str, Any], expected_key: str) -> list[dict[str, Any]]:
    items = response.get(expected_key)
    if not isinstance(items, list):
        keys = ", ".join(sorted(response.keys()))
        raise AvanzaFetchError(
            f"Response did not contain a list named '{expected_key}'. Available keys: {keys}"
        )
    return [item for item in items if isinstance(item, dict)]


def fetch_top(
    session: requests.Session,
    *,
    endpoint: str,
    expected_key: str,
    payload_factory,
    top_n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset, limit in _pages(top_n, PAGE_SIZE):
        response = _post_json(session, endpoint, payload_factory(offset, limit))
        page = _extract_items(response, expected_key)
        rows.extend(page)
        if len(page) < limit:
            break
    return rows[:top_n]


def _underlying_name(row: dict[str, Any]) -> str:
    underlying = row.get("underlyingInstrument")
    if isinstance(underlying, dict):
        value = underlying.get("name")
        if value:
            return str(value).strip()
    return "Unknown"


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize(rows: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        normalized.append(
            {
                "rank": rank,
                "category": category,
                "orderbook_id": str(row.get("orderbookId", "")),
                "name": row.get("name"),
                "issuer": row.get("issuer"),
                "product_type": row.get("subType") if category == "warrant" else "certificate",
                "direction": row.get("direction"),
                "underlying": _underlying_name(row),
                "marketplace": row.get("marketplaceCode"),
                "country_code": row.get("countryCode"),
                "turnover_sek": row.get("totalValueTraded"),
                "leverage": row.get("leverage"),
                "stop_loss": row.get("stopLoss"),
                "spread": row.get("spread"),
                "buy_price": row.get("buyPrice"),
                "sell_price": row.get("sellPrice"),
                "one_day_change_percent": row.get("oneDayChangePercent"),
            }
        )
    return normalized


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    issuer_turnover: dict[str, float] = defaultdict(float)
    underlying_turnover: dict[str, float] = defaultdict(float)
    direction_turnover: dict[str, float] = defaultdict(float)
    issuer_count: Counter[str] = Counter()
    underlying_count: Counter[str] = Counter()

    total_turnover = 0.0
    for row in rows:
        turnover = _number(row.get("turnover_sek"))
        total_turnover += turnover
        issuer = str(row.get("issuer") or "Unknown")
        underlying = str(row.get("underlying") or "Unknown")
        direction = str(row.get("direction") or "Unknown")

        issuer_turnover[issuer] += turnover
        underlying_turnover[underlying] += turnover
        direction_turnover[direction] += turnover
        issuer_count[issuer] += 1
        underlying_count[underlying] += 1

    def top(mapping: dict[str, float], n: int = 20) -> list[dict[str, Any]]:
        return [
            {"name": name, "turnover_sek": value}
            for name, value in sorted(mapping.items(), key=lambda item: item[1], reverse=True)[:n]
        ]

    return {
        "number_of_products": len(rows),
        "total_turnover_sek": total_turnover,
        "top_issuers_by_turnover": top(issuer_turnover),
        "top_underlyings_by_turnover": top(underlying_turnover),
        "direction_turnover": top(direction_turnover, n=10),
        "top_issuers_by_product_count": [
            {"name": name, "products": count} for name, count in issuer_count.most_common(20)
        ],
        "top_underlyings_by_product_count": [
            {"name": name, "products": count} for name, count in underlying_count.most_common(20)
        ],
    }


def main() -> int:
    fetched_at = datetime.now(timezone.utc)
    session = requests.Session()

    try:
        raw_warrants = fetch_top(
            session,
            endpoint=WARRANT_ENDPOINT,
            expected_key="warrants",
            payload_factory=_warrant_payload,
            top_n=TOP_N,
        )
        raw_certificates = fetch_top(
            session,
            endpoint=CERTIFICATE_ENDPOINT,
            expected_key="certificates",
            payload_factory=_certificate_payload,
            top_n=TOP_N,
        )
    except AvanzaFetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    warrants = normalize(raw_warrants, "warrant")
    certificates = normalize(raw_certificates, "certificate")
    all_rows = warrants + certificates

    document = {
        "schema_version": 1,
        "source": "Avanza public web endpoints (unofficial)",
        "fetched_at_utc": fetched_at.isoformat(),
        "coverage": {
            "requested_top_n_per_list": TOP_N,
            "warrants_received": len(warrants),
            "certificates_received": len(certificates),
            "certificate_issuer_filter": CERTIFICATE_ISSUERS,
        },
        "interpretation_notes": [
            "Turnover is an Avanza activity signal, not total Swedish market share.",
            "Before Swedish products start trading, turnover should normally refer to the previous completed session.",
            "A product appearing in this list is not proof that it was newly issued.",
            "The endpoints are undocumented and may change without notice.",
        ],
        "summary": {
            "all_products": build_summary(all_rows),
            "warrants": build_summary(warrants),
            "certificates": build_summary(certificates),
        },
        "warrants": warrants,
        "certificates": certificates,
    }

    data_dir = Path("data")
    snapshots_dir = data_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    latest_path = data_dir / "latest.json"
    snapshot_name = fetched_at.strftime("%Y-%m-%dT%H-%M-%SZ.json")
    snapshot_path = snapshots_dir / snapshot_name

    serialized = json.dumps(document, ensure_ascii=False, indent=2)
    latest_path.write_text(serialized + "\n", encoding="utf-8")
    snapshot_path.write_text(serialized + "\n", encoding="utf-8")

    print(
        f"Saved {len(warrants)} warrants and {len(certificates)} certificates "
        f"to {latest_path} and {snapshot_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
