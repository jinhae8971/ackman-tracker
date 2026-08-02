"""알림 — GitHub Step Summary 마크다운 및 Issue 본문 생성.

별도 알림 인프라(Slack, 이메일, 웹훅)를 두지 않는다. GitHub 자체 알림을
재사용하는 것이 "시크릿 0개" 제약을 유지하는 유일한 방법이다.

  - `emit_summary()`  : `$GITHUB_STEP_SUMMARY` 에 쓸 마크다운. 로컬이면 stdout.
  - `render_strong_issue()` / `render_failure_issue()` : Issue 본문.
  - Issue 제목은 **결정적**으로 만든다. 워크플로우가 같은 제목의 열린 이슈를
    찾으면 새로 만들지 않고 코멘트만 달아 매일 중복 생성되는 것을 막는다.

데모 실행:  python -m src.pipeline.notify --demo
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

try:
    from src.common.schema import ENTITY_NAME
except ImportError:  # 직접 실행
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    from src.common.schema import ENTITY_NAME

STRONG_CONVICTIONS = ("STRONG_NEW", "STRONG_ADD", "STRONG_TRIM", "FULL_EXIT")
MAX_EVENT_ROWS = 30

DISCLAIMER = (
    "13F 는 미국 상장 롱 포지션만 포함하며 스왑·공매도·비상장 자산은 "
    "나타나지 않습니다. 본 자료는 정보 제공 목적이며 투자 자문이 아닙니다."
)


# ---------------------------------------------------------------- 포맷 헬퍼

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _f(value, default=0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def usd(value) -> str:
    """사람이 읽는 달러 표기."""
    v = _f(value)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def _md_escape(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _label(ev: dict) -> str:
    ticker = ev.get("ticker")
    name = ev.get("issuer_name") or ev.get("cusip") or "(unknown)"
    return f"**{_md_escape(ticker)}** {_md_escape(name)}" if ticker else _md_escape(name)


def _latest_metrics(metrics) -> dict:
    """dict / list[dict] / None 을 모두 받아 최신 분기 한 건으로 정규화."""
    if not metrics:
        return {}
    if isinstance(metrics, dict):
        return metrics
    rows = [m for m in metrics if isinstance(m, dict)]
    if not rows:
        return {}
    return max(rows, key=lambda m: str(m.get("report_date") or ""))


def split_strong(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(STRONG_* / FULL_EXIT, 나머지) 로 분리."""
    events = [e for e in (events or []) if isinstance(e, dict)]
    strong = [e for e in events if e.get("conviction") in STRONG_CONVICTIONS]
    rest = [e for e in events if e.get("conviction") not in STRONG_CONVICTIONS]
    return strong, rest


def _sort_key(ev: dict):
    return (
        str(ev.get("report_date") or ""),
        -abs(_f(ev.get("weight_delta_bp"))),
        str(ev.get("cusip") or ""),
    )


# ---------------------------------------------------------------- 테이블

def _event_table(events: list[dict], with_conviction: bool = True) -> list[str]:
    head = "| 종목 | 유형 | 확신도 | 주식수 변화 | 비중 변화 | 평가액 변화 | 기준일 |"
    sep = "|---|---|---|---:|---:|---:|---|"
    if not with_conviction:
        head = "| 종목 | 유형 | 주식수 변화 | 비중 변화 | 평가액 변화 | 기준일 |"
        sep = "|---|---|---:|---:|---:|---|"
    lines = [head, sep]
    for ev in events:
        dp = ev.get("share_delta_pct")
        dp_s = "신규" if ev.get("event_type") == "NEW" else (
            "전량청산" if ev.get("event_type") == "EXIT"
            else (f"{_f(dp):+.1f}%" if dp is not None else "—")
        )
        wb = _f(ev.get("weight_before"))
        wa = _f(ev.get("weight_after"))
        bp = _f(ev.get("weight_delta_bp"))
        w_s = f"{wb:.2f}% → {wa:.2f}% ({bp:+.0f}bp)"
        cells = [
            _label(ev),
            _md_escape(ev.get("event_type")),
            f"`{_md_escape(ev.get('conviction'))}`",
            dp_s,
            w_s,
            usd(ev.get("value_delta_usd")),
            _md_escape(ev.get("report_date")),
        ]
        if not with_conviction:
            cells.pop(2)
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _metrics_block(m: dict) -> list[str]:
    if not m:
        return ["_분기 지표 없음 (analytics 미실행 또는 데이터 없음)._"]
    lines = [
        "| 지표 | 값 |",
        "|---|---:|",
        f"| 기준일 (report_date) | {_md_escape(m.get('report_date'))} |",
        f"| 제출일 (filing_date) | {_md_escape(m.get('filing_date'))} |",
        f"| 공시 지연 | {int(_f(m.get('lag_days')))}일 |",
        f"| 총 포트폴리오 | {usd(m.get('total_value_usd'))} |",
        f"| 포지션 수 | {int(_f(m.get('position_count')))} |",
        f"| HHI | {_f(m.get('hhi')):.4f} |",
        f"| Top-1 / Top-3 / Top-5 | "
        f"{_f(m.get('top1_pct')):.1f}% / {_f(m.get('top3_pct')):.1f}% / "
        f"{_f(m.get('top5_pct')):.1f}% |",
    ]
    if m.get("turnover_pct") is not None:
        lines.append(f"| 회전율 | {_f(m.get('turnover_pct')):.1f}% |")
    counts = [
        ("신규", m.get("new_count")), ("청산", m.get("exit_count")),
        ("증량", m.get("add_count")), ("감량", m.get("trim_count")),
    ]
    if any(c[1] for c in counts):
        lines.append(
            "| 변화 건수 | "
            + " · ".join(f"{k} {int(_f(v))}" for k, v in counts)
            + " |"
        )
    return lines


