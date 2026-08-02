# Ackman Tracker

빌 애크먼(**Pershing Square Capital Management, L.P.**, CIK `0001336528`)의 SEC 공시를
수집·정규화·분석해 정적 대시보드로 서빙하는 시스템입니다.

**서버 0대 · 시크릿 0개 · 비용 $0.**
GitHub Actions 크론이 EDGAR에서 데이터를 받아 JSONL을 리포지토리에 커밋하고,
GitHub Pages가 대시보드를 서빙합니다. SEC와 OpenFIGI 모두 인증을 요구하지 않으므로
**유출될 자격증명 자체가 없는 것**이 이 아키텍처의 핵심 장점입니다.

> 13F는 미국 상장 롱 포지션만 포함하며 스왑·공매도·비상장 자산은 나타나지 않습니다.
> 본 자료는 정보 제공 목적이며 투자 자문이 아닙니다.

---

## 1. 구조

```
src/common/schema.py      공유 데이터 계약 (수정 금지)
src/collector/            EDGAR 수집·13F 파싱·CUSIP→티커 매핑
src/analytics/            분기 diff · conviction · 포트폴리오 지표
src/pipeline/             오케스트레이션 · 무결성 게이트 · 알림
dashboard/                정적 HTML 대시보드 + 빌드 스크립트
.github/workflows/        크론 3종
data/
  raw/                    EDGAR 원문 보존 (감사 추적)
  normalized/             holdings / filings / events / metrics .jsonl
  reference/              cusip_map.json (영구 캐시)
  state/                  last_seen.json (멱등성 상태)
```

데이터 흐름:

```
submissions API → 신규 accession 판정(state) → 파싱·정규화 → CUSIP 매핑
   → holdings.jsonl → diff → events/metrics.jsonl
   → [무결성 게이트] ──실패──▶ Issue 생성 + 워크플로우 실패 (커밋 안 함)
                      └─통과──▶ git commit → build-dashboard → GitHub Pages
```

---

## 2. 로컬 실행

### 2.1 준비

Python **3.11**, 의존성은 `requests` 하나뿐입니다.

```bash
git clone https://github.com/<owner>/ackman-tracker.git
cd ackman-tracker
python -m pip install requests

# SEC는 연락처가 담긴 User-Agent를 요구합니다. 없으면 403이 돌아옵니다.
export SEC_USER_AGENT="AckmanTracker <이름> <이메일>"
export PYTHONPATH="$PWD"        # python -m src.* 실행을 위해
```

`SEC_USER_AGENT`는 **비밀이 아닙니다.** SEC가 요구하는 것은 문제 발생 시 연락할
수단일 뿐이므로 시크릿이 아니라 평문 환경변수/워크플로우 `env:`로 지정합니다.
설정하지 않으면 `src/common/schema.py`의 기본값이 쓰입니다.

### 2.2 명령

```bash
# 도움말
python -m src.pipeline.run --help

# 신규 공시만 처리 (평상시 실행 경로). 신규가 없으면 즉시 종료합니다.
python -m src.pipeline.run --mode incremental

# 전량 백필. 처음 한 번만 필요합니다. 개수 제한으로 먼저 시험하세요.
python -m src.pipeline.run --mode backfill --limit 3
python -m src.pipeline.run --mode backfill

# 수집 없이 분석만 다시 (analytics 로직을 고쳤을 때)
python -m src.pipeline.run --mode events-only

# 13D/G·Form 4 신규 감지만 (수집·분석 없음)
python -m src.pipeline.run --detect-only --forms "SC 13D,SC 13D/A,4"

# 아무것도 쓰지 않고 계획만 확인
python -m src.pipeline.run --mode incremental --dry-run
```

개별 모듈을 직접 부를 수도 있습니다. 파이프라인은 아래 진입점을 호출할 뿐입니다.

```bash
python -m src.collector.backfill [--limit N]
python -m src.analytics.build_events
python dashboard/build.py
```

### 2.3 검증

```bash
python -m src.pipeline.gate        # 무결성 게이트 단위 테스트 (합성 데이터)
python -m src.pipeline.notify --demo --kind summary   # 알림 마크다운 렌더
python -m src.pipeline._selftest   # 파이프라인 전체 자체 점검 (네트워크 불필요)
```

`--mode incremental`은 collector/analytics가 아직 없어도 크래시하지 않습니다.
어느 단계가 준비되지 않았는지 로그로 보고한 뒤 정상 종료합니다.

