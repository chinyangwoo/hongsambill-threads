# -*- coding: utf-8 -*-
"""
홍삼빌호텔 구글 리뷰 → SNS(스레드·인스타그램·페이스북) + 구글 블로그(Blogger) 자동 게시 앱
==========================================================================================
workflow_dispatch 수동 실행, 또는 fetch_google_reviews.py(자동 수집)가 호출.

[블로그] BLOGGER_REFRESH_TOKEN 시크릿이 등록돼 있을 때만 켜진다 (없으면 기존 3개 플랫폼만).
  - AEO/SEO 구조: 직답 요약 → 기본정보 표 → 리뷰 원문 → 확인된 사실 목록 → FAQ → 문의
  - JSON-LD(@graph: BlogPosting + Hotel + FAQPage) 삽입. 자기 리뷰 별점 마크업은 넣지 않음
    (구글 정책상 자기 업체 리뷰 별점은 리치결과 대상 아님 → 불이익 방지)
  - 영문 슬러그 제목으로 먼저 발행 → 한국어 제목으로 수정 (Blogger 주소가 blog-post_12.html 이 되는 것 방지)
  - 블로그 사진은 images/ 원본을 jsDelivr CDN 주소로 연결 (캐시 폴더는 3일 뒤 지워지므로)

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
  BLOGGER_REFRESH_TOKEN                          (블로그 — 있으면 블로그 게시 켜짐)
  GBP_CLIENT_ID, GBP_CLIENT_SECRET               (블로그 토큰 발급에 쓴 OAuth 클라이언트)
  BLOGGER_BLOG_ID                                (선택 — 블로그가 여러 개일 때만)

자동 수집 스크립트가 추가로 넘겨주는 값 (수동 실행 땐 없어도 됨):
  REVIEW_DATE (리뷰 작성일), GBP_AVG_RATING, GBP_REVIEW_COUNT, HOTEL_ADDRESS

워크플로 입력 → 환경변수:
  REVIEW_TEXT     : 구글 리뷰 본문 (필수)
  REVIEWER_LABEL  : 작성자 표기 (기본 "한 손님")
  PLATFORMS       : all / threads / instagram / facebook / blogger (기본 all)
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
# 블로그 글은 항목이 많고, 외국어 리뷰는 번역(review_ko)까지 들어가 4000 에서 잘림 → 별도 한도
BLOG_MAX_TOKENS = int(os.environ.get("CLAUDE_BLOG_MAX_TOKENS", "16000"))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

REVIEW_TEXT = os.environ.get("REVIEW_TEXT", "").strip()
REVIEWER_LABEL = os.environ.get("REVIEWER_LABEL", "").strip() or "한 손님"
PLATFORMS_INPUT = (os.environ.get("PLATFORMS", "all").strip().lower() or "all")
STARS_RAW = os.environ.get("STARS", "").strip()

BLOGGER_API = "https://www.googleapis.com/blogger/v3"
BLOGGER_REFRESH_TOKEN = os.environ.get("BLOGGER_REFRESH_TOKEN", "").strip()
BLOGGER_ENABLED = bool(BLOGGER_REFRESH_TOKEN)
BLOG_IMAGE_BASE = f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/{IMAGE_DIR}/"

REVIEW_DATE = os.environ.get("REVIEW_DATE", "").strip()          # 예: 2026-09-08
GBP_AVG_RATING = os.environ.get("GBP_AVG_RATING", "").strip()    # 예: 4.6
GBP_REVIEW_COUNT = os.environ.get("GBP_REVIEW_COUNT", "").strip()
HOTEL_ADDRESS = os.environ.get("HOTEL_ADDRESS", "").strip()

RESERVATION_NO = "1661-3889"
DIRECT_NO = "010-8545-0290"
FB_FIRST_COMMENT = f"홍삼빌호텔 예약번호 {RESERVATION_NO}, 직통상담문의 {DIRECT_NO}"

# 검증용 금지 표현 (facebook_auto_post.py 와 동일 계열)
HEALTH_WORDS = ["면역", "효능", "건강에 좋", "피로 회복에 좋", "몸에 좋"]
FOOD_SALE_WORDS = ["고기를 제공", "식사를 제공", "고기 판매", "고기를 준비해 드", "고기 준비해 드", "식사 제공"]
FABRICATION_WORDS = ["할인", "이벤트 진행", "재개장", "리뉴얼 오픈", "오픈 예정", "특가"]

HOTEL_INFO = """- 홍삼빌호텔: 전북 진안군, 마이산 인근의 3성급 호텔. 객실 40개. 가족 단위 방문객이 많음.
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
        return ["threads", "instagram", "facebook"] + (["blogger"] if BLOGGER_ENABLED else [])
    if PLATFORMS_INPUT in ("threads", "instagram", "facebook"):
        return [PLATFORMS_INPUT]
    if PLATFORMS_INPUT == "blogger":
        if not BLOGGER_ENABLED:
            raise RuntimeError("BLOGGER_REFRESH_TOKEN 시크릿이 없어 블로그 게시를 할 수 없습니다.")
        return ["blogger"]
    raise RuntimeError(f"platforms 값이 잘못됨: {PLATFORMS_INPUT} (all/threads/instagram/facebook/blogger)")


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


