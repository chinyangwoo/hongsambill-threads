# -*- coding: utf-8 -*-
"""
구글 리뷰 원문 보관 (리뷰 분석·팀장 고지 시스템의 '수집' 단계)
================================================================
- 구글 비즈니스 프로필 API로 홍삼빌호텔 리뷰 전체를 읽어 reviews_archive.json 에 저장한다.
- 인증·지점 조회·원문 추출은 fetch_google_reviews.py 를 그대로 재사용 (그 파일은 수정하지 않음).
- 이 저장소는 공개이므로 작성자 이름·프로필은 저장하지 않는다.
  저장 항목: 리뷰 ID, 별점, 원문, 작성일, 답글 여부 — 모두 구글 지도에 이미 공개된 내용.
- 분석(항목 분류·팀별 고지)은 여기서 하지 않는다. 비공개 대시보드에서 Claude가 처리.
"""
import json
from pathlib import Path

import fetch_google_reviews as F

OUT = Path(__file__).resolve().parent / "reviews_archive.json"


def main():
    raw = F.fetch_reviews()
    seen, rows = set(), []
    for r in raw:
        rv = F.normalize(r)
        if rv["id"] in seen:
            continue
        seen.add(rv["id"])
        rows.append({
            "id": rv["id"],
            "stars": rv["stars"],
            "text": (rv["text"] or "").strip(),
            "created": (rv["created_raw"] or "")[:10],
            "replied": "reviewReply" in r,
        })
    rows.sort(key=lambda x: x["created"], reverse=True)
    data = {
        "updated": str(F.now_kst()),
        "average_rating": F.PROFILE.get("GBP_AVG_RATING"),
        "total_review_count": F.PROFILE.get("GBP_REVIEW_COUNT"),
        "count": len(rows),
        "reviews": rows,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ 리뷰 {len(rows)}건 저장 → {OUT.name} (구글 표시 {data['total_review_count']}건 / 평균 {data['average_rating']})")


if __name__ == "__main__":
    main()
