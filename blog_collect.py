# -*- coding: utf-8 -*-
"""
블로그 성과 수집기 (홍보 성과판용)
=================================
구글 블로그(Blogspot)와 네이버 블로그의 글 목록·댓글·공감·방문자 수를 모아
metrics/<채널>.json 에 저장합니다. 로그인이나 API 키가 필요 없는 공개 정보만 씁니다.

- 구글 블로그: 글, 댓글 수 (글별 조회수는 공개되지 않음)
- 네이버 블로그: 글, 댓글·공감·공유 수, 블로그 전체 누적 방문자 수, 이웃 수
  (네이버는 글별 조회수를 공개하지 않으므로 '방문자 수 증가분'을 조회수로 씁니다)

사용법: python blog_collect.py
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
OUT_DIR = "metrics"
KEEP_DAYS = 60
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}

BLOGSPOT = {
    "blog_hotel":  {"label": "홍삼빌호텔 구글 블로그", "business": "hsb", "host": "hongsamhotel.blogspot.com"},
    "blog_sauna":  {"label": "진안사우나 구글 블로그", "business": "spa", "host": "jinansauna.blogspot.com"},
    "blog_sungsu": {"label": "성수주조장 구글 블로그", "business": "ssb", "host": "sungsubrewery.blogspot.com"},
}
NAVER = {
    "naver_halo":   {"label": "홍삼빌호텔 네이버 블로그", "business": "hsb", "id": "halo_universe"},
    "naver_straw":  {"label": "딸기막걸리 네이버 블로그", "business": "ssb", "id": "born_to_drink"},
    "naver_sungsu": {"label": "성수주조장 네이버 블로그", "business": "ssb", "id": "sungsu-brewery"},
}


def get_json(url, referer=None):
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def now():
    return datetime.now(KST)


def save(key, cfg, posts, followers=None, extra=None, errors=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    cut = (now() - timedelta(days=KEEP_DAYS)).isoformat()
    posts = sorted([p for p in posts if p["posted_at"] >= cut], key=lambda p: p["posted_at"], reverse=True)
    out = {"channel": key, "label": cfg["label"], "business": cfg["business"], "updated": now().isoformat(),
           "followers": followers, "errors": errors or [], "posts": posts}
    out.update(extra or {})
    with open(os.path.join(OUT_DIR, key + ".json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"✅ {cfg['label']}: 글 {len(posts)}개, 댓글 {sum(p['comments'] or 0 for p in posts)}"
          + (f", 누적 방문자 {extra.get('visitors_total')}" if extra and extra.get('visitors_total') is not None else ""))


def post(key, cfg, pid, at, title, link, views=None, likes=0, comments=0, shares=0):
    return {"post_id": str(pid), "channel": key, "channel_label": cfg["label"], "business": cfg["business"],
            "posted_at": at, "topic": title[:120], "hook": title[:80], "fetched_at": now().isoformat(),
            "views": views, "reach": None, "likes": likes, "comments": comments, "shares": shares, "saves": None,
            "permalink": link}


def collect_blogspot(key, cfg):
    posts, errors = [], []
    try:
        start = 1
        while start < 600:
            d = get_json(f"https://{cfg['host']}/feeds/posts/default?alt=json&max-results=150&start-index={start}")
            entries = d["feed"].get("entry", [])
            for e in entries:
                pid = e["id"]["$t"].split("post-")[-1]
                at = datetime.fromisoformat(e["published"]["$t"]).astimezone(KST).isoformat()
                link = next((l["href"] for l in e.get("link", []) if l.get("rel") == "alternate"), None)
                posts.append(post(key, cfg, pid, at, e["title"]["$t"], link,
                                  comments=int(e.get("thr$total", {}).get("$t", 0))))
            if len(entries) < 150:
                break
            start += 150
    except Exception as e:
        errors.append(str(e)[:300])
    save(key, cfg, posts, errors=errors)


def collect_naver(key, cfg):
    bid = cfg["id"]
    ref = f"https://m.blog.naver.com/{bid}"
    posts, errors, followers, extra = [], [], None, {}
    try:
        info = get_json(f"https://m.blog.naver.com/api/blogs/{bid}", ref)["result"]
        followers = info.get("subscriberCount")
        extra = {"visitors_total": info.get("totalVisitorCount"), "visitors_today": info.get("dayVisitorCount")}
    except Exception as e:
        errors.append("blog " + str(e)[:200])
    cut = (now() - timedelta(days=KEEP_DAYS)).timestamp() * 1000
    try:
        for page in range(1, 8):
            r = get_json(f"https://m.blog.naver.com/api/blogs/{bid}/post-list?categoryNo=0&itemCount=30&page={page}", ref)["result"]
            items = r.get("items") or []
            for i in items:
                at = datetime.fromtimestamp(i["addDate"] / 1000, KST).isoformat()
                title = re.sub(r"<[^>]+>", "", i.get("titleWithInspectMessage") or "")
                posts.append(post(key, cfg, i["logNo"], at, title, f"https://blog.naver.com/{bid}/{i['logNo']}",
                                  likes=i.get("sympathyCnt") or 0, comments=i.get("commentCnt") or 0,
                                  shares=i.get("shareCnt") or 0))
            if len(items) < 30 or (items and items[-1]["addDate"] < cut):
                break
            time.sleep(1)
    except Exception as e:
        errors.append("posts " + str(e)[:200])
    save(key, cfg, posts, followers=followers, extra=extra, errors=errors)


if __name__ == "__main__":
    for k, c in BLOGSPOT.items():
        collect_blogspot(k, c)
    for k, c in NAVER.items():
        collect_naver(k, c)
