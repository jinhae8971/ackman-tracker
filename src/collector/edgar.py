"""EDGAR 접근 계층 — HTTP 클라이언트, submissions API, 파일링 목록/다운로드.

설계 규율 (docs/architecture.md §2.3, §6.3):
  * 모든 요청에 User-Agent 필수. 없으면 SEC 가 403 을 반환한다.
  * SEC 합산 제한은 초당 10요청이지만, GitHub Actions 러너가 공유 IP 를 쓰므로
    자체적으로 초당 3요청 이하(REQUEST_INTERVAL_SEC = 0.34s)로 제한한다.
  * 403 / 429 / 5xx 는 지수 백오프로 최대 3회 재시도.
  * 내려받은 원문은 data/raw/ 에 보존하여 감사 추적과 재실행 캐시로 함께 쓴다.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

from src.common import schema
from src.common.schema import Filing, Paths

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 상수

ARCHIVE_BASE = schema.ARCHIVE_BASE
SUBMISSIONS_URL = schema.SUBMISSIONS_URL
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"

DEFAULT_13F_FORMS = ["13F-HR", "13F-HR/A"]

RETRYABLE_STATUS = (403, 429, 500, 502, 503, 504)

# 관측용: 실제로 나간 요청 시각(monotonic). rate limit 준수 검증에 쓴다.
REQUEST_TIMES: list[float] = []

_session: Optional[requests.Session] = None
_last_request_at: float = 0.0


# ---------------------------------------------------------------- HTTP

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": schema.USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        })
        _session = s
    return _session


def _throttle() -> None:
    """요청 시작 간격을 REQUEST_INTERVAL_SEC 이상으로 강제 (<= 약 2.94 req/s)."""
    global _last_request_at
    now = time.monotonic()
    wait = schema.REQUEST_INTERVAL_SEC - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()
    REQUEST_TIMES.append(_last_request_at)


def max_requests_per_second() -> int:
    """지금까지 나간 요청 중 임의의 1초 구간 최대 요청 수. rate limit 자체 감사용."""
    if not REQUEST_TIMES:
        return 0
    ts = sorted(REQUEST_TIMES)
    best, left = 0, 0
    for right in range(len(ts)):
        while ts[right] - ts[left] >= 1.0:
            left += 1
        best = max(best, right - left + 1)
    return best


def http_get(url: str, retries: int = 3, timeout: int = 30) -> requests.Response:
    """rate limit 준수 + 지수 백오프 재시도가 적용된 GET."""
    last_err: Optional[str] = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = _get_session().get(url, timeout=timeout)
        except requests.RequestException as exc:      # 네트워크 계층 오류
            last_err = f"{type(exc).__name__}: {exc}"
            log.warning("GET %s failed (%s), retry %d/%d", url, last_err,
                        attempt + 1, retries)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 200:
            return resp
        last_err = f"HTTP {resp.status_code}"
        if resp.status_code in RETRYABLE_STATUS:
            log.warning("GET %s -> %s, backoff retry %d/%d", url,
                        last_err, attempt + 1, retries)
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"GET failed after {retries} attempts ({last_err}): {url}")


def http_get_json(url: str) -> dict:
    return http_get(url).json()


# ---------------------------------------------------------------- submissions

def fetch_submissions() -> dict:
    """data.sec.gov submissions API 원본 JSON.

    `filings.files` 가 비어있지 않으면(대용량 이력 분할) 추가 페이지를 받아
    `filings.recent` 뒤에 이어 붙인 사본을 돌려준다. Pershing Square 는 현재
    분할이 없지만(files == []) 코드 경로는 유지한다.
    """
    data = http_get_json(SUBMISSIONS_URL)
    extra_files = (data.get("filings") or {}).get("files") or []
    if not extra_files:
        return data

    recent = data["filings"]["recent"]
    for meta in extra_files:
        name = meta.get("name")
        if not name:
            continue
        page = http_get_json(f"{SUBMISSIONS_BASE}/{name}")
        for key, values in page.items():
            if isinstance(values, list) and key in recent:
                recent[key] = list(recent[key]) + list(values)
    return data


def accession_nodash(accession: str) -> str:
    return accession.replace("-", "")


def filing_index_url(accession: str) -> str:
    return f"{ARCHIVE_BASE}/{accession_nodash(accession)}/index.json"


def filing_url(accession: str, document: Optional[str] = None) -> str:
    acc = accession_nodash(accession)
    if document:
        return f"{ARCHIVE_BASE}/{acc}/{document}"
    return f"{ARCHIVE_BASE}/{acc}/"


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def list_filings(forms: Optional[list[str]] = None,
                 submissions: Optional[dict] = None) -> list[Filing]:
    """submissions 를 Filing 레코드로 평탄화. forms 로 필터.

    `submissions` 는 계약 확장(선택 인자)이다 — 이미 받아둔 JSON 을 재사용해
    동일 실행 안에서 submissions 를 두 번 내려받지 않기 위한 것이며,
    생략하면 계약 그대로 fetch_submissions() 를 호출한다.
    """
    data = submissions if submissions is not None else fetch_submissions()
    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    wanted = set(forms) if forms else None

    out: list[Filing] = []
    for i, accession in enumerate(accessions):
        def col(name: str):
            seq = recent.get(name) or []
            return seq[i] if i < len(seq) else None

        form_type = _clean(col("form")) or ""
        if wanted is not None and form_type not in wanted:
            continue
        primary_document = _clean(col("primaryDocument")) or ""
        out.append(Filing(
            accession=accession,
            form_type=form_type,
            filing_date=_clean(col("filingDate")) or "",
            report_date=_clean(col("reportDate")),
            primary_document=primary_document,
            url=filing_url(accession, primary_document or None),
            items=_clean(col("items")),
        ))
    out.sort(key=lambda f: (f.filing_date, f.accession))
    return out


# ---------------------------------------------------------------- 파일 다운로드

def raw_dir_for(accession: str, report_date: Optional[str] = None,
                form_group: str = "13F") -> str:
    return os.path.join(Paths.RAW, form_group, report_date or "unknown", accession)


def fetch_filing_files(accession: str,
                       report_date: Optional[str] = None,
                       form_group: str = "13F",
                       force: bool = False) -> dict[str, bytes]:
    """index.json 을 읽어 해당 파일링의 XML 을 모두 내려받고 data/raw/ 에 보존.

    보존 경로: data/raw/{form_group}/{report_date}/{accession}/
    이미 완전한 사본이 있으면(_manifest.json 기준) 네트워크를 타지 않는다 —
    재실행 멱등성과 SEC 부하 절감을 동시에 얻기 위함. `force=True` 로 무시 가능.
    """
    target = raw_dir_for(accession, report_date, form_group)
    manifest_path = os.path.join(target, "_manifest.json")

    if not force and os.path.exists(manifest_path):
        manifest = schema.load_json(manifest_path, {}) or {}
        names = manifest.get("files") or []
        if names and all(os.path.exists(os.path.join(target, n)) for n in names):
            log.debug("raw cache hit: %s", accession)
            files = {}
            for name in names:
                with open(os.path.join(target, name), "rb") as fh:
                    files[name] = fh.read()
            return files

    index = http_get_json(filing_index_url(accession))
    items = ((index.get("directory") or {}).get("item")) or []
    names = [it["name"] for it in items
             if str(it.get("name", "")).lower().endswith(".xml")]
    if not names:
        raise FileNotFoundError(
            f"{accession}: index.json 에 XML 문서가 없음 "
            f"(구형 텍스트 전용 파일링일 수 있음)")

    os.makedirs(target, exist_ok=True)
    files: dict[str, bytes] = {}
    for name in names:
        content = http_get(filing_url(accession, name)).content
        files[name] = content
        tmp = os.path.join(target, name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(content)
        os.replace(tmp, os.path.join(target, name))

    schema.save_json(manifest_path, {
        "accession": accession,
        "report_date": report_date,
        "files": sorted(files),
        "source": filing_url(accession),
    })
    return files
