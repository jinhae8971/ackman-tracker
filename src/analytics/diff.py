"""분기 간 포지션 변화 감지 (docs/architecture.md §5.1, §5.2).

순수 함수만 둔다 — 네트워크 호출도, 파일 I/O 도 하지 않는다.
Collector 가 만든 holdings 딕셔너리를 읽어 Event 목록을 만드는 것이 전부다.

핵심 규율 세 가지:
  1. 포지션 동일성 키는 ``(cusip, title_of_class)``. CUSIP 단독은 금지 —
     Alphabet 이 Cl A(02079K305) / Cl C(02079K107) 두 행으로 나온다.
  2. 주식 분할 보정(:func:`adjust_splits`)이 diff 보다 **먼저** 실행된다.
     무보정 시 ADD 오탐의 최대 원인이다.
  3. |share_delta_pct| <= MIN_DELTA_PCT 는 HOLD (노이즈 억제).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from ..common.schema import (
    MIN_DELTA_PCT,
    STRONG_DELTA_BP,
    STRONG_NEW_WEIGHT_PCT,
    Event,
    make_event_id,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 분할 판정 상수

SPLIT_RATIO_TOL = 0.02        # 주식 수 비율이 정수배에 ±2% 이내면 분할 후보
SPLIT_MAX_FACTOR = 50         # 50:1 까지 분할로 인정 (CMG 2024-06 50:1 실사례)
SPLIT_VALUE_DRIFT_MAX = 1.5   # 분할이면 평가액은 유지된다: 비율이 [1/1.5, 1.5] 밖이면 매매
SPLIT_MIN_REVERSE_FACTOR = 4  # 역분할은 1:4 이상만 인정 — 아래 주석 참조

# 역분할 하한을 4로 둔 이유: "포지션의 절반을 팔았다"는 흔한 재량 매매이고 주식 수
# 비율이 정확히 0.5 로 떨어진다. 같은 분기에 주가가 오르면 평가액까지 유지되어
# 1:2 역분할과 구분이 불가능해진다. 반면 실제 역분할은 대부분 1:5, 1:10, 1:20 이다.
# 흔한 TRIM 을 HOLD 로 삼키는 것보다 드문 1:2 역분할을 TRIM 으로 흘리는 편이 안전하다.
#
# 정방향 상한을 50 으로 올린 이유: Chipotle(CMG)이 2024-06 에 50:1 분할을 했고
# 실제 13F 에 743,984 -> 28,815,165 주로 잡힌다. 상한이 20 이면 이 행이
# "+3773% ADD"(STRONG_ADD 급 오탐)로 흘러나온다 — 평가액은 오히려 -16.5% 인데도.
# 상한을 올려도 오검출이 늘지 않는 이유는 판정의 실질적 관문이 배수 자체가 아니라
# _value_supports_split 의 ±50% 평가액 밴드이기 때문이다. 실제로 Allergan
# 2014-06(주식 수 x48.3)은 평가액이 x65.9 로 뛰므로 여전히 ADD 로 남는다.
#
# 알려진 한계: 분할과 재량 매매가 같은 분기에 겹치면 13F 만으로 배수를 특정할 수
# 없다. CMG 도 분할과 동시에 약 22% 를 덤어냈고 주식 수 비율은 38.73(≈39)이 되어
# 보정 후 HOLD 로 남는다(실제로는 TRIM). 배수가 틀린 HOLD 가 +3773% ADD 보다는
# 훨씬 덜 해롭다는 판단이며, 정확히 잡으려면 외부 분할 캘린더가 필요하다.


# ---------------------------------------------------------------- 내부 헬퍼

def position_key(h: dict) -> tuple:
    """포지션 동일성 키. schema.Holding.key 와 동일한 정의.

    put_call 을 포함해야 같은 종목의 보통주와 PUT/CALL 이 서로 다른 포지션으로
    남는다. 빼면 옵션 보유가 보통주 포지션을 덮어써 매수/매도 방향이 뒤집힌다.
    """
    return (str(h.get("cusip") or ""),
            str(h.get("title_of_class") or ""),
            str(h.get("put_call") or ""))


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) or math.isinf(f) else f


def _index(holdings: list, label: str = "") -> dict:
    """position_key -> holding. 중복 키는 경고 후 마지막 행 채택.

    수집 단계(parse_13f.aggregate_rows)에서 이미 합산했으므로 정상 데이터라면
    중복은 나오지 않는다. 그래도 남는다면 상류 계약이 깨진 것이므로 조용히
    삼키지 않고 경고를 남긴다.
    """
    out = {}
    for h in holdings:
        k = position_key(h)
        if k in out:
            log.warning(
                "duplicate position key %s in %s quarter (put_call=%r) — 마지막 행 채택",
                k, label or "?", h.get("put_call"),
            )
        out[k] = h
    return out


def _quarter_report_date(curr: list) -> str:
    dates = sorted({str(h.get("report_date")) for h in curr if h.get("report_date")})
    return dates[-1] if dates else ""


def _quarter_filing_date(curr: list) -> str:
    """분기 대표 제출일 = 최초 공시일(min).

    정정(13F-HR/A)이 몇 달 뒤 도착하면 max 는 지연일수를 왜곡한다.
    포트폴리오가 처음 공개된 날이 사용자에게 의미 있는 값이므로 min 을 쓴다.
    """
    dates = sorted({str(h.get("filing_date")) for h in curr if h.get("filing_date")})
    return dates[0] if dates else ""


# ---------------------------------------------------------------- 분할 보정

def _split_factor(prev_shares: int, curr_shares: int) -> Optional[float]:
    """주식 수 비율만으로 분할 배수 후보를 구한다. 아니면 None.

    정방향 분할(2:1)은 2.0, 역분할(1:10)은 0.1 을 돌려준다.
    """
    if prev_shares <= 0 or curr_shares <= 0:
        return None
    ratio = curr_shares / prev_shares
    if ratio >= 1.0:
        n = round(ratio)
        if 2 <= n <= SPLIT_MAX_FACTOR and abs(ratio - n) / n <= SPLIT_RATIO_TOL:
            return float(n)
        return None
    inv = prev_shares / curr_shares
    n = round(inv)
    if (SPLIT_MIN_REVERSE_FACTOR <= n <= SPLIT_MAX_FACTOR
            and abs(inv - n) / n <= SPLIT_RATIO_TOL):
        return 1.0 / n
    return None


def _value_supports_split(prev_value: int, curr_value: int, factor: float) -> bool:
    """value 가 주식 수에 '비례해서' 움직였는지 검사.

    순수 분할이면 주당 가격이 배수만큼 나뉘므로 평가액은 대략 유지된다(비율 ≈ 1).
    실제 매수라면 평가액도 배수만큼 늘어난다(비율 ≈ factor).

    두 조건을 모두 요구한다:
      * 로그 거리상 1 이 factor 보다 가까울 것 (factor=2 면 value 비율 < 1.414)
      * 평가액 변동 자체가 시장 드리프트 범위(±50%) 안일 것 —
        배수가 클수록(10:1 등) 첫 조건만으로는 느슨해지므로 이 밴드가 조인다.
    """
    if prev_value <= 0 or curr_value <= 0:
        return False  # 판단 근거 없음 -> 보정하지 않는다 (보수적)
    vr = curr_value / prev_value
    if not (1.0 / SPLIT_VALUE_DRIFT_MAX <= vr <= SPLIT_VALUE_DRIFT_MAX):
        return False
    return abs(math.log(vr)) < abs(math.log(vr / factor))


def adjust_splits(prev: list, curr: list) -> list:
    """주식 분할을 감지해 ``prev`` 의 shares 를 현재 분기 기준으로 환산한다.

    판정 조건 (둘 다 만족해야 보정):
      * 주식 수 비율이 정수배(2,3,4,10...)에 ±SPLIT_RATIO_TOL 이내로 근접
      * value 변동이 그 배수에 비례하지 **않음** (분할이면 평가액은 유지)

    보정 사실은 로그로만 남기고 Event 로 발행하지 않는다.
    반환값은 prev 의 얕은 복사본 목록이며, 보정된 행에는 감사용으로
    ``split_factor`` 키가 붙는다 (holdings.jsonl 로 되돌아가지 않는다).
    원본 ``prev`` 리스트와 그 안의 dict 는 변경하지 않는다.
    """
    cmap = _index(curr, "curr")
    out = []
    for h in prev:
        row = dict(h)
        c = cmap.get(position_key(h))
        if c is not None:
            f = _split_factor(_int(h.get("shares")), _int(c.get("shares")))
            if f is not None and _value_supports_split(
                _int(h.get("value_usd")), _int(c.get("value_usd")), f
            ):
                adjusted = int(round(_int(h.get("shares")) * f))
                log.info(
                    "split adjusted %s %s: %s -> %s shares (x%s, report_date=%s)",
                    h.get("cusip"), h.get("ticker") or h.get("issuer_name"),
                    _int(h.get("shares")), adjusted,
                    round(f, 4), c.get("report_date"),
                )
                row["shares"] = adjusted
                row["split_factor"] = f
        out.append(row)
    return out


# ---------------------------------------------------------------- 분류

def _event_type(prev: Optional[dict], curr: Optional[dict],
                share_delta_pct: Optional[float]) -> str:
    if prev is None:
        return "NEW"
    if curr is None:
        return "EXIT"
    if share_delta_pct is None:
        return "HOLD"
    if share_delta_pct > MIN_DELTA_PCT:
        return "ADD"
    if share_delta_pct < -MIN_DELTA_PCT:
        return "TRIM"
    return "HOLD"


def classify_conviction(ev: Event) -> str:
    """확신도 등급 (docs/architecture.md §5.2).

    증감률만 쓰면 '10억 달러의 3% 증량'과 '1000만 달러의 300% 증량'을
    구분하지 못하므로 비중(bp)과 진입 규모를 함께 본다.
    """
    if ev.event_type == "EXIT":
        return "FULL_EXIT"
    if ev.event_type == "NEW" and _float(ev.weight_after) >= STRONG_NEW_WEIGHT_PCT:
        return "STRONG_NEW"
    if ev.event_type == "ADD" and _float(ev.weight_delta_bp) >= STRONG_DELTA_BP:
        return "STRONG_ADD"
    if ev.event_type == "TRIM" and _float(ev.weight_delta_bp) <= -STRONG_DELTA_BP:
        return "STRONG_TRIM"
    return "ROUTINE"


# ---------------------------------------------------------------- diff

def quarter_diff(prev: list, curr: list) -> list:
    """두 연속 분기를 (cusip, title_of_class) 로 outer join 해 Event 목록 생성.

    prev 가 비어 있으면(최초 분기) curr 전체가 NEW 가 된다.
    curr 가 비어 있으면 기준 분기 자체가 없으므로 빈 목록을 돌려준다
    (13F 는 0종목으로 제출되지 않는다; 무결성 게이트가 별도로 잡는다).
    """
    prev_rows = list(prev or [])
    curr_rows = list(curr or [])
    if not curr_rows:
        if prev_rows:
            log.warning("quarter_diff: curr 가 비어 있어 이벤트를 생성하지 않는다")
        return []

    prev_adj = adjust_splits(prev_rows, curr_rows)
    pmap = _index(prev_adj, "prev")
    cmap = _index(curr_rows, "curr")

    report_date = _quarter_report_date(curr_rows)
    quarter_filing = _quarter_filing_date(curr_rows)

    events = []
    for key in sorted(set(pmap) | set(cmap)):
        p = pmap.get(key)
        c = cmap.get(key)

        prev_shares = _int(p.get("shares")) if p else 0
        curr_shares = _int(c.get("shares")) if c else 0
        prev_value = _int(p.get("value_usd")) if p else 0
        curr_value = _int(c.get("value_usd")) if c else 0
        weight_before = round(_float(p.get("weight_pct")), 4) if p else 0.0
        weight_after = round(_float(c.get("weight_pct")), 4) if c else 0.0

        if p is None:
            share_delta_pct = None          # 분모 없음
        elif c is None:
            share_delta_pct = -100.0
        elif prev_shares > 0:
            share_delta_pct = round((curr_shares - prev_shares) / prev_shares * 100.0, 4)
        else:
            share_delta_pct = None

        etype = _event_type(p, c, share_delta_pct)
        src = c if c is not None else p

        ev = Event(
            event_id=make_event_id(report_date, key[0], etype),
            report_date=report_date,
            filing_date=str((c or {}).get("filing_date") or quarter_filing),
            event_type=etype,
            conviction="ROUTINE",
            cusip=key[0],
            ticker=(c or {}).get("ticker") or (p or {}).get("ticker"),
            issuer_name=str(src.get("issuer_name") or ""),
            prev_shares=prev_shares,
            curr_shares=curr_shares,
            share_delta_pct=share_delta_pct,
            prev_value_usd=prev_value,
            curr_value_usd=curr_value,
            value_delta_usd=curr_value - prev_value,
            weight_before=weight_before,
            weight_after=weight_after,
            weight_delta_bp=round((weight_after - weight_before) * 100.0, 4),
            source="13F",
            provisional=False,
        )
        ev.conviction = classify_conviction(ev)
        events.append(ev)

    # 결정적 정렬: 현재 평가액 큰 순 -> 이전 평가액 큰 순 -> CUSIP
    events.sort(key=lambda e: (-e.curr_value_usd, -e.prev_value_usd, e.cusip, e.event_type))
    return events