def call_claude(system, user, max_tokens=None):
    """Claude 호출. (본문 텍스트, stop_reason) — facebook_auto_post.py 와 동일 패턴"""
    max_tokens = max_tokens or MAX_TOKENS
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
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
            with urllib.request.urlopen(req, timeout=300) as res:
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
        print(f"⚠️ 응답이 max_tokens({max_tokens})에서 잘렸습니다. 복구를 시도합니다.")
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
        # GitHub 일시 오류(500 등)로 push 가 실패하면 전체 게시가 멈추므로 최대 4회 재시도
        for push_try in range(1, 5):
            try:
                git("pull", "--rebase", "origin", BRANCH)
                git("push", "origin", f"HEAD:{BRANCH}")
                break
            except subprocess.CalledProcessError as e:
                if push_try == 4:
                    raise
                wait = 15 * push_try
                print(f"⚠️ git push 실패({push_try}/4): {e} — {wait}초 후 재시도")
                time.sleep(wait)
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
# 4. 구글 블로그(Blogger) — AEO/SEO 최적화 글
# ─────────────────────────────────────────────
BLOG_SYSTEM = f"""# 역할
너는 홍삼빌호텔 공식 블로그 편집자다. 실제 구글 리뷰 1건을 바탕으로,
검색엔진(SEO)과 ChatGPT·Gemini·Claude·Grok 같은 AI 답변엔진(AEO)이 '출처로 인용하기 좋은' 글의 재료를 만든다.
AI가 인용하는 글의 조건: 질문에 첫 문장이 바로 답하고, 사실이 짧은 문장으로 분리돼 있고, 근거가 분명하다.
사람이 검수하지 않고 그대로 발행된다.

# 호텔 정보 (이 범위 안에서만 쓴다)
{HOTEL_INFO}

# 절대 규칙
1. 리뷰 작성자 실명·아이디 금지. 작성자는 지정된 표기(reviewer_label)로만 지칭.
2. 리뷰와 [호텔 정보]에 없는 사실·수치·가격·할인·이벤트·거리·소요시간·재개장 시점을 만들지 마라.
3. 홍삼 효능 주장 금지. 호텔이 고기·음식을 판매·제공한다는 표현 금지.
4. 리뷰가 말하지 않은 칭찬을 지어내지 마라. 아쉬운 점이 있으면 그대로 적되 개선 약속은 하지 마라.
5. 과장 수식어(최고의, 완벽한, 압도적인 등) 금지. 담백한 설명문, 존댓말(~합니다).
6. 리뷰가 한국어가 아니면 review_ko 에 자연스러운 한국어 번역을 넣는다. 한국어 리뷰면 빈 문자열.

# 작성 항목
- slug: 영문 소문자·하이픈 3~6단어 (예: jinan-maisan-family-hotel-review). 리뷰 핵심 반영.
- title: 한국어 25~45자. '홍삼빌호텔' 필수 + '진안' 또는 '마이산' 포함 + 리뷰 핵심 포인트.
  사람들이 실제로 검색할 법한 표현 (예: "진안 마이산 가족여행 숙소, 홍삼빌호텔 투숙 후기 — 넓은 객실과 세탁실")
- summary: 2~3문장. 첫 문장에서 핵심을 바로 말한다 (예: "홍삼빌호텔은 평일에 조용하게 쉬기 좋았다는 가족 여행객의 후기입니다.").
  "이 후기의 결론은", "요약하자면" 같은 메타 표현으로 시작하지 마라. 누가(reviewer_label) 어떤 여행으로 묵었고 무엇이 좋았/아쉬웠는지.
- highlights: 리뷰에서 확인되는 구체적 사실 3~6개. 각 1문장, 주어가 분명한 평서문.
- good_for: 이 후기가 특히 참고될 여행자 유형 1~2문장 (리뷰 근거로만).
- faq: 3~4개. 질문(q)은 사람들이 AI·검색창에 실제로 묻는 형태
  (예: "진안 마이산 근처에 가족이 묵기 좋은 호텔이 있나요?", "홍삼빌호텔에 세탁실이 있나요?").
  답(a)은 2~3문장, 첫 문장에서 바로 답하고 근거가 '투숙객 후기'인지 '호텔 정보'인지 드러낸다.
  리뷰·호텔정보로 답할 수 없는 질문은 만들지 마라.
- labels: 블로그 라벨 4~6개 (예: 홍삼빌호텔, 진안숙소, 마이산숙소, 가족여행, 구글리뷰). '#' 없이.

# 출력 (JSON 하나만, 앞뒤 설명·코드블록 금지)
{{"slug":"...","title":"...","summary":"...","highlights":["..."],"good_for":"...",
"faq":[{{"q":"...","a":"..."}}],"labels":["..."],"review_ko":""}}"""


