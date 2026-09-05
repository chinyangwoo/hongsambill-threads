# -*- coding: utf-8 -*-
"""
홍삼빌호텔 인스타그램 완전 자동 포스팅 앱
================================================
스레드 자동포스팅(threads_auto_post.py)과 동일한 구조.
같은 저장소에 두면 images/ 폴더를 공유합니다.

동작 순서:
  1. instagram_topics.json 에서 이번 회차 주제 선택 (순환)
  2. Claude API 로 인스타 캡션 생성 (첫 줄 훅 + 본문 + 해시태그, 최근 글과 중복 방지)
  3. Google Drive 사진 폴더에서 랜덤 3장 다운로드 → 인스타 규격 JPG 로 자동 변환
     (PNG 그대로 넣어도 됨, 비율·크기 자동 보정)
  4. 변환된 JPG 를 저장소 ig_cache/ 에 커밋 → 공개 URL 확보
  5. Instagram API 로 캐러셀(3장) 컨테이너 생성 → 처리 완료 확인 → 게시
  6. instagram_posted_log.json 에 기록 (다음 회차 중복 방지)
  7. 토큰 발급 40일 경과 시 자동 갱신 (60일 만료)

필요한 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY        : Claude API 키 (스레드와 공용)
  INSTAGRAM_ACCESS_TOKEN   : Instagram 장기 액세스 토큰 (60일, 자동 갱신)
  INSTAGRAM_USER_ID        : Instagram 비즈니스/크리에이터 계정 ID (숫자)
  GDRIVE_API_KEY           : Google Drive API 키 (공개 폴더 조회·다운로드용)
  GH_PAT                   : (선택) 갱신 토큰을 Secrets 에 자동 저장
  GITHUB_REPOSITORY        : Actions 자동 주입

Drive 폴더 ID 는 instagram_topics.json 의 "drive_folder_id" 에 기록.
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
IG_API = "https://graph.instagram.com/v21.0"
DRIVE_API = "https://www.googleapis.com/drive/v3"
CACHE_DIR = "ig_cache"          # 변환된 JPG 임시 보관 (공개 URL 용)
CACHE_KEEP_DAYS = 3             # 이 기간 지난 캐시는 자동 삭제
TOPICS_FILE = "instagram_topics.json"
LOG_FILE = "instagram_posted_log.json"
TOKEN_FILE = ".ig_token_meta.json"
MAX_CAPTION_LEN = 900           # 인스타 한도 2,200자. 가독성 위해 900자 내로
IMAGE_COUNT = 3                 # 캐러셀 장수 (인스타 캐러셀 2~10장)
MAX_SIDE = 1440                 # 인스타 권장 가로 1080~1440px
MIN_RATIO, MAX_RATIO = 0.8, 1.91   # 인스타 허용 비율 4:5 ~ 1.91:1
KST = timezone(timedelta(hours=9))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ACCESS_TOKEN = os.environ["INSTAGRAM_ACCESS_TOKEN"]
USER_ID = os.environ["INSTAGRAM_USER_ID"]
GDRIVE_API_KEY = os.environ["GDRIVE_API_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")


def http_json(url, data=None, method=None):
    """표준 라이브러리만 사용하는 HTTP 헬퍼"""
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
# 1. 주제 선택 (순환)
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
# 2. Claude 로 캡션 생성
# ─────────────────────────────────────────────
def generate_caption(topic, cfg, log):
    recent = [p["text"] for p in log["posts"][-9:]]
    recent_block = "\n---\n".join(recent) if recent else "(없음)"

    system = f"""당신은 인스타그램에서 저장·공유가 많이 되는 호텔 계정을 운영하는 20대 SNS 마케터입니다.
전북 진안군 마이산 근처의 '{cfg['brand_name']}' 공식 인스타그램 캡션을 작성합니다.

호텔 기본 정보:
{cfg['brand_info']}

인스타그램 캡션의 구조 (스레드와 다름 — 반드시 지킬 것):
1) 첫 줄 (125자 이내): '더보기'를 누르기 전에 보이는 유일한 줄. 스크롤을 멈추게 하는 훅.
   예시 스타일: "서울에서 3시간, 사람 없는 마이산 뷰 호텔", "홍삼스파 하고 자면 다음날 몸이 다름 (진심)"
2) 빈 줄
3) 본문 3~6개 짧은 문단: 후기·경험담·정보 느낌. 문장은 짧게, 문단마다 빈 줄. 광고 티 금지.
   실용 정보(코스, 소요시간, 꿀팁 등)를 1개 이상 넣어 '저장' 가치를 만들 것.
4) 빈 줄
5) 댓글·저장·태그를 부르는 마무리 한 줄 (매번 다른 방식):
   · "같이 갈 사람 태그" · "저장해두고 가을에 꺼내보기" · "밸런스 게임: 스파 먼저 vs 등산 먼저"
   · "진안 가본 사람 꿀팁 댓글로" · "이런 여행 좋아하는 사람 손"
