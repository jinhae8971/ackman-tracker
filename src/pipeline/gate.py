"""데이터 무결성 게이트 (docs/architecture.md §6.3).

이 시스템의 최악의 실패 모드는 "조용히 잘못된 데이터가 커밋되는 것"이다.
`integrity_gate()` 는 위반 사유 문자열 리스트를 반환하며, 리스트가 비어 있지 않으면
호출자(`run.py`)는 **커밋하지 않고** 워크플로우를 실패시킨다.

위반 사유 문자열은 `[CODE] 설명` 형식이다. CODE 는 아래 5종이며, 테스트와
워크플로우 로그 grep 에서 안정적으로 쓸 수 있도록 고정한다.

    EMPTY_QUARTER  최신 분기 포지션 수 0
    TOTAL_SWING    총액이 전분기 대비 허용치 초과 변동 (엔티티별로 조정 가능 —
                   신생 펀드는 정상적으로 배 이상 커진다. entities.Entity 참고)
    WEIGHT_SUM     weight_pct 합계가 기대치 ± 0.5 범위 밖
    DUP_POSITION   동일 report_date 에 (cusip, title_of_class, put_call) 중복
    DUP_EVENT_ID   event_id 중복

절삭 엔티티(Citadel)의 WEIGHT_SUM
---------------------------------
Citadel 은 보통주 상위 200종목만 보존하므로 weight_pct 합계가 100 이 아니라
12% 안팎이다. 결함이 아니라 의도된 표기다 — 비중은 '절삭 전 전체 포트폴리오'
기준으로 계산해야 하며, 남은 200종목 기준으로 재계산하면 합이 100% 가 되어
6,000종목을 든 마켓메이커가 집중 포트폴리오처럼 보인다.

그래서 절삭 엔티티는 기대치를 100 이 아니라 `coverage.jsonl` 의 `coverage_pct`
로 둔다. 느슨해지는 것이 아니라 오히려 더 강한 검사다 — 누군가 절삭 후 비중을
재계산하면 합계가 100 으로 튀어 그 자리에서 걸린다.

자체 단위 테스트:  python -m src.pipeline.gate
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from typing import Optional

try:  # 패키지로 import 될 때
    from src.common.schema import (ENTITY_MAX_TOTAL_SWING_PCT as MAX_TOTAL_SWING_PCT,
                                   Paths, read_jsonl)
except ImportError:  # python src/pipeline/gate.py 처럼 직접 실행될 때
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    from src.common.schema import (ENTITY_MAX_TOTAL_SWING_PCT as MAX_TOTAL_SWING_PCT,
                                   Paths, read_jsonl)

# weight_pct 합계 허용 오차 (퍼센트 포인트)
WEIGHT_SUM_TOLERANCE_PCT = 0.5

VIOLATION_CODES = (
    "EMPTY_QUARTER",
    "TOTAL_SWING",
    "WEIGHT_SUM",
    "DUP_POSITION",
    "DUP_EVENT_ID",
)


# ---------------------------------------------------------------- 내부 헬퍼

def _num(value, default=0.0) -> float:
    """None / 빈 문자열 / 문자열 숫자를 관대하게 float 로."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quarter_rollup(holdings: list[dict]):
    """report_date 별 (총액, 포지션 수, weight 합계)."""
    totals: dict[str, float] = defaultdict(float)
    counts: Counter = Counter()
    weights: dict[str, float] = defaultdict(float)
    for h in holdings:
        rd = h.get("report_date") or "(missing report_date)"
        totals[rd] += _num(h.get("value_usd"))
        counts[rd] += 1
        weights[rd] += _num(h.get("weight_pct"))
    return totals, counts, weights


def _metrics_by_date(metrics) -> dict[str, dict]:
    if not metrics:
        return {}
    if isinstance(metrics, dict):
        metrics = [metrics]
    out = {}
    for m in metrics:
        if isinstance(m, dict) and m.get("report_date"):
            out[m["report_date"]] = m
    return out


# ---------------------------------------------------------------- 게이트

