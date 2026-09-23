#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
법원경매정보(courtauction.go.kr) 실시간 물건 수집기.

검증된 공식 웹스퀘어 엔드포인트를 직접 호출한다:
  검색       POST /pgj/pgjsearch/searchControllerMain.on
  사건상세   POST /pgj/pgj15A/selectAuctnCsSrchRslt.on

이 수집기는 결과를 analyze.py/track.py가 먹는 공용 스키마로 직접 저장하며,
주소에서 법정동코드(lawd_cd)를 파생해 국토부 실거래가 시세 조회가 동작하게 한다.

설계 원칙(안정성):
- 세션 초기화가 실패해도 포기하지 않고 검색으로 진행(불안정한 해외 접속 대비).
- 페이지마다 독립 try/except. 수집 0건이면 기존 properties.json을 보존(덮어쓰지 않음).
- 항상 정상 종료(exit 0). 추정·하드코딩 값은 넣지 않는다(모르면 null).

표준 라이브러리만 사용.
"""
import os, sys, json, re, ssl, time
import http.cookiejar
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "properties.json")
KST = timezone(timedelta(hours=9))

BASE = "https://www.courtauction.go.kr"
SEARCH_EP = BASE + "/pgj/pgjsearch/searchControllerMain.on"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 용도 표시명 → 공용 스키마 type
def type_from_usage(u):
    u = u or ""
    if "아파트" in u:
        return "아파트"
    if "오피스텔" in u:
        return "오피스텔"
    if any(k in u for k in ("연립", "다세대", "빌라")):
        return "빌라"
    return "기타"


# 수도권 시군구 → 법정동코드 5자리(국토부 LAWD_CD). 필요 시 확장.
SIGUNGU_LAWD = {
    # 서울 25구
    "종로구": "11110", "중구": "11140", "용산구": "11170", "성동구": "11200",
    "광진구": "11215", "동대문구": "11230", "중랑구": "11260", "성북구": "11290",
    "강북구": "11305", "도봉구": "11320", "노원구": "11350", "은평구": "11380",
    "서대문구": "11410", "마포구": "11440", "양천구": "11470", "강서구": "11500",
    "구로구": "11530", "금천구": "11545", "영등포구": "11560", "동작구": "11590",
    "관악구": "11620", "서초구": "11650", "강남구": "11680", "송파구": "11710", "강동구": "11740",
    # 인천
    "인천 중구": "28110", "인천 동구": "28140", "미추홀구": "28177", "연수구": "28185",
    "남동구": "28200", "부평구": "28237", "계양구": "28245", "인천 서구": "28260",
    "강화군": "28710", "옹진군": "28720",
    # 경기 — 구 있는 시(더 구체적, 먼저 매칭)
    "수원시 장안구": "41111", "수원시 권선구": "41113", "수원시 팔달구": "41115", "수원시 영통구": "41117",
    "성남시 수정구": "41131", "성남시 중원구": "41133", "성남시 분당구": "41135",
    "안양시 만안구": "41171", "안양시 동안구": "41173",
    "부천시 원미구": "41192", "부천시 소사구": "41194", "부천시 오정구": "41196", "부천시": "41190",
    "고양시 덕양구": "41281", "고양시 일산동구": "41285", "고양시 일산서구": "41287",
    "안산시 상록구": "41271", "안산시 단원구": "41273",
    "용인시 처인구": "41461", "용인시 기흥구": "41463", "용인시 수지구": "41465",
    # 경기 — 시 단위
    "의정부시": "41150", "광명시": "41210", "평택시": "41220", "동두천시": "41250",
    "과천시": "41290", "구리시": "41310", "남양주시": "41360", "오산시": "41370",
    "시흥시": "41390", "군포시": "41410", "의왕시": "41430", "하남시": "41450",
    "파주시": "41480", "이천시": "41500", "안성시": "41550", "김포시": "41570",
    "화성시": "41590", "광주시": "41610", "양주시": "41630", "포천시": "41650",
    "여주시": "41670", "양평군": "41830", "가평군": "41820", "연천군": "41800",
}
_LAWD_KEYS = sorted(SIGUNGU_LAWD.keys(), key=len, reverse=True)


def lawd_from_address(addr):
    a = addr or ""
    for k in _LAWD_KEYS:
        if k in a:
            return SIGUNGU_LAWD[k]
    return ""


def region_from_address(addr):
    """baseline 매칭용: 서울/인천은 '시도 구', 경기는 '경기 시'."""
    if not addr:
        return ""
    p = addr.split()
    sido = p[0]
    short = ("서울" if "서울" in sido else "인천" if "인천" in sido
             else "경기" if "경기" in sido else sido.replace("특별시", "").replace("광역시", "").replace("도", ""))
    if short in ("서울", "인천"):
        for t in p[1:]:
            if t.endswith("구") or t.endswith("군"):
                return f"{short} {t}"
    if short == "경기":
        for t in p[1:]:
            if t.endswith("시"):
                return f"{short} {t}"
    return f"{short} {p[1]}" if len(p) > 1 else short


def extract_area(text):
    m = re.search(r'([\d.]+)\s*(?:㎡|m2|M2)', str(text or ""))
    if m:
        try:
            return round(float(m.group(1)), 2)
        except ValueError:
            pass
    return None


def extract_floor(text):
    m = re.search(r'제?\s*(\d+)\s*층', str(text or ""))
    return int(m.group(1)) if m else None


def extract_apt_name(addr, bld):
    """주소·건물내역에서 단지명 추정(국토부 매칭용). 못 찾으면 빈 문자열."""
    for src in (bld or "", addr or ""):
        m = re.search(r'([가-힣A-Za-z0-9]+(?:아파트|자이|푸르지오|힐스테이트|더샵|아이파크|캐슬|채|타운|팰리스|파크))', src)
        if m:
            return m.group(1)
    return ""


SPECIAL_RIGHTS_KW = ["유치권", "법정지상권", "분묘기지권", "지분", "선순위전세권", "대지권미등기", "가처분", "예고등기"]


def analyze_rights(row):
    """검색행의 비고/현황 텍스트에서 권리 위험을 보수적으로 추출(공용 스키마)."""
    notes = " ".join(str(row.get(k, "") or "") for k in ("mulBigo", "pjbBuldList", "gdsDspslObjClsNm", "rmk"))
    senior_tenant = False
    assumed_deposit = 0
    special = [k for k in SPECIAL_RIGHTS_KW if k in notes]
    lien = 0
    if any(w in notes for w in ("임차인", "대항력", "전입", "보증금")):
        if "대항력" in notes and "없음" not in notes:
            if "배당요구" not in notes or "미배당" in notes or "배당요구종기" in notes:
                senior_tenant = True
                m = re.search(r'보증금\s*([\d,]+)\s*만', notes)
                if m:
                    assumed_deposit = int(m.group(1).replace(",", "")) * 10000
    if "유치권" in notes:
        m = re.search(r'유치권\D*([\d,]+)\s*만', notes)
        if m:
            lien = int(m.group(1).replace(",", "")) * 10000
    return {"senior_tenant": senior_tenant, "assumed_deposit": assumed_deposit,
            "special_rights": special, "lien_amount": lien}


# --------------------- 세션 ---------------------
def make_opener():
    cj = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=ctx))
    try:  # 세션 쿠키 확보(실패해도 계속 진행)
        req = urllib.request.Request(BASE + "/", headers={
            "User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        op.open(req, timeout=12).read()
    except Exception as e:  # noqa: BLE001
        print(f"[crawl] 세션 초기화 경고(무시하고 진행): {type(e).__name__}", file=sys.stderr)
    return op


def _to_int(x, d=0):
    try:
        return int(str(x).replace(",", ""))
    except (ValueError, TypeError):
        return d


# --------------------- 수집 ---------------------
def _opener():
    """요청마다 독립 opener(스레드 안전, 워밍업 GET 없음)."""
    cj = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                       urllib.request.HTTPSHandler(context=ctx))


def fetch_page(page, page_size, bgn, end, timeout=25):
    """검색 1페이지. 실패 시 None, 결과 없음은 []."""
    headers = {"User-Agent": UA, "Content-Type": "application/json;charset=UTF-8",
               "Accept": "application/json", "Accept-Language": "ko-KR,ko;q=0.9",
               "Referer": BASE + "/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml",
               "submissionid": "sbm_selectGdsDtlSrch", "SC-Pgmid": "PGJ151M01"}
    payload = {
        "dma_pageInfo": {"pageNo": str(page), "pageSize": str(page_size), "totalYn": "Y" if page == 1 else "N"},
        "dma_srchGdsDtlSrchInfo": {"mvprpRletDvsCd": "00031R", "cortAuctnSrchCondCd": "0004601",
                                   "cortStDvs": "0", "bidBgngYmd": bgn, "bidEndYmd": end, "pgmId": "PGJ151M01"},
    }
    try:
        req = urllib.request.Request(SEARCH_EP, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
        with _opener().open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("data", {}).get("dlt_srchResult", [])
    except Exception as e:  # noqa: BLE001
        print(f"[crawl] p{page} 오류: {type(e).__name__}", file=sys.stderr)
        return None


def row_to_item(row, f):
    sido = row.get("hjguSido", "") or row.get("printSt", "")
    usage = row.get("dspslUsgNm", "") or ""
    if not any(s in sido for s in f["sido"]):
        return None
    if not any(u in usage for u in f["usage"]):
        return None
    appr = _to_int(row.get("gamevalAmt"))
    if appr < f["min_appr"] or appr > f["max_appr"]:
        return None
    case = row.get("srnSaNo", "")
    if not case:
        return None
    court = row.get("jiwonNm", "법원")
    seq = _to_int(row.get("maemulSer", 1), 1)
    addr = (row.get("printSt", "") or "").strip()
    bld = row.get("pjbBuldList", "") or ""
    raw = str(row.get("maeGiil", ""))
    sale_date = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}" if len(raw) == 8 else None
    item = {"id": f"{court}_{case}_{seq}", "court": court, "case_no": case, "address": addr,
            "region": region_from_address(addr), "type": type_from_usage(usage),
            "apt_name": extract_apt_name(addr, bld), "lawd_cd": lawd_from_address(addr),
            "exclusive_area": extract_area(bld), "floor": extract_floor(row.get("buldList") or bld),
            "appraisal": appr, "min_bid": _to_int(row.get("minmaePrice"), appr),
            "fail_rounds": _to_int(row.get("yuchalCnt")), "sale_date": sale_date,
            "eviction": "normal", "market_price_override": None}
    item.update(analyze_rights(row))
    return item


def crawl(cfg):
    f = {"sido": cfg.get("sido", ["서울특별시", "경기도", "인천광역시"]),
         "usage": cfg.get("usage", ["아파트", "오피스텔", "연립다세대", "다세대", "연립", "빌라"]),
         "min_appr": cfg.get("min_appraisal", 50000000),
         "max_appr": cfg.get("max_appraisal", 5000000000)}
    days = cfg.get("sale_date_to_days", 60)
    cap = cfg.get("max_properties", 100000)
    page_size = cfg.get("page_size", 100)
    workers = max(1, cfg.get("concurrency", 3))
    max_pages = cfg.get("max_pages", 500)
    delay = cfg.get("polite_delay_sec", 0.3)
    now = datetime.now(KST)
    bgn, end = now.strftime("%Y%m%d"), (now + timedelta(days=days)).strftime("%Y%m%d")

    make_opener()  # 세션 워밍업(실패 무시)
    props, seen = [], set()
    page = 1
    while page <= max_pages and len(props) < cap:
        batch = list(range(page, min(page + workers, max_pages + 1)))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                rowsets = list(ex.map(lambda pg: fetch_page(pg, page_size, bgn, end), batch))
        else:
            rowsets = [fetch_page(pg, page_size, bgn, end) for pg in batch]

        any_rows = False
        for pg, rows in zip(batch, rowsets):
            if not rows:
                continue
            any_rows = True
            got = 0
            for row in rows:
                if len(props) >= cap:
                    break
                item = row_to_item(row, f)
                if not item or item["id"] in seen:
                    continue
                seen.add(item["id"])
                props.append(item)
                got += 1
            print(f"[crawl] p{pg}: {len(rows)}건 중 {got}건 채택 (누적 {len(props)})")
        if not any_rows:            # 배치 전체가 빈 결과 → 끝
            break
        page += workers
        time.sleep(delay)
    return props


def sample_properties():
    p = os.path.join(DATA, "properties.sample.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def save(props):
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(props, f, ensure_ascii=False, indent=2)


def main():
    cfg = {}
    cp = os.path.join(DATA, "crawl-config.json")
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            cfg = json.load(f)

    if "--sample" in sys.argv:
        props = sample_properties()
        save(props)
        print(f"[sample] {len(props)}건 → properties.json")
        return

    try:
        props = crawl(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[crawl] 수집 오류: {type(e).__name__}: {e}", file=sys.stderr)
        props = []

    if props:
        save(props)
        # MOLIT 조회 가능 비율(법정동코드 확보) 리포트
        with_lawd = sum(1 for p in props if p.get("lawd_cd"))
        print(f"[crawl] 수집 {len(props)}건 → properties.json "
              f"(법정동코드 확보 {with_lawd}/{len(props)}, 시세조회 가능)")
        return

    # 수집 0건: 기존 데이터 보존(덮어쓰지 않음), 최초 실행이면 샘플
    if os.path.exists(OUT):
        print("[crawl] 수집 0건 — 기존 properties.json 유지(분석·추적 계속). "
              "접속 차단이 의심되면 crawl-config의 조건을 확인하세요.", file=sys.stderr)
    else:
        props = sample_properties()
        save(props)
        print(f"[crawl] 수집 0건 — 최초 실행이라 샘플 {len(props)}건으로 시작", file=sys.stderr)


if __name__ == "__main__":
    main()
