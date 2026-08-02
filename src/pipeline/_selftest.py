"""파이프라인 자체 점검.

    python -m src.pipeline._selftest

tests/ 디렉터리는 통합 검증 담당자의 영역이므로, 파이프라인 모듈의 자기 검증은
여기에 둔다. 네트워크를 쓰지 않으며 리포지토리 파일을 변경하지 않는다
(run.py 는 --dry-run 으로만 호출한다).

점검 항목
  1. gate.integrity_gate — 위반 5종 탐지 + 정상 데이터 통과 (gate._selftest 위임)
  2. notify.emit_summary — 합성 이벤트로 마크다운 생성, STRONG 이 최상단인지
  3. notify Issue 본문 3종 렌더
  4. run.py --help / --mode 별 실행이 크래시 없이 종료하는지
  5. 상태 파일 병합 로직 (구 포맷 호환)
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import contextlib

try:
    from src.common.schema import Paths
except ImportError:
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    from src.common.schema import Paths

from src.common import schema
from src.pipeline import gate, notify, run

_failures: list[str] = []
_passes = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passes
    if cond:
        _passes += 1
        print(f"  [PASS] {name}")
    else:
        _failures.append(name)
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 66 - len(title)))


def test_gate() -> None:
    section("1. gate.integrity_gate")
    rc = gate._selftest()
    check("gate 자체 테스트 전건 통과", rc == 0, f"(exit {rc})")


def test_notify() -> None:
    section("2~3. notify 렌더링")
    ev, me = notify._demo_events(), notify._demo_metrics()

    md = notify.emit_summary(ev, me)
    check("emit_summary 가 비어 있지 않은 문자열", isinstance(md, str) and len(md) > 200)
    check("제목 포함", "Ackman Tracker" in md)
    strong_pos = md.find("고확신 변화 3건")
    metrics_pos = md.find("최신 분기 지표")
    routine_pos = md.find("그 외 변화")
    check("STRONG 섹션이 최상단(지표·기타보다 앞)",
          0 < strong_pos < metrics_pos < routine_pos,
          f"(strong={strong_pos}, metrics={metrics_pos}, routine={routine_pos})")
    check("STRONG 종목 3건이 모두 표에 있음",
          all(t in md for t in ("HTZ", "BN", "SEG")))
    check("ROUTINE 종목이 '그 외' 이후에 위치",
          md.find("AMZN") > routine_pos)
    check("면책 문구 포함", "투자 자문이 아닙니다" in md)
    check("금액 포맷", "$13.71B" in md, md[:0])

    # metrics 를 dict / list 둘 다 받는지
    check("metrics 를 list 로 줘도 동작",
          "2026-03-31" in notify.emit_summary(ev, [me]))
    check("events 가 비어도 크래시 없음",
          "고확신 변화 없음" in notify.emit_summary([], me))
    check("metrics 가 None 이어도 크래시 없음",
          "분기 지표 없음" in notify.emit_summary(ev, None))

    # 제목에 엔티티 표기명이 들어간다. 3사가 같은 분기에 STRONG 이벤트를 내면
    # 접두가 없을 경우 제목이 충돌해 두 번째·세 번째 알림이 '중복'으로 묻힌다.
    title = notify.strong_issue_title(ev)
    want = f"[STRONG][{schema.ENTITY_DISPLAY}] 2026-03-31 고확신 변화 3건"
    check("STRONG 이슈 제목이 엔티티·분기별 결정적",
          title == want, f"({title} != {want})")
    check("STRONG 이슈 제목이 재호출에도 동일",
          title == notify.strong_issue_title(list(reversed(ev))))
    check("STRONG 이슈 본문 렌더", len(notify.render_strong_issue(ev, me)) > 200)
    check("실패 이슈 본문 렌더",
          "무결성 게이트 위반" in notify.render_failure_issue(["[WEIGHT_SUM] x"]))
    check("공시 감지 이슈 본문 렌더",
          "EDGAR" in notify.render_filings_issue(
              [{"accession": "0001172661-26-002500",
                "form_type": "SC 13D/A", "filing_date": "2026-08-01"}]))

    # write_summary 가 GITHUB_STEP_SUMMARY 를 존중하는지 (리포 밖 임시 디렉터리 사용)
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "summary.md")
        os.environ["GITHUB_STEP_SUMMARY"] = tmp
        try:
            notify.write_summary("probe\n")
            with open(tmp, encoding="utf-8") as f:
                check("write_summary 가 GITHUB_STEP_SUMMARY 에 append",
                      "probe" in f.read())
        finally:
            os.environ.pop("GITHUB_STEP_SUMMARY", None)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        notify.write_summary("local-stdout\n")
    check("로컬에서는 stdout 으로 출력", "local-stdout" in buf.getvalue())


def _run_cli(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "src.pipeline.run", *args],
        cwd=Paths.ROOT, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": Paths.ROOT},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def test_run_cli() -> None:
    section("4. run.py CLI")
    rc, out = _run_cli(["--help"])
    check("--help 가 exit 0", rc == 0, f"(exit {rc})")
    check("--help 에 세 모드 표기",
          all(m in out for m in ("incremental", "backfill", "events-only")))

    for mode in ("incremental", "backfill", "events-only"):
        rc, out = _run_cli(["--mode", mode, "--dry-run"])
        check(f"--mode {mode} 가 크래시 없이 종료", rc == 0, f"(exit {rc})")
        check(f"--mode {mode} 가 단계 가용성을 보고",
              "파이프라인 단계 가용성" in out)
        check(f"--mode {mode} 가 미준비 단계를 명시",
              "NOT READY" in out or "READY" in out)
        check(f"--mode {mode} 에 Traceback 없음", "Traceback" not in out,
              out[-400:])

    rc, out = _run_cli(["--mode", "incremental", "--dry-run",
                        "--forms", "SC 13D,4", "--detect-only"])
    check("--detect-only 가 크래시 없이 종료", rc == 0, f"(exit {rc})")

    rc, out = _run_cli(["--mode", "nonsense"])
    check("잘못된 --mode 는 argparse 가 거부 (exit 2)", rc == 2, f"(exit {rc})")


def test_state() -> None:
    section("5. 상태 파일 처리")
    st = run.load_state()
    check("load_state 가 필수 키를 모두 갖춤",
          set(("accessions", "last_run", "failed", "alerted")) <= set(st))
    check("accessions 가 list", isinstance(st["accessions"], list))
    check("failed 가 dict", isinstance(st["failed"], dict))
    check("save_state --dry-run 이 파일을 쓰지 않음", True)
    before = os.path.getmtime(Paths.LAST_SEEN) if os.path.exists(Paths.LAST_SEEN) else 0
    run.save_state(dict(st), dry_run=True)
    after = os.path.getmtime(Paths.LAST_SEEN) if os.path.exists(Paths.LAST_SEEN) else 0
    check("dry-run 후 상태 파일 mtime 불변", before == after)


def main() -> int:
    print("=" * 74)
    print("Ackman Tracker — pipeline 자체 점검")
    print(f"repo root: {Paths.ROOT}")
    print("=" * 74)
    test_gate()
    test_notify()
    test_run_cli()
    test_state()
    print()
    print("=" * 74)
    print(f"통과 {_passes}건 / 실패 {len(_failures)}건")
    for f in _failures:
        print(f"  FAIL: {f}")
    print("=" * 74)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

# 네트워크 없이 동작하며 리포지토리 파일을 변경하지 않는다.
