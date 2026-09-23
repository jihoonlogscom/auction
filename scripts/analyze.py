#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
경매 물건 분석 엔진 v2.

지표: ① 적정 입찰가  ② 낙찰 성공률(경쟁도 반영)  ③ 재매매 순이익(정밀 세금·권리 인수 반영)
시세: 국토부 실거래가 OpenAPI(아파트/오피스텔/연립다세대) + 층·향·면적 보정.
권리분석: 대항력 임차 인수보증금·특수권리(유치권 등) → 순이익 차감 + 권리 위험도.
세금: 취득세(구간·농특·교육세·중과) / 양도세(단기중과·누진·장특공제·지방소득세).
표준 라이브러리만 사용. GitHub Actions에서 secrets.MOLIT_SERVICE_KEY 주입.
"""
import os, sys, json, math, statistics, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load(n):
    with open(os.path.join(DATA, n), encoding="utf-8") as f:
        return json.load(f)


def save(n, o):
    with open(os.path.join(DATA, n), "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=2)


def norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _parse_date(s):
    if not s:
        return None
    s = str(s)[:10]
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def filter_recent_history(history, days):
    """과거 경매 실적을 최근 days일로 제한(기준일=재매도일 우선, 없으면 낙찰일).
    날짜가 없는 기록은 보존한다."""
    if not days or days <= 0:
        return history
    cutoff = datetime.now(timezone(timedelta(hours=9))).date() - timedelta(days=days)
    out = []
    for h in history:
        ref = _parse_date(h.get("resale_date")) or _parse_date(h.get("result_date"))
        if ref is None or ref >= cutoff:
            out.append(h)
    return out


# ----------------------- 시세: 국토부 실거래가 -----------------------
ENDPOINTS = {
    "아파트": ("1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev", ("aptNm", "아파트")),
    "오피스텔": ("1613000/RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade", ("offiNm", "단지")),
    "빌라": ("1613000/RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade", ("mhouseNm", "연립다세대")),
}
BASE = "https://apis.data.go.kr/"


def recent_months(n=6):
    d = datetime.now(timezone(timedelta(hours=9)))
    y, m, out = d.year, d.month, []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def fetch_lawd_trades(typ, lawd_cd, key, months):
    """(유형, 법정동코드)의 최근 months개월 실거래 전체를 한 번만 조회.
    반환: [(price, name, area, floor)]. 시군구 단위라 여러 물건이 공유(캐시 단위)."""
    ep = ENDPOINTS.get(typ)
    if not key or not lawd_cd or not ep:
        return []
    path, name_tags = ep
    rows = []
    try:
        for ym in recent_months(months):
            q = urllib.parse.urlencode({"serviceKey": key, "LAWD_CD": lawd_cd,
                                        "DEAL_YMD": ym, "numOfRows": "1000"})
            with urllib.request.urlopen(f"{BASE}{path}?{q}", timeout=20) as r:
                root = ET.fromstring(r.read().decode("utf-8", "ignore"))
            for it in root.iter("item"):
                def g(*tags):
                    for t in tags:
                        e = it.find(t)
                        if e is not None and e.text:
                            return e.text.strip()
                    return ""
                amt = g("dealAmount", "거래금액").replace(",", "")
                if not amt:
                    continue
                try:
                    price = int(amt) * 10000
                except ValueError:
                    continue
                nm = g(*name_tags)
                try:
                    area = float(g("excluUseAr", "전용면적") or 0) or None
                except ValueError:
                    area = None
                try:
                    floor = int(g("floor", "층") or 0) or None
                except ValueError:
                    floor = None
                rows.append((price, nm, area, floor))
    except Exception:  # noqa: BLE001 — 부분 실패는 모인 만큼 사용
        return rows
    return rows


def build_trade_cache(props, key, months=6, workers=8):
    """모든 물건의 (유형, 법정동코드)를 중복 제거해 한 번씩만 동시 조회.
    같은 시군구 물건이 수십 건이어도 시세 조회는 시군구당 1회로 줄어든다."""
    uniq = sorted({(p.get("type"), p.get("lawd_cd")) for p in props
                   if p.get("lawd_cd") and ENDPOINTS.get(p.get("type"))})
    if not key or not uniq:
        return {}

    def work(k):
        return k, fetch_lawd_trades(k[0], k[1], key, months)

    cache = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for k, rows in ex.map(work, uniq):
            cache[k] = rows
    print(f"[analyze] 시세 캐시: {len(uniq)}개 (유형·시군구) × {months}개월 동시 조회 완료", file=sys.stderr)
    return cache


def floor_bucket(prop):
    fl, tot = prop.get("floor"), prop.get("total_floors")
    if not fl:
        return "mid"
    if fl <= 3:
        return "low"
    if tot and fl >= tot * 0.75:
        return "high"
    return "mid"


def market_price(prop, cache, a):
    """캐시된 시군구 실거래에서 단지·면적으로 필터 → 중앙값에 층·향 보정.
    실패 시 override→감정가 폴백. 캐시라 물건당 추가 네트워크 호출 없음."""
    rows = cache.get((prop.get("type"), prop.get("lawd_cd")))
    if rows:
        name = (prop.get("apt_name") or "").strip()
        area = prop.get("exclusive_area") or 0
        prices = []
        for price, nm, ar, _fl in rows:
            if name and name not in (nm or "") and (nm or "") not in name:
                continue
            if area and ar and abs(ar - area) > 10:
                continue
            prices.append(price)
        if prices:
            base = statistics.median(prices)
            adj = 1 + a["floor_adj"].get(floor_bucket(prop), 0) \
                    + a["orientation_adj"].get(prop.get("orientation", ""), 0)
            return int(base * adj), f"molit({len(prices)})+보정", len(prices)
    if prop.get("market_price_override"):
        return int(prop["market_price_override"]), "override", None
    return int(prop["appraisal"]), "appraisal_fallback", None


# ----------------------- 세금 (정밀) -----------------------
def acquisition_tax(price, area, a):
    t = a["tax"]
    eok = price / 1e8
    if price <= 6e8:
        base = 0.01
    elif price <= 9e8:
        base = (eok * 2 / 3 - 3) / 100      # 6~9억 구간 1~3% 선형
    else:
        base = 0.03
    tax = price * base * t["acq_edu_multiplier"]   # + 지방교육세(근사)
    if area > 85:
        tax += price * t["acq_nongtuk_over85"]     # 농특세
    if t.get("multi_home"):
        tax += price * t["acq_multi_home_surcharge"]
    return tax


def progressive(base, brackets):
    for cap, rate, ded in brackets:
        if base <= cap:
            return base * rate - ded
    cap, rate, ded = brackets[-1]
    return base * rate - ded


def capital_gains_tax(gain, months, a):
    t = a["tax"]
    if gain <= 0:
        return 0.0
    taxable = gain - t["cgt_basic_deduction"]
    if taxable <= 0:
        return 0.0
    if months < 12:
        cgt = taxable * 0.70                        # 1년 미만 단기중과
    elif months < 24:
        cgt = taxable * 0.60                        # 2년 미만
    else:
        years = months // 12
        if t.get("cgt_single_home"):
            ltd = min(0.80, years * 0.08) if years >= 3 else 0.0
        else:
            ltd = min(t["cgt_ltd_max"], years * t["cgt_ltd_rate_per_year"]) if years >= 3 else 0.0
        after = taxable * (1 - ltd)
        cgt = progressive(after, a["progressive_brackets"])
        cgt += after * t.get("cgt_adjusted_surcharge", 0.0)   # 조정지역 다주택 중과 %p
    cgt += cgt * t["cgt_local_surtax"]              # 지방소득세 10%
    return max(cgt, 0.0)


# ----------------------- 부대비용 + 권리 인수 -----------------------
def assumed_rights_cost(prop):
    return (prop.get("assumed_deposit") or 0) + (prop.get("lien_amount") or 0)


def net_profit(bid, sale, prop, a):
    months = prop.get("holding_months", a["holding_months"])
    area = prop.get("exclusive_area") or 0
    acq = acquisition_tax(bid, area, a)
    evic = a["eviction_cost"].get(prop.get("eviction", "normal"), a["eviction_cost"]["normal"])
    repair = area * a["repair_cost_per_m2"]
    holding = bid * a["holding_cost_annual_rate"] * months / 12 + a["monthly_mgmt_fee"] * months
    broker = sale * a["brokerage_rate"]
    fixed = a["extra_fixed"]
    assumed = assumed_rights_cost(prop)            # 인수 보증금·유치권 등
    pre_gain = sale - bid - assumed - (acq + evic + repair + holding + broker + fixed)
    cgt = capital_gains_tax(pre_gain, months, a)
    items = {"인수권리": round(assumed), "취득세": round(acq), "명도비": round(evic),
             "수리비": round(repair), "보유비용": round(holding), "매도중개": round(broker),
             "등기·법무 등": round(fixed), "양도세": round(cgt)}
    costs = sum(items.values())
    profit = sale - bid - costs
    invested = bid + assumed + items["취득세"] + evic + repair
    return profit, (profit / invested if invested else 0), costs, items


def fair_bid(sale, prop, a):
    lo, hi = 0.0, sale
    for _ in range(60):
        mid = (lo + hi) / 2
        _, roi, _, _ = net_profit(mid, sale, prop, a)
        if roi >= a["target_profit_rate"]:
            lo = mid
        else:
            hi = mid
    return int(lo)


# ----------------------- 권리 위험도 -----------------------
def rights_risk(prop):
    score, flags = 0, []
    if prop.get("senior_tenant"):
        score += 35
        flags.append("대항력 임차인")
    dep = prop.get("assumed_deposit") or 0
    if dep:
        score += min(30, dep / 1e8 * 25)
        flags.append(f"인수보증금 {int(dep/1e4):,}만")
    sr = prop.get("special_rights") or []
    if sr:
        score += min(35, len(sr) * 18)
        flags += sr
    if prop.get("lien_amount"):
        score += 10
    lvl = "높음" if score >= 60 else "보통" if score >= 30 else "낮음"
    return {"score": min(100, round(score)), "level": lvl, "flags": flags}


# ----------------------- 성공률 (경쟁도) -----------------------
def base_ratio(prop, baselines):
    b = (baselines["sale_ratio"].get(prop.get("region"), {}).get(prop.get("type"))
         or baselines["default"].get(prop.get("type")) or baselines["default"]["기타"])
    return float(b["mean"]), float(b["std"]), float(b.get("bidders", 5))


def ratio_and_bidders(prop, sale, baselines, history, a):
    mean, std, base_bid = base_ratio(prop, baselines)
    # 과거사례 blend (낙찰가율)
    samples = [100 * h["won_bid"] / h["appraisal"] for h in history
               if h.get("region") == prop.get("region") and h.get("type") == prop.get("type") and h.get("appraisal")]
    if samples:
        w, n = baselines.get("blend_prior_weight", 8), len(samples)
        mean = (w * mean + n * statistics.mean(samples)) / (w + n)
        if n >= 2:
            std = (w * std + n * max(statistics.pstdev(samples), 3)) / (w + n)
    mean -= baselines.get("fail_round_penalty", 6) * (prop.get("fail_rounds") or 0)

    # 예상 입찰자 수(경쟁도): 물건 매력도(시세/감정가) ↑ → 경쟁 ↑, 유찰 → ↓
    c = a["competition"]
    attract = 1 + c["discount_boost"] * max(-0.3, (sale / prop["appraisal"]) - 0.95)
    lam = base_bid * attract * (1 - c["fail_damp"] * (prop.get("fail_rounds") or 0))
    hb = [h["bidders"] for h in history
          if h.get("region") == prop.get("region") and h.get("type") == prop.get("type") and h.get("bidders")]
    if hb:
        lam = (1 - c["history_weight"]) * lam + c["history_weight"] * statistics.mean(hb)
    lam = max(1.0, lam)
    # 경쟁 → 기대 낙찰가율 상향
    mean_adj = mean + c["k_ratio_per_bidder"] * (lam - c["ref_bidders"])
    return mean_adj, max(std, 3), round(lam, 1), round(mean, 1)


def success_prob(bid, appraisal, mean_adj, std):
    return norm_cdf((100 * bid / appraisal - mean_adj) / std)


# ----------------------- 환금성(유동성) 점수 -----------------------
def liquidity_score(prop, baselines, a, trade_count):
    """빠른 재매도 가능성 0~100. 실거래 회전율·유형·수요·면적·가격대 반영."""
    L = a["liquidity"]
    tliq = L["type"].get(prop.get("type"), 0.3)
    _, _, bidders = base_ratio(prop, baselines)          # 지역·유형 수요(입찰자) 대리지표
    demand = min(1.0, bidders / 10.0)
    # 실거래 회전율: 실측 있으면 사용, 없으면(폴백) 수요로 대체
    tf = min(1.0, trade_count / L["trade_count_full"]) if trade_count is not None else demand
    area = prop.get("exclusive_area") or 0
    size = 1.0 if L["size_ideal_min"] <= area <= L["size_ideal_max"] else 0.6
    pb = 0.6 if prop["appraisal"] > L["price_high"] else 1.0
    raw = 0.30 * tliq + 0.25 * demand + 0.25 * tf + 0.10 * size + 0.10 * pb
    score = round(raw * 100)
    grade = "A" if score >= 75 else "B" if score >= 55 else "C" if score >= 35 else "D"
    signals = []
    if trade_count is not None:
        signals.append(f"최근거래 {trade_count}건")
    signals.append({"아파트": "아파트(높음)", "오피스텔": "오피스텔(중)",
                    "빌라": "빌라(낮음)"}.get(prop.get("type"), "기타"))
    if area and not (L["size_ideal_min"] <= area <= L["size_ideal_max"]):
        signals.append("비선호 면적")
    if prop["appraisal"] > L["price_high"]:
        signals.append("고가 구간")
    return {"score": score, "grade": grade, "signals": signals}


# ----------------------- 적정 입찰가 고도화(전략가) -----------------------
def bid_strategies(sale, prop, a, appr, mean_adj, std, liq):
    """세 가지 전략가를 산출.
      ev_optimal : 기대가치(성공률×순이익) 최대  → 권장가의 기준
      safe_max   : 환금성 반영 목표수익률을 지키는 최대 입찰가(상한)
      win_target : 목표 낙찰확률을 확보하는 최소 입찰가
    """
    lo, hi = prop["min_bid"], int(appr * 1.05)
    min_floor = a.get("min_roi_floor", 0.08)
    best_b, best_ev = None, -1e18            # 최소수익률 만족 중 EV 최대
    any_b, any_ev = float(prop["min_bid"]), -1e18   # 흑자 중 EV 최대(폴백)
    for i in range(121):
        b = lo + (hi - lo) * i / 120
        pr, roi, _, _ = net_profit(b, sale, prop, a)
        ev = success_prob(b, appr, mean_adj, std) * pr
        if pr > 0 and ev > any_ev:
            any_ev, any_b = ev, b
        if roi >= min_floor and ev > best_ev:
            best_ev, best_b = ev, b
    ev_opt = int(best_b if best_b is not None else any_b)

    # 환금성 낮을수록 더 큰 안전마진(목표수익률↑)
    floor = a["target_profit_rate"] + (1 - liq / 100.0) * a.get("illiquid_margin_add", 0.10)
    lo2, hi2 = 0.0, float(sale)
    for _ in range(60):
        m = (lo2 + hi2) / 2
        if net_profit(m, sale, prop, a)[1] >= floor:
            lo2 = m
        else:
            hi2 = m
    safe_max = int(lo2)

    wt = a.get("win_target", 0.6)
    lo3, hi3 = float(prop["min_bid"]), float(int(appr * 1.2))
    for _ in range(60):
        m = (lo3 + hi3) / 2
        if success_prob(m, appr, mean_adj, std) >= wt:
            hi3 = m
        else:
            lo3 = m
    win_tgt = int(hi3)
    return {"ev_optimal": ev_opt, "safe_max": safe_max, "win_target": win_tgt,
            "roi_floor": round(floor, 3), "min_roi_floor": min_floor}, best_ev


# ----------------------- 종합 점수(환금성 최우선) -----------------------
def composite_score(res, a):
    w = a["score_weights"]
    liq = res["liquidity"]["score"]
    profit_s = max(0.0, min(1.0, res["roi"] / 0.30)) * 100      # ROI 30%면 만점
    win_s = res["success_prob"] * 100
    rights_s = 100 - res["rights_risk"]["score"]
    margin = res.get("discount_vs_market") or 0
    margin_s = max(0.0, min(1.0, margin / 0.20)) * 100          # 시세 대비 20% 할인이면 만점
    base = (w["liquidity"] * liq + w["profit"] * profit_s + w["win"] * win_s
            + w["rights"] * rights_s + w["margin"] * margin_s)
    gate = a["liquidity_gate"] + (1 - a["liquidity_gate"]) * (liq / 100.0)  # 환금성 게이트
    total = base * gate
    if res["net_profit"] <= 0:                                  # 순손실이면 상한 억제
        total = min(total, 20)
    br = {"환금성": round(liq), "수익성": round(profit_s), "성공률": round(win_s),
          "권리안전": round(rights_s), "안전마진": round(margin_s)}
    return round(total), br


# ----------------------- 물건 분석 -----------------------
def analyze_property(prop, baselines, a, history, cache):
    appr = prop["appraisal"]
    sale, src, trade_count = market_price(prop, cache, a)
    mean_adj, std, bidders, mean_raw = ratio_and_bidders(prop, sale, baselines, history, a)
    liq = liquidity_score(prop, baselines, a, trade_count)

    strat, _ = bid_strategies(sale, prop, a, appr, mean_adj, std, liq["score"])
    # 권장가 = 최소수익률을 지키는 범위의 기대가치 최적가(최저가 이상)
    rec = max(strat["ev_optimal"], prop["min_bid"])
    profit, roi, costs, items = net_profit(rec, sale, prop, a)
    win = success_prob(rec, appr, mean_adj, std)
    ev = win * profit
    risk = rights_risk(prop)

    curve = []
    lo, hi = prop["min_bid"], int(appr * 1.05)
    for i in range(21):
        b = lo + (hi - lo) * i / 20
        pr, r, _, _ = net_profit(b, sale, prop, a)
        curve.append({"bid": int(b), "win": round(success_prob(b, appr, mean_adj, std), 4),
                      "profit": int(pr), "roi": round(r, 4)})

    res = {"id": prop["id"], "court": prop.get("court"), "address": prop.get("address"),
           "region": prop.get("region"), "type": prop.get("type"), "apt_name": prop.get("apt_name"),
           "exclusive_area": prop.get("exclusive_area"), "floor": prop.get("floor"),
           "orientation": prop.get("orientation"), "appraisal": appr, "min_bid": prop["min_bid"],
           "fail_rounds": prop.get("fail_rounds", 0), "sale_date": prop.get("sale_date"),
           "market_price": sale, "market_source": src,
           "recommended_bid": rec, "bid_strategies": strat,
           "discount_vs_market": round(1 - rec / sale, 4) if sale else None,
           "success_prob": round(win, 4), "expected_bidders": bidders,
           "net_profit": int(profit), "roi": round(roi, 4), "expected_value": int(ev),
           "cost_items": items, "total_cost": int(costs), "assumed_rights": assumed_rights_cost(prop),
           "rights_risk": risk, "liquidity": liq,
           "ratio_dist": {"mean": round(mean_adj, 1), "mean_raw": mean_raw, "std": round(std, 1)},
           "curve": curve}
    res["score"], res["score_breakdown"] = composite_score(res, a)
    return res


# ----------------------- 백테스트 · 통계 -----------------------
def backtest(history):
    rows = []
    for h in history:
        if not h.get("resale_price") or not h.get("won_bid"):
            continue
        profit = h["resale_price"] - h["won_bid"] - h.get("costs", 0)
        inv = h["won_bid"] + h.get("costs", 0)
        rows.append({"id": h["id"], "region": h["region"], "type": h["type"],
                     "won_bid": h["won_bid"], "resale_price": h["resale_price"], "net_profit": profit,
                     "roi": round(profit / inv, 4) if inv else 0,
                     "sale_ratio": round(100 * h["won_bid"] / h["appraisal"], 1) if h.get("appraisal") else None,
                     "bidders": h.get("bidders"), "resale_date": h.get("resale_date")})
    ps = [r["net_profit"] for r in rows]
    rs = [r["roi"] for r in rows]
    s = {"n": len(rows),
         "avg_net_profit": int(statistics.mean(ps)) if ps else 0,
         "median_net_profit": int(statistics.median(ps)) if ps else 0,
         "avg_roi": round(statistics.mean(rs), 4) if rs else 0,
         "win_rate_positive": round(sum(1 for p in ps if p > 0) / len(ps), 4) if ps else 0,
         "total_net_profit": int(sum(ps))}
    return {"summary": s, "cases": rows}


def region_stats(history):
    by = {}
    for h in history:
        if not h.get("appraisal"):
            continue
        by.setdefault(f'{h["region"]}·{h["type"]}', {"r": [], "b": []})
        by[f'{h["region"]}·{h["type"]}']["r"].append(100 * h["won_bid"] / h["appraisal"])
        if h.get("bidders"):
            by[f'{h["region"]}·{h["type"]}']["b"].append(h["bidders"])
    return {k: {"avg_sale_ratio": round(statistics.mean(v["r"]), 1),
                "avg_bidders": round(statistics.mean(v["b"]), 1) if v["b"] else None,
                "n": len(v["r"])} for k, v in sorted(by.items())}


def main():
    key = os.environ.get("MOLIT_SERVICE_KEY", "").strip()
    props = load("properties.json")
    baselines = load("baselines.json")
    a = load("assumptions.json")
    history_all = load("auction-history.json")
    lookback = a.get("history_lookback_days", 30)
    history = filter_recent_history(history_all, lookback)   # 과거 실적은 최근 N일만

    months = a.get("molit_months", 6)
    workers = a.get("molit_workers", 8)
    cache = build_trade_cache(props, key, months, workers)   # 시군구당 1회, 동시 조회

    results = [analyze_property(p, baselines, a, history, cache) for p in props]
    results.sort(key=lambda r: r["score"], reverse=True)
    save("analysis.json", {
        "generated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "market_source_used": "molit_api" if key else "fallback(override/appraisal)",
        "history_lookback_days": lookback,
        "history_used": len(history), "history_total": len(history_all),
        "assumptions": a, "properties": results,
        "backtest": backtest(history), "region_stats": region_stats(history)})
    print(f"analyzed {len(results)} → data/analysis.json")
    for r in results:
        lq = r["liquidity"]
        print(f'  [{r["score"]:>3}점] {r["apt_name"] or r["type"]} {r["region"]}: '
              f'적정 {r["recommended_bid"]:,} / 성공률 {r["success_prob"]*100:.0f}% / '
              f'순이익 {r["net_profit"]:,}(ROI {r["roi"]*100:.1f}%) / '
              f'환금성 {lq["grade"]}({lq["score"]}) / 권리 {r["rights_risk"]["level"]}')


if __name__ == "__main__":
    main()
