# -*- coding: utf-8 -*-
"""
홍삼빌호텔 구글 리뷰 → SNS(스레드·인스타그램·페이스북) 자동 게시 앱
====================================================================
workflow_dispatch 수동 실행 전용. 예약 실행 없음.

동작 순서:
  1. review_text 해시를 review_sns_log.json 과 대조 → 해시+플랫폼 조합으로 판정,
     이미 성공 게시한 플랫폼만 건너뛰고 아직 안 된 플랫폼은 게시 (전부 게시됐으면 중단)
  2. Claude API 1회 호출로 스레드·인스타·페이스북 3개 글을 JSON 으로 한 번에 생성
     (검증 불합격 시 지적사항을 붙여 최대 3회 재생성 — 기존 스크립트와 동일 패턴)
  3. images/ 폴더에서 랜덤 3장 선택 → 인스타 규격 JPG 변환 → review_sns_cache/ 커밋
     → raw.githubusercontent 공개 URL 확보 (3개 플랫폼 공용)
  4. 각 플랫폼 API 로 게시 (스레드·인스타 캐러셀, 페이스북 3장 첨부)
     페이스북은 게시 후 첫 댓글로 예약번호·직통상담 자동 등록
  5. review_sns_log.json 에 시각·해시·플랫폼별 게시ID·생성 본문 기록 (커밋은 워크플로가)

안전장치:
  - stars 4점 미만 또는 본문 15자 미만이면 게시하지 않고 정상 종료
  - 리뷰 작성자 실명 금지 → reviewer_label 로만 지칭
  - 리뷰에 없는 사실·수치·가격·할인·재개장 시점 생성 금지 (프롬프트 + 코드 검증)
  - 홍삼 효능 주장 금지, 호텔이 음식을 판매·제공한다는 표현 금지
  - 한 플랫폼이 실패해도 나머지는 계속 진행, 실패 내용은 로그에 기록

필요한 환경변수 (GitHub Secrets — 기존 것 그대로 사용, 새 발급 없음):
  ANTHROPIC_API_KEY
  THREADS_ACCESS_TOKEN, THREADS_USER_ID          (스레드 게시 시)
  INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_USER_ID      (인스타 게시 시)
  FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN               (페이스북 게시 시)
  GH_PAT                                         (선택 — 이 스크립트에서는 미사용)

워크플로 입력 → 환경변수:
  REVIEW_TEXT     : 구글 리뷰 본문 (필수)
  REVIEWER_LABEL  : 작성자 표기 (기본 "한 손님")
  PLATFORMS       : all / threads / instagram / facebook (기본 all)
  STARS           : 별점 (선택, 예: 5 또는 4.5)
"""

import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from PIL import Image, ImageOps

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
THREADS_API = "https://graph.threads.net/v1.0"
IG_API = "https://graph.instagram.com/v21.0"
FB_API = "https://graph.facebook.com/v21.0"

IMAGE_DIR = "images"
CACHE_DIR = "review_sns_cache"      # 변환된 JPG 임시 보관 (공개 URL 용)
CACHE_KEEP_DAYS = 3
LOG_FILE = "review_sns_log.json"

IMAGE_COUNT = 3
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
MAX_SIDE = 1440                     # 인스타 권장 최대변
MIN_RATIO, MAX_RATIO = 0.8, 1.91    # 인스타 허용 비율 (가장 엄격 → 3개 플랫폼 공용)
KST = timezone(timedelta(hours=9))

MAX_RETRY = 3                       # 글 검증 불합격 시 재생성 횟수
MIN_REVIEW_LEN = 15                 # 리뷰 본문 최소 길이
MIN_STARS = 4.0                     # 이 미만이면 게시하지 않음

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4000"))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

REVIEW_TEXT = os.environ.get("REVIEW_TEXT", "").strip()
REVIEWER_LABEL = os.environ.get("REVIEWER_LABEL", "").strip() or "한 손님"
PLATFORMS_INPUT = (os.environ.get("PLATFORMS", "all").strip().lower() or "all")
STARS_RAW = os.environ.get("STARS", "").strip()

RESERVATION_NO = "1661-3889"
DIRECT_NO = "010-8545-0290"
FB_FIRST_COMMENT = f"홍삼빌호텔 예약번호 {RESERVATION_NO}, 직통상담문의 {DIRECT_NO}"

