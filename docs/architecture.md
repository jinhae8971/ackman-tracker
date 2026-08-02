# Ackman Tracker — 시스템 아키텍처 설계서

**대상**: Pershing Square Capital Management 포지션 추적 및 변화 감지 시스템
**범위**: SEC 공시 전량 수집 → 정규화 → 변화 감지 → 정적 대시보드
**운영 형태**: GitHub Actions 크론 + GitHub Pages (서버리스, 무료)
**작성일**: 2026-08-02
**설계 검증**: 본 문서의 모든 엔드포인트·스키마·파싱 경로는 작성 시점에 실제 호출로 검증됨 (부록 A)

---

## 1. 설계 목표와 비목표

이 시스템의 목표는 빌 애크먼의 공시 기반 포지션을 시계열로 축적하고, 분기 간 변화를 자동으로 감지해 사람이 판단할 수 있는 형태로 제시하는 것입니다. 핵심 가치는 예측이 아니라 **누락 없는 관측과 재현 가능한 기록**에 있습니다. 애크먼의 움직임은 공시 시점에 이미 시장에 알려지므로, 시스템이 제공할 수 있는 우위는 속도가 아니라 맥락입니다. 언제 진입했고, 얼마나 오래 들고 있었고, 어떤 국면에서 늘리고 줄였는지를 한 화면에서 볼 수 있게 하는 것이 설계의 중심입니다.

명시적 비목표는 세 가지입니다. 첫째, 매매 신호를 생성하지 않습니다. 13F는 45일 지연 데이터이며 이를 신호로 쓰는 것은 구조적으로 열위입니다. 둘째, 유료 데이터 소스에 의존하지 않습니다. 전량 공개 공시와 무료 공개 API만 사용합니다. 셋째, 실시간 스트리밍을 하지 않습니다. 공시 데이터의 갱신 주기가 분기·일 단위이므로 스트리밍 인프라는 과설계입니다.

---

## 2. 데이터 소스 계층

### 2.1 소스 인벤토리

Pershing Square Capital Management, L.P.의 EDGAR CIK는 **0001336528**, 13F File Number는 **028-11694**입니다. submissions API로 확인한 누적 제출 이력의 폼 분포는 다음과 같습니다.

| 폼 타입 | 누적 건수 | 성격 | 시스템 내 역할 |
|---|---|---|---|
| `13F-HR` | 82 | 분기말 미국 상장주식 보유 명세 | **주 데이터 소스** — 포지션 시계열의 근간 |
| `13F-HR/A` | 15 | 13F 정정 | 원본 덮어쓰기 대상. 정정 이력 별도 보관 |
| `SC 13D` / `SC 13D/A` | 24 / 182 | 5%+ 지분, 경영 참여 의도 | **선행 지표** — 13F보다 훨씬 빠름 |
| `SC 13G` / `SC 13G/A` | 20 / 29 | 5%+ 지분, 수동적 보유 | 지분율 변동 추적 |
| `Form 3 / 4` | 18 / 129 | 내부자 지위·거래 | 10%+ 보유 종목의 일 단위 매매 |
| `DFAN14A`, `PREN14A`, `PRRN14A` | 156 | 위임장 경쟁 자료 | 액티비스트 캠페인 국면 표시 |
| `425` | 39 | 합병 관련 커뮤니케이션 | SPAC·M&A 이벤트 태깅 |

이 분포 자체가 설계 판단의 근거가 됩니다. `SC 13D/A`가 182건으로 13F(82건)의 두 배가 넘는다는 사실은, 이 매니저의 실제 활동 궤적이 분기 스냅샷보다 13D 계열 공시에 훨씬 촘촘하게 기록되어 있음을 뜻합니다. 13F만 보는 트래커는 애크먼의 절반만 보는 셈입니다.

### 2.2 지연 시간 특성

각 소스의 정보 신선도가 다르므로 대시보드에서 이를 명시적으로 구분해 표시해야 합니다.

| 소스 | 법정 제출 기한 | 실질 지연 |
|---|---|---|
| 13F-HR | 분기말 후 45일 | 45일 (통상 마감일 직전 제출) |
| SC 13D (신규) | 취득 후 **5영업일** (2024-02-05 개정) | 약 1주 |
| SC 13D/A (정정) | 사유 발생 후 **2영업일** | 2~3일 |
| SC 13G (QII) | 분기말 후 45일 / 10% 돌파 시 월말 후 5영업일 | 최대 45일 |
| Form 4 | 거래 후 2영업일 | 2~3일 |

