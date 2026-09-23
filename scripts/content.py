#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
경매 블로그 콘텐츠 생성기 (네이버 블로그용 · 사람 말투).

- 종합점수 top을 뽑아 이미 발행한 사건(data/published.json)은 제외, 다음 순위로 채움.
- 현재 물건이 부족하면 과거 사건(실적)으로 채움.
- 물건 1건당 1편. 사건 id 해시로 문구·구성을 물건마다 다르게 골라 정형화(AI 티)를 줄인다.
- 프리미엄: 적정 입찰가·예상 순이익 등 핵심 금액은 HTML에 넣지 않아 개발자도구로도 안 보임.
  감정가·최저가·시세만 노출하고, 나머지는 댓글 문의로 유도.
표준 라이브러리만 사용.
"""
import os, sys, json, re, hashlib
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONTENT = os.path.join(ROOT, "content")
KST = timezone(timedelta(hours=9))
DAILY_COUNT = 10


def load(n, default=None):
    p = os.path.join(DATA, n)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else default


def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def won(n):
    if n is None:
        return "—"
    neg = n < 0
    n = abs(int(round(n)))
    eok, man = n // 10**8, round((n % 10**8) / 10**4)
    s = f"{eok}억 {man:,}만" if eok else f"{man:,}만"
    return ("−" if neg else "") + s


def slugify(s):
    return re.sub(r"[^0-9A-Za-z가-힣]+", "-", str(s)).strip("-")[:60]


def dong(a):
    m = re.search(r"([가-힣]+(?:동|읍|면))", a or "")
    return m.group(1) if m else ""


def pname(r):
    return (r.get("apt_name") or "").strip() or dong(r.get("address")) or r.get("type") or "물건"


def verdict(r):
    hi = (r.get("rights_risk") or {}).get("level") == "높음"
    if r["net_profit"] > 0 and r["success_prob"] >= 0.35 and r["roi"] >= 0.10 and not hi:
        return "v-go"
    if r["net_profit"] > 0 and r["roi"] >= 0.05 and not hi:
        return "v-hold"
    return "v-skip"


def _seed(s):
    return int(hashlib.md5(str(s).encode()).hexdigest(), 16)


def _pick(pool, seed, salt=0):
    return pool[(seed + salt) % len(pool)]


# --------- 문구 풀(물건마다 다르게 선택되어 반복감을 줄임) ---------
OPENERS = [
    "요즘 {reg} 쪽 경매 물건 계속 들여다보고 있는데, 이건 좀 눈에 띄더라고요.",
    "손품 팔다가 걸린 물건 하나 공유해봅니다. {reg} {typ}이에요.",
    "오늘은 {reg}에 나온 {typ} 하나 같이 보실까요.",
    "이 물건, 처음 보고 잠깐 멈칫했습니다. {reg} {typ}거든요.",
    "주말에 경매 검색하다 담아둔 물건입니다. {reg} {typ}.",
]
PRICE = [
    "감정가는 {app}원인데 {fr}번 유찰되면서 최저가가 {min}원까지 빠졌어요.",
    "{fr}번 유찰됐고 지금 최저가가 {min}원입니다. 감정가({app}원) 대비 꽤 내려왔죠.",
    "감정가 {app}원짜리가 유찰 {fr}번 거치면서 {min}원부터 시작합니다.",
]
CLOSE = [
    "저라면 {v} 물건이에요.",
    "개인적으론 {v} 쪽으로 봅니다.",
    "결론만 말하면 {v} 물건이라고 보고 있어요.",
    "제 기준에선 {v} 물건입니다.",
]
CTA = [
    "얼마 써야 낙찰되고 되팔면 얼마 남는지는 따로 정리해뒀는데, 공개하긴 좀 그래서요. 궁금하신 분은 댓글 남겨주시면 하나씩 알려드릴게요.",
    "적정 입찰가랑 예상 수익 숫자는 아껴뒀습니다. 필요하신 분 댓글 주시면 확인하고 답변드릴게요.",
    "구체적인 입찰가·수익 계산은 블라인드 처리했어요. 궁금하시면 댓글 부탁드립니다. 순서대로 답 드리겠습니다.",
]


def cta_box(line):
    return (f'<div class="cta"><b>💬 적정 입찰가·예상 수익 문의</b><br>{line}'
            f'<div class="btn">👇 댓글로 문의하기</div></div>')


def make_title(r, nm):
    reg, typ, s = r["region"], r["type"], _seed(r["id"])
    disc = r.get("discount_vs_market") or 0
    rk = (r.get("rights_risk") or {}).get("level")
    fr = r.get("fail_rounds", 0)
    if rk == "높음":
        return _pick([f"{reg} {nm}, 이거 잘못 들어가면 보증금 물립니다",
                      f"[주의] {reg} {nm} 경매 – 권리 안 보고 입찰하면 큰일나요"], s)
    if r["score"] >= 55 and disc > 0.15:
        return _pick([f"시세보다 확 싼 {reg} {nm}, 이거 물건 괜찮네요",
                      f"{reg} {nm} 경매… 감정가 {won(r['appraisal'])}에 이 가격이면 봐야죠"], s)
    if fr >= 2:
        return _pick([f"{fr}번 유찰된 {reg} {typ}, 슬슬 줍줍각인가요",
                      f"{reg} {typ} {fr}회 유찰… 이쯤이면 들어가도 될까요"], s)
    if fr >= 1:
        return _pick([f"{reg} {nm}, 최저가 {won(r['min_bid'])}원까지 내려왔습니다",
                      f"유찰 {fr}회 {reg} {nm} 경매 한번 뜯어봤어요"], s)
    return _pick([f"오늘 본 {reg} {typ} 경매 하나 – {nm}",
                  f"{reg} {nm}, 경매로 나왔길래 분석해봤습니다"], s)


def make_past_title(c):
    r = c["roi"] * 100
    return _pick([f"{c['region']} {c['type']} 경매, 실제로 수익률 {r:.0f}% 나온 사례 복기",
                  f"경매로 {c['region']} {c['type']} 낙찰받고 되팔면? 실제 결과 복기"], _seed(c["id"]))


STYLE = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
.auc{max-width:760px;margin:0 auto;font-family:'Pretendard',system-ui,'Malgun Gothic','Apple SD Gothic Neo',sans-serif;font-size:16px;line-height:1.85;color:#26292f}
.auc p{margin:1.05em 0}
.auc table{width:100%;border-collapse:collapse;margin:1.1em 0;font-size:.96em}
.auc th,.auc td{border-bottom:1px solid #ececf0;padding:9px 4px;text-align:left;vertical-align:top}
.auc th{color:#8b909b;font-weight:600;width:34%;white-space:nowrap}
.auc b{color:#12151b}
.auc .cta{margin:1.5em 0;background:#f3faf5;border:1px solid #cdead9;border-radius:14px;padding:18px 20px;line-height:1.7}
.auc .cta b{color:#127a4a}
.auc .cta .btn{display:inline-block;margin-top:10px;background:#1f9d63;color:#fff;font-weight:700;border-radius:999px;padding:9px 18px}
.auc .tags{margin-top:1.8em;color:#3d7bd6;font-size:.9em;line-height:1.9;word-break:keep-all}
.auc .disc{font-size:.85em;color:#a2a7b0;margin-top:1.6em}
</style>
"""


