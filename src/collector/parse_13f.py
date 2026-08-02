"""13F 파싱 계층 (docs/architecture.md §3, §4.3).

세 가지 함정을 여기서 전부 처리한다.
  1. value 단위 — schemaVersion 없으면 천 달러, X0202 이상이면 달러.
     반드시 schema.normalize_value() 를 통과시킨다.
  2. 체크섬 — primary_doc 의 tableValueTotal 과 infotable 의 **정규화 이전**
     raw value 합계가 일치해야 한다. 불일치는 ValueError 로 하드 실패.
  3. 정정 — amendmentType 이 RESTATEMENT 면 전체 대체, NEW HOLDINGS 면 병합.
     덮어쓰기로 처리하면 2024Q4 가 11종목 $12.66B -> 1종목 $46.5M 로 붕괴한다.
"""
from __future__ import annotations

from dataclasses import replace

import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional

from src.common import schema
from src.common.schema import Filing, Holding

from . import edgar

log = logging.getLogger(__name__)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MDY_DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")

RESTATEMENT = "RESTATEMENT"
NEW_HOLDINGS = "NEW HOLDINGS"


class ChecksumError(ValueError):
    """tableValueTotal 대조 실패. 계약상 ValueError 계열 하드 실패."""


# ---------------------------------------------------------------- XML 유틸

