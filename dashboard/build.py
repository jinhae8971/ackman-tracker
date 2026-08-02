#!/usr/bin/env python3
"""dashboard/build.py — normalized JSONL -> dashboard/data/*.json (멀티 엔티티).

대시보드는 런타임에 집계하지 않는다. 모든 집계는 여기서 끝내고
`index.html` 은 fetch 한 JSON 을 그리기만 한다 (빌드 체인 없음의 대가).

출력:
  dashboard/data/index.json      — 엔티티 목록 + 요약 (사이드탭이 제일 먼저 읽는다)
  dashboard/data/{key}.json      — 엔티티별 전체 페이로드
  dashboard/data/compare.json    — 3사 비교 뷰 전용 사전 집계

입력 해석 (엔티티별):
  1) data/normalized/{key}/holdings.jsonl 이 있고 비어 있지 않으면 그것
  2) (pershing 한정) dashboard/sample/ 합성 데이터 — --no-sample 로 비활성화
  3) 아무것도 없으면 그 엔티티는 **건너뛴다**. 단 하나도 못 만들면 exit 1.

표준 라이브러리만 사용한다.

사용:
    python3 dashboard/build.py [--entity KEY] [--out-dir DIR] [--no-sample] [--quiet]
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

# 공유 계약에서 상수/레지스트리를 가져온다. 계약 파일이 없어도 대시보드 빌드는
# 살아남아야 하므로 Pershing 단독 폴백을 유지한다.
try:
    from src.common import entities as _entities            # type: ignore
    from src.common.schema import (                          # type: ignore
        STRONG_NEW_WEIGHT_PCT, STRONG_DELTA_BP, MIN_DELTA_PCT,
    )
    ENTITY_ORDER = list(_entities.ORDER)

    def _entity_meta(key: str) -> dict:
        e = _entities.get(key)
        return dict(key=e.key, name=e.name, display=e.display, manager=e.manager,
                    profile=e.profile, color=e.color, blurb=e.blurb,
                    cik=e.cik, cik_short=e.cik_short, truncated=e.truncated,
                    exclude_options=e.exclude_options, max_positions=e.max_positions,
                    archive_base=e.archive_base)
except Exception:  # pragma: no cover
    STRONG_NEW_WEIGHT_PCT, STRONG_DELTA_BP, MIN_DELTA_PCT = 5.0, 200, 1.0
    ENTITY_ORDER = ["pershing"]

    def _entity_meta(key: str) -> dict:
        return dict(key="pershing", name="Pershing Square Capital Management, L.P.",
                    display="Pershing Square", manager="Bill Ackman",
                    profile="conviction", color="#2563eb", blurb="",
                    cik="0001336528", cik_short="1336528", truncated=False,
                    exclude_options=False, max_positions=None,
                    archive_base="https://www.sec.gov/Archives/edgar/data/1336528")

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

# 마켓메이커 프로파일 전용 경고 — 개별 종목을 '확신'으로 읽지 못하게 막는다.
MARKET_MAKER_LIMITATIONS = [
    "이 운용사는 마켓메이커·멀티전략 성격이다. 개별 종목 편입을 '확신'이나 방향성 베팅으로 "
    "해석하면 안 된다. 상당수는 헤지·차익거래·고객 유동성 공급의 잔여물이다.",
    "옵션(PUT/CALL) 포지션은 표시에서 제외했다. 원 13F 에서는 옵션 명목가가 전체의 다수를 "
    "차지하므로, 여기 보이는 금액은 실제 익스포저의 일부다.",
    "보통주 상위 종목만 표시한다. 비중(weight_pct)은 절삭 전 전체 포트폴리오 기준으로 계산해 "
    "합이 100% 에 미달한다 — 이것이 정직한 표기다.",
]

# 비교 뷰에서 활성 엔티티 컨텍스트. 순차 빌드라 단일 작성자만 존재한다.
_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/1336528"


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
        return f"{_ARCHIVE_BASE}/"
    return f"{_ARCHIVE_BASE}/{str(accession).replace('-', '')}/"


def qlabel(report_date: str) -> str:
    y, m, _ = report_date.split("-")
    return f"{y} Q{(int(m) - 1) // 3 + 1}"


def lag_days(report_date: str, filing_date: str):
    try:
        return (date.fromisoformat(filing_date) - date.fromisoformat(report_date)).days
    except Exception:
        return None


# ----------------------------------------------------------------- 입력 해석
def usable(d: str) -> bool:
    p = os.path.join(d, "holdings.jsonl")
    return os.path.exists(p) and os.path.getsize(p) > 0


def resolve_input(key: str, allow_sample: bool) -> tuple:
    """(dir, kind, warnings) 또는 (None, None, None) — 데이터가 없으면 None."""
    normalized = os.path.join(_ROOT, "data", "normalized", key)
    if usable(normalized):
        return normalized, "normalized", []

    # 합성 데이터 폴백은 기본 엔티티에만 적용한다. Berkshire 자리에 Pershing
    # 더미가 뜨는 것보다 탭이 비활성으로 보이는 편이 훨씬 정직하다.
    if allow_sample and key == ENTITY_ORDER[0]:
        sample = os.path.join(_HERE, "sample")
        if usable(sample):
            return sample, "sample", [
                f"실데이터(data/normalized/{key}/holdings.jsonl)가 아직 없어 "
                "dashboard/sample/ 합성 데이터로 빌드했습니다. 수치는 검증용 더미입니다."
            ]
    return None, None, None


def fail_no_input(checked: list) -> None:
    print("=" * 72, file=sys.stderr)
    print("[FATAL] 어느 엔티티에서도 입력 데이터를 찾지 못했습니다. 중단합니다.",
          file=sys.stderr)
    print("        (빈 대시보드를 조용히 만드는 것이 최악의 실패 모드입니다.)", file=sys.stderr)
    print("", file=sys.stderr)
    print("  확인한 경로 (holdings.jsonl 이 존재하고 크기 > 0 이어야 함):", file=sys.stderr)
    for d in checked:
        p = os.path.join(d, "holdings.jsonl")
        why = "없음" if not os.path.exists(p) else (
            "0 바이트" if os.path.getsize(p) == 0 else "읽기 실패")
        print(f"    - {p}  ({why})", file=sys.stderr)
    print("", file=sys.stderr)
    print("  해결 방법:", file=sys.stderr)
    print("    1) 실데이터 생성:   TRACKER_ENTITY=pershing python -m src.collector.backfill "
          "&& TRACKER_ENTITY=pershing python -m src.analytics.build_events", file=sys.stderr)
    print("    2) 합성 데이터 생성: python3 dashboard/sample_data.py", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    raise SystemExit(1)


# ----------------------------------------------------------------- 집계
def key_of(h: dict) -> str:
    """포지션 동일성 키 — schema.Holding.key 와 같은 정의(put_call 포함).

    put_call 을 빼면 같은 발행사의 보통주와 옵션 포지션이 한 칸에서 충돌해
    매수/매도 방향이 뒤집힌다.
    """
    return (f"{h.get('cusip')}|{h.get('title_of_class') or ''}"
            f"|{h.get('put_call') or ''}")


def group_quarters(holdings: list) -> list:
    """report_date 별 스냅샷. 동일 포지션 키 중복은 최신 filing_date 채택."""
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
        rows.sort(key=lambda r: -int(r.get("value_usd") or 0))
        base_fd = min((r.get("filing_date") or "9999-12-31") for r in rows)
        last_fd = max((r.get("filing_date") or "") for r in rows)
        quarters.append(dict(
            report_date=rd, label=qlabel(rd), filing_date=base_fd,
            last_filing_date=last_fd, rows=rows,
            total_value_usd=total, position_count=len(rows),
            weights_recomputed=False,
        ))
    return quarters


def normalize_weights(quarters: list, truncated: bool) -> bool:
    """weight_pct 위생 점검.

    절삭되지 않은 엔티티는 합이 100±1 을 벗어나면 value 기준으로 재계산한다.
    절삭된 엔티티(Citadel)는 **절대 재계산하지 않는다** — 상위 200종목만으로
    비중을 다시 매기면 합이 100% 가 되어, 6,000종목을 든 마켓메이커가 마치
    집중 포트폴리오인 것처럼 보이는 치명적 왜곡이 생긴다.
    """
    if truncated:
        return False
    recomputed = False
    for q in quarters:
        total = q["total_value_usd"]
        wsum = sum(float(r.get("weight_pct") or 0) for r in q["rows"])
        if total > 0 and (wsum <= 0 or abs(wsum - 100.0) > 1.0):
            for r in q["rows"]:
                r["weight_pct"] = round(int(r.get("value_usd") or 0) / total * 100.0, 4)
            q["weights_recomputed"] = True
            recomputed = True
    return recomputed


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


def build_series(quarters: list, periods: dict, filings: list, top: int = 0) -> dict:
    """종목별 시계열. top>0 이면 최신 비중 상위 top 종목만 남긴다.

    Citadel 처럼 971개 CUSIP 을 추적하는 엔티티에서 전 종목 타임라인을 실으면
    페이로드가 수십 MB 로 부푼다. 브라우저에서 그릴 수 없는 데이터는 정직한
    데이터가 아니라 그냥 무게다.
    """
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
                put_call=r.get("put_call"),
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
        m["latest_weight"] = m["weights"][-1] if n else 0.0
        m["max_weight"] = max(m["weights"]) if n else 0.0
        out.append(m)

    out.sort(key=lambda m: (-m["latest_weight"], -m["max_weight"], m["name"] or ""))
    truncated_series = False
    if top and len(out) > top:
        out = out[:top]
        truncated_series = True

    kept = {m["key"] for m in out}
    for m in out:
        tl = []
        for q in quarters:
            i = idx[q["report_date"]]
            if not m["present"][i]:
                continue
            acc = m["accessions"][i]
            row = next((r for r in q["rows"] if key_of(r) == m["key"]), None)
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

    return dict(labels=labels, report_dates=dates, securities=out,
                truncated=truncated_series, shown=len(kept))


def build_current(quarters: list, periods: dict, events: list, metrics: list) -> dict:
    q = quarters[-1]
    prev = quarters[-2] if len(quarters) > 1 else None
    prev_by_key = {key_of(r): r for r in prev["rows"]} if prev else {}
    ev_by_key = {}
    for e in events:
        if e.get("report_date") == q["report_date"] and not e.get("provisional"):
            ev_by_key[e.get("cusip")] = e

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
        ev = ev_by_key.get(r.get("cusip"))
        delta_pct = round((sh - ps) / ps * 100.0, 2) if ps else None
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


def enrich_events(events: list, limit: int = 0) -> list:
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
    out.sort(key=lambda e: (e.get("filing_date") or "", e.get("report_date") or "",
                            abs(float(e.get("weight_delta_bp") or 0))), reverse=True)
    if limit and len(out) > limit:
        # 최신순 상위 limit 건 + 잠정 이벤트는 전부 보존 (건수가 적고 의미가 크다)
        head = out[:limit]
        seen = {id(x) for x in head}
        head += [e for e in out[limit:] if e["provisional"] and id(e) not in seen]
        out = head
    return out


def enrich_filings(filings: list) -> list:
    out = []
    for f in filings:
        f = dict(f)
        f["dir_url"] = edgar_url(f.get("accession"))
        f["doc_url"] = f.get("url") or f["dir_url"]
        f["url"] = f["dir_url"]
        f["label"] = qlabel(f["report_date"]) if f.get("report_date") else None
        out.append(f)
    out.sort(key=lambda f: (f.get("filing_date") or "", f.get("accession") or ""), reverse=True)
    return out


# ----------------------------------------------------------------- 엔티티 빌드
def build_entity(key: str, allow_sample: bool) -> tuple:
    """(payload, summary) 또는 (None, None)."""
    global _ARCHIVE_BASE

    em = _entity_meta(key)
    _ARCHIVE_BASE = em["archive_base"]

    src, kind, warnings = resolve_input(key, allow_sample)
    if src is None:
        return None, None

    holdings = read_jsonl(os.path.join(src, "holdings.jsonl"))
    events = read_jsonl(os.path.join(src, "events.jsonl"))
    metrics = read_jsonl(os.path.join(src, "metrics.jsonl"))
    filings = read_jsonl(os.path.join(src, "filings.jsonl"))
    coverage = read_jsonl(os.path.join(src, "coverage.jsonl"))
    if not holdings:
        return None, None

    quarters = group_quarters(holdings)
    if not quarters or quarters[-1]["position_count"] == 0:
        print(f"[WARN] {key}: 유효 분기가 없어 건너뜁니다.", file=sys.stderr)
        return None, None

    if normalize_weights(quarters, em["truncated"]):
        warnings.append("일부 분기에서 weight_pct 합계가 100±1 을 벗어나 value 기준으로 "
                        "재계산했습니다.")

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

    coverage = sorted(coverage, key=lambda c: c.get("report_date") or "")
    cov_min = min((c.get("coverage_pct") or 0.0) for c in coverage) if coverage else None
    cov_latest = coverage[-1] if coverage else None
    # 절삭 공시는 index.html 이 meta.coverage_* 로 직접 배너를 만든다. 여기서
    # warnings 에도 넣으면 같은 경고가 두 번 쌓여 오히려 읽히지 않는다.

    # 페이로드 크기 방어: 절삭 엔티티는 시계열/이벤트를 상위 구간으로 제한한다.
    series_top = 60 if em["truncated"] else 0
    events_limit = 400 if em["truncated"] else 0

    periods = holding_periods(quarters)
    ev = enrich_events(events, limit=events_limit)
    limitations = list(LIMITATIONS) if em["profile"] != "market_maker" else \
        MARKET_MAKER_LIMITATIONS + LIMITATIONS[:2]

    payload = dict(
        meta=dict(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            data_source=kind,
            source_dir=os.path.relpath(src, _ROOT).replace("\\", "/"),
            is_sample=(kind == "sample"),
            metrics_derived=metrics_derived,
            warnings=warnings,
            entity=em,
            coverage=coverage,
            coverage_min_pct=cov_min,
            coverage_latest_pct=(cov_latest or {}).get("coverage_pct"),
            edgar_archive_base=em["archive_base"],
            edgar_url_pattern=f"{em['archive_base']}/{{accession_no_dashes}}/",
            thresholds=dict(min_delta_pct=MIN_DELTA_PCT,
                            strong_new_weight_pct=STRONG_NEW_WEIGHT_PCT,
                            strong_delta_bp=STRONG_DELTA_BP),
            counts=dict(holdings=len(holdings), events=len(events),
                        metrics=len(metrics), filings=len(filings),
                        quarters=len(quarters)),
        ),
        disclaimer=DISCLAIMER,
        limitations=limitations,
        quarters=[dict(report_date=q["report_date"], label=q["label"],
                       filing_date=q["last_filing_date"] or q["filing_date"],
                       lag_days=lag_days(q["report_date"],
                                         q["last_filing_date"] or q["filing_date"]),
                       total_value_usd=q["total_value_usd"],
                       position_count=q["position_count"]) for q in quarters],
        current=build_current(quarters, periods, events, metrics),
        events=ev,
        provisional_events=[e for e in ev if e["provisional"]],
        series=build_series(quarters, periods, filings, top=series_top),
        metrics=metrics,
        filings=enrich_filings(filings),
    )

    cur = payload["current"]
    summary = dict(
        key=key, **{k: em[k] for k in ("display", "name", "manager", "profile",
                                       "color", "blurb", "cik", "truncated")},
        report_date=cur["report_date"], label=cur["label"],
        filing_date=cur["filing_date"], lag_days=cur["lag_days"],
        total_value_usd=cur["total_value_usd"], position_count=cur["position_count"],
        hhi=cur["hhi"], top5_pct=cur["top5_pct"],
        quarters=len(payload["quarters"]),
        first_report_date=payload["quarters"][0]["report_date"],
        coverage_pct=(cov_latest or {}).get("coverage_pct"),
        coverage_min_pct=cov_min,
        is_sample=(kind == "sample"),
        data_url=f"data/{key}.json",
    )
    return payload, summary


# ----------------------------------------------------------------- 비교 뷰
DIRECTION = {"NEW": +1, "ADD": +1, "TRIM": -1, "EXIT": -1}


def build_compare(payloads: dict) -> dict:
    """3사 비교 사전 집계.

    분기 축은 모든 엔티티 report_date 의 합집합이고, 데이터가 없는 칸은 null 로
    남겨 프론트가 spanGaps 로 잇는다. 절삭 엔티티(Citadel)의 금액을 다른 두
    곳과 같은 축에 올리면 잘못된 인상을 주므로, 시리즈마다 truncated 플래그를
    실어 프론트가 반드시 주석을 달게 한다.
    """
    keys = [k for k in ENTITY_ORDER if k in payloads]
    # 축은 '2곳 이상이 데이터를 가진 분기'로 좁힌다. Pershing 만 있는 2013~2020 구간까지
    # 실으면 비교 차트의 3분의 2가 빈 칸이 되어 오히려 읽기 어려워진다.
    counts: dict = {}
    for k in keys:
        for q in payloads[k]["quarters"]:
            counts[q["report_date"]] = counts.get(q["report_date"], 0) + 1
    dates = sorted(d for d, c in counts.items() if c >= min(2, len(keys)))
    if not dates:
        dates = sorted(counts)
    idx = {d: i for i, d in enumerate(dates)}
    n = len(dates)

    def blank():
        return [None] * n

    series = {m: {} for m in ("total_value_usd", "position_count", "hhi",
                              "top5_pct", "turnover_pct")}
    for k in keys:
        for m in series:
            series[m][k] = blank()
        for q in payloads[k]["quarters"]:
            i = idx.get(q["report_date"])
            if i is None:                      # 축에서 잘려나간 초기 분기
                continue
            series["total_value_usd"][k][i] = q["total_value_usd"]
            series["position_count"][k][i] = q["position_count"]
        for mt in payloads[k]["metrics"]:
            i = idx.get(mt.get("report_date"))
            if i is None:
                continue
            series["hhi"][k][i] = mt.get("hhi")
            series["top5_pct"][k][i] = mt.get("top5_pct")
            series["turnover_pct"][k][i] = mt.get("turnover_pct")

    # --- 공통 보유: 최신 분기 기준으로 2곳 이상이 든 CUSIP ---
    latest_by_cusip: dict = {}
    for k in keys:
        for p in payloads[k]["current"]["positions"]:
            if p.get("put_call"):
                continue
            slot = latest_by_cusip.setdefault(p["cusip"], dict(
                cusip=p["cusip"], ticker=p.get("ticker"),
                name=p.get("issuer_name"), holders={}))
            if p.get("ticker"):
                slot["ticker"] = p["ticker"]
            prev = slot["holders"].get(k)
            if prev is None or p["value_usd"] > prev["value_usd"]:
                slot["holders"][k] = dict(
                    entity=k, value_usd=p["value_usd"],
                    weight_pct=p["weight_pct"], shares=p["shares"],
                    event_type=p.get("event_type"), conviction=p.get("conviction"),
                    report_date=payloads[k]["current"]["report_date"])

    common = []
    for c in latest_by_cusip.values():
        if len(c["holders"]) < 2:
            continue
        holders = [c["holders"][k] for k in keys if k in c["holders"]]
        common.append(dict(cusip=c["cusip"], ticker=c["ticker"], name=c["name"],
                           holder_count=len(holders), holders=holders,
                           total_value_usd=sum(h["value_usd"] for h in holders),
                           max_weight_pct=max(h["weight_pct"] for h in holders)))
    common.sort(key=lambda c: (-c["holder_count"], -c["total_value_usd"]))

    # --- 엇갈린 매매: 같은 분기·같은 CUSIP 에서 방향이 반대인 경우 ---
    moves: dict = {}
    for k in keys:
        for e in payloads[k]["events"]:
            if e.get("provisional"):
                continue
            d = DIRECTION.get(e.get("event_type"))
            if not d:
                continue
            slot = moves.setdefault((e.get("report_date"), e.get("cusip")), {})
            slot[k] = dict(entity=k, event_type=e.get("event_type"),
                           conviction=e.get("conviction"),
                           weight_delta_bp=e.get("weight_delta_bp"),
                           share_delta_pct=e.get("share_delta_pct"),
                           direction=d, ticker=e.get("ticker"),
                           name=e.get("issuer_name"))
    opposing = []
    for (rd, cusip), by_ent in moves.items():
        dirs = {m["direction"] for m in by_ent.values()}
        if len(by_ent) < 2 or len(dirs) < 2:
            continue
        rows = [by_ent[k] for k in keys if k in by_ent]
        ticker = next((r["ticker"] for r in rows if r.get("ticker")), None)
        name = next((r["name"] for r in rows if r.get("name")), None)
        opposing.append(dict(report_date=rd, label=qlabel(rd) if rd else None,
                             cusip=cusip, ticker=ticker, name=name, moves=rows,
                             magnitude=sum(abs(float(r.get("weight_delta_bp") or 0))
                                           for r in rows)))
    opposing.sort(key=lambda o: (o["report_date"] or "", o["magnitude"]), reverse=True)

    return dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        entities=[dict(key=k, **{f: _entity_meta(k)[f]
                                 for f in ("display", "manager", "color",
                                           "profile", "truncated")})
                  for k in keys],
        labels=[qlabel(d) for d in dates],
        report_dates=dates,
        series=series,
        common=common[:60],
        opposing=opposing[:120],
        notes=[
            "Citadel 은 보통주 상위 200종목만 수집하므로 총액·종목수를 다른 두 곳과 "
            "직접 비교하면 안 된다. 추세(방향)만 비교 가능하다.",
            "13F 는 45일 지연 스냅샷이다. '엇갈린 매매'는 같은 분기말 기준일 뿐 "
            "실제로 같은 날 반대로 거래했다는 뜻이 아니다.",
        ],
    )


# ----------------------------------------------------------------- 기록
def write_json(path: str, payload) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=False)
    os.replace(tmp, path)
    with open(path, encoding="utf-8") as f:      # 자체 검증: 실제로 파싱되는지
        json.load(f)
    return os.path.getsize(path)


# ----------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="normalized JSONL -> dashboard/data/*.json 사전 집계 (멀티 엔티티)")
    ap.add_argument("--entity", default=None,
                    help=f"이 엔티티만 빌드 ({', '.join(ENTITY_ORDER)}). 기본 전체.")
    ap.add_argument("--out-dir", default=os.path.join(_HERE, "data"),
                    help="출력 디렉터리 (기본 dashboard/data)")
    ap.add_argument("--no-sample", action="store_true",
                    help="dashboard/sample 폴백을 금지 (CI/실배포용)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    targets = [args.entity] if args.entity else list(ENTITY_ORDER)
    for t in targets:
        if t not in ENTITY_ORDER:
            print(f"[FATAL] 알 수 없는 엔티티 '{t}'. 사용 가능: {', '.join(ENTITY_ORDER)}",
                  file=sys.stderr)
            return 1

    out_dir = os.path.abspath(args.out_dir)
    payloads: dict = {}
    summaries: list = []
    for key in targets:
        payload, summary = build_entity(key, allow_sample=not args.no_sample)
        if payload is None:
            if not args.quiet:
                print(f"[build] {key:10s} 데이터 없음 — 건너뜀", file=sys.stderr)
            continue
        payloads[key] = payload
        summaries.append(summary)
        size = write_json(os.path.join(out_dir, f"{key}.json"), payload)
        if not args.quiet:
            c = payload["meta"]["counts"]
            cur = payload["current"]
            cov = payload["meta"]["coverage_latest_pct"]
            print(f"[build] {key:10s} {c['quarters']:2d}분기 "
                  f"holdings={c['holdings']:<5d} events={c['events']:<5d} "
                  f"최신 {cur['report_date']} {cur['position_count']}종목 "
                  f"${cur['total_value_usd']:,} "
                  + (f"(커버리지 {cov:.1f}%) " if cov is not None else "")
                  + f"[{size:,}B]")

    if not payloads:
        fail_no_input([os.path.join(_ROOT, "data", "normalized", k) for k in targets])

    index = dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        default_entity=summaries[0]["key"],
        entities=summaries,
        disclaimer=DISCLAIMER,
    )
    write_json(os.path.join(out_dir, "index.json"), index)

    compare = build_compare(payloads)
    write_json(os.path.join(out_dir, "compare.json"), compare)

    # 구 배포 경로 호환: 기본 엔티티 페이로드를 예전 위치에도 남긴다.
    legacy = os.path.join(_HERE, "dashboard_data.json")
    write_json(legacy, payloads[summaries[0]["key"]])

    if not args.quiet:
        print(f"[build] index.json {len(summaries)}개 엔티티, "
              f"compare.json 공통 {len(compare['common'])}종목 / "
              f"엇갈림 {len(compare['opposing'])}건")
        for s in summaries:
            for w in payloads[s["key"]]["meta"]["warnings"]:
                print(f"[build][WARN] {s['key']}: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# end of build.py