**종료 코드**: `0` 정상 또는 대기, `1` 무결성 게이트 실패(**커밋 금지**),
`2` 인자 오류.

---

## 3. GitHub 배포

1. **리포지토리 생성 후 push.** 기본 브랜치는 `main`을 전제로 합니다.
   다른 이름을 쓰면 `build-dashboard.yml`의 `branches:`를 바꾸세요.
2. **Actions 활성화** — Settings → Actions → General →
   *Allow all actions*, Workflow permissions는 *Read and write permissions*.
   (워크플로우 파일에 `permissions:`를 명시했지만, 조직 정책이 기본값을
   read-only로 강제하면 커밋이 실패합니다.)
3. **Pages 활성화** — Settings → Pages → Source를 **GitHub Actions**로 설정.
   `deploy-pages`는 이 설정 없이는 실패합니다.
4. **수동 실행으로 전 경로 검증** — Actions 탭에서 세 워크플로우를 각각
   `Run workflow`로 실행합니다. 최초에는 `fetch-13f`를 `mode=backfill`,
   `limit=3`으로 돌려 소량으로 확인한 뒤 전량 백필하는 것을 권합니다.
5. **크론은 자동으로 동작합니다.** 별도 설정이 필요 없습니다.
   단, 60일간 리포지토리에 활동이 없으면 GitHub가 스케줄을 자동 비활성화합니다
   (파이프라인이 데이터를 커밋하므로 실제로는 잘 발생하지 않습니다).

**시크릿은 하나도 등록하지 않습니다.** 워크플로우는 기본 `GITHUB_TOKEN`만 씁니다.

### 워크플로우

| 파일 | 트리거 | 역할 | 권한 |
|---|---|---|---|
| `fetch-13f.yml` | `0 21 * * *` + 수동 | 매일 submissions 확인, 신규 13F만 처리 → 커밋 | `contents: write`, `issues: write` |
| `poll-daily.yml` | `0 22 * * 1-5` + 수동 | 13D/G·Form 3/4 신규 감지 → Issue | `contents: write`, `issues: write` |
| `build-dashboard.yml` | `push: data/normalized/**` + 수동 | 대시보드 빌드 → Pages 배포 | `contents: write`, `pages: write`, `id-token: write` |

`fetch-13f`와 `poll-daily`는 같은 concurrency 그룹(`ackman-tracker-data`)을 공유합니다.
둘 다 리포지토리에 커밋하므로 동시에 push 하면 경합이 나기 때문입니다.

---

## 4. 운영 주의사항

### SEC User-Agent (필수)

모든 SEC 요청에 `User-Agent: {이름} {이메일}` 헤더가 있어야 하며, 없으면 **403**입니다.
워크플로우에서는 `env: SEC_USER_AGENT:`로 평문 지정되어 있습니다.
**포크해서 쓰는 경우 세 워크플로우의 이 값을 본인 연락처로 바꾸세요.**
남의 연락처로 요청을 보내면 그쪽이 SEC의 차단 통지를 받습니다.

### Rate limit

SEC는 `data.sec.gov` / `www.sec.gov` / `efts.sec.gov` **전 도메인 합산 초당 10요청**으로
제한합니다. 이 시스템은 `REQUEST_INTERVAL_SEC = 0.34`(초당 3요청 이하)로 자체 제한합니다.
GitHub Actions 러너는 공유 IP이므로 다른 사용자와 쿼터를 나눠 쓸 가능성이 있어
여유를 크게 두었습니다. **이 값을 낮추지 마세요.** 403/429가 오면 지수 백오프로 3회
재시도합니다.

OpenFIGI는 무인증 시 **분당 25요청, 요청당 100건 배치**입니다. CUSIP은 불변이므로
`data/reference/cusip_map.json`에 영구 캐시하며, 미스만 호출합니다.

전량 백필은 82건의 13F × 파일링당 2~3회 요청이므로 수 분이 걸립니다.
`fetch-13f`의 `timeout-minutes`를 90으로 잡은 이유입니다.

### 무결성 게이트

**조용히 잘못된 데이터가 커밋되는 것이 최악의 실패 모드입니다.**
`src/pipeline/gate.py`가 커밋 직전에 다음을 검사하고, 하나라도 걸리면
커밋하지 않고 워크플로우를 실패시킵니다.

