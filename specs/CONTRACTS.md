# 모듈 계약 (Module Contracts) — 확정본

이 문서는 병렬 개발의 인터페이스 계약이다. **각 에이전트는 자기 담당 디렉터리 밖의 파일을 수정하지 않는다.**
공유 계약은 `src/common/schema.py` 하나뿐이며, 이 파일은 오케스트레이터만 수정한다.

설계 근거 전문은 `docs/architecture.md` 참조.

---

## 담당 분할 (파일 충돌 방지)

| 모듈 | 담당 디렉터리 | 산출물 |
|---|---|---|
| A. Collector | `src/collector/` | EDGAR 수집·파싱·CUSIP 매핑 |
| B. Analytics | `src/analytics/` | diff, conviction, 포트폴리오 지표 |
| C. Pipeline | `src/pipeline/`, `.github/workflows/` | 오케스트레이션, 크론, 무결성 게이트 |
| D. Dashboard | `dashboard/` | 정적 HTML 대시보드, 빌드 스크립트 |
| 공유 | `src/common/schema.py` | **오케스트레이터 전용 — 수정 금지** |

---

## A. Collector — `src/collector/`

```python
# src/collector/edgar.py
def fetch_submissions() -> dict
    """data.sec.gov submissions API 원본 JSON."""

def list_filings(forms: list[str] | None = None) -> list[Filing]
    """submissions 를 Filing 레코드로 평탄화. forms 로 필터."""

def fetch_filing_files(accession: str) -> dict[str, bytes]
    """index.json 을 읽어 해당 파일링의 XML 을 모두 내려받고 data/raw/ 에 보존."""

# src/collector/parse_13f.py
def parse_13f(accession: str, filing_date: str, form_type: str) -> tuple[Filing, list[Holding]]
    """13F 파싱. 반드시:
       1) primary_doc 에서 schemaVersion / amendmentType / tableValueTotal / periodOfReport 추출
       2) value 를 schema.normalize_value 로 달러 정규화
       3) 2단 체크섬:
            3a) tableEntryTotal vs 파싱 행 수 — 불일치 시 ChecksumError 하드 실패.
                행 누락은 그 종목을 '전량청산' 이벤트로 둔갑시키므로 타협 없음.
            3b) tableValueTotal vs 정규화 이전 raw 합계 — 오차가 행 수를 넘으면
                하드 실패, 그 이내면 경고 후 통과. 제출인 자신의 반올림으로
                총액이 몇 달러 어긋나는 공시가 실재한다
                (0002045724-26-000002 은 정확히 $1 어긋난다).
       4) weight_pct 계산
       ticker/figi 는 None 으로 두고 map_cusips 가 채운다."""

def resolve_quarter(filings: list[tuple[Filing, list[Holding]]]) -> list[Holding]
    """한 report_date 의 원본+정정을 유효 포지션 집합으로 해석.
       filing_date 오름차순 정렬 후:
         amendment_type == 'RESTATEMENT' -> 누적 초기화 (전체 대체)
         amendment_type == 'NEW HOLDINGS' -> 병합 (기존 유지 + 신규 추가)
       병합 키는 Holding.key = (cusip, title_of_class).
       ★ 실측 반례: 0001172661-25-001497 은 NEW HOLDINGS 이며 Hertz 1종목뿐.
         덮어쓰기로 처리하면 2024Q4 가 11종목 $12.66B -> 1종목 $46.5M 로 붕괴한다.
       병합 후 weight_pct 를 재계산할 것."""

# src/collector/cusip_map.py
def map_cusips(cusips: list[str]) -> dict[str, dict]
    """OpenFIGI v3 로 CUSIP -> {ticker, figi, name, security_type}.
       data/reference/cusip_map.json 캐시 우선 조회, 미스만 API 호출.
       배치 최대 100건/요청, 무인증 시 분당 25요청 제한 준수.
       거래소 다중 레코드 중 exchCode == 'US' 이고 보통주/ADR 계열 우선 선택.
       실패한 CUSIP 은 캐시에 {"ticker": null, "unresolved": true} 로 기록 (재시도 폭주 방지)."""

# src/collector/backfill.py  (CLI)
#   python -m src.collector.backfill [--limit N] [--force]
#   전체 13F-HR/A 를 수집 -> resolve_quarter -> CUSIP 매핑
#   -> data/normalized/holdings.jsonl, filings.jsonl 생성
```

**출력 계약**: `holdings.jsonl` 은 `Holding` dataclass 필드를 그대로 가지며,
`(report_date, -value_usd)` 로 정렬되어 있어야 한다.

---

## B. Analytics — `src/analytics/`

Collector 출력을 **읽기만** 한다. 네트워크 호출 금지 (테스트 가능성 확보).

