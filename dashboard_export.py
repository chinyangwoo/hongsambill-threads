# -*- coding: utf-8 -*-
"""
홍보 성과판 저장본 만들기
=========================
GitHub 에 쌓인 metrics/*.json 을 모아 성과판이 바로 읽을 수 있는 한 파일(dashboard_snapshot.json)로 만듭니다.
성과판은 실시간 연결(Supabase)이 안 될 때 이 저장본을 보여 줍니다.

사용법: python dashboard_export.py [출력파일]
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
RAW = "https://raw.githubusercontent.com/chinyangwoo/"
FILES = {
    "threads_hotel": "hongsambill-threads/main/metrics/threads_hotel.json",
    "fb_hotel": "hongsambill-threads/main/metrics/fb_hotel.json",
    "ig_hotel": "hongsambill-threads/main/metrics/ig_hotel.json",
    "ig_seongsu": "hongsambill-threads/main/metrics/ig_seongsu.json",
    "threads_sauna": "jinan-sauna-threads/main/metrics/threads_sauna.json",
}
AI = "hongsambill-threads/main/metrics/ai_citations.json"


def get(path):
    try:
        with urllib.request.urlopen(RAW + path, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"⚠️ {path}: {e}")
        return None


def main(out):
    now = datetime.now(KST)
    cut = (now - timedelta(days=62)).isoformat()
    posts, channels = [], []
    for key, path in FILES.items():
        d = get(path)
        if not d:
            continue
        channels.append({"channel": key, "label": d.get("label"), "business": d.get("business"),
                         "followers": d.get("followers"), "updated_at": d.get("updated")})
        for p in d.get("posts", []):
            if p.get("posted_at", "") < cut:
                continue
            posts.append({k: p.get(k) for k in ("channel", "post_id", "business", "posted_at", "permalink",
                                                 "views", "likes", "comments", "shares", "saves", "fetched_at")}
                         | {"topic": (p.get("topic") or "")[:80], "hook": (p.get("hook") or "")[:80]})
    posts.sort(key=lambda p: p["posted_at"], reverse=True)
    ai = []
    a = get(AI) or {}
    acut = (now - timedelta(days=31)).strftime("%Y-%m-%d")
    for c in a.get("checks", []):
        if c.get("date", "") < acut:
            continue
        ai.append({"day": c["date"], "question": c["question"], "business": c.get("business"),
                   "mentioned": c.get("mentioned", []), "rank": c.get("rank", 0), "cited": c.get("cited", False),
                   "found": c.get("found_in_search", False), "sources": c.get("sources", []),
                   "answer": (c.get("answer") or "")[:600]})
    ai.sort(key=lambda x: x["day"], reverse=True)
    snap = {"generated_at": now.isoformat(), "posts": posts, "channels": channels, "snaps": [], "ai": ai,
            "log": [{"at": now.isoformat(), "note": f"저장본 {len(posts)} posts, {len(ai)} ai"}]}
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"export": snap, "saved_at": now.isoformat()}, f, ensure_ascii=False)
    print(f"✅ {out}: 글 {len(posts)}개, 채널 {len(channels)}개, AI 점검 {len(ai)}건")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dashboard_snapshot.json")
