"""Parser tests against the real queryFreeUnit response captured on 2026-11-18 (number anonymized)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.errors import PortalChanged
from app.quota import parse_free_units

REAL = json.loads((Path(__file__).parent / "fixtures" / "free_unit_2026-11-18.json").read_text(encoding="utf-8"))


def test_real_response():
    q = parse_free_units(REAL)
    assert q.remaining_mb == 3946.78
    assert q.total_mb == 6250.0               # only the bundle that still has data
    assert q.used_mb == 2303.22
    assert q.portal_total_mb == 12500.0       # WE's figure, includes the exhausted bundle
    assert q.offer == "Nitro Prime 105. Prepaid"
    assert q.renews_in_days == 20
    assert q.expires_at.startswith("2026-12-08T22:00")  # 2026-12-09 00:00 Cairo
    assert len(q.buckets) == 2
    assert "3.85 GB left of 6.10 GB" in q.summary()


def test_all_buckets_exhausted_uses_newest_bundle():
    p = copy.deepcopy(REAL)
    e = p["body"][0]
    e["remain"] = e["actualRemain"] = 0.0
    for d in e["freeUnitBeanDetailList"]:
        d["currentAmount"] = 0.0
    q = parse_free_units(p)
    assert q.remaining_mb == 0 and q.total_mb == 6250.0 and q.renews_in_days == 20


def test_gb_unit_converted():
    p = copy.deepcopy(REAL)
    e = p["body"][0]
    e["measureUnit"] = "1109"
    e["remain"] = e["actualRemain"] = 2.0
    e["freeUnitBeanDetailList"] = []
    assert parse_free_units(p).remaining_mb == 2048.0


def test_unknown_unit_is_loud():
    p = copy.deepcopy(REAL)
    p["body"][0]["measureUnit"] = "9999"
    with pytest.raises(PortalChanged, match="measureUnit"):
        parse_free_units(p)


def test_no_mobile_bucket_is_loud():
    p = copy.deepcopy(REAL)
    p["body"][0]["freeUnitType"] = "VOICE_MINUTES"
    with pytest.raises(PortalChanged, match="VOICE_MINUTES"):
        parse_free_units(p)


def test_error_header_is_loud():
    with pytest.raises(PortalChanged):
        parse_free_units({"header": {"retCode": "9999", "errorMsg": "x"}, "body": []})