| 코드 | 검사 |
|---|---|
| `EMPTY_QUARTER` | 최신 분기 포지션 수 0 |
| `TOTAL_SWING` | 총액이 전분기 대비 ±80%(`MAX_TOTAL_SWING_PCT`) 초과 변동 |
| `WEIGHT_SUM` | `weight_pct` 합계가 100 ± 0.5 범위 밖 |
| `DUP_POSITION` | 동일 `report_date`에 `(cusip, title_of_class)` 중복 |
| `DUP_EVENT_ID` | `event_id` 중복 |

게이트가 실패하면 `[FAILURE] ...` 제목의 Issue가 생성됩니다(동일 제목의 열린 이슈가
있으면 코멘트만 추가되어 매일 중복 생성되지 않습니다). 조치 순서는 이렇습니다.

1. Actions 로그에서 `::error::[CODE] ...` 줄을 확인합니다.
2. 원인을 고칩니다. 대부분 파서의 단위 정규화(구 스키마 천 달러 vs `X0202` 달러)나
   정정 병합(`NEW HOLDINGS`를 덮어쓰기로 처리) 문제입니다.
3. `fetch-13f`를 수동 실행합니다.

**게이트를 우회하지 마세요.** `--skip-gate`는 로컬 디버깅 전용이며 워크플로우에서
쓰지 않습니다.

### 멱등성과 상태 파일

`data/state/last_seen.json`이 유일한 상태입니다.

```json
{
  "accessions": ["0001172661-26-002336"],  // 처리 완료 (게이트 통과 후에만 추가)
  "failed": {"0001172661-25-001497": {"attempts": 2, "error": "..."}},
  "alerted": ["0001172661-26-002500"],     // 13D/G·Form4 알림 완료
  "last_run": "2026-08-02T21:00:00Z"
}
```

- 파이프라인은 몇 번을 재실행해도 같은 결과를 냅니다. 신규 accession이 없으면
  즉시 exit 0 하면서 `::notice::No new filings`를 출력합니다.
- **`accessions`는 게이트를 통과했고 실제로 `normalized/` 산출물에 반영된
  accession만 승격됩니다.** 한 파일링 파싱이 실패해도 나머지는 정상 처리되며,
  실패 건은 `failed` 맵에 기록되어 다음 실행에서 자동 재시도됩니다.
- 처음부터 다시 만들려면 `accessions`를 `[]`로 비우고 `--mode backfill`을 실행합니다.
- 데이터가 오염되면 `git revert`가 가장 빠른 복구 수단입니다. JSONL을 Git에 두는
  이유가 이것입니다 — 버전 관리가 곧 감사 추적입니다.

### 알림

별도 알림 인프라(Slack·이메일·웹훅)를 두지 않고 GitHub Issue를 재사용합니다.
Issue 제목이 결정적이라 중복 생성이 방지됩니다.

| 상황 | 제목 | 중복 방지 |
|---|---|---|
| 워크플로우 실패 | `[FAILURE] {workflow} 워크플로우 실패` | 열린 동일 제목 이슈에 코멘트 |
| STRONG 이벤트 | `[STRONG] {report_date} 고확신 변화 N건` | open/closed 전체 조회 후 존재하면 생성 안 함 |
| 신규 13D/G·Form 4 | `[공시 감지] {날짜} 신규 파일링 N건` | 상태 파일 `alerted` + 제목 조회 |

Issue 알림을 받으려면 리포지토리를 **Watch → All Activity** 로 설정하세요.

### 구조적 한계 (완화만 가능, 제거 불가)

- **13F는 롱 온리 스냅샷**입니다. 스왑·CDS·공매도·비상장 자산은 원리적으로 보이지 않습니다.
- **45일 지연**은 우회 불가능합니다. 13D 채널이 부분적으로 보완할 뿐입니다.
- **매매 신호를 생성하지 않습니다.** 45일 지연 데이터를 신호로 쓰는 것은 구조적 열위입니다.

---

## 5. 문서

| 문서 | 내용 |
|---|---|
| `docs/architecture.md` | 설계 근거 전문, 실측 검증 로그, 파서 참조 구현 |
| `specs/CONTRACTS.md` | 모듈 간 인터페이스 계약 |
| `PROJECT.md` | 작업 보드, 확정 설계 결정, 알려진 함정, 검증 기준선 |
