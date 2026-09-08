# -*- coding: utf-8 -*-
"""
홍삼빌호텔 스레드(Threads) 완전 자동 포스팅 앱
================================================
동작 순서:
  1. topics.json 에서 이번 회차 주제를 선택 (순환 방식)
  2. Claude API 로 스레드 스타일의 홍보 글 생성 (480자 이내, 최근 글과 중복 방지)
  3. images/ 폴더에서 랜덤 3장 추출 → GitHub 공개 URL 생성
  4. Threads API 로 캐러셀(3장) + 글 게시
  5. posted_log.json 에 기록 저장 (다음 회차 중복 방지용)
  6. 토큰 만료 임박 시 자동 갱신

[2026-09-08 수정 내용]
  - 생성된 글에 '빈줄' 같은 편집 지시어가 그대로 찍히던 문제 수정
    · 프롬프트에서 '빈줄' 이라는 단어 자체를 제거하고, 형식은 예시로만 보여줌
    · 글 생성 후 clean_text() 로 '빈줄', '[빈줄]', '(줄바꿈)' 같은 지시어를 강제 삭제
    · 지시어가 남아 있거나 글이 비어 있으면 최대 3번까지 다시 생성
  - 480자 초과 시 글자 단위로 잘려 해시태그가 '#홍' 처럼 깨지던 문제 수정
    · 줄 단위로 잘라서 문장이 중간에 끊기지 않게 함
  - 광고식 편집(문장마다 줄바꿈) 금지, 사람이 폰으로 쓴 것처럼 이어 쓰기

필요한 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY      : Claude API 키
  THREADS_ACCESS_TOKEN   : Threads 장기 액세스 토큰 (60~90일 유효, 자동 갱신)
  THREADS_USER_ID        : Threads 사용자 ID (숫자)
  GITHUB_REPOSITORY      : (Actions 가 자동 주입) owner/repo 형식
"""

import json
import os
import random
import re
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
MAX_RETRY = 3                             # 글 생성 재시도 횟수

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
# 2. 글 정리 (편집 지시어 제거 + 길이 맞추기)
# ─────────────────────────────────────────────
# 모델이 실수로 출력하는 편집 지시어들. 글에 절대 남으면 안 됨.
BANNED_MARKERS = [
    r"\[?\(?빈\s*줄\)?\]?",
    r"\[?\(?줄\s*바꿈\)?\]?",
    r"\[?\(?공백\s*줄\)?\]?",
    r"\[?\(?한\s*줄\s*띄움\)?\]?",
    r"\[?\(?단락\s*구분\)?\]?",
    r"\[?\(?해시태그\)?\]?\s*:",
    r"\[?\(?본문\)?\]?\s*:",
    r"\[?\(?훅\)?\]?\s*:",
]


def clean_text(text):
    """편집 지시어 삭제, 따옴표 제거, 연속 공백줄 정리"""
    text = text.strip()
    # 앞뒤 따옴표/코드블록 제거
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    text = text.strip('"“”\'')
    # 편집 지시어 제거 (한 줄 통째로 지시어면 그 줄 삭제)
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        is_marker_line = any(re.fullmatch(p, stripped) for p in BANNED_MARKERS)
        if is_marker_line:
            continue
        for p in BANNED_MARKERS:
            line = re.sub(p, "", line)
        line = re.sub(r"[ \t]{2,}", " ", line)   # 지시어 지운 자리의 겹친 공백 정리
        lines.append(line.rstrip())
    text = "\n".join(lines)
    # 공백줄 3개 이상 → 1개로, 앞뒤 정리
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def has_banned_marker(text):
    return bool(re.search(r"빈\s*줄|줄\s*바꿈|공백\s*줄", text))


def trim_to_limit(text, limit=MAX_TEXT_LEN):
    """limit 초과 시 줄 단위로 잘라서 문장이 중간에 끊기지 않게 함.
    해시태그 줄(마지막 줄)이 잘리면 아예 빼서 '#홍' 같은 깨진 태그가 안 남게 함."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    hashtag_line = lines[-1] if lines and lines[-1].lstrip().startswith("#") else ""
    body_lines = lines[:-1] if hashtag_line else lines
    budget = limit - (len(hashtag_line) + 2 if hashtag_line else 0)
    kept, total = [], 0
    for line in body_lines:
        add = len(line) + (1 if kept else 0)
        if total + add > budget:
            break
        kept.append(line)
        total += add
    result = "\n".join(kept).rstrip()
    if hashtag_line:
        result += "\n\n" + hashtag_line
    return result.strip()


# ─────────────────────────────────────────────
# 3. Claude 로 글 생성 (MZ 말투 + 댓글 유도)
# ─────────────────────────────────────────────
def build_prompts(topic, cfg, log):
    recent = [p["text"] for p in log["posts"][-9:] if p.get("text")]
    recent_block = "\n---\n".join(recent) if recent else "(없음)"

    system = f"""당신은 대한민국 스레드(Threads)에서 조회수가 팡팡 터지는 글을 쓰는 20대 SNS 크리에이터입니다.
전북 진안군 마이산 근처의 '{cfg['brand_name']}'을 홍보하는 글을 작성합니다.

호텔 기본 정보:
{cfg['brand_info']}