즉 **13D/A와 Form 4가 사실상의 준실시간 채널**이고, 13F는 사후 정합성 검증용 앵커입니다. 파이프라인은 이 둘을 다른 주기로 폴링해야 합니다.

### 2.3 접근 엔드포인트 (검증 완료)

```
# 제출 이력 전체 (JSON, 인증 불필요)
GET https://data.sec.gov/submissions/CIK0001336528.json

# 개별 파일링 디렉터리 목록
GET https://www.sec.gov/Archives/edgar/data/1336528/{accession_no_dashes}/index.json

# 13F 정보 테이블 / 커버페이지
GET .../{accession_no_dashes}/infotable.xml
GET .../{accession_no_dashes}/primary_doc.xml

# 신규 공시 폴링용 Atom 피드
GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001336528&type=SC+13D&output=atom

# CUSIP → 티커 매핑 (무료, 키 불필요)
POST https://api.openfigi.com/v3/mapping
```

**필수 준수 사항**: 모든 요청에 `User-Agent: {이름} {이메일}` 헤더가 있어야 하며, 없으면 403이 반환됩니다. SEC는 `data.sec.gov` / `www.sec.gov` / `efts.sec.gov` 전 도메인 합산 **초당 10요청**으로 제한합니다. 파이프라인은 이보다 훨씬 낮은 초당 3요청으로 자체 제한하도록 설계합니다 — GitHub Actions 러너 IP가 공유 IP이므로 다른 사용자와 쿼터를 나눠 쓸 가능성이 있기 때문입니다.

---

## 3. 파싱 계층 — 반드시 알아야 할 함정

### 3.1 `value` 필드의 단위 전환 (치명적)

13F XML의 `<value>` 필드 단위가 스키마 버전에 따라 다릅니다. 이를 놓치면 포트폴리오 규모가 1000배 왜곡됩니다. 실측 확인 결과는 다음과 같습니다.

| 파일링 | 제출일 | schemaVersion | sum(value) | 실제 의미 |
|---|---|---|---|---|
| 0001172661-22-002568 | 2022-11-14 | *(없음, 구 스키마)* | 7,877,045 | **$7.88B** (천 달러 단위) |
| 0001172661-23-000673 | 2023-02-13 | `X0202` | 8,784,004,892 | **$8.78B** (달러 단위) |

따라서 파서는 반드시 다음 규칙을 적용합니다.

```python
def normalize_value(raw_value: int, schema_version: str | None) -> int:
    """13F value를 달러 단위로 정규화."""
    if schema_version and schema_version >= "X0202":
        return raw_value          # 이미 달러
    return raw_value * 1000       # 구 스키마: 천 달러 → 달러
```

`schemaVersion`은 `primary_doc.xml`에서 읽습니다. 구 스키마 파일링에는 이 태그가 아예 없으므로 `None` 판정으로 안전하게 분기됩니다.

### 3.2 체크섬 검증

`primary_doc.xml`의 `<tableEntryTotal>`과 `<tableValueTotal>`은 정보 테이블의 행 수·금액 합계를 담고 있습니다. 파싱 직후 이를 대조하면 XML 파싱 오류와 부분 다운로드를 즉시 잡아낼 수 있습니다. 실측 두 건 모두 `sum(value)`와 `tableValueTotal`이 정확히 일치했으므로, 이 검증은 **하드 실패 조건**으로 두어도 안전합니다.

### 3.3 XML 네임스페이스

두 문서의 네임스페이스가 다르며, 스키마 버전에 따라서도 변합니다. 하드코딩하지 말고 루트 태그에서 동적으로 추출하는 것이 안전합니다.

```
infotable.xml   → http://www.sec.gov/edgar/document/thirteenf/informationtable
primary_doc.xml → http://www.sec.gov/edgar/thirteenffiler
```

```python
def ns_of(root) -> dict:
    tag = root.tag
    return {"n": tag[1:tag.index("}")]} if tag.startswith("{") else {}
```

### 3.4 CUSIP → 티커 매핑

13F는 CUSIP만 담고 티커를 담지 않습니다. SEC의 `company_tickers.json`(약 797KB)은 티커↔CIK만 제공하고 CUSIP은 없으므로 단독으로는 불충분합니다. 발행사명 문자열 매칭은 `ALPHABET INC`처럼 클래스가 나뉘는 종목에서 실패합니다.