def integrity_gate(
    holdings: list[dict],
    metrics: list[dict],
    events: Optional[list[dict]] = None,
    coverage: Optional[list[dict]] = None,
) -> list[str]:
    """무결성 위반 사유 리스트를 반환한다. 빈 리스트면 통과.

    계약서 시그니처는 `(holdings, metrics)` 두 개다. `events` 는 event_id 중복
    검사를 위해 추가한 **선택 인자**이며, 생략하면 `data/normalized/events.jsonl`
    을 직접 읽는다. 위치 인자 두 개만 넘기는 기존 호출은 그대로 동작한다.

    `coverage` 는 절삭 엔티티의 분기별 `coverage_pct` 를 담은 레코드다.
    비어 있으면 모든 분기의 weight_pct 합계 기대치를 100 으로 본다.
    """
    violations: list[str] = []

    if events is None:
        events = read_jsonl(Paths.EVENTS)
    expected_weight = {
        c.get("report_date"): _num(c.get("coverage_pct"), 100.0)
        for c in (coverage or [])
        if c.get("report_date") and c.get("truncated")
    }

    holdings = holdings or []
    mbd = _metrics_by_date(metrics)

    # ---------- 1) 최신 분기 포지션 수 0
    if not holdings:
        violations.append(
            "[EMPTY_QUARTER] holdings 가 비어 있음 — 최신 분기 포지션 수 0"
        )
        # 이후 검사는 의미가 없다. 단, event_id 중복은 독립이므로 계속 진행한다.
        latest = None
        totals, counts, weights = {}, Counter(), {}
    else:
        totals, counts, weights = _quarter_rollup(holdings)
        dates = sorted(totals)
        latest = dates[-1]

        if counts.get(latest, 0) == 0:
            violations.append(
                f"[EMPTY_QUARTER] 최신 분기 {latest} 의 포지션 수가 0"
            )
        m_latest = mbd.get(latest)
        if m_latest is not None and int(_num(m_latest.get("position_count"))) == 0:
            violations.append(
                f"[EMPTY_QUARTER] metrics 의 {latest} position_count 가 0 "
                f"(holdings 기준 {counts.get(latest, 0)}종목)"
            )

        # ---------- 2) 총액 전분기 대비 급변
        if len(dates) >= 2:
            prev_d, curr_d = dates[-2], dates[-1]
            prev_v, curr_v = totals[prev_d], totals[curr_d]
            if prev_v <= 0:
                violations.append(
                    f"[TOTAL_SWING] 전분기 {prev_d} 총액이 {prev_v:,.0f} — "
                    "변동률을 계산할 수 없음"
                )
            else:
                swing = (curr_v - prev_v) / prev_v * 100.0
                if abs(swing) > MAX_TOTAL_SWING_PCT:
                    violations.append(
                        f"[TOTAL_SWING] 총액 변동 {swing:+.1f}% 가 허용치 "
                        f"±{MAX_TOTAL_SWING_PCT:.0f}% 초과 "
                        f"({prev_d} ${prev_v:,.0f} -> {curr_d} ${curr_v:,.0f})"
                    )

        # ---------- 3) weight_pct 합계
        for rd in dates:
            s = weights[rd]
            want = expected_weight.get(rd, 100.0)
            if abs(s - want) > WEIGHT_SUM_TOLERANCE_PCT:
                note = "" if want == 100.0 else " (절삭 커버리지 기준)"
                violations.append(
                    f"[WEIGHT_SUM] {rd} weight_pct 합계 {s:.4f} 가 "
                    f"{want:.4f} ± {WEIGHT_SUM_TOLERANCE_PCT} 범위 밖{note}"
                )

        # ---------- 4) 동일 report_date 내 (cusip, title_of_class) 중복
        # 키에 put_call 을 포함한다(schema.Holding.key 와 동일). 빼면 같은
        # 발행사의 보통주와 PUT/CALL 행이 '중복'으로 오인돼, 정상 데이터가
        # 게이트에서 튕긴다.
        keys: Counter = Counter()
        for h in holdings:
            keys[
                (
                    h.get("report_date"),
                    h.get("cusip"),
                    h.get("title_of_class"),
                    h.get("put_call") or "",
                )
            ] += 1
        for (rd, cusip, cls, pc), n in sorted(keys.items(), key=lambda kv: str(kv[0])):
            if n > 1:
                violations.append(
                    f"[DUP_POSITION] {rd} 에 (cusip={cusip}, "
                    f"title_of_class={cls}, put_call={pc or '-'}) 가 {n}회 중복"
                )

    # ---------- 5) event_id 중복
    ev_ids: Counter = Counter(
        e.get("event_id") for e in (events or []) if isinstance(e, dict)
    )
    for eid, n in sorted(ev_ids.items(), key=lambda kv: str(kv[0])):
        if eid is None:
            violations.append(f"[DUP_EVENT_ID] event_id 가 없는 이벤트 {n}건")
        elif n > 1:
            violations.append(f"[DUP_EVENT_ID] event_id '{eid}' 가 {n}회 중복")

    return violations


def gate_from_disk() -> list[str]:
    """디스크의 normalized 산출물로 게이트를 돌린다.

    coverage.jsonl 은 절삭 엔티티에만 존재한다. 없으면 빈 리스트가 되어
    기대치가 100 으로 유지되므로 기존 엔티티의 동작은 그대로다.
    """
    return integrity_gate(
        read_jsonl(Paths.HOLDINGS),
        read_jsonl(Paths.METRICS),
        read_jsonl(Paths.EVENTS),
        read_jsonl(Paths.COVERAGE),
    )


