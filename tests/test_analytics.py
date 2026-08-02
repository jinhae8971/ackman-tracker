"""Analytics 모듈 테스트 — 표준 라이브러리 unittest 만 사용 (pytest 불필요).

    python3 tests/test_analytics.py
    python3 -m unittest tests.test_analytics -v

Collector 가 아직 holdings.jsonl 을 만들지 않았을 수 있으므로 전 케이스가
합성 픽스처로 동작한다. 실제 파일이 있으면 마지막 클래스가 추가로 검증한다.
"""
from __future__ import annotations

import filecmp
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.analytics import build_events                                  # noqa: E402
from src.analytics.diff import (                                        # noqa: E402
    adjust_splits, classify_conviction, position_key, quarter_diff,
)
from src.analytics.metrics import (                                     # noqa: E402
    holding_periods, lag_days, quarter_metrics, turnover_pct,
)
from src.common import schema                                           # noqa: E402
from src.common.schema import MIN_DELTA_PCT, Paths, read_jsonl, write_jsonl  # noqa: E402

Q1, Q2 = "2025-12-31", "2026-03-31"
F1, F2 = "2026-02-14", "2026-05-15"


def h(cusip, name, value, shares, weight, *, title="COM", report=Q2, filing=F2,
      ticker=None, put_call=None):
    """holdings.jsonl 한 행에 해당하는 합성 픽스처."""
    return {
        "report_date": report, "filing_date": filing,
        "accession": "0001172661-26-000001", "form_type": "13F-HR",
        "amendment_type": None,
        "cusip": cusip, "issuer_name": name, "title_of_class": title,
        "value_usd": int(value), "shares": int(shares), "share_type": "SH",
        "put_call": put_call, "discretion": "SOLE",
        "weight_pct": float(weight), "ticker": ticker, "figi": None,
        "schema_version": "X0202",
    }


def reweight(rows):
    """value_usd 기준으로 weight_pct 를 재계산한 새 목록."""
    total = sum(r["value_usd"] for r in rows)
    return [dict(r, weight_pct=round(r["value_usd"] / total * 100, 4)) for r in rows]


def by_cusip(events):
    return {e.cusip: e for e in events}


# ====================================================================== diff

class TestEventClassification(unittest.TestCase):
    """NEW / ADD / TRIM / EXIT / HOLD 기본 분류."""

    def setUp(self):
        self.prev = reweight([
            h("023135106", "AMAZON COM INC", 2_000_000_000, 10_000_000, 0, report=Q1, filing=F1),
            h("594918104", "MICROSOFT CORP", 1_000_000_000, 3_000_000, 0, report=Q1, filing=F1),
            h("30303M102", "META PLATFORMS INC", 1_000_000_000, 2_000_000, 0, report=Q1, filing=F1),
            h("42806J700", "HERTZ GLOBAL HLDGS", 500_000_000, 100_000_000, 0, report=Q1, filing=F1),
        ])
        self.curr = reweight([
            h("023135106", "AMAZON COM INC", 2_400_000_000, 12_000_000, 0),   # ADD +20%
            h("594918104", "MICROSOFT CORP", 700_000_000, 2_000_000, 0),      # TRIM -33%
            h("30303M102", "META PLATFORMS INC", 1_100_000_000, 2_000_000, 0),  # HOLD (주식수 동일)
            h("90353T100", "UBER TECHNOLOGIES INC", 900_000_000, 20_000_000, 0),  # NEW
            # HERTZ 없음 -> EXIT
        ])
        self.ev = by_cusip(quarter_diff(self.prev, self.curr))

    def test_all_five_types(self):
        self.assertEqual(self.ev["023135106"].event_type, "ADD")
        self.assertEqual(self.ev["594918104"].event_type, "TRIM")
        self.assertEqual(self.ev["30303M102"].event_type, "HOLD")
        self.assertEqual(self.ev["90353T100"].event_type, "NEW")
        self.assertEqual(self.ev["42806J700"].event_type, "EXIT")
        self.assertEqual(len(self.ev), 5)

    def test_new_fields(self):
        e = self.ev["90353T100"]
        self.assertEqual(e.prev_shares, 0)
        self.assertEqual(e.curr_shares, 20_000_000)
        self.assertIsNone(e.share_delta_pct)          # 분모 없음
        self.assertEqual(e.prev_value_usd, 0)
        self.assertEqual(e.value_delta_usd, 900_000_000)
        self.assertEqual(e.weight_before, 0.0)
        self.assertGreater(e.weight_after, 0)

    def test_exit_fields(self):
        e = self.ev["42806J700"]
        self.assertEqual(e.curr_shares, 0)
        self.assertEqual(e.curr_value_usd, 0)
        self.assertEqual(e.share_delta_pct, -100.0)
        self.assertEqual(e.weight_after, 0.0)
        self.assertEqual(e.value_delta_usd, -500_000_000)
        # EXIT 은 curr 행이 없으므로 분기 대표 report_date/filing_date 를 쓴다
        self.assertEqual(e.report_date, Q2)
        self.assertEqual(e.filing_date, F2)

    def test_add_delta_math(self):
        e = self.ev["023135106"]
        self.assertAlmostEqual(e.share_delta_pct, 20.0, places=4)
        self.assertEqual(e.value_delta_usd, 400_000_000)

    def test_trim_delta_math(self):
        e = self.ev["594918104"]
        self.assertAlmostEqual(e.share_delta_pct, -33.3333, places=3)

    def test_event_id_is_deterministic(self):
        e = self.ev["023135106"]
        self.assertEqual(e.event_id, f"{Q2}:023135106:ADD")
        self.assertEqual(e.event_id, schema.make_event_id(Q2, "023135106", "ADD"))
        again = by_cusip(quarter_diff(self.prev, self.curr))
        self.assertEqual([x.event_id for x in quarter_diff(self.prev, self.curr)],
                         [again[c].event_id for c in
                          [x.cusip for x in quarter_diff(self.prev, self.curr)]])

    def test_inputs_not_mutated(self):
        snapshot = [dict(r) for r in self.prev]
        quarter_diff(self.prev, self.curr)
        self.assertEqual(self.prev, snapshot)