# 검증용 금지 표현 (facebook_auto_post.py 와 동일 계열)
HEALTH_WORDS = ["면역", "효능", "건강에 좋", "피로 회복에 좋", "몸에 좋"]
FOOD_SALE_WORDS = ["고기를 제공", "식사를 제공", "고기 판매", "고기를 준비해 드", "고기 준비해 드", "식사 제공"]
FABRICATION_WORDS = ["할인", "이벤트 진행", "재개장", "리뉴얼 오픈", "오픈 예정", "특가"]

HOTEL_INFO = """- 홍삼빌호텔: 전북 진안군, 마이산 인근의 호텔. 객실 40개. 가족 단위 방문객이 많음.
- 예약 대표번호 1661-3889 / 직통 상담 010-8545-0290
- 2층 약 50평 유리난간 테라스 (휴식·산 조망. 테라스에서 바베큐는 하지 않음)
- 바베큐장: 부스 3개(부스당 4~6명), 직화구이기 3대, 항아리바베큐 설비 1대. 호텔은 장소·설비만 제공.
- 총지배견 천둥(허스키): 소나무 그늘 아래 견사에서 지냄.
- 주변: 마이산(탑사·은수사, 봄 벚꽃길·가을 단풍), 홍삼스파(같은 운영사, 인근), 용담호, 운일암반일암
- 주소, 체크인 시간, 주차, 조식 여부, 객실 유형, 요금은 확인되지 않았으니 쓰지 마라."""


def http_json(url, data=None, method=None):
    """표준 라이브러리만 사용하는 HTTP 헬퍼 (기존 스크립트와 동일)"""
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
# 0. 입력 검사 · 중복(해시) 검사
# ─────────────────────────────────────────────
def resolve_platforms():
    if PLATFORMS_INPUT == "all":
        return ["threads", "instagram", "facebook"]
    if PLATFORMS_INPUT in ("threads", "instagram", "facebook"):
        return [PLATFORMS_INPUT]
    raise RuntimeError(f"platforms 값이 잘못됨: {PLATFORMS_INPUT} (all/threads/instagram/facebook)")


def parse_stars():
    if not STARS_RAW:
        return None
    try:
        return float(STARS_RAW.replace("점", "").strip())
    except ValueError:
        print(f"⚠️ stars 값을 숫자로 읽을 수 없어 무시합니다: {STARS_RAW!r}")
        return None


def review_hash(text):
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
    else:
        log = {"count": 0, "posts": []}
    log.setdefault("count", 0)
    log.setdefault("posts", [])
    return log


def already_posted_platforms(log, h):
    """이 리뷰(해시)로 이미 '성공' 게시된 플랫폼 집합. 실패했던 플랫폼은 재시도 대상."""
    done = set()
    for p in log["posts"]:
        if p.get("hash") != h:
            continue
        for name, r in (p.get("results") or {}).items():
            if r.get("ok"):
                done.add(name)
    return done