권장 경로는 **OpenFIGI v3 mapping API**입니다. 인증 없이 사용 가능하고, CUSIP을 정확한 티커·FIGI·증권유형으로 해석하며, 클래스 구분까지 반영합니다. 실측에서 `02079K107 → GOOG`, `02079K305 → GOOGL`이 정확히 분리되었습니다. 매핑 결과는 `cusip_map.json`으로 영구 캐시하여 재호출을 없앱니다 — CUSIP은 불변이므로 한 번 해석하면 다시 조회할 필요가 없습니다.

주의할 점은 OpenFIGI가 CUSIP 하나에 대해 거래소별로 수십 개 레코드를 반환한다는 것입니다. `exchCode == "US"` 이고 `securityType`이 보통주/ADR 계열인 항목을 우선 선택하는 결정 규칙이 필요합니다.

---

## 4. 데이터 모델

저장 형태는 **파일 기반 append-only 시계열**입니다. DB를 두지 않는 이유는 4.4에서 설명합니다.

### 4.1 디렉터리 구조

```
data/
  raw/                              # 원문 보존 (감사 추적)
    13F/{report_date}/{accession}/{infotable.xml, primary_doc.xml}
    13D/{accession}/...
  normalized/
    holdings.jsonl                  # 정규화 포지션 (append-only)
    filings.jsonl                   # 파일링 메타
    events.jsonl                    # 감지된 변화 이벤트
  reference/
    cusip_map.json                  # CUSIP → 티커 (영구 캐시)
    sector_map.json                 # 티커 → 섹터
  state/
    last_seen.json                  # 마지막 처리 accession (멱등성 보장)
```

### 4.2 정규화 스키마

**`holdings.jsonl`** — 한 행이 (파일링 × 종목) 한 건입니다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `report_date` | date | 분기말 기준일 (`periodOfReport`) |
| `filing_date` | date | 실제 제출일 — 지연 계산용 |
| `accession` | string | 파일링 고유 ID (멱등성 키) |
| `form_type` | string | `13F-HR` \| `13F-HR/A` |
| `is_amendment` | bool | 정정 여부 |
| `cusip` | string | 원본 식별자 |
| `ticker` | string \| null | OpenFIGI 매핑 결과 |
| `issuer_name` | string | 원문 발행사명 |
| `title_of_class` | string | 증권 클래스 |
| `value_usd` | int | **달러 정규화 후** 값 |
| `shares` | int | `sshPrnamt` |
| `share_type` | string | `SH` \| `PRN` |
| `put_call` | string \| null | 옵션 포지션 구분 |
| `discretion` | string | `SOLE` / `DEFINED` / `OTHER` |
| `weight_pct` | float | 파생: 포트폴리오 내 비중 |
| `schema_version` | string \| null | 단위 정규화 감사용 |

**`events.jsonl`** — 변화 감지 결과입니다.

| 필드 | 설명 |
|---|---|
| `event_id` | `{report_date}:{cusip}:{event_type}` — 중복 방지 결정적 ID |
| `event_type` | `NEW` \| `ADD` \| `TRIM` \| `EXIT` \| `HOLD` |
| `prev_shares` / `curr_shares` | 주식 수 변화 |
| `share_delta_pct` | 주식 수 기준 증감률 |
| `value_delta_usd` | 평가액 변화 |
| `weight_before` / `weight_after` | 비중 변화 (bp) |
| `conviction_signal` | 파생 등급 (5.2 참조) |

### 4.3 정정(Amendment) 처리 규칙 — 주의 요망

`13F-HR/A`를 "최신 것으로 덮어쓰기"로 처리하면 **틀립니다.** 정정에는 두 가지 종류가 있고, `primary_doc.xml`의 `<amendmentType>`이 이를 구분합니다.

```xml
<isAmendment>true</isAmendment>
<amendmentNo>1</amendmentNo>
<amendmentInfo>
  <amendmentType>NEW HOLDINGS</amendmentType>   <!-- 또는 RESTATEMENT -->
</amendmentInfo>
```

- **`RESTATEMENT`** — 정보 테이블 전체를 재작성. 원본을 **대체**합니다.
- **`NEW HOLDINGS`** — 원본에서 누락된 종목만 담음. 원본에 **병합**해야 합니다.

이 구분은 가상의 우려가 아닙니다. 실측 검증에서 Pershing Square의 2025-04-16 정정(`0001172661-25-001497`, 2024-12-31 기준)은 `NEW HOLDINGS` 타입이며 **Hertz 단 1종목, $46.5M**만 담고 있습니다. 이를 대체로 처리하면 해당 분기 포트폴리오가 10종목 수십억 달러에서 1종목 4천만 달러로 붕괴하고, 다음 분기 diff에서 9건의 허위 `EXIT` 이벤트와 9건의 허위 `NEW` 이벤트가 연쇄 발생합니다.

