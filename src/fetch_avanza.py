#!/usr/bin/env python3
"""Fetch and pre-process Swedish ETP activity from Avanza's public web endpoints.

The script keeps the complete fetched product rows, but also creates an agent-optimised
file with deterministic calculations. The language model therefore does not need to
sum hundreds of rows itself.

Outputs:
- data/latest.json       Compact but information-rich file intended for the agent.
- data/latest_full.json  Complete raw rows plus the same analytics.
- data/history/YYYY-MM-DD.json  Compact daily history record.

The endpoints are undocumented and may change without notice. No login is used.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

BASE_URL = "https://www.avanza.se"
WARRANT_ENDPOINT = "/_api/market-warrant-filter/"
CERTIFICATE_ENDPOINT = "/_api/market-certificate-filter/"

TOP_N = int(os.getenv("TOP_N", "500"))
PAGE_SIZE = min(int(os.getenv("PAGE_SIZE", "100")), 100)
TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "1.0"))
AGENT_TOP_PRODUCTS_PER_FAMILY = int(os.getenv("AGENT_TOP_PRODUCTS_PER_FAMILY", "100"))
MAX_CHANGE_ROWS = int(os.getenv("MAX_CHANGE_ROWS", "100"))
MIN_PRODUCT_CHANGE_TURNOVER_SEK = float(os.getenv("MIN_PRODUCT_CHANGE_TURNOVER_SEK", "100000"))
MIN_PREVIOUS_TURNOVER_FOR_PERCENT = float(os.getenv("MIN_PREVIOUS_TURNOVER_FOR_PERCENT", "50000"))
MIN_UNDERLYING_TURNOVER_FOR_SHARE_CHANGE = float(
    os.getenv("MIN_UNDERLYING_TURNOVER_FOR_SHARE_CHANGE", "500000")
)

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

PRODUCT_FAMILIES = ("warrants_turbos_minis", "certificates")

LEVERAGE_BUCKETS: tuple[tuple[float, float | None, str], ...] = (
    (0.0, 2.0, "0-<2"),
    (2.0, 3.0, "2-<3"),
    (3.0, 5.0, "3-<5"),
    (5.0, 10.0, "5-<10"),
    (10.0, 20.0, "10-<20"),
    (20.0, 50.0, "20-<50"),
    (50.0, 100.0, "50-<100"),
    (100.0, None, "100+"),
)


class AvanzaFetchError(RuntimeError):
    """Raised when an Avanza response cannot be fetched or parsed."""


def _post_json(session: requests.Session, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{BASE_URL}{endpoint}"
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            response = session.post(url, headers=HEADERS, json=payload, timeout=TIMEOUT_SECONDS)
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
    payload_factory: Callable[[int, int], dict[str, Any]],
    top_n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset, limit in _pages(top_n, PAGE_SIZE):
        response = _post_json(session, endpoint, payload_factory(offset, limit))
        page = _extract_items(response, expected_key)
        rows.extend(page)
        
        time.sleep(REQUEST_DELAY_SECONDS)
        
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
        result = float(value or 0)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _share(numerator: float, denominator: float) -> float:
    return round((numerator / denominator) * 100.0, 4) if denominator > 0 else 0.0


def normalize(rows: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    family = "warrants_turbos_minis" if category == "warrant" else "certificates"
    normalized: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        normalized.append(
            {
                "rank": rank,
                "category": category,
                "product_family": family,
                "orderbook_id": str(row.get("orderbookId", "")),
                "name": row.get("name"),
                "issuer": row.get("issuer") or "Unknown",
                "detailed_product_type": row.get("subType") if category == "warrant" else "certificate",
                "direction": row.get("direction"),
                "underlying": _underlying_name(row),
                "marketplace": row.get("marketplaceCode"),
                "country_code": row.get("countryCode"),
                "turnover_sek": _number(row.get("totalValueTraded")),
                "leverage": _number_or_none(row.get("leverage")),
                "stop_loss": row.get("stopLoss"),
                "spread": _number_or_none(row.get("spread")),
                "buy_price": _number_or_none(row.get("buyPrice")),
                "sell_price": _number_or_none(row.get("sellPrice")),
                "one_day_change_percent": _number_or_none(row.get("oneDayChangePercent")),
            }
        )
    return normalized


def _weighted_median(pairs: Sequence[tuple[float, float]]) -> float | None:
    usable = sorted((value, weight) for value, weight in pairs if weight > 0)
    total_weight = sum(weight for _, weight in usable)
    if total_weight <= 0:
        return None
    threshold = total_weight / 2.0
    running = 0.0
    for value, weight in usable:
        running += weight
        if running >= threshold:
            return value
    return usable[-1][0] if usable else None


def _leverage_bucket(value: float | None) -> str:
    if value is None or value < 0:
        return "unknown_or_not_applicable"
    for lower, upper, label in LEVERAGE_BUCKETS:
        if value >= lower and (upper is None or value < upper):
            return label
    return "unknown_or_not_applicable"


def build_leverage_summary(rows: Sequence[dict[str, Any]], exact_limit: int = 25) -> dict[str, Any]:
    bucket_turnover: dict[str, float] = defaultdict(float)
    bucket_count: Counter[str] = Counter()
    exact_turnover: dict[float, float] = defaultdict(float)
    exact_count: Counter[float] = Counter()
    weighted_pairs: list[tuple[float, float]] = []
    total_with_leverage = 0.0
    total_all = sum(_number(row.get("turnover_sek")) for row in rows)

    for row in rows:
        turnover = _number(row.get("turnover_sek"))
        leverage = _number_or_none(row.get("leverage"))
        bucket = _leverage_bucket(leverage)
        bucket_turnover[bucket] += turnover
        bucket_count[bucket] += 1
        if leverage is not None and leverage >= 0:
            level = round(leverage, 2)
            exact_turnover[level] += turnover
            exact_count[level] += 1
            weighted_pairs.append((leverage, turnover))
            total_with_leverage += turnover

    buckets = []
    ordered_labels = [label for _, _, label in LEVERAGE_BUCKETS] + ["unknown_or_not_applicable"]
    for label in ordered_labels:
        turnover = bucket_turnover.get(label, 0.0)
        count = bucket_count.get(label, 0)
        if count == 0 and turnover == 0:
            continue
        buckets.append(
            {
                "bucket": label,
                "turnover_sek": turnover,
                "share_of_scope_turnover_pct": _share(turnover, total_all),
                "product_count": count,
            }
        )

    popular_exact = [
        {
            "leverage": level,
            "turnover_sek": turnover,
            "share_of_turnover_with_known_leverage_pct": _share(turnover, total_with_leverage),
            "product_count": exact_count[level],
        }
        for level, turnover in sorted(exact_turnover.items(), key=lambda item: item[1], reverse=True)[
            :exact_limit
        ]
    ]

    weighted_average = (
        sum(value * weight for value, weight in weighted_pairs) / total_with_leverage
        if total_with_leverage > 0
        else None
    )

    return {
        "turnover_with_known_leverage_sek": total_with_leverage,
        "share_of_scope_turnover_with_known_leverage_pct": _share(total_with_leverage, total_all),
        "turnover_weighted_average_leverage": _round(weighted_average),
        "turnover_weighted_median_leverage": _round(_weighted_median(weighted_pairs)),
        "buckets": buckets,
        "popular_exact_levels": popular_exact,
    }


def _scope_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    total_turnover = sum(_number(row.get("turnover_sek")) for row in rows)
    issuer_turnover: dict[str, float] = defaultdict(float)
    issuer_count: Counter[str] = Counter()
    underlying_turnover: dict[str, float] = defaultdict(float)
    underlying_count: Counter[str] = Counter()

    for row in rows:
        turnover = _number(row.get("turnover_sek"))
        issuer = str(row.get("issuer") or "Unknown")
        underlying = str(row.get("underlying") or "Unknown")
        issuer_turnover[issuer] += turnover
        issuer_count[issuer] += 1
        underlying_turnover[underlying] += turnover
        underlying_count[underlying] += 1

    issuers = [
        {
            "issuer": issuer,
            "turnover_sek": turnover,
            "share_of_observed_top_list_turnover_pct": _share(turnover, total_turnover),
            "product_count": issuer_count[issuer],
        }
        for issuer, turnover in sorted(issuer_turnover.items(), key=lambda item: item[1], reverse=True)
    ]
    underlyings = [
        {
            "underlying": underlying,
            "turnover_sek": turnover,
            "share_of_observed_top_list_turnover_pct": _share(turnover, total_turnover),
            "product_count": underlying_count[underlying],
        }
        for underlying, turnover in sorted(
            underlying_turnover.items(), key=lambda item: item[1], reverse=True
        )
    ]

    return {
        "number_of_products": len(rows),
        "total_turnover_sek": total_turnover,
        "issuers": issuers,
        "underlyings": underlyings,
        "leverage": build_leverage_summary(rows),
    }


def build_underlying_analysis(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build an all-underlying issuer matrix without duplicating raw product rows.

    Every observed underlying is retained. Leverage is summarized once per broad
    product family and underlying, rather than repeated for every issuer-product pair.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total_scope_turnover = sum(_number(row.get("turnover_sek")) for row in rows)
    for row in rows:
        grouped[str(row.get("underlying") or "Unknown")].append(row)

    output: list[dict[str, Any]] = []
    for underlying, underlying_rows in grouped.items():
        underlying_turnover = sum(_number(row.get("turnover_sek")) for row in underlying_rows)

        by_family: dict[str, Any] = {}
        family_totals: dict[str, float] = {}
        for family in PRODUCT_FAMILIES:
            family_rows = [row for row in underlying_rows if row.get("product_family") == family]
            if not family_rows:
                continue
            family_turnover = sum(_number(row.get("turnover_sek")) for row in family_rows)
            family_totals[family] = family_turnover
            family_issuer_turnover: dict[str, float] = defaultdict(float)
            family_issuer_count: Counter[str] = Counter()
            for row in family_rows:
                issuer = str(row.get("issuer") or "Unknown")
                family_issuer_turnover[issuer] += _number(row.get("turnover_sek"))
                family_issuer_count[issuer] += 1

            by_family[family] = {
                "turnover_sek": family_turnover,
                "share_of_underlying_turnover_pct": _share(
                    family_turnover, underlying_turnover
                ),
                "product_count": len(family_rows),
                "issuers": [
                    {
                        "issuer": issuer,
                        "turnover_sek": turnover,
                        "share_within_underlying_and_family_pct": _share(
                            turnover, family_turnover
                        ),
                        "product_count": family_issuer_count[issuer],
                    }
                    for issuer, turnover in sorted(
                        family_issuer_turnover.items(), key=lambda item: item[1], reverse=True
                    )
                ],
                "leverage": build_leverage_summary(family_rows, exact_limit=15),
            }

        issuer_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in underlying_rows:
            issuer_groups[str(row.get("issuer") or "Unknown")].append(row)

        issuers = []
        for issuer, issuer_rows in issuer_groups.items():
            issuer_turnover = sum(_number(row.get("turnover_sek")) for row in issuer_rows)
            issuer_by_family: dict[str, Any] = {}
            for family in PRODUCT_FAMILIES:
                family_rows = [row for row in issuer_rows if row.get("product_family") == family]
                if family_rows:
                    family_turnover = sum(_number(row.get("turnover_sek")) for row in family_rows)
                    issuer_by_family[family] = {
                        "turnover_sek": family_turnover,
                        "share_within_underlying_and_family_pct": _share(
                            family_turnover, family_totals.get(family, 0.0)
                        ),
                        "product_count": len(family_rows),
                    }

            issuers.append(
                {
                    "issuer": issuer,
                    "turnover_sek": issuer_turnover,
                    "share_within_underlying_pct": _share(
                        issuer_turnover, underlying_turnover
                    ),
                    "product_count": len(issuer_rows),
                    "by_product_family": issuer_by_family,
                }
            )

        output.append(
            {
                "underlying": underlying,
                "turnover_sek": underlying_turnover,
                "share_of_observed_top_list_turnover_pct": _share(
                    underlying_turnover, total_scope_turnover
                ),
                "product_count": len(underlying_rows),
                "by_product_family": by_family,
                "issuers": sorted(issuers, key=lambda item: item["turnover_sek"], reverse=True),
            }
        )

    return sorted(output, key=lambda item: item["turnover_sek"], reverse=True)

def build_issuer_analysis(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total_scope_turnover = sum(_number(row.get("turnover_sek")) for row in rows)
    for row in rows:
        grouped[str(row.get("issuer") or "Unknown")].append(row)

    output = []
    for issuer, issuer_rows in grouped.items():
        issuer_turnover = sum(_number(row.get("turnover_sek")) for row in issuer_rows)
        by_family = {}
        for family in PRODUCT_FAMILIES:
            family_rows = [row for row in issuer_rows if row.get("product_family") == family]
            if family_rows:
                family_scope_total = sum(
                    _number(row.get("turnover_sek"))
                    for row in rows
                    if row.get("product_family") == family
                )
                family_turnover = sum(_number(row.get("turnover_sek")) for row in family_rows)
                by_family[family] = {
                    "turnover_sek": family_turnover,
                    "share_of_observed_family_turnover_pct": _share(
                        family_turnover, family_scope_total
                    ),
                    "product_count": len(family_rows),
                }

        by_underlying: dict[str, dict[str, Any]] = {}
        for row in issuer_rows:
            underlying = str(row.get("underlying") or "Unknown")
            entry = by_underlying.setdefault(
                underlying, {"underlying": underlying, "turnover_sek": 0.0, "product_count": 0}
            )
            entry["turnover_sek"] += _number(row.get("turnover_sek"))
            entry["product_count"] += 1

        output.append(
            {
                "issuer": issuer,
                "turnover_sek": issuer_turnover,
                "share_of_observed_top_list_turnover_pct": _share(
                    issuer_turnover, total_scope_turnover
                ),
                "product_count": len(issuer_rows),
                "by_product_family": by_family,
                "top_underlyings": sorted(
                    by_underlying.values(), key=lambda item: item["turnover_sek"], reverse=True
                )[:50],
            }
        )

    return sorted(output, key=lambda item: item["turnover_sek"], reverse=True)


def _product_key(row: dict[str, Any]) -> str:
    orderbook_id = str(row.get("orderbook_id") or "").strip()
    if orderbook_id:
        return f"id:{orderbook_id}"
    return "fallback:" + "|".join(
        [
            str(row.get("product_family") or ""),
            str(row.get("issuer") or ""),
            str(row.get("name") or ""),
        ]
    )


def _flatten_document_rows(document: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("warrants", "certificates"):
        value = document.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    # Version 2 full files also expose raw_products.
    raw_products = document.get("raw_products")
    if not rows and isinstance(raw_products, dict):
        for key in ("warrants_turbos_minis", "certificates"):
            value = raw_products.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))

    # Adapt version 1 rows so the first version 2 run can compare with them.
    adapted: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        if not row.get("product_family"):
            category = str(row.get("category") or "")
            row["product_family"] = (
                "warrants_turbos_minis" if category == "warrant" else "certificates"
            )
        if "detailed_product_type" not in row and "product_type" in row:
            row["detailed_product_type"] = row.get("product_type")
        adapted.append(row)
    return adapted


def _aggregate_maps(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    family_total: dict[str, float] = defaultdict(float)
    family_issuer: dict[tuple[str, str], float] = defaultdict(float)
    underlying_total: dict[str, float] = defaultdict(float)
    family_underlying_total: dict[tuple[str, str], float] = defaultdict(float)
    family_underlying_issuer: dict[tuple[str, str, str], float] = defaultdict(float)

    for row in rows:
        family = str(row.get("product_family") or "Unknown")
        issuer = str(row.get("issuer") or "Unknown")
        underlying = str(row.get("underlying") or "Unknown")
        turnover = _number(row.get("turnover_sek"))
        family_total[family] += turnover
        family_issuer[(family, issuer)] += turnover
        underlying_total[underlying] += turnover
        family_underlying_total[(family, underlying)] += turnover
        family_underlying_issuer[(family, underlying, issuer)] += turnover

    return {
        "family_total": family_total,
        "family_issuer": family_issuer,
        "underlying_total": underlying_total,
        "family_underlying_total": family_underlying_total,
        "family_underlying_issuer": family_underlying_issuer,
    }


def build_changes(
    current_rows: Sequence[dict[str, Any]],
    previous_document: dict[str, Any] | None,
) -> dict[str, Any]:
    previous_rows = _flatten_document_rows(previous_document)
    if not previous_rows:
        return {
            "status": "no_previous_snapshot",
            "previous_fetched_at_utc": None,
            "note": "Changes require at least two snapshots.",
        }

    previous_by_key = {_product_key(row): row for row in previous_rows}
    current_by_key = {_product_key(row): row for row in current_rows}

    increases: list[dict[str, Any]] = []
    decreases: list[dict[str, Any]] = []
    for key, current in current_by_key.items():
        previous = previous_by_key.get(key)
        if previous is None:
            continue
        current_turnover = _number(current.get("turnover_sek"))
        previous_turnover = _number(previous.get("turnover_sek"))
        if max(current_turnover, previous_turnover) < MIN_PRODUCT_CHANGE_TURNOVER_SEK:
            continue
        delta = current_turnover - previous_turnover
        pct = (
            (delta / previous_turnover) * 100.0
            if previous_turnover >= MIN_PREVIOUS_TURNOVER_FOR_PERCENT
            else None
        )
        item = {
            "orderbook_id": current.get("orderbook_id"),
            "name": current.get("name"),
            "issuer": current.get("issuer"),
            "underlying": current.get("underlying"),
            "product_family": current.get("product_family"),
            "leverage": current.get("leverage"),
            "current_turnover_sek": current_turnover,
            "previous_turnover_sek": previous_turnover,
            "turnover_delta_sek": delta,
            "turnover_change_pct": _round(pct, 2),
            "current_rank": current.get("rank"),
            "previous_rank": previous.get("rank"),
            "rank_improvement": (
                int(previous.get("rank")) - int(current.get("rank"))
                if str(previous.get("rank", "")).isdigit()
                and str(current.get("rank", "")).isdigit()
                else None
            ),
        }
        (increases if delta >= 0 else decreases).append(item)

    increases.sort(key=lambda item: item["turnover_delta_sek"], reverse=True)
    decreases.sort(key=lambda item: item["turnover_delta_sek"])

    newly_observed = [
        {
            "orderbook_id": row.get("orderbook_id"),
            "name": row.get("name"),
            "issuer": row.get("issuer"),
            "underlying": row.get("underlying"),
            "product_family": row.get("product_family"),
            "leverage": row.get("leverage"),
            "turnover_sek": _number(row.get("turnover_sek")),
            "rank": row.get("rank"),
        }
        for key, row in current_by_key.items()
        if key not in previous_by_key
    ]
    newly_observed.sort(key=lambda item: item["turnover_sek"], reverse=True)

    dropped = [
        {
            "orderbook_id": row.get("orderbook_id"),
            "name": row.get("name"),
            "issuer": row.get("issuer"),
            "underlying": row.get("underlying"),
            "product_family": row.get("product_family"),
            "leverage": row.get("leverage"),
            "previous_turnover_sek": _number(row.get("turnover_sek")),
            "previous_rank": row.get("rank"),
        }
        for key, row in previous_by_key.items()
        if key not in current_by_key
    ]
    dropped.sort(key=lambda item: item["previous_turnover_sek"], reverse=True)

    current_maps = _aggregate_maps(current_rows)
    previous_maps = _aggregate_maps(previous_rows)

    issuer_share_changes = []
    family_issuer_keys = set(current_maps["family_issuer"]) | set(previous_maps["family_issuer"])
    for family, issuer in family_issuer_keys:
        current_turnover = current_maps["family_issuer"].get((family, issuer), 0.0)
        previous_turnover = previous_maps["family_issuer"].get((family, issuer), 0.0)
        current_share = _share(current_turnover, current_maps["family_total"].get(family, 0.0))
        previous_share = _share(previous_turnover, previous_maps["family_total"].get(family, 0.0))
        issuer_share_changes.append(
            {
                "product_family": family,
                "issuer": issuer,
                "current_turnover_sek": current_turnover,
                "previous_turnover_sek": previous_turnover,
                "turnover_delta_sek": current_turnover - previous_turnover,
                "current_share_pct": current_share,
                "previous_share_pct": previous_share,
                "share_change_percentage_points": round(current_share - previous_share, 4),
            }
        )
    issuer_share_changes.sort(
        key=lambda item: abs(item["share_change_percentage_points"]), reverse=True
    )

    underlying_changes = []
    underlying_keys = set(current_maps["underlying_total"]) | set(previous_maps["underlying_total"])
    for underlying in underlying_keys:
        current_turnover = current_maps["underlying_total"].get(underlying, 0.0)
        previous_turnover = previous_maps["underlying_total"].get(underlying, 0.0)
        delta = current_turnover - previous_turnover
        pct = (
            (delta / previous_turnover) * 100.0
            if previous_turnover >= MIN_PREVIOUS_TURNOVER_FOR_PERCENT
            else None
        )
        underlying_changes.append(
            {
                "underlying": underlying,
                "current_turnover_sek": current_turnover,
                "previous_turnover_sek": previous_turnover,
                "turnover_delta_sek": delta,
                "turnover_change_pct": _round(pct, 2),
            }
        )
    underlying_changes.sort(key=lambda item: abs(item["turnover_delta_sek"]), reverse=True)

    issuer_underlying_share_changes = []
    combo_keys = set(current_maps["family_underlying_issuer"]) | set(
        previous_maps["family_underlying_issuer"]
    )
    for family, underlying, issuer in combo_keys:
        current_underlying_total = current_maps["family_underlying_total"].get(
            (family, underlying), 0.0
        )
        previous_underlying_total = previous_maps["family_underlying_total"].get(
            (family, underlying), 0.0
        )
        if max(current_underlying_total, previous_underlying_total) < MIN_UNDERLYING_TURNOVER_FOR_SHARE_CHANGE:
            continue
        current_turnover = current_maps["family_underlying_issuer"].get(
            (family, underlying, issuer), 0.0
        )
        previous_turnover = previous_maps["family_underlying_issuer"].get(
            (family, underlying, issuer), 0.0
        )
        current_share = _share(current_turnover, current_underlying_total)
        previous_share = _share(previous_turnover, previous_underlying_total)
        issuer_underlying_share_changes.append(
            {
                "product_family": family,
                "underlying": underlying,
                "issuer": issuer,
                "current_underlying_turnover_sek": current_underlying_total,
                "previous_underlying_turnover_sek": previous_underlying_total,
                "current_issuer_turnover_sek": current_turnover,
                "previous_issuer_turnover_sek": previous_turnover,
                "current_share_within_underlying_pct": current_share,
                "previous_share_within_underlying_pct": previous_share,
                "share_change_percentage_points": round(current_share - previous_share, 4),
            }
        )
    issuer_underlying_share_changes.sort(
        key=lambda item: abs(item["share_change_percentage_points"]), reverse=True
    )

    return {
        "status": "compared_with_previous_snapshot",
        "previous_fetched_at_utc": previous_document.get("fetched_at_utc")
        if isinstance(previous_document, dict)
        else None,
        "product_turnover_increases": increases[:MAX_CHANGE_ROWS],
        "product_turnover_decreases": decreases[:MAX_CHANGE_ROWS],
        "newly_observed_in_top_lists": newly_observed[:MAX_CHANGE_ROWS],
        "dropped_from_observed_top_lists": dropped[:MAX_CHANGE_ROWS],
        "issuer_share_changes_by_product_family": issuer_share_changes,
        "underlying_turnover_changes": underlying_changes[:MAX_CHANGE_ROWS],
        "issuer_share_changes_within_underlying_and_family": issuer_underlying_share_changes[
            :MAX_CHANGE_ROWS
        ],
        "interpretation_notes": [
            "Newly observed does not mean newly issued.",
            "Dropped from the observed top list does not mean delisted.",
            "Percentage changes are omitted when previous turnover is too small for a meaningful comparison.",
            "Top-list cutoffs can themselves cause products to enter or leave the observed universe.",
        ],
    }


def build_analytics(
    rows: Sequence[dict[str, Any]], previous_document: dict[str, Any] | None
) -> dict[str, Any]:
    by_family = {}
    for family in PRODUCT_FAMILIES:
        family_rows = [row for row in rows if row.get("product_family") == family]
        by_family[family] = _scope_summary(family_rows)

    return {
        "overall": _scope_summary(rows),
        "by_product_family": by_family,
        "issuers": build_issuer_analysis(rows),
        "underlyings": build_underlying_analysis(rows),
        "changes": build_changes(rows, previous_document),
    }


def _load_previous_document(data_dir: Path) -> dict[str, Any] | None:
    candidates = [data_dir / "latest_full.json", data_dir / "latest.json"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and _flatten_document_rows(data):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _agent_product_highlights(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    fields = (
        "rank",
        "product_family",
        "orderbook_id",
        "name",
        "issuer",
        "detailed_product_type",
        "underlying",
        "turnover_sek",
        "leverage",
        "spread",
        "buy_price",
        "sell_price",
    )
    output: dict[str, list[dict[str, Any]]] = {}
    for family in PRODUCT_FAMILIES:
        family_rows = [row for row in rows if row.get("product_family") == family]
        family_rows.sort(key=lambda row: _number(row.get("turnover_sek")), reverse=True)
        output[family] = [
            {field: row.get(field) for field in fields}
            for row in family_rows[:AGENT_TOP_PRODUCTS_PER_FAMILY]
        ]
    return output


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    fetched_at = datetime.now(timezone.utc)
    data_dir = Path("data")
    previous_document = _load_previous_document(data_dir)
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
    analytics = build_analytics(all_rows, previous_document)

    common = {
        "schema_version": 2,
        "source": "Avanza public web endpoints (unofficial)",
        "fetched_at_utc": fetched_at.isoformat(),
        "coverage": {
            "requested_top_n_per_list": TOP_N,
            "warrants_turbos_minis_received": len(warrants),
            "certificates_received": len(certificates),
            "total_products_received": len(all_rows),
            "certificate_issuer_filter": CERTIFICATE_ISSUERS,
        },
        "interpretation_notes": [
            "All shares refer only to observed turnover in the fetched Avanza top lists, not total Swedish market share.",
            "The two broad product families are certificates and warrants/turbos/Mini Futures.",
            "Direction is retained in raw data but is deliberately not a central analytical dimension.",
            "Before Swedish products start trading, turnover should normally refer to the previous completed session.",
            "A product appearing in a top list is not proof that it was newly issued.",
            "The endpoints are undocumented and may change without notice.",
        ],
    }

    full_document = {
        **common,
        "analytics": analytics,
        # Keep legacy keys for compatibility and easy inspection.
        "warrants": warrants,
        "certificates": certificates,
        "raw_products": {
            "warrants_turbos_minis": warrants,
            "certificates": certificates,
        },
    }

    agent_document = {
        **common,
        "purpose": "Agent-optimised deterministic analysis; use latest_full.json only for deeper product-level drill-down.",
        "full_raw_file": "data/latest_full.json",
        "analytics": analytics,
        "product_highlights": _agent_product_highlights(all_rows),
    }

    history_document = {
        **common,
        "analytics": analytics,
        "product_highlights": _agent_product_highlights(all_rows),
    }

    latest_path = data_dir / "latest.json"
    latest_full_path = data_dir / "latest_full.json"
    history_path = data_dir / "history" / f"{fetched_at.date().isoformat()}.json"

    _write_json(latest_path, agent_document)
    _write_json(latest_full_path, full_document)
    _write_json(history_path, history_document)

    print(
        f"Saved {len(warrants)} warrants/turbos/minis and {len(certificates)} certificates. "
        f"Agent file: {latest_path}; full file: {latest_full_path}; history: {history_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