# ─────────────────────────────────────────────
# 1. Claude 1회 호출 → 3개 플랫폼 글 JSON 생성
# ─────────────────────────────────────────────
SYSTEM_PROMPT = f"""# 역할
너는 홍삼빌호텔의 SNS 콘텐츠 라이터다. 실제 고객이 남긴 구글 리뷰 1건을 소재로
스레드(Threads)·인스타그램·페이스북 게시글 3개를 한 번에 작성한다.
네 글은 사람이 검수하지 않고 그대로 발행된다. 사실 오류·과장은 곧 호텔 신뢰도 하락이다.

# 호텔 정보 (이 범위 안에서만 쓴다)
{HOTEL_INFO}

# 공통 절대 규칙 (모든 플랫폼)
1. 리뷰 작성자의 실명·아이디를 절대 쓰지 마라. 작성자는 반드시 지정된 표기(reviewer_label)로만 지칭한다.
2. 리뷰에 없는 사실·수치·가격·할인·이벤트·재개장/오픈 시점을 만들어내지 마라.
   리뷰와 위 [호텔 정보]에 있는 내용만 쓴다.
3. 홍삼 효능 주장 금지 ("면역력", "피로 회복" 등). "홍삼"은 지역명·상호로만 언급한다.
4. 호텔이 고기·음식을 판매·제공한다는 표현 금지. 바베큐장은 "장소·설비 제공"으로만.
5. 리뷰에 아쉬운 점이 있으면 짧게 솔직하게 인정하되, 개선 일정·약속("곧 고치겠습니다" 등)은 하지 마라.
6. 리뷰 왜곡 금지: 리뷰가 말하지 않은 칭찬을 지어내지 마라.

# [threads] 스레드 글
- 독자: 20~30대. 짧은 문장과 줄바꿈 위주, 친구에게 말하듯 편한 반말.
- 리뷰에서 인상적인 부분을 자연스럽게 녹여서 소개.
- 마지막 줄은 댓글을 유도하는 질문 한 줄로 끝낸다.
- 해시태그 절대 금지 ('#' 문자 사용 금지). 이모지 0~2개. 공백 포함 350자 이내.

# [instagram] 인스타그램 캡션
- 독자: 20~30대. 친근하되 스레드보다 살짝 차분·감성적.
- 리뷰의 실제 구절을 1개 이상 따옴표로 인용한다 (예: "○○○"라는 후기).
- 첫 줄(125자 이내)은 스크롤을 멈추게 하는 훅.
- 마지막 줄에 해시태그 6~9개, #홍삼빌호텔 반드시 포함. 전체 공백 포함 1000자 이내.

# [facebook] 페이스북 글
- 독자: 40~60대. 정중한 존댓말 완결 문장("~합니다 / ~입니다 / ~드립니다"), 문단형.
- 한 문단 2~3문장, 문단 사이 빈 줄 1개. 인터넷 신조어·반말 금지.
- {REVIEWER_LABEL} 께서 남겨 주신 후기라는 점이 자연스럽게 드러나게.
- 마지막 문단은 방문 권유로 마무리 (예약번호 {RESERVATION_NO} 언급 가능).
- 해시태그 절대 금지 ('#' 문자 사용 금지). 이모지 최대 2개. 공백 포함 250~500자.

# 출력 형식 (코드가 파싱한다. 이 JSON 하나만 출력하고 다른 말·코드블록 표시를 덧붙이지 마라)
{{"threads": "스레드 본문 (줄바꿈은 \\n)", "instagram": "인스타 캡션", "facebook": "페이스북 본문"}}
- 설명·인사·사고 과정을 앞뒤에 붙이지 말고, 여는 중괄호로 시작해 닫는 중괄호로 끝내라."""


def call_claude(system, user):
    """Claude 호출. (본문 텍스트, stop_reason) — facebook_auto_post.py 와 동일 패턴"""
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()

    data = None
    for net_try in (1, 2):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                data = json.loads(res.read().decode())
            break
        except Exception as e:
            print(f"⚠️ Claude 호출 실패({net_try}/2): {e}")
            if net_try == 2:
                return "", "api_error"
            time.sleep(5)

    blocks = data.get("content", []) if data else []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    stop = (data or {}).get("stop_reason")
    if not text:
        print(f"⚠️ Claude 응답에 본문이 없음 (stop_reason={stop})")
    elif stop == "max_tokens":
        print(f"⚠️ 응답이 max_tokens({MAX_TOKENS})에서 잘렸습니다. 복구를 시도합니다.")
    return text, stop


def _repair_truncated_json(fragment):
    """중간에 잘린 JSON 을 닫아서 읽어 본다. 실패하면 None. (facebook 스크립트와 동일)"""
    s = fragment.find("{")
    if s < 0:
        return None
    frag = fragment[s:].rstrip()
    while frag.endswith("\\"):
        frag = frag[:-1]
    in_str, esc, depth = False, False, 0
    for ch in frag:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
    if in_str:
        frag += '"'
    if depth > 0:
        frag += "}" * depth
    try:
        return json.loads(frag, strict=False)
    except Exception:
        return None


def parse_json(text):
    if not text:
        raise ValueError("응답이 비어 있음")
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(t[s:e + 1], strict=False)
        except Exception:
            pass
    repaired = _repair_truncated_json(t)
    if repaired is not None:
        print("🩹 잘린 JSON 을 복구해서 읽었습니다.")
        return repaired
    raise ValueError("JSON 을 찾을 수 없음")


def _common_problems(name, text):
    problems = []
    if not text or not text.strip():
        return [f"{name}: 본문이 비어 있음"]
    for w in HEALTH_WORDS:
        if w in text:
            problems.append(f"{name}: 효능 표현 '{w}'")
    for w in FOOD_SALE_WORDS:
        if w in text:
            problems.append(f"{name}: 식사 제공 표현 '{w}'")
    for w in FABRICATION_WORDS:
        if w in text and w not in REVIEW_TEXT:
            problems.append(f"{name}: 리뷰에 없는 표현 '{w}' (가격·할인·재개장 생성 금지)")
    return problems