```python
def resolve_quarter(filings: list[dict]) -> list[dict]:
    """한 report_date의 파일링들을 유효 포지션 집합으로 해석."""
    filings = sorted(filings, key=lambda f: f["filing_date"])
    holdings = {}
    for f in filings:
        if f["amendment_type"] == "RESTATEMENT":
            holdings = {}                       # 전체 대체
        for h in f["holdings"]:
            holdings[(h["cusip"], h["title_of_class"])] = h   # 병합/갱신
    return list(holdings.values())
```

병합 키를 CUSIP 단독이 아니라 `(cusip, title_of_class)`로 둔 것은 Alphabet처럼 동일 발행사가 복수 클래스를 갖는 경우를 분리하기 위함입니다.

### 4.4 왜 DB가 아니라 파일인가

데이터 규모가 결정 요인입니다. 13F 82건 × 평균 10~20 포지션 = 약 1,500행, 13D 계열을 모두 더해도 1만 행 수준입니다. 이 규모에서 Postgres를 두면 얻는 것은 없고 운영 부담(호스팅 비용, 커넥션 관리, 백업, 마이그레이션)만 생깁니다.

반면 JSONL을 Git에 커밋하면 **버전 관리가 곧 감사 추적**이 됩니다. 어느 커밋에서 어떤 수치가 바뀌었는지 `git log -p`로 즉시 추적되고, 파이프라인 버그로 데이터가 오염되어도 `git revert` 한 번으로 복구됩니다. 금융 데이터 파이프라인에서 이 성질은 상당한 가치가 있습니다. 데이터가 10만 행을 넘어서면 DuckDB 단일 파일로 승격하는 것이 다음 단계이고, 그때도 서버는 필요 없습니다.

---

## 5. 변화 감지 알고리즘

### 5.1 분기 간 diff

두 연속 분기의 포지션 집합을 CUSIP 키로 outer join 한 뒤 분류합니다.

```python
NEW  : cusip ∉ prev,  cusip ∈ curr
EXIT : cusip ∈ prev,  cusip ∉ curr
ADD  : share_delta_pct >  +MIN_DELTA
TRIM : share_delta_pct <  -MIN_DELTA
HOLD : |share_delta_pct| ≤ MIN_DELTA
```

임계값 `MIN_DELTA`는 **1%**로 둡니다. 0%로 두면 주식 분할·환매 등에 의한 미세 조정이 이벤트로 잡혀 노이즈가 발생하고, 5%로 두면 대형 포지션의 유의미한 조정을 놓칩니다.

**필수 전처리**: 주식 분할과 티커 변경을 CUSIP 레벨에서 먼저 보정해야 합니다. 분할이 있으면 주식 수가 두 배로 뛰어 `ADD` 오탐이 발생합니다. 보정 없이 주식 수 델타만 쓰면 안 됩니다. CUSIP 자체가 바뀌는 사례(스핀오프, 재편입)도 있으므로 FIGI 레벨(`shareClassFIGI`)에서 동일성을 판정하는 것이 더 견고합니다.

### 5.2 확신도 신호 (Conviction Signal)

단순 증감률만으로는 "10억 달러 포지션의 3% 증량"과 "1000만 달러 포지션의 300% 증량"을 구분하지 못합니다. 두 축을 함께 봅니다.

```
conviction = f(비중 변화 bp, 신규 여부, 포지션 절대 규모)

STRONG_NEW    : NEW  이고 진입 비중 ≥ 5%
STRONG_ADD    : ADD  이고 비중 변화 ≥ +200bp
STRONG_TRIM   : TRIM 이고 비중 변화 ≤ -200bp
FULL_EXIT     : EXIT
ROUTINE       : 그 외
```

### 5.3 포트폴리오 수준 지표

집중도는 이 매니저를 이해하는 핵심 축입니다. Q1 2026 기준 11개 포지션에 $13.71B — 상위 4개 종목이 전체의 약 66%를 차지하는 극단적 집중형 포트폴리오입니다. 따라서 다음 지표를 매 분기 산출합니다.

**HHI (허핀달 지수)** = Σ(weightᵢ)² — 값이 클수록 집중. **Top-N 집중도** = 상위 1/3/5 종목 누적 비중. **회전율** = Σ|Δshares × price| / 평균 포트폴리오 가치 — 애크먼은 저회전 전략이므로 회전율 급등은 그 자체로 이례 신호입니다. **포지션 수 추이**와 **평균 보유 기간**(CUSIP별 최초 등장~소멸 분기 수)도 함께 봅니다.