def tag_line(reg, typ, nm):
    sido = reg.split()[0]
    ts = ["부동산경매", "법원경매", "경매투자", "경매물건", "부동산투자", "재테크",
          sido + "경매", typ + "경매", nm.replace(" ", ""), reg.replace(" ", ""),
          "경매공부", "낙찰가", "경매수익", "부동산재테크"]
    out = []
    for t in ts:
        if t and t not in out:
            out.append(t)
    return " ".join("#" + t for t in out)


def generate_post(r, rank):
    nm = pname(r)
    s = _seed(r["id"])
    vc = verdict(r)
    reg, typ = r["region"], r["type"]
    vword = {"v-go": "적극적으로 노려볼 만한", "v-hold": "조건 맞으면 도전해볼",
             "v-skip": "좀 신중하게 봐야 할"}[vc]
    op = _pick(OPENERS, s).format(reg=reg, typ=typ)
    cta = _pick(CTA, s, 3)
    area = r.get("exclusive_area")
    py = f" (약 {area/3.3058:.0f}평)" if area else ""
    fl, ori = r.get("floor"), r.get("orientation")
    disc = r.get("discount_vs_market") or 0
    ratio_mean = (r.get("ratio_dist") or {}).get("mean") or 0
    next_min = int(r["min_bid"] * 0.8)          # 다음 유찰 시 통상 20% 저감
    evict = {"easy": "수월한 편", "normal": "보통 수준", "hard": "까다로울 수 있는"}.get(r.get("eviction"), "보통 수준")
    lqg = (r.get("liquidity") or {}).get("grade", "—")
    lqs = (r.get("liquidity") or {}).get("score", 0)

    # 시세·저평가
    if disc > 0:
        price_para = (f"제일 먼저 보는 게 시세죠. 이 근처 실거래 흐름 보면 대략 <b>{won(r['market_price'])}원</b> 선이에요. "
                      f"지금 최저가가 {won(r['min_bid'])}원이니까, 잘 받으면 시세보다 <b>{disc*100:.0f}%쯤 아래</b>에서 잡을 수 있다는 계산이 나옵니다. "
                      f"경매의 맛이 여기 있죠. 물론 이 차이가 곧 수익은 아니고, 세금·비용 빼고 나면 얘기가 달라지는데 그건 뒤에서.")
    else:
        price_para = (f"시세부터 보면 이 근처는 {won(r['market_price'])}원 안팎이에요. 감정가({won(r['appraisal'])}원)랑 비교하면 "
                      f"가격 메리트가 아주 크진 않아서, 최저가가 더 빠지는지 지켜볼 필요가 있는 물건입니다. 무리해서 들어갈 자리는 아니에요.")

    # 유찰 흐름
    if r["fail_rounds"] >= 1:
        fail_para = (f"유찰이 <b>{r['fail_rounds']}번</b> 났어요. 그래서 최저가가 감정가에서 {won(r['min_bid'])}원까지 내려온 겁니다. "
                     f"만약 이번에도 유찰되면 다음 회차는 대략 {won(next_min)}원 근처에서 다시 시작할 텐데, "
                     f"그때까지 기다렸다 더 싸게 노릴지, 이번에 확실히 잡을지가 늘 고민 포인트예요. "
                     f"보통 좋은 물건일수록 다음 회차까지 안 기다려지더라고요.")
    else:
        fail_para = ("아직 유찰 없이 첫 회차라 최저가가 감정가와 같아요. 신건은 경쟁 붙으면 값이 금방 올라가서, "
                     "감정가 이하로 잡으려면 응찰 타이밍과 상한선을 미리 정해두는 게 중요합니다.")

    # 경쟁·성공률
    win_para = (f"그럼 경쟁은요. 이 지역·유형은 과거 통계상 보통 <b>감정가의 {ratio_mean:.0f}% 안팎</b>에 낙찰되는 편이고, "
                f"이 물건엔 <b>약 {r.get('expected_bidders','?')}명</b> 정도 응찰할 걸로 봅니다. "
                f"제가 잡은 권장가로 넣었을 때 낙찰 확률은 <b>{r['success_prob']*100:.0f}%</b> 수준으로 계산돼요. "
                f"확률을 더 높이려면 값을 올려 써야 하고, 그럼 남는 게 줄죠. 이 줄타기를 어디서 끊느냐가 사실상 전부입니다.")

    # 권리
    rk = r.get("rights_risk") or {}
    flags = rk.get("flags") or []
    if rk.get("level") == "높음":
        rights_para = (f"<b>여기서 제일 조심할 건 권리예요.</b> {' · '.join(flags)} — 위험도 '높음'으로 봤습니다. "
                       f"대항력 있는 임차인 보증금은 배당 못 받으면 낙찰자가 인수하는데, 이 금액이 그대로 취득원가에 얹혀요. "
                       f"싸게 받았다고 좋아했다가 인수 보증금 때문에 오히려 손해 보는 게 딱 이런 케이스입니다. "
                       f"매각물건명세서·현황조사서에서 임차인 전입일·배당요구 여부 꼭 직접 확인하세요.")
    elif rk.get("level") == "보통" or flags:
        rights_para = (f"권리는 보통 수준인데 {(' · '.join(flags)) if flags else '임차인 관계'} 부분은 확인이 필요해요. "
                       f"배당요구를 했는지에 따라 인수 금액이 달라지니, 명세서에서 이 부분만큼은 꼭 짚고 넘어가세요.")
    else:
        rights_para = ("권리는 비교적 깔끔한 편이에요. 말소기준권리 이후 권리가 소멸되는 일반적인 구조라 인수 위험은 낮게 봤습니다. "
                       "그래도 명세서 확인은 습관처럼 하시고요.")

    # 비용·세금 관점(항목 노출, 금액 블라인드)
    cats = [k for k in ("취득세", "명도비", "수리비", "보유비용", "매도중개", "양도세", "인수권리")
            if (r.get("cost_items") or {}).get(k)]
    cost_para = (f"수익 계산할 때 저는 {', '.join(cats)}까지 다 뺍니다. 특히 취득 후 2년 안에 팔면 <b>양도세</b>가 꽤 크게 물고, "
                 f"명도는 이 물건 기준 {evict}으로 봤어요. 이런 걸 다 반영해야 '실제로 손에 쥐는' 순이익이 나오는데, "
                 f"감정가만 보고 싸다고 들어갔다가 세금·비용에서 다 까먹는 경우가 정말 많습니다.")

    liq_note = ("환금성 등급도 {g}라 되팔 때 크게 안 밀릴 물건이고요.".format(g=lqg) if lqs >= 55
                else "다만 환금성 등급이 {g}라 매도 기간은 좀 넉넉히 잡는 게 안전해요.".format(g=lqg))

    body = STYLE + f"""
<div class="auc">
<p>{op}</p>
<table>
<tr><th>소재지</th><td>{r.get('address') or reg}</td></tr>
<tr><th>종류·면적</th><td>{typ}{(' · 전용 %.1f㎡%s' % (area, py)) if area else ''}{(' · %d층' % fl) if fl else ''}{(' · ' + ori) if ori else ''}</td></tr>
<tr><th>감정가</th><td>{won(r['appraisal'])}원</td></tr>
<tr><th>최저입찰가</th><td>{won(r['min_bid'])}원 · 유찰 {r['fail_rounds']}회</td></tr>
<tr><th>예상 시세</th><td>{won(r['market_price'])}원</td></tr>
<tr><th>예상 낙찰가율</th><td>감정가의 {ratio_mean:.0f}% 안팎</td></tr>
</table>
<p>{price_para}</p>
<p>{fail_para}</p>
<p>{win_para}</p>
<p>{rights_para}</p>
<p>{cost_para}</p>
<p><b>그래서 결론적으로, 얼마에 써야 하고 되팔면 얼마 남느냐.</b> 저는 보수·권장·공격 세 가지 입찰가랑
예상 순이익·수익률(ROI)까지 다 뽑아놨는데요 — {cta}</p>
{cta_box(cta)}
<p>정리하면 {vword} 물건이에요. {liq_note} 판단은 각자 몫이지만 저는 이렇게 봤습니다.
비슷하게 보고 계신 분, 혹은 이 물건 임장 다녀오신 분 있으면 댓글로 정보 나눠요.</p>
<div class="tags">{tag_line(reg, typ, nm)}</div>
<div class="disc">개인 공부 기록 겸 참고용이에요. 투자 권유 아니고 숫자는 근사치라, 실제 입찰 전엔 서류 꼭 직접 확인하세요.</div>
</div>"""
    return make_title(r, nm), body