def validate_blog(b):
    problems = []
    if not isinstance(b, dict) or not b:
        return ["blog: JSON 없음"]
    title = b.get("title", "")
    if "홍삼빌호텔" not in title:
        problems.append("blog: 제목에 '홍삼빌호텔' 누락")
    if not ("진안" in title or "마이산" in title):
        problems.append("blog: 제목에 '진안' 또는 '마이산' 누락")
    if not (15 <= len(title) <= 60):
        problems.append(f"blog: 제목 길이 {len(title)}자 (25~45자)")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+){1,7}", b.get("slug", "")):
        problems.append("blog: slug 는 영문 소문자·하이픈 3~6단어")
    if len(b.get("summary", "")) < 40:
        problems.append("blog: summary 너무 짧음")
    if re.match(r"\s*(이 후기의 결론|요약하자면|결론적으로|이 글은)", b.get("summary", "")):
        problems.append("blog: summary 를 '이 후기의 결론은' 같은 메타 표현으로 시작하지 말 것")
    hl = b.get("highlights") or []
    if not (3 <= len(hl) <= 6):
        problems.append(f"blog: highlights {len(hl)}개 (3~6개)")
    faq = b.get("faq") or []
    if not (3 <= len(faq) <= 4) or any(not (f.get("q") and f.get("a")) for f in faq if isinstance(f, dict)):
        problems.append("blog: faq 는 q·a 가 있는 3~4개")
    labels = b.get("labels") or []
    if not (3 <= len(labels) <= 8):
        problems.append("blog: labels 4~6개")
    all_text = " ".join([title, b.get("summary", ""), b.get("good_for", ""), " ".join(hl)]
                        + [f"{f.get('q', '')} {f.get('a', '')}" for f in faq if isinstance(f, dict)])
    problems += [p.replace("blog_all", "blog") for p in _common_problems("blog_all", all_text)]
    for w in ("최고의", "완벽한", "압도적"):
        if w in all_text and w not in REVIEW_TEXT:
            problems.append(f"blog: 과장 표현 '{w}'")
    return problems


def generate_blog(stars):
    stars_line = f"별점: {stars}점\n" if stars is not None else ""
    date_line = f"리뷰 작성일: {REVIEW_DATE}\n" if REVIEW_DATE else ""
    base_user = f"""아래 구글 리뷰 1건으로 블로그 글 재료를 JSON 으로만 출력하세요.

작성자 표기(reviewer_label): {REVIEWER_LABEL}
{stars_line}{date_line}리뷰 본문:
\"\"\"{REVIEW_TEXT}\"\"\""""
    feedback, last = "", []
    for attempt in range(1, MAX_RETRY + 1):
        text, stop = call_claude(BLOG_SYSTEM, base_user + feedback, max_tokens=BLOG_MAX_TOKENS)
        try:
            b = parse_json(text) if text else {}
        except Exception as e:
            b = {}
            print(f"⚠️ 블로그 JSON 파싱 실패: {e}")
        problems = validate_blog(b)
        if not problems:
            print(f"✅ 블로그 글 {attempt}회차 검증 통과")
            return b
        last = problems
        print(f"⚠️ 블로그 {attempt}회차 검증 실패: {problems}")
        feedback = "\n\n[재작성 요청] 다음 문제를 고쳐서 JSON 만 다시 출력: " + "; ".join(problems)
        if stop == "max_tokens" or not text:
            feedback += "\n글이 길어 잘렸습니다. 각 항목을 짧게 쓰고, 설명 없이 여는 중괄호로 시작해 닫는 중괄호로 끝내세요."
        time.sleep(2)
    raise RuntimeError(f"블로그 글 생성 {MAX_RETRY}회 실패: {last}")