def validate(posts):
    """3개 글이 규칙을 지켰는지 검사. 통과하면 빈 리스트."""
    problems = []

    th = posts.get("threads", "")
    problems += _common_problems("threads", th)
    if th:
        if "#" in th:
            problems.append("threads: 해시태그('#') 사용 금지")
        if len(th) > 480:
            problems.append(f"threads: 너무 김 ({len(th)}자, 350자 이내로)")
        lines = [l for l in th.strip().splitlines() if l.strip()]
        if lines and "?" not in lines[-1]:
            problems.append("threads: 마지막 줄이 댓글 유도 질문(물음표)이 아님")

    ig = posts.get("instagram", "")
    problems += _common_problems("instagram", ig)
    if ig:
        tags = re.findall(r"#[^\s#]+", ig)
        if not (6 <= len(tags) <= 9):
            problems.append(f"instagram: 해시태그 {len(tags)}개 (6~9개여야 함)")
        if "#홍삼빌호텔" not in tags:
            problems.append("instagram: #홍삼빌호텔 누락")
        if '"' not in ig and "'" not in ig and "“" not in ig:
            problems.append("instagram: 리뷰 구절 인용(따옴표) 없음")
        if len(ig) > 1100:
            problems.append(f"instagram: 너무 김 ({len(ig)}자, 1000자 이내로)")

    fb = posts.get("facebook", "")
    problems += _common_problems("facebook", fb)
    if fb:
        if "#" in fb:
            problems.append("facebook: 해시태그('#') 사용 금지")
        if not (200 <= len(fb) <= 600):
            problems.append(f"facebook: 본문 길이 {len(fb)}자 (250~500자 안으로)")
        if fb.count("니다") + fb.count("세요") < 2:
            problems.append("facebook: 정중한 존댓말 완결 문장으로")

    return problems


def generate_posts(stars):
    stars_line = f"별점: {stars}점\n" if stars is not None else ""
    base_user = f"""아래 구글 리뷰 1건으로 스레드·인스타그램·페이스북 게시글 3개를 JSON 으로만 출력하세요.

작성자 표기(reviewer_label): {REVIEWER_LABEL}
{stars_line}리뷰 본문:
\"\"\"{REVIEW_TEXT}\"\"\""""

    feedback = ""
    last_problems = []
    for attempt in range(1, MAX_RETRY + 1):
        text, stop = call_claude(SYSTEM_PROMPT, base_user + feedback)
        try:
            posts = parse_json(text) if text else {}
        except Exception as e:
            posts = {}
            print(f"⚠️ JSON 파싱 실패: {e}\n{text[:300]}")

        problems = validate(posts)
        if not problems:
            print(f"✅ 생성 {attempt}회차 검증 통과")
            return posts

        last_problems = problems
        print(f"⚠️ {attempt}회차 검증 실패: {problems}")
        # 매번 원본 요청 + 이번 지적사항만 붙인다 (요청문이 계속 길어지지 않도록)
        feedback = "\n\n[재작성 요청] 다음 문제를 고쳐서 JSON 만 다시 출력: " + "; ".join(problems)
        if stop == "max_tokens" or not text:
            feedback += "\n글이 길어 잘렸습니다. 각 본문을 더 짧게 쓰고, 설명 없이 여는 중괄호로 시작해 닫는 중괄호로 끝내세요."
        time.sleep(2)

    raise RuntimeError(f"게시글 생성 {MAX_RETRY}회 실패, 게시하지 않음: {last_problems}")


# ─────────────────────────────────────────────
# 2. images/ 랜덤 3장 → JPG 변환 → 캐시 커밋 → 공개 URL
# ─────────────────────────────────────────────
def to_sns_jpg(raw_bytes):
    """어떤 형식이든 → 인스타 규격 JPG (3개 플랫폼 공용. instagram_auto_post.py 와 동일)"""
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
        new_h = int(w / MIN_RATIO)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif ratio > MAX_RATIO:
        new_w = int(h * MAX_RATIO)
        left = (w - new_w) // 2
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
    """변환된 JPG 를 커밋·푸시해야 raw.githubusercontent URL 이 살아남 (기존과 동일)"""
    def git(*args):
        subprocess.run(["git", *args], check=True)
    git("config", "user.name", "auto-post-bot")
    git("config", "user.email", "bot@users.noreply.github.com")
    git("add", "-A", CACHE_DIR)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        git("commit", "-m", f"review_sns_cache: {', '.join(names)}")
        git("pull", "--rebase", "origin", BRANCH)
        git("push", "origin", f"HEAD:{BRANCH}")
    time.sleep(10)   # raw URL 반영 대기


