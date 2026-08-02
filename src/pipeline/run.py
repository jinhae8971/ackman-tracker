"""파이프라인 오케스트레이터.

    python -m src.pipeline.run --mode {incremental|backfill|events-only}

이 모듈은 **로직을 갖지 않는다.** collector / analytics 의 CLI 진입점을 호출해
조립하고, 결과를 무결성 게이트로 검증하고, 상태(`data/state/{entity}.json`)를
갱신한다. 파싱·diff·지표 계산은 전부 다른 모듈의 책임이다.
추적 대상은 `--entity {pershing|berkshire|citadel}` 로 고른다.

설계 원칙
---------
멱등성   : 처리 완료한 accession 집합을 상태 파일에 유지하고 신규만 처리한다.
           신규가 없으면 즉시 exit 0 + `::notice::`.
게이트   : `gate.integrity_gate()` 가 위반을 하나라도 반환하면 상태를 갱신하지
           않고 exit 1 한다. 워크플로우는 이 exit code 로 커밋을 막는다.
부분 실패: 한 파일링이 실패해도 전체가 멈추지 않는다. 정규화 산출물에 실제로
           반영된 accession 만 `accessions` 로 승격하고, 나머지는 `failed` 맵에
           남겨 다음 실행에서 자동 재시도한다.
미완성 모듈: collector / analytics / dashboard 가 아직 없어도 크래시하지 않고
           어느 단계가 준비되지 않았는지 명확히 보고한 뒤 exit 0 한다.
           나중에 모듈이 생기면 이 파일을 고치지 않아도 그대로 동작한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    from src.common import entities, schema
    from src.common.schema import Paths, load_json, read_jsonl, save_json
except ImportError:  # 직접 실행
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    from src.common import entities, schema
    from src.common.schema import Paths, load_json, read_jsonl, save_json

from src.pipeline import notify
from src.pipeline.gate import format_violations, integrity_gate

MODES = ("incremental", "backfill", "events-only")

FORMS_13F = ("13F-HR", "13F-HR/A")
FORMS_FAST = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "3", "4")

# 다른 에이전트가 만드는 진입점 (specs/CONTRACTS.md)
STAGES = {
    "collector": ("src.collector.backfill", [sys.executable, "-m", "src.collector.backfill"]),
    "analytics": ("src.analytics.build_events", [sys.executable, "-m", "src.analytics.build_events"]),
    "dashboard": (None, [sys.executable, os.path.join("dashboard", "build.py")]),
}

EXIT_OK = 0
EXIT_GATE_FAILED = 1


# ---------------------------------------------------------------- 로그 유틸

def notice(msg: str) -> None:
    print(f"::notice::{msg}", flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}", flush=True)


def error(msg: str) -> None:
    print(f"::error::{msg}", flush=True)


def log(msg: str) -> None:
    print(msg, flush=True)


def group(title: str) -> None:
    print(f"::group::{title}", flush=True)


def endgroup() -> None:
    print("::endgroup::", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_output(**kwargs) -> None:
    """GitHub Actions 스텝 출력. 로컬이면 표준출력에만 남긴다."""
    path = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for k, v in kwargs.items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        v = str(v)
        if "\n" in v:
            lines.append(f"{k}<<__EOF__\n{v}\n__EOF__")
        else:
            lines.append(f"{k}={v}")
    payload = "\n".join(lines)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(payload + "\n")
    else:
        log(f"[output] {payload}")


# ---------------------------------------------------------------- 단계 탐지

def stage_available(name: str) -> tuple[bool, str]:
    """다른 에이전트의 모듈이 준비됐는지 확인 (import 부작용 없이)."""
    module, cmd = STAGES[name]
    if module is None:                      # 파일 기반 진입점 (dashboard/build.py)
        path = os.path.join(Paths.ROOT, cmd[-1])
        return (os.path.exists(path), path)
    try:
        # find_spec 은 부모 패키지를 import 하므로 임의의 예외가 올라올 수 있다.
        # 다른 에이전트의 미완성 코드가 파이프라인을 죽이지 않도록 전부 흡수한다.
        spec = importlib.util.find_spec(module)
    except Exception as exc:  # noqa: BLE001
        return (False, f"{module} ({type(exc).__name__}: {exc})")
    return (spec is not None, module)


def report_stages() -> dict[str, bool]:
    group("파이프라인 단계 가용성")
    status = {}
    for name in ("collector", "analytics", "dashboard"):
        ok, detail = stage_available(name)
        status[name] = ok
        log(f"  {name:<10} : {'READY    ' if ok else 'NOT READY'}  ({detail})")
    endgroup()
    missing = [n for n, ok in status.items() if not ok]
    if missing:
        warn(
            "아직 준비되지 않은 단계: " + ", ".join(missing)
            + " — 해당 단계는 건너뜁니다 (모듈이 추가되면 코드 수정 없이 동작)."
        )
    return status


def run_stage(name: str, extra_args: list[str] | None = None,
              dry_run: bool = False) -> tuple[bool, str]:
    """다른 모듈의 CLI 를 서브프로세스로 실행. (성공여부, 출력) 반환."""
    _, cmd = STAGES[name]
    cmd = list(cmd) + list(extra_args or [])
    printable = " ".join(cmd[1:])
    if dry_run:
        log(f"  [dry-run] {printable}")
        return True, "(dry-run)"
    group(f"{name} 실행 — {printable}")
    try:
        proc = subprocess.run(
            cmd, cwd=Paths.ROOT, text=True, capture_output=True,
            env={**os.environ, "PYTHONPATH": Paths.ROOT},
        )
    except OSError as exc:
        endgroup()
        error(f"{name} 실행 불가: {exc}")
        return False, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    log(out.rstrip()[-20000:])
    endgroup()
    if proc.returncode != 0:
        warn(f"{name} 가 exit {proc.returncode} 로 종료 — 부분 실패로 격리하고 계속 진행")
        return False, out[-4000:]
    return True, out[-4000:]


# ---------------------------------------------------------------- 상태

DEFAULT_STATE = {"accessions": [], "last_run": None, "failed": {}, "alerted": []}


def load_state() -> dict:
    state = load_json(Paths.LAST_SEEN, default=None) or {}
    merged = dict(DEFAULT_STATE)
    merged.update({k: v for k, v in state.items() if v is not None})
    merged["accessions"] = sorted(set(merged.get("accessions") or []))
    merged["alerted"] = sorted(set(merged.get("alerted") or []))
    merged["failed"] = dict(merged.get("failed") or {})
    return merged


def save_state(state: dict, dry_run: bool = False) -> None:
    state["accessions"] = sorted(set(state.get("accessions") or []))
    state["alerted"] = sorted(set(state.get("alerted") or []))
    state["last_run"] = now_iso()
    if dry_run:
        log("  [dry-run] 상태 파일을 쓰지 않습니다.")
        return
    save_json(Paths.LAST_SEEN, state)
    log(f"  상태 저장: {Paths.LAST_SEEN} "
        f"(accessions={len(state['accessions'])}, failed={len(state['failed'])}, "
        f"alerted={len(state['alerted'])})")


# ---------------------------------------------------------------- 공시 탐지

def _as_dict(f) -> dict:
    if isinstance(f, dict):
        return f
    return {
        "accession": getattr(f, "accession", None),
        "form_type": getattr(f, "form_type", None),
        "filing_date": getattr(f, "filing_date", None),
        "report_date": getattr(f, "report_date", None),
        "url": getattr(f, "url", None),
    }


def discover_filings(forms: tuple[str, ...]) -> list[dict] | None:
    """collector 를 통해 EDGAR 제출 이력을 조회한다.

    collector 가 아직 없거나 호출이 실패하면 None 을 반환한다 (하드 실패 아님).
    """
    wanted = {f.strip().upper() for f in forms if f.strip()}
    try:
        from src.collector import edgar  # type: ignore
    except Exception as exc:              # noqa: BLE001 — 미구현/임포트 오류 모두 흡수
        warn(f"collector.edgar 를 불러올 수 없습니다 ({type(exc).__name__}: {exc})")
        return None

    try:
        if hasattr(edgar, "list_filings"):
            raw = edgar.list_filings(sorted(wanted) or None)
            rows = [_as_dict(f) for f in raw]
        elif hasattr(edgar, "fetch_submissions"):
            rows = _flatten_submissions(edgar.fetch_submissions())
        else:
            warn("collector.edgar 에 list_filings / fetch_submissions 가 없습니다.")
            return None
    except Exception as exc:              # noqa: BLE001
        error(f"EDGAR 조회 실패 ({type(exc).__name__}: {exc})")
        return None

    rows = [r for r in rows if r.get("accession")]
    if wanted:
        rows = [r for r in rows if str(r.get("form_type", "")).upper() in wanted]
    rows.sort(key=lambda r: (str(r.get("filing_date") or ""), str(r["accession"])))
    return rows


def _flatten_submissions(sub: dict) -> list[dict]:
    """submissions API 원본 JSON -> 평탄한 파일링 목록 (fallback 경로)."""
    out = []
    blocks = []
    recent = (sub.get("filings") or {}).get("recent")
    if recent:
        blocks.append(recent)
    for b in blocks:
        n = len(b.get("accessionNumber", []))
        for i in range(n):
            out.append({
                "accession": b["accessionNumber"][i],
                "form_type": b.get("form", [None] * n)[i],
                "filing_date": b.get("filingDate", [None] * n)[i],
                "report_date": b.get("reportDate", [None] * n)[i] or None,
                "url": None,
            })
    return out


def processed_accessions() -> set[str]:
    """정규화 산출물에 실제로 반영된 accession 집합 (부분 실패 판정용)."""
    seen = set()
    for path in (Paths.FILINGS, Paths.HOLDINGS):
        for row in read_jsonl(path):
            acc = row.get("accession")
            if acc:
                seen.add(acc)
    return seen


# ---------------------------------------------------------------- 알림 파일

def write_alert(path: str | None, title: str, body: str) -> None:
    """워크플로우가 읽어 Issue 로 올릴 본문을 파일로 남긴다."""
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    log(f"  알림 본문 기록: {path} (제목: {title})")


# ---------------------------------------------------------------- 메인

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.pipeline.run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Ackman Tracker 파이프라인 오케스트레이터.\n"
            "collector -> analytics -> 무결성 게이트 -> 상태 갱신 순으로 조립한다."
        ),
        epilog=(
            "예시:\n"
            "  python -m src.pipeline.run --mode incremental\n"
            "  python -m src.pipeline.run --mode backfill --limit 5\n"
            "  python -m src.pipeline.run --mode events-only\n"
            "  python -m src.pipeline.run --detect-only --forms 'SC 13D,SC 13D/A,4'\n"
            "\n종료 코드: 0 정상/대기, 1 무결성 게이트 실패(커밋 금지)\n"
        ),
    )
    p.add_argument("--mode", choices=MODES, default="incremental",
                   help="incremental: 신규 accession 만 처리(기본). "
                        "backfill: 전량 재수집. events-only: 수집 없이 분석만.")
    p.add_argument("--limit", type=int, default=None,
                   help="backfill 시 collector 에 넘길 파일링 개수 상한")
    p.add_argument("--forms", default=None,
                   help="쉼표로 구분한 폼 타입 필터 "
                        f"(기본 13F: {','.join(FORMS_13F)})")
    p.add_argument("--detect-only", action="store_true",
                   help="신규 공시 감지만 하고 수집·분석은 하지 않는다 "
                        "(poll-daily 워크플로우용)")
    p.add_argument("--skip-analytics", action="store_true",
                   help="analytics 단계를 건너뛴다")
    p.add_argument("--skip-gate", action="store_true",
                   help="무결성 게이트를 건너뛴다 (디버깅 전용, 워크플로우에서 사용 금지)")
    p.add_argument("--alert-window-days", type=int, default=14,
                   help="detect-only 모드에서 알림을 낼 최대 공시 경과일. "
                        "상태가 유실돼도 과거 공시가 대량 재알림되지 않도록 막는다. "
                        "0 이면 창을 적용하지 않는다.")
    p.add_argument("--alert-file", default=None,
                   help="Issue 본문을 기록할 파일 경로")
    p.add_argument("--entity", default=None,
                   help=f"추적 대상 엔티티 ({', '.join(entities.ORDER)}). "
                        f"기본 {entities.DEFAULT_KEY}.")
    p.add_argument("--dry-run", action="store_true",
                   help="상태 파일과 외부 명령 실행 없이 계획만 출력")
    return p


def _rebind_entity(argv) -> None:
    """`--entity` 가 현재 활성 엔티티와 다르면 환경변수를 세우고 재실행한다.

    schema.Paths 는 임포트 시점에 확정되므로 import 이후에 환경변수를 바꿔도
    경로가 따라오지 않는다. 인자를 40여 개 함수에 관통시키는 대신 프로세스를
    한 번 갈아끼우는 쪽이 훨씬 적은 표면적으로 같은 보장을 준다. 하위 단계는
    서브프로세스라 환경변수를 그대로 물려받는다.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    want = None
    for i, a in enumerate(argv):
        if a == "--entity" and i + 1 < len(argv):
            want = argv[i + 1]
        elif a.startswith("--entity="):
            want = a.split("=", 1)[1]
    if not want:
        return
    want = entities.get(want).key           # 오타는 여기서 즉시 실패
    if want == schema.ENTITY_KEY:
        return
    os.environ[entities.ENV_VAR] = want
    os.execv(sys.executable,
             [sys.executable, "-m", "src.pipeline.run"] + argv)