6) 빈 줄
7) 해시태그 10~15개: 반드시 {' '.join(cfg['hashtags'])} 를 포함하고,
   주제에 맞는 태그를 추가 (예: #호캉스 #국내여행 #힐링여행 #등산스타그램 #가을여행)

말투:
- 친구에게 추천하듯 친근한 반말 (스레드보다 살짝 차분·감성적)
- MZ 표현은 자연스러운 것 1~2개만 ("찐", "인생샷", "저장 필수", "~각", "미쳤다")
- 이모지 3~6개, 문단 시작이나 강조 위치에 자연스럽게
- 전체 길이 공백 포함 {MAX_CAPTION_LEN}자 이내 (해시태그 포함)

절대 금지:
- 최근 게시글과 비슷한 훅/소재/마무리 방식 반복
- 과장 광고, 노골적 예약 유도, 가격 언급
- 캡션 외의 설명·따옴표·머리말 출력"""

    user = f"""오늘의 주제: {topic}

최근 게시글 (이것과 겹치지 않게):
{recent_block}

위 주제로 인스타그램 캡션 본문만 출력해 주세요."""

    body = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 1200,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        data = json.loads(res.read().decode())

    text = "".join(b["text"] for b in data["content"] if b["type"] == "text").strip()
    return text[:2190]


# ─────────────────────────────────────────────
# 3. Google Drive 폴더에서 랜덤 3장 → JPG 변환 → 공개 URL
# ─────────────────────────────────────────────
def list_drive_images(folder_id):
    """공개(링크 공유) 폴더의 이미지 목록 (API 키만으로 조회 가능)"""
    q = f"'{folder_id}' in parents and trashed = false and mimeType contains 'image/'"
    files, token = [], None
    while True:
        params = {
            "q": q, "fields": "nextPageToken,files(id,name,mimeType,size)",
            "pageSize": 1000, "key": GDRIVE_API_KEY,
        }
        if token:
            params["pageToken"] = token
        res = http_json(f"{DRIVE_API}/files?{urllib.parse.urlencode(params)}")
        files += res.get("files", [])
        token = res.get("nextPageToken")
        if not token:
            break
    return files


def download_drive_file(file_id):
    url = f"{DRIVE_API}/files/{file_id}?alt=media&key={GDRIVE_API_KEY}"
    with urllib.request.urlopen(url, timeout=120) as res:
        return res.read()


def to_instagram_jpg(raw_bytes):
    """어떤 형식이든 → 인스타 규격 JPG (RGB, 비율 4:5~1.91:1 로 중앙 크롭, 최대 1440px)"""
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)           # 휴대폰 회전 정보 반영
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    w, h = img.size
    ratio = w / h
    if ratio < MIN_RATIO:                        # 너무 세로로 김 → 위아래 크롭
        new_h = int(w / MIN_RATIO)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif ratio > MAX_RATIO:                      # 너무 가로로 김 → 좌우 크롭
        new_w = int(h * MAX_RATIO)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))

    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def prune_cache():
    """오래된 캐시 JPG 삭제 (저장소 용량 관리)"""
    if not os.path.isdir(CACHE_DIR):
        return
    cutoff = time.time() - CACHE_KEEP_DAYS * 86400
    for f in os.listdir(CACHE_DIR):
        p = os.path.join(CACHE_DIR, f)
        if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
            os.remove(p)


def git_push_cache(names):
    """변환된 JPG 를 저장소에 커밋·푸시해야 raw.githubusercontent URL 이 살아남"""
    def git(*args):
        subprocess.run(["git", *args], check=True)
    git("config", "user.name", "auto-post-bot")
    git("config", "user.email", "bot@users.noreply.github.com")
    git("add", "-A", CACHE_DIR)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        git("commit", "-m", f"ig_cache: {', '.join(names)}")
        git("pull", "--rebase", "origin", BRANCH)
        git("push", "origin", f"HEAD:{BRANCH}")
    time.sleep(10)   # raw URL 반영 대기


def pick_images(cfg):
    folder_id = cfg["drive_folder_id"]
    files = list_drive_images(folder_id)
    if len(files) < IMAGE_COUNT:
        raise RuntimeError(
            f"Drive 폴더에 이미지가 {len(files)}장뿐입니다 (최소 {IMAGE_COUNT}장). "
            "폴더가 '링크가 있는 모든 사용자' 로 공유되어 있는지 확인하세요."
        )
    chosen = random.sample(files, IMAGE_COUNT)

    os.makedirs(CACHE_DIR, exist_ok=True)
    prune_cache()
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    names, urls = [], []
    for i, f in enumerate(chosen, 1):
        jpg = to_instagram_jpg(download_drive_file(f["id"]))
        name = f"{stamp}_{i}.jpg"
        with open(os.path.join(CACHE_DIR, name), "wb") as fp:
            fp.write(jpg)
        names.append(name)
        urls.append(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{CACHE_DIR}/{name}")
        print(f"   {f['name']} → {name} ({len(jpg)//1024} KB)")

    git_push_cache(names)
    return [f["name"] for f in chosen], urls


# ─────────────────────────────────────────────
# 4. Instagram 캐러셀 게시
# ─────────────────────────────────────────────
def wait_container(container_id, max_wait=180):
    """컨테이너 처리 상태를 폴링 → FINISHED 되면 반환 (스레드의 고정 35초 대기보다 안전)"""
    waited = 0
    while waited < max_wait:
        res = http_json(
            f"{IG_API}/{container_id}?fields=status_code,status&access_token={ACCESS_TOKEN}"
        )
        code = res.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"컨테이너 처리 실패: {res}")
        time.sleep(5)
        waited += 5
    raise RuntimeError("컨테이너 처리 시간 초과 (이미지 용량/비율 확인 필요)")


def post_to_instagram(caption, image_urls):
    # 4-1. 각 이미지 → 캐러셀 자식 컨테이너
    child_ids = []
    for url in image_urls:
        res = http_json(f"{IG_API}/{USER_ID}/media", {
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": ACCESS_TOKEN,
        })
        child_ids.append(res["id"])
        time.sleep(2)

    for cid in child_ids:
        wait_container(cid)

    # 4-2. 캐러셀 부모 컨테이너 (캡션 + 자식들)
    res = http_json(f"{IG_API}/{USER_ID}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "caption": caption,
        "access_token": ACCESS_TOKEN,
    })
    creation_id = res["id"]
    wait_container(creation_id)

    # 4-3. 게시
    res = http_json(f"{IG_API}/{USER_ID}/media_publish", {
        "creation_id": creation_id,
        "access_token": ACCESS_TOKEN,
    })
    return res["id"]


# ─────────────────────────────────────────────
# 5. 토큰 자동 갱신 (60일 만료 → 40일마다 갱신)
# ─────────────────────────────────────────────
def refresh_token_if_needed():
    meta = {}
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            meta = json.load(f)
    last = meta.get("refreshed_at")
    if last:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
        if days < 40:
            return None

    try:
        res = http_json(
            "https://graph.instagram.com/refresh_access_token"
            f"?grant_type=ig_refresh_token&access_token={ACCESS_TOKEN}"
        )
        new_token = res["access_token"]
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"refreshed_at": datetime.now(timezone.utc).isoformat()}, f)
        print("🔄 인스타 토큰이 갱신되었습니다.")
        return new_token
    except Exception as e:
        print(f"⚠️ 토큰 갱신 실패 (다음 실행에서 재시도): {e}")
        return None


def update_github_secret(new_token):
    pat = os.environ.get("GH_PAT")
    if not pat or not new_token:
        if new_token:
            print("⚠️ GH_PAT 미설정: Secrets 의 INSTAGRAM_ACCESS_TOKEN 을 수동 교체해 주세요.")
        return
    try:
        from base64 import b64encode
        from nacl import encoding, public

        def gh_api(path, method="GET", body=None):
            req = urllib.request.Request(
                f"https://api.github.com{path}",
                data=json.dumps(body).encode() if body else None,
                method=method,
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode()
                return json.loads(raw) if raw else {}

        key = gh_api(f"/repos/{REPO}/actions/secrets/public-key")
        pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pk).encrypt(new_token.encode())
        gh_api(
            f"/repos/{REPO}/actions/secrets/INSTAGRAM_ACCESS_TOKEN",
            method="PUT",
            body={"encrypted_value": b64encode(sealed).decode(), "key_id": key["key_id"]},
        )
        print("✅ 새 토큰이 GitHub Secrets 에 자동 저장되었습니다.")
    except Exception as e:
        print(f"⚠️ Secrets 자동 저장 실패, 수동 교체 필요: {e}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    log = load_log()
    topic, cfg = pick_topic(log)
    print(f"📌 이번 회차 주제: {topic}")

    caption = generate_caption(topic, cfg, log)
    print(f"✍️ 생성된 캡션 ({len(caption)}자):\n{caption}\n")

    print("🖼️ Drive 에서 이미지 추출·변환 중...")
    chosen, urls = pick_images(cfg)
    print(f"🖼️ 선택된 이미지: {chosen}")

    post_id = post_to_instagram(caption, urls)
    print(f"🚀 게시 완료! media id = {post_id}")

    log["count"] += 1
    log["posts"].append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "topic": topic,
        "text": caption,
        "images": chosen,
        "post_id": post_id,
    })
    log["posts"] = log["posts"][-30:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    update_github_secret(refresh_token_if_needed())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