def pick_images():
    files = [
        f for f in os.listdir(IMAGE_DIR)
        if os.path.splitext(f)[1].lower() in IMAGE_EXTS
    ]
    if len(files) < IMAGE_COUNT:
        raise RuntimeError(f"images/ 폴더에 이미지가 {len(files)}장뿐입니다 (최소 {IMAGE_COUNT}장).")
    chosen = random.sample(files, IMAGE_COUNT)

    os.makedirs(CACHE_DIR, exist_ok=True)
    prune_cache()
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    names, urls = [], []
    for i, fname in enumerate(chosen, 1):
        with open(os.path.join(IMAGE_DIR, fname), "rb") as fp:
            raw = fp.read()
        jpg = to_sns_jpg(raw)
        name = f"{stamp}_{i}.jpg"
        with open(os.path.join(CACHE_DIR, name), "wb") as fp:
            fp.write(jpg)
        names.append(name)
        urls.append(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{CACHE_DIR}/{name}")
        print(f"   {fname} → {name} ({len(jpg)//1024} KB)")

    git_push_cache(names)
    return chosen, urls


# ─────────────────────────────────────────────
# 3. 플랫폼별 게시 (기존 스크립트와 동일 방식)
# ─────────────────────────────────────────────
def post_to_threads(text, image_urls):
    token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]
    child_ids = []
    for url in image_urls:
        res = http_json(f"{THREADS_API}/{user_id}/threads", {
            "media_type": "IMAGE",
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": token,
        })
        child_ids.append(res["id"])
        time.sleep(3)

    res = http_json(f"{THREADS_API}/{user_id}/threads", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "text": text,
        "access_token": token,
    })
    creation_id = res["id"]

    time.sleep(35)
    res = http_json(f"{THREADS_API}/{user_id}/threads_publish", {
        "creation_id": creation_id,
        "access_token": token,
    })
    return res["id"]


def wait_ig_container(container_id, token, max_wait=180):
    waited = 0
    while waited < max_wait:
        res = http_json(f"{IG_API}/{container_id}?fields=status_code,status&access_token={token}")
        code = res.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"컨테이너 처리 실패: {res}")
        time.sleep(5)
        waited += 5
    raise RuntimeError("컨테이너 처리 시간 초과 (이미지 용량/비율 확인 필요)")


def post_to_instagram(caption, image_urls):
    token = os.environ["INSTAGRAM_ACCESS_TOKEN"]
    user_id = os.environ["INSTAGRAM_USER_ID"]
    child_ids = []
    for url in image_urls:
        res = http_json(f"{IG_API}/{user_id}/media", {
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": token,
        })
        child_ids.append(res["id"])
        time.sleep(2)

    for cid in child_ids:
        wait_ig_container(cid, token)

    res = http_json(f"{IG_API}/{user_id}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "caption": caption,
        "access_token": token,
    })
    creation_id = res["id"]
    wait_ig_container(creation_id, token)

    res = http_json(f"{IG_API}/{user_id}/media_publish", {
        "creation_id": creation_id,
        "access_token": token,
    })
    return res["id"]


def post_to_facebook(text, image_urls):
    token = os.environ["FB_PAGE_ACCESS_TOKEN"]
    page_id = os.environ["FB_PAGE_ID"]
    # 페이지 토큰으로 /me 를 조회해 진짜 페이지 ID 확정 (Secrets 값이 틀려도 동작)
    try:
        me = http_json(f"{FB_API}/me?fields=id,name&access_token={token}")
        real_id = str(me.get("id", ""))
        if real_id and real_id != str(page_id):
            print(f"🔁 FB_PAGE_ID({page_id}) → 토큰의 실제 페이지 ID {real_id} ({me.get('name')}) 사용")
            page_id = real_id
    except Exception as e:
        print(f"⚠️ /me 조회 실패, Secrets 의 FB_PAGE_ID 사용: {e}")

    photo_ids = []
    for url in image_urls:
        res = http_json(f"{FB_API}/{page_id}/photos", {
            "url": url, "published": "false", "access_token": token,
        })
        photo_ids.append(res["id"])
        time.sleep(2)
    data = {"message": text, "access_token": token}
    for i, pid in enumerate(photo_ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid})
    res = http_json(f"{FB_API}/{page_id}/feed", data)
    return res["id"]


