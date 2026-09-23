# 라이브 수집 켜기 — 한국 IP self-hosted 러너

GitHub 호스티드 러너(미국·유럽 IP)는 법원경매정보(courtauction.go.kr)에 지오블록됩니다.
해결의 핵심은 **크롤을 한국 IP에서 돌리는 것**입니다. 순서대로 진행하세요.

## 0단계 (먼저!) — 엔드포인트 검증: 본인 PC에서 1회 실행
인프라를 들이기 전에, 크롤러의 요청/파싱이 실제 사이트와 맞는지부터 확인합니다.

```bash
git clone https://github.com/jihoonlogscom/auction && cd auction
python scripts/crawl.py            # 한국 PC(가정망/회사망)에서
cat data/properties.json | head    # 실제 물건이 들어왔는지 확인
```

- **실제 물건이 채워지면** → 엔드포인트 정상. 1단계(러너)로 진행.
- **403/빈 결과/파싱오류면** → 사이트 구조가 조금 다른 것. `python scripts/crawl.py` 실행 시
  출력되는 응답(또는 브라우저 개발자도구 Network 탭의 `searchControllerMain.on` 응답 JSON)을
  저에게 붙여주세요. `build_search_body()`·`parse_rows()`를 실물에 맞춰 확정하겠습니다.
- **연결 자체가 안 되면(회사망 차단 등)** → 가정망이나 국내 VPS에서 시도.

## 1단계 — self-hosted 러너 등록 (한국 머신)
대상 머신: 집 PC / 라즈베리파이 / **국내 VPS(카페24·가비아·NHN클라우드 등, 권장)**.
매일 06시 cron에 켜져 있어야 하므로, 항상 켜두는 VPS나 홈서버가 좋습니다.

1. GitHub 저장소 → **Settings → Actions → Runners → New self-hosted runner**
2. OS 선택 후 화면에 나오는 명령을 그 머신에서 실행(다운로드 → `./config.sh --url ... --token ...`)
3. 상시 실행(서비스 등록):
   - Linux: `sudo ./svc.sh install && sudo ./svc.sh start`
   - Windows: 러너를 서비스로 설치(설치 마법사 옵션)
4. 머신에 **Python 3.10+ 와 git** 설치 확인 (`python3 --version`, `git --version`)

> ⚠️ **보안**: self-hosted 러너는 공개 저장소에서 위험합니다(외부 PR이 러너에서 코드 실행).
> 이 저장소를 **비공개(Private)** 로 바꾸거나, 최소한 외부 PR 실행을 막으세요.
> (Settings → Actions → General → Fork pull request workflows 제한)

## 2단계 — 워크플로우를 self-hosted로
파이프라인 전체(크롤·분석·추적)를 한국 러너에서 돌리면 가장 단순합니다. MOLIT·GitHub도
한국에서 접속되므로 분리할 필요가 없습니다. 동봉한 `analyze-selfhosted.yml`을 쓰거나,
기존 워크플로우에서 한 줄만 바꾸세요:

```yaml
jobs:
  pipeline:
    runs-on: [self-hosted]      # ← ubuntu-latest 에서 변경
```

self-hosted 러너에는 setup-python이 불안정할 수 있어, 동봉 파일은 그 스텝을 빼고 시스템 python을 씁니다.

## 대안 A — 크롤만 self-hosted, 분석은 GitHub(하이브리드)
머신 부담을 줄이려면 크롤 job만 self-hosted로 두고, properties.json을 커밋/푸시하면
push 트리거로 analyze·track이 GitHub 호스티드에서 돕니다. (analyze-selfhosted.yml 하단 주석 참고)

## 대안 B — 온비드(공매) 공식 API
법원경매 대신 **공매**도 무방하면, 한국자산관리공사 온비드 OpenAPI(data.go.kr)는
해외에서도 접속되는 API 게이트웨이라 러너 없이 GitHub에서 바로 동작합니다. 이 경우
`crawl.py`를 온비드 물건목록 조회로 교체하면 됩니다(원하시면 만들어 드립니다).
