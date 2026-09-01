# -*- coding: utf-8 -*-
"""
홍삼빌호텔 스레드(Threads) 완전 자동 포스팅 앱
================================================
동작 순서:
  1. topics.json 에서 이번 회차 주제를 선택 (순환 방식)
  2. Claude API 로 스레드 스타일의 홍보 글 생성 (500자 이내, 최근 글과 중복 방지)
  3. images/ 폴더에서 랜덤 3장 추출 → GitHub 공개 URL 생성
  4. Threads API 로 캐러셀(3장) + 글 게시
  5. posted_log.json 에 기록 저장 (다음 회차 중복 방지용)
  6. 토큰 만료 임박 시 자동 갱신

필요한 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY      : Claude API 키
  THREADS_ACCESS_TOKEN   : Threads 장기 액세스 토큰 (60~90일 유효, 자동 갱신)
  THREADS_USER_ID        : Threads 사용자 ID (숫자)
  GITHUB_REPOSITORY      : (Actions 가 자동 주입) owner/repo 형식
"""

import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
THREADS_API = "https://graph.threads.net/v1.0"
IMAGE_DIR = "images"
LOG_FILE = "posted_log.json"
TOKEN_FILE = ".token_meta.json"          # 토큰 갱신 날짜 기록
MAX_TEXT_LEN = 480                        # Threads 500자 제한, 여유분 확보
IMAGE_COUNT = 3                           # 랜덤 추출 이미지 수
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
KST = timezone(timedelta(hours=9))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
USER_ID = os.environ["THREADS_USER_ID"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")   # 예: "yangwoo/hongsambill-threads"
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")


def http_json(url, data=None, method=None):
    """간단한 HTTP 요청 헬퍼 (표준 라이브러리만 사용)"""
    if data is not None and not isinstance(data, bytes):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} 오류: {url}\n응답: {body}") from e


# ─────────────────────────────────────────────
# 1. 주제 선택 (순환)
# ─────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"count": 0, "posts": []}


def pick_topic(log):
    with open("topics.json", encoding="utf-8") as f:
        cfg = json.load(f)
    topics = cfg["topics"]
    topic = topics[log["count"] % len(topics)]
    return topic, cfg


# ─────────────────────────────────────────────
# 2. Claude 로 글 생성
# ─────────────────────────────────────────────
def generate_post(topic, cfg, log):
    recent = [p["text"] for p in log["posts"][-9:]]  # 최근 3일치(9개)와 중복 방지
    recent_block = "\n---\n".join(recent) if recent else "(없음)"

    system = f"""당신은 대한민국 스레드(Threads)에서 조회수가 잘 터지는 글을 쓰는 SNS 전문 카피라이터입니다.
전북 진안군 마이산 근처의 '{cfg['brand_name']}'을 홍보하는 글을 작성합니다.

호텔 기본 정보:
{cfg['brand_info']}

스레드에서 잘 터지는 글의 규칙:
- 첫 문장은 반드시 스크롤을 멈추게 하는 훅(궁금증, 공감, 의외성)으로 시작
- 광고 티가 나지 않는, 친구에게 말하듯 편안한 반말 또는 존댓말 톤
- 짧은 문장, 줄바꿈을 자주 사용해 모바일에서 읽기 쉽게
- 이모지는 1~3개만 자연스럽게
- 마지막에 가벼운 질문이나 행동 유도로 댓글을 부르기
- 해시태그는 마지막 줄에 2~3개만: {' '.join(cfg['hashtags'])} 중에서 선택
- 전체 길이는 공백 포함 {MAX_TEXT_LEN}자 이내 (매우 중요!)

절대 금지:
- 최근 게시글과 비슷한 소재/문장 반복
- 과장 광고 표현("최고", "1등" 등 근거 없는 주장)
- 글 외의 다른 설명, 따옴표, 머리말 출력"""

    user = f"""오늘의 주제: {topic}

최근 게시글 (이것과 겹치지 않게):
{recent_block}

위 주제로 스레드 게시글 본문만 출력해 주세요."""

    body = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 800,
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
    if len(text) > 495:
        text = text[:495]
    return text