### 5.4 13D 연계 — 조기 감지

13F diff는 45일 지연이지만, 13D는 5영업일입니다. 따라서 **13D/G 신규 제출을 감지하면 다음 13F를 기다리지 않고 즉시 이벤트를 발행**합니다. 13D의 Item 4(취득 목적)와 Item 5(지분율)를 파싱해 `PENDING_13F` 상태의 이벤트로 기록하고, 이후 13F가 도착하면 두 레코드를 대조해 정합성을 확인합니다. 이 대조는 데이터 품질 검증인 동시에, 애크먼이 13D 제출 이후 분기말까지 추가 매집했는지를 드러내는 분석 지표이기도 합니다.

---

## 6. 파이프라인 아키텍처

### 6.1 워크플로우 구성

세 개의 워크플로우로 분리합니다. 관심사 분리와 실패 격리가 이유입니다 — 일간 13D 폴링이 실패해도 분기 13F 수집이 영향받지 않아야 합니다.

```yaml
# .github/workflows/poll-daily.yml — 13D/G, Form 4 감지
on:
  schedule: [{ cron: "0 22 * * 1-5" }]   # 평일 UTC 22:00 (미 동부 장마감 후)
  workflow_dispatch:

# .github/workflows/fetch-13f.yml — 13F 수집
on:
  schedule: [{ cron: "0 21 * * *" }]     # 매일 1회 확인, 신규 없으면 즉시 종료
  workflow_dispatch:

# .github/workflows/build-dashboard.yml — 대시보드 빌드·배포
on:
  push: { paths: ["data/normalized/**"] }  # 데이터 변경 시에만 트리거
  workflow_dispatch:
```

13F 수집을 "45일째 되는 날 한 번"이 아니라 매일 확인하도록 한 것은 의도적입니다. 제출일은 마감 45일 이전 임의 시점일 수 있고, 정정 파일링은 언제든 올 수 있습니다. 매일 submissions API 한 번(118KB) 호출하는 비용은 사실상 0이며, 신규 accession이 없으면 즉시 종료하므로 러너 시간도 거의 쓰지 않습니다.

### 6.2 멱등성 설계

파이프라인은 몇 번을 재실행해도 같은 결과를 내야 합니다. `state/last_seen.json`에 처리 완료한 accession 집합을 유지하고, 신규 accession만 처리합니다. `events.jsonl`의 `event_id`가 `{report_date}:{cusip}:{event_type}`로 결정적이므로 중복 삽입이 원천 차단됩니다.

```python
seen = set(json.load(open("state/last_seen.json"))["accessions"])
new = [a for a in submissions_accessions if a not in seen]
if not new:
    print("::notice::No new filings"); sys.exit(0)
```

### 6.3 견고성 설계

**Rate limit**: `time.sleep(0.34)`로 초당 3요청 이하 유지, 429/403 응답 시 지수 백오프 최대 3회 재시도.

**부분 실패 격리**: 한 파일링 파싱 실패가 전체 실행을 중단시키지 않도록 파일링 단위 try/except로 감싸고, 실패 건은 `state/failed.json`에 기록해 다음 실행에서 재시도합니다.

**데이터 무결성 게이트**: 체크섬 불일치(3.2), 포트폴리오 총액 전분기 대비 ±80% 초과 변동, 포지션 수 0 — 이 세 조건 중 하나라도 걸리면 커밋하지 않고 워크플로우를 실패시킵니다. 조용히 잘못된 데이터가 커밋되는 것이 최악의 실패 모드이기 때문입니다.

**알림**: 워크플로우 실패 및 `STRONG_*` 이벤트 발생 시 GitHub Issue 자동 생성. 별도 알림 인프라 없이 GitHub 자체 알림을 재사용합니다.

**권한**: 워크플로우에 `permissions: { contents: write, pages: write, id-token: write }`만 부여합니다. 기본 `GITHUB_TOKEN` 외 시크릿이 필요 없는 것이 이 아키텍처의 큰 장점입니다 — SEC와 OpenFIGI 모두 인증을 요구하지 않으므로 유출될 자격증명 자체가 없습니다.

### 6.4 처리 흐름

```
[submissions API]
      │  신규 accession 판정 (state/last_seen.json)
      ▼
[index.json → infotable.xml + primary_doc.xml 다운로드]
      │  raw/ 에 원문 보존
      ▼
[파싱 + 단위 정규화 + 체크섬 검증]  ← 실패 시 하드 스톱
      │
      ▼
[CUSIP → 티커 매핑]  ← cusip_map.json 캐시 우선, 미스만 OpenFIGI 호출
      │
      ▼
[holdings.jsonl append]
      │
      ▼
[전분기 대비 diff → events.jsonl]
      │
      ▼
[무결성 게이트] ── 실패 ──▶ [Issue 생성 + 워크플로우 실패]
      │ 통과
      ▼
[git commit & push] ──▶ [build-dashboard 트리거] ──▶ [GitHub Pages]
```