# ---------------------------------------------------------------- 공개 API

def emit_summary(events: list[dict], metrics: dict) -> str:
    """GITHUB_STEP_SUMMARY 에 쓸 마크다운 생성 (계약 시그니처).

    `metrics` 는 dict(최신 분기) 또는 list[dict](전체 분기) 둘 다 허용한다.
    STRONG_* 이벤트를 항상 최상단에 배치한다.
    """
    m = _latest_metrics(metrics)
    strong, routine = split_strong(events)
    strong.sort(key=_sort_key, reverse=True)
    routine.sort(key=_sort_key, reverse=True)

    out: list[str] = []
    out.append(f"## Ackman Tracker — 파이프라인 실행 요약")
    out.append("")
    out.append(f"`{ENTITY_NAME}` · 생성 {_now()}")
    out.append("")

    # --- STRONG 을 맨 위에
    if strong:
        out.append(f"### 🔴 고확신 변화 {len(strong)}건 (STRONG)")
        out.append("")
        out.extend(_event_table(strong))
        out.append("")
    else:
        out.append("### 고확신 변화 없음")
        out.append("")
        out.append("이번 실행에서 `STRONG_*` / `FULL_EXIT` 이벤트는 감지되지 않았습니다.")
        out.append("")

    # --- 분기 지표
    out.append("### 최신 분기 지표")
    out.append("")
    out.extend(_metrics_block(m))
    out.append("")

    # --- 나머지 이벤트
    out.append(f"### 그 외 변화 {len(routine)}건")
    out.append("")
    if routine:
        shown = routine[:MAX_EVENT_ROWS]
        out.extend(_event_table(shown, with_conviction=False))
        if len(routine) > len(shown):
            out.append("")
            out.append(f"_… 외 {len(routine) - len(shown)}건 생략._")
    else:
        out.append("_없음._")
    out.append("")

    out.append("---")
    out.append("")
    out.append(f"> {DISCLAIMER}")
    return "\n".join(out) + "\n"