class TestThreshold(unittest.TestCase):
    """MIN_DELTA_PCT(1%) 경계값 — 0.9% 는 HOLD, 1.1% 는 ADD."""

    def _delta(self, pct):
        prev = reweight([h("023135106", "AMAZON COM INC", 1_000_000_000, 1_000_000, 0,
                           report=Q1, filing=F1),
                         h("594918104", "MICROSOFT CORP", 1_000_000_000, 1_000_000, 0,
                           report=Q1, filing=F1)])
        curr_shares = int(round(1_000_000 * (1 + pct / 100)))
        curr = reweight([h("023135106", "AMAZON COM INC", 1_000_000_000, curr_shares, 0),
                         h("594918104", "MICROSOFT CORP", 1_000_000_000, 1_000_000, 0)])
        return by_cusip(quarter_diff(prev, curr))["023135106"]

    def test_min_delta_is_one_percent(self):
        self.assertEqual(MIN_DELTA_PCT, 1.0)

    def test_below_threshold_is_hold(self):
        self.assertEqual(self._delta(0.9).event_type, "HOLD")

    def test_above_threshold_is_add(self):
        self.assertEqual(self._delta(1.1).event_type, "ADD")

    def test_exactly_at_threshold_is_hold(self):
        """계약: '이하 변동은 HOLD' — 1.0% 정확히는 HOLD."""
        self.assertEqual(self._delta(1.0).event_type, "HOLD")

    def test_negative_boundaries(self):
        self.assertEqual(self._delta(-0.9).event_type, "HOLD")
        self.assertEqual(self._delta(-1.1).event_type, "TRIM")