---

## 7. 대시보드 설계

### 7.1 기술 선택

단일 HTML 파일에 Chart.js를 CDN으로 임베드하고, 데이터는 빌드 시점에 생성한 `dashboard_data.json`을 fetch 합니다. React/Vite 빌드 체인을 쓰지 않는 이유는 대시보드가 화면 5개 규모이고, 빌드 체인을 두면 의존성 취약점 관리와 빌드 실패라는 새로운 실패 모드가 생기기 때문입니다. 정적 파일 하나는 영원히 열립니다.

### 7.2 화면 정의

**화면 1 — 현재 포트폴리오 (랜딩)**
최신 분기 기준 보유 종목을 비중 순으로 표시합니다. 각 행은 종목명, 티커, 비중, 평가액, 주식 수, 전분기 대비 변화(색상 인코딩), 최초 진입 분기, 보유 분기 수를 담습니다. 상단에는 총 포트폴리오 가치, 포지션 수, HHI, Top-5 집중도를 KPI 카드로 배치하고, **데이터 기준일과 제출일을 함께 명시**합니다 — 사용자가 45일 지연을 항상 인지해야 하기 때문입니다.

**화면 2 — 변화 감지 피드**
`events.jsonl`을 시간 역순으로 나열합니다. 신규 진입은 초록, 청산은 빨강, 증량/감량은 채도로 강도를 표현합니다. `STRONG_*` 이벤트는 상단에 고정 표시합니다. 13D 기반 조기 이벤트는 별도 배지로 구분해, 13F 확정 전 잠정 정보임을 명확히 합니다.

**화면 3 — 포지션 시계열**
종목별 비중 추이를 스택 영역 차트로 표시합니다. X축은 분기, Y축은 비중 100%. 이 화면 하나로 "무엇이 언제 들어와서 언제 나갔는지"의 전체 서사가 읽힙니다. 특정 종목 클릭 시 해당 종목의 주식 수·평가액 추이와 관련 공시(13D/A, Form 4, DFAN14A) 타임라인이 드릴다운으로 열립니다.

**화면 4 — 집중도·회전율**
HHI, Top-N 집중도, 포지션 수, 회전율의 시계열을 하나의 멀티라인 차트로 표시합니다. 액티비스트 캠페인 기간(DFAN14A 제출 구간)을 배경 음영으로 오버레이하면, 캠페인과 포트폴리오 집중도 변화의 관계가 시각적으로 드러납니다.

**화면 5 — 공시 원문 인덱스**
전 파일링을 폼 타입·날짜로 필터링하고 EDGAR 원문으로 직접 링크합니다. 모든 수치에서 원문까지 두 클릭 내에 도달할 수 있어야 한다는 것이 원칙입니다. 검증 불가능한 대시보드는 신뢰할 수 없습니다.

### 7.3 표시 원칙

모든 화면에 데이터 기준일(`report_date`)과 제출일(`filing_date`), 그리고 "현재로부터 N일 경과"를 상시 표시합니다. 13F 기반 수치와 13D 기반 잠정 수치는 시각적으로 반드시 구분합니다. 어떤 수치도 출처 공시 링크 없이 표시하지 않습니다.

---

## 8. 스택 선정 근거 요약

| 레이어 | 선택 | 대안 | 선정 이유 |
|---|---|---|---|
| 수집 | Python 3.11 + `requests` | Node/axios | XML 파싱(`xml.etree`) 표준 라이브러리 지원, 금융 데이터 생태계 |
| 파싱 | `xml.etree.ElementTree` | `lxml` | 표준 라이브러리로 의존성 0, 13F 규모에 성능 충분 |
| 저장 | JSONL + Git | Postgres / SQLite | 1만 행 규모, Git이 감사 추적을 무료 제공 |
| 매핑 | OpenFIGI v3 | 유료 CUSIP 라이선스 | 무료·무인증·정확, 클래스 구분 지원 |
| 스케줄 | GitHub Actions cron | Cloud Scheduler / cron 서버 | 무료, 코드와 동일 저장소, 시크릿 불필요 |
| 대시보드 | 정적 HTML + Chart.js | React SPA | 빌드 체인 없음, 영구 동작, 배포 실패 모드 최소 |
| 배포 | GitHub Pages | Vercel / S3 | 무료, 동일 저장소, 추가 계정 불필요 |

