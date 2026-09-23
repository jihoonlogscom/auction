#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
법원경매정보(courtauction.go.kr) 자동 수집기.

사이트의 WebSquare 화면이 내부적으로 호출하는 JSON 엔드포인트를 그대로 호출해
전국(설정 시도) 부동산 경매 물건을 수집하고, 현황조사서에서 권리(임대차·대항력·
인수보증금)를 자동 추출해 data/properties.json 으로 저장한다. 사용자 입력 불필요.

  검색       : POST /pgj/pgjsearch/searchControllerMain.on
  현황조사서 : POST /pgj/pgjsearch/selectCurstExmndc.on   (점유관계·임대차현황)

주의:
- 위 엔드포인트는 대법원이 공식 문서로 제공하는 API가 아니라, 사이트가 내부적으로
  쓰는 비공식 경로다. 사이트가 개편되면 요청 본문(build_search_body)·응답 키
  (parse_rows)를 한 번 재보정해야 한다. 그 두 함수만 손보면 나머지는 그대로 동작한다.
- 과도한 트래픽을 피하려고 polite_delay를 둔다. 개인 리서치 용도로만 사용.
- 샌드박스처럼 courtauction 접속이 막힌 환경에서는 `python crawl.py --sample`로
  샘플 물건을 써서 파이프라인을 검증한다.

