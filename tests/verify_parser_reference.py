"""부록 B 참조 구현 검증 스크립트 — 설계서의 파서 로직을 실제 EDGAR 데이터로 확인."""
import re, time
import xml.etree.ElementTree as ET
import requests

UA = {"User-Agent": "AckmanTracker younggil jinhae8971@gmail.com"}
BASE = "https://www.sec.gov/Archives/edgar/data/1336528"


def get(url, retries=3):
    for i in range(retries):
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 200:
            time.sleep(0.34)
            return r
        if r.status_code in (403, 429):
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
    raise RuntimeError(f"failed after {retries}: {url}")


def ns_of(root):
    t = root.tag
    return {"n": t[1: t.index("}")]} if t.startswith("{") else {}


def parse_13f(accession):
    acc = accession.replace("-", "")
    idx = get(f"{BASE}/{acc}/index.json").json()
    names = [i["name"] for i in idx["directory"]["item"]]
    info_name = next(n for n in names
                     if n.lower().endswith(".xml") and "primary" not in n.lower())

    primary = get(f"{BASE}/{acc}/primary_doc.xml").text
    m = re.search(r"<schemaVersion>(.*?)</schemaVersion>", primary)
    schema = m.group(1) if m else None
    am = re.search(r"<amendmentType>(.*?)</amendmentType>", primary)
    amendment_type = am.group(1) if am else None
    total_declared = int(re.search(r"<tableValueTotal>(\d+)</tableValueTotal>",
                                   primary).group(1))
    period = re.search(r"<periodOfReport>(.*?)</periodOfReport>", primary).group(1)

    root = ET.fromstring(get(f"{BASE}/{acc}/{info_name}").content)
    ns = ns_of(root)
    tag = lambda p: f"n:{p}" if ns else p
    path = lambda p: "/".join(tag(x) for x in p.split("/"))

    rows, raw_sum = [], 0
    for it in root.findall(tag("infoTable"), ns):
        g = lambda p: it.findtext(path(p), namespaces=ns)
        raw = int(g("value"))
        raw_sum += raw
        rows.append({
            "cusip": g("cusip"),
            "issuer_name": g("nameOfIssuer"),
            "title_of_class": g("titleOfClass"),
            "value_usd": raw if (schema and schema >= "X0202") else raw * 1000,
            "shares": int(g("shrsOrPrnAmt/sshPrnamt")),
            "share_type": g("shrsOrPrnAmt/sshPrnamtType"),
            "put_call": g("putCall"),
            "discretion": g("investmentDiscretion"),
        })

    assert raw_sum == total_declared, \
        f"checksum mismatch {raw_sum} != {total_declared} ({accession})"

    total = sum(r["value_usd"] for r in rows)
    for r in rows:
        r["weight_pct"] = round(r["value_usd"] / total * 100, 4)

    return {"accession": accession, "period": period, "schema_version": schema,
            "amendment_type": amendment_type, "holdings": rows}


def resolve_quarter(filings):
    """§4.3 — RESTATEMENT는 대체, NEW HOLDINGS는 병합."""
    holdings = {}
    for f in filings:
        if f["amendment_type"] == "RESTATEMENT":
            holdings = {}
        for h in f["holdings"]:
            holdings[(h["cusip"], h["title_of_class"])] = h
    return list(holdings.values())


if __name__ == "__main__":
    cases = [
        ("0001172661-26-002336", "최신 13F (X0202)"),
        ("0001172661-22-002568", "구 스키마 (천 달러 단위)"),
        ("0001172661-25-001119", "2024Q4 원본"),
        ("0001172661-25-001497", "2024Q4 정정 (NEW HOLDINGS)"),
    ]
    res = {}
    for acc, label in cases:
        d = res[acc] = parse_13f(acc)
        total = sum(x["value_usd"] for x in d["holdings"])
        print(f"[OK] {label:28s} {d['period']} schema={str(d['schema_version']):6s} "
              f"amend={str(d['amendment_type']):14s} n={len(d['holdings']):2d} ${total:,}")

    print("\n--- §4.3 정정 처리 검증 (2024-12-31) ---")
    merged = resolve_quarter([res["0001172661-25-001119"], res["0001172661-25-001497"]])
    naive = res["0001172661-25-001497"]["holdings"]
    print(f"올바른 병합 : {len(merged):2d}종목 ${sum(x['value_usd'] for x in merged):,}")
    print(f"단순 덮어쓰기: {len(naive):2d}종목 ${sum(x['value_usd'] for x in naive):,}  <-- 설계 오류 재현")
