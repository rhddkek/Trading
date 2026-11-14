# 트레이딩 분석 (미국·한국 주식, 개인용)

GitHub Pages로 서비스되는 정적 사이트입니다. 장중 약 20분 간격으로 GitHub Actions가 시세를 수집해 `gh-pages` 브랜치에 배포합니다.

- `index.html` — 화면과 모든 계산(점수, 스크리너, 시장 국면, 시뮬레이션, 포지션 계산, 백테스트, 포트폴리오)
- `scripts/universe.json` — 분석 대상 종목·지수 (여기에 추가·삭제, 코스닥은 4번째 값에 `"KQ"`)
- `scripts/fetch.py` — Yahoo Finance에서 시세·재무·실적일·뉴스 수집
- `.github/workflows/update.yml` — 자동 갱신·배포

## 참고한 공개 스킬

기능 구성은 아래 공개 프로젝트의 아이디어를 참고해 직접 구현했습니다(코드를 가져오지 않음).

- TauricResearch/TradingAgents — 역할별 분석팀, 강세·약세 토론
- Jakeschincariol/day-trader-skill — 스캐너, 목표·손절, 반복 시뮬레이션, 예산·리스크 점검
- tradermonty/claude-trading-skills — 추세 템플릿·VCP·모멘텀 버스트·배당 스크리너, 시장 폭·분산일, 포지션 사이징, 백테스트

투자 판단을 돕는 개인용 참고 자료이며 매수·매도 권유가 아닙니다.
