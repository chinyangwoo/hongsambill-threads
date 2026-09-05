# -*- coding: utf-8 -*-
"""
홍삼빌호텔 구글 리뷰 → 페이스북 "고객 후기" 자동 게시
================================================
동작 순서:
  1. 구글 비즈니스 프로필 API 로 최근 리뷰 조회
  2. 별점 4~5점 + 아직 게시 안 한 리뷰 중 가장 최근 1건 선택
  3. Claude 로 "고객 후기" 게시글 작성 (리뷰 원문 인용, 이름은 성만 표시)
  4. Drive 사진 1장 + 글을 페이스북 페이지에 게시 → 예약 안내 댓글
  5. google_reviews_log.json 에 게시한 리뷰 ID 기록 (중복 방지)

필요한 GitHub Secrets:
  GBP_CLIENT_ID, GBP_CLIENT_SECRET, GBP_REFRESH_TOKEN : 구글 OAuth (gbp_oauth_helper.py 로 발급)
  FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN, FB_APP_SECRET       : 페이스북 (기존)
  ANTHROPIC_API_KEY, GDRIVE_API_KEY, GH_PAT             : 기존 공용
설정: facebook_topics.json 의 brand_info / hashtags / fixed_comment / drive_folder_id 재사용
"""

import io
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from PIL import Image, ImageOps

FB_API = "https://graph.facebook.com/v21.0"
DRIVE_API = "https://www.googleapis.com/drive/v3"
GBP_ACCOUNTS = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
GBP_LOCATIONS = "https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations?readMask=name,title&pageSize=100"
GBP_REVIEWS = "https://mybusiness.googleapis.com/v4/{location}/reviews?pageSize=50&orderBy=updateTime%20desc"
CACHE_DIR = "fb_cache"
TOPICS_FILE = "facebook_topics.json"
LOG_FILE = "google_reviews_log.json"
MIN_STARS = 4
MIN_COMMENT_LEN = 15          # 이보다 짧은 리뷰(별점만 등)는 건너뜀
KST = timezone(timedelta(hours=9))
STAR = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PAGE_ID = os.environ["FB_PAGE_ID"]
FB_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
GDRIVE_API_KEY = os.environ["GDRIVE_API_KEY"]
GBP_CLIENT_ID = os.environ["GBP_CLIENT_ID"]
GBP_CLIENT_SECRET = os.environ["GBP_CLIENT_SECRET"]
GBP_REFRESH_TOKEN = os.environ["GBP_REFRESH_TOKEN"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")


def http_json(url, data=None, headers=None, method=None):
    if data is not None and not isinstance(data, bytes):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} 오류: {url.split('?')[0]}\n응답: {body}") from e


# ─────────────────────────────────────────────
# 1. 구글 리뷰 조회
# ─────────────────────────────────────────────
def google_access_token():
    res = http_json("https://oauth2.googleapis.com/token", {
        "client_id": GBP_CLIENT_ID, "client_secret": GBP_CLIENT_SECRET,
        "refresh_token": GBP_REFRESH_TOKEN, "grant_type": "refresh_token",
    })
    return res["access_token"]


def fetch_reviews():
    h = {"Authorization": f"Bearer {google_access_token()}"}
    accounts = http_json(GBP_ACCOUNTS, headers=h).get("accounts", [])
    if not accounts:
        raise RuntimeError("구글 비즈니스 프로필 계정을 찾을 수 없습니다 (API 승인/권한 확인).")
    reviews = []
    for acc in accounts:
        locs = http_json(GBP_LOCATIONS.format(account=acc["name"]), headers=h).get("locations", [])
        for loc in locs:
            full = f"{acc['name']}/{loc['name']}"          # accounts/123/locations/456
            print(f"📍 업체: {loc.get('title')} ({full})")
            res = http_json(GBP_REVIEWS.format(location=full), headers=h)
            reviews += res.get("reviews", [])
    print(f"⭐ 조회된 리뷰 {len(reviews)}건")
    return reviews


def pick_review(reviews, log):
    done = set(log["posted"])
    cands = [
        r for r in reviews
        if STAR.get(r.get("starRating"), 0) >= MIN_STARS
        and len((r.get("comment") or "").strip()) >= MIN_COMMENT_LEN
        and r["reviewId"] not in done
    ]
    cands.sort(key=lambda r: r.get("createTime", ""), reverse=True)
    return cands[0] if cands else None


def mask_name(name):
    """홍길동 → 홍**님, John Smith → J***님"""
    name = (name or "고객").strip()
    return name[0] + "*" * max(1, min(len(name) - 1, 3)) + "님"


