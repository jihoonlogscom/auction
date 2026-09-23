#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
예측 → 실제 결과 대조 · 고도화 학습 아카이브.

매 실행마다:
  1) analyze.py가 만든 data/analysis.json의 '예측'을 물건별로 스냅샷(data/predictions.json).
     - 물건이 사라져도 마지막 예측이 보존됨(경매가 끝나면 그 예측이 채점 대상이 된다).
  2) 매각기일이 지난(=결과가 나온) 물건의 '실제 결과'를 수집/병합(data/outcomes.json).
     - 실제 낙찰가·응찰자수·낙찰여부. 이후 재매도가(resale)는 알게 되는 대로 채워짐.
     - 실제 결과 수집은 법원경매정보 결과 조회(크롤러)로 시도하며, 실패 시 기존값 유지.
       사용자가 outcomes.json에 직접 실제값을 넣어도 된다.
  3) 예측 ↔ 실제를 조인해 오차와 보정 제안을 담은 학습 파일 생성(data/calibration.json).
     - 이 파일을 나중에 업로드하면, 오차를 근거로 baseline·경쟁도·비용 파라미터를 튜닝한다.

표준 라이브러리만 사용.
"""
import os, sys, json, statistics
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
KST = timezone(timedelta(hours=9))


def load(n, default=None):
    p = os.path.join(DATA, n)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(n, o):
    with open(os.path.join(DATA, n), "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=2)


def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


# --------------------- 1) 예측 스냅샷 ---------------------
def snapshot_predictions(analysis, predictions):
    day = (analysis.get("generated_at") or today())[:10]
    for r in analysis.get("properties", []):
        pred = {
            "snapshot_date": day, "sale_date": r.get("sale_date"),
            "region": r.get("region"), "type": r.get("type"),
            "appraisal": r.get("appraisal"), "min_bid": r.get("min_bid"),
            "fail_rounds": r.get("fail_rounds"),
            "market_price": r.get("market_price"),
            "recommended_bid": r.get("recommended_bid"),
            "predicted_win_prob": r.get("success_prob"),
            "predicted_ratio_mean": r.get("ratio_dist", {}).get("mean"),
            "predicted_ratio_std": r.get("ratio_dist", {}).get("std"),
            "expected_bidders": r.get("expected_bidders"),
            "predicted_net_profit": r.get("net_profit"),
            "predicted_roi": r.get("roi"),
            "liquidity": r.get("liquidity", {}).get("score"),
            "rights_level": r.get("rights_risk", {}).get("level"),
            "score": r.get("score"),
        }
        cur = predictions.get(r["id"])
        if cur:
            cur["first_seen"] = cur.get("first_seen", day)
            cur["last"] = pred            # 최신 예측 유지(기일 임박 예측이 채점 기준)
        else:
            predictions[r["id"]] = {"first_seen": day, "last": pred}
    return predictions


# --------------------- 2) 실제 결과 수집/병합 ---------------------
def reconcile_outcomes(predictions, outcomes, cfg):
    """매각기일이 지난 물건의 실제 결과를 채운다.
    라이브 수집은 법원경매정보 결과조회가 필요하나 비공식 경로라 여기서는
    (a) 이미 outcomes.json에 있는 값 유지 (b) 기일 지난 미확정 건을 pending으로 표기.
    실제 낙찰가/재매도가는 크롤러 결과조회 또는 사용자가 직접 채운다."""
    day = today()
    for pid, rec in predictions.items():
        sale_date = (rec.get("last") or {}).get("sale_date")
        if pid in outcomes:
            continue
        if sale_date and sale_date <= day:
            # TODO(crawl): 법원경매정보 결과조회로 won_bid/bidders 채우기
            outcomes[pid] = {"status": "pending", "note": "결과 대기(수집 필요)",
                             "won_bid": None, "bidders": None, "result": None,
                             "resale_price": None}
    return outcomes


# --------------------- 3) 예측 ↔ 실제 조인 · 오차 ---------------------
def build_calibration(predictions, outcomes, assumptions):
    records = []
    for pid, rec in predictions.items():
        out = outcomes.get(pid)
        if not out or out.get("status") == "pending" or not out.get("won_bid"):
            continue
        p = rec["last"]
        appr = p["appraisal"] or 0
        won = out["won_bid"]
        actual_ratio = round(100 * won / appr, 1) if appr else None
        would_win = (p["recommended_bid"] is not None and p["recommended_bid"] >= won)
        resale = out.get("resale_price")
        actual_profit = None
        if resale:
            actual_profit = resale - won - (out.get("resale_costs") or 0)

        err = {}
        if actual_ratio is not None and p.get("predicted_ratio_mean") is not None:
            err["ratio_err"] = round(actual_ratio - p["predicted_ratio_mean"], 1)   # +면 실제가 더 높음
        if out.get("bidders") is not None and p.get("expected_bidders") is not None:
            err["bidders_err"] = round(out["bidders"] - p["expected_bidders"], 1)
        if p.get("predicted_win_prob") is not None:
            err["win_brier"] = round((p["predicted_win_prob"] - (1 if would_win else 0)) ** 2, 3)
        if actual_profit is not None and p.get("predicted_net_profit"):
            err["profit_err"] = int(actual_profit - p["predicted_net_profit"])
            err["profit_err_pct"] = round((actual_profit - p["predicted_net_profit"]) / abs(p["predicted_net_profit"]), 3)
        if resale and p.get("market_price"):
            err["resale_vs_market_pct"] = round((resale - p["market_price"]) / p["market_price"], 3)

        records.append({
            "id": pid, "region": p["region"], "type": p["type"], "fail_rounds": p["fail_rounds"],
            "sale_date": p.get("sale_date"), "snapshot_date": p.get("snapshot_date"),
            "predicted": {"ratio_mean": p.get("predicted_ratio_mean"),
                          "win_prob": p.get("predicted_win_prob"),
                          "bidders": p.get("expected_bidders"),
                          "recommended_bid": p.get("recommended_bid"),
                          "net_profit": p.get("predicted_net_profit"),
                          "roi": p.get("predicted_roi"), "score": p.get("score"),
                          "liquidity": p.get("liquidity")},
            "actual": {"result": out.get("result"), "won_bid": won, "ratio": actual_ratio,
                       "bidders": out.get("bidders"), "would_win": would_win,
                       "resale_price": resale, "actual_profit": actual_profit,
                       "resale_date": out.get("resale_date")},
            "errors": err,
        })
    return records, summarize(records, assumptions)


def _bias(vals):
    return round(statistics.mean(vals), 2) if vals else None


def summarize(records, assumptions):
    ratio_e = [r["errors"]["ratio_err"] for r in records if "ratio_err" in r["errors"]]
    bidd_e = [r["errors"]["bidders_err"] for r in records if "bidders_err" in r["errors"]]
    brier = [r["errors"]["win_brier"] for r in records if "win_brier" in r["errors"]]
    prof_e = [r["errors"]["profit_err_pct"] for r in records if "profit_err_pct" in r["errors"]]
    mkt_e = [r["errors"]["resale_vs_market_pct"] for r in records if "resale_vs_market_pct" in r["errors"]]
    n_resale = sum(1 for r in records if r["actual"]["resale_price"])

    # 지역·유형별 낙찰가율 오차
    by = {}
    for r in records:
        if "ratio_err" not in r["errors"]:
            continue
        by.setdefault(f'{r["region"]}·{r["type"]}', []).append(r["errors"]["ratio_err"])
    by_rt = {k: {"ratio_bias": _bias(v), "n": len(v)} for k, v in sorted(by.items())}

    sug = []
    rb = _bias(ratio_e)
    if rb is not None and abs(rb) >= 2:
        sug.append(f"전체 낙찰가율이 예측보다 평균 {rb:+.1f}%p → baseline mean을 {rb:+.1f}%p 방향으로 보정 검토")
    for k, v in by_rt.items():
        if v["ratio_bias"] is not None and abs(v["ratio_bias"]) >= 3 and v["n"] >= 2:
            sug.append(f"{k}: 낙찰가율 예측 오차 {v['ratio_bias']:+.1f}%p(n={v['n']}) → 해당 셀 baseline mean 보정")
    bb = _bias(bidd_e)
    if bb is not None and abs(bb) >= 1:
        sug.append(f"응찰자 수가 예측보다 평균 {bb:+.1f}명 → competition base bidders {bb:+.1f} 방향 보정")
    if brier:
        bs = round(statistics.mean(brier), 3)
        sug.append(f"성공률 Brier {bs} (0에 가까울수록 정확) → 높으면 std·경쟁도 재조정")
    pb = _bias(prof_e)
    mb = _bias(mkt_e)
    if mb is not None and abs(mb) >= 0.03:
        sign = "높게" if mb > 0 else "낮게"
        sug.append(f"실제 재매도가가 예측 시세보다 평균 {mb*100:+.0f}% {sign} 형성 → 시세 추정(층·향 보정·중앙값) 재보정")

    return {
        "n_resolved": len(records), "n_with_resale": n_resale,
        "ratio_bias": rb, "ratio_mae": round(statistics.mean([abs(x) for x in ratio_e]), 2) if ratio_e else None,
        "bidders_bias": bb, "win_brier": round(statistics.mean(brier), 3) if brier else None,
        "profit_bias_pct": pb, "market_bias_pct": mb, "by_region_type": by_rt,
        "suggested_adjustments": sug,
    }


def main():
    analysis = load("analysis.json")
    if not analysis:
        print("analysis.json 없음 — analyze.py 먼저 실행", file=sys.stderr)
        return
    predictions = load("predictions.json", {}) or {}
    outcomes = load("outcomes.json", {}) or {}

    predictions = snapshot_predictions(analysis, predictions)
    outcomes = reconcile_outcomes(predictions, outcomes, load("crawl-config.json", {}))
    records, summary = build_calibration(predictions, outcomes, analysis.get("assumptions", {}))

    save("predictions.json", predictions)
    save("outcomes.json", outcomes)
    save("calibration.json", {
        "generated_at": datetime.now(KST).isoformat(),
        "how_to_use": "이 파일을 업로드해 예측 오차 기반으로 baselines/assumptions 튜닝을 요청하세요.",
        "assumptions_snapshot": {k: analysis.get("assumptions", {}).get(k)
                                 for k in ("target_profit_rate", "min_roi_floor", "win_target",
                                           "competition", "score_weights", "liquidity_gate")},
        "records": records, "summary": summary,
    })
    s = summary
    print(f"예측 스냅샷 {len(predictions)}건 · 채점 완료 {s['n_resolved']}건(재매도 {s['n_with_resale']}) "
          f"· 낙찰가율 bias {s['ratio_bias']} · 응찰자 bias {s['bidders_bias']}")
    for x in s["suggested_adjustments"]:
        print("  - " + x)


if __name__ == "__main__":
    main()