이 스택의 총 운영 비용은 **$0**이고, 관리해야 할 시크릿은 **0개**, 유지보수 대상 서버는 **0대**입니다. 데이터 규모가 이 선택을 정당화합니다.

---

## 9. 리스크와 구조적 한계

가장 중요한 섹션입니다. 이 한계들은 완화할 수 있을 뿐 제거할 수 없습니다.

**13F는 롱 온리 스냅샷입니다.** 미국 상장 주식의 매수 포지션만 담깁니다. 공매도, 스왑, 대부분의 파생상품, 채권, 비상장 자산, 현금은 전혀 나타나지 않습니다. 애크먼은 역사적으로 CDS와 토탈리턴스왑을 통해 대규모 포지션을 구축한 이력이 있으며(2020년 크레딧 헤지가 대표 사례), 그런 포지션은 이 시스템에 **원리적으로 보이지 않습니다**. 대시보드는 "이것은 포트폴리오의 일부다"라는 문구를 상시 표시해야 합니다.

**해외 상장 종목의 커버리지가 불완전합니다.** Q1 2026 최대 보유 종목인 Brookfield Corp는 13F에 포함되었지만(미국 상장 클래스), 매니저가 캐나다 상장분을 별도 보유할 경우 그 부분은 13F에 나타나지 않을 수 있습니다.

**45일 지연은 우회 불가능합니다.** 13F 공시 시점의 포지션은 이미 최대 4.5개월 전 상태일 수 있습니다. 13D 채널이 이를 부분적으로 보완하지만 5% 이상 지분에만 적용됩니다.

**PSH 펀드 성과와 13F 포트폴리오는 일치하지 않습니다.** Pershing Square Holdings의 NAV 수익률을 13F 종목으로 재현하려는 시도는 실패합니다. 헤지, 레버리지, 비공시 자산 때문입니다. 두 수치를 나란히 놓되 등가로 취급하지 않아야 합니다.

**주식 분할·티커 변경·스핀오프**는 무보정 시 오탐의 최대 원인입니다. FIGI 레벨 동일성 판정과 분할 보정을 구현 초기부터 넣어야 합니다.

**EDGAR 스키마 변경 리스크**가 있습니다. 3.1에서 본 단위 전환이 그 사례입니다. 파서는 스키마 버전을 명시적으로 읽고, 미지의 버전을 만나면 실패하도록(silent pass가 아니라) 설계합니다.

**법적·이용약관 측면**은 상대적으로 안전합니다. SEC 공시는 퍼블릭 도메인이고 API는 인증 없이 공개되어 있으며, rate limit 준수와 User-Agent 명시가 유일한 요구사항입니다. OpenFIGI는 무료 티어 사용을 허용합니다. 다만 **본 시스템은 정보 제공용이며 투자 자문이 아닙니다** — 대시보드에 면책 고지를 표시할 것을 권합니다.

---

## 10. 구현 로드맵

**Phase 1 — 데이터 백필 (1~2일)**
submissions API에서 13F-HR 82건 전량을 수집해 원문을 보존하고, 단위 정규화와 체크섬 검증을 포함한 파서로 `holdings.jsonl`을 구축합니다. CUSIP 매핑 캐시를 채웁니다. 이 단계 완료 시점에 2005년 이후 전체 포지션 시계열이 확보됩니다.

**Phase 2 — 변화 감지 (1일)**
분기 간 diff, 분할 보정, conviction 등급 산출, 포트폴리오 지표(HHI, 집중도, 회전율)를 구현해 `events.jsonl`을 생성합니다. 백필 데이터로 과거 20년치 이벤트를 소급 생성해 알고리즘을 눈으로 검증합니다.

**Phase 3 — 자동화 (1일)**
세 개 워크플로우를 구성하고 멱등성·무결성 게이트·알림을 붙입니다. `workflow_dispatch`로 수동 실행해 전 경로를 검증한 뒤 크론을 활성화합니다.

**Phase 4 — 대시보드 (2~3일)**
화면 1·2·3을 먼저 구현해 배포하고, 4·5를 이어서 추가합니다.

**Phase 5 — 13D 채널 확장 (2일)**
13D/G 파싱과 조기 이벤트 발행, 13F 대조 정합성 검증을 추가합니다. 이 단계가 시스템의 정보 가치를 가장 크게 끌어올립니다.

