"""Parse `cz/cbs/bb/queryFreeUnit` into a normalized mobile-data reading.

Real response (2026-11-18), abridged:
    body: [ { freeUnitType: "MOBILE_INTERNET_USAGE_PREPAID", freeUnitTypeName: "Mobile Internet",
              measureUnit: "1108", offerName: "Nitro Prime 105. Prepaid",
              total: 12500.0, used: 8553.22, remain: 3946.78, actualRemain: 3946.78,
              freeUnitBeanDetailList: [
                {initialAmount: 6250, currentAmount: 3946.78, expireTime: <ms>, remainingDaysForRenewal: 20, ...},
                {initialAmount: 6250, currentAmount: 0.0,     expireTime: <ms>, remainingDaysForRenewal: 2, ...} ] } ]

Gotcha: the top-level `total` adds up every bundle still in its validity window, including one
already used up (above: 6250 + 6250, of which 6250 is gone). "X of 12500" would be misleading,
so `total_mb` counts only bundles that still have data. The portal totals are kept as well.

measureUnit codes come from the portal's CBS_MEASURE_UNIT dictionary: 1106 Bytes, 1107 KB,
1108 MB, 1109 GB. An unknown code raises PortalChanged instead of being guessed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import PortalChanged
from .logs import event, get_logger, iso

log = get_logger("quota")

UNIT_TO_MB = {"1106": 1 / (1024 * 1024), "1107": 1 / 1024, "1108": 1.0, "1109": 1024.0}
MOBILE_PREFIX = "MOBILE_INTERNET"


def _ts(ms: Any) -> str | None:
    try:
        return iso(datetime.fromtimestamp(int(ms) / 1000, timezone.utc))
    except (TypeError, ValueError):
        return None


def _mb(value: Any, unit: str, where: str) -> float:
    if unit not in UNIT_TO_MB:
        raise PortalChanged(f"unknown measureUnit {unit!r} in {where}; add it to UNIT_TO_MB")
    return round(float(value or 0) * UNIT_TO_MB[unit], 2)


@dataclass
class Bucket:
    offer: str | None
    origin: str | None
    initial_mb: float
    remaining_mb: float
    effective_at: str | None
    expires_at: str | None
    renews_in_days: int | None


@dataclass
class Quota:
    remaining_mb: float
    total_mb: float            # initial size of the bundles that still have data
    used_mb: float             # total_mb - remaining_mb
    portal_total_mb: float     # WE's own figure (includes exhausted bundles)
    portal_used_mb: float
    offer: str | None
    expires_at: str | None     # earliest expiry among bundles that still have data
    renews_in_days: int | None
    fetched_at: str
    buckets: list[Bucket] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        gb = lambda mb: f"{mb / 1024:.2f} GB"
        tail = f", renews in {self.renews_in_days} d" if self.renews_in_days is not None else ""
        return f"{gb(self.remaining_mb)} left of {gb(self.total_mb)}{tail}"


def parse_free_units(payload: dict[str, Any]) -> Quota:
    header = payload.get("header") or {}
    if str(header.get("retCode")) != "0":
        raise PortalChanged(f"queryFreeUnit retCode {header.get('retCode')}: {header.get('errorMsg')}")
    entries = payload.get("body")
    if not isinstance(entries, list):
        raise PortalChanged(f"queryFreeUnit body is {type(entries).__name__}, expected list")

    mobile = [e for e in entries if str(e.get("freeUnitType", "")).upper().startswith(MOBILE_PREFIX)]
    if not mobile:
        types = [e.get("freeUnitType") for e in entries]
        raise PortalChanged(f"no {MOBILE_PREFIX}* free unit in response (types present: {types})")

    buckets: list[Bucket] = []
    remaining = portal_total = portal_used = 0.0
    offer = None
    for e in mobile:
        unit = str(e.get("measureUnit"))
        where = e.get("freeUnitType")
        rem = _mb(e.get("actualRemain", e.get("remain")), unit, where)
        remaining += rem
        portal_total += _mb(e.get("total"), unit, where)
        portal_used += _mb(e.get("used"), unit, where)
        offer = offer or e.get("offerName")
        details = e.get("freeUnitBeanDetailList") or []
        for d in details:
            dunit = str(d.get("measureUnit") or unit)
            buckets.append(Bucket(
                offer=d.get("offeringName"), origin=d.get("originType"),
                initial_mb=_mb(d.get("initialAmount"), dunit, where),
                remaining_mb=_mb(d.get("currentAmount"), dunit, where),
                effective_at=_ts(d.get("effectiveTime")), expires_at=_ts(d.get("expireTime")),
                renews_in_days=d.get("remainingDaysForRenewal")))
        if details:
            detail_sum = sum(_mb(d.get("currentAmount"), str(d.get("measureUnit") or unit), where) for d in details)
            if abs(detail_sum - rem) > 1.0:
                event(log, logging.WARNING, "quota.detail_mismatch", type=where, top=rem, details=detail_sum)
        else:
            buckets.append(Bucket(offer=e.get("offerName"), origin=None, initial_mb=_mb(e.get("total"), unit, where),
                                  remaining_mb=rem, effective_at=_ts(e.get("effectiveTime")),
                                  expires_at=_ts(e.get("expireTime")), renews_in_days=None))

    live = [b for b in buckets if b.remaining_mb > 0]
    if live:
        total = sum(b.initial_mb for b in live)
        soonest = min(live, key=lambda b: b.expires_at or "9999")
    else:  # everything used up: report against the newest bundle
        newest = max(buckets, key=lambda b: b.effective_at or "")
        total, soonest = newest.initial_mb, newest
    return Quota(
        remaining_mb=round(remaining, 2), total_mb=round(total, 2), used_mb=round(max(total - remaining, 0), 2),
        portal_total_mb=round(portal_total, 2), portal_used_mb=round(portal_used, 2), offer=offer,
        expires_at=soonest.expires_at, renews_in_days=soonest.renews_in_days, fetched_at=iso(), buckets=buckets)
