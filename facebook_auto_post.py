# -*- coding: utf-8 -*-
"""
홍삼빌호텔 페이스북 페이지 완전 자동 포스팅 앱
================================================
인스타 자동포스팅(instagram_auto_post.py)과 동일한 구조.
같은 Drive 사진 폴더를 공유합니다.

동작 순서:
  1. facebook_topics.json 에서 이번 회차 주제 선택 (순환)
  2. Claude API 로 페이스북 게시글 생성 (존댓말·정보형, 최근 글과 중복 방지)
  3. Google Drive 사진 폴더에서 랜덤 3장 다운로드 → JPG 변환 → 저장소 fb_cache/ 커밋
  4. Facebook 페이지에 사진 3장 + 글 게시 → 첫 댓글로 예약 안내 등록
  5. facebook_posted_log.json 에 기록
  6. 토큰이 단기(만료 있음)이면 자동으로 영구 페이지 토큰으로 변환 → Secrets 저장

필요한 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY      : Claude API 키 (공용)
  FB_PAGE_ID             : 페이스북 페이지 ID (숫자)
  FB_PAGE_ACCESS_TOKEN   : 페이지 액세스 토큰
  FB_APP_SECRET          : Meta 앱 시크릿 코드 (토큰 영구화용)
  GDRIVE_API_KEY         : Google Drive API 키 (공용)
  GH_PAT                 : 변환된 토큰을 Secrets 에 자동 저장
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

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
FB_API = "https://graph.facebook.com/v21.0"
DRIVE_API = "https://www.googleapis.com/drive/v3"
CACHE_DIR = "fb_cache"
CACHE_KEEP_DAYS = 3
TOPICS_FILE = "facebook_topics.json"
LOG_FILE = "facebook_posted_log.json"
MAX_TEXT_LEN = 700
IMAGE_COUNT = 3
MAX_SIDE = 1600
MIN_RATIO, MAX_RATIO = 0.6, 2.0     # 페이스북은 비율 제한이 느슨함
KST = timezone(timedelta(hours=9))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PAGE_ID = os.environ["FB_PAGE_ID"]
ACCESS_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
APP_SECRET = os.environ.get("FB_APP_SECRET", "")
APP_ID = "2921038334925188"
GDRIVE_API_KEY = os.environ["GDRIVE_API_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")


def http_json(url, data=None, method=None):
    if data is not None and not isinstance(data, bytes):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} 오류: {url.split('?')[0]}\n응답: {body}") from e


# ─────────────────────────────────────────────
# 0. 토큰 영구화 (단기 토큰 → 만료 없는 페이지 토큰)
# ─────────────────────────────────────────────
def update_github_secret(name, value):
    pat = os.environ.get("GH_PAT")
    if not pat:
        print(f"⚠️ GH_PAT 미설정: Secrets 의 {name} 을 수동 교체해 주세요.")
        return False
    try:
        from base64 import b64encode
        from nacl import encoding, public

        def gh_api(path, method="GET", body=None):
            req = urllib.request.Request(
                f"https://api.github.com{path}",
                data=json.dumps(body).encode() if body else None,
                method=method,
                headers={"Authorization": f"Bearer {pat}",
                         "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode()
                return json.loads(raw) if raw else {}

        key = gh_api(f"/repos/{REPO}/actions/secrets/public-key")
        pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pk).encrypt(value.encode())
        gh_api(f"/repos/{REPO}/actions/secrets/{name}", method="PUT",
               body={"encrypted_value": b64encode(sealed).decode(), "key_id": key["key_id"]})
        print(f"✅ {name} 이 GitHub Secrets 에 자동 저장되었습니다.")
        return True
    except Exception as e:
        print(f"⚠️ Secrets 자동 저장 실패 ({name}): {e}")
        return False


def ensure_permanent_token():
    """토큰에 만료가 있으면 장기 토큰으로 교환 → 페이지 토큰 확보 → Secrets 저장"""
    global ACCESS_TOKEN
    if not APP_SECRET:
        return
    try:
        info = http_json(
            f"{FB_API}/debug_token?input_token={ACCESS_TOKEN}"
            f"&access_token={APP_ID}|{APP_SECRET}"
        )["data"]
    except Exception as e:
        print(f"⚠️ 토큰 검사 실패: {e}")
        return

    expires = info.get("expires_at", 0)
    if expires == 0:
        return  # 이미 영구 토큰

    print(f"🔄 단기 토큰 감지 (만료 {datetime.fromtimestamp(expires, KST):%m-%d %H:%M}) → 영구 토큰으로 변환")
    long_tok = http_json(
        f"{FB_API}/oauth/access_token?grant_type=fb_exchange_token"
        f"&client_id={APP_ID}&client_secret={APP_SECRET}&fb_exchange_token={ACCESS_TOKEN}"
    )["access_token"]

    # 사용자 토큰이면 페이지 토큰으로, 페이지 토큰이면 그대로
    if info.get("type") == "USER":
        page = http_json(f"{FB_API}/{PAGE_ID}?fields=access_token&access_token={long_tok}")
        long_tok = page["access_token"]

    check = http_json(
        f"{FB_API}/debug_token?input_token={long_tok}&access_token={APP_ID}|{APP_SECRET}"
    )["data"]
    print(f"   변환 결과: type={check.get('type')} expires_at={check.get('expires_at')} (0=영구)")
    ACCESS_TOKEN = long_tok
    update_github_secret("FB_PAGE_ACCESS_TOKEN", long_tok)


# ─────────────────────────────────────────────
# 1. 주제 선택
# ─────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"count": 0, "posts": []}


def pick_topic(log):
    with open(TOPICS_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    topics = cfg["topics"]
    return topics[log["count"] % len(topics)], cfg


# ─────────────────────────────────────────────
# 2. Claude 로 글 생성 (페이스북: 40~60대 독자, 존댓말·정보형)
# ─────────────────────────────────────────────
def generate_post(topic, cfg, log):
    recent = [p["text"] for p in log["posts"][-9:]]
    recent_block = "\n---\n".join(recent) if recent else "(없음)"

    system = f"""당신은 지역 호텔의 페이스북 페이지를 운영하는 친절한 홍보 담당자입니다.