def main(argv=None) -> int:
    _rebind_entity(argv)
    args = build_parser().parse_args(argv)

    forms = tuple(
        f.strip() for f in (args.forms.split(",") if args.forms else FORMS_13F)
        if f.strip()
    )

    log("=" * 72)
    log(f"13F Tracker 파이프라인 — entity={schema.ENTITY_KEY} "
        f"({schema.ENTITY_DISPLAY}) mode={args.mode} "
        f"forms={list(forms)} detect_only={args.detect_only} dry_run={args.dry_run}")
    log(f"repo root: {Paths.ROOT}")
    log(f"UTC      : {now_iso()}")
    log("=" * 72)

    stages = report_stages()
    state = load_state()
    seen = set(state["accessions"])
    alerted = set(state["alerted"])
    log(f"상태: 처리완료 {len(seen)}건 / 재시도 대기 {len(state['failed'])}건 / "
        f"알림완료 {len(alerted)}건 / 최근 실행 {state['last_run']}")

    new_filings: list[dict] = []
    collection_ran = False
    collector_output = ""

    # ---------------------------------------------------------- 1) 신규 판정
    if args.mode != "events-only":
        if not stages["collector"]:
            warn("collector 미구현 — 수집 단계를 건너뜁니다. "
                 "기존 산출물이 있으면 분석·게이트만 수행합니다.")
        else:
            filings = discover_filings(forms)
            if filings is None:
                warn("EDGAR 조회 결과를 얻지 못해 수집 단계를 건너뜁니다.")
            else:
                baseline = (seen | alerted) if args.detect_only else seen
                new_filings = [f for f in filings if f["accession"] not in baseline]
                log(f"EDGAR 조회: 대상 폼 {len(filings)}건 중 신규 {len(new_filings)}건")
                for f in new_filings[:20]:
                    log(f"  + {f.get('filing_date')}  "
                        f"{str(f.get('form_type')):<10} {f['accession']}")
                if len(new_filings) > 20:
                    log(f"  … 외 {len(new_filings) - 20}건")

                if not new_filings and args.mode != "backfill":
                    notice("No new filings — 신규 accession 이 없어 즉시 종료합니다.")
                    set_output(status="no_new", new_filings=0, strong_events=0,
                               gate="skipped", alert="false")
                    if not args.dry_run:
                        save_state(state)
                    return EXIT_OK

                # ------------------------------ detect-only (poll-daily)
                if args.detect_only:
                    if not new_filings:
                        notice("No new filings — 감지할 신규 공시가 없습니다.")
                        set_output(status="no_new", new_filings=0, strong_events=0,
                                   gate="skipped", alert="false")
                        save_state(state, args.dry_run)
                        return EXIT_OK

                    # 콜드스타트: `alerted` 가 비어 있으면 이번이 첫 폴링이다.
                    # 이때 감지된 것은 "신규"가 아니라 과거 이력 전체이므로
                    # 알리지 않고 기준선으로만 적재한다. 이 처리가 없으면 첫
                    # 실행에서 수백 건짜리 Issue 가 생기고, 상태 저장이 실패하면
                    # 매일 같은 Issue 가 반복 생성된다.
                    if not alerted:
                        state["alerted"] = sorted(
                            {f["accession"] for f in filings})
                        save_state(state, args.dry_run)
                        notice(f"콜드스타트 — 기존 공시 {len(filings)}건을 "
                               f"기준선으로 적재했습니다(알림 없음). "
                               f"다음 실행부터 신규만 알립니다.")
                        set_output(status="seeded", new_filings=0,
                                   strong_events=0, gate="skipped",
                                   alert="false")
                        return EXIT_OK

                    # 최근성 창: 상태가 부분 유실돼도 오래된 공시가 대량으로
                    # 재알림되는 것을 막는다. 창을 벗어난 건은 조용히 기준선에
                    # 편입한다.
                    if args.alert_window_days > 0:
                        cutoff = (datetime.now(timezone.utc)
                                  - timedelta(days=args.alert_window_days)
                                  ).strftime("%Y-%m-%d")
                        fresh = [f for f in new_filings
                                 if str(f.get("filing_date") or "") >= cutoff]
                        stale = len(new_filings) - len(fresh)
                        if stale:
                            log(f"  최근성 창({args.alert_window_days}일) 밖 "
                                f"{stale}건은 알리지 않고 기준선에 편입합니다.")
                        if not fresh:
                            state["alerted"] = sorted(
                                alerted | {f["accession"] for f in new_filings})
                            save_state(state, args.dry_run)
                            notice("창 안에 드는 신규 공시가 없습니다.")
                            set_output(status="no_new", new_filings=0,
                                       strong_events=0, gate="skipped",
                                       alert="false")
                            return EXIT_OK
                        # 창 밖 건도 기준선에는 넣어 다음 실행에서 다시 뜨지 않게 한다.
                        alerted = alerted | {
                            f["accession"] for f in new_filings
                            if f not in fresh}
                        new_filings = fresh
                    tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    title = notify.filings_issue_title(len(new_filings), tag)
                    write_alert(args.alert_file, title,
                                notify.render_filings_issue(new_filings))
                    notify.write_summary(
                        notify.render_filings_issue(new_filings))
                    state["alerted"] = sorted(
                        alerted | {f["accession"] for f in new_filings})
                    save_state(state, args.dry_run)
                    notice(f"신규 공시 {len(new_filings)}건 감지 (알림 대상)")
                    set_output(status="detected", new_filings=len(new_filings),
                               strong_events=0, gate="skipped", alert="true",
                               alert_title=title)
                    return EXIT_OK

                # ------------------------------ 수집 실행
                extra = []
                if args.limit:
                    extra += ["--limit", str(args.limit)]
                ok, collector_output = run_stage("collector", extra, args.dry_run)
                collection_ran = True
                if not ok:
                    warn("collector 부분 실패 — 반영된 파일링만 승격하고 나머지는 "
                         "failed 로 남깁니다.")

    # ---------------------------------------------------------- 2) 분석
    analytics_ran = False
    if args.skip_analytics:
        log("analytics 단계를 --skip-analytics 로 건너뜁니다.")
    elif not stages["analytics"]:
        warn("analytics 미구현 — 이벤트/지표 생성을 건너뜁니다.")
    else:
        ok, _ = run_stage("analytics", None, args.dry_run)
        analytics_ran = True
        if not ok:
            warn("analytics 실패 — 기존 events/metrics 로 게이트를 진행합니다.")

    # ---------------------------------------------------------- 3) 게이트
    holdings = read_jsonl(Paths.HOLDINGS)
    metrics = read_jsonl(Paths.METRICS)
    events = read_jsonl(Paths.EVENTS)
    log(f"산출물: holdings {len(holdings)}행 / events {len(events)}행 / "
        f"metrics {len(metrics)}행")

    if not holdings and not (collection_ran or analytics_ran):
        notice(
            "부트스트랩 대기 — 수집·분석 단계가 아직 준비되지 않았고 정규화 "
            "데이터도 없습니다. 커밋할 것이 없으므로 정상 종료합니다."
        )
        set_output(status="pending", new_filings=len(new_filings),
                   strong_events=0, gate="skipped", alert="false")
        if not args.dry_run:
            save_state(state)
        return EXIT_OK

    if args.skip_gate:
        warn("--skip-gate: 무결성 게이트를 건너뜁니다 (디버깅 전용).")
        violations = []
    else:
        group("무결성 게이트")
        violations = integrity_gate(holdings, metrics, events)
        log(format_violations(violations, annotate=False))
        endgroup()

    if violations:
        for v in violations:
            error(v)
        error(f"무결성 게이트 실패 {len(violations)}건 — 상태를 갱신하지 않고 "
              "종료합니다. 데이터를 커밋하지 마십시오.")
        title = notify.failure_issue_title()
        body = notify.render_failure_issue(violations=violations,
                                           stage="integrity_gate",
                                           detail=collector_output or None)
        write_alert(args.alert_file, title, body)
        notify.write_summary(
            "## ❌ 무결성 게이트 실패\n\n"
            + "\n".join(f"- `{v}`" for v in violations)
            + "\n\n데이터는 커밋되지 않았습니다.\n"
        )
        set_output(status="gate_failed", new_filings=len(new_filings),
                   strong_events=0, gate="fail", alert="true", alert_title=title)
        return EXIT_GATE_FAILED

    # ---------------------------------------------------------- 4) 상태 갱신
    if new_filings:
        done = processed_accessions()
        promoted = [f["accession"] for f in new_filings if f["accession"] in done]
        still = [f for f in new_filings if f["accession"] not in done]
        state["accessions"] = sorted(seen | set(promoted))
        for acc in promoted:
            state["failed"].pop(acc, None)
        for f in still:
            prev = state["failed"].get(f["accession"], {})
            state["failed"][f["accession"]] = {
                "form_type": f.get("form_type"),
                "filing_date": f.get("filing_date"),
                "attempts": int(prev.get("attempts", 0)) + 1,
                "last_attempt": now_iso(),
                "error": "정규화 산출물에 반영되지 않음 (collector 부분 실패)",
            }
        log(f"승격 {len(promoted)}건 / 재시도 대기 {len(still)}건")
        if still:
            warn(f"{len(still)}건이 반영되지 않아 다음 실행에서 재시도합니다: "
                 + ", ".join(f['accession'] for f in still[:10]))
    save_state(state, args.dry_run)

    # ---------------------------------------------------------- 5) 요약·알림
    # 알림 대상은 최신 분기로 한정한다. 전체 이력을 넘기면 백필/재빌드 때마다
    # 과거 STRONG 이벤트 전량(135건)이 재알림되어 Issue 가 무의미해진다.
    latest_rd = max(
        (str(e.get("report_date") or "") for e in events), default="")
    alert_events = [e for e in events if str(e.get("report_date") or "") == latest_rd]
    strong, _rest = notify.split_strong(alert_events)
    summary = notify.emit_summary(events, metrics)
    notify.write_summary(summary)

    alert = "false"
    title = ""
    if strong:
        title = notify.strong_issue_title(alert_events)
        write_alert(args.alert_file, title,
                    notify.render_strong_issue(alert_events, metrics))
        alert = "true"
        notice(f"STRONG 이벤트 {len(strong)}건 — Issue 알림 대상")

    set_output(status="ok", new_filings=len(new_filings),
               strong_events=len(strong), gate="pass",
               alert=alert, alert_title=title)
    notice(
        f"완료 — 신규 {len(new_filings)}건 처리, holdings {len(holdings)}행, "
        f"events {len(events)}행, STRONG {len(strong)}건, 게이트 통과"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