def generate_past_post(c, rank):
    nm = c["region"] + " " + c["type"]
    s = _seed(c["id"])
    cta = _pick(CTA, s, 3)
    body = STYLE + f"""
<div class="auc">
<p>이번엔 이미 끝난 경매 하나 복기해볼게요. {c['region']} {c['type']}인데, 감정가 {won(c.get('appraisal'))}원짜리가
낙찰가율 {c.get('sale_ratio','—')}%에 낙찰됐고, 나중에 재매도해서 <b>수익률 {c['roi']*100:.1f}%</b> 나왔던 건이에요.</p>
<p>낙찰가율 {c.get('sale_ratio','—')}% 보면 그 동네 경쟁이 대충 감이 오죠. 응찰자도 {c.get('bidders','—')}명쯤 붙었고요.
실제 낙찰가랑 매도가, 순이익 액수는 이번엔 좀 아껴둘게요.</p>
{cta_box(cta)}
<p>지금 진행 중인 비슷한 물건에 이 기준 대입해보면 감 잡기 좋습니다.</p>
<div class="tags">{tag_line(c['region'], c['type'], nm)}</div>
<div class="disc">지난 사례 복기예요. 시장 상황 따라 결과는 달라질 수 있습니다.</div>
</div>"""
    return make_past_title(c), body