전북 진안군 마이산 근처 '{cfg['brand_name']}' 페이지에 올릴 게시글을 작성합니다.

호텔 기본 정보:
{cfg['brand_info']}

페이스북 독자 특성: 40~60대가 많고, 가족·효도·동창회·등산 모임 여행을 계획하는 분들.
공유와 댓글이 많이 달리는 글은 '정보가 있고, 정중하며, 읽기 편한' 글입니다.

글의 구조:
1) 첫 줄: 관심을 끄는 한 문장 (질문형 또는 계절·상황 공감). 예: "가을 마이산, 언제 가야 단풍이 가장 예쁠까요?"
2) 빈 줄
3) 본문 3~5개 짧은 문단, 문단 사이 빈 줄. 실제로 도움이 되는 정보(코스, 소요시간, 준비물, 시기 등)를 반드시 포함.
   호텔 자랑보다 '진안 여행이 좋은 이유'를 먼저, 호텔은 자연스럽게 한두 문장.
4) 빈 줄
5) 마무리: 부드러운 참여 유도 한 문장 (예: "가족과 함께 가고 싶은 분은 댓글로 태그해 주세요", "다녀오신 분들의 팁도 댓글로 나눠 주세요")
6) 빈 줄
7) 해시태그 3~5개: {' '.join(cfg['hashtags'])} 중심으로

말투:
- 정중한 존댓말 (~합니다, ~해요 혼용 가능), 과장·유행어 없음
- 이모지 2~4개만 (🍁🏔️🌿🛁 등 상황에 맞게)
- 전체 길이 공백 포함 {MAX_TEXT_LEN}자 이내

절대 금지:
- 최근 게시글과 비슷한 첫 줄/소재/마무리 반복
- 가격 언급, 과장 광고, 노골적 예약 강요
- 글 외의 설명·따옴표·머리말 출력"""

    user = f"""오늘의 주제: {topic}

최근 게시글 (이것과 겹치지 않게):
{recent_block}