의존성 없음(표준 라이브러리 urllib만 사용).
"""
import os, sys, json, time, gzip, io
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BASE = "https://www.courtauction.go.kr"
SEARCH_EP = "/pgj/pgjsearch/searchControllerMain.on"
RIGHTS_EP = "/pgj/pgjsearch/selectCurstExmndc.on"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# 용도 표시명 → 우리 스키마 type
USAGE_TO_TYPE = {"아파트": "아파트", "오피스텔": "오피스텔",
                 "연립": "빌라", "다세대": "빌라", "빌라": "빌라"}


def load(n):
    with open(os.path.join(DATA, n), encoding="utf-8") as f:
        return json.load(f)


def save(n, o):
    with open(os.path.join(DATA, n), "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=2)


# --------------------- 세션 & 요청 ---------------------
class Session:
    """쿠키를 유지하며 courtauction에 요청. WebSquare는 JSESSIONID가 필요하다."""
    def __init__(self):
        self.cookie = ""

    def _headers(self, json_body=True):
        h = {"User-Agent": UA, "Referer": BASE + "/pgj/index.on",
             "Accept": "application/json, text/plain, */*",
             "Accept-Encoding": "gzip", "Origin": BASE}
        if json_body:
            h["Content-Type"] = "application/json;charset=UTF-8"
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def _read(self, resp):
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", "ignore")

    def bootstrap(self):
        """메인 페이지를 한 번 열어 세션 쿠키를 확보."""
        req = urllib.request.Request(BASE + "/pgj/index.on", headers=self._headers(False))
        with urllib.request.urlopen(req, timeout=20) as r:
            sc = r.headers.get_all("Set-Cookie") or []
            self.cookie = "; ".join(c.split(";")[0] for c in sc)
        return bool(self.cookie)

    def post_json(self, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(BASE + path, data=data, headers=self._headers(True))
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(self._read(r))


# --------------------- 요청 본문 / 응답 파싱 (개편 시 여기만 보정) ---------------------
def build_search_body(cfg, page):
    """검색 조건 → WebSquare 요청 본문.
    실제 필드명은 사이트 응답을 한 번 캡처해 맞추면 된다. 아래는 알려진 구조 근사."""
    return {
        "dma_pageInfo": {"pageNo": page, "pageSize": cfg.get("page_size", 40),
                         "totalYn": "Y"},
        "dma_srchGdsDtlSrch": {
            "bidDvsCd": "000331",            # 기일입찰
            "mvprpRletDvsCd": "00031R",      # 부동산
            "cortAuctnSrchCondDto": {
                "sidoList": cfg.get("sido", []),
                "lclsUtilCd": "",            # 용도 대분류(선택)
                "aeeEvlAmtFrom": cfg.get("min_appraisal", 0),
                "dspslDxdyFrom": cfg.get("sale_date_from_days", 0),
                "dspslDxdyTo": cfg.get("sale_date_to_days", 21),
            },
        },
    }


def _first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def parse_rows(resp):
    """응답 JSON에서 물건 행 리스트를 뽑아 공용 스키마로 변환."""
    # 응답 구조가 버전마다 달라 여러 경로를 시도
    rows = (_first(resp, "data", "result", "dlt_srchResult", default=None)
            or _first(resp.get("data", {}) if isinstance(resp.get("data"), dict) else {},
                      "dlt_srchResult", "list", default=None) or [])
    out = []
    for r in rows:
        usage = _first(r, "lclsUtilNm", "utilNm", "용도", default="") or ""
        typ = next((v for k, v in USAGE_TO_TYPE.items() if k in usage), "기타")
        appr = int(_first(r, "aeeEvlAmt", "감정평가액", "gamEvalAmt", default=0) or 0)
        minb = int(_first(r, "fstpbAmt", "lwsDspslPrc", "최저매각가격", default=0) or 0)
        fail = int(_first(r, "flbdNcnt", "yuchalCnt", "유찰횟수", default=0) or 0)
        case = _first(r, "csNo", "사건번호", "userCsNo", default="")
        court = _first(r, "cortOfcNm", "법원명", default="")
        addr = _first(r, "prptAddr", "소재지", "adongSdNm", default="")
        area = float(_first(r, "excluUseAr", "전용면적", default=0) or 0)
        lawd = _first(r, "adongCd", "bjdongCd", default="")
        if not (appr and case):
            continue
        out.append({
            "id": case, "court": court, "address": addr,
            "region": _region_from_addr(addr), "type": typ,
            "apt_name": _first(r, "bldgNm", "aptNm", "건물명", default="") or "",
            "lawd_cd": (str(lawd)[:5] if lawd else ""), "exclusive_area": area,
            "floor": _to_int(_first(r, "flr", "층", default=None)),
            "appraisal": appr, "min_bid": minb or appr,
            "fail_rounds": fail, "eviction": "normal", "market_price_override": None,
            "_case_key": {"csNo": case, "cortOfcCd": _first(r, "cortOfcCd", default="")},
        })
    return out


def _to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _region_from_addr(addr):
    """'경기도 수원시 …' → '경기 수원시' 형태로 축약(baseline 매칭용)."""
    if not addr:
        return ""
    parts = addr.split()
    if len(parts) >= 2:
        sido = parts[0].replace("특별시", "").replace("광역시", "").replace("특별자치시", "") \
            .replace("도", "") or parts[0]
        sido = {"서울": "서울", "경기": "경기", "인천": "인천", "부산": "부산"}.get(sido, sido)
        return f"{sido} {parts[1]}"
    return addr


# --------------------- 권리분석 자동 추출 ---------------------
def fetch_rights(sess, case_key, cfg):
    """현황조사서 JSON에서 임대차현황 → 대항력·인수보증금 추정."""
    try:
        body = {"dma_srchCsDtlInf": case_key}
        resp = sess.post_json(RIGHTS_EP, body)
    except Exception:
        return {}
    tenants = (_first(resp, "data", "result", default={}) or {})
    leases = _first(tenants, "dlt_lease", "임대차현황", "leaseList", default=[]) or []
    senior = False
    assumed = 0
    for lz in leases:
        deposit = int(_first(lz, "deposit", "보증금", "rtDpsAmt", default=0) or 0)
        opposing = _first(lz, "oppsBiztAbilYn", "대항력", default="")
        distrib = _first(lz, "dvdmYn", "배당요구여부", default="")
        # 대항력 있고 배당요구 안 함/불충분 → 낙찰자 인수 추정
        if str(opposing).startswith("Y") or "유" in str(opposing) or "있" in str(opposing):
            senior = True
            if str(distrib).startswith("N") or "무" in str(distrib) or "없" in str(distrib):
                assumed += deposit
    return {"senior_tenant": senior, "assumed_deposit": assumed,
            "special_rights": [], "lien_amount": 0}


# --------------------- 샘플(오프라인 검증용) ---------------------
def sample_properties():
    return load("properties.sample.json") if os.path.exists(os.path.join(DATA, "properties.sample.json")) \
        else load("properties.json")


# --------------------- main ---------------------
def crawl(cfg):
    sess = Session()
    if not sess.bootstrap():
        print("경고: 세션 확보 실패 — 응답 확인 필요", file=sys.stderr)
    props, seen = [], set()
    for page in range(1, cfg.get("max_pages", 5) + 1):
        try:
            resp = sess.post_json(SEARCH_EP, build_search_body(cfg, page))
        except urllib.error.HTTPError as e:
            print(f"검색 실패 p{page}: HTTP {e.code}", file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001
            print(f"검색 실패 p{page}: {type(e).__name__}", file=sys.stderr)
            break
        rows = parse_rows(resp)
        if not rows:
            break
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            props.append(r)
        time.sleep(cfg.get("polite_delay_sec", 0.8))
        if len(props) >= cfg.get("max_properties", 60):
            break
    props = props[: cfg.get("max_properties", 60)]

    # 유형 필터
    allow = set()
    for u in cfg.get("usage", []):
        allow.add(USAGE_TO_TYPE.get(u, u))
    if allow:
        props = [p for p in props if p["type"] in allow]

    # 권리분석 자동 추출
    if cfg.get("fetch_rights"):
        for p in props:
            ck = p.pop("_case_key", None)
            if ck:
                p.update(fetch_rights(sess, ck, cfg))
                time.sleep(cfg.get("polite_delay_sec", 0.8))
    else:
        for p in props:
            p.pop("_case_key", None)
    return props


def main():
    cfg = load("crawl-config.json")
    use_sample = "--sample" in sys.argv
    if use_sample:
        props = sample_properties()
        for p in props:
            p.pop("_case_key", None)
        print(f"[sample] {len(props)}건 사용")
    else:
        props = crawl(cfg)
        print(f"[crawl] {len(props)}건 수집")
        if not props:
            # 수집 실패 시 기존 properties.json 보존(사이트 개편 등)
            print("수집 0건 — 기존 properties.json 유지", file=sys.stderr)
            return
    save("properties.json", props)
    print("→ data/properties.json 저장")


if __name__ == "__main__":
    main()
