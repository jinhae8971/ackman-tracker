"""공유 데이터 계약 (Contract).

이 파일은 모든 모듈이 의존하는 단일 진실 공급원이다.
collector / analytics / pipeline / dashboard 는 이 스키마를 통해서만 통신한다.
필드 추가는 가능하나 기존 필드의 이름·타입·의미 변경은 금지 (breaking change).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Literal, Optional
import json
import os

from . import entities

# ---------------------------------------------------------------- 상수

# 활성 엔티티는 프로세스 시작 시 `TRACKER_ENTITY` 로 한 번만 결정된다.
# 이 모듈을 임포트한 모든 코드는 자동으로 같은 엔티티를 바라본다.
ENTITY = entities.active()

CIK = ENTITY.cik
CIK_SHORT = ENTITY.cik_short
ENTITY_KEY = ENTITY.key
ENTITY_NAME = ENTITY.name
ENTITY_DISPLAY = ENTITY.display
ARCHIVE_BASE = ENTITY.archive_base
SUBMISSIONS_URL = ENTITY.submissions_url
GOLDEN = ENTITY.golden
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# SEC 요구사항: User-Agent 없으면 403. 초당 10요청 제한 -> 자체 3req/s.
USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "AckmanTracker younggil jinhae8971@gmail.com"
)
REQUEST_INTERVAL_SEC = 0.34

# 변화 감지 임계값 (docs/architecture.md §5.1)
MIN_DELTA_PCT = 1.0          # 이하 변동은 HOLD 로 간주 (노이즈 억제)
STRONG_NEW_WEIGHT_PCT = 5.0  # 신규 진입 비중 이 이상이면 STRONG_NEW
STRONG_DELTA_BP = 200        # 비중 변화 200bp 이상이면 STRONG_ADD / STRONG_TRIM

# 무결성 게이트 (docs/architecture.md §6.3)
MAX_TOTAL_SWING_PCT = 80.0   # 전분기 대비 총액 변동이 이를 넘으면 파이프라인 실패
# 엔티티가 근거와 함께 허용치를 올릴 수 있다(entities.Entity.max_total_swing_pct).
# 게이트는 상수가 아니라 이 값을 봐야 한다 — 신생 펀드는 정상적으로 배로 큰다.
ENTITY_MAX_TOTAL_SWING_PCT = (
    ENTITY.max_total_swing_pct
    if ENTITY.max_total_swing_pct is not None else MAX_TOTAL_SWING_PCT)

EventType = Literal["NEW", "ADD", "TRIM", "EXIT", "HOLD"]
Conviction = Literal["STRONG_NEW", "STRONG_ADD", "STRONG_TRIM", "FULL_EXIT", "ROUTINE"]
AmendmentType = Optional[Literal["RESTATEMENT", "NEW HOLDINGS"]]


# ---------------------------------------------------------------- 레코드

@dataclass
class Holding:
    """정규화된 단일 포지션. data/normalized/holdings.jsonl 한 행."""
    report_date: str          # YYYY-MM-DD (분기말 기준일)
    filing_date: str          # YYYY-MM-DD (실제 제출일)
    accession: str            # 0001172661-26-002336
    form_type: str            # 13F-HR | 13F-HR/A
    amendment_type: AmendmentType
    cusip: str
    issuer_name: str
    title_of_class: str
    value_usd: int            # 반드시 달러 단위로 정규화된 값
    shares: int
    share_type: str           # SH | PRN
    put_call: Optional[str]   # PUT | CALL | None
    discretion: str           # SOLE | DEFINED | OTHER
    weight_pct: float
    ticker: Optional[str] = None
    figi: Optional[str] = None
    schema_version: Optional[str] = None   # X0202 등. 단위 정규화 감사용

    @property
    def key(self) -> tuple[str, str, str]:
        """포지션 동일성 키.

        Alphabet 처럼 복수 클래스를 분리하려면 title_of_class 가 필요하고,
        Citadel 처럼 같은 종목의 보통주·PUT·CALL 을 동시에 보고하는 운용사를
        다루려면 put_call 이 필요하다. 셋 중 하나라도 빠지면 서로 다른
        포지션이 하나로 뭉개진다.
        """
        return (self.cusip, self.title_of_class, self.put_call or "")


@dataclass
class Filing:
    """파일링 메타. data/normalized/filings.jsonl 한 행."""
    accession: str
    form_type: str
    filing_date: str
    report_date: Optional[str]
    primary_document: str
    url: str
    amendment_type: AmendmentType = None
    schema_version: Optional[str] = None
    entry_total: Optional[int] = None      # tableEntryTotal
    value_total_raw: Optional[int] = None  # tableValueTotal (정규화 이전 원시값)
    items: Optional[str] = None            # 13D/8-K 등의 item 코드


@dataclass
class Event:
    """감지된 변화. data/normalized/events.jsonl 한 행."""
    event_id: str             # {report_date}:{cusip}:{event_type}[:PUT|:CALL] — 결정적 ID
    report_date: str
    filing_date: str
    event_type: EventType
    conviction: Conviction
    cusip: str
    ticker: Optional[str]
    issuer_name: str
    prev_shares: int
    curr_shares: int
    share_delta_pct: Optional[float]
    prev_value_usd: int
    curr_value_usd: int
    value_delta_usd: int
    weight_before: float
    weight_after: float
    weight_delta_bp: float
    put_call: Optional[str] = None  # PUT | CALL | None(보통주). 옵션 포지션은
                                    # '확신 매수'로 읽으면 안 되므로 별도 표기한다.
    source: str = "13F"       # 13F | 13D | 13G | FORM4
    provisional: bool = False # 13D 기반 조기 이벤트는 True


@dataclass
class QuarterMetrics:
    """분기 포트폴리오 지표. dashboard_data.json 에 포함."""
    report_date: str
    filing_date: str
    lag_days: int
    total_value_usd: int
    position_count: int
    hhi: float                # Σ(weight_i/100)^2, 0~1
    top1_pct: float
    top3_pct: float
    top5_pct: float
    turnover_pct: Optional[float] = None
    new_count: int = 0
    exit_count: int = 0
    add_count: int = 0
    trim_count: int = 0


# ---------------------------------------------------------------- JSONL I/O

def write_jsonl(path: str, records: list, sort_key=None) -> int:
    """레코드 목록을 JSONL 로 원자적 기록. dataclass/dict 모두 허용."""
    rows = [asdict(r) if hasattr(r, "__dataclass_fields__") else r for r in records]
    if sort_key:
        rows.sort(key=sort_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return len(rows)


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 경로

class Paths:
    """모든 모듈이 사용하는 경로. 루트는 리포지토리 루트 기준.

    엔티티별 산출물은 `data/normalized/{key}/`, `data/raw/{key}/`,
    `data/state/{key}.json` 으로 분리한다. CUSIP -> 티커 매핑만은
    엔티티와 무관한 사실이므로 `data/reference/` 아래에서 공유한다.
    """
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    DATA = os.path.join(ROOT, "data")
    RAW_ROOT = os.path.join(DATA, "raw")
    NORMALIZED_ROOT = os.path.join(DATA, "normalized")
    REFERENCE = os.path.join(DATA, "reference")
    STATE = os.path.join(DATA, "state")
    DASHBOARD = os.path.join(ROOT, "dashboard")
    DASHBOARD_DATA_DIR = os.path.join(DASHBOARD, "data")

    # --- 엔티티 무관 (공유) ---
    CUSIP_MAP = os.path.join(REFERENCE, "cusip_map.json")
    DASHBOARD_INDEX = os.path.join(DASHBOARD_DATA_DIR, "index.json")
    DASHBOARD_COMPARE = os.path.join(DASHBOARD_DATA_DIR, "compare.json")
    DASHBOARD_HTML = os.path.join(DASHBOARD, "index.html")

    # --- 활성 엔티티 ---
    RAW = os.path.join(RAW_ROOT, ENTITY_KEY)
    NORMALIZED = os.path.join(NORMALIZED_ROOT, ENTITY_KEY)
    HOLDINGS = os.path.join(NORMALIZED, "holdings.jsonl")
    FILINGS = os.path.join(NORMALIZED, "filings.jsonl")
    EVENTS = os.path.join(NORMALIZED, "events.jsonl")
    METRICS = os.path.join(NORMALIZED, "metrics.jsonl")
    COVERAGE = os.path.join(NORMALIZED, "coverage.jsonl")
    LAST_SEEN = os.path.join(STATE, f"{ENTITY_KEY}.json")
    DASHBOARD_DATA = os.path.join(DASHBOARD_DATA_DIR, f"{ENTITY_KEY}.json")

    @classmethod
    def for_entity(cls, key: str) -> dict:
        """다른 엔티티의 경로 묶음. 대시보드 빌더처럼 한 프로세스에서
        여러 엔티티를 읽어야 할 때만 쓴다."""
        entity = entities.get(key)
        normalized = os.path.join(cls.NORMALIZED_ROOT, entity.key)
        return {
            "entity": entity,
            "raw": os.path.join(cls.RAW_ROOT, entity.key),
            "normalized": normalized,
            "holdings": os.path.join(normalized, "holdings.jsonl"),
            "filings": os.path.join(normalized, "filings.jsonl"),
            "events": os.path.join(normalized, "events.jsonl"),
            "metrics": os.path.join(normalized, "metrics.jsonl"),
            "coverage": os.path.join(normalized, "coverage.jsonl"),
            "last_seen": os.path.join(cls.STATE, f"{entity.key}.json"),
            "dashboard_data": os.path.join(cls.DASHBOARD_DATA_DIR,
                                           f"{entity.key}.json"),
        }


# ---------------------------------------------------------------- 유틸

def normalize_value(raw_value: int, schema_version: Optional[str]) -> int:
    """13F value 를 달러 단위로 정규화.

    2023-02 이전 구 스키마는 천 달러 단위, X0202 이후는 달러 단위.
    실측: 0001172661-22-002568 sum=7,877,045(=$7.88B) vs
          0001172661-23-000673 sum=8,784,004,892(=$8.78B)
    """
    if schema_version and schema_version >= "X0202":
        return raw_value
    return raw_value * 1000


def make_event_id(report_date: str, cusip: str, event_type: str,
                  put_call: Optional[str] = None) -> str:
    """결정적 이벤트 ID.

    put_call 이 키에 들어가는 이유
    -----------------------------
    같은 CUSIP 을 보통주와 PUT/CALL 로 동시에 보유하는 운용사가 있다
    (Situational Awareness: NVDA 보통주 2,855주 + PUT $1.57B). 이 셋은
    diff.position_key 기준으로 이미 별개 포지션이므로, ID 에 put_call 이
    없으면 한 분기에 같은 ID 가 두세 번 생겨 DUP_EVENT_ID 로 게이트가 막힌다.

    보통주(put_call=None)는 접미가 붙지 않는다. 기존 3사의 event_id 는
    바이트 단위로 그대로 유지된다.
    """
    suffix = f":{put_call.upper()}" if put_call else ""
    return f"{report_date}:{cusip}:{event_type}{suffix}"


def edgar_period_to_iso(period: str) -> str:
    """13F primary_doc 의 MM-DD-YYYY 를 YYYY-MM-DD 로 변환."""
    mm, dd, yyyy = period.split("-")
    return f"{yyyy}-{mm}-{dd}"
