"""Pipeline — 오케스트레이션 / 무결성 게이트 / 알림.

이 패키지는 로직을 갖지 않는다. collector·analytics 의 진입점을 호출해 조립하고,
그 결과를 검증(gate)하고 보고(notify)하는 역할만 한다.

  python -m src.pipeline.run --mode {incremental|backfill|events-only}
  python -m src.pipeline.gate            # 게이트 자체 단위 테스트
  python -m src.pipeline.notify --demo   # 합성 이벤트로 마크다운 렌더
  python -m src.pipeline._selftest       # 전체 자체 점검
"""

__all__ = ["gate", "notify", "run"]