# ─────────────────────────────────────────────
# 3. 랜덤 이미지 3장 → 공개 URL
# ─────────────────────────────────────────────
def pick_images():
    files = [
        f for f in os.listdir(IMAGE_DIR)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTS
    ]
    if len(files) < IMAGE_COUNT:
        raise RuntimeError(
            f"images/ 폴더에 이미지가 {len(files)}장뿐입니다. 최소 {IMAGE_COUNT}장이 필요합니다."
        )
    chosen = random.sample(files, IMAGE_COUNT)
    urls = [
        f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{IMAGE_DIR}/{urllib.parse.quote(f)}"
        for f in chosen
    ]
    return chosen, urls


# ─────────────────────────────────────────────
# 4. Threads 캐러셀 게시 (컨테이너 생성 → 게시)
# ─────────────────────────────────────────────
def post_to_threads(text, image_urls):
    # 4-1. 각 이미지를 캐러셀 아이템 컨테이너로 생성
    child_ids = []
    for url in image_urls:
        res = http_json(f"{THREADS_API}/{USER_ID}/threads", {
            "media_type": "IMAGE",
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": ACCESS_TOKEN,
        })
        child_ids.append(res["id"])
        time.sleep(3)

    # 4-2. 캐러셀 컨테이너 생성 (글 + 자식 이미지들)
    res = http_json(f"{THREADS_API}/{USER_ID}/threads", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "text": text,
        "access_token": ACCESS_TOKEN,
    })
    creation_id = res["id"]

    # 4-3. 미디어 처리 대기 후 게시 (Meta 권장: 30초 내외)
    time.sleep(35)
    res = http_json(f"{THREADS_API}/{USER_ID}/threads_publish", {
        "creation_id": creation_id,
        "access_token": ACCESS_TOKEN,
    })
    return res["id"]


# ─────────────────────────────────────────────
# 5. 토큰 자동 갱신 (만료 60일 → 40일마다 갱신)
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
            return None  # 아직 갱신 불필요

    try:
        res = http_json(
            f"https://graph.threads.net/refresh_access_token"
            f"?grant_type=th_refresh_token&access_token={ACCESS_TOKEN}"
        )
        new_token = res["access_token"]
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"refreshed_at": datetime.now(timezone.utc).isoformat()}, f)
        print("🔄 토큰이 갱신되었습니다.")
        return new_token
    except Exception as e:
        print(f"⚠️ 토큰 갱신 실패 (다음 실행에서 재시도): {e}")
        return None


def update_github_secret(new_token):
    """새 토큰을 GitHub Secret 에 자동 저장 (GH_PAT 이 설정된 경우)"""
    pat = os.environ.get("GH_PAT")
    if not pat or not new_token:
        if new_token:
            print("⚠️ GH_PAT 미설정: GitHub Secrets 의 THREADS_ACCESS_TOKEN 을 수동으로 교체해 주세요.")
            print(f"   새 토큰: {new_token[:20]}... (전체 값은 Actions 로그 보안상 출력 생략)")
        return
    try:
        # PyNaCl 로 시크릿 암호화 업로드
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
            f"/repos/{REPO}/actions/secrets/THREADS_ACCESS_TOKEN",
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

    text = generate_post(topic, cfg, log)
    print(f"✍️ 생성된 글 ({len(text)}자):\n{text}\n")

    chosen, urls = pick_images()
    print(f"🖼️ 선택된 이미지: {chosen}")

    post_id = post_to_threads(text, urls)
    print(f"🚀 게시 완료! post id = {post_id}")

    # 로그 저장 (최근 30개 유지)
    log["count"] += 1
    log["posts"].append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "topic": topic,
        "text": text,
        "images": chosen,
        "post_id": post_id,
    })
    log["posts"] = log["posts"][-30:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    # 토큰 갱신 체크
    new_token = refresh_token_if_needed()
    update_github_secret(new_token)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