def post_fb_first_comment(post_id):
    """게시 직후 첫 댓글로 예약번호·직통상담 등록"""
    token = os.environ["FB_PAGE_ACCESS_TOKEN"]
    try:
        res = http_json(f"{FB_API}/{post_id}/comments", {
            "message": FB_FIRST_COMMENT, "access_token": token,
        })
        print(f"💬 첫 댓글 등록 완료: {res.get('id')}")
        return res.get("id")
    except Exception as e:
        print(f"⚠️ 첫 댓글 등록 실패 (게시는 완료됨): {e}")
        print("   → pages_manage_engagement 권한이 필요할 수 있습니다.")
        return None


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    platforms = resolve_platforms()
    stars = parse_stars()
    print(f"📌 대상 플랫폼: {platforms} / 작성자 표기: {REVIEWER_LABEL} / 별점: {stars if stars is not None else '(미입력)'}")

    # 안전장치: 별점·본문 길이 미달이면 게시하지 않고 정상 종료
    if not REVIEW_TEXT or len(REVIEW_TEXT) < MIN_REVIEW_LEN:
        print(f"⏭️ 리뷰 본문이 {MIN_REVIEW_LEN}자 미만이라 게시하지 않습니다. (현재 {len(REVIEW_TEXT)}자)")
        return
    if stars is not None and stars < MIN_STARS:
        print(f"⏭️ 별점 {stars}점 (< {MIN_STARS}) 이라 게시하지 않습니다.")
        return

    # 1) 중복 검사 (해시 + 플랫폼 조합) — 이미 성공 게시한 플랫폼만 건너뛴다
    h = review_hash(REVIEW_TEXT)
    log = load_log()
    done = already_posted_platforms(log, h)
    skipped = [p for p in platforms if p in done]
    platforms = [p for p in platforms if p not in done]
    if skipped:
        print(f"⏭️ 이 리뷰로 이미 게시된 플랫폼 건너뜀: {skipped}")
    if not platforms:
        print("⏭️ 요청한 모든 플랫폼에 이미 게시된 리뷰입니다 — 중단합니다.")
        return
    print(f"▶️ 이번에 게시할 플랫폼: {platforms}")

    # 2) Claude 1회 호출로 3개 글 생성 (불합격 시 재생성)
    posts = generate_posts(stars)
    for name in ("threads", "instagram", "facebook"):
        print(f"\n✍️ [{name}] ({len(posts.get(name, ''))}자)\n{posts.get(name, '')}")

    # 3) 이미지 3장 → JPG → 공개 URL
    print("\n🖼️ images/ 에서 이미지 선택·변환 중...")
    chosen, urls = pick_images()
    print(f"🖼️ 선택된 이미지: {chosen}")

    # 4) 플랫폼별 게시 — 한 플랫폼이 실패해도 나머지는 계속
    results = {}
    posters = {
        "threads": lambda: post_to_threads(posts["threads"], urls),
        "instagram": lambda: post_to_instagram(posts["instagram"], urls),
        "facebook": lambda: post_to_facebook(posts["facebook"], urls),
    }
    for name in platforms:
        try:
            post_id = posters[name]()
            results[name] = {"ok": True, "post_id": post_id}
            print(f"🚀 [{name}] 게시 완료! post id = {post_id}")
            if name == "facebook":
                time.sleep(5)
                comment_id = post_fb_first_comment(post_id)
                results[name]["comment_id"] = comment_id
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)[:500]}
            print(f"❌ [{name}] 게시 실패 (나머지는 계속 진행): {e}", file=sys.stderr)

    # 5) 로그 기록 (커밋·푸시는 워크플로 마지막 단계가 담당)
    log["count"] += 1
    log["posts"].append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "hash": h,
        "reviewer_label": REVIEWER_LABEL,
        "stars": stars,
        "platforms": platforms,
        "results": results,
        "texts": {k: posts.get(k, "") for k in ("threads", "instagram", "facebook")},
        "images": chosen,
    })
    log["posts"] = log["posts"][-60:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print(f"\n📝 {LOG_FILE} 기록 완료")

    ok = [n for n, r in results.items() if r.get("ok")]
    fail = [n for n, r in results.items() if not r.get("ok")]
    print(f"결과: 성공 {ok or '없음'} / 실패 {fail or '없음'}")
    if platforms and not ok:
        raise RuntimeError("모든 플랫폼 게시에 실패했습니다.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