def _e(t):
    """HTML 이스케이프"""
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _date_ko(d):
    try:
        y, m, dd = d.split("-")[:3]
        return f"{int(y)}년 {int(m)}월 {int(dd)}일"
    except Exception:
        return d


def build_blog_html(b, stars, image_names):
    now = datetime.now(KST)
    today_ko = f"{now.year}년 {now.month}월 {now.day}일"
    star_txt = f"★{stars:g}" if stars is not None else ""
    src_parts = ["구글 리뷰"]
    if REVIEW_DATE:
        src_parts.append(f"{_date_ko(REVIEW_DATE)} 작성")
    if star_txt:
        src_parts.append(f"별점 {star_txt}")

    rows = [
        ("숙소명", "홍삼빌호텔 (Hongsambill Hotel)"),
        ("위치", HOTEL_ADDRESS or "전북 진안군, 마이산 인근"),
        ("등급·규모", "3성급 호텔, 객실 40개"),
        ("주요 시설", "2층 약 50평 테라스(산 조망), 바베큐장(장소·설비 제공)"),
        ("예약 문의", f"대표번호 {RESERVATION_NO} / 직통 상담 {DIRECT_NO}"),
    ]
    if GBP_AVG_RATING and GBP_REVIEW_COUNT:
        rows.append(("구글 평점", f"{GBP_AVG_RATING}점 (리뷰 {GBP_REVIEW_COUNT}개, {today_ko} 기준)"))
    table = "".join(
        f'<tr><th style="text-align:left;padding:6px 10px;border:1px solid #ddd;background:#f7f7f7;white-space:nowrap">{_e(k)}</th>'
        f'<td style="padding:6px 10px;border:1px solid #ddd">{_e(v)}</td></tr>' for k, v in rows)

    imgs = "".join(
        f'<p style="text-align:center"><img src="{BLOG_IMAGE_BASE}{urllib.parse.quote(n)}" '
        f'alt="진안 마이산 홍삼빌호텔 사진 {i}" loading="lazy" style="max-width:100%;height:auto"/></p>'
        for i, n in enumerate(image_names, 1))

    review_block = f'<blockquote style="border-left:4px solid #b33;margin:0;padding:8px 14px;background:#fafafa">{_e(REVIEW_TEXT).replace(chr(10), "<br/>")}</blockquote>'
    if b.get("review_ko"):
        review_block += f'<p><strong>한국어 번역:</strong> {_e(b["review_ko"]).replace(chr(10), "<br/>")}</p>'

    faq = [f for f in b["faq"] if isinstance(f, dict)]
    faq_html = "".join(f"<h3>{_e(f['q'])}</h3><p>{_e(f['a'])}</p>" for f in faq)
    hl_html = "".join(f"<li>{_e(h)}</li>" for h in b["highlights"])

    address = {"@type": "PostalAddress", "addressLocality": "진안군",
               "addressRegion": "전북특별자치도", "addressCountry": "KR"}
    if HOTEL_ADDRESS:
        address["streetAddress"] = HOTEL_ADDRESS
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "BlogPosting", "headline": b["title"], "description": b["summary"],
             "inLanguage": "ko-KR", "datePublished": now.isoformat(timespec="seconds"),
             "dateModified": now.isoformat(timespec="seconds"),
             "image": [f"{BLOG_IMAGE_BASE}{urllib.parse.quote(n)}" for n in image_names],
             "author": {"@type": "Organization", "name": "홍삼빌호텔"},
             "publisher": {"@type": "Organization", "name": "홍삼빌호텔"},
             "about": {"@id": "#hongsambill-hotel"},
             "keywords": ", ".join(b["labels"])},
            {"@type": "Hotel", "@id": "#hongsambill-hotel", "name": "홍삼빌호텔",
             "alternateName": "Hongsambill Hotel", "telephone": "+82-1661-3889",
             "numberOfRooms": 40, "starRating": {"@type": "Rating", "ratingValue": "3"},
             "address": address,
             "containedInPlace": {"@type": "Place", "name": "마이산 도립공원 인근"}},
            {"@type": "FAQPage", "mainEntity": [
                {"@type": "Question", "name": f["q"],
                 "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in faq]},
        ],
    }
    ld_json = json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")

    return f"""<p><em>이 글은 홍삼빌호텔에 실제로 남겨진 {_e(" · ".join(src_parts))}를 바탕으로 작성했습니다. (게시일 {today_ko})</em></p>
<h2>한 줄 요약</h2>
<p>{_e(b["summary"])}</p>
<h2>홍삼빌호텔 기본 정보</h2>
<table style="border-collapse:collapse;width:100%;font-size:95%">{table}</table>
{imgs}
<h2>투숙객이 남긴 구글 리뷰 원문</h2>
<p>{_e(REVIEWER_LABEL)}께서 남겨 주신 후기입니다{(" (" + star_txt + ")") if star_txt else ""}.</p>
{review_block}
<h2>이 후기에서 확인할 수 있는 점</h2>
<ul>{hl_html}</ul>
<h2>이런 분께 참고가 됩니다</h2>
<p>{_e(b.get("good_for", ""))}</p>
<h2>홍삼빌호텔 자주 묻는 질문</h2>
{faq_html}
<h2>예약 문의</h2>
<p>홍삼빌호텔 예약 대표번호 <strong>{RESERVATION_NO}</strong>, 직통 상담 <strong>{DIRECT_NO}</strong>로 문의하실 수 있습니다.</p>
<script type="application/ld+json">{ld_json}</script>"""


def _json_req(url, token, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} 오류: {url.split('?')[0]}\n응답: {body}") from e


def blogger_token():
    res = http_json("https://oauth2.googleapis.com/token", {
        "client_id": os.environ.get("BLOGGER_CLIENT_ID") or os.environ["GBP_CLIENT_ID"],
        "client_secret": os.environ.get("BLOGGER_CLIENT_SECRET") or os.environ["GBP_CLIENT_SECRET"],
        "refresh_token": BLOGGER_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    })
    return res["access_token"]


def blogger_blog_id(token):
    bid = os.environ.get("BLOGGER_BLOG_ID", "").strip()
    if bid:
        return bid
    blogs = _json_req(f"{BLOGGER_API}/users/self/blogs", token).get("items", [])
    if len(blogs) == 1:
        print(f"📝 블로그: {blogs[0].get('name')} ({blogs[0].get('url')})")
        return blogs[0]["id"]
    listing = ", ".join(f"{b.get('name')}={b['id']}" for b in blogs) or "없음"
    raise RuntimeError(f"블로그를 하나로 정할 수 없습니다. BLOGGER_BLOG_ID 시크릿을 지정하세요. (목록: {listing})")


def post_to_blogger(b, stars, image_names):
    token = blogger_token()
    blog_id = blogger_blog_id(token)
    html = build_blog_html(b, stars, image_names)
    # ① 영문 슬러그 제목으로 발행 → 주소(URL)가 영문으로 고정됨
    slug_title = b["slug"].replace("-", " ")
    res = _json_req(f"{BLOGGER_API}/blogs/{blog_id}/posts?isDraft=false", token,
                    {"title": slug_title, "content": html, "labels": b["labels"]}, "POST")
    post_id, url = res["id"], res.get("url")
    # ② 한국어 제목으로 교체 (주소는 그대로 유지)
    try:
        time.sleep(3)
        _json_req(f"{BLOGGER_API}/blogs/{blog_id}/posts/{post_id}", token,
                  {"title": b["title"]}, "PATCH")
    except Exception as e:
        print(f"⚠️ 블로그 제목 한국어 교체 실패 (영문 제목으로 게시됨): {e}")
    print(f"🔗 블로그 주소: {url}")
    return post_id, url


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

    # 2) Claude 1회 호출로 SNS 3개 글 생성 (불합격 시 재생성) — SNS 플랫폼이 남아 있을 때만
    posts = {}
    if any(p in platforms for p in ("threads", "instagram", "facebook")):
        posts = generate_posts(stars)
        for name in ("threads", "instagram", "facebook"):
            print(f"\n✍️ [{name}] ({len(posts.get(name, ''))}자)\n{posts.get(name, '')}")

    # 2-1) 블로그 글은 형식이 달라 별도 호출로 생성
    blog = None
    if "blogger" in platforms:
        try:
            blog = generate_blog(stars)
            print(f"\n✍️ [blogger] {blog['title']}\n{blog['summary']}")
        except Exception as e:
            print(f"❌ 블로그 글 생성 실패 (나머지는 계속 진행): {e}", file=sys.stderr)

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

    def _blogger():
        if blog is None:
            raise RuntimeError("블로그 글 생성 실패로 게시하지 않음")
        pid, url = post_to_blogger(blog, stars, chosen)
        results_extra["blogger_url"] = url
        return pid
    posters["blogger"] = _blogger
    results_extra = {}

    for name in platforms:
        try:
            post_id = posters[name]()
            results[name] = {"ok": True, "post_id": post_id}
            if name == "blogger":
                results[name]["url"] = results_extra.get("blogger_url")
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
        "texts": {**{k: posts.get(k, "") for k in ("threads", "instagram", "facebook")},
                  **({"blogger_title": blog["title"], "blogger_summary": blog["summary"]} if blog else {})},
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