# ─────────────────────────────────────────────
# 2. Claude 로 후기 게시글 작성
# ─────────────────────────────────────────────
def generate_post(review, cfg):
    stars = STAR.get(review.get("starRating"), 5)
    comment = review["comment"].strip()
    who = mask_name(review.get("reviewer", {}).get("displayName"))
    when = review.get("createTime", "")[:10]

    system = f"""당신은 '{cfg['brand_name']}' 페이스북 페이지 담당자입니다.
구글에 올라온 실제 고객 리뷰를 소개하는 '고객 후기' 게시글을 씁니다.

호텔 정보: {cfg['brand_info']}

구조:
1) 첫 줄: 후기 소개 한 문장 (예: "이번 주 구글에 남겨주신 후기를 소개합니다 ⭐")
2) 빈 줄
3) 리뷰 원문을 그대로 인용 (줄 앞에 " 기호 없이, 따옴표 「 」 로 감싸기). 오타·문장은 손대지 말 것.
   마지막에 "— {who} (구글 리뷰, {'★' * stars})"
4) 빈 줄
5) 감사 인사 + 리뷰에서 언급된 점을 짧게 이어받는 2~3문장 (정중한 존댓말, 과장 없이)
6) 빈 줄
7) "구글에 남겨주시는 후기 하나하나가 큰 힘이 됩니다" 류의 마무리 한 줄
8) 빈 줄
9) 해시태그: {' '.join(cfg['hashtags'][:6])} #고객후기 #구글리뷰

전체 700자 이내. 이모지 2~3개. 리뷰 내용을 바꾸거나 없는 내용을 추가하지 말 것. 글 외의 설명 출력 금지."""

    user = f"리뷰 작성일: {when}\n별점: {stars}\n리뷰 원문:\n{comment}\n\n위 리뷰로 게시글 본문만 출력해 주세요."
    body = json.dumps({"model": "claude-sonnet-5", "max_tokens": 1000, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
                                 headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                                          "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=120) as res:
        data = json.loads(res.read().decode())
    return "".join(b["text"] for b in data["content"] if b["type"] == "text").strip()


# ─────────────────────────────────────────────
# 3. Drive 사진 1장 → JPG → 공개 URL
# ─────────────────────────────────────────────
def pick_image(cfg):
    q = f"'{cfg['drive_folder_id']}' in parents and trashed = false and mimeType contains 'image/'"
    params = {"q": q, "fields": "files(id,name)", "pageSize": 1000, "key": GDRIVE_API_KEY}
    files = http_json(f"{DRIVE_API}/files?{urllib.parse.urlencode(params)}").get("files", [])
    if not files:
        raise RuntimeError("Drive 폴더에 이미지가 없습니다.")
    f = random.choice(files)
    with urllib.request.urlopen(f"{DRIVE_API}/files/{f['id']}?alt=media&key={GDRIVE_API_KEY}", timeout=120) as r:
        raw = r.read()
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1]); img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((1600, 1600))
    out = io.BytesIO(); img.save(out, "JPEG", quality=88, optimize=True)

    os.makedirs(CACHE_DIR, exist_ok=True)
    name = f"review_{datetime.now(KST):%Y%m%d_%H%M}.jpg"
    with open(os.path.join(CACHE_DIR, name), "wb") as fp:
        fp.write(out.getvalue())
    subprocess.run(["git", "config", "user.name", "auto-post-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "-A", CACHE_DIR], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        subprocess.run(["git", "commit", "-m", f"fb_cache: {name}"], check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", BRANCH], check=True)
        subprocess.run(["git", "push", "origin", f"HEAD:{BRANCH}"], check=True)
    time.sleep(10)
    return f["name"], f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{CACHE_DIR}/{name}"


# ─────────────────────────────────────────────
# 4. 페이스북 게시
# ─────────────────────────────────────────────
def post_to_facebook(text, image_url):
    res = http_json(f"{FB_API}/{PAGE_ID}/photos", {
        "url": image_url, "message": text, "access_token": FB_TOKEN,
    })
    return res.get("post_id") or res["id"]


def post_fixed_comment(post_id, text):
    if not text:
        return
    try:
        http_json(f"{FB_API}/{post_id}/comments", {"message": text, "access_token": FB_TOKEN})
        print("💬 예약 안내 댓글 등록 완료")
    except Exception as e:
        print(f"⚠️ 댓글 등록 실패: {e}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    with open(TOPICS_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    log = {"posted": []}
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)

    reviews = fetch_reviews()
    review = pick_review(reviews, log)
    if not review:
        print("ℹ️ 게시할 새 리뷰(4점 이상, 미게시)가 없습니다. 종료.")
        return

    print(f"📝 선택된 리뷰: {review.get('starRating')} / {review['comment'][:60]}...")
    text = generate_post(review, cfg)
    print(f"✍️ 생성된 글:\n{text}\n")

    img_name, img_url = pick_image(cfg)
    post_id = post_to_facebook(text, img_url)
    print(f"🚀 게시 완료! post id = {post_id}")
    time.sleep(5)
    post_fixed_comment(post_id, cfg.get("fixed_comment", ""))

    log["posted"].append(review["reviewId"])
    log.setdefault("history", []).append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "review_id": review["reviewId"], "stars": review.get("starRating"),
        "post_id": post_id, "image": img_name,
    })
    log["history"] = log["history"][-50:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