위 주제로 페이스북 게시글 본문만 출력해 주세요."""

    body = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 1200,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        data = json.loads(res.read().decode())
    return "".join(b["text"] for b in data["content"] if b["type"] == "text").strip()


# ─────────────────────────────────────────────
# 3. Drive 이미지 → JPG → 공개 URL
# ─────────────────────────────────────────────
def list_drive_images(folder_id):
    q = f"'{folder_id}' in parents and trashed = false and mimeType contains 'image/'"
    files, token = [], None
    while True:
        params = {"q": q, "fields": "nextPageToken,files(id,name,mimeType,size)",
                  "pageSize": 1000, "key": GDRIVE_API_KEY}
        if token:
            params["pageToken"] = token
        res = http_json(f"{DRIVE_API}/files?{urllib.parse.urlencode(params)}")
        files += res.get("files", [])
        token = res.get("nextPageToken")
        if not token:
            break
    return files


def download_drive_file(file_id):
    with urllib.request.urlopen(f"{DRIVE_API}/files/{file_id}?alt=media&key={GDRIVE_API_KEY}", timeout=120) as res:
        return res.read()


def to_jpg(raw_bytes):
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    w, h = img.size
    ratio = w / h
    if ratio < MIN_RATIO:
        new_h = int(w / MIN_RATIO); top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif ratio > MAX_RATIO:
        new_w = int(h * MAX_RATIO); left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def prune_cache():
    if not os.path.isdir(CACHE_DIR):
        return
    cutoff = time.time() - CACHE_KEEP_DAYS * 86400
    for f in os.listdir(CACHE_DIR):
        p = os.path.join(CACHE_DIR, f)
        if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
            os.remove(p)


def git_push_cache(names):
    def git(*args):
        subprocess.run(["git", *args], check=True)
    git("config", "user.name", "auto-post-bot")
    git("config", "user.email", "bot@users.noreply.github.com")
    git("add", "-A", CACHE_DIR)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        git("commit", "-m", f"fb_cache: {', '.join(names)}")
        git("pull", "--rebase", "origin", BRANCH)
        git("push", "origin", f"HEAD:{BRANCH}")
    time.sleep(10)


def pick_images(cfg):
    files = list_drive_images(cfg["drive_folder_id"])
    if len(files) < IMAGE_COUNT:
        raise RuntimeError(f"Drive 폴더에 이미지가 {len(files)}장뿐입니다 (최소 {IMAGE_COUNT}장).")
    chosen = random.sample(files, IMAGE_COUNT)
    os.makedirs(CACHE_DIR, exist_ok=True)
    prune_cache()
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    names, urls = [], []
    for i, f in enumerate(chosen, 1):
        jpg = to_jpg(download_drive_file(f["id"]))
        name = f"{stamp}_{i}.jpg"
        with open(os.path.join(CACHE_DIR, name), "wb") as fp:
            fp.write(jpg)
        names.append(name)
        urls.append(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{CACHE_DIR}/{name}")
        print(f"   {f['name']} → {name} ({len(jpg)//1024} KB)")
    git_push_cache(names)
    return [f["name"] for f in chosen], urls


# ─────────────────────────────────────────────
# 4. 페이스북 페이지 게시 (사진 3장 + 글) → 첫 댓글
# ─────────────────────────────────────────────
def post_to_facebook(text, image_urls):
    photo_ids = []
    for url in image_urls:
        res = http_json(f"{FB_API}/{PAGE_ID}/photos", {
            "url": url, "published": "false", "access_token": ACCESS_TOKEN,
        })
        photo_ids.append(res["id"])
        time.sleep(2)

    data = {"message": text, "access_token": ACCESS_TOKEN}
    for i, pid in enumerate(photo_ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid})
    res = http_json(f"{FB_API}/{PAGE_ID}/feed", data)
    return res["id"]


def post_fixed_comment(post_id, text):
    if not text:
        return None
    try:
        res = http_json(f"{FB_API}/{post_id}/comments", {"message": text, "access_token": ACCESS_TOKEN})
        print(f"💬 고정 댓글 등록 완료: {res.get('id')}")
        return res.get("id")
    except Exception as e:
        print(f"⚠️ 댓글 등록 실패 (게시는 완료됨): {e}")
        return None


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    ensure_permanent_token()

    log = load_log()
    topic, cfg = pick_topic(log)
    print(f"📌 이번 회차 주제: {topic}")

    text = generate_post(topic, cfg, log)
    print(f"✍️ 생성된 글 ({len(text)}자):\n{text}\n")

    print("🖼️ Drive 에서 이미지 추출·변환 중...")
    chosen, urls = pick_images(cfg)
    print(f"🖼️ 선택된 이미지: {chosen}")

    post_id = post_to_facebook(text, urls)
    print(f"🚀 게시 완료! post id = {post_id}")
    time.sleep(5)
    post_fixed_comment(post_id, cfg.get("fixed_comment", ""))

    log["count"] += 1
    log["posts"].append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "topic": topic, "text": text, "images": chosen, "post_id": post_id,
    })
    log["posts"] = log["posts"][-30:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