def write_summary(markdown: str) -> str:
    """$GITHUB_STEP_SUMMARY 에 append. 없으면(로컬) stdout 으로 출력."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown)
            if not markdown.endswith("\n"):
                f.write("\n")
        return path
    sys.stdout.write(markdown)
    if not markdown.endswith("\n"):
        sys.stdout.write("\n")
    return "(stdout)"


# ---------------------------------------------------------------- Issue 본문

def _run_link() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return "(로컬 실행)"


def strong_issue_title(events: list[dict]) -> str:
    """분기별로 결정적인 제목. 같은 분기를 여러 번 처리해도 제목이 같아
    워크플로우의 중복 방지 조회에 걸린다."""
    strong, _ = split_strong(events)
    if not strong:
        return ""
    dates = sorted({str(e.get("report_date") or "") for e in strong})
    tag = dates[-1] or "unknown"
    return f"[STRONG] {tag} 고확신 변화 {len(strong)}건"


def render_strong_issue(events: list[dict], metrics=None) -> str:
    strong, _ = split_strong(events)
    strong.sort(key=_sort_key, reverse=True)
    m = _latest_metrics(metrics)
    out = [
        f"`{ENTITY_NAME}` 의 공시에서 고확신 변화가 감지되었습니다.",
        "",
        f"- 감지 시각: {_now()}",
        f"- 워크플로우 실행: {_run_link()}",
        "",
        "### 감지된 변화",
        "",
    ]
    out.extend(_event_table(strong))
    if m:
        out += ["", "### 해당 분기 지표", ""] + _metrics_block(m)
    out += [
        "",
        "이 이슈는 파이프라인이 자동 생성했습니다. 확인 후 닫으면 같은 분기에 대해",
        "다시 생성되지 않습니다.",
        "",
        "---",
        "",
        f"> {DISCLAIMER}",
    ]
    return "\n".join(out) + "\n"


def failure_issue_title(workflow: str | None = None) -> str:
    wf = workflow or os.environ.get("GITHUB_WORKFLOW") or "pipeline"
    return f"[FAILURE] {wf} 워크플로우 실패"


def render_failure_issue(
    violations: list[str] | None = None,
    stage: str | None = None,
    detail: str | None = None,
) -> str:
    out = [
        f"파이프라인이 실패했습니다. 데이터는 커밋되지 않았습니다.",
        "",
        f"- 워크플로우: `{os.environ.get('GITHUB_WORKFLOW', '(local)')}`",
        f"- 실행 링크: {_run_link()}",
        f"- 실패 시각: {_now()}",
    ]
    if stage:
        out.append(f"- 실패 단계: `{stage}`")
    out.append("")
    if violations:
        out += [f"### 무결성 게이트 위반 {len(violations)}건", ""]
        out += [f"- `{v}`" for v in violations]
        out.append("")
        out += [
            "게이트 코드 의미는 `src/pipeline/gate.py` 상단 주석을 참조하세요.",
            "",
        ]
    if detail:
        out += ["### 상세", "", "```", detail.strip()[:4000], "```", ""]
    out += [
        "### 다음 조치",
        "",
        "1. 위 실행 링크에서 로그를 확인합니다.",
        "2. 원인을 고친 뒤 `workflow_dispatch` 로 재실행합니다.",
        "3. `data/state/last_seen.json` 은 게이트 통과 시에만 갱신되므로,",
        "   실패한 accession 은 다음 실행에서 자동으로 재시도됩니다.",
        "",
        "동일 제목의 열린 이슈가 있으면 새 이슈 대신 코멘트가 달립니다.",
    ]
    return "\n".join(out) + "\n"


def filings_issue_title(count: int, tag: str) -> str:
    return f"[공시 감지] {tag} 신규 파일링 {count}건"


def render_filings_issue(filings: list[dict]) -> str:
    from src.common.schema import ARCHIVE_BASE

    out = [
        f"`{ENTITY_NAME}` 의 신규 공시가 감지되었습니다 "
        "(13F 분기 스냅샷보다 훨씬 빠른 채널입니다).",
        "",
        f"- 감지 시각: {_now()}",
        f"- 워크플로우 실행: {_run_link()}",
        "",
        "| 폼 | 제출일 | Accession | 원문 |",
        "|---|---|---|---|",
    ]
    for f in filings:
        acc = str(f.get("accession") or "")
        url = f.get("url") or f"{ARCHIVE_BASE}/{acc.replace('-', '')}/"
        out.append(
            f"| `{_md_escape(f.get('form_type'))}` | {_md_escape(f.get('filing_date'))} "
            f"| `{_md_escape(acc)}` | [EDGAR]({url}) |"
        )
    out += [
        "",
        "13D/G 는 취득 후 5영업일(정정 2영업일), Form 4 는 거래 후 2영업일 내",
        "제출됩니다. 13F 는 45일 지연이므로 이 이슈가 먼저 도착합니다.",
        "",
        "---",
        "",
        f"> {DISCLAIMER}",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 데모

def _demo_events() -> list[dict]:
    return [
        dict(event_id="2026-03-31:42806J700:NEW", report_date="2026-03-31",
             filing_date="2026-05-15", event_type="NEW", conviction="STRONG_NEW",
             cusip="42806J700", ticker="HTZ", issuer_name="HERTZ GLOBAL HOLDINGS INC",
             prev_shares=0, curr_shares=15_241_127, share_delta_pct=None,
             prev_value_usd=0, curr_value_usd=1_070_261_595,
             value_delta_usd=1_070_261_595, weight_before=0.0, weight_after=7.8,
             weight_delta_bp=780.0, accession="0001172661-26-002336"),
        dict(event_id="2026-03-31:11271J107:ADD", report_date="2026-03-31",
             filing_date="2026-05-15", event_type="ADD", conviction="STRONG_ADD",
             cusip="11271J107", ticker="BN", issuer_name="BROOKFIELD CORP",
             prev_shares=40_000_000, curr_shares=59_697_208, share_delta_pct=49.2,
             prev_value_usd=1_500_000_000, curr_value_usd=2_415_946_008,
             value_delta_usd=915_946_008, weight_before=13.1, weight_after=17.6,
             weight_delta_bp=450.0, accession="0001172661-26-002336"),
        dict(event_id="2026-03-31:812215200:EXIT", report_date="2026-03-31",
             filing_date="2026-05-15", event_type="EXIT", conviction="FULL_EXIT",
             cusip="812215200", ticker="SEG", issuer_name="SEAPORT ENTERTAINMENT GROUP",
             prev_shares=5_023_780, curr_shares=0, share_delta_pct=-100.0,
             prev_value_usd=107_910_794, curr_value_usd=0,
             value_delta_usd=-107_910_794, weight_before=0.9, weight_after=0.0,
             weight_delta_bp=-90.0, accession="0001172661-26-002336"),
        dict(event_id="2026-03-31:023135106:TRIM", report_date="2026-03-31",
             filing_date="2026-05-15", event_type="TRIM", conviction="ROUTINE",
             cusip="023135106", ticker="AMZN", issuer_name="AMAZON COM INC",
             prev_shares=12_000_000, curr_shares=11_451_981, share_delta_pct=-4.6,
             prev_value_usd=2_500_000_000, curr_value_usd=2_385_104_083,
             value_delta_usd=-114_895_917, weight_before=18.2, weight_after=17.4,
             weight_delta_bp=-80.0, accession="0001172661-26-002336"),
        dict(event_id="2026-03-31:02079K107:HOLD", report_date="2026-03-31",
             filing_date="2026-05-15", event_type="HOLD", conviction="ROUTINE",
             cusip="02079K107", ticker="GOOG", issuer_name="ALPHABET INC CL C",
             prev_shares=311_726, curr_shares=311_726, share_delta_pct=0.0,
             prev_value_usd=85_000_000, curr_value_usd=89_421_720,
             value_delta_usd=4_421_720, weight_before=0.6, weight_after=0.7,
             weight_delta_bp=10.0, accession="0001172661-26-002336"),
    ]


def _demo_metrics() -> dict:
    return dict(report_date="2026-03-31", filing_date="2026-05-15", lag_days=45,
                total_value_usd=13_714_299_861, position_count=11, hhi=0.1428,
                top1_pct=17.6, top3_pct=50.7, top5_pct=78.2, turnover_pct=12.4,
                new_count=1, exit_count=1, add_count=1, trim_count=1)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.pipeline.notify",
        description="파이프라인 알림 마크다운 생성 (Step Summary / Issue 본문)",
    )
    p.add_argument("--demo", action="store_true",
                   help="합성 이벤트로 전체 출력물을 렌더해 검증한다")
    p.add_argument("--from-disk", action="store_true",
                   help="data/normalized 의 events/metrics 로 Step Summary 생성")
    p.add_argument("--kind", choices=["summary", "strong", "failure", "filings"],
                   default="summary", help="--demo 와 함께 쓸 출력 종류")
    args = p.parse_args(argv)

    if args.from_disk:
        from src.common.schema import Paths, read_jsonl
        md = emit_summary(read_jsonl(Paths.EVENTS), read_jsonl(Paths.METRICS))
        write_summary(md)
        return 0

    if not args.demo:
        p.print_help()
        return 0

    ev, me = _demo_events(), _demo_metrics()
    if args.kind == "summary":
        out = emit_summary(ev, me)
    elif args.kind == "strong":
        print(f"# 제목: {strong_issue_title(ev)}\n")
        out = render_strong_issue(ev, me)
    elif args.kind == "failure":
        print(f"# 제목: {failure_issue_title('fetch-13f')}\n")
        out = render_failure_issue(
            violations=[
                "[TOTAL_SWING] 총액 변동 -94.0% 가 허용치 ±80% 초과",
                "[WEIGHT_SUM] 2026-03-31 weight_pct 합계 88.3333 가 100 ± 0.5 범위 밖",
            ],
            stage="integrity_gate",
        )
    else:
        f = [dict(accession="0001172661-26-002500", form_type="SC 13D/A",
                  filing_date="2026-08-01"),
             dict(accession="0001172661-26-002501", form_type="4",
                  filing_date="2026-08-01")]
        print(f"# 제목: {filings_issue_title(len(f), '2026-08-02')}\n")
        out = render_filings_issue(f)
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
