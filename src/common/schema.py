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

# ---------------------------------------------------------------- 상수

CIK = "0001336528"
CIK_SHORT = "1336528"
ENTITY_NAME = "Pershing Square Capital Management, L.P."
ARCHIVE_BASE = f"https://www.sec.gov/Archives/edgar/data/{CIK_SHORT}"
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
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
    def key(self) -> tuple[str, str]:
        """포지션 동일성 키. Alphabet 처럼 복수 클래스 분리를 위해 클래스 포함."""
        return (self.cusip, self.title_of_class)


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
    event_id: str             # {report_date}:{cusip}:{event_type} — 결정적 ID
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
    """모든 모듈이 사용하는 경로. 루트는 리포지토리 루트 기준."""
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    DATA = os.path.join(ROOT, "data")
    RAW = os.path.join(DATA, "raw")
    NORMALIZED = os.path.join(DATA, "normalized")
    REFERENCE = os.path.join(DATA, "reference")
    STATE = os.path.join(DATA, "state")

    HOLDINGS = os.path.join(NORMALIZED, "holdings.jsonl")
    FILINGS = os.path.join(NORMALIZED, "filings.jsonl")
    EVENTS = os.path.join(NORMALIZED, "events.jsonl")
    METRICS = os.path.join(NORMALIZED, "metrics.jsonl")

    CUSIP_MAP = os.path.join(REFERENCE, "cusip_map.json")
    LAST_SEEN = os.path.join(STATE, "last_seen.json")

    DASHBOARD_DATA = os.path.join(ROOT, "dashboard", "dashboard_data.json")
    DASHBOARD_HTML = os.path.join(ROOT, "dashboard", "index.html")


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


def make_event_id(report_date: str, cusip: str, event_type: str) -> str:
    return f"{report_date}:{cusip}:{event_type}"


def edgar_period_to_iso(period: str) -> str:
    """13F primary_doc 의 MM-DD-YYYY 를 YYYY-MM-DD 로 변환."""
    mm, dd, yyyy = period.split("-")
    return f"{yyyy}-{mm}-{dd}"
