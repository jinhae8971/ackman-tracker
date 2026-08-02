#!/usr/bin/env python3
"""dashboard/build.py — normalized JSONL -> dashboard/dashboard_data.json.

대시보드는 런타임에 집계하지 않는다. 모든 집계는 여기서 끝내고
`index.html` 은 fetch 한 JSON 을 그리기만 한다 (빌드 체인 없음의 대가).

입력 해석 순서:
  1) --input DIR 이 주어지면 그 디렉터리 (없거나 비면 하드 실패)
  2) data/normalized/holdings.jsonl 이 존재하고 비어 있지 않으면 그것 (실데이터)
  3) dashboard/sample/holdings.jsonl (합성 데이터, --no-sample 로 비활성화)
  4) 어느 것도 없으면 **명확한 에러 + exit 1**. 빈 대시보드를 만들지 않는다.

표준 라이브러리만 사용한다.

사용:
    python3 dashboard/build.py [--input DIR] [--out FILE] [--no-sample] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 공유 계약에서 상수만 가져온다. 계약 파일이 없어도 대시보드 빌드는 살아남아야 하므로 폴백.
try:
    from src.common.schema import (  # type: ignore
        CIK, CIK_SHORT, ENTITY_NAME, ARCHIVE_BASE,
        STRONG_NEW_WEIGHT_PCT, STRONG_DELTA_BP, MIN_DELTA_PCT,
    )
except Exception:  # pragma: no cover
    CIK = "0001336528"
    CIK_SHORT = "1336528"
    ENTITY_NAME = "Pershing Square Capital Management, L.P."
    ARCHIVE_BASE = f"https://www.sec.gov/Archives/edgar/data/{CIK_SHORT}"
    STRONG_NEW_WEIGHT_PCT, STRONG_DELTA_BP, MIN_DELTA_PCT = 5.0, 200, 1.0

DISCLAIMER = ("13F는 미국 상장 롱 포지션만 포함하며 스왑·공매도·비상장 자산은 "
              "나타나지 않습니다. 본 자료는 정보 제공 목적이며 투자 자문이 아닙니다.")

LIMITATIONS = [
    "13F는 미국 상장 롱 포지션 스냅샷이다. 공매도·토탈리턴스왑·CDS·채권·비상장 자산·현금은 "
    "원리적으로 나타나지 않는다 (2020년 크레딧 헤지가 대표 사례).",
    "45일 지연은 우회 불가능하다. 공시 시점의 포지션은 이미 최대 4.5개월 전 상태일 수 있다.",
    "13D 기반 잠정 수치는 5% 이상 지분에만 적용되며 13F로 확정되기 전이다.",
    "해외 상장 클래스(예: Brookfield 캐나다 상장분)는 13F에 포함되지 않을 수 있다.",
    "PSH 펀드 NAV 성과와 13F 포트폴리오는 일치하지 않는다. 등가로 취급하면 안 된다.",
]


# ----------------------------------------------------------------- I/O
def read_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"[FATAL] {path}:{i} JSON 파싱 실패: {exc}")
    return rows


def edgar_url(accession) -> str:
    if not accession:
        return f"{ARCHIVE_BASE}/"
    return f"{ARCHIVE_BASE}/{str(accession).replace('-', '')}/"


def qlabel(report_date: str) -> str:
    y, m, _ = report_date.split("-")
    return f"{y} Q{(int(m) - 1) // 3 + 1}"


def lag_days(report_date: str, filing_date: str):
    try:
        return (date.fromisoformat(filing_date) - date.fromisoformat(report_date)).days
    except Exception:
        return None


# ----------------------------------------------------------------- 입력 해석
def resolve_input(explicit: str | None, allow_sample: bool) -> tuple:
    normalized = os.path.join(_ROOT, "data", "normalized")
    sample = os.path.join(_HERE, "sample")
    checked = []

    def usable(d):
        p = os.path.join(d, "holdings.jsonl")
        return os.path.exists(p) and os.path.getsize(p) > 0

    if explicit:
        d = os.path.abspath(explicit)
        if not usable(d):
            fail_no_input([d], explicit=True)
        return d, "explicit", []

    checked.append(normalized)
    if usable(normalized):
        return normalized, "normalized", []

    if allow_sample:
        checked.append(sample)
        if usable(sample):
            return sample, "sample", [
                "실데이터(data/normalized/holdings.jsonl)가 아직 없어 "
                "dashboard/sample/ 합성 데이터로 빌드했습니다. 수치는 검증용 더미입니다."
            ]

    fail_no_input(checked, explicit=False)


def fail_no_input(checked: list, explicit: bool) -> None:
    print("=" * 72, file=sys.stderr)
    print("[FATAL] 입력 데이터를 찾을 수 없습니다. 대시보드를 빌드하지 않고 중단합니다.",
          file=sys.stderr)
    print("        (빈 대시보드를 조용히 만드는 것이 최악의 실패 모드입니다.)", file=sys.stderr)
    print("", file=sys.stderr)
    print("  확인한 경로 (holdings.jsonl 이 존재하고 크기 > 0 이어야 함):", file=sys.stderr)
    for d in checked:
        p = os.path.join(d, "holdings.jsonl")
        if not os.path.exists(p):
            why = "없음"
        elif os.path.getsize(p) == 0:
            why = "0 바이트"
        else:
            why = "읽기 실패"
        print(f"    - {p}  ({why})", file=sys.stderr)
    print("", file=sys.stderr)
    print("  해결 방법:", file=sys.stderr)
    if not explicit:
        print("    1) 실데이터 생성:   python -m src.collector.backfill "
              "&& python -m src.analytics.build_events", file=sys.stderr)
        print("    2) 합성 데이터 생성: python3 dashboard/sample_data.py", file=sys.stderr)
        print("    3) 직접 지정:       python3 dashboard/build.py --input <DIR>", file=sys.stderr)
    else:
        print("    --input 으로 지정한 디렉터리에 holdings.jsonl 이 있는지 확인하세요.",
              file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    raise SystemExit(1)


# ----------------------------------------------------------------- 집계
def key_of(h: dict) -> str:
    return f"{h.get('cusip')}|{h.get('title_of_class') or ''}"


def group_quarters(holdings: list) -> list:
    """report_date 별 스냅샷. 동일 (cusip,title_of_class) 중복은 최신 filing_date 채택."""
    by_q: dict = {}
    for h in holdings:
        rd = h.get("report_date")
        if not rd:
            continue
        slot = by_q.setdefault(rd, {})
        k = key_of(h)
        prev = slot.get(k)
        if prev is None or (h.get("filing_date") or "") >= (prev.get("filing_date") or ""):
            slot[k] = h

    quarters = []
    for rd in sorted(by_q):
        rows = list(by_q[rd].values())
        total = sum(int(r.get("value_usd") or 0) for r in rows)
        # weight_pct 가 비어 있거나 합이 크게 어긋나면 값 기준으로 재계산 (표시 일관성)
        wsum = sum(float(r.get("weight_pct") or 0) for r in rows)
        recomputed = False
        if total > 0 and (wsum <= 0 or abs(wsum - 100.0) > 1.0):
            for r in rows:
                r["weight_pct"] = round(int(r.get("value_usd") or 0) / total * 100.0, 4)
            recomputed = True
        rows.sort(key=lambda r: -int(r.get("value_usd") or 0))
        # 13F 원본 filing_date (정정 행의 늦은 날짜에 끌려가지 않도록 최빈/최소값 사용)
        base_fd = min((r.get("filing_date") or "9999-12-31") for r in rows)
        last_fd = max((r.get("filing_date") or "") for r in rows)
        quarters.append(dict(
            report_date=rd,
            label=qlabel(rd),
            filing_date=base_fd,
            last_filing_date=last_fd,
            rows=rows,
            total_value_usd=total,
            position_count=len(rows),
            weights_recomputed=recomputed,
        ))
    return quarters


def derive_metrics(quarters: list, events: list) -> list:
    ev_by_q: dict = {}
    for e in events:
        if e.get("provisional"):
            continue
        ev_by_q.setdefault(e.get("report_date"), []).append(e)

    out = []
    for i, q in enumerate(quarters):
        weights = sorted((float(r.get("weight_pct") or 0) for r in q["rows"]), reverse=True)
        turnover = None
        if i > 0:
            prev = {key_of(r): int(r.get("value_usd") or 0) for r in quarters[i - 1]["rows"]}
            curr = {key_of(r): int(r.get("value_usd") or 0) for r in q["rows"]}
            churn = sum(abs(curr.get(k, 0) - prev.get(k, 0)) for k in set(prev) | set(curr))
            avg = (quarters[i - 1]["total_value_usd"] + q["total_value_usd"]) / 2.0
            turnover = round(churn / avg * 100.0, 3) if avg else None
        evs = ev_by_q.get(q["report_date"], [])
        out.append(dict(
            report_date=q["report_date"],
            filing_date=q["last_filing_date"] or q["filing_date"],
            lag_days=lag_days(q["report_date"], q["last_filing_date"] or q["filing_date"]),
            total_value_usd=q["total_value_usd"],
            position_count=q["position_count"],
            hhi=round(sum((w / 100.0) ** 2 for w in weights), 6),
            top1_pct=round(sum(weights[:1]), 4),
            top3_pct=round(sum(weights[:3]), 4),
            top5_pct=round(sum(weights[:5]), 4),
            turnover_pct=turnover,
            new_count=sum(1 for e in evs if e.get("event_type") == "NEW"),
            exit_count=sum(1 for e in evs if e.get("event_type") == "EXIT"),
            add_count=sum(1 for e in evs if e.get("event_type") == "ADD"),
            trim_count=sum(1 for e in evs if e.get("event_type") == "TRIM"),
        ))
    return out


def holding_periods(quarters: list) -> dict:
    """key -> {first_seen, last_seen, quarters_held, is_current}."""
    out: dict = {}
    last_rd = quarters[-1]["report_date"] if quarters else None
    for q in quarters:
        for r in q["rows"]:
            k = key_of(r)
            e = out.setdefault(k, dict(first_seen=q["report_date"], last_seen=q["report_date"],
                                       quarters_held=0, is_current=False))
            e["last_seen"] = q["report_date"]
            e["quarters_held"] += 1
    for k, e in out.items():
        e["is_current"] = (e["last_seen"] == last_rd)
        e["first_seen_label"] = qlabel(e["first_seen"])
        e["last_seen_label"] = qlabel(e["last_seen"])
    return out


def build_series(quarters: list, periods: dict, filings: list) -> dict:
    labels = [q["label"] for q in quarters]
    dates = [q["report_date"] for q in quarters]
    idx = {q["report_date"]: i for i, q in enumerate(quarters)}
    n = len(quarters)

    meta: dict = {}
    for q in quarters:
        for r in q["rows"]:
            k = key_of(r)
            m = meta.setdefault(k, dict(
                key=k, cusip=r.get("cusip"), ticker=r.get("ticker"),
                name=r.get("issuer_name"), title_of_class=r.get("title_of_class"),
                weights=[0.0] * n, shares=[None] * n, values=[None] * n,
                present=[False] * n, accessions=[None] * n))
            i = idx[q["report_date"]]
            m["weights"][i] = round(float(r.get("weight_pct") or 0), 4)
            m["shares"][i] = int(r.get("shares") or 0)
            m["values"][i] = int(r.get("value_usd") or 0)
            m["present"][i] = True
            m["accessions"][i] = r.get("accession")
            if r.get("ticker"):
                m["ticker"] = r["ticker"]

    # 종목별 관련 공시 타임라인:
    #   (a) 보유했던 분기의 13F/13F-A 파일링
    #   (b) filings.jsonl 에 선택 확장 필드 subject_cusip 이 있으면 그 공시들
    by_cusip: dict = {}
    for f in filings:
        sc = f.get("subject_cusip")
        if sc:
            by_cusip.setdefault(sc, []).append(f)

    out = []
    for k, m in meta.items():
        p = periods.get(k, {})
        m.update(first_seen=p.get("first_seen"), first_seen_label=p.get("first_seen_label"),
                 last_seen=p.get("last_seen"), last_seen_label=p.get("last_seen_label"),
                 quarters_held=p.get("quarters_held", 0), is_current=p.get("is_current", False))
        tl = []
        for q in quarters:
            i = idx[q["report_date"]]
            if not m["present"][i]:
                continue
            acc = m["accessions"][i]
            row = next((r for r in q["rows"] if key_of(r) == k), None)
            tl.append(dict(date=(row or {}).get("filing_date") or q["filing_date"],
                           form_type=(row or {}).get("form_type") or "13F-HR",
                           accession=acc, url=edgar_url(acc),
                           note=f"{q['label']} 보유 명세", report_date=q["report_date"]))
        for f in by_cusip.get(m["cusip"], []):
            tl.append(dict(date=f.get("filing_date"), form_type=f.get("form_type"),
                           accession=f.get("accession"),
                           url=f.get("url") or edgar_url(f.get("accession")),
                           note=f.get("items") or "", report_date=f.get("report_date")))
        tl.sort(key=lambda t: (t.get("date") or "", t.get("accession") or ""), reverse=True)
        m["timeline"] = tl
        m["latest_weight"] = m["weights"][-1] if n else 0.0
        m["max_weight"] = max(m["weights"]) if n else 0.0
        out.append(m)

    out.sort(key=lambda m: (-m["latest_weight"], -m["max_weight"], m["name"] or ""))
    return dict(labels=labels, report_dates=dates, securities=out)


def build_current(quarters: list, periods: dict, events: list, metrics: list) -> dict:
    q = quarters[-1]
    prev = quarters[-2] if len(quarters) > 1 else None
    prev_by_key = {key_of(r): r for r in prev["rows"]} if prev else {}
    ev_by_cusip = {}
    for e in events:
        if e.get("report_date") == q["report_date"] and not e.get("provisional"):
            ev_by_cusip[e.get("cusip")] = e

    m = next((x for x in metrics if x["report_date"] == q["report_date"]), None) or {}

    positions = []
    for r in q["rows"]:
        k = key_of(r)
        p = prev_by_key.get(k)
        pv = int(p.get("value_usd") or 0) if p else None
        ps = int(p.get("shares") or 0) if p else None
        pw = float(p.get("weight_pct") or 0) if p else None
        sh = int(r.get("shares") or 0)
        w = float(r.get("weight_pct") or 0)
        ev = ev_by_cusip.get(r.get("cusip"))
        delta_pct = None
        if ps:
            delta_pct = round((sh - ps) / ps * 100.0, 2)
        hp = periods.get(k, {})
        positions.append(dict(
            key=k, cusip=r.get("cusip"), ticker=r.get("ticker"),
            issuer_name=r.get("issuer_name"), title_of_class=r.get("title_of_class"),
            value_usd=int(r.get("value_usd") or 0), shares=sh, weight_pct=round(w, 4),
            prev_value_usd=pv, prev_shares=ps, prev_weight_pct=pw,
            share_delta_pct=delta_pct,
            weight_delta_bp=round((w - pw) * 100.0, 1) if pw is not None else None,
            event_type=(ev or {}).get("event_type") or ("NEW" if p is None else "HOLD"),
            conviction=(ev or {}).get("conviction") or "ROUTINE",
            first_seen=hp.get("first_seen"), first_seen_label=hp.get("first_seen_label"),
            quarters_held=hp.get("quarters_held", 1),
            accession=r.get("accession"), form_type=r.get("form_type"),
            amendment_type=r.get("amendment_type"),
            filing_date=r.get("filing_date"),
            edgar_url=edgar_url(r.get("accession")),
            put_call=r.get("put_call"), share_type=r.get("share_type"),
            discretion=r.get("discretion"),
            source="13F", provisional=False,
        ))

    return dict(
        report_date=q["report_date"], label=q["label"],
        filing_date=q["last_filing_date"] or q["filing_date"],
        base_filing_date=q["filing_date"],
        lag_days=m.get("lag_days") if m else lag_days(q["report_date"], q["filing_date"]),
        accession=q["rows"][0].get("accession") if q["rows"] else None,
        edgar_url=edgar_url(q["rows"][0].get("accession") if q["rows"] else None),
        total_value_usd=q["total_value_usd"], position_count=q["position_count"],
        hhi=m.get("hhi"), top1_pct=m.get("top1_pct"), top3_pct=m.get("top3_pct"),
        top5_pct=m.get("top5_pct"), turnover_pct=m.get("turnover_pct"),
        new_count=m.get("new_count"), exit_count=m.get("exit_count"),
        add_count=m.get("add_count"), trim_count=m.get("trim_count"),
        positions=positions,
    )


def enrich_events(events: list) -> list:
    out = []
    for e in events:
        e = dict(e)
        rd = e.get("report_date") or ""
        e["label"] = qlabel(rd) if rd.count("-") == 2 else rd
        e["lag_days"] = lag_days(rd, e.get("filing_date") or "")
        e["edgar_url"] = edgar_url(e.get("accession"))
        e["provisional"] = bool(e.get("provisional"))
        e["is_strong"] = str(e.get("conviction") or "").startswith("STRONG") or \
            e.get("conviction") == "FULL_EXIT"
        out.append(e)
    # 최신순 -> 확신도 -> 비중변화 크기순
    out.sort(key=lambda e: (e.get("filing_date") or "", e.get("report_date") or "",
                            abs(float(e.get("weight_delta_bp") or 0))), reverse=True)
    return out


def enrich_filings(filings: list, quarters: list) -> list:
    out = []
    for f in filings:
        f = dict(f)
        # 계약이 요구하는 원문 디렉터리 패턴 — collector 가 primary_doc 직링크를 넣어도 항상 제공
        f["dir_url"] = edgar_url(f.get("accession"))
        f["doc_url"] = f.get("url") or f["dir_url"]
        f["url"] = f["dir_url"]
        f["label"] = qlabel(f["report_date"]) if f.get("report_date") else None
        out.append(f)
    out.sort(key=lambda f: (f.get("filing_date") or "", f.get("accession") or ""), reverse=True)
    return out


# ----------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="normalized JSONL -> dashboard/dashboard_data.json 사전 집계")
    ap.add_argument("--input", default=None, help="입력 디렉터리 (기본: 자동 탐색)")
    ap.add_argument("--out", default=os.path.join(_HERE, "dashboard_data.json"),
                    help="출력 JSON 경로")
    ap.add_argument("--no-sample", action="store_true",
                    help="dashboard/sample 폴백을 금지 (CI/실배포용)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    src, kind, warnings = resolve_input(args.input, allow_sample=not args.no_sample)

    holdings = read_jsonl(os.path.join(src, "holdings.jsonl"))
    events = read_jsonl(os.path.join(src, "events.jsonl"))
    metrics = read_jsonl(os.path.join(src, "metrics.jsonl"))
    filings = read_jsonl(os.path.join(src, "filings.jsonl"))

    if not holdings:
        fail_no_input([src], explicit=bool(args.input))

    quarters = group_quarters(holdings)
    if not quarters:
        print("[FATAL] holdings.jsonl 에 report_date 를 가진 유효 행이 없습니다.", file=sys.stderr)
        return 1
    if quarters[-1]["position_count"] == 0:
        print("[FATAL] 최신 분기 포지션 수가 0 입니다. 빌드를 중단합니다.", file=sys.stderr)
        return 1

    if not events:
        warnings.append("events.jsonl 이 없거나 비어 있어 변화 감지 피드가 비었습니다. "
                        "`python -m src.analytics.build_events` 를 먼저 실행하세요.")
    if not metrics:
        warnings.append("metrics.jsonl 이 없어 집중도·회전율 지표를 holdings 에서 파생 계산했습니다.")
        metrics = derive_metrics(quarters, events)
        metrics_derived = True
    else:
        metrics = sorted(metrics, key=lambda m: m.get("report_date") or "")
        metrics_derived = False
    if not filings:
        warnings.append("filings.jsonl 이 없어 공시 인덱스를 13F holdings 에서 역산했습니다.")
        seen = {}
        for h in holdings:
            acc = h.get("accession")
            if acc and acc not in seen:
                seen[acc] = dict(accession=acc, form_type=h.get("form_type") or "13F-HR",
                                 filing_date=h.get("filing_date"),
                                 report_date=h.get("report_date"),
                                 amendment_type=h.get("amendment_type"),
                                 primary_document="primary_doc.xml",
                                 url=edgar_url(acc), items=None,
                                 schema_version=h.get("schema_version"))
        filings = list(seen.values())

    if any(q["weights_recomputed"] for q in quarters):
        warnings.append("일부 분기에서 weight_pct 합계가 100±1 을 벗어나 value 기준으로 재계산했습니다.")

    periods = holding_periods(quarters)
    ev = enrich_events(events)
    payload = dict(
        meta=dict(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            data_source=kind,
            source_dir=os.path.relpath(src, _ROOT).replace("\\", "/"),
            is_sample=(kind == "sample"),
            metrics_derived=metrics_derived,
            warnings=warnings,
            entity=dict(name=ENTITY_NAME, cik=CIK, cik_short=CIK_SHORT),
            edgar_archive_base=ARCHIVE_BASE,
            edgar_url_pattern=f"{ARCHIVE_BASE}/{{accession_no_dashes}}/",
            thresholds=dict(min_delta_pct=MIN_DELTA_PCT,
                            strong_new_weight_pct=STRONG_NEW_WEIGHT_PCT,
                            strong_delta_bp=STRONG_DELTA_BP),
            counts=dict(holdings=len(holdings), events=len(events),
                        metrics=len(metrics), filings=len(filings),
                        quarters=len(quarters)),
        ),
        disclaimer=DISCLAIMER,
        limitations=LIMITATIONS,
        quarters=[dict(report_date=q["report_date"], label=q["label"],
                       filing_date=q["last_filing_date"] or q["filing_date"],
                       lag_days=lag_days(q["report_date"],
                                         q["last_filing_date"] or q["filing_date"]),
                       total_value_usd=q["total_value_usd"],
                       position_count=q["position_count"]) for q in quarters],
        current=build_current(quarters, periods, events, metrics),
        events=ev,
        provisional_events=[e for e in ev if e["provisional"]],
        series=build_series(quarters, periods, filings),
        metrics=metrics,
        filings=enrich_filings(filings, quarters),
    )

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=False)
    os.replace(tmp, out)

    # 자체 검증: 방금 쓴 파일이 실제로 파싱되는지 확인
    with open(out, encoding="utf-8") as f:
        back = json.load(f)
    assert back["current"]["position_count"] == quarters[-1]["position_count"]

    if not args.quiet:
        c = payload["meta"]["counts"]
        cur = payload["current"]
        print(f"[build] source={kind} ({payload['meta']['source_dir']})")
        print(f"[build] out={out} ({os.path.getsize(out):,} bytes)")
        print(f"[build] quarters={c['quarters']} holdings={c['holdings']} "
              f"events={c['events']} metrics={c['metrics']} filings={c['filings']}")
        print(f"[build] latest {cur['report_date']} (filed {cur['filing_date']}, "
              f"lag {cur['lag_days']}d): {cur['position_count']} positions "
              f"${cur['total_value_usd']:,}")
        for w in warnings:
            print(f"[build][WARN] {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# end of build.py
              f"${cur['total_value_usd']:,}")
        for w in warnings:
            print(f"[build][WARN] {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# end of build.py