class TestSplitAdjustment(unittest.TestCase):
    """주식 분할이 ADD/TRIM 으로 오탐되지 않아야 한다."""

    def test_two_for_one_split_is_hold_not_add(self):
        prev = reweight([h("594918104", "MICROSOFT CORP", 1_000_000_000, 5_000_000, 0,
                           report=Q1, filing=F1)])
        curr = reweight([h("594918104", "MICROSOFT CORP", 1_020_000_000, 10_000_000, 0)])
        ev = by_cusip(quarter_diff(prev, curr))["594918104"]
        self.assertEqual(ev.event_type, "HOLD")
        self.assertEqual(ev.prev_shares, 10_000_000)   # 현재 기준으로 환산됨

    def test_adjust_splits_returns_corrected_prev(self):
        prev = [h("594918104", "MICROSOFT CORP", 1_000_000_000, 5_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("594918104", "MICROSOFT CORP", 1_020_000_000, 10_000_000, 100)]
        out = adjust_splits(prev, curr)
        self.assertEqual(out[0]["shares"], 10_000_000)
        self.assertEqual(out[0]["split_factor"], 2.0)
        self.assertEqual(prev[0]["shares"], 5_000_000)  # 원본 불변

    def test_ten_for_one_split(self):
        prev = [h("90353T100", "UBER TECHNOLOGIES INC", 900_000_000, 2_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("90353T100", "UBER TECHNOLOGIES INC", 880_000_000, 20_000_000, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 20_000_000)
        self.assertEqual(quarter_diff(prev, curr)[0].event_type, "HOLD")

    def test_reverse_split_is_hold_not_trim(self):
        """1:10 역분할 — 주식 수가 1/10 이지만 평가액은 유지."""
        prev = [h("42806J700", "HERTZ GLOBAL HLDGS", 500_000_000, 100_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("42806J700", "HERTZ GLOBAL HLDGS", 495_000_000, 10_000_000, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 10_000_000)
        self.assertEqual(quarter_diff(prev, curr)[0].event_type, "HOLD")

    def test_genuine_doubling_is_still_add(self):
        """주식 수 2배 + 평가액 2배 = 실제 매수. 분할로 오인하면 안 된다."""
        prev = [h("023135106", "AMAZON COM INC", 1_000_000_000, 5_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("023135106", "AMAZON COM INC", 2_000_000_000, 10_000_000, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 5_000_000)
        ev = quarter_diff(prev, curr)[0]
        self.assertEqual(ev.event_type, "ADD")
        self.assertAlmostEqual(ev.share_delta_pct, 100.0, places=4)

    def test_ratio_outside_tolerance_is_not_split(self):
        """+2.5배 처럼 정수배에서 벗어나면 분할이 아니다."""
        prev = [h("023135106", "AMAZON COM INC", 1_000_000_000, 4_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("023135106", "AMAZON COM INC", 1_010_000_000, 10_000_000, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 4_000_000)
        self.assertEqual(quarter_diff(prev, curr)[0].event_type, "ADD")

    def test_split_within_two_percent_tolerance(self):
        """2:1 분할 직후 소량 매수(+1.5%) — 분할 보정 후 잔여분만 남는다."""
        prev = [h("023135106", "AMAZON COM INC", 1_000_000_000, 5_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("023135106", "AMAZON COM INC", 1_015_000_000, 10_150_000, 100)]
        ev = quarter_diff(prev, curr)[0]
        self.assertEqual(ev.prev_shares, 10_000_000)
        self.assertEqual(ev.event_type, "ADD")
        self.assertAlmostEqual(ev.share_delta_pct, 1.5, places=4)

    def test_sold_half_into_rally_is_trim_not_reverse_split(self):
        """'절반 매도' + 주가 상승 = 주식수 0.5배·평가액 유지. 1:2 역분할과 신호가
        같지만, 흔한 재량 매매를 삼키지 않도록 1:2 역분할은 인정하지 않는다."""
        prev = [h("023135106", "AMAZON COM INC", 1_000_000_000, 10_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("023135106", "AMAZON COM INC", 1_000_000_000, 5_000_000, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 10_000_000)
        self.assertEqual(quarter_diff(prev, curr)[0].event_type, "TRIM")

    def test_large_value_swing_blocks_split_adjustment(self):
        """주식 수는 10배인데 평가액도 10배 -> 매수. 드리프트 밴드 밖."""
        prev = [h("90353T100", "UBER TECHNOLOGIES INC", 100_000_000, 2_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("90353T100", "UBER TECHNOLOGIES INC", 1_000_000_000, 20_000_000, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 2_000_000)
        self.assertEqual(quarter_diff(prev, curr)[0].event_type, "ADD")

    def test_zero_prev_shares_never_adjusted(self):
        prev = [h("A", "A", 0, 0, 100, report=Q1, filing=F1)]
        curr = [h("A", "A", 1_000, 100, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 0)

    def test_chipotle_fifty_for_one_is_not_a_3773pct_add(self):
        """실데이터 회귀: CMG 2024-06 50:1 분할 (holdings.jsonl 실측값).

        주식 수 743,984 -> 28,815,165 인데 평가액은 오히려 -16.5%.
        SPLIT_MAX_FACTOR 가 20 이던 시절 이 행은 +3773% ADD 로 새어나왔다.
        분할과 ~22% 매도가 겹쳐 배수가 39 로 추정되므로 결과는 HOLD 다
        (정확히는 TRIM 이지만, 오탐 ADD 보다 낛다는 의도적 선택).
        """
        prev = [h("169656105", "CHIPOTLE MEXICAN GRILL INC", 2_162_590_372,
                  743_984, 100, report=Q1, filing=F1)]
        curr = [h("169656105", "CHIPOTLE MEXICAN GRILL INC", 1_805_270_087,
                  28_815_165, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 29_015_376)
        ev = quarter_diff(prev, curr)[0]
        self.assertEqual(ev.event_type, "HOLD")
        self.assertNotEqual(ev.conviction, "STRONG_ADD")

    def test_allergan_style_massive_buy_still_add(self):
        """실데이터 회귀: ALLERGAN 2014-06 은 주식 수 x48 이지만 평가액도 x66.

        상한을 50 으로 올려도 평가액 밴드가 막아 ADD 로 남아야 한다.
        """
        prev = [h("018490102", "ALLERGAN INC", 74_141_000, 597_431, 100,
                  report=Q1, filing=F1)]
        curr = [h("018490102", "ALLERGAN INC", 4_886_826_000, 28_878_538, 100)]
        self.assertEqual(adjust_splits(prev, curr)[0]["shares"], 597_431)
        self.assertEqual(quarter_diff(prev, curr)[0].event_type, "ADD")

    def test_split_emits_no_separate_event(self):
        prev = [h("594918104", "MICROSOFT CORP", 1_000_000_000, 5_000_000, 100,
                  report=Q1, filing=F1)]
        curr = [h("594918104", "MICROSOFT CORP", 1_020_000_000, 10_000_000, 100)]
        events = quarter_diff(prev, curr)
        self.assertEqual(len(events), 1)
        self.assertNotIn("SPLIT", [e.event_type for e in events])


class TestShareClassSeparation(unittest.TestCase):
    """Alphabet Cl A / Cl C 는 별개 포지션 (CUSIP 단독 키 금지)."""

    GOOGL = "02079K305"   # Cl A
    GOOG = "02079K107"    # Cl C

    def test_keys_are_cusip_plus_class(self):
        a = h(self.GOOGL, "ALPHABET INC", 9_310_043, 32_376, 0.1,
              title="CAP STK CL A", ticker="GOOGL")
        c = h(self.GOOG, "ALPHABET INC", 89_421_720, 311_726, 0.7,
              title="CAP STK CL C", ticker="GOOG")
        self.assertNotEqual(position_key(a), position_key(c))
        self.assertEqual(position_key(a), (self.GOOGL, "CAP STK CL A"))

    def test_two_classes_produce_two_events(self):
        prev = reweight([
            h(self.GOOG, "ALPHABET INC", 80_000_000, 300_000, 0,
              title="CAP STK CL C", ticker="GOOG", report=Q1, filing=F1),
            h("023135106", "AMAZON COM INC", 2_000_000_000, 10_000_000, 0,
              report=Q1, filing=F1),
        ])
        curr = reweight([
            h(self.GOOG, "ALPHABET INC", 89_421_720, 311_726, 0,
              title="CAP STK CL C", ticker="GOOG"),
            h(self.GOOGL, "ALPHABET INC", 9_310_043, 32_376, 0,
              title="CAP STK CL A", ticker="GOOGL"),
            h("023135106", "AMAZON COM INC", 2_000_000_000, 10_000_000, 0),
        ])
        ev = by_cusip(quarter_diff(prev, curr))
        self.assertEqual(len(ev), 3)
        self.assertEqual(ev[self.GOOG].event_type, "ADD")     # +3.9%
        self.assertEqual(ev[self.GOOGL].event_type, "NEW")    # 신규 클래스
        self.assertEqual(ev[self.GOOGL].ticker, "GOOGL")
        self.assertEqual(ev[self.GOOG].ticker, "GOOG")

    def test_same_cusip_different_class_not_merged(self):
        """동일 CUSIP 이라도 클래스가 다르면 합쳐지지 않는다."""
        prev = [h("11271J107", "BROOKFIELD CORP", 1_000_000_000, 30_000_000, 100,
                  title="CL A", report=Q1, filing=F1)]
        curr = [h("11271J107", "BROOKFIELD CORP", 1_000_000_000, 30_000_000, 100,
                  title="CL B")]
        types = sorted(e.event_type for e in quarter_diff(prev, curr))
        self.assertEqual(types, ["EXIT", "NEW"])


class TestConviction(unittest.TestCase):
    """확신도 등급 — 증감률이 아니라 비중(bp)·진입 규모로 판정."""

    def test_strong_new_at_or_above_5pct(self):
        prev = reweight([h("023135106", "AMAZON COM INC", 9_000_000_000, 10_000_000, 0,
                           report=Q1, filing=F1)])
        curr = reweight([
            h("023135106", "AMAZON COM INC", 9_000_000_000, 10_000_000, 0),
            h("90353T100", "UBER TECHNOLOGIES INC", 1_000_000_000, 20_000_000, 0),  # 10%
        ])
        ev = by_cusip(quarter_diff(prev, curr))["90353T100"]
        self.assertEqual(ev.event_type, "NEW")
        self.assertEqual(ev.conviction, "STRONG_NEW")
        self.assertGreaterEqual(ev.weight_after, schema.STRONG_NEW_WEIGHT_PCT)

    def test_small_new_is_routine(self):
        prev = reweight([h("023135106", "AMAZON COM INC", 9_900_000_000, 10_000_000, 0,
                           report=Q1, filing=F1)])
        curr = reweight([
            h("023135106", "AMAZON COM INC", 9_900_000_000, 10_000_000, 0),
            h("42806J700", "HERTZ GLOBAL HLDGS", 100_000_000, 20_000_000, 0),  # 1%
        ])
        ev = by_cusip(quarter_diff(prev, curr))["42806J700"]
        self.assertEqual(ev.event_type, "NEW")
        self.assertEqual(ev.conviction, "ROUTINE")

    def test_strong_add_needs_200bp(self):
        """소형 포지션의 300% 증량이라도 비중 변화가 작으면 ROUTINE."""
        prev = reweight([
            h("023135106", "AMAZON COM INC", 9_950_000_000, 10_000_000, 0, report=Q1, filing=F1),
            h("42806J700", "HERTZ GLOBAL HLDGS", 50_000_000, 10_000_000, 0, report=Q1, filing=F1),
        ])
        curr = reweight([
            h("023135106", "AMAZON COM INC", 9_800_000_000, 10_000_000, 0),
            h("42806J700", "HERTZ GLOBAL HLDGS", 200_000_000, 40_000_000, 0),  # +300%
        ])
        ev = by_cusip(quarter_diff(prev, curr))["42806J700"]
        self.assertEqual(ev.event_type, "ADD")
        self.assertAlmostEqual(ev.share_delta_pct, 300.0, places=4)
        self.assertLess(ev.weight_delta_bp, schema.STRONG_DELTA_BP)
        self.assertEqual(ev.conviction, "ROUTINE")

    def test_strong_add_on_large_position(self):
        """대형 포지션의 소폭 증량이라도 비중이 200bp 이상 뛰면 STRONG_ADD."""
        prev = reweight([
            h("023135106", "AMAZON COM INC", 3_000_000_000, 10_000_000, 0, report=Q1, filing=F1),
            h("594918104", "MICROSOFT CORP", 7_000_000_000, 20_000_000, 0, report=Q1, filing=F1),
        ])
        curr = reweight([
            h("023135106", "AMAZON COM INC", 4_500_000_000, 15_000_000, 0),
            h("594918104", "MICROSOFT CORP", 7_000_000_000, 20_000_000, 0),
        ])
        ev = by_cusip(quarter_diff(prev, curr))["023135106"]
        self.assertEqual(ev.event_type, "ADD")
        self.assertGreaterEqual(ev.weight_delta_bp, schema.STRONG_DELTA_BP)
        self.assertEqual(ev.conviction, "STRONG_ADD")

    def test_strong_trim(self):
        prev = reweight([
            h("023135106", "AMAZON COM INC", 5_000_000_000, 20_000_000, 0, report=Q1, filing=F1),
            h("594918104", "MICROSOFT CORP", 5_000_000_000, 20_000_000, 0, report=Q1, filing=F1),
        ])
        curr = reweight([
            h("023135106", "AMAZON COM INC", 2_000_000_000, 8_000_000, 0),
            h("594918104", "MICROSOFT CORP", 5_000_000_000, 20_000_000, 0),
        ])
        ev = by_cusip(quarter_diff(prev, curr))["023135106"]
        self.assertEqual(ev.event_type, "TRIM")
        self.assertLessEqual(ev.weight_delta_bp, -schema.STRONG_DELTA_BP)
        self.assertEqual(ev.conviction, "STRONG_TRIM")

    def test_full_exit_always(self):
        prev = reweight([
            h("023135106", "AMAZON COM INC", 9_990_000_000, 10_000_000, 0, report=Q1, filing=F1),
            h("42806J700", "HERTZ GLOBAL HLDGS", 10_000_000, 1_000_000, 0, report=Q1, filing=F1),
        ])
        curr = reweight([h("023135106", "AMAZON COM INC", 9_990_000_000, 10_000_000, 0)])
        ev = by_cusip(quarter_diff(prev, curr))["42806J700"]
        self.assertEqual(ev.event_type, "EXIT")
        self.assertEqual(ev.conviction, "FULL_EXIT")   # 규모 무관

    def test_hold_is_routine(self):
        prev = reweight([h("023135106", "AMAZON COM INC", 1_000_000_000, 10_000_000, 0,
                           report=Q1, filing=F1)])
        curr = reweight([h("023135106", "AMAZON COM INC", 1_100_000_000, 10_000_000, 0)])
        self.assertEqual(quarter_diff(prev, curr)[0].conviction, "ROUTINE")

    def test_classify_conviction_is_pure(self):
        ev = schema.Event(
            event_id="x", report_date=Q2, filing_date=F2, event_type="ADD",
            conviction="ROUTINE", cusip="023135106", ticker="AMZN",
            issuer_name="AMAZON COM INC", prev_shares=1, curr_shares=2,
            share_delta_pct=100.0, prev_value_usd=1, curr_value_usd=2,
            value_delta_usd=1, weight_before=1.0, weight_after=3.0,
            weight_delta_bp=200.0,
        )
        self.assertEqual(classify_conviction(ev), "STRONG_ADD")
        ev.weight_delta_bp = 199.9
        self.assertEqual(classify_conviction(ev), "ROUTINE")


class TestFirstQuarter(unittest.TestCase):
    """이전 분기가 없는 최초 분기 — 전부 NEW, 크래시 없음."""

    def setUp(self):
        self.curr = reweight([
            h("11271J107", "BROOKFIELD CORP", 2_415_946_008, 59_697_208, 0),
            h("023135106", "AMAZON COM INC", 2_385_104_083, 11_451_981, 0),
            h("42806J700", "HERTZ GLOBAL HLDGS", 70_261_595, 15_241_127, 0),
        ])

    def test_empty_prev_list(self):
        events = quarter_diff([], self.curr)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e.event_type == "NEW" for e in events))
        self.assertTrue(all(e.prev_shares == 0 for e in events))
        self.assertTrue(all(e.share_delta_pct is None for e in events))

    def test_none_prev(self):
        events = quarter_diff(None, self.curr)   # 방어적: None 도 허용
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e.event_type == "NEW" for e in events))

    def test_empty_curr_returns_empty(self):
        self.assertEqual(quarter_diff(self.curr, []), [])

    def test_both_empty(self):
        self.assertEqual(quarter_diff([], []), [])

    def test_metrics_without_prev(self):
        events = quarter_diff([], self.curr)
        m = quarter_metrics(self.curr, events, None)
        self.assertIsNone(m.turnover_pct)
        self.assertEqual(m.new_count, 3)
        self.assertEqual(m.exit_count, 0)
        self.assertEqual(m.position_count, 3)


# =================================================================== metrics

class TestHHI(unittest.TestCase):
    """HHI = Σ(weight/100)^2."""

    def test_single_position_is_one(self):
        rows = [h("023135106", "AMAZON COM INC", 1_000_000_000, 10_000_000, 100.0)]
        self.assertAlmostEqual(quarter_metrics(rows, [], None).hhi, 1.0, places=6)

    def test_four_equal_positions_is_quarter(self):
        rows = [h("023135106", "AMAZON COM INC", 250, 10, 25.0),
                h("594918104", "MICROSOFT CORP", 250, 10, 25.0),
                h("30303M102", "META PLATFORMS INC", 250, 10, 25.0),
                h("90353T100", "UBER TECHNOLOGIES INC", 250, 10, 25.0)]
        self.assertAlmostEqual(quarter_metrics(rows, [], None).hhi, 0.25, places=6)

    def test_ten_equal_positions_is_point_one(self):
        rows = [h(f"{i:09d}", f"CO {i}", 100, 10, 10.0) for i in range(10)]
        self.assertAlmostEqual(quarter_metrics(rows, [], None).hhi, 0.10, places=6)

    def test_hhi_from_value_when_weight_missing(self):
        rows = [dict(h("023135106", "AMAZON COM INC", 750, 10, 0), weight_pct=None),
                dict(h("594918104", "MICROSOFT CORP", 250, 10, 0), weight_pct=None)]
        m = quarter_metrics(rows, [], None)
        self.assertAlmostEqual(m.hhi, 0.75 ** 2 + 0.25 ** 2, places=6)
        self.assertAlmostEqual(m.top1_pct, 75.0, places=4)

    def test_concentrated_portfolio_hhi_higher_than_diffuse(self):
        conc = [h("A", "A", 900, 10, 90.0), h("B", "B", 100, 10, 10.0)]
        diff_ = [h("A", "A", 500, 10, 50.0), h("B", "B", 500, 10, 50.0)]
        self.assertGreater(quarter_metrics(conc, [], None).hhi,
                           quarter_metrics(diff_, [], None).hhi)


class TestTopN(unittest.TestCase):

    def setUp(self):
        # 2026Q1 실측 비중 (docs/architecture.md 부록 A)
        self.rows = [
            h("11271J107", "BROOKFIELD CORP", 2_415_946_008, 59_697_208, 17.6),
            h("023135106", "AMAZON COM INC", 2_385_104_083, 11_451_981, 17.4),
            h("90353T100", "UBER TECHNOLOGIES INC", 2_154_934_398, 29_958_771, 15.7),
            h("594918104", "MICROSOFT CORP", 2_092_970_053, 5_654_078, 15.3),
            h("76131D103", "RESTAURANT BRANDS INTL", 1_673_501_194, 22_645_483, 12.2),
            h("30303M102", "META PLATFORMS INC", 1_522_358_404, 2_660_861, 11.1),
            h("44267T102", "HOWARD HUGHES HLDGS", 1_192_581_569, 18_852_064, 8.7),
            h("812215200", "SEAPORT ENTERTAINMENT", 107_910_794, 5_023_780, 0.8),
            h("02079K107", "ALPHABET INC", 89_421_720, 311_726, 0.7, title="CAP STK CL C"),
            h("42806J700", "HERTZ GLOBAL HLDGS", 70_261_595, 15_241_127, 0.5),
            h("02079K305", "ALPHABET INC", 9_310_043, 32_376, 0.1, title="CAP STK CL A"),
        ]

    def test_top_n_cumulative(self):
        m = quarter_metrics(self.rows, [], None)
        self.assertEqual(m.position_count, 11)
        self.assertAlmostEqual(m.top1_pct, 17.6, places=4)
        self.assertAlmostEqual(m.top3_pct, 17.6 + 17.4 + 15.7, places=4)
        self.assertAlmostEqual(m.top5_pct, 17.6 + 17.4 + 15.7 + 15.3 + 12.2, places=4)

    def test_top4_matches_project_baseline_66pct(self):
        """PROJECT.md 기준선: 2026Q1 Top-4 집중도 약 66%."""
        top4 = sum(sorted((r["weight_pct"] for r in self.rows), reverse=True)[:4])
        self.assertAlmostEqual(top4, 66.0, delta=0.5)

    def test_total_value_matches_baseline(self):
        m = quarter_metrics(self.rows, [], None)
        self.assertEqual(m.total_value_usd, 13_714_299_861)

    def test_top_n_when_fewer_positions(self):
        rows = [h("A", "A", 600, 10, 60.0), h("B", "B", 400, 10, 40.0)]
        m = quarter_metrics(rows, [], None)
        self.assertAlmostEqual(m.top1_pct, 60.0, places=4)
        self.assertAlmostEqual(m.top3_pct, 100.0, places=4)
        self.assertAlmostEqual(m.top5_pct, 100.0, places=4)


class TestTurnoverAndLag(unittest.TestCase):

    def test_lag_days(self):
        self.assertEqual(lag_days("2026-03-31", "2026-05-15"), 45)
        self.assertEqual(lag_days("2024-12-31", "2025-02-14"), 45)
        self.assertEqual(lag_days("bad", "2026-05-15"), 0)

    def test_metrics_lag_days(self):
        rows = [h("023135106", "AMAZON COM INC", 100, 10, 100.0)]
        self.assertEqual(quarter_metrics(rows, [], None).lag_days, 45)

    def test_zero_turnover_when_unchanged(self):
        prev = [h("023135106", "AMAZON COM INC", 1_000_000, 10, 100.0, report=Q1, filing=F1)]
        curr = [h("023135106", "AMAZON COM INC", 1_000_000, 10, 100.0)]
        self.assertAlmostEqual(turnover_pct(prev, curr), 0.0, places=6)

    def test_full_replacement_is_200pct(self):
        """포트폴리오 전체 교체 = |−V| + |+V| / V = 200%."""
        prev = [h("023135106", "AMAZON COM INC", 1_000_000, 10, 100.0, report=Q1, filing=F1)]
        curr = [h("90353T100", "UBER TECHNOLOGIES INC", 1_000_000, 10, 100.0)]
        self.assertAlmostEqual(turnover_pct(prev, curr), 200.0, places=4)

    def test_partial_turnover(self):
        prev = [h("A", "A", 800, 10, 80.0, report=Q1, filing=F1),
                h("B", "B", 200, 10, 20.0, report=Q1, filing=F1)]
        curr = [h("A", "A", 600, 8, 75.0), h("B", "B", 200, 10, 25.0)]
        # |600-800| = 200, 평균 가치 = (1000+800)/2 = 900 -> 22.22%
        self.assertAlmostEqual(turnover_pct(prev, curr), 200 / 900 * 100, places=4)

    def test_turnover_none_without_prev(self):
        curr = [h("A", "A", 100, 10, 100.0)]
        self.assertIsNone(turnover_pct(None, curr))
        self.assertIsNone(turnover_pct([], curr))

    def test_event_counts(self):
        prev = reweight([h("A", "A", 500, 10, 0, report=Q1, filing=F1),
                         h("B", "B", 300, 10, 0, report=Q1, filing=F1),
                         h("C", "C", 200, 10, 0, report=Q1, filing=F1)])
        curr = reweight([h("A", "A", 1000, 20, 0),     # ADD (평가액도 2배 -> 분할 아님)
                         h("B", "B", 150, 5, 0),       # TRIM (평가액도 1/2)
                         h("D", "D", 400, 10, 0)])     # NEW, C 는 EXIT
        events = quarter_diff(prev, curr)
        m = quarter_metrics(curr, events, prev)
        self.assertEqual((m.new_count, m.add_count, m.trim_count, m.exit_count),
                         (1, 1, 1, 1))

    def test_metrics_accepts_event_dicts(self):
        curr = [h("A", "A", 100, 10, 100.0)]
        events = [{"event_type": "NEW"}, {"event_type": "ADD"}]
        m = quarter_metrics(curr, events, None)
        self.assertEqual((m.new_count, m.add_count), (1, 1))


class TestHoldingPeriods(unittest.TestCase):

    def setUp(self):
        self.rows = [
            h("023135106", "AMAZON COM INC", 100, 10, 50.0, report="2025-06-30",
              filing="2025-08-14", ticker="AMZN"),
            h("42806J700", "HERTZ GLOBAL HLDGS", 100, 10, 50.0, report="2025-06-30",
              filing="2025-08-14", ticker="HTZ"),
            h("023135106", "AMAZON COM INC", 100, 10, 50.0, report="2025-09-30",
              filing="2025-11-14", ticker="AMZN"),
            h("42806J700", "HERTZ GLOBAL HLDGS", 100, 10, 50.0, report="2025-09-30",
              filing="2025-11-14", ticker="HTZ"),
            h("023135106", "AMAZON COM INC", 100, 10, 100.0, report=Q2, filing=F2,
              ticker="AMZN"),
        ]

    def test_periods(self):
        hp = holding_periods(self.rows)
        self.assertEqual(set(hp), {"023135106", "42806J700"})
        amzn = hp["023135106"]
        self.assertEqual(amzn["first_seen"], "2025-06-30")
        self.assertEqual(amzn["last_seen"], Q2)
        self.assertEqual(amzn["quarters_held"], 3)
        self.assertTrue(amzn["is_current"])
        self.assertEqual(amzn["ticker"], "AMZN")

    def test_exited_position_not_current(self):
        hz = holding_periods(self.rows)["42806J700"]
        self.assertEqual(hz["quarters_held"], 2)
        self.assertFalse(hz["is_current"])
        self.assertEqual(hz["last_seen"], "2025-09-30")

    def test_empty_input(self):
        self.assertEqual(holding_periods([]), {})


# =============================================================== build_events

def _two_quarter_fixture() -> list[dict]:
    q1 = reweight([
        h("11271J107", "BROOKFIELD CORP", 2_000_000_000, 50_000_000, 0, report=Q1, filing=F1),
        h("023135106", "AMAZON COM INC", 2_000_000_000, 10_000_000, 0, report=Q1, filing=F1),
        h("594918104", "MICROSOFT CORP", 1_000_000_000, 6_000_000, 0, report=Q1, filing=F1),
        h("42806J700", "HERTZ GLOBAL HLDGS", 100_000_000, 20_000_000, 0, report=Q1, filing=F1),
        h("02079K107", "ALPHABET INC", 80_000_000, 300_000, 0, title="CAP STK CL C",
          ticker="GOOG", report=Q1, filing=F1),
    ])
    q2 = reweight([
        h("11271J107", "BROOKFIELD CORP", 2_415_946_008, 59_697_208, 0),      # ADD
        h("023135106", "AMAZON COM INC", 2_385_104_083, 10_000_000, 0),       # HOLD
        h("594918104", "MICROSOFT CORP", 500_000_000, 3_000_000, 0),          # TRIM
        h("90353T100", "UBER TECHNOLOGIES INC", 2_154_934_398, 29_958_771, 0),  # NEW
        h("02079K107", "ALPHABET INC", 89_421_720, 600_000, 0, title="CAP STK CL C",
          ticker="GOOG"),                                                     # 2:1 분할
        h("02079K305", "ALPHABET INC", 9_310_043, 32_376, 0, title="CAP STK CL A",
          ticker="GOOGL"),                                                    # NEW (별개 클래스)
        # HERTZ 없음 -> EXIT
    ])
    return q1 + q2


class TestBuildEvents(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ackman-analytics-")
        self.holdings = os.path.join(self.tmp, "holdings.jsonl")
        self.events = os.path.join(self.tmp, "events.jsonl")
        self.metrics = os.path.join(self.tmp, "metrics.jsonl")
        write_jsonl(self.holdings, _two_quarter_fixture())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra):
        return build_events.main([
            "--holdings", self.holdings,
            "--events", self.events,
            "--metrics", self.metrics, *extra,
        ])

    def test_runs_and_writes(self):
        self.assertEqual(self._run(), 0)
        self.assertTrue(os.path.exists(self.events))
        self.assertTrue(os.path.exists(self.metrics))
        self.assertEqual(len(read_jsonl(self.metrics)), 2)

    def test_first_quarter_is_all_new(self):
        self._run()
        q1 = [e for e in read_jsonl(self.events) if e["report_date"] == Q1]
        self.assertEqual(len(q1), 5)
        self.assertTrue(all(e["event_type"] == "NEW" for e in q1))

    def test_second_quarter_classification(self):
        self._run()
        q2 = {e["cusip"]: e for e in read_jsonl(self.events) if e["report_date"] == Q2}
        self.assertEqual(q2["11271J107"]["event_type"], "ADD")
        self.assertEqual(q2["023135106"]["event_type"], "HOLD")
        self.assertEqual(q2["594918104"]["event_type"], "TRIM")
        self.assertEqual(q2["90353T100"]["event_type"], "NEW")
        self.assertEqual(q2["42806J700"]["event_type"], "EXIT")
        self.assertEqual(q2["02079K305"]["event_type"], "NEW")
        self.assertEqual(q2["02079K107"]["event_type"], "HOLD")   # 2:1 분할 보정됨

    def test_idempotent_byte_identical(self):
        """2회 연속 실행 결과가 바이트 단위로 동일해야 한다."""
        self._run()
        first_e = os.path.join(self.tmp, "first_events.jsonl")
        first_m = os.path.join(self.tmp, "first_metrics.jsonl")
        shutil.copyfile(self.events, first_e)
        shutil.copyfile(self.metrics, first_m)
        self._run()
        self.assertTrue(filecmp.cmp(first_e, self.events, shallow=False))
        self.assertTrue(filecmp.cmp(first_m, self.metrics, shallow=False))
        with open(first_e, "rb") as a, open(self.events, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_event_ids_unique(self):
        self._run()
        ids = [e["event_id"] for e in read_jsonl(self.events)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_exclude_hold_flag(self):
        self._run("--exclude-hold")
        types = {e["event_type"] for e in read_jsonl(self.events)}
        self.assertNotIn("HOLD", types)
        # 지표 집계는 영향받지 않는다
        m = {x["report_date"]: x for x in read_jsonl(self.metrics)}
        self.assertEqual(m[Q2]["new_count"], 2)
        self.assertEqual(m[Q2]["exit_count"], 1)

    def test_missing_holdings_file_is_graceful(self):
        rc = build_events.main([
            "--holdings", os.path.join(self.tmp, "nope.jsonl"),
            "--events", self.events, "--metrics", self.metrics,
        ])
        self.assertEqual(rc, 0)

    def test_empty_holdings_file_is_graceful(self):
        empty = os.path.join(self.tmp, "empty.jsonl")
        open(empty, "w").close()
        rc = build_events.main([
            "--holdings", empty, "--events", self.events, "--metrics", self.metrics,
        ])
        self.assertEqual(rc, 0)

    def test_metrics_turnover_present_from_second_quarter(self):
        self._run()
        m = {x["report_date"]: x for x in read_jsonl(self.metrics)}
        self.assertIsNone(m[Q1]["turnover_pct"])
        self.assertIsNotNone(m[Q2]["turnover_pct"])

    def test_build_is_pure(self):
        rows = _two_quarter_fixture()
        e1, m1 = build_events.build(rows)
        e2, m2 = build_events.build(rows)
        self.assertEqual(e1, e2)
        self.assertEqual(m1, m2)

    def test_duplicate_event_id_raises(self):
        """동일 CUSIP + 복수 클래스는 event_id 가 충돌하므로 하드 실패."""
        rows = reweight([
            h("11271J107", "BROOKFIELD CORP", 500, 10, 0, title="CL A"),
            h("11271J107", "BROOKFIELD CORP", 500, 10, 0, title="CL B"),
        ])
        with self.assertRaises(ValueError):
            build_events.build(rows)


# ============================================================== 실데이터 (선택)

@unittest.skipUnless(
    os.path.exists(Paths.HOLDINGS) and os.path.getsize(Paths.HOLDINGS) > 0,
    "data/normalized/holdings.jsonl 없음 (Collector 미완료) — 합성 픽스처로만 검증",
)
class TestRealData(unittest.TestCase):
    """Collector 산출물이 있으면 실데이터로도 동작을 확인한다."""

    @classmethod
    def setUpClass(cls):
        cls.holdings = read_jsonl(Paths.HOLDINGS)

    def test_builds_without_error(self):
        events, metrics = build_events.build(self.holdings)
        self.assertGreater(len(metrics), 0)
        self.assertEqual(len({e["event_id"] for e in events}), len(events))

    def test_weights_sum_to_100_per_quarter(self):
        by_q = {}
        for r in self.holdings:
            by_q.setdefault(r["report_date"], []).append(r)
        for rd, rows in by_q.items():
            total = sum(float(r.get("weight_pct") or 0) for r in rows)
            self.assertAlmostEqual(total, 100.0, delta=0.5, msg=f"{rd} weight sum")

    def test_alphabet_classes_separate_if_present(self):
        keys = {(r["cusip"], r["title_of_class"]) for r in self.holdings}
        goog = [k for k in keys if k[0] in ("02079K107", "02079K305")]
        if len(goog) >= 2:
            self.assertEqual(len(goog), len(set(goog)))
            self.assertEqual(len({k[0] for k in goog}), len(goog))


if __name__ == "__main__":
    unittest.main(verbosity=2)