def select(analysis, published, count):
    picks = []
    for r in analysis.get("properties", []):
        if r["id"] in published:
            continue
        picks.append(("current", r))
        if len(picks) >= count:
            return picks
    cases = sorted([c for c in (analysis.get("backtest", {}).get("cases") or [])
                    if c.get("resale_price")], key=lambda c: c.get("roi", 0), reverse=True)
    for c in cases:
        if ("past_" + c["id"]) in published:
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
    published = load("published.json", {}) or {}
    picks = select(analysis, published, DAILY_COUNT)
    if not picks:
        print("발행할 새 콘텐츠 없음(모두 발행됨)")
        return

    day = today()
    outdir = os.path.join(CONTENT, day)
    os.makedirs(outdir, exist_ok=True)
    index = []
    for rank, (kind, item) in enumerate(picks, 1):
        title, html = (generate_post(item, rank) if kind == "current"
                       else generate_past_post(item, rank))
        pid = item["id"] if kind == "current" else "past_" + item["id"]
        base = pname(item) if kind == "current" else item["id"]
        fname = f"{rank:02d}-{slugify(base)}.html"
        doc = (f'<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
               f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
               f'<title>{title}</title>\n</head>\n<body>\n{html}\n</body>\n</html>\n')
        open(os.path.join(outdir, fname), "w", encoding="utf-8").write(doc)
        published[pid] = {"date": day, "title": title, "kind": kind}
        index.append({"rank": rank, "kind": kind, "id": pid, "title": title, "file": fname})

    json.dump({"date": day, "count": len(index), "posts": index},
              open(os.path.join(outdir, "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(published, open(os.path.join(DATA, "published.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    cur = sum(1 for k, _ in picks if k == "current")
    print(f"[content] {day}: {len(index)}편 (현재 {cur}/과거 {len(index)-cur}) → content/{day}/")
    for x in index:
        print(f"  {x['rank']:>2}. {x['title'][:50]}")


if __name__ == "__main__":
    main()