[말투 — 20대가 친구한테 카톡 보내듯]
- 편한 반말, 구어체 그대로 ("근데", "아 그리고", "ㄹㅇ", "~했음", "~인 듯")
- 요즘 표현은 1~3개만 자연스럽게: "찐", "갓생", "~해버렸다", "미쳤다", "국룰", "진심", "TMI", "~인 사람 손", "이거 나만 몰랐음?", "개맛도리", "도파민", "저장 필수", "~각", "인생샷"
- 억지로 도배하면 없어 보이니까 딱 어울리는 것만
- 이모지는 0~2개
- 완벽하게 정돈된 문어체 금지. 진짜 사람이 폰으로 쓴 글처럼

[형식 — 이게 제일 중요]
- 문장마다 줄을 바꾸는 광고식 편집 절대 금지. 문장은 자연스럽게 이어서 쓰고, 이야기 흐름이 바뀔 때만 줄을 바꿈
- 글 전체가 2~4개 덩어리로만 나뉘게. 덩어리 사이는 그냥 한 번 엔터를 두 번 쳐서 띄우면 됨
- 글에는 오직 본문 글자만 출력. 형식 설명, 편집 지시, 대괄호나 괄호로 된 안내 문구, 머리말, 따옴표는 절대 출력하지 않음
- 아래는 형식 예시 (내용은 따라 쓰지 말고 모양만 참고):

솔직히 진안 처음 갈 때 기대 하나도 안 했음. 근데 마이산 탑사 올라가는 길에 안개 낀 거 보고 그냥 말이 안 나오더라. 사진으로 봤을 땐 그냥 돌탑인데 실물은 진심 다른 세계임.

내려와서 홍삼빌호텔 체크인했는데 로비에서 천둥이가 먼저 마중 나옴. 허스키가 총지배견이라는데 이 정도면 사장 아니냐. 스파 갔다 와서 침대에 뻗었는데 이게 힐링이지 싶었음.

사진보다 실물이 더 좋았던 여행지 있는 사람? 댓글로 풀어줘 🐺

#홍삼빌호텔 #마이산 #진안여행

[구조]
- 첫 문장: 스크롤 멈추게 하는 훅 (의외의 고백, 공감 백퍼 상황, 숫자 활용 등)
- 중간: 후기/경험담 느낌의 스토리 (광고 티 절대 금지)
- 마지막: 댓글을 부르는 장치 딱 1개, 매번 다른 방식으로
  (밸런스 게임 / 경험 소환 / 의견 요청 / 정보 요청 / 친구 태그 유도)
- 해시태그는 맨 마지막 줄에 2~3개만, {' '.join(cfg['hashtags'])} 중에서 선택

[길이]
- 공백 포함 {MAX_TEXT_LEN}자 이내. 넘기지 말 것. 400자 안팎이면 딱 좋음

[절대 금지]
- 최근 게시글과 비슷한 소재/문장/훅/댓글유도 방식 반복
- 과장 광고 표현, 노골적인 예약 유도
- 유행어 4개 이상 남발
- 본문 외의 다른 설명, 따옴표, 머리말, 형식 안내 문구 출력"""

    user = f"""오늘의 주제: {topic}

최근 게시글 (이것과 겹치지 않게):
{recent_block}

위 주제로 스레드 게시글 본문만 출력해 주세요. 본문 글자 외에는 아무것도 쓰지 마세요."""
    return system, user


def call_claude(system, user):
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
    return "".join(b["text"] for b in data["content"] if b["type"] == "text")


def generate_post(topic, cfg, log):
    system, user = build_prompts(topic, cfg, log)
    last_error = "알 수 없음"
    for attempt in range(1, MAX_RETRY + 1):
        raw = call_claude(system, user)
        text = clean_text(raw)

        if not text:
            last_error = "빈 글"
        elif has_banned_marker(text):
            last_error = "편집 지시어('빈줄' 등) 포함"
        elif len(text) < 80:
            last_error = f"너무 짧음 ({len(text)}자)"
        else:
            return trim_to_limit(text)

        print(f"⚠️ 글 생성 {attempt}회차 실패: {last_error} → 다시 생성")
        time.sleep(2)

    raise RuntimeError(f"글 생성 {MAX_RETRY}회 모두 실패 (마지막 원인: {last_error})")


# ─────────────────────────────────────────────
# 4. 랜덤 이미지 3장 → 공개 URL
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
# 5. Threads 캐러셀 게시 (컨테이너 생성 → 게시)
# ─────────────────────────────────────────────
def post_to_threads(text, image_urls):
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

    res = http_json(f"{THREADS_API}/{USER_ID}/threads", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "text": text,
        "access_token": ACCESS_TOKEN,
    })
    creation_id = res["id"]

    time.sleep(35)
    res = http_json(f"{THREADS_API}/{USER_ID}/threads_publish", {
        "creation_id": creation_id,
        "access_token": ACCESS_TOKEN,
    })
    return res["id"]


# ─────────────────────────────────────────────
# 6. 토큰 자동 갱신
# ─────────────────────────────────────────────
def refresh_token_if_needed():
    meta = {}
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            meta = json.load(f)
    last = meta.get("refreshed_at")
    if last:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
        if days < 20:
            return None

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

    new_token = refresh_token_if_needed()
    update_github_secret(new_token)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
