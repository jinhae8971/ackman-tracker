"""추적 대상 엔티티(운용사) 레지스트리.

단일 엔티티 가정(Pershing Square)을 걷어내고, 13F 를 제출하는 어떤
운용사든 같은 파이프라인으로 처리할 수 있게 만든다.

바인딩 방식
-----------
`TRACKER_ENTITY` 환경변수를 `schema.py` 임포트 시점에 읽어 활성 엔티티를
결정한다. 40여 개 함수에 `entity` 인자를 관통시키는 대신 프로세스 단위로
고정하는 쪽을 택했다. `run.py` 는 각 스테이지를 서브프로세스로 띄우므로
환경변수가 그대로 전파된다.

수집 정책
---------
운용사마다 13F 의 성격이 완전히 다르다.

  Pershing  ~11종목  / $13.7B  — 고확신 집중 포트폴리오
  Berkshire ~29종목  / $263B   — 고확신 집중 (단, 복수 매니저 중복 행 존재)
  Citadel   6,733종목/ $618B   — 마켓메이커. 옵션 7,304행 포함, 분기당 7.85MB

Citadel 을 그대로 적재하면 저장소가 감당하지 못하고, Top-10 비중 21.7% 인
포트폴리오에 '확신 매수' 같은 해석을 붙이는 것 자체가 오독이다. 그래서
`exclude_options` / `max_positions` 정책으로 잘라내되, 잘라낸 사실을
`coverage.jsonl` 에 남기고 대시보드에 명시한다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

ENV_VAR = "TRACKER_ENTITY"
DEFAULT_KEY = "pershing"


@dataclass(frozen=True)
class Entity:
    key: str                       # 디렉터리/파일명에 쓰는 슬러그
    cik: str                       # 10자리 제로패딩 CIK
    name: str                      # EDGAR 등록 법인명
    display: str                   # 대시보드 표기명
    manager: str                   # 대표 운용역
    profile: str                   # conviction | market_maker
    color: str                     # UI 강조색
    blurb: str = ""

    # --- 수집 정책 -----------------------------------------------------
    exclude_options: bool = False  # PUT/CALL 행 제외
    max_positions: Optional[int] = None   # 가치 상위 N 종목만 보존
    default_backfill: Optional[int] = None  # 기본 백필 분기 수 (None=전량)

    # --- 알림 정책 -----------------------------------------------------
    # STRONG('고확신 변화') Issue 를 낼 것인가. 마켓메이커는 분기당 90건 규모의
    # STRONG 이 잡히는데, 그 대부분이 헤지·차익거래의 잔여물이라 '확신'이라는
    # 라벨 자체가 오독이다. 알림을 끄는 것은 데이터를 숨기는 것이 아니라
    # 의미 없는 신호로 알림 채널을 마비시키지 않기 위한 것이다 — 수집·저장·
    # 대시보드 표시는 그대로 유지된다.
    alert_strong: bool = True

    # --- 검증 기준선 (report_date -> (종목수, 총액 USD)) ----------------
    golden: dict = field(default_factory=dict)

    @property
    def cik_short(self) -> str:
        return self.cik.lstrip("0")

    @property
    def archive_base(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{self.cik_short}"

    @property
    def submissions_url(self) -> str:
        return f"https://data.sec.gov/submissions/CIK{self.cik}.json"

    @property
    def truncated(self) -> bool:
        """포트폴리오 일부만 보존하는 엔티티인가."""
        return bool(self.exclude_options or self.max_positions)


PERSHING = Entity(
    key="pershing",
    cik="0001336528",
    name="Pershing Square Capital Management, L.P.",
    display="Pershing Square",
    manager="Bill Ackman",
    profile="conviction",
    color="#2563eb",
    blurb="10종목 안팎에 집중하는 액티비스트 헤지펀드. 편입 자체가 시그널이다.",
    golden={
        "2026-03-31": (11, 13_714_299_861),
        "2024-12-31": (11, 12_661_093_451),
        "2022-09-30": (6, 7_877_045_000),
    },
)

BERKSHIRE = Entity(
    key="berkshire",
    cik="0001067983",
    name="BERKSHIRE HATHAWAY INC",
    display="Berkshire Hathaway",
    manager="Warren Buffett",
    profile="conviction",
    color="#b45309",
    blurb="복수 매니저(버핏·콤스·웨슐러)가 함께 보고한다. 동일 종목이 여러 행으로 나뉘어 합산이 필수다.",
    default_backfill=20,
)

CITADEL = Entity(
    key="citadel",
    cik="0001423053",
    name="CITADEL ADVISORS LLC",
    display="Citadel Advisors",
    manager="Ken Griffin",
    profile="market_maker",
    color="#0f766e",
    blurb="마켓메이커 성격의 멀티전략 운용사. 6,000종목 이상을 보유하며 옵션 비중이 높아 개별 종목 해석은 신중해야 한다.",
    exclude_options=True,
    max_positions=200,
    default_backfill=20,
    alert_strong=False,
)

ORDER = ["pershing", "berkshire", "citadel"]
REGISTRY = {e.key: e for e in (PERSHING, BERKSHIRE, CITADEL)}


def all_entities() -> list[Entity]:
    return [REGISTRY[k] for k in ORDER]


def get(key: str) -> Entity:
    slug = (key or "").strip().lower()
    if slug not in REGISTRY:
        raise KeyError(
            f"알 수 없는 엔티티 '{key}'. 사용 가능: {', '.join(ORDER)}")
    return REGISTRY[slug]


def active() -> Entity:
    """`TRACKER_ENTITY` 가 가리키는 엔티티. 미설정 시 Pershing."""
    return get(os.environ.get(ENV_VAR) or DEFAULT_KEY)