def local_name(tag: str) -> str:
    """네임스페이스를 제거한 태그명. 스키마 버전별 URI 변화를 흡수한다."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def ns_of(root: ET.Element) -> dict:
    """루트 태그에서 기본 네임스페이스를 동적 추출 (하드코딩 금지, §3.3)."""
    tag = root.tag
    return {"n": tag[1:tag.index("}")]} if tag.startswith("{") else {}


def _text_of(root: ET.Element, name: str) -> Optional[str]:
    """문서 전체에서 local name 이 일치하는 첫 요소의 텍스트."""
    for el in root.iter():
        if local_name(el.tag) == name:
            text = (el.text or "").strip()
            if text:
                return text
    return None


def _flat_fields(element: ET.Element) -> dict[str, str]:
    """한 infoTable 요소를 {local_name: text} 로 평탄화.

    shrsOrPrnAmt/sshPrnamt 처럼 중첩된 값도 이름 충돌 없이 잡힌다.
    """
    out: dict[str, str] = {}
    for sub in element.iter():
        key = local_name(sub.tag)
        if key in out:
            continue
        out[key] = (sub.text or "").strip()
    return out


def _as_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    cleaned = text.replace(",", "").replace("$", "").strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return int(round(float(cleaned)))


def to_iso_date(period: Optional[str]) -> Optional[str]:
    """primary_doc 의 periodOfReport 를 YYYY-MM-DD 로 정규화.

    표준은 MM-DD-YYYY 이지만 이미 ISO 인 문서도 있으므로 양쪽을 받는다.
    """
    if not period:
        return None
    value = period.strip()
    if _ISO_DATE.match(value):
        return value
    if _MDY_DATE.match(value):
        return schema.edgar_period_to_iso(value)
    if "/" in value:                                   # MM/DD/YYYY
        parts = value.split("/")
        if len(parts) == 3 and len(parts[2]) == 4:
            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    log.warning("알 수 없는 periodOfReport 형식: %r", period)
    return value


def normalize_amendment_type(raw: Optional[str],
                             form_type: str = "") -> Optional[str]:
    """amendmentType 문자열을 RESTATEMENT / NEW HOLDINGS 로 정규화."""
    if raw:
        upper = " ".join(raw.upper().split())
        if "RESTAT" in upper:
            return RESTATEMENT
        if "NEW" in upper:
            return NEW_HOLDINGS
        log.warning("미지의 amendmentType %r -> RESTATEMENT 로 보수 처리", raw)
        return RESTATEMENT
    if form_type.endswith("/A"):
        # EDGAR 상 정정에는 amendmentType 이 있어야 한다. 없으면 명시적으로 경고하고
        # 전체 재작성(RESTATEMENT)으로 본다 — 원본이 남는 병합보다 사실 왜곡이 적다.
        log.warning("정정 파일링에 amendmentType 이 없음 -> RESTATEMENT 로 처리")
        return RESTATEMENT
    return None


# ---------------------------------------------------------------- 문서 선택

def _pick_documents(files: dict[str, bytes]) -> tuple[bytes, bytes, str]:
    """(primary_doc, infotable, infotable 파일명) 을 고른다.

    파일명이 항상 infotable.xml 인 것은 아니므로 '이름이 primary 가 아닌 .xml'
    후보 중 루트 local name 이 informationTable 인 것을 우선한다.
    """
    names = list(files)
    primary_name = next(
        (n for n in names if "primary" in n.lower() and n.lower().endswith(".xml")),
        None)
    candidates = [n for n in names
                  if n.lower().endswith(".xml") and n != primary_name]

    info_name = None
    for name in candidates:
        try:
            root = ET.fromstring(files[name])
        except ET.ParseError:
            continue
        if local_name(root.tag) == "informationTable":
            info_name = name
            break
    if info_name is None and candidates:
        info_name = candidates[0]

    if primary_name is None:
        # 드물게 커버페이지 이름이 다를 수 있다. informationTable 이 아닌 나머지 중 선택.
        for name in names:
            if name == info_name:
                continue
            try:
                root = ET.fromstring(files[name])
            except ET.ParseError:
                continue
            if local_name(root.tag) in ("edgarSubmission", "informationTableSubmission"):
                primary_name = name
                break
    if primary_name is None or info_name is None:
        raise FileNotFoundError(
            f"13F 문서 쌍을 찾지 못함 (primary={primary_name}, infotable={info_name}), "
            f"files={names}")
    return files[primary_name], files[info_name], info_name


# ---------------------------------------------------------------- 파싱

def parse_primary_doc(content: bytes) -> dict:
    """primary_doc.xml 에서 커버페이지 메타를 뽑는다."""
    try:
        root = ET.fromstring(content)
        get = lambda name: _text_of(root, name)          # noqa: E731
    except ET.ParseError:
        text = content.decode("utf-8", "replace")

        def get(name: str) -> Optional[str]:             # 정규식 폴백
            m = re.search(rf"<[\w:]*{name}>(.*?)</[\w:]*{name}>", text, re.S)
            return m.group(1).strip() if m else None

    return {
        "schema_version": get("schemaVersion"),
        "amendment_type_raw": get("amendmentType"),
        "is_amendment": (get("isAmendment") or "").lower() in ("true", "1", "y"),
        "amendment_no": _as_int(get("amendmentNo")),
        "period_of_report": to_iso_date(get("periodOfReport")),
        "entry_total": _as_int(get("tableEntryTotal")),
        "value_total_raw": _as_int(get("tableValueTotal")),
    }


def parse_information_table(content: bytes) -> list[dict]:
    """infotable XML 을 원시 행 목록으로. value 는 아직 정규화하지 않는다."""
    root = ET.fromstring(content)
    ns = ns_of(root)
    tag = "n:infoTable" if ns else "infoTable"
    rows: list[dict] = []
    for element in root.findall(tag, ns):
        f = _flat_fields(element)
        raw_value = _as_int(f.get("value"))
        if raw_value is None:
            raise ValueError(f"infoTable 행에 value 가 없음: {f}")
        rows.append({
            "cusip": (f.get("cusip") or "").strip().upper(),
            "issuer_name": f.get("nameOfIssuer") or "",
            "title_of_class": f.get("titleOfClass") or "",
            "raw_value": raw_value,
            "shares": _as_int(f.get("sshPrnamt")) or 0,
            "share_type": f.get("sshPrnamtType") or "SH",
            "put_call": (f.get("putCall") or "").strip() or None,
            "discretion": f.get("investmentDiscretion") or "",
        })
    if not rows:
        raise ValueError("infoTable 에 infoTable 행이 하나도 없음")
    return rows


def parse_13f(accession: str, filing_date: str, form_type: str,
              report_date: Optional[str] = None,
              force: bool = False) -> tuple[Filing, list[Holding]]:
    """13F 한 건을 (Filing, list[Holding]) 로 파싱.

    `report_date` 는 submissions 의 reportDate 힌트(원문 보존 경로 결정용,
    계약 확장 선택 인자)이며 최종 값은 primary_doc 의 periodOfReport 가 이긴다.
    """
    files = edgar.fetch_filing_files(accession, report_date=report_date, force=force)
    primary_bytes, info_bytes, info_name = _pick_documents(files)

    meta = parse_primary_doc(primary_bytes)
    schema_version = meta["schema_version"]
    amendment_type = normalize_amendment_type(meta["amendment_type_raw"], form_type)
    period = meta["period_of_report"] or report_date
    if not period:
        raise ValueError(f"{accession}: periodOfReport 를 확정할 수 없음")

    rows = parse_information_table(info_bytes)

    # --- 체크섬 1: 행 수. tableEntryTotal 은 파싱 행 수와 정확히 같아야 한다 ---
    # 행 누락은 가장 위험한 실패다. 조용히 한 종목이 사라지면 그 종목은
    # '전량청산' 이벤트로 둔갑한다. 보유 71건 전량에서 불일치가 0건이므로
    # 경고가 아니라 하드 실패로 둔다.
    if meta["entry_total"] is not None and meta["entry_total"] != len(rows):
        raise ChecksumError(
            f"entry count mismatch {len(rows)} != {meta['entry_total']} "
            f"({accession}, {info_name})")

    # --- 체크섬 2: 금액. 정규화 **이전** raw 합계와 tableValueTotal 대조 ---
    # 제출인 스스로의 반올림 때문에 총액이 몇 달러 어긋나는 경우가 실제로 있다
    # (0002045724-26-000002: 29행 합계가 커버페이지보다 정확히 $1 많다).
    # 이걸 하드 실패로 두면 정상 공시가 파이프라인을 멈춘다.
    #
    # 허용 오차는 '행 수'다. 행마다 최대 1단위의 반올림 오차를 가정한 상한이며,
    # 그 이상은 반올림으로 설명되지 않는다. 행 누락은 위 체크섬 1이 이미
    # 잡으므로, 이 완화가 누락을 통과시키지 않는다.
    raw_sum = sum(r["raw_value"] for r in rows)
    declared = meta["value_total_raw"]
    if declared is None:
        log.warning("%s: tableValueTotal 이 없어 금액 체크섬을 건너뜀", accession)
    elif raw_sum != declared:
        drift = raw_sum - declared
        if abs(drift) > len(rows):
            raise ChecksumError(
                f"checksum mismatch {raw_sum} != {declared} "
                f"({accession}, {info_name}, 차이 {drift:+d} > 행수 {len(rows)})")
        log.warning("%s: tableValueTotal 이 %+d 어긋남 (행수 %d 이내 — "
                    "제출인 반올림으로 간주하고 진행)", accession, drift, len(rows))

    # --- 단위 정규화 -> 달러 ---
    total_usd = sum(schema.normalize_value(r["raw_value"], schema_version)
                    for r in rows)

    holdings: list[Holding] = []
    for r in rows:
        value_usd = schema.normalize_value(r["raw_value"], schema_version)
        holdings.append(Holding(
            report_date=period,
            filing_date=filing_date,
            accession=accession,
            form_type=form_type,
            amendment_type=amendment_type,
            cusip=r["cusip"],
            issuer_name=r["issuer_name"],
            title_of_class=r["title_of_class"],
            value_usd=value_usd,
            shares=r["shares"],
            share_type=r["share_type"],
            put_call=r["put_call"],
            discretion=r["discretion"],
            weight_pct=round(value_usd / total_usd * 100, 4) if total_usd else 0.0,
            ticker=None,                 # map_cusips 가 채운다
            figi=None,
            schema_version=schema_version,
        ))

    filing = Filing(
        accession=accession,
        form_type=form_type,
        filing_date=filing_date,
        report_date=period,
        primary_document="primary_doc.xml",
        url=edgar.filing_url(accession),
        amendment_type=amendment_type,
        schema_version=schema_version,
        entry_total=meta["entry_total"] if meta["entry_total"] is not None else len(rows),
        value_total_raw=declared if declared is not None else raw_sum,
        items=None,
    )
    return filing, holdings


# ---------------------------------------------------------------- 정정 해석

def recompute_weights(holdings: list[Holding]) -> list[Holding]:
    total = sum(h.value_usd for h in holdings)
    for h in holdings:
        h.weight_pct = round(h.value_usd / total * 100, 4) if total else 0.0
    return holdings


def aggregate_rows(holdings: list[Holding]) -> list[Holding]:
    """같은 파일링 안의 동일 포지션 행을 합산한다.

    13F 는 한 종목을 여러 행으로 쪼개 보고할 수 있다. Berkshire 는 매니저
    (버핏/콤스/웨슐러)와 자회사 단위로 나뉘어 29종목이 90행으로 들어오고,
    운용재량(SOLE/DEFINED/OTHER)이 다르면 같은 종목도 별도 행이 된다.
    행을 덮어쓰면 포트폴리오의 3분의 2가 조용히 사라진다 — 반드시 합산해야
    한다. 키는 (cusip, title_of_class, put_call) 로, 보통주와 PUT/CALL 은
    끝까지 분리한다.
    """
    merged: dict[tuple[str, str, str], Holding] = {}
    for h in holdings:
        prev = merged.get(h.key)
        if prev is None:
            merged[h.key] = replace(h)
            continue
        prev.value_usd += h.value_usd
        prev.shares += h.shares
        # 재량 구분이 섞이면 대표값을 OTHER 로 낮춘다 (보수적 표기).
        if prev.discretion != h.discretion:
            prev.discretion = "MIXED"
    return list(merged.values())


def resolve_quarter(filings: list[tuple[Filing, list[Holding]]]) -> list[Holding]:
    """한 report_date 의 원본+정정을 유효 포지션 집합으로 해석 (§4.3).

    filing_date 오름차순으로 훑으면서
      RESTATEMENT  -> 누적 초기화 후 전체 대체
      NEW HOLDINGS -> 기존 유지 + 신규 추가(동일 키는 갱신)

    파일링 '내부'의 중복 행은 aggregate_rows 로 합산하고, 파일링 '사이'는
    나중 것으로 대체한다. 정정 파일링이 그 종목의 최종 상태를 다시
    보고하는 것이므로 대체가 맞고, 합산하면 이중계상이 된다.
    """
    ordered = sorted(filings, key=lambda pair: (pair[0].filing_date,
                                                pair[0].accession))
    merged: dict[tuple[str, str, str], Holding] = {}
    for filing, holdings in ordered:
        if filing.amendment_type == RESTATEMENT:
            log.info("RESTATEMENT %s -> 이전 누적 %d건 폐기",
                     filing.accession, len(merged))
            merged = {}
        rows = aggregate_rows(holdings)
        if len(rows) != len(holdings):
            log.info("  %s 중복 행 합산: %d행 -> %d포지션",
                     filing.accession, len(holdings), len(rows))
        for holding in rows:
            merged[holding.key] = holding

    result = list(merged.values())
    recompute_weights(result)
    result.sort(key=lambda h: (-h.value_usd, h.cusip, h.title_of_class))
    return result