```python
# src/analytics/diff.py
def quarter_diff(prev: list[dict], curr: list[dict]) -> list[Event]
    """CUSIP+클래스 키로 outer join.
       NEW  : 이전 분기에 없음
       EXIT : 현재 분기에 없음 (curr_shares=0)
       ADD  : share_delta_pct >  +MIN_DELTA_PCT
       TRIM : share_delta_pct <  -MIN_DELTA_PCT
       HOLD : 그 외
       ★ 주식 분할 보정 필수: adjust_splits() 를 먼저 적용할 것."""

def adjust_splits(prev: list[dict], curr: list[dict]) -> list[dict]
    """주식 수 비율이 정수배(2,3,4,10...)에 ±2% 이내로 근접하고
       value 변동은 그에 비례하지 않는 경우 분할로 판정해 prev.shares 를 보정.
       보정 사실은 로그로 남기고 Event 에 반영하지 않는다."""

def classify_conviction(ev: Event) -> Conviction
    """STRONG_NEW  : NEW  and weight_after >= STRONG_NEW_WEIGHT_PCT
       STRONG_ADD  : ADD  and weight_delta_bp >= +STRONG_DELTA_BP
       STRONG_TRIM : TRIM and weight_delta_bp <= -STRONG_DELTA_BP
       FULL_EXIT   : EXIT
       ROUTINE     : 그 외"""

# src/analytics/metrics.py
def quarter_metrics(holdings: list[dict], events: list[dict],
                    prev: list[dict] | None) -> QuarterMetrics
    """HHI = Σ(weight/100)^2. top1/3/5 누적 비중.
       turnover_pct = Σ|value 변화| / 평균 포트폴리오 가치 * 100 (prev 없으면 None).
       lag_days = filing_date - report_date."""

def holding_periods(all_holdings: list[dict]) -> dict[str, dict]
    """CUSIP 별 {first_seen, last_seen, quarters_held, is_current}."""

# src/analytics/build_events.py  (CLI)
#   python -m src.analytics.build_events
#   holdings.jsonl 전체를 분기 순회하며 events.jsonl, metrics.jsonl 생성
#   event_id 는 schema.make_event_id 로 생성 — 재실행 시 동일 결과(멱등)
#   형식: {report_date}:{cusip}:{event_type}[:PUT|:CALL]
#   put_call 이 들어가는 이유: 같은 CUSIP 을 보통주와 PUT/CALL 로 동시에
#   보유하는 운용사가 있어(SA 의 NVDA, Pershing 의 2013년 PG) 접미가 없으면
#   한 분기에 같은 ID 가 중복 생성돼 게이트의 DUP_EVENT_ID 에 걸린다.
#   보통주는 접미가 붙지 않으므로 옵션을 안 든 엔티티의 기존 ID 는 불변이다.
```

---

## C. Pipeline — `src/pipeline/`, `.github/workflows/`

```python
# src/pipeline/run.py  (CLI)
#   python -m src.pipeline.run --mode {incremental|backfill|events-only}
#   1) state/{entity}.json 로 신규 accession 판정. 없으면 exit 0 + ::notice::
#   2) collector 호출 -> holdings 갱신
#   3) analytics 호출 -> events/metrics 갱신
#   4) integrity_gate() 통과 시에만 state 갱신
#   5) STRONG_* 이벤트가 있으면 GitHub Step Summary 에 출력

# src/pipeline/gate.py
def integrity_gate(holdings: list[dict], metrics: list[dict]) -> list[str]
    """다음 중 하나라도 걸리면 위반 사유 리스트 반환 (호출자가 exit 1):
       - 최신 분기 포지션 수 0
       - 총액이 전분기 대비 ±MAX_TOTAL_SWING_PCT 초과 변동
       - weight_pct 합계가 100 ± 0.5 범위 밖
       - 동일 report_date 에 중복 (cusip, title_of_class) 존재
       - event_id 중복"""

# src/pipeline/notify.py
def emit_summary(events: list[dict], metrics: dict) -> str
    """GITHUB_STEP_SUMMARY 에 쓸 마크다운 생성. 로컬 실행 시 stdout."""
```

**워크플로우 3종** (`.github/workflows/`):

| 파일 | 크론 | 역할 |
|---|---|---|
| `fetch-13f.yml` | `0 21 * * *` | 매일 submissions 확인, 신규 13F 만 처리 |
| `poll-daily.yml` | `0 22 * * 1-5` | 13D/G·Form 4 신규 감지 → Issue |
| `build-dashboard.yml` | `push: data/normalized/**` | 대시보드 빌드 → GitHub Pages |

공통 요구사항: `permissions` 최소화(`contents: write`, Pages 워크플로만 `pages: write`, `id-token: write`),
`concurrency` 그룹으로 중복 실행 차단, `workflow_dispatch` 항상 포함,
실패 시 Issue 자동 생성, 시크릿 사용 금지(`SEC_USER_AGENT` 는 env 변수로 평문 지정).

---

## D. Dashboard — `dashboard/`

```python
# dashboard/build.py  (CLI)
#   python dashboard/build.py
#   normalized/*.jsonl -> dashboard/dashboard_data.json (단일 파일, 사전 집계 완료)
```

`index.html` 은 **단일 파일**. Chart.js 만 CDN 으로 로드하고 빌드 체인 없음.
`dashboard_data.json` 을 fetch 하며, 화면 5개(현재 포트폴리오 / 변화 피드 / 포지션 시계열 /
집중도·회전율 / 공시 인덱스)를 탭으로 전환한다.

**표시 원칙 (강제)**
1. 모든 화면에 `report_date`, `filing_date`, `현재로부터 N일 경과`를 상시 노출.
2. 13F 확정 수치와 13D 기반 잠정 수치(`provisional: true`)를 시각적으로 구분.
3. 모든 수치에서 EDGAR 원문 링크까지 2클릭 이내 도달.
4. 하단 고정 면책: "13F 는 미국 상장 롱 포지션만 포함하며 스왑·공매도·비상장 자산은
   나타나지 않습니다. 본 자료는 정보 제공 목적이며 투자 자문이 아닙니다."
5. `localStorage` 등 브라우저 스토리지 사용 금지 — 모든 상태는 메모리 변수로.

---

## 공통 규율

- Python 3.11, 표준 라이브러리 + `requests` 만 사용. 그 외 의존성 추가 금지.
- 네트워크 호출은 `time.sleep(REQUEST_INTERVAL_SEC)` 준수, 403/429 시 지수 백오프 3회.
- 모든 CLI 는 `python -m` 으로 실행 가능해야 하며 `--help` 를 제공한다.
- 실패는 조용히 넘기지 않는다. 데이터 무결성 위반은 예외를 던진다.
- 커밋하지 않는다 (오케스트레이터가 통합 후 일괄 처리).
