"""CUSIP -> 티커/FIGI 매핑 (docs/architecture.md §3.4).

13F 는 CUSIP 만 담고 티커를 담지 않는다. 발행사명 문자열 매칭은 ALPHABET INC
처럼 클래스가 나뉘는 종목에서 실패하므로 OpenFIGI v3 mapping API 를 쓴다.
무인증·무료이며 CUSIP 은 불변이므로 결과를 영구 캐시한다.

주의: OpenFIGI 는 CUSIP 하나에 대해 거래소별로 수십 개 레코드를 돌려준다.
      exchCode == "US" 이고 보통주/ADR 계열인 레코드를 우선 선택해야
      02079K107 -> GOOG, 02079K305 -> GOOGL 가 정확히 분리된다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable, Optional

import requests

from src.common import schema
from src.common.schema import Paths

log = logging.getLogger(__name__)

OPENFIGI_URL = schema.OPENFIGI_URL

# 무인증 OpenFIGI 실측 제한: 분당 25요청 / 요청당 10 job.
# (CONTRACTS.md 는 100건/요청으로 적혀 있으나 무인증 키에서는 413 이 난다.
#  413 을 만나면 배치를 반으로 쪼개 재시도하므로 상한이 올라가도 안전하다.)
BATCH_SIZE = 10
MIN_REQUEST_INTERVAL_SEC = 60.0 / 25.0     # 2.4s
RETRIES = 3

# securityType 선호 순위 — 앞쪽일수록 우선.
_TYPE_PRIORITY = (
    "common stock", "ordinary shares", "reit", "depositary receipt", "adr",
    "ads", "class", "unit", "trust", "fund", "preference", "preferred",
    "right", "warrant",
)

_last_request_at: float = 0.0
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "Content-Type": "application/json",
            "User-Agent": schema.USER_AGENT,
        })
        _session = s
    return _session


def _throttle() -> None:
    global _last_request_at
    wait = MIN_REQUEST_INTERVAL_SEC - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


# ---------------------------------------------------------------- 레코드 선택

def _rank(record: dict) -> tuple:
    exch = (record.get("exchCode") or "").upper()
    sec_type = " ".join(filter(None, [
        str(record.get("securityType") or ""),
        str(record.get("securityType2") or ""),
    ])).lower()
    type_rank = len(_TYPE_PRIORITY)
    for i, token in enumerate(_TYPE_PRIORITY):
        if token in sec_type:
            type_rank = i
            break
    return (
        0 if exch == "US" else 1,                                  # 미국 상장 우선
        0 if (record.get("ticker") or "").strip() else 1,          # 티커 있는 것 우선
        type_rank,                                                 # 보통주/ADR 우선
        0 if (record.get("marketSector") or "") == "Equity" else 1,
        (record.get("ticker") or ""),                              # 결정적 tie-break
    )


def select_record(records: list[dict]) -> Optional[dict]:
    """거래소별 다중 레코드 중 대표 1건 선택."""
    usable = [r for r in records if isinstance(r, dict)]
    if not usable:
        return None
    return sorted(usable, key=_rank)[0]


def _to_entry(record: dict) -> dict:
    # OpenFIGI 는 상장폐지된 종목의 티커에 '*' 접미사를 붙인다 (예: HHC*).
    # 표시·조인에 쓰기 위해 접미사는 떼고 delisted 플래그로 보존한다.
    raw_ticker = (record.get("ticker") or "").strip()
    delisted = raw_ticker.endswith("*")
    return {
        "ticker": raw_ticker.rstrip("*") or None,
        "delisted": delisted,
        "figi": record.get("figi"),
        "name": record.get("name"),
        "security_type": record.get("securityType") or record.get("securityType2"),
        "exch_code": record.get("exchCode"),
        "market_sector": record.get("marketSector"),
        "composite_figi": record.get("compositeFIGI"),
        "share_class_figi": record.get("shareClassFIGI"),
        "unresolved": False,
        "source": "openfigi",
    }


# ---------------------------------------------------------------- API 호출

def _post(jobs: list[dict]) -> list[dict]:
    payload = json.dumps(jobs)
    last_err = None
    for attempt in range(RETRIES):
        _throttle()
        try:
            resp = _get_session().post(OPENFIGI_URL, data=payload, timeout=30)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 413 and len(jobs) > 1:
            half = len(jobs) // 2
            log.warning("OpenFIGI 413 — 배치 %d -> %d 로 분할", len(jobs), half)
            return _post(jobs[:half]) + _post(jobs[half:])
        last_err = f"HTTP {resp.status_code} {resp.text[:200]}"
        if resp.status_code in (429, 500, 502, 503, 504):
            log.warning("OpenFIGI %s — 백오프 재시도 %d/%d",
                        last_err, attempt + 1, RETRIES)
            time.sleep(max(2 ** attempt, MIN_REQUEST_INTERVAL_SEC))
            continue
        raise RuntimeError(f"OpenFIGI 요청 실패: {last_err}")
    raise RuntimeError(f"OpenFIGI 요청 실패 (재시도 소진): {last_err}")


def load_cache() -> dict[str, dict]:
    return schema.load_json(Paths.CUSIP_MAP, {}) or {}


def save_cache(cache: dict[str, dict]) -> None:
    schema.save_json(Paths.CUSIP_MAP, cache)


def map_cusips(cusips: Iterable[str], force: bool = False) -> dict[str, dict]:
    """CUSIP -> {ticker, figi, name, security_type, ...}.

    캐시(data/reference/cusip_map.json) 우선 조회, 미스만 OpenFIGI 호출.
    해석 실패한 CUSIP 은 {"ticker": None, "unresolved": True} 로 캐시에 남겨
    매 실행마다 재시도가 폭주하지 않게 한다.
    """
    wanted = []
    seen = set()
    for c in cusips:
        c = (c or "").strip().upper()
        if c and c not in seen:
            seen.add(c)
            wanted.append(c)

    cache = load_cache()
    misses = [c for c in wanted
              if force or c not in cache
              or (cache[c].get("unresolved") and cache[c].get("retry"))]

    if misses:
        log.info("OpenFIGI 조회 %d건 (캐시 적중 %d건)",
                 len(misses), len(wanted) - len(misses))
    for start in range(0, len(misses), BATCH_SIZE):
        batch = misses[start:start + BATCH_SIZE]
        jobs = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]
        try:
            results = _post(jobs)
        except Exception as exc:                      # 매핑 실패로 백필을 죽이지 않는다
            log.error("OpenFIGI 배치 실패 (%s) — 해당 CUSIP 은 미해석 처리", exc)
            results = [{"error": str(exc)} for _ in batch]

        for cusip, result in zip(batch, results):
            records = (result or {}).get("data") or []
            chosen = select_record(records)
            if chosen is None:
                reason = (result or {}).get("warning") or (result or {}).get("error")
                log.warning("CUSIP 미해석: %s (%s)", cusip, reason)
                cache[cusip] = {"ticker": None, "figi": None, "name": None,
                                "security_type": None, "unresolved": True,
                                "reason": reason, "source": "openfigi"}
            else:
                cache[cusip] = _to_entry(chosen)
        save_cache(cache)

    return {c: cache.get(c, {"ticker": None, "figi": None, "name": None,
                             "security_type": None, "unresolved": True})
            for c in wanted}


# ---------------------------------------------------------------- CLI (점검용)

def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m src.collector.cusip_map",
        description="CUSIP 을 OpenFIGI 로 해석해 캐시에 채운다 (점검용).")
    parser.add_argument("cusips", nargs="+", help="조회할 CUSIP 목록")
    parser.add_argument("--force", action="store_true", help="캐시를 무시하고 재조회")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for cusip, entry in map_cusips(args.cusips, force=args.force).items():
        print(f"{cusip} -> {entry.get('ticker')!s:8s} "
              f"{entry.get('security_type')!s:20s} {entry.get('name')}")
    print(f"cache: {os.path.relpath(Paths.CUSIP_MAP, Paths.ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
