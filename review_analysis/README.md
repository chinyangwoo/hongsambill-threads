# 구글 리뷰 분석 · 팀장 고지판

- 매주 월요일 07:47 `review_archive.yml` 이 구글 리뷰 원문을 `reviews_archive.json` 에 저장합니다 (작성자 이름 제외).
- 같은 날 아침 Claude 예약 작업이 새 리뷰를 항목별로 분류하고, 팀별 고지(칭찬·제안·경고·제언)를 작성해
  **비공개** 고지판(claude.ai 아티팩트)을 갱신합니다.
- 이 저장소는 공개이므로 분석 결과·팀별 고지는 여기에 저장하지 않습니다.

`build.py ARCHIVE CLS TEAM_REPORT OUT_HTML` — 분류 캐시와 팀 고지 JSON으로 고지판 HTML 생성.
