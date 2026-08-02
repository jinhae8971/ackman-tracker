"""백필 CLI — 13F 전량(또는 최근 N건) 수집 -> 정정 해석 -> CUSIP 매핑 -> JSONL.

    python -m src.collector.backfill [--limit N] [--force]

출력 계약:
  data/normalized/holdings.jsonl  — 분기별 '유효 포지션'(정정 해석 완료) 1행 = 1종목,
                                    (report_date, -value_usd) 정렬
  data/normalized/filings.jsonl   — 파일링 메타 (13F 는 정정타입/스키마/체크섬 포함)

멱등성: 원문은 data/raw/ 에 보존되어 재실행 시 네트워크를 타지 않고,
        같은 인자로 다시 돌리면 완전히 동일한 JSONL 이 나온다.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import asdict
from typing import Optional

from src.common import schema
from src.common.schema import Filing, Holding, Paths

from . import cusip_map, edgar
from .parse_13f import parse_13f, resolve_quarter

log = logging.getLogger("collector.backfill")

FORMS_13F = ["13F-HR", "13F-HR/A"]

ENTITY = schema.ENTITY

# 검증 기준선 (Golden Data) — 엔티티가 들고 있고, 해당 분기가 결과에 있으면 자동 대조.
GOLDEN = ENTITY.golden


# ---------------------------------------------------------------- 수집 정책

def apply_entity_policy(rows: list[Holding], report_date: str
                        ) -> tuple[list[Holding], dict]:
    """엔티티 정책(옵션 제외 / 상위 N)을 적용하고 커버리지를 계산한다.

    Citadel 같은 마켓메이커는 분기당 6,700종목 · 7.8MB 를 보고한다. 전량을
    Git 에 적재하면 리포지토리가 감당하지 못하고, OpenFIGI 매핑 비용도
    폭증한다. 그래서 보통주 상위 N 만 보존하되 — 잘라냈다는 사실 자체를
    coverage.jsonl 로 남겨 대시보드가 '전체의 몇 %를 보고 있는지' 명시하게 한다.

    중요: weight_pct 는 **자르기 전 전체 포트폴리오** 기준으로 이미 계산되어
    있으며 여기서 재계산하지 않는다. 상위 200종목만으로 비중을 다시 매기면
    합이 100% 가 되어 마치 집중 포트폴리오인 것처럼 보이는 왜곡이 생긴다.
    """
    full_n = len(rows)
    full_value = sum(h.value_usd for h in rows)
    opt_rows = [h for h in rows if h.put_call]

    kept = rows
    if ENTITY.exclude_options:
        kept = [h for h in kept if not h.put_call]
    if ENTITY.max_positions and len(kept) > ENTITY.max_positions:
        kept = sorted(kept, key=lambda h: -h.value_usd)[:ENTITY.max_positions]

    kept_value = sum(h.value_usd for h in kept)
    coverage = {
        "report_date": report_date,
        "entity": ENTITY.key,
        "truncated": ENTITY.truncated and len(kept) != full_n,
        "full_positions": full_n,
        "full_value_usd": full_value,
        "kept_positions": len(kept),
        "kept_value_usd": kept_value,
        "coverage_pct": round(kept_value / full_value * 100, 4) if full_value else 0.0,
        "option_positions": len(opt_rows),
        "option_value_usd": sum(h.value_usd for h in opt_rows),
        "excluded_options": bool(ENTITY.exclude_options),
        "max_positions": ENTITY.max_positions,
    }
    if coverage["truncated"]:
        log.info("  %s 정책 적용: %d종목 -> %d종목 (가치 커버리지 %.1f%%)",
                 report_date, full_n, len(kept), coverage["coverage_pct"])
    kept.sort(key=lambda h: (-h.value_usd, h.cusip, h.title_of_class))
    return kept, coverage


# ---------------------------------------------------------------- 선택 로직

def select_filings(filings: list[Filing], limit: Optional[int]) -> list[Filing]:
    """최근 `limit` 건을 고르되, 같은 report_date 의 형제 파일링을 모두 끌어온다.

    원본 없이 정정만 창(window)에 걸리면 그 분기가 붕괴하므로 반드시 필요하다.
    (예: 2024Q4 정정 0001172661-25-001497 은 Hertz 1종목뿐이다.)
    """
    ordered = sorted(filings, key=lambda f: (f.filing_date, f.accession))
    if not limit or limit >= len(ordered):
        return ordered
    window = ordered[-limit:]
    dates = {f.report_date for f in window if f.report_date}
    picked = {f.accession for f in window}
    expanded = [f for f in ordered
                if f.accession in picked or (f.report_date in dates)]
    if len(expanded) > len(window):
        log.info("정정/원본 짝을 맞추기 위해 %d건 -> %d건으로 확장",
                 len(window), len(expanded))
    return expanded


# ---------------------------------------------------------------- 본체

def run(limit: Optional[int] = None, force: bool = False,
        forms: Optional[list[str]] = None, map_tickers: bool = True) -> dict:
    forms = forms or FORMS_13F

    log.info("submissions 조회: %s", schema.SUBMISSIONS_URL)
    submissions = edgar.fetch_submissions()
    all_filings = edgar.list_filings(submissions=submissions)
    log.info("전체 파일링 %d건 (엔터티: %s)",
             len(all_filings), submissions.get("name"))

    filings_13f = [f for f in all_filings if f.form_type in forms]
    targets = select_filings(filings_13f, limit)
    log.info("13F 대상 %d건 / 전체 13F %d건", len(targets), len(filings_13f))

    parsed: list[tuple[Filing, list[Holding]]] = []
    failures: dict[str, str] = {}
    for i, f in enumerate(targets, 1):
        try:
            filing, holdings = parse_13f(f.accession, f.filing_date, f.form_type,
                                         report_date=f.report_date, force=force)
        except ValueError:
            # 체크섬/무결성 위반은 조용히 넘기지 않는다 (architecture §6.3 하드 스톱)
            raise
        except Exception as exc:                       # 부분 실패 격리
            failures[f.accession] = f"{type(exc).__name__}: {exc}"
            log.error("[%d/%d] %s %s 파싱 실패 — %s",
                      i, len(targets), f.form_type, f.accession, exc)
            continue
        filing.items = f.items
        filing.primary_document = f.primary_document or filing.primary_document
        filing.url = f.url or filing.url
        parsed.append((filing, holdings))
        log.info("[%d/%d] %-9s %s %s schema=%-5s amend=%-12s n=%2d  $%s",
                 i, len(targets), filing.form_type, filing.report_date,
                 filing.accession, filing.schema_version or "-",
                 filing.amendment_type or "-", len(holdings),
                 f"{sum(h.value_usd for h in holdings):,}")

    # --- 분기별 정정 해석 ------------------------------------------------
    by_quarter: dict[str, list[tuple[Filing, list[Holding]]]] = defaultdict(list)
    for filing, holdings in parsed:
        by_quarter[filing.report_date].append((filing, holdings))

    resolved: dict[str, list[Holding]] = {}
    coverage_rows: list[dict] = []
    for report_date in sorted(by_quarter):
        rows = resolve_quarter(by_quarter[report_date])
        # 정책 적용은 CUSIP -> 티커 매핑 '앞'이어야 한다. 뒤로 미루면 버릴
        # 6,500종목까지 OpenFIGI 에 물어보게 된다.
        kept, coverage = apply_entity_policy(rows, report_date)
        resolved[report_date] = kept
        coverage_rows.append(coverage)

    # --- CUSIP -> 티커 ---------------------------------------------------
    cusips = sorted({h.cusip for rows in resolved.values() for h in rows})
    mapping: dict[str, dict] = {}
    if map_tickers and cusips:
        mapping = cusip_map.map_cusips(cusips, force=False)
    elif cusips:
        mapping = cusip_map.load_cache()
        log.info("--no-map: OpenFIGI 호출 없이 캐시만 사용")
    for rows in resolved.values():
        for h in rows:
            entry = mapping.get(h.cusip) or {}
            h.ticker = entry.get("ticker")
            h.figi = entry.get("composite_figi") or entry.get("figi")

    # --- 기존 결과와 병합 후 기록 (처리한 분기만 교체) --------------------
    kept_holdings = [r for r in schema.read_jsonl(Paths.HOLDINGS)
                     if r.get("report_date") not in resolved]
    new_holdings = [asdict(h) for rows in resolved.values() for h in rows]
    n_h = schema.write_jsonl(Paths.HOLDINGS, kept_holdings + new_holdings,
                             sort_key=lambda r: (r["report_date"], -r["value_usd"]))

    filing_rows: dict[str, dict] = {r["accession"]: r
                                    for r in schema.read_jsonl(Paths.FILINGS)}
    for f in all_filings:                       # 공시 인덱스: 전 폼 타입 수록
        filing_rows[f.accession] = asdict(f)
    for filing, _ in parsed:                    # 13F 는 파싱 메타로 덮어쓴다
        filing_rows[filing.accession] = asdict(filing)
    n_f = schema.write_jsonl(Paths.FILINGS, list(filing_rows.values()),
                             sort_key=lambda r: (r["filing_date"], r["accession"]))

    kept_cov = [r for r in schema.read_jsonl(Paths.COVERAGE)
                if r.get("report_date") not in resolved]
    schema.write_jsonl(Paths.COVERAGE, kept_cov + coverage_rows,
                       sort_key=lambda r: r["report_date"])

    return {
        "quarters": resolved,
        "coverage": coverage_rows,
        "filings_written": n_f,
        "holdings_written": n_h,
        "failures": failures,
        "mapping": mapping,
        "parsed": parsed,
    }


# ---------------------------------------------------------------- 리포트

def _report(result: dict) -> int:
    resolved: dict[str, list[Holding]] = result["quarters"]
    print()
    print(f"{'report_date':<12} {'n':>3} {'total_usd':>18}  schema  filings")
    print("-" * 62)
    for report_date in sorted(resolved):
        rows = resolved[report_date]
        total = sum(h.value_usd for h in rows)
        schemas = sorted({h.schema_version or "-" for h in rows})
        accs = sorted({h.accession for h in rows})
        print(f"{report_date:<12} {len(rows):>3} {total:>18,}  "
              f"{','.join(schemas):<7} {len(accs)}")

    print()
    failed = 0
    for report_date, (want_n, want_total) in sorted(GOLDEN.items()):
        rows = resolved.get(report_date)
        if not rows:
            continue
        got_n, got_total = len(rows), sum(h.value_usd for h in rows)
        ok = (got_n == want_n and got_total == want_total)
        failed += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] golden {report_date}: "
              f"{got_n}종목 ${got_total:,} (기대 {want_n}종목 ${want_total:,})")

    truncated = [c for c in (result.get("coverage") or []) if c.get("truncated")]
    if truncated:
        worst = min(c["coverage_pct"] for c in truncated)
        print(f"정책 절삭 {len(truncated)}개 분기 — 최저 가치 커버리지 {worst:.1f}%")

    mapping = result.get("mapping") or {}
    unresolved = [c for c, e in mapping.items() if not e.get("ticker")]
    print(f"\nCUSIP 매핑: {len(mapping) - len(unresolved)}/{len(mapping)} 해석"
          + (f" (미해석: {', '.join(unresolved)})" if unresolved else ""))

    print(f"holdings.jsonl {result['holdings_written']}행 / "
          f"filings.jsonl {result['filings_written']}행")
    print(f"SEC 요청 {len(edgar.REQUEST_TIMES)}건, "
          f"최대 {edgar.max_requests_per_second()} req/s "
          f"(제한 3 req/s)")
    if result["failures"]:
        print(f"\n파싱 실패 {len(result['failures'])}건:")
        for accession, reason in result["failures"].items():
            print(f"  - {accession}: {reason}")
    return 1 if failed else 0


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.collector.backfill",
        description=f"{ENTITY.display} 13F 를 EDGAR 에서 수집해 정규화 JSONL 을 만든다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예) python -m src.collector.backfill --limit 8")
    parser.add_argument("--limit", type=int, default=None,
                        help="최근 N건의 13F 만 처리 (생략 시 엔티티 기본값). "
                             "같은 분기의 원본/정정은 자동으로 함께 포함된다.")
    parser.add_argument("--force", action="store_true",
                        help="data/raw 캐시를 무시하고 EDGAR 에서 다시 내려받는다.")
    parser.add_argument("--forms", default=",".join(FORMS_13F),
                        help="처리할 폼 타입 (쉼표 구분). 기본 13F-HR,13F-HR/A")
    parser.add_argument("--no-map", action="store_true",
                        help="OpenFIGI 호출 없이 기존 CUSIP 캐시만 사용한다.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG 로그 출력")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s", stream=sys.stderr)

    log.info("엔티티: %s (%s, CIK %s)", ENTITY.display, ENTITY.manager, ENTITY.cik)
    limit = args.limit if args.limit is not None else ENTITY.default_backfill
    result = run(limit=limit, force=args.force,
                 forms=[f.strip() for f in args.forms.split(",") if f.strip()],
                 map_tickers=not args.no_map)
    status = _report(result)
    if result["failures"]:
        status = status or 0        # 부분 실패는 경고. 무결성 위반만 예외로 중단.
    return status


if __name__ == "__main__":
    raise SystemExit(main())
