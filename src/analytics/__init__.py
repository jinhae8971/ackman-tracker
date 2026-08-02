"""Analytics 모듈 — 분기 diff, 확신도 등급, 포트폴리오 지표.

Collector 산출물(holdings.jsonl)을 읽기만 하는 순수 계산 계층이다.
네트워크 호출을 하지 않으므로 전 함수가 픽스처만으로 테스트 가능하다.
"""
from .diff import adjust_splits, classify_conviction, position_key, quarter_diff
from .metrics import holding_periods, lag_days, quarter_metrics, turnover_pct

__all__ = [
    "adjust_splits",
    "classify_conviction",
    "position_key",
    "quarter_diff",
    "holding_periods",
    "lag_days",
    "quarter_metrics",
    "turnover_pct",
]
