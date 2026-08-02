#!/usr/bin/env python3
"""dashboard/sample_data.py — 계약 준수 합성 데이터 생성기.

Collector(Agent A) / Analytics(Agent B) 산출물이 아직 없으므로,
대시보드 개발·검증을 위해 `src/common/schema.py` 계약과 100% 동일한 형태의
holdings / filings / events / metrics JSONL 을 생성한다.

**출력 위치는 기본적으로 `dashboard/sample/` 이다.**
`data/normalized/` 에 쓰지 않는 이유: 그 디렉터리는 Collector/Analytics 담당이며,
합성 데이터로 덮어쓰면 병렬 작업 중인 다른 에이전트의 산출물을 파괴한다.
`build.py` 는 `data/normalized/` 를 우선 조회하고, 비어 있을 때만 이 샘플로 폴백한다.

수치 기준선 (docs/architecture.md 부록 A, PROJECT.md 검증 기준선):
  - 2026-03-31 : 11종목 / $13,714,299,861 (전 종목 평가액·주식수 실측 고정)
  - 2024-12-31 : 11종목 / $12,661,093,451 (원본 10종목 $12,614,560,346
                 + NEW HOLDINGS 정정 Hertz $46,533,105 병합 결과)

사용:
    python3 dashboard/sample_data.py [--out DIR] [--force]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

# ----------------------------------------------------------------- 공유 계약 로드
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from src.common.schema import (  # type: ignore
        Holding, Filing, Event, QuarterMetrics,
        write_jsonl, make_event_id,
        MIN_DELTA_PCT, STRONG_NEW_WEIGHT_PCT, STRONG_DELTA_BP,
    )
    _HAVE_SCHEMA = True
except Exception as exc:  # pragma: no cover - 계약 파일은 항상 있어야 정상
    print(f"[FATAL] src/common/schema.py 를 import 할 수 없습니다: {exc}", file=sys.stderr)
    print("        리포지토리 루트에서 실행했는지 확인하세요.", file=sys.stderr)
    raise SystemExit(1)


# ----------------------------------------------------------------- 종목 마스터
# (ticker) -> cusip / 발행사명 / 증권 클래스
# Alphabet 은 CUSIP 이 다른 별개 행이며 (cusip, title_of_class) 로 키를 잡는다.
SECURITIES = {
    "BN":    ("11271J107", "BROOKFIELD CORP",           "CL A LTD VT SH"),
    "AMZN":  ("023135106", "AMAZON COM INC",            "COM"),
    "UBER":  ("90353T100", "UBER TECHNOLOGIES INC",     "COM"),
    "MSFT":  ("594918104", "MICROSOFT CORP",            "COM"),
    "QSR":   ("76131D103", "RESTAURANT BRANDS INTL INC","COM"),
    "META":  ("30303M102", "META PLATFORMS INC",        "CL A"),
    "HHH":   ("44267T102", "HOWARD HUGHES HLDGS INC",   "COM"),
    "SEG":   ("812215200", "SEAPORT ENTERTAINMENT GROUP","COM"),
    "GOOG":  ("02079K107", "ALPHABET INC",              "CAP STK CL C"),
    "HTZ":   ("42806J700", "HERTZ GLOBAL HOLDINGS INC", "COM"),
    "GOOGL": ("02079K305", "ALPHABET INC",              "CAP STK CL A"),
    # 과거 보유 후 청산된 종목 (EXIT 이벤트 생성용)
    "CMG":   ("169656105", "CHIPOTLE MEXICAN GRILL INC","COM"),
    "CP":    ("13646K108", "CANADIAN PACIFIC KANSAS CITY","COM"),
    "FNMA":  ("313586109", "FEDERAL NATL MTG ASSN",     "COM"),
    "FMCC":  ("313400301", "FEDERAL HOME LN MTG CORP",  "COM"),
}

# 2026-03-31 실측 평가액/주식수 (docs/architecture.md 부록 A) — 하드 고정
Q1_2026_EXACT = {
    "BN":    (2_415_946_008, 59_697_208),
    "AMZN":  (2_385_104_083, 11_451_981),
    "UBER":  (2_154_934_398, 29_958_771),
    "MSFT":  (2_092_970_053,  5_654_078),
    "QSR":   (1_673_501_194, 22_645_483),
    "META":  (1_522_358_404,  2_660_861),
    "HHH":   (1_192_581_569, 18_852_064),
    "SEG":   (  107_910_794,  5_023_780),
    "GOOG":  (   89_421_720,    311_726),
    "HTZ":   (   70_261_595, 15_241_127),
    "GOOGL": (    9_310_043,     32_376),
}
Q1_2026_TOTAL = 13_714_299_861

# 2024-12-31 병합 결과 (PROJECT.md 알려진 함정)
Q4_2024_TOTAL = 12_661_093_451
Q4_2024_HTZ_VALUE = 46_533_105          # NEW HOLDINGS 정정으로 추가된 단일 종목
Q4_2024_BASE_TOTAL = Q4_2024_TOTAL - Q4_2024_HTZ_VALUE   # 12,614,560,346

# ----------------------------------------------------------------- 분기 시나리오
# 각 분기: 종목 -> (주식수, 참조 단가). 평가액 = round(주식수 * 단가) 후
# target_total 이 지정된 분기는 비례 스케일링으로 정확히 맞춘다.
QUARTERS = [
    dict(
        report_date="2024-06-30", filing_date="2024-08-14",
        accession="0001172661-24-002655",
        pos={
            "QSR":   (22_600_000,  68.00),
            "HHH":   (18_850_000,  86.00),
            "META":  ( 3_200_000, 500.00),
            "CMG":   (28_900_000,  62.00),
            "CP":    (15_200_000,  80.00),
            "FNMA":  (115_000_000,  1.50),
            "FMCC":  (63_300_000,   1.40),
            "GOOG":  ( 1_480_000, 178.00),
            "GOOGL": (   590_000, 176.00),
        },
    ),
    dict(
        report_date="2024-09-30", filing_date="2024-11-14",
        accession="0001172661-24-003418",
        pos={
            "QSR":   (22_600_000,  69.50),
            "HHH":   (18_850_000,  68.00),
            "META":  ( 3_200_000, 570.00),
            "CMG":   (26_000_000,  57.00),
            "CP":    (15_200_000,  84.00),
            "FNMA":  (115_000_000,  1.60),
            "FMCC":  (63_300_000,   1.55),
            "SEG":   ( 5_023_780,  26.00),   # HHH 스핀오프로 신규 편입
            "GOOG":  ( 1_480_000, 165.00),
            "GOOGL": (   590_000, 163.00),
        },
    ),
    dict(
        report_date="2024-12-31", filing_date="2025-02-14",
        accession="0001172661-25-001119",
        # NEW HOLDINGS 정정으로 Hertz 1종목이 나중에 병합됨 (금액 고정)
        amend_accession="0001172661-25-001497",
        amend_filing_date="2025-04-16",
        amend_type="NEW HOLDINGS",
        amend_symbols=["HTZ"],
        fixed={"HTZ": (Q4_2024_HTZ_VALUE, 12_500_000)},
        target_total=Q4_2024_TOTAL,
        pos={
            "QSR":   (22_645_483,  66.00),
            "HHH":   (18_852_064,  71.00),
            "META":  ( 3_050_000, 590.00),
            "CMG":   (22_000_000,  60.00),
            "CP":    (15_200_000,  76.00),
            "FNMA":  (118_000_000,  4.20),   # 2024 대선 후 급등
            "FMCC":  (63_300_000,   4.00),
            "SEG":   ( 5_023_780,  24.00),
            "GOOG":  ( 1_480_000, 190.00),
            "GOOGL": (   590_000, 188.00),
        },
    ),
    dict(
        report_date="2025-03-31", filing_date="2025-05-15",
        accession="0001172661-25-001743",
        pos={
            "BN":    (55_000_000,  34.00),   # STRONG_NEW
            "UBER":  (26_500_000,  60.00),   # STRONG_NEW
            "QSR":   (22_645_483,  65.00),
            "HHH":   (18_852_064,  68.00),
            "META":  ( 2_900_000, 610.00),
            "CP":    (12_000_000,  72.00),   # TRIM
            "FNMA":  (118_000_000,  4.00),
            "FMCC":  (63_300_000,   3.80),
            "SEG":   ( 5_023_780,  22.00),
            "HTZ":   (15_241_127,   7.50),   # ADD + 급등
            "GOOG":  ( 1_480_000, 155.00),
            "GOOGL": (   590_000, 153.00),
            # CMG 청산 -> FULL_EXIT
        },
    ),
    dict(
        report_date="2025-06-30", filing_date="2025-08-14",
        accession="0001172661-25-002204",
        pos={
            "BN":    (58_000_000,  36.50),
            "AMZN":  ( 8_900_000, 178.00),   # STRONG_NEW
            "UBER":  (28_000_000,  65.00),
            "QSR":   (22_645_483,  67.00),
            "HHH":   (18_852_064,  70.00),
            "META":  ( 2_750_000, 640.00),
            "FNMA":  (118_000_000,  5.50),
            "FMCC":  (63_300_000,   5.20),
            "SEG":   ( 5_023_780,  21.00),
            "HTZ":   (15_241_127,   5.20),
            "GOOG":  (   700_000, 176.00),   # STRONG_TRIM
            "GOOGL": (   200_000, 174.00),
            # CP 청산 -> FULL_EXIT
        },
    ),
    dict(
        report_date="2025-09-30", filing_date="2025-11-14",
        accession="0001172661-25-002877",
        pos={
            "BN":    (59_000_000,  38.20),
            "AMZN":  (10_500_000, 195.00),
            "UBER":  (29_500_000,  68.00),
            "MSFT":  ( 4_900_000, 340.00),   # STRONG_NEW
            "QSR":   (22_645_483,  70.00),
            "HHH":   (18_852_064,  72.00),
            "META":  ( 2_700_000, 655.00),
            "FNMA":  (118_000_000,  6.20),
            "SEG":   ( 5_023_780,  22.50),
            "HTZ":   (15_241_127,   6.00),
            "GOOG":  (   350_000, 245.00),
            "GOOGL": (    40_000, 243.00),
            # FMCC 청산 -> FULL_EXIT
        },
    ),
    dict(
        report_date="2025-12-31", filing_date="2026-02-13",
        accession="0001172661-26-000781",
        pos={
            "BN":    (59_697_208,  39.10),
            "AMZN":  (11_200_000, 201.00),
            "UBER":  (29_958_771,  70.00),
            "MSFT":  ( 5_654_078, 355.00),   # ADD
            "QSR":   (22_645_483,  72.00),
            "META":  ( 2_660_861, 600.00),
            "HHH":   (18_852_064,  65.00),
            "SEG":   ( 5_023_780,  22.00),
            "GOOG":  (   311_726, 270.00),
            "HTZ":   (15_241_127,   5.00),
            "GOOGL": (    32_376, 268.00),
            # FNMA 청산 -> FULL_EXIT
        },
    ),
    dict(
        report_date="2026-03-31", filing_date="2026-05-15",
        accession="0001172661-26-002336",
        target_total=Q1_2026_TOTAL,
        fixed={k: v for k, v in Q1_2026_EXACT.items()},
        pos={},   # 전 종목 고정값
    ),
]

# ----------------------------------------------------------------- 보조 공시
# events/holdings 와 무관하게 화면 5(공시 인덱스)와 화면 3 드릴다운 타임라인을 채운다.
# `subject_cusip` 은 스키마 필수 필드가 아닌 선택 확장 필드다 (build.py 가 있으면 사용).
OTHER_FILINGS = [
    ("0001172661-24-002101", "SC 13D/A", "2024-07-09", "44267T102", "Item 4 개정 — 지배구조 제안"),
    ("0001172661-24-002480", "4",        "2024-08-02", "44267T102", "내부자 거래 신고"),
    ("0001172661-24-003002", "SC 13D",   "2024-10-01", "812215200", "스핀오프 신주 취득 신고"),
    ("0001172661-24-003555", "SC 13G/A", "2024-11-27", "76131D103", "연말 지분율 갱신"),
    ("0001172661-25-000410", "8-K",      "2025-01-21", None,        "펀드 공지"),
    ("0001172661-25-000902", "SC 13D/A", "2025-02-03", "42806J700", "Hertz 지분 5% 돌파"),
    ("0001172661-25-001497", "13F-HR/A", "2025-04-16", "42806J700", "NEW HOLDINGS 정정 (Hertz)"),
    ("0001172661-25-001820", "DFAN14A",  "2025-05-28", "44267T102", "위임장 캠페인 자료"),
    ("0001172661-25-001955", "DFAN14A",  "2025-06-11", "44267T102", "위임장 캠페인 자료 2"),
    ("0001172661-25-002090", "425",      "2025-07-22", "812215200", "합병 관련 커뮤니케이션"),
    ("0001172661-25-002455", "4",        "2025-09-08", "44267T102", "내부자 거래 신고"),
    ("0001172661-25-002710", "SC 13D/A", "2025-10-17", "42806J700", "지분율 변동 보고"),
    ("0001172661-25-003011", "SC 13G",   "2025-12-02", "594918104", "수동적 보유 신고"),
    ("0001172661-26-000233", "4",        "2026-01-15", "812215200", "내부자 거래 신고"),
    ("0001172661-26-001044", "SC 13D/A", "2026-03-04", "44267T102", "Item 5 지분율 갱신"),
    ("0001172661-26-001902", "PREN14A",  "2026-04-21", "44267T102", "위임장 경쟁 예비 자료"),
    ("0001172661-26-002891", "SC 13D/A", "2026-06-12", "44267T102", "분기말 후 추가 매집 (13F 미확정)"),
    ("0001172661-26-003140", "SC 13D/A", "2026-07-08", "812215200", "분기말 후 추가 매집 (13F 미확정)"),
]

# 13D 기반 조기(잠정) 이벤트 — 아직 13F 로 확정되지 않은 구간
PROVISIONAL = [
    dict(
        report_date="2026-06-30", filing_date="2026-06-12",
        accession="0001172661-26-002891", sym="HHH",
        prev_shares=18_852_064, curr_shares=21_400_000,
        source="13D",
    ),
    dict(
        report_date="2026-06-30", filing_date="2026-07-08",
        accession="0001172661-26-003140", sym="SEG",
        prev_shares=5_023_780, curr_shares=6_610_000,
        source="13D",
    ),
]

SCHEMA_VERSION = "X0202"   # 2023-02 이후 파일링은 전부 달러 단위 스키마


# ----------------------------------------------------------------- 값 계산
def _resolve_values(q: dict) -> dict:
    """분기 시나리오 -> {sym: (value_usd, shares)}."""
    fixed = dict(q.get("fixed") or {})
    raw = {s: (shares * price, shares) for s, (shares, price) in q["pos"].items()}
    target = q.get("target_total")

    out: dict = {}
    if target is None:
        for s, (v, sh) in raw.items():
            out[s] = (int(round(v)), sh)
        for s, (v, sh) in fixed.items():
            out[s] = (int(v), int(sh))
        return out

    fixed_sum = sum(int(v) for v, _ in fixed.values())
    remaining = target - fixed_sum
    raw_sum = sum(v for v, _ in raw.values())
    if raw and raw_sum <= 0:
        raise ValueError(f"{q['report_date']}: raw 합계가 0 이하")

    if raw:
        scale = remaining / raw_sum
        for s, (v, sh) in raw.items():
            out[s] = (int(round(v * scale)), sh)
        # 반올림 잔차는 최대 포지션에 흡수시켜 합계를 정확히 맞춘다
        drift = remaining - sum(v for v, _ in out.values())
        if drift:
            biggest = max(out, key=lambda s: out[s][0])
            out[biggest] = (out[biggest][0] + drift, out[biggest][1])
    elif remaining != 0:
        raise ValueError(f"{q['report_date']}: 고정값 합계가 target_total 과 불일치")

    for s, (v, sh) in fixed.items():
        out[s] = (int(v), int(sh))
    return out


def build_holdings() -> tuple[list, list]:
    """holdings.jsonl 레코드와 분기별 스냅샷(dict) 목록을 함께 반환."""
    holdings: list = []
    snapshots: list = []

    for q in QUARTERS:
        vals = _resolve_values(q)
        total = sum(v for v, _ in vals.values())
        if q.get("target_total") and total != q["target_total"]:
            raise AssertionError(
                f"{q['report_date']} 총액 불일치: {total:,} != {q['target_total']:,}")

        amend_syms = set(q.get("amend_symbols") or [])
        snap = {}
        for sym, (value, shares) in vals.items():
            cusip, issuer, cls = SECURITIES[sym]
            is_amend = sym in amend_syms
            h = Holding(
                report_date=q["report_date"],
                # 정정으로 병합된 행은 정정 파일링의 accession/filing_date 를 갖는다
                filing_date=q["amend_filing_date"] if is_amend else q["filing_date"],
                accession=q["amend_accession"] if is_amend else q["accession"],
                form_type="13F-HR/A" if is_amend else "13F-HR",
                amendment_type=q["amend_type"] if is_amend else None,
                cusip=cusip,
                issuer_name=issuer,
                title_of_class=cls,
                value_usd=int(value),
                shares=int(shares),
                share_type="SH",
                put_call=None,
                discretion="SOLE",
                weight_pct=round(value / total * 100.0, 4),
                ticker=sym,
                figi=f"BBG{cusip[:8]}",
                schema_version=SCHEMA_VERSION,
            )
            holdings.append(h)
            snap[(cusip, cls)] = dict(
                sym=sym, cusip=cusip, ticker=sym, issuer_name=issuer,
                title_of_class=cls, value_usd=int(value), shares=int(shares),
                weight_pct=h.weight_pct,
            )
        snapshots.append(dict(
            report_date=q["report_date"],
            # 분기 대표 filing_date = 그 분기 파일링 중 가장 늦은 것(정정 포함)
            filing_date=max([q["filing_date"]] + ([q["amend_filing_date"]] if amend_syms else [])),
            base_filing_date=q["filing_date"],
            accession=q["accession"],
            total=total,
            positions=snap,
        ))
    return holdings, snapshots


# ----------------------------------------------------------------- 이벤트/지표
def _conviction(event_type: str, weight_after: float, weight_delta_bp: float) -> str:
    if event_type == "EXIT":
        return "FULL_EXIT"
    if event_type == "NEW" and weight_after >= STRONG_NEW_WEIGHT_PCT:
        return "STRONG_NEW"
    if event_type == "ADD" and weight_delta_bp >= STRONG_DELTA_BP:
        return "STRONG_ADD"
    if event_type == "TRIM" and weight_delta_bp <= -STRONG_DELTA_BP:
        return "STRONG_TRIM"
    return "ROUTINE"


def build_events(snapshots: list) -> list:
    events: list = []
    for i in range(1, len(snapshots)):
        prev, curr = snapshots[i - 1], snapshots[i]
        keys = set(prev["positions"]) | set(curr["positions"])
        for key in sorted(keys):
            p = prev["positions"].get(key)
            c = curr["positions"].get(key)
            ref = c or p
            prev_shares = p["shares"] if p else 0
            curr_shares = c["shares"] if c else 0
            prev_value = p["value_usd"] if p else 0
            curr_value = c["value_usd"] if c else 0
            wb = p["weight_pct"] if p else 0.0
            wa = c["weight_pct"] if c else 0.0

            if p is None:
                etype, delta_pct = "NEW", None
            elif c is None:
                etype, delta_pct = "EXIT", -100.0
            else:
                delta_pct = round((curr_shares - prev_shares) / prev_shares * 100.0, 4)
                if delta_pct > MIN_DELTA_PCT:
                    etype = "ADD"
                elif delta_pct < -MIN_DELTA_PCT:
                    etype = "TRIM"
                else:
                    etype = "HOLD"

            wdbp = round((wa - wb) * 100.0, 2)
            events.append(Event(
                event_id=make_event_id(curr["report_date"], ref["cusip"], etype),
                report_date=curr["report_date"],
                filing_date=curr["filing_date"],
                event_type=etype,
                conviction=_conviction(etype, wa, wdbp),
                cusip=ref["cusip"],
                ticker=ref["ticker"],
                issuer_name=ref["issuer_name"],
                prev_shares=prev_shares,
                curr_shares=curr_shares,
                share_delta_pct=delta_pct,
                prev_value_usd=prev_value,
                curr_value_usd=curr_value,
                value_delta_usd=curr_value - prev_value,
                weight_before=wb,
                weight_after=wa,
                weight_delta_bp=wdbp,
                source="13F",
                provisional=False,
            ))

    # 13D 기반 잠정 이벤트 (13F 미확정 구간)
    last = snapshots[-1]
    for pv in PROVISIONAL:
        cusip, issuer, cls = SECURITIES[pv["sym"]]
        base = last["positions"].get((cusip, cls))
        if base is None:
            continue
        px = base["value_usd"] / base["shares"]
        curr_value = int(round(pv["curr_shares"] * px))
        delta_pct = round(
            (pv["curr_shares"] - pv["prev_shares"]) / pv["prev_shares"] * 100.0, 4)
        wb = base["weight_pct"]
        wa = round(curr_value / (last["total"] + (curr_value - base["value_usd"])) * 100.0, 4)
        wdbp = round((wa - wb) * 100.0, 2)
        etype = "ADD" if delta_pct > MIN_DELTA_PCT else "HOLD"
        events.append(Event(
            event_id=make_event_id(pv["report_date"], cusip, etype),
            report_date=pv["report_date"],
            filing_date=pv["filing_date"],
            event_type=etype,
            conviction=_conviction(etype, wa, wdbp),
            cusip=cusip,
            ticker=pv["sym"],
            issuer_name=issuer,
            prev_shares=pv["prev_shares"],
            curr_shares=pv["curr_shares"],
            share_delta_pct=delta_pct,
            prev_value_usd=base["value_usd"],
            curr_value_usd=curr_value,
            value_delta_usd=curr_value - base["value_usd"],
            weight_before=wb,
            weight_after=wa,
            weight_delta_bp=wdbp,
            source=pv["source"],
            provisional=True,
        ))
    return events


def build_metrics(snapshots: list, events: list) -> list:
    by_q: dict = {}
    for e in events:
        if e.provisional:
            continue
        by_q.setdefault(e.report_date, []).append(e)

    metrics: list = []
    for i, s in enumerate(snapshots):
        weights = sorted((p["weight_pct"] for p in s["positions"].values()), reverse=True)
        hhi = round(sum((w / 100.0) ** 2 for w in weights), 6)
        evs = by_q.get(s["report_date"], [])

        turnover = None
        if i > 0:
            prev = snapshots[i - 1]
            keys = set(prev["positions"]) | set(s["positions"])
            churn = sum(
                abs(s["positions"].get(k, {}).get("value_usd", 0)
                    - prev["positions"].get(k, {}).get("value_usd", 0))
                for k in keys)
            avg = (prev["total"] + s["total"]) / 2.0
            turnover = round(churn / avg * 100.0, 3)

        lag = (date.fromisoformat(s["filing_date"]) - date.fromisoformat(s["report_date"])).days
        metrics.append(QuarterMetrics(
            report_date=s["report_date"],
            filing_date=s["filing_date"],
            lag_days=lag,
            total_value_usd=s["total"],
            position_count=len(s["positions"]),
            hhi=hhi,
            top1_pct=round(sum(weights[:1]), 4),
            top3_pct=round(sum(weights[:3]), 4),
            top5_pct=round(sum(weights[:5]), 4),
            turnover_pct=turnover,
            new_count=sum(1 for e in evs if e.event_type == "NEW"),
            exit_count=sum(1 for e in evs if e.event_type == "EXIT"),
            add_count=sum(1 for e in evs if e.event_type == "ADD"),
            trim_count=sum(1 for e in evs if e.event_type == "TRIM"),
        ))
    return metrics


def build_filings(snapshots: list) -> list:
    rows: list = []
    for q in QUARTERS:
        vals = _resolve_values(q)
        amend_syms = set(q.get("amend_symbols") or [])
        base = {s: v for s, v in vals.items() if s not in amend_syms}
        acc_nodash = q["accession"].replace("-", "")
        rows.append(Filing(
            accession=q["accession"],
            form_type="13F-HR",
            filing_date=q["filing_date"],
            report_date=q["report_date"],
            primary_document="primary_doc.xml",
            url=f"https://www.sec.gov/Archives/edgar/data/1336528/{acc_nodash}/",
            amendment_type=None,
            schema_version=SCHEMA_VERSION,
            entry_total=len(base),
            value_total_raw=sum(v for v, _ in base.values()),
            items=None,
        ))
        if amend_syms:
            am = {s: v for s, v in vals.items() if s in amend_syms}
            a_nodash = q["amend_accession"].replace("-", "")
            rows.append(Filing(
                accession=q["amend_accession"],
                form_type="13F-HR/A",
                filing_date=q["amend_filing_date"],
                report_date=q["report_date"],
                primary_document="primary_doc.xml",
                url=f"https://www.sec.gov/Archives/edgar/data/1336528/{a_nodash}/",
                amendment_type=q["amend_type"],
                schema_version=SCHEMA_VERSION,
                entry_total=len(am),
                value_total_raw=sum(v for v, _ in am.values()),
                items=None,
            ))

    seen = {r.accession for r in rows}
    for acc, form, fdate, subject, note in OTHER_FILINGS:
        if acc in seen:
            continue
        nodash = acc.replace("-", "")
        f = Filing(
            accession=acc,
            form_type=form,
            filing_date=fdate,
            report_date=None,
            primary_document="primary_doc.xml",
            url=f"https://www.sec.gov/Archives/edgar/data/1336528/{nodash}/",
            amendment_type=None,
            schema_version=None,
            entry_total=None,
            value_total_raw=None,
            items=note,
        )
        row = f.__dict__.copy()
        # 선택 확장 필드 — build.py 는 있으면 쓰고 없으면 우아하게 무시한다
        row["subject_cusip"] = subject
        rows.append(row)
    return rows


# ----------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Ackman Tracker 대시보드용 합성 데이터 생성 (계약 준수)")
    ap.add_argument("--out", default=os.path.join(_HERE, "sample"),
                    help="출력 디렉터리 (기본: dashboard/sample)")
    ap.add_argument("--force", action="store_true",
                    help="data/normalized 등 샘플 디렉터리 밖으로도 강제 출력 허용")
    args = ap.parse_args(argv)

    out = os.path.abspath(args.out)
    normalized = os.path.abspath(os.path.join(_ROOT, "data", "normalized"))
    if out == normalized and not args.force:
        print("[ERROR] data/normalized 는 Collector/Analytics 담당 디렉터리입니다.", file=sys.stderr)
        print("        합성 데이터로 덮어쓰면 실데이터가 파괴됩니다. 정말 원하면 --force.", file=sys.stderr)
        return 1

    holdings, snapshots = build_holdings()
    events = build_events(snapshots)
    metrics = build_metrics(snapshots, events)
    filings = build_filings(snapshots)

    n_h = write_jsonl(os.path.join(out, "holdings.jsonl"), holdings,
                      sort_key=lambda r: (r["report_date"], -r["value_usd"]))
    n_f = write_jsonl(os.path.join(out, "filings.jsonl"), filings,
                      sort_key=lambda r: (r["filing_date"], r["accession"]))
    n_e = write_jsonl(os.path.join(out, "events.jsonl"), events,
                      sort_key=lambda r: (r["report_date"], r["cusip"]))
    n_m = write_jsonl(os.path.join(out, "metrics.jsonl"), metrics,
                      sort_key=lambda r: r["report_date"])

    latest = snapshots[-1]
    print(f"[sample_data] out={out}")
    print(f"  holdings.jsonl : {n_h} rows / {len(snapshots)} quarters")
    print(f"  filings.jsonl  : {n_f} rows")
    print(f"  events.jsonl   : {n_e} rows "
          f"({sum(1 for e in events if e.provisional)} provisional)")
    print(f"  metrics.jsonl  : {n_m} rows")
    print(f"  latest quarter : {latest['report_date']} "
          f"{len(latest['positions'])} positions ${latest['total']:,}")

    # 기준선 자체 검증
    assert latest["total"] == Q1_2026_TOTAL, "2026Q1 총액 기준선 불일치"
    assert len(latest["positions"]) == 11, "2026Q1 포지션 수 기준선 불일치"
    q4 = next(s for s in snapshots if s["report_date"] == "2024-12-31")
    assert q4["total"] == Q4_2024_TOTAL, "2024Q4 총액 기준선 불일치"
    assert len(q4["positions"]) == 11, "2024Q4 포지션 수 기준선 불일치"
    for s in snapshots:
        wsum = sum(p["weight_pct"] for p in s["positions"].values())
        assert abs(wsum - 100.0) < 0.5, f"{s['report_date']} weight 합계 {wsum}"
    print("  baseline checks: OK (2026Q1 / 2024Q4 / weight sums)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