**Phase 6 (선택) — 성과 귀속**
시장 가격 데이터를 결합해 종목별 기여도와 진입가 추정을 추가합니다. 현 설계 범위 밖이지만 스키마는 이를 수용하도록 설계되어 있습니다.

---

## 부록 A — 검증 로그

작성 시점(2026-08-02)에 실제 호출로 확인한 결과입니다.

| 검증 항목 | 결과 |
|---|---|
| `data.sec.gov/submissions/CIK0001336528.json` | HTTP 200, 118,421 bytes |
| 엔터티명 | `Pershing Square Capital Management, L.P.` |
| 13F-HR 누적 | 82건 (최신: 2026-05-15 제출, 2026-03-31 기준) |
| 파일링 `index.json` | HTTP 200, `infotable.xml` 5,520 bytes 확인 |
| `infotable.xml` 파싱 | 11개 포지션, 합계 $13,714,299,861 |
| `tableValueTotal` 대조 | 일치 (2건 샘플 모두) |
| 단위 전환 검증 | 2022-11 구스키마 = 천 달러 / 2023-02 `X0202` = 달러 |
| 부록 B 파서 실행 | 3개 파일링(신·구 스키마, 정정) 전부 정상 파싱 |
| 정정 타입 확인 | `0001172661-25-001497` = `NEW HOLDINGS`, 1종목 $46.5M (§4.3 근거) |
| OpenFIGI CUSIP 매핑 | `02079K107 → GOOG`, `02079K305 → GOOGL` 정확 분리 |
| `company_tickers.json` | HTTP 200, 796,564 bytes |
| browse-edgar Atom 피드 | HTTP 200, 정상 파싱 |
| `Archives/edgar/daily-index/` | HTTP 200 |

**Q1 2026 실측 포트폴리오** (검증용 기준 데이터, 단위 USD)

| 종목 | CUSIP | 평가액 | 주식 수 | 비중 |
|---|---|---:|---:|---:|
| Brookfield Corp | 11271J107 | 2,415,946,008 | 59,697,208 | 17.6% |
| Amazon.com | 023135106 | 2,385,104,083 | 11,451,981 | 17.4% |
| Uber Technologies | 90353T100 | 2,154,934,398 | 29,958,771 | 15.7% |
| Microsoft | 594918104 | 2,092,970,053 | 5,654,078 | 15.3% |
| Restaurant Brands Intl | 76131D103 | 1,673,501,194 | 22,645,483 | 12.2% |
| Meta Platforms | 30303M102 | 1,522,358,404 | 2,660,861 | 11.1% |
| Howard Hughes Holdings | 44267T102 | 1,192,581,569 | 18,852,064 | 8.7% |
| Seaport Entertainment | 812215200 | 107,910,794 | 5,023,780 | 0.8% |
| Alphabet Cl C | 02079K107 | 89,421,720 | 311,726 | 0.7% |
| Hertz Global Holdings | 42806J700 | 70,261,595 | 15,241,127 | 0.5% |
| Alphabet Cl A | 02079K305 | 9,310,043 | 32,376 | 0.1% |
| **합계** | | **13,714,299,861** | | **100%** |

Top-4 집중도 66.0%, 포지션 11개 — 극단적 집중형 포트폴리오임을 확인할 수 있습니다.

---

## 부록 B — 핵심 파서 참조 구현

```python
import json, time, re
import xml.etree.ElementTree as ET
import requests

UA = {"User-Agent": "AckmanTracker younggil jinhae8971@gmail.com"}
CIK = "0001336528"
BASE = "https://www.sec.gov/Archives/edgar/data/1336528"

def get(url: str, retries: int = 3):
    for i in range(retries):
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 200:
            time.sleep(0.34)              # < 3 req/s
            return r
        if r.status_code in (403, 429):
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
    raise RuntimeError(f"failed after {retries}: {url}")

def ns_of(root):
    t = root.tag
    return {"n": t[1: t.index("}")]} if t.startswith("{") else {}

def parse_13f(accession: str) -> dict:
    acc = accession.replace("-", "")
    idx = get(f"{BASE}/{acc}/index.json").json()
    names = [i["name"] for i in idx["directory"]["item"]]
    info_name = next(n for n in names
                     if n.lower().endswith(".xml") and "primary" not in n.lower())

    primary = get(f"{BASE}/{acc}/primary_doc.xml").text
    m = re.search(r"<schemaVersion>(.*?)</schemaVersion>", primary)
    schema = m.group(1) if m else None
    am = re.search(r"<amendmentType>(.*?)</amendmentType>", primary)
    # RESTATEMENT | NEW HOLDINGS | None(원본)