"""포트폴리오 수준 지표 (docs/architecture.md §5.3).

집중도(HHI, Top-N), 회전율, 공시 지연, 보유 기간을 산출한다.
애크먼은 저회전·초집중 전략(2026Q1 기준 11종목 / Top-4 66%)이므로
회전율 급등이나 HHI 급락 자체가 이례 신호다.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from ..common.schema import QuarterMetrics
from .diff import position_key

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 헬퍼

def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _as_dict(rec) -> dict:
    """Event dataclass 와 dict 를 모두 받아준다."""
    if hasattr(rec, "__dataclass_fields__"):
        return {f: getattr(rec, f) for f in rec.__dataclass_fields__}
    return dict(rec)


def _parse_date(s) -> Optional[date]:
    try:
        y, m, d = str(s).split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def lag_days(report_date: str, filing_date: str) -> int:
    """filing_date - report_date. 파싱 불가 시 0."""
    r, f = _parse_date(report_date), _parse_date(filing_date)
    if r is None or f is None:
        return 0
    return (f - r).days


def _weights(holdings: list[dict]) -> list[float]:
    """비중 목록. weight_pct 가 전부 있으면 그것을, 아니면 value 로 재계산.

    혼용은 합계를 깨뜨리므로 전부/전무로만 분기한다.
    """
    stored = [_num(h.get("weight_pct")) for h in holdings]
    if holdings and all(w is not None for w in stored):
        return [float(w) for w in stored]
    total = sum(_int(h.get("value_usd")) for h in holdings)
    if total <= 0:
        return [0.0 for _ in holdings]
    log.debug("weight_pct 누락 -> value_usd 로 비중 재계산 (n=%d)", len(holdings))
    return [_int(h.get("value_usd")) / total * 100.0 for h in holdings]


def _top_n(sorted_weights: list[float], n: int) -> float:
    return round(sum(sorted_weights[:n]), 4)


def turnover_pct(prev: Optional[list[dict]], curr: list[dict]) -> Optional[float]:
    """Σ|value 변화| / 평균 포트폴리오 가치 × 100.

    신규/청산은 전액이 변화량으로 잡힌다. prev 가 없거나 비면 None.

    주의: 이 정의는 가격 변동분까지 포함한다 (CONTRACTS.md 의 정의를 따름).
    순수 매매 회전율을 원하면 Σ|Δshares × price| 를 별도로 계산해야 한다.
    """
    if not prev or not curr:
        return None
    pmap = {position_key(h): _int(h.get("value_usd")) for h in prev}
    cmap = {position_key(h): _int(h.get("value_usd")) for h in curr}
    delta = sum(abs(cmap.get(k, 0) - pmap.get(k, 0)) for k in set(pmap) | set(cmap))
    avg = (sum(pmap.values()) + sum(cmap.values())) / 2.0
    if avg <= 0:
        return None
    return round(delta / avg * 100.0, 4)


# ---------------------------------------------------------------- 분기 지표

def quarter_metrics(holdings: list[dict], events: list[dict],
                    prev: Optional[list[dict]]) -> QuarterMetrics:
    """한 분기의 포트폴리오 지표.

    HHI = Σ(weight/100)^2 — 단일 종목 100% 면 1.0, 균등 4종목이면 0.25.
    top1/3/5 는 비중 내림차순 누적. 포지션 수가 N 보다 적으면 전체 합.
    """
    rows = list(holdings or [])
    report_dates = sorted({str(h.get("report_date")) for h in rows if h.get("report_date")})
    filing_dates = sorted({str(h.get("filing_date")) for h in rows if h.get("filing_date")})
    report_date = report_dates[-1] if report_dates else ""
    # 최초 공시일 기준 (정정이 늦게 와도 지연일수가 왜곡되지 않게)
    filing_date = filing_dates[0] if filing_dates else ""

    weights = _weights(rows)
    hhi = round(sum((w / 100.0) ** 2 for w in weights), 6)
    desc = sorted(weights, reverse=True)

    counts = {"NEW": 0, "EXIT": 0, "ADD": 0, "TRIM": 0}
    for e in (events or []):
        d = _as_dict(e)
        t = d.get("event_type")
        if t in counts:
            counts[t] += 1

    return QuarterMetrics(
        report_date=report_date,
        filing_date=filing_date,
        lag_days=lag_days(report_date, filing_date),
        total_value_usd=sum(_int(h.get("value_usd")) for h in rows),
        position_count=len(rows),
        hhi=hhi,
        top1_pct=_top_n(desc, 1),
        top3_pct=_top_n(desc, 3),
        top5_pct=_top_n(desc, 5),
        turnover_pct=turnover_pct(prev, rows),
        new_count=counts["NEW"],
        exit_count=counts["EXIT"],
        add_count=counts["ADD"],
        trim_count=counts["TRIM"],
    )


# ---------------------------------------------------------------- 보유 기간

def holding_periods(all_holdings: list[dict]) -> dict[str, dict]:
    """CUSIP 별 {first_seen, last_seen, quarters_held, is_current}.

    quarters_held 는 '등장한 분기 수'이며 연속 구간이 아니다 —
    청산 후 재진입(스핀오프·재편입)이 실제로 발생하므로 누적 카운트가 맞다.
    is_current 는 데이터셋의 최신 report_date 에 존재하는지 여부.
    """
    rows = list(all_holdings or [])
    if not rows:
        return {}
    latest = max(str(h.get("report_date") or "") for h in rows)

    acc: dict[str, dict] = {}
    for h in rows:
        cusip = str(h.get("cusip") or "")
        if not cusip:
            continue
        rd = str(h.get("report_date") or "")
        e = acc.setdefault(cusip, {
            "cusip": cusip,
            "ticker": None,
            "issuer_name": "",
            "titles": set(),
            "_dates": set(),
        })
        e["_dates"].add(rd)
        e["titles"].add(str(h.get("title_of_class") or ""))
        if h.get("ticker"):
            e["ticker"] = h["ticker"]
        if h.get("issuer_name"):
            e["issuer_name"] = h["issuer_name"]

    out: dict[str, dict] = {}
    for cusip, e in acc.items():
        dates = sorted(d for d in e["_dates"] if d)
        out[cusip] = {
            "cusip": cusip,
            "ticker": e["ticker"],
            "issuer_name": e["issuer_name"],
            "titles": sorted(e["titles"]),
            "first_seen": dates[0] if dates else "",
            "last_seen": dates[-1] if dates else "",
            "quarters_held": len(dates),
            "is_current": latest in e["_dates"],
        }
    return out