def format_violations(violations: list[str], annotate: bool = True) -> str:
    """GitHub Actions 로그용 문자열. annotate 면 ::error:: 주석을 붙인다."""
    if not violations:
        return "무결성 게이트 통과 (위반 0건)"
    lines = [f"무결성 게이트 실패 — 위반 {len(violations)}건"]
    for v in violations:
        lines.append(f"::error::{v}" if annotate else f"  - {v}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 자체 테스트

def _good_dataset():
    """게이트를 통과해야 하는 합성 데이터."""
    holdings = [
        # 2025-12-31 — 총액 1,000,000,000
        dict(report_date="2025-12-31", filing_date="2026-02-14",
             accession="0001172661-26-000001", form_type="13F-HR",
             cusip="11271J107", title_of_class="COM", issuer_name="BROOKFIELD CORP",
             value_usd=600_000_000, shares=100, weight_pct=60.0),
        dict(report_date="2025-12-31", filing_date="2026-02-14",
             accession="0001172661-26-000001", form_type="13F-HR",
             cusip="023135106", title_of_class="COM", issuer_name="AMAZON COM INC",
             value_usd=400_000_000, shares=200, weight_pct=40.0),
        # 2026-03-31 — 총액 1,200,000,000 (+20%)
        dict(report_date="2026-03-31", filing_date="2026-05-15",
             accession="0001172661-26-002336", form_type="13F-HR",
             cusip="11271J107", title_of_class="COM", issuer_name="BROOKFIELD CORP",
             value_usd=700_000_000, shares=110, weight_pct=58.3333),
        dict(report_date="2026-03-31", filing_date="2026-05-15",
             accession="0001172661-26-002336", form_type="13F-HR",
             cusip="023135106", title_of_class="COM", issuer_name="AMAZON COM INC",
             value_usd=500_000_000, shares=220, weight_pct=41.6667),
    ]
    metrics = [
        dict(report_date="2025-12-31", filing_date="2026-02-14", lag_days=45,
             total_value_usd=1_000_000_000, position_count=2, hhi=0.52,
             top1_pct=60.0, top3_pct=100.0, top5_pct=100.0),
        dict(report_date="2026-03-31", filing_date="2026-05-15", lag_days=45,
             total_value_usd=1_200_000_000, position_count=2, hhi=0.5138,
             top1_pct=58.33, top3_pct=100.0, top5_pct=100.0),
    ]
    events = [
        dict(event_id="2026-03-31:11271J107:ADD", report_date="2026-03-31",
             event_type="ADD", conviction="ROUTINE", cusip="11271J107"),
        dict(event_id="2026-03-31:023135106:ADD", report_date="2026-03-31",
             event_type="ADD", conviction="ROUTINE", cusip="023135106"),
    ]
    return holdings, metrics, events


def _codes(violations: list[str]) -> set:
    out = set()
    for v in violations:
        if v.startswith("[") and "]" in v:
            out.add(v[1:v.index("]")])
    return out


def _selftest() -> int:
    import copy

    failures = 0
    results = []

    def check(name: str, violations: list[str], expected: set):
        nonlocal failures
        got = _codes(violations)
        ok = got == expected
        if not ok:
            failures += 1
        results.append((ok, name, expected, got, violations))

    # --- 0) 정상 데이터는 통과해야 한다
    h, m, e = _good_dataset()
    check("정상 데이터 (위반 없음)", integrity_gate(h, m, e), set())

    # --- 1) EMPTY_QUARTER: 최신 분기 포지션 0
    check("1) EMPTY_QUARTER — holdings 비어 있음",
          integrity_gate([], [], []), {"EMPTY_QUARTER"})

    h, m, e = _good_dataset()
    m[-1]["position_count"] = 0
    check("1b) EMPTY_QUARTER — metrics position_count 0",
          integrity_gate(h, m, e), {"EMPTY_QUARTER"})

    # --- 2) TOTAL_SWING: 전분기 대비 -95%
    h, m, e = _good_dataset()
    for row in h:
        if row["report_date"] == "2026-03-31":
            row["value_usd"] = int(row["value_usd"] * 0.05)
    check("2) TOTAL_SWING — 총액 -95%",
          integrity_gate(h, m, e), {"TOTAL_SWING"})

    # --- 3) WEIGHT_SUM: 합계 100 이탈
    h, m, e = _good_dataset()
    h[-1]["weight_pct"] = 30.0          # 58.3333 + 30 = 88.33
    check("3) WEIGHT_SUM — 합계 88.33",
          integrity_gate(h, m, e), {"WEIGHT_SUM"})

    # --- 4) DUP_POSITION: 동일 report_date 에 같은 (cusip, class)
    h, m, e = _good_dataset()
    dup = copy.deepcopy(h[-1])
    dup["cusip"] = "11271J107"          # 같은 분기의 Brookfield 와 중복
    dup["weight_pct"] = 0.0
    dup["value_usd"] = 0
    h.append(dup)
    check("4) DUP_POSITION — (cusip, title_of_class) 중복",
          integrity_gate(h, m, e), {"DUP_POSITION"})

    # --- 5) DUP_EVENT_ID
    h, m, e = _good_dataset()
    e.append(copy.deepcopy(e[0]))
    check("5) DUP_EVENT_ID — event_id 중복",
          integrity_gate(h, m, e), {"DUP_EVENT_ID"})

    # --- 6) 경계값: weight 합계 100.5 는 통과, 100.51 은 실패
    h, m, e = _good_dataset()
    h[-1]["weight_pct"] = 42.1667       # 58.3333 + 42.1667 = 100.5
    check("6a) WEIGHT_SUM 경계 100.5 (통과)", integrity_gate(h, m, e), set())
    h[-1]["weight_pct"] = 42.30         # 100.6333
    check("6b) WEIGHT_SUM 경계 100.63 (실패)",
          integrity_gate(h, m, e), {"WEIGHT_SUM"})

    # --- 7) 복합 위반: 여러 개가 동시에 잡히는지
    h, m, e = _good_dataset()
    h[-1]["weight_pct"] = 10.0
    e.append(copy.deepcopy(e[0]))
    check("7) 복합 — WEIGHT_SUM + DUP_EVENT_ID",
          integrity_gate(h, m, e), {"WEIGHT_SUM", "DUP_EVENT_ID"})

    # --- 8) 절삭 엔티티: 합계가 coverage_pct 와 맞으면 통과
    h, m, e = _good_dataset()
    dates = sorted({r["report_date"] for r in h})
    for row in h:                       # 전체를 1/10 로 눌러 합계 ~10% 로 만든다
        row["weight_pct"] = round(row["weight_pct"] / 10.0, 4)
    cov = []
    for rd in dates:
        s_rd = sum(r["weight_pct"] for r in h if r["report_date"] == rd)
        cov.append({"report_date": rd, "truncated": True,
                    "coverage_pct": round(s_rd, 4)})
    check("8a) 절삭 — 합계가 coverage_pct 와 일치 (통과)",
          integrity_gate(h, m, e, cov), set())

    # 절삭 후 비중을 재계산해 합이 100 이 되면 반드시 걸려야 한다.
    # 이것이 이 게이트가 막으려는 진짜 사고다.
    h2 = copy.deepcopy(h)
    for rd in dates:
        rows = [r for r in h2 if r["report_date"] == rd]
        tot = sum(r["weight_pct"] for r in rows)
        for r in rows:
            r["weight_pct"] = round(r["weight_pct"] / tot * 100.0, 4)
    check("8b) 절삭 — 비중 재계산으로 합계 100 (실패해야 정상)",
          integrity_gate(h2, m, e, cov), {"WEIGHT_SUM"})

    # coverage 를 안 넘기면 기대치가 100 이라 절삭 데이터는 위반으로 잡힌다.
    check("8c) 절삭 — coverage 미제공 시 기대치 100",
          integrity_gate(h, m, e), {"WEIGHT_SUM"})

    # --- 9) DUP_POSITION 은 put_call 을 구분한다
    h, m, e = _good_dataset()
    opt = copy.deepcopy(h[-1])
    opt["put_call"] = "Put"             # 같은 발행사의 PUT 은 중복이 아니다
    opt["weight_pct"] = 0.0
    opt["value_usd"] = 0
    h.append(opt)
    check("9a) DUP_POSITION — 보통주 + PUT 은 중복 아님", integrity_gate(h, m, e), set())
    h.append(copy.deepcopy(opt))        # 같은 PUT 이 두 번이면 중복이다
    check("9b) DUP_POSITION — 같은 PUT 2회는 중복",
          integrity_gate(h, m, e), {"DUP_POSITION"})

    print("=" * 74)
    print("gate.integrity_gate 자체 테스트")
    print("=" * 74)
    for ok, name, expected, got, violations in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"       기대 코드: {sorted(expected) or '없음'} / 실제: {sorted(got) or '없음'}")
        for v in violations:
            print(f"       -> {v}")
    print("-" * 74)
    print(f"총 {len(results)}건 중 실패 {failures}건")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
