# -*- coding: utf-8 -*-
"""
SNS 성과 수집기 (홍보 대시보드용)
=================================
각 자동 포스팅 봇의 게시 기록(post_id)을 읽어 조회·도달·좋아요·댓글·공유·저장 수치를
Threads / Instagram / Facebook API 에서 가져와 metrics/<채널>.json 에 저장합니다.
Supabase 가 이 파일을 주기적으로 읽어 대시보드에 보여 줍니다.

사용법:  python metrics_collect.py <채널키> [<채널키> ...]
채널키:  threads_hotel  fb_hotel  ig_hotel  ig_seongsu  threads_sauna
(각 채널은 해당 토큰 환경변수가 있어야 수집됩니다. 없으면 건너뜁니다.)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
KEEP_DAYS = 60          # 이 기간 안의 게시물만 유지
REFRESH_DAYS = 14       # 게시 후 이 기간 동안은 매번 다시 수집 (그 뒤는 마지막 값 유지)
OUT_DIR = "metrics"

CHANNELS = {
    "threads_hotel":  {"label": "홍삼빌호텔 스레드",   "business": "hsb", "kind": "threads", "log": "posted_log.json",
                       "token": "THREADS_ACCESS_TOKEN", "user": "THREADS_USER_ID"},
    "threads_sauna":  {"label": "진안사우나 스레드",   "business": "spa", "kind": "threads", "log": "posted_log.json",
                       "token": "THREADS_ACCESS_TOKEN", "user": "THREADS_USER_ID"},
    "fb_hotel":       {"label": "홍삼빌호텔 페이스북", "business": "hsb", "kind": "facebook", "log": "facebook_posted_log.json",
                       "token": "FB_PAGE_ACCESS_TOKEN", "user": "FB_PAGE_ID"},
    "ig_hotel":       {"label": "홍삼빌호텔 인스타",   "business": "hsb", "kind": "instagram", "log": "instagram_posted_log.json",
                       "token": "INSTAGRAM_ACCESS_TOKEN", "user": "INSTAGRAM_USER_ID"},
    "ig_seongsu":     {"label": "성수주조장 인스타",   "business": "ssb", "kind": "instagram", "log": "seongsu_instagram_posted_log.json",
                       "token": "SEONGSU_INSTAGRAM_ACCESS_TOKEN", "user": "SEONGSU_INSTAGRAM_USER_ID"},
}


def http_json(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: {body}") from None


def metric_values(res):
    out = {}
    for item in res.get("data", []):
        name = item.get("name")
        if "total_value" in item:
            out[name] = item["total_value"].get("value", 0)
        else:
            vals = item.get("values") or [{}]
            out[name] = vals[0].get("value", 0)
    return out


def try_metrics(base, pid, token, candidates):
    """지표 조합을 차례로 시도해 처음 성공한 값을 돌려준다 (API 버전마다 허용 지표가 달라서)."""
    for metrics in candidates:
        try:
            q = urllib.parse.urlencode({"metric": metrics, "access_token": token})
            return metric_values(http_json(f"{base}/{pid}/insights?{q}"))
        except Exception as e:
            last = e
    print(f"   ⚠️ insights 실패 {pid}: {last}")
    return {}


def as_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ── 채널별 수집 ─────────────────────────────────────────
def fetch_threads(pid, token):
    base = "https://graph.threads.net/v1.0"
    ins = try_metrics(base, pid, token, ["views,likes,replies,reposts,quotes,shares", "views,likes,replies,reposts,quotes", "views,likes,replies"])
    link = None
    try:
        link = http_json(f"{base}/{pid}?fields=permalink&access_token={urllib.parse.quote(token)}").get("permalink")
    except Exception:
        pass
    return {"views": as_int(ins.get("views")), "reach": None, "likes": as_int(ins.get("likes")),
            "comments": as_int(ins.get("replies")),
            "shares": as_int(ins.get("shares")) + as_int(ins.get("reposts")) + as_int(ins.get("quotes")),
            "saves": None, "permalink": link}


def fetch_instagram(pid, token):
    base = "https://graph.instagram.com/v21.0"
    media = {}
    try:
        media = http_json(f"{base}/{pid}?fields=like_count,comments_count,permalink&access_token={urllib.parse.quote(token)}")
    except Exception as e:
        print(f"   ⚠️ media 실패 {pid}: {e}")
    ins = try_metrics(base, pid, token, ["views,reach,saved,shares", "reach,saved,shares", "reach,saved"])
    return {"views": as_int(ins.get("views")) if "views" in ins else None, "reach": as_int(ins.get("reach")),
            "likes": as_int(media.get("like_count")), "comments": as_int(media.get("comments_count")),
            "shares": as_int(ins.get("shares")), "saves": as_int(ins.get("saved")), "permalink": media.get("permalink")}


def fetch_facebook(pid, token):
    base = "https://graph.facebook.com/v21.0"
    post = {}
    try:
        fields = "permalink_url,shares,reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)"
        post = http_json(f"{base}/{pid}?fields={urllib.parse.quote(fields)}&access_token={urllib.parse.quote(token)}")
    except Exception as e:
        print(f"   ⚠️ post 실패 {pid}: {e}")
    ins = try_metrics(base, pid, token, ["post_impressions_unique,post_impressions", "post_impressions_unique", "post_media_view"])
    views = ins.get("post_impressions", ins.get("post_media_view"))
    return {"views": as_int(views) if views is not None else None,
            "reach": as_int(ins.get("post_impressions_unique")) if "post_impressions_unique" in ins else None,
            "likes": as_int(((post.get("reactions") or {}).get("summary") or {}).get("total_count")),
            "comments": as_int(((post.get("comments") or {}).get("summary") or {}).get("total_count")),
            "shares": as_int((post.get("shares") or {}).get("count")), "saves": None,
            "permalink": post.get("permalink_url")}


def fetch_followers(kind, user, token):
    try:
        if kind == "threads":
            q = urllib.parse.urlencode({"metric": "followers_count", "access_token": token})
            return as_int(metric_values(http_json(f"https://graph.threads.net/v1.0/{user}/threads_insights?{q}")).get("followers_count"))
        if kind == "instagram":
            return as_int(http_json(f"https://graph.instagram.com/v21.0/me?fields=followers_count&access_token={urllib.parse.quote(token)}").get("followers_count"))
        if kind == "facebook":
            return as_int(http_json(f"https://graph.facebook.com/v21.0/{user}?fields=followers_count&access_token={urllib.parse.quote(token)}").get("followers_count"))
    except Exception as e:
        print(f"   ⚠️ 팔로워 수집 실패: {e}")
    return None


FETCH = {"threads": fetch_threads, "instagram": fetch_instagram, "facebook": fetch_facebook}


def collect(key):
    ch = CHANNELS[key]
    token = os.environ.get(ch["token"], "").strip()
    if not token:
        print(f"⏭️  {ch['label']}: 토큰({ch['token']}) 없음 — 건너뜀")
        return
    if not os.path.exists(ch["log"]):
        print(f"⏭️  {ch['label']}: 게시 기록 {ch['log']} 없음 — 건너뜀")
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{key}.json")
    old = {}
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            old = {p["post_id"]: p for p in json.load(f).get("posts", [])}
    with open(ch["log"], encoding="utf-8") as f:
        log = json.load(f)
    now = datetime.now(KST)
    posts = dict(old)
    n = 0
    for p in log.get("posts", []):
        pid = str(p.get("post_id") or "")
        if not pid:
            continue
        try:
            at = datetime.strptime(p["at"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except Exception:
            continue
        age = now - at
        if age.days > KEEP_DAYS:
            continue
        if pid in old and age.days > REFRESH_DAYS:
            continue
        m = FETCH[ch["kind"]](pid, token)
        text = (p.get("text") or "").strip()
        posts[pid] = {
            "post_id": pid, "channel": key, "channel_label": ch["label"], "business": ch["business"],
            "posted_at": at.isoformat(), "topic": p.get("topic") or "", "hook": text.split("\n")[0][:120],
            "fetched_at": now.isoformat(), **m,
        }
        if not posts[pid].get("permalink") and p.get("permalink"):
            posts[pid]["permalink"] = p["permalink"]
        n += 1
        time.sleep(0.5)
    cutoff = now - timedelta(days=KEEP_DAYS)
    keep = [v for v in posts.values() if datetime.fromisoformat(v["posted_at"]) >= cutoff]
    keep.sort(key=lambda v: v["posted_at"], reverse=True)
    followers = fetch_followers(ch["kind"], os.environ.get(ch["user"], "me") or "me", token)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"channel": key, "label": ch["label"], "business": ch["business"], "updated": now.isoformat(),
                   "followers": followers, "posts": keep}, f, ensure_ascii=False, indent=1)
    tv = sum(as_int(v.get("views")) for v in keep)
    print(f"✅ {ch['label']}: {n}건 갱신, 보관 {len(keep)}건, 누적 조회 {tv:,}, 팔로워 {followers}")


if __name__ == "__main__":
    keys = sys.argv[1:] or list(CHANNELS)
    bad = [k for k in keys if k not in CHANNELS]
    if bad:
        sys.exit(f"알 수 없는 채널: {bad}")
    for k in keys:
        try:
            collect(k)
        except Exception as e:
            print(f"❌ {k} 실패: {e}")
