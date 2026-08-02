"""CLI — holdings.jsonl 전체를 분기 순회해 events.jsonl / metrics.jsonl 생성.

    python -m src.analytics.build_events [--help]

멱등: event_id 가 {report_date}:{cusip}:{event_type}[:PUT|:CALL] 로 결정적이고 출력 정렬도
고정이므로, 같은 입력에 대해 몇 번을 재실행해도 바이트 단위로 동일한 파일이 나온다.
네트워크 호출은 하지 않는다.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import OrderedDict
from typing import Optional

from ..common import schema
from ..common.schema import Paths, read_jsonl, write_jsonl
from .diff import quarter_diff
from .metrics import holding_periods, quarter_metrics

log = logging.getLogger("analytics.build_events")

EVENT_SORT_KEY = lambda r: (r["report_date"], r["cusip"], r["event_type"],  # noqa: E731
                            r.get("put_call") or "")
METRIC_SORT_KEY = lambda r: (r["report_date"],)                              # noqa: E731


def group_by_quarter(holdings: list[dict]) -> "OrderedDict[str, list[dict]]":
    """report_date 오름차순으로 묶는다. 분기 내부는 (평가액 내림차순, cusip)."""
    buckets: dict[str, list[dict]] = {}
    for h in holdings:
        rd = str(h.get("report_date") or "")
        if not rd:
            log.warning("report_date 없는 행 무시: cusip=%s", h.get("cusip"))
            continue
        buckets.setdefault(rd, []).append(h)
    out: "OrderedDict[str, list[dict]]" = OrderedDict()
    for rd in sorted(buckets):
        rows = buckets[rd]
        rows.sort(key=lambda h: (-int(h.get("value_usd") or 0),
                                 str(h.get("cusip") or ""),
                                 str(h.get("title_of_class") or "")))
        out[rd] = rows
    return out


def build(holdings: list[dict], include_hold: bool = True
          ) -> tuple[list[dict], list[dict]]:
    """holdings -> (events, metrics) 딕셔너리 목록. 순수 함수 (I/O 없음)."""
    quarters = group_by_quarter(holdings)
    events: list[dict] = []
    metrics: list[dict] = []
    prev: Optional[list[dict]] = None

    for rd, rows in quarters.items():
        qevents = quarter_diff(prev or [], rows)
        # 지표는 HOLD 포함 여부와 무관하게 전체 이벤트로 집계한다.
        qm = quarter_metrics(rows, [e for e in qevents], prev)
        metrics.append(_to_dict(qm))
        for e in qevents:
            if not include_hold and e.event_type == "HOLD":
                continue
            events.append(_to_dict(e))
        prev = rows

    _assert_unique_event_ids(events)
    return events, metrics


def _to_dict(rec) -> dict:
    return {f: getattr(rec, f) for f in rec.__dataclass_fields__}


def _assert_unique_event_ids(events: list[dict]) -> None:
    """event_id 중복은 무결성 위반이다 (pipeline gate 도 동일 조건을 본다).

    ID 키는 (report_date, cusip, event_type, put_call) 이다. 보유 4사 전체
    이력에서 이 조합의 충돌은 0건이지만, 같은 CUSIP·같은 put_call 이 서로 다른
    title_of_class 로 등장하는 공시가 나타나면 다시 충돌할 수 있다. 그때는
    조용히 덮어쓰지 말고 여기서 멈춰야 한다 — 이벤트 한 건이 소리 없이
    사라지는 것이 최악이다.
    """
    seen: dict[str, int] = {}
    for e in events:
        seen[e["event_id"]] = seen.get(e["event_id"], 0) + 1
    dupes = sorted(k for k, v in seen.items() if v > 1)
    if dupes:
        raise ValueError(
            "duplicate event_id detected (%d): %s — 동일 (cusip, put_call) 이 "
            "한 분기에 복수 title_of_class 로 존재한다. "
            "schema.make_event_id 에 title_of_class 를 추가해야 한다."
            % (len(dupes), ", ".join(dupes[:5]))
        )


# ---------------------------------------------------------------- 요약 출력

def summarize(events: list[dict], metrics: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"분기 {len(metrics)}개 / 이벤트 {len(events)}건")
    if not metrics:
        return "\n".join(lines)

    by_type: dict[str, int] = {}
    for e in events:
        by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1
    lines.append("  타입별: " + ", ".join(
        f"{k}={by_type[k]}" for k in ("NEW", "ADD", "TRIM", "EXIT", "HOLD") if k in by_type))

    strong = [e for e in events if e["conviction"] != "ROUTINE"]
    lines.append(f"  STRONG_*/FULL_EXIT: {len(strong)}건")

    m = metrics[-1]
    lines.append(
        f"  최신 분기 {m['report_date']} (제출 {m['filing_date']}, lag {m['lag_days']}일): "
        f"{m['position_count']}종목 ${m['total_value_usd']:,} "
        f"HHI={m['hhi']} top1={m['top1_pct']}% top3={m['top3_pct']}% top5={m['top5_pct']}% "
        f"turnover={m['turnover_pct']}"
    )
    last_rd = m["report_date"]
    notable = [e for e in events
               if e["report_date"] == last_rd and e["event_type"] != "HOLD"]
    for e in notable[:10]:
        lines.append(
            f"    {e['event_type']:<4} {e['conviction']:<11} "
            f"{(e['ticker'] or e['cusip']):<8} {e['issuer_name'][:28]:<28} "
            f"{e['prev_shares']:>12,} -> {e['curr_shares']:>12,} "
            f"({e['share_delta_pct']}%) {e['weight_delta_bp']:+.0f}bp"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.analytics.build_events",
        description="holdings.jsonl -> events.jsonl / metrics.jsonl (멱등, 네트워크 없음)",
    )
    p.add_argument("--holdings", default=Paths.HOLDINGS, help="입력 holdings.jsonl 경로")
    p.add_argument("--events", default=Paths.EVENTS, help="출력 events.jsonl 경로")
    p.add_argument("--metrics", default=Paths.METRICS, help="출력 metrics.jsonl 경로")
    p.add_argument("--exclude-hold", action="store_true",
                   help="HOLD 이벤트를 출력에서 제외 (지표 집계에는 영향 없음)")
    p.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 요약만 출력")
    p.add_argument("-v", "--verbose", action="store_true", help="분할 보정 등 상세 로그")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not os.path.exists(args.holdings):
        print(f"::notice::holdings 파일 없음: {args.holdings} — 생성할 이벤트 없음")
        return 0

    holdings = read_jsonl(args.holdings)
    if not holdings:
        print(f"::notice::holdings 비어 있음: {args.holdings} — 생성할 이벤트 없음")
        return 0

    events, metrics = build(holdings, include_hold=not args.exclude_hold)

    if not args.dry_run:
        n_e = write_jsonl(args.events, events, sort_key=EVENT_SORT_KEY)
        n_m = write_jsonl(args.metrics, metrics, sort_key=METRIC_SORT_KEY)
        print(f"wrote {n_e} events -> {args.events}")
        print(f"wrote {n_m} metrics -> {args.metrics}")

    print(summarize(events, metrics))

    hp = holding_periods(holdings)
    current = [v for v in hp.values() if v["is_current"]]
    if current:
        longest = max(current, key=lambda v: v["quarters_held"])
        print(f"  현재 보유 {len(current)}종목 / 추적된 CUSIP {len(hp)}개, "
              f"최장 보유: {longest['ticker'] or longest['cusip']} "
              f"{longest['quarters_held']}분기 (최초 {longest['first_seen']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
