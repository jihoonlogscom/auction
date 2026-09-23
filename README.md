# 경매 분석 · 자동 수집형

부동산 경매 물건을 **매일 자동으로 수집·분석**해 ① 적정 입찰가 ② 낙찰 성공률(경쟁도 반영) ③ 낙찰 후 재매매 순이익을 계산하고, 과거 실현 사례로 통계를 학습·백테스트하는 정적 웹사이트 + GitHub Actions 파이프라인. **사용자 데이터 입력 불필요.**

## 파이프라인 (완전 자동)

```
[매일 06:00 KST]
  scripts/crawl.py     법원경매정보(courtauction.go.kr) 내부 JSON API 수집
      │                  ├ 물건 목록: 감정가·최저가·유찰·소재지·용도·기일
      │                  └ 현황조사서: 임대차현황 → 대항력·인수보증금 자동 추출
      ▼
  data/properties.json
      │
  scripts/analyze.py   국토부 실거래가 API로 시세 조회(층·향 보정)
      │                  → 적정입찰가·성공률·정밀세금·순이익
      ▼
  data/analysis.json ──▶ index.html  (Cloudflare 자동 재배포)
```

## 데이터 출처와 정직한 한계

| 출처 | 용도 | 성격 |
|---|---|---|
| **법원경매정보** courtauction.go.kr | 물건 목록·권리(현황조사서) | 사이트가 내부적으로 쓰는 **비공식 JSON API**. 화면 버튼이 호출하는 것과 동일. 공식 문서 API 아님 → **사이트 개편 시 재보정 필요** |
| **국토부 실거래가 OpenAPI** data.go.kr | 시세(아파트·오피스텔·연립다세대) | 공식·안정. 서비스키 필요 |

- 법원경매 크롤러가 깨질 경우를 대비해 워크플로우는 **샘플 폴백**으로 파이프라인을 멈추지 않습니다.
- 사이트가 개편되면 `scripts/crawl.py`의 `build_search_body()`(요청 본문)와 `parse_rows()`(응답 키) **두 함수만** 실제 응답에 맞춰 보정하면 됩니다. 세션·권리추출·저장 로직은 그대로 동작합니다.
- 과도한 트래픽을 피하려 `polite_delay`를 둡니다. 개인 리서치 용도로 사용하세요.

### 안정 대안 — 온비드 공매 API
법원경매(court auction)가 아니라 **공매(公賣)** 로도 괜찮다면, 한국자산관리공사 **온비드 물건목록 조회 OpenAPI**(data.go.kr, 공식·서비스키)가 문서화된 안정적 대안입니다. 크롤러 대신 이 API로 `properties.json`을 채우도록 바꿀 수 있습니다.

## 설정

