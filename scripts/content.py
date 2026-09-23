#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
경매 블로그 콘텐츠 생성기 (별도 패키지).

매일 배치의 track 다음 단계로 실행:
  1) analysis.json에서 종합점수 top을 뽑아, 이미 발행한 사건(data/published.json)은 제외
     하고 다음 순위로 채워 최대 N편(기본 10) 선정.
  2) 현재 물건으로 N편을 못 채우면 과거 사건(실적)으로 채운다.
  3) 물건 1건당 1편의 스타일 HTML(티스토리·워드프레스용, 표·스타일 인라인)을 생성해
     content/<날짜>/ 에 저장하고, 발행한 사건은 원장에 기록(영구 중복 방지).

스타일: 전문 분석(수치 표) + 초보 친화 스토리텔링 + 권리·명도 리스크 + '살까 말까' 결론.
표준 라이브러리만 사용.
"""
import os, sys, json, re
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONTENT = os.path.join(ROOT, "content")
KST = timezone(timedelta(hours=9))
DAILY_COUNT = 10


def load(n, default=None):
    p = os.path.join(DATA, n)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


# ----------------------- 포맷 -----------------------
def won(n):
    if n is None:
        return "—"
    neg = n < 0
    n = abs(int(round(n)))
    eok, man = n // 10**8, round((n % 10**8) / 10**4)
    s = f"{eok}억 {man:,}만" if eok else f"{man:,}만"
    return ("−" if neg else "") + s + "원"


def pct(x):
    return f"{x*100:.0f}%" if x is not None else "—"


def slugify(s):
    return re.sub(r"[^0-9A-Za-z가-힣]+", "-", str(s)).strip("-")[:60]


def verdict(r):
    hi = (r.get("rights_risk") or {}).get("level") == "높음"
    if r["net_profit"] > 0 and r["success_prob"] >= 0.35 and r["roi"] >= 0.10 and not hi:
        return "입찰 적합", "이 물건은 수익성·낙찰 가능성·권리 안전성이 고루 갖춰져 적극 검토할 만합니다."
    if r["net_profit"] > 0 and r["roi"] >= 0.05 and not hi:
        return "조건부 도전", "수익은 나지만 낙찰 경쟁이나 마진이 빠듯합니다. 상한선을 지키는 전제에서만 도전하세요."
    return "보류 권장", "현재 조건에서는 순이익이 얇거나 권리 위험이 커서 무리한 입찰은 권하지 않습니다."


# ----------------------- HTML 조각 -----------------------
STYLE = """
<style>
.auc{max-width:820px;margin:0 auto;font-family:'Pretendard',system-ui,'Malgun Gothic',sans-serif;line-height:1.75;color:#1f2430}
.auc h2{font-size:1.5em;margin:1.6em 0 .5em;border-left:5px solid #E0A82E;padding-left:.5em}
.auc .lead{font-size:1.05em;color:#3a4252;background:#fbf7ec;border:1px solid #f0e4c4;border-radius:12px;padding:16px 18px}
.auc table{width:100%;border-collapse:collapse;margin:.8em 0;font-size:.97em}
.auc th,.auc td{border:1px solid #e5e8ee;padding:9px 12px;text-align:left}
.auc th{background:#f5f7fa;color:#5b6473;width:38%}
.auc td.num{text-align:right;font-variant-numeric:tabular-nums}
.auc .kpis{display:flex;flex-wrap:wrap;gap:10px;margin:1em 0}
.auc .kpi{flex:1 1 140px;border:1px solid #e5e8ee;border-radius:12px;padding:12px 14px;background:#fff}
.auc .kpi .k{font-size:.82em;color:#8b94a7}
.auc .kpi .v{font-size:1.25em;font-weight:800;margin-top:3px}
.auc .gold{color:#B9821A}.auc .jade{color:#1f9d63}.auc .rust{color:#d23b40}.auc .blue{color:#2f6df0}
.auc .verdict{display:inline-block;font-weight:800;border-radius:999px;padding:6px 16px;font-size:1.05em}
.auc .v-go{background:#e5f7ee;color:#1f9d63}.auc .v-hold{background:#fbf1d8;color:#B9821A}.auc .v-skip{background:#fbe6e6;color:#d23b40}
.auc .warn{background:#fff6f6;border:1px solid #f3caca;border-radius:10px;padding:12px 14px;color:#a3363a}
.auc .disc{font-size:.85em;color:#8b94a7;border-top:1px solid #eee;margin-top:2em;padding-top:1em}
.auc .tag{display:inline-block;font-size:.8em;color:#5b6473;border:1px solid #dfe3ea;border-radius:999px;padding:2px 10px;margin-right:6px}
</style>
"""


def kpi(k, v, cls=""):
    return f'<div class="kpi"><div class="k">{k}</div><div class="v {cls}">{v}</div></div>'


def cost_table(items):
    order = ["인수권리", "취득세", "명도비", "수리비", "보유비용", "매도중개", "등기·법무 등", "양도세"]
    rows = "".join(f'<tr><th>− {k}</th><td class="num">{won(items[k])}</td></tr>'
                   for k in order if items.get(k))
    return rows


def generate_post(r, rank):
    vt, vexpl = verdict(r)
    vcls = {"입찰 적합": "v-go", "조건부 도전": "v-hold", "보류 권장": "v-skip"}[vt]
    lq = r.get("liquidity") or {}
    rk = r.get("rights_risk") or {}
    st = r.get("bid_strategies") or {}
    area = r.get("exclusive_area")
    pyeong = f"({area/3.3058:.0f}평)" if area else ""
    title = (f"[{r['region']} {r['type']} 경매] {r.get('apt_name') or r['type']} "
             f"감정가 {won(r['appraisal'])}·적정입찰가 {won(r['recommended_bid'])} "
             f"낙찰 성공률·예상수익 분석")
    disc = r.get("discount_vs_market")
    disc_txt = f"시세보다 약 {disc*100:.0f}% 낮은 값" if disc and disc > 0 else "시세 대비 메리트 제한적"

    # 스토리 인트로(초보 친화)
    intro = (f"오늘 살펴볼 물건은 <b>{r['region']} {r.get('apt_name') or r['type']}</b>입니다. "
             f"감정가 {won(r['appraisal'])}에 {r['fail_rounds']}회 유찰된 상태로, 최저가는 {won(r['min_bid'])}까지 내려와 있습니다. "
             f"데이터로 계산한 <b>적정 입찰가는 {won(r['recommended_bid'])}</b>({disc_txt})이고, "
             f"그 가격에 넣었을 때 낙찰 성공률은 <b>{pct(r['success_prob'])}</b>, 재매도 시 예상 순이익은 "
             f"<b class='{'jade' if r['net_profit']>=0 else 'rust'}'>{won(r['net_profit'])}</b>로 추정됩니다.")

    kpis = "".join([
        kpi("종합 점수", f"{r['score']}점", "gold"),
        kpi("환금성", f"{lq.get('grade','—')} ({lq.get('score','—')})", "blue"),
        kpi("적정 입찰가", won(r["recommended_bid"]), "gold"),
        kpi("낙찰 성공률", pct(r["success_prob"]), "blue"),
        kpi("예상 순이익", won(r["net_profit"]), "jade" if r["net_profit"] >= 0 else "rust"),
        kpi("예상 ROI", f"{r['roi']*100:.1f}%", "jade" if r["roi"] >= 0 else "rust"),
    ])

    rights_html = ""
    flags = rk.get("flags") or []
    if rk.get("level") in ("보통", "높음") or flags:
        rights_html = (f'<div class="warn"><b>권리 위험도: {rk.get("level","—")}</b> '
                       f'({", ".join(flags) if flags else "특이사항 점검 필요"})<br>'
                       f'인수해야 할 보증금·특수권리가 있는지 매각물건명세서와 현황조사서를 반드시 확인하세요. '
                       f'인수 금액은 실질 취득원가에 더해집니다.</div>')
    else:
        rights_html = ('<p>말소기준권리 이후 권리가 소멸되는 일반적인 구조로, 현재 파악된 인수 위험은 낮습니다. '
                       '다만 실제 입찰 전 매각물건명세서 확인은 필수입니다.</p>')

    body = f"""{STYLE}
<div class="auc">
<p><span class="tag">{r['region']}</span><span class="tag">{r['type']}</span>
<span class="tag">유찰 {r['fail_rounds']}회</span><span class="tag">사건 {r['id']}</span></p>

<div class="lead">{intro}</div>

<div class="kpis">{kpis}</div>

<h2>1. 물건 개요</h2>
<table>
<tr><th>소재지</th><td>{r.get('address') or r['region']}</td></tr>
<tr><th>물건 종류</th><td>{r['type']} {('· 전용 %.1f㎡ %s' % (area, pyeong)) if area else ''}</td></tr>
<tr><th>감정가</th><td class="num">{won(r['appraisal'])}</td></tr>
<tr><th>최저입찰가</th><td class="num">{won(r['min_bid'])}</td></tr>
<tr><th>유찰 횟수</th><td class="num">{r['fail_rounds']}회</td></tr>
<tr><th>예상 시세(매도가)</th><td class="num">{won(r['market_price'])}</td></tr>
</table>

<h2>2. 얼마에 써야 할까 — 적정 입찰가</h2>
<p>수익과 낙찰 가능성을 함께 고려하면, 입찰가는 하나가 아니라 <b>전략에 따라 세 가지</b>로 볼 수 있습니다.</p>
<table>
<tr><th>보수 · 안전마진</th><td class="num">{won(st.get('safe_max'))}</td></tr>
<tr><th>권장 · 기대가치 최적</th><td class="num gold"><b>{won(r['recommended_bid'])}</b></td></tr>
<tr><th>공격 · 성공 확보</th><td class="num">{won(st.get('win_target'))}</td></tr>
</table>
<p>‘권장가’는 낙찰 성공률과 예상 순이익을 곱한 <b>기대가치가 가장 큰 지점</b>입니다.
더 낮게 쓰면 수익은 커지지만 낙찰 확률이 떨어지고, 더 높게 쓰면 반대가 됩니다.</p>

<h2>3. 낙찰 성공률과 경쟁</h2>
<p>인근 지역·유형의 과거 낙찰가율과 예상 응찰자 수를 반영하면, 권장가 기준 낙찰 성공률은
<b class="blue">{pct(r['success_prob'])}</b>, 예상 응찰자는 <b>약 {r.get('expected_bidders','—')}명</b>으로 추정됩니다.
경쟁이 치열할수록 낙찰가가 올라가 수익이 얇아지니, 상한선을 미리 정해두는 것이 중요합니다.</p>

<h2>4. 낙찰받으면 얼마 남을까 — 예상 수익</h2>
<table>
<tr><th>예상 매도가(시세)</th><td class="num">{won(r['market_price'])}</td></tr>
<tr><th>− 낙찰가(권장)</th><td class="num">{won(r['recommended_bid'])}</td></tr>
{cost_table(r.get('cost_items') or {})}
<tr><th><b>= 예상 순이익</b></th><td class="num {'jade' if r['net_profit']>=0 else 'rust'}"><b>{won(r['net_profit'])}</b></td></tr>
</table>
<p>취득세·명도·수리·보유·중개·양도세까지 반영한 <b>실현 기준</b> 순이익입니다.
투자원금 대비 수익률(ROI)은 <b>{r['roi']*100:.1f}%</b> 수준입니다.</p>

<h2>5. 권리·명도 리스크</h2>
{rights_html}

<h2>6. 결론 — 살까, 말까</h2>
<p><span class="verdict {vcls}">{vt}</span></p>
<p>{vexpl} 환금성은 <b>{lq.get('grade','—')}등급</b>으로, {'실거래가 활발해 되팔기 수월한 편' if lq.get('score',0)>=55 else '거래가 많지 않아 매도 기간을 넉넉히 잡아야 하는 편'}입니다.</p>

<div class="disc">※ 본 글의 적정 입찰가·성공률·수익·권리 분석은 공개 데이터 기반 <b>자동 추정</b>이며 투자 자문이 아닙니다.
세율·부대비용은 근사값이고, 실제 입찰 전 매각물건명세서·현황조사서·감정평가서를 직접 확인하세요.
경매는 명도·권리·유동성 위험이 따릅니다.</div>
</div>"""
    return title, body


def generate_past_post(c, rank):
    """과거 실적(낙찰→재매도) 복기 콘텐츠(현재 물건이 부족할 때)."""
    profit = c.get("net_profit")
    roi = c.get("roi")
    title = (f"[경매 복기] {c['region']} {c['type']} — 낙찰가 {won(c['won_bid'])} → "
             f"매도 {won(c['resale_price'])}, 수익률 {roi*100:.1f}%")
    kpis = "".join([
        kpi("낙찰가", won(c["won_bid"]), "gold"),
        kpi("최종 매도가", won(c["resale_price"]), "blue"),
        kpi("순이익", won(profit), "jade" if profit >= 0 else "rust"),
        kpi("수익률", f"{roi*100:.1f}%", "jade" if roi >= 0 else "rust"),
    ])
    body = f"""{STYLE}
<div class="auc">
<p><span class="tag">{c['region']}</span><span class="tag">{c['type']}</span><span class="tag">복기</span></p>
<div class="lead">이번엔 실제로 종결된 경매를 복기합니다. <b>{c['region']} {c['type']}</b>는 감정가 {won(c.get('appraisal'))}에
낙찰가 {won(c['won_bid'])}(낙찰가율 {c.get('sale_ratio','—')}%)로 낙찰됐고, 이후 {won(c['resale_price'])}에 재매도되어
순이익 <b class="{'jade' if profit>=0 else 'rust'}">{won(profit)}</b>, 수익률 <b>{roi*100:.1f}%</b>를 남겼습니다.</div>
<div class="kpis">{kpis}</div>
<h2>무엇을 배울 수 있나</h2>
<p>낙찰가율 {c.get('sale_ratio','—')}%는 이 지역·유형의 경쟁 강도를 보여줍니다. 응찰자는 약 {c.get('bidders','—')}명이었고,
재매도까지의 기간과 부대비용이 최종 수익률을 갈랐습니다. 지금 진행 중인 비슷한 물건에 이 기준을 대입해보면
적정 입찰가와 기대 수익의 감을 잡을 수 있습니다.</p>
<div class="disc">※ 과거 실적 복기이며 투자 자문이 아닙니다. 시장 상황에 따라 결과는 달라질 수 있습니다.</div>
</div>"""
    return title, body


# ----------------------- 선정 & 발행 -----------------------
def select(analysis, history, published, count):
    picks = []
    for r in analysis.get("properties", []):        # 이미 점수순 정렬
        if r["id"] in published:
            continue
        picks.append(("current", r))
        if len(picks) >= count:
            return picks
    # 부족분은 과거 사건으로
    cases = sorted([c for c in (analysis.get("backtest", {}).get("cases") or [])
                    if c.get("resale_price")], key=lambda c: c.get("roi", 0), reverse=True)
    for c in cases:
        pid = "past_" + c["id"]
        if pid in published:
            continue
        picks.append(("past", c))
        if len(picks) >= count:
            break
    return picks


def main():
    analysis = load("analysis.json")
    if not analysis:
        print("analysis.json 없음 — analyze.py 먼저 실행", file=sys.stderr)
        return
    history = load("auction-history.json", [])
    published = load("published.json", {}) or {}

    picks = select(analysis, history, published, DAILY_COUNT)
    if not picks:
        print("발행할 새 콘텐츠 없음(모두 발행됨)")
        return

    day = today()
    outdir = os.path.join(CONTENT, day)
    os.makedirs(outdir, exist_ok=True)
    index = []
    for rank, (kind, item) in enumerate(picks, 1):
        if kind == "current":
            title, html = generate_post(item, rank)
            pid = item["id"]
            score = item.get("score")
        else:
            title, html = generate_past_post(item, rank)
            pid = "past_" + item["id"]
            score = None
        fname = f"{rank:02d}-{slugify(item.get('apt_name') or item.get('id'))}.html"
        with open(os.path.join(outdir, fname), "w", encoding="utf-8") as f:
            f.write(html)
        published[pid] = {"date": day, "title": title, "kind": kind}
        index.append({"rank": rank, "kind": kind, "id": pid, "title": title,
                      "file": fname, "score": score})

    # 날짜별 인덱스(발행 목록)
    with open(os.path.join(outdir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"date": day, "count": len(index), "posts": index}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA, "published.json"), "w", encoding="utf-8") as f:
        json.dump(published, f, ensure_ascii=False, indent=2)

    cur = sum(1 for k, _ in picks if k == "current")
    print(f"[content] {day}: {len(index)}편 생성 (현재 {cur} / 과거 {len(index)-cur}) → content/{day}/")
    for x in index:
        print(f"  {x['rank']:>2}. [{x['kind']}] {x['title'][:60]}")


if __name__ == "__main__":
    main()