### 1. 국토부 실거래가 서비스키
1. [공공데이터포털](https://www.data.go.kr) → "국토교통부_아파트 매매 실거래가 상세 자료"(+오피스텔·연립다세대) 활용신청
2. 발급 인증키(Decoding)를 GitHub → Settings → Secrets → Actions → `MOLIT_SERVICE_KEY` 로 등록
> 키가 없어도 동작(시세는 감정가/override 폴백).

### 2. 수집 범위 조정 — `data/crawl-config.json`
```json
{ "sido": ["경기도","서울특별시","인천광역시"], "usage": ["아파트","오피스텔","연립다세대"],
  "min_appraisal": 300000000, "sale_date_to_days": 21, "max_properties": 60, "fetch_rights": true }
```

### 3. 로컬 실행
```bash
python3 scripts/crawl.py            # 라이브 수집 (network 필요)
python3 scripts/crawl.py --sample   # 오프라인: 샘플로 파이프라인 검증
python3 scripts/analyze.py          # data/analysis.json 생성
python3 -m http.server 8000         # http://localhost:8000
```
표준 라이브러리만 사용 — 설치 불필요.

### 4. Cloudflare 무료 배포
Cloudflare Pages/Workers에 이 저장소 연결 → 빌드 명령 없음, 출력 `/`. `main` 푸시(=봇 커밋)마다 자동 재배포.

## 분석 로직
- **적정입찰가**: `예상매도가 − 부대비용 − 목표이익` 만족 최대 입찰가(이진탐색).
- **성공률**: 지역·유형별 낙찰가율 분포(과거사례 blend·유찰 페널티) + **경쟁도(예상 입찰자 수)** 로 기대 낙찰가율 보정 → `P(실제 낙찰가율 ≤ 내 입찰가율)`.
- **순이익**: `매도가 − 낙찰가 − 인수권리(보증금·유치권) − 부대비용`. 취득세(구간·농특·교육세·중과), 양도세(단기중과·누진·장특공제·지방소득세) 정밀 반영. 과거 실현 사례로 백테스트.
- **권리 위험도**: 대항력 임차인·인수보증금·특수권리 → 낮음/보통/높음, 판정에 반영.



## 라이브 수집이 안 될 때 (해외 IP 지오블록)

GitHub Actions 러너는 미국·유럽 IP인데, **법원경매정보(courtauction.go.kr)는 해외 데이터센터 IP를 차단/무응답 처리**하는 경우가 많습니다. 로그에 `TimeoutError: timed out`이 `sock.connect` 단계에서 나면 이 경우입니다(HTTP 오류가 아니라 연결 자체가 안 됨 = 네트워크 차단 신호). 이때 크롤러는 트레이스백 없이 **기존 데이터를 보존**하고(최초엔 샘플로) 정상 종료하며, 분석·추적 단계는 계속됩니다.

**먼저 확인**: 본인 PC(한국)에서 `python scripts/crawl.py`를 돌려보세요. 여기서 되면 → 지오블록 확정. 안 되면 → 엔드포인트 재보정 필요(아래 '개편 시 보정').

**해결책 (권장 순)**
1. **한국 IP의 self-hosted 러너** — 집 PC나 국내 VPS(카페24·가비아·NHN클라우드 등)에 GitHub Actions self-hosted runner를 설치하고, `crawl` job만 `runs-on: self-hosted`로 지정. 분석·추적(MOLIT는 해외에서도 접속 가능)은 그대로 GitHub 호스티드 러너에서 돌려도 됩니다.
2. **국내 서버 cron + push** — 국내 서버에서 `crawl.py`를 cron으로 돌려 `properties.json`을 커밋/푸시. 나머지 파이프라인(analyze·track)은 push 트리거로 GitHub Actions에서 실행.
3. **한국 프록시** — 크롤 요청만 국내 프록시로 우회(요청에 proxy 핸들러 추가). 프록시 신뢰성·약관 확인 필요.
4. **온비드(공매) 공식 API로 전환** — 법원경매 대신 공매도 무방하면, 한국자산관리공사 온비드 OpenAPI(data.go.kr)는 해외에서도 접속되는 API 게이트웨이라 러너 위치와 무관하게 동작합니다.

`crawl-config.json`의 `connect_timeout`(기본 25초)·`retries`(기본 2회)를 조정할 수 있으나, 연결 자체가 막힌 경우엔 타임아웃을 늘려도 해결되지 않습니다(위 1~4로).

### 개편 시 보정
접속은 되는데 결과가 0건이면 사이트 구조가 바뀐 것입니다. `scripts/crawl.py`의 `build_search_body()`(요청 본문)와 `parse_rows()`(응답 키)만 실제 응답에 맞춰 수정하세요.

## 자기고도화 루프 (예측 → 실제 대조 → 학습파일)

매일 예측을 저장해 두고, 그 경매가 끝나 실제 결과가 나오면 예측과 대조해 **오차와 보정 제안**을 별도 파일로 쌓습니다. 이 파일을 나중에 업로드하면 그걸 근거로 모델(baseline·경쟁도·시세·비용)을 튜닝합니다.

`scripts/track.py` (매 실행 crawl→analyze 다음에 자동 실행):
1. **예측 스냅샷** `data/predictions.json` — 물건별 마지막 예측(권장가·예상 낙찰가율·성공률·응찰자·순이익·환금성·점수)을 보존. 물건이 목록에서 사라져도 남습니다.
2. **실제 결과** `data/outcomes.json` — 매각기일이 지난 물건의 실제 낙찰가·응찰자수·낙찰여부, 이후 재매도가. (법원경매정보 결과조회로 자동 채우거나, 직접 입력 가능)
3. **학습 파일** `data/calibration.json` — 예측↔실제를 조인해 오차 계산:
   - `ratio_err` 예측 낙찰가율 vs 실제, `bidders_err` 예상 응찰자 vs 실제
   - `win_brier` 성공률 예측 정확도, `would_win` 권장가로 낙찰됐을지
   - `resale_vs_market_pct` 예측 시세 vs 실제 재매도가
   - 지역·유형별 오차 + **suggested_adjustments**(사람이 읽는 보정 제안)

### 나중에 고도화 요청하는 법
`data/calibration.json`(원하면 `predictions.json`·`outcomes.json`도 함께)을 업로드하고 "이걸로 고도화해줘"라고 하면, 오차·바이어스를 근거로 baseline 낙찰가율·경쟁도(응찰자)·시세 보정·세율/비용 가정을 조정한 새 `assumptions.json`·`baselines.json`을 만들어 드립니다.

예시 신호:
```
낙찰가율 bias +4.2%p → baseline mean 상향
경기 수원시·아파트 +3.5%p → 해당 셀 보정
응찰자 bias +2.1명 → competition base bidders 상향
성공률 Brier 0.065 (양호)
```

## 주의
세율·부대비용·권리 판단은 근사 추정이며 실제 세무·법률 판단을 대체하지 않습니다. 명도·권리·유동성 위험이 있으며 이 도구는 투자 자문이 아닙니다.
