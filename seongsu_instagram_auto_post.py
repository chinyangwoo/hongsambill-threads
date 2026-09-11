# -*- coding: utf-8 -*-
"""
성수주조장 인스타그램 완전 자동 포스팅 앱 (딸기막걸리 판매 CTA 최우선)
=====================================================================
홍삼빌호텔 instagram_auto_post.py 와 동일한 구조. 파일명·환경변수·캐시 폴더에
'seongsu_' 접두어를 붙여 같은 저장소에 넣어도 충돌하지 않습니다.

동작 순서 (하루 2회, KST 10:47 / 17:47 — 슬롯은 워크플로 SLOT 환경변수로 고정):
  1. 요일별 테마 + 회차 순환으로 이번 글의 주제·CTA 유형 결정
     (구매 CTA 는 최소 2회에 1회 강제, 같은 CTA 연속 금지)
  2. Claude API 로 캡션 생성 (훅 15자 이내 + 본문 + CTA 단독 마지막 줄 + 해시태그 15~20개)
  3. 생성 결과 자동 검수: 문단 정리, 브랜드 해시태그 보충, 30개 초과 제거, 링크 문자열 제거
  4. Google Drive 폴더에서 랜덤 3장 (최근 4회 사용한 사진 제외) → 인스타 규격 JPG 변환
  5. 변환 JPG 를 seongsu_ig_cache/ 에 커밋 → 공개 URL 확보
  6. Instagram API 캐러셀(3장) 게시 → 첫 댓글에 구매 링크 고정 등록
  7. seongsu_instagram_posted_log.json 에 기록 → 다음 회차 중복 방지
  8. 토큰 발급 40일 경과 시 자동 갱신 (60일 만료)

필요한 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY                 : Claude API 키 (홍삼빌과 공용 가능)
  SEONGSU_INSTAGRAM_ACCESS_TOKEN    : 성수주조장 인스타 장기 토큰 (60일, 자동 갱신)
  SEONGSU_INSTAGRAM_USER_ID         : 성수주조장 인스타 비즈니스 계정 ID (숫자)
  GDRIVE_API_KEY                    : Google Drive API 키 (홍삼빌과 공용 가능)
  GH_PAT                            : (선택) 갱신 토큰을 Secrets 에 자동 저장
  GITHUB_REPOSITORY / GITHUB_REF_NAME : Actions 자동 주입

Drive 폴더 ID, 구매 링크, 주제, CTA 풀은 seongsu_instagram_topics.json 에서 관리.
"""

import io
import json
import os
import random
import re
import shutil
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
IG_API = "https://graph.instagram.com/v21.0"
DRIVE_API = "https://www.googleapis.com/drive/v3"
CACHE_DIR = "seongsu_ig_cache"
CACHE_KEEP_DAYS = 3
TOPICS_FILE = "seongsu_instagram_topics.json"
LOG_FILE = "seongsu_instagram_posted_log.json"
TOKEN_FILE = ".seongsu_ig_token_meta.json"
SECRET_NAME = "SEONGSU_INSTAGRAM_ACCESS_TOKEN"

MAX_CAPTION_LEN = 1000          # 인스타 한도 2,200자. 판매 캡션은 짧을수록 좋음 (해시태그 포함)
MAX_HASHTAGS = 30               # 인스타 한도 30개 (초과 시 게시 실패)
LOCAL_IMAGE_DIR = "images_sungsu"    # 저장소 안 사진 폴더 (Drive 폴더와 함께 랜덤 추출)
IMAGE_COUNT = 3                 # 캐러셀 장수
EXCLUDE_RECENT_POSTS = 4        # 최근 N회 게시에 쓴 사진은 이번 회차 제외
MAX_SIDE = 1440
MIN_RATIO, MAX_RATIO = 0.8, 1.91

# 릴스(슬라이드 영상) 설정 — 하루 2회 중 REELS_SLOT 회차는 사진 캐러셀 대신 릴스로 게시
REELS_SLOT = "PM"                 # "AM" | "PM" | "" (빈 문자열이면 릴스 사용 안 함)
REELS_IMAGE_COUNT = 4             # 릴스에 쓸 사진 장수
REELS_SEC_PER_IMAGE = 2.0         # 사진 1장당 노출 초 (4장 × 2초 = 8초)
REELS_W, REELS_H = 1080, 1920     # 릴스 규격 9:16
KST = timezone(timedelta(hours=9))
CLAUDE_MODEL = "claude-sonnet-5"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ACCESS_TOKEN = os.environ["SEONGSU_INSTAGRAM_ACCESS_TOKEN"]
USER_ID = os.environ["SEONGSU_INSTAGRAM_USER_ID"]
GDRIVE_API_KEY = os.environ["GDRIVE_API_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
DRY_RUN = os.environ.get("DRY_RUN") == "1"   # 1 이면 캡션만 생성하고 게시하지 않음


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
# 1. 주제 · 슬롯 · CTA 선택
# ─────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"count": 0, "posts": []}


def load_cfg():
    with open(TOPICS_FILE, encoding="utf-8") as f:
        return json.load(f)


def current_slot():
    """발행 슬롯 결정.
    1순위: 워크플로가 cron별로 넘긴 SLOT 환경변수(AM/PM) — 실행이 지연돼도 슬롯이 안 바뀜
    2순위: 시간 추정(수동 실행 등 SLOT이 없을 때) — 오전(~15시)=AM, 이후=PM
    AM: 밝은 제품컷·정보 / PM: 무드·라이프스타일"""
    env_slot = (os.environ.get("SLOT") or "").strip().upper()
    if env_slot in ("AM", "PM"):
        return env_slot
    return "AM" if datetime.now(KST).hour < 15 else "PM"


def pick_topic(cfg, log):
    topics = cfg["topics"]
    return topics[log["count"] % len(topics)]


def pick_cta(cfg, log):
    """
    CTA 전략 (판매 최우선):
      - 구매 CTA 는 최소 2회에 1회 (직전 글이 구매 CTA 가 아니면 이번은 무조건 구매)
      - 같은 CTA 유형 연속 금지
      - 나머지는 선물DM > 저장 > 태그 > 공유 > 댓글 순 가중치
    """
    pool = cfg["cta_pool"]
    last = log["posts"][-1]["cta_type"] if log["posts"] else None
    if last != "구매":
        cta_type = "구매"
    else:
        weighted = ["선물DM"] * 3 + ["저장"] * 3 + ["태그"] * 2 + ["공유"] * 2 + ["댓글"] * 1
        cta_type = random.choice([c for c in weighted if c != last and c in pool])
    return cta_type, random.choice(pool[cta_type])


# ─────────────────────────────────────────────
# 2. Claude 로 캡션 생성
# ─────────────────────────────────────────────
def call_claude(system, user, max_tokens=4000):
    # claude-sonnet-5 는 답변 전에 생각(thinking) 토큰을 쓰므로 여유를 크게 잡음 (1200 이면 본문이 잘림)
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
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
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        meta = {k: data.get(k) for k in ("stop_reason", "model", "usage")}
        types = [b.get("type") for b in data.get("content", [])]
        print(f"⚠️ Claude 응답이 비어 있음: content types={types} meta={meta}")
    return text


def generate_caption(topic, slot, cta_type, cta_text, cfg, log, fmt="캐러셀"):
    recent = [p["text"] for p in log["posts"][-8:]]
    recent_block = "\n---\n".join(recent) if recent else "(없음)"
    weekday = datetime.now(KST).weekday()
    theme = cfg["weekly_theme"][str(weekday)]
    slot_guide = (
        "오전 발행: 밝고 가벼운 톤, 제품·정보·원료 중심. 점심/주말 계획을 세우는 사람에게."
        if slot == "AM" else
        "저녁 발행: 퇴근 후·홈파티·혼술 무드. 지금 당장 한 잔 하고 싶게."
    )

    system = f"""당신은 '{cfg['brand_name']}'(1925년 창업, 전북 진안, 100년 전통 양조장)의 인스타그램 콘텐츠 에디터입니다.
주력상품 {cfg['product_name']}을 실제로 '팔기 위한' 캡션을 씁니다. 목표는 좋아요가 아니라 구매·저장·DM 입니다.

브랜드 정보:
{cfg['brand_info']}

타겟: 20~30대. 트렌디하지만 가볍고 솔직한 톤. 광고 티 나는 문장은 즉시 이탈시킵니다.
"전통주"보다 "요즘 술", "감성 술", "선물하기 좋은 술"의 맥락으로 접근합니다.

캡션 구조 (반드시 이 순서):
1) 첫 줄 = 훅. 15자 이내. 인스타는 첫 줄만 보이고 나머지는 '더보기'에 숨습니다. 첫 줄이 조회수를 결정합니다.
   아래 훅 유형 중 하나를 골라 쓰되, 최근 게시글에서 쓴 유형은 피할 것:
   ① 반전 ("막걸리인데 딸기우유 맛")  ② 숫자·구체 ("6도인데 혼자 한 병 비움")  ③ 질문 ("술 못 마시는 친구 뭐 줘요?")
   ④ 상황·공감 ("퇴근하고 이거 한 잔이면 끝")  ⑤ 경고·반어 ("이거 선물하면 답례 옵니다")
   훅에 브랜드명·제품명·'소개합니다'·느낌표 금지. 읽는 순간 '왜?'가 떠오르게. 훅만 따로 읽어도 궁금해야 합니다.
   좋은 예: "막걸리인데 왜 딸기우유 맛이 나죠" / "퇴근하고 이거 한 잔이면 끝"
   나쁜 예: "100년 전통의 프리미엄 딸기막걸리를 소개합니다!"
2) 빈 줄
3) 본문 = 문단 2~3개. 각 문단은 2~4줄, 한 줄에 한 문장(25자 이내 권장).
   ★ 줄바꿈 규칙 (반드시 지킬 것):
     - 문단 '안'의 문장들은 그냥 줄바꿈(엔터 1번)으로만 구분합니다. 문장마다 빈 줄을 넣지 마세요.
     - 빈 줄(엔터 2번)은 '문단과 문단 사이'에만 넣습니다.
     - 즉 본문 전체에서 빈 줄은 1~2개뿐입니다. 모든 문장을 빈 줄로 떼어놓으면 글이 세로로 길어져 이탈합니다.
   올바른 예:
     흔들어서 한 입 마시면
     진짜 딸기우유인가 싶어요
     근데 끝에 남는 건 은은한 쌀 향
     (빈 줄)
     진안 마령면 물로 빚고
     국산 딸기 그대로 넣어서
     자연스러운 단맛이 나요
   맛은 구체적 감각으로: "달달함" 대신 "첫 입은 딸기, 끝은 은은한 쌀 향".
본문 마지막 문장은 독자에게 던지는 짧은 질문 1개 (예: "여러분은 어떤 안주랑 드세요?"). CTA 와 별개로 댓글을 부르는 장치입니다.
   아래 중 하나 이상을 자연스럽게 녹일 것:
   - 순간(퇴근 후 / 주말 낮술 / 홈파티 / 선물 / 캠핑 / 비 오는 날)
   - 스토리(100년 양조장, 진안 마령면, 3대째, 국산 딸기, 2025 대한민국주류대상 대상)
   - 정보(6도, 750ml, 페어링 음식, 차갑게 마시는 법, 냉장 보관)
4) 빈 줄
5) CTA 한 줄 — 반드시 아래 문장을 거의 그대로, 캡션 마지막 문장으로 단독 배치:
   "{cta_text}"
   CTA 는 이 한 개만. 다른 유도 문장을 섞지 마세요. 명령형 대신 제안형.
6) 빈 줄 두 개
7) 해시태그 15~20개, 한 줄에 공백으로 나열:
   - 브랜드 고정 5개 (반드시 전부): {' '.join(cfg['hashtags_brand'])}
   - 대중 태그 5~7개 (여기서 선택): {' '.join(cfg['hashtags_general'])}
   - 컨텍스트 태그 5~8개: 이번 글 주제에 맞게 (예: #퇴근후 #홈파티 #선물추천 #캠핑술 #낮술 #집들이선물 #금요일밤)

말투: ~요 체로 통일 (존댓말·반말 혼용 금지). 친한 친구가 DM 보내듯 부드럽게.
이모지 2~4개, 문장 끝이나 줄 시작에만.
전체 길이 공백 포함 {MAX_CAPTION_LEN}자 이내 (해시태그 포함).

절대 금지:
- URL·링크 주소를 캡션에 쓰지 말 것 (인스타 캡션은 링크가 안 눌림. 링크는 첫 댓글·프로필에 있음)
- 과장 효능("건강에 좋다", "숙취 없다"), 경쟁사 언급, 미성년 관련 표현, "!!!" 남발
- 가격 언급, 할인 언급 (실제와 다를 수 있음)
- 최근 게시글과 비슷한 훅·소재·문장 반복
- 캡션 외의 설명·따옴표·머리말·"[캡션]" 같은 라벨 출력 금지. 캡션 본문만 출력."""

    fmt_guide = (
        "릴스(8초 슬라이드 영상). 캡션은 본문 문단 1~2개로 더 짧게. 훅은 영상 첫 화면 위에서 읽히는 한 문장."
        if fmt == "릴스" else
        "사진 캐러셀 3장. 본문 문단 2~3개."
    )
    user = f"""발행 슬롯: {slot} — {slot_guide}
게시 형식: {fmt} — {fmt_guide}
오늘 요일 테마: {theme}
이번 글 주제: {topic}
CTA 유형: {cta_type} / CTA 문장: "{cta_text}"

최근 게시글 (훅·소재·표현이 겹치지 않게):
{recent_block}

위 조건으로 인스타그램 캡션 본문만 출력해 주세요."""

    for attempt in range(3):
        text = call_claude(system, user)
        if len(HASHTAG_RE.sub("", text).strip()) >= 30:
            return text
        print(f"⚠️ 캐션 생성 결과가 비어 있거나 너무 짧음 ({attempt + 1}/3) → 재시도")
        time.sleep(5)
    raise RuntimeError("캐션 생성 3회 실패: 모델이 본문을 돌려주지 않았습니다. 게시를 중단합니다.")


# ─────────────────────────────────────────────
# 3. 캡션 자동 검수 (게시 실패·품질 저하 방지)
# ─────────────────────────────────────────────
HASHTAG_RE = re.compile(r"#[^\s#]+")
URL_RE = re.compile(r"https?://\S+|www\.\S+|smartstore\.naver\.com\S*", re.I)
MAX_BODY_PARAGRAPHS = 3      # 훅·CTA 를 뺀 본문 문단 최대 개수


def tidy_paragraphs(body):
    """
    가독성 보정: 모델이 모든 문장을 빈 줄로 떼어놓으면 글이 세로로 길어져 이탈합니다.
    훅(첫 덩어리)과 CTA(마지막 덩어리)는 그대로 두고,
    가운데 덩어리가 너무 많으면 균등하게 묶어 문단 2~3개로 만듭니다.
    (문단 안 문장은 줄바꿈 1번, 문단 사이만 빈 줄)
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    if len(blocks) <= 2:
        return "\n\n".join(blocks)

    hook, cta, middle = blocks[0], blocks[-1], blocks[1:-1]
    if len(middle) > MAX_BODY_PARAGRAPHS:
        size = -(-len(middle) // MAX_BODY_PARAGRAPHS)      # 올림 나눗셈
        middle = ["\n".join(middle[i:i + size]) for i in range(0, len(middle), size)]
    return "\n\n".join([hook, *middle, cta])


def sanitize_caption(text, cfg):
    # 모델이 붙였을 수 있는 라벨·따옴표·코드펜스 제거
    text = text.strip().strip("`").strip()
    text = re.sub(r"^\[?캡션\]?\s*[:：]?\s*", "", text).strip()
    text = text.strip('"“”').strip()
    # 캡션 안의 링크 제거 (링크는 첫 댓글로). 링크만 있던 줄은 통째로 삭제
    lines = []
    for ln in text.split("\n"):
        if URL_RE.search(ln):
            ln = URL_RE.sub("", ln).strip(" :→-")
            if len(ln) < 4:
                continue
        lines.append(ln.rstrip())
    text = "\n".join(lines)

    tags = HASHTAG_RE.findall(text)
    body = HASHTAG_RE.sub("", text).strip().strip('"“”').strip()
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = tidy_paragraphs(body)

    # 브랜드 고정 태그 보충 (중복 제거, 순서 유지)
    seen, ordered = set(), []
    for t in cfg["hashtags_brand"] + tags:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    if len(ordered) < 12:                     # 너무 적으면 대중 태그로 보충
        for t in cfg["hashtags_general"]:
            if t not in seen and len(ordered) < 15:
                seen.add(t)
                ordered.append(t)
    ordered = ordered[:MAX_HASHTAGS]

    caption = f"{body}\n\n\n{' '.join(ordered)}"
    if len(caption) > 2190:
        caption = caption[:2190]
    return caption


# ─────────────────────────────────────────────
# 4. Google Drive 랜덤 3장 → JPG 변환 → 공개 URL
# ─────────────────────────────────────────────
def list_drive_images(folder_id):
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
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
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
    if DRY_RUN:
        print("🧪 DRY_RUN → 캐시 커밋/푸시 생략")
        return
    def git(*args):
        subprocess.run(["git", *args], check=True)
    git("config", "user.name", "auto-post-bot")
    git("config", "user.email", "bot@users.noreply.github.com")
    git("add", "-A", CACHE_DIR)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        git("commit", "-m", f"{CACHE_DIR}: {', '.join(names)}")
        git("pull", "--rebase", "origin", BRANCH)
        git("push", "origin", f"HEAD:{BRANCH}")
    time.sleep(10)


def list_local_images():
    """GitHub 저장소 안 images_sungsu/ 폴더의 사진 목록 (Drive 폴더와 함께 사용)"""
    if not os.path.isdir(LOCAL_IMAGE_DIR):
        return []
    return [
        {"id": None, "name": f, "local_path": os.path.join(LOCAL_IMAGE_DIR, f)}
        for f in sorted(os.listdir(LOCAL_IMAGE_DIR))
        if os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg")
    ]


def read_image_bytes(f):
    if f.get("local_path"):
        with open(f["local_path"], "rb") as fp:
            return fp.read()
    return download_drive_file(f["id"])


# ─────────────────────────────────────────────
# 4-b. 릴스: 사진 4장 → 9:16 프레임 → 8초 슬라이드 MP4 (ffmpeg)
# ─────────────────────────────────────────────
def to_reels_frame(raw_bytes):
    """사진을 1080x1920 릴스 프레임으로: 흐린 배경 + 원본 비율 유지한 사진을 가운데 배치"""
    from PIL import ImageFilter
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw_bytes))).convert("RGB")
    bg = ImageOps.fit(img, (REELS_W, REELS_H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
    fg = img.copy()
    fg.thumbnail((REELS_W - 80, REELS_H - 400), Image.LANCZOS)
    bg.paste(fg, ((REELS_W - fg.width) // 2, (REELS_H - fg.height) // 2))
    return bg


def build_reels_video(frames, out_path):
    """프레임 이미지 목록 → 슬라이드 MP4. 장당 REELS_SEC_PER_IMAGE 초, H.264 + 무음 AAC (인스타 릴스 규격)"""
    tmp = os.path.join(CACHE_DIR, "_reel_frames")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    list_path = os.path.join(tmp, "list.txt")
    with open(list_path, "w", encoding="utf-8") as fp:
        for i, fr in enumerate(frames):
            fr.save(os.path.join(tmp, f"f{i}.jpg"), "JPEG", quality=92)
            fp.write(f"file 'f{i}.jpg'\nduration {REELS_SEC_PER_IMAGE}\n")
        fp.write(f"file 'f{len(frames) - 1}.jpg'\n")   # concat demuxer: 마지막 프레임 유지용
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-shortest", "-r", "30",
        "-vf", f"scale={REELS_W}:{REELS_H},format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    shutil.rmtree(tmp, ignore_errors=True)


def pick_reels_images(cfg, log):
    files = list_local_images()
    if len(files) < REELS_IMAGE_COUNT:
        raise RuntimeError(f"{LOCAL_IMAGE_DIR}/ 폴더에 이미지가 {len(files)}장뿐입니다 (릴스 최소 {REELS_IMAGE_COUNT}장).")
    recent_names = {n for p in log["posts"][-EXCLUDE_RECENT_POSTS:] for n in p.get("images", [])}
    fresh = [f for f in files if f["name"] not in recent_names]
    candidates = fresh if len(fresh) >= REELS_IMAGE_COUNT else files
    chosen = random.sample(candidates, REELS_IMAGE_COUNT)

    os.makedirs(CACHE_DIR, exist_ok=True)
    prune_cache()
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    frames = [to_reels_frame(read_image_bytes(f)) for f in chosen]
    name = f"{stamp}_reel.mp4"
    out = os.path.join(CACHE_DIR, name)
    build_reels_video(frames, out)
    print(f"  🎬 릴스 생성: {name} ({os.path.getsize(out) // 1024} KB, {len(frames)}장 × {REELS_SEC_PER_IMAGE}초)")
    git_push_cache([name])
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{CACHE_DIR}/{name}"
    return [f["name"] for f in chosen], [url]


def pick_images(cfg, log):
    files = list_local_images()   # GitHub 저장소 images_sungsu/ 폴더만 사용 (Drive 미사용)
    if len(files) < IMAGE_COUNT:
        raise RuntimeError(
            f"{LOCAL_IMAGE_DIR}/ 폴더에 이미지가 {len(files)}장뿐입니다 (최소 {IMAGE_COUNT}장). "
            "폴더가 '링크가 있는 모든 사용자' 로 공유되어 있는지, drive_folder_id 가 맞는지 확인하세요."
        )

    # 최근 N회에 쓴 사진은 제외 (사진이 충분할 때만)
    recent_names = {n for p in log["posts"][-EXCLUDE_RECENT_POSTS:] for n in p.get("images", [])}
    fresh = [f for f in files if f["name"] not in recent_names]
    candidates = fresh if len(fresh) >= IMAGE_COUNT else files
    chosen = random.sample(candidates, IMAGE_COUNT)

    os.makedirs(CACHE_DIR, exist_ok=True)
    prune_cache()
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    names, urls = [], []
    for i, f in enumerate(chosen, 1):
        jpg = to_instagram_jpg(read_image_bytes(f))
        name = f"{stamp}_{i}.jpg"
        with open(os.path.join(CACHE_DIR, name), "wb") as fp:
            fp.write(jpg)
        names.append(name)
        urls.append(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{CACHE_DIR}/{name}")
        print(f"   {f['name']} → {name} ({len(jpg)//1024} KB)")

    git_push_cache(names)
    return [f["name"] for f in chosen], urls


# ─────────────────────────────────────────────
# 5. Instagram 캐러셀 게시 + 첫 댓글(구매 링크)
# ─────────────────────────────────────────────
def wait_container(container_id, max_wait=180):
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

    res = http_json(f"{IG_API}/{USER_ID}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "caption": caption,
        "access_token": ACCESS_TOKEN,
    })
    creation_id = res["id"]
    wait_container(creation_id)

    res = http_json(f"{IG_API}/{USER_ID}/media_publish", {
        "creation_id": creation_id,
        "access_token": ACCESS_TOKEN,
    })
    return res["id"]


def post_reel_to_instagram(caption, video_url):
    """릴스 게시: 영상 컨테이너 생성 → 처리 대기(최대 10분) → 발행. share_to_feed 로 피드에도 노출."""
    res = http_json(f"{IG_API}/{USER_ID}/media", {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": ACCESS_TOKEN,
    })
    creation_id = res["id"]
    wait_container(creation_id, max_wait=600)

    res = http_json(f"{IG_API}/{USER_ID}/media_publish", {
        "creation_id": creation_id,
        "access_token": ACCESS_TOKEN,
    })
    return res["id"]


def post_fixed_comment(media_id, text):
    """첫 댓글 = 구매 링크. 캡션 링크는 안 눌리므로 판매 전환의 핵심 경로."""
    if not text:
        return None
    for attempt in range(3):
        try:
            res = http_json(f"{IG_API}/{media_id}/comments", {
                "message": text,
                "access_token": ACCESS_TOKEN,
            })
            print(f"💬 구매 링크 댓글 등록 완료: {res.get('id')}")
            return res.get("id")
        except Exception as e:
            print(f"⚠️ 댓글 등록 실패 ({attempt+1}/3): {e}")
            time.sleep(10)
    print("⚠️ 댓글 3회 실패 — 게시는 완료됨. 구매 링크 댓글을 수동으로 달아주세요.")
    return None


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
            print(f"⚠️ GH_PAT 미설정: Secrets 의 {SECRET_NAME} 을 수동 교체해 주세요.")
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
            f"/repos/{REPO}/actions/secrets/{SECRET_NAME}",
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
    cfg = load_cfg()
    slot = current_slot()
    topic = pick_topic(cfg, log)
    cta_type, cta_text = pick_cta(cfg, log)
    print(f"📌 회차 {log['count']+1} | 슬롯 {slot} | 주제: {topic}")
    print(f"🎯 CTA: [{cta_type}] {cta_text}")

    is_reel = bool(REELS_SLOT) and slot == REELS_SLOT
    fmt = "릴스" if is_reel else "캐러셀"
    print(f"🎞️ 게시 형식: {fmt}")

    raw = generate_caption(topic, slot, cta_type, cta_text, cfg, log, fmt)
    if DRY_RUN:
        print(f"📝 모델 원문 ({len(raw)}자):\n{raw}\n")
    caption = sanitize_caption(raw, cfg)
    if len(HASHTAG_RE.sub("", caption).strip()) < 30:
        raise RuntimeError("캐션 본문이 비어 있어 게시를 중단합니다 (해시태그만 있는 글 방지).")
    print(f"✍️ 캡션 ({len(caption)}자, 해시태그 {len(HASHTAG_RE.findall(caption))}개):\n{caption}\n")

    print(f"🖼️ {LOCAL_IMAGE_DIR}/ 에서 이미지 추출·변환 중... ({fmt})")
    if is_reel:
        chosen, urls = pick_reels_images(cfg, log)
    else:
        chosen, urls = pick_images(cfg, log)
    print(f"🖼️ 선택된 이미지: {chosen}")

    if DRY_RUN:
        print("🧪 DRY_RUN=1 → 게시하지 않고 종료 (캡션·미디어 변환까지만 검증)")
        return

    if is_reel:
        post_id = post_reel_to_instagram(caption, urls[0])
    else:
        post_id = post_to_instagram(caption, urls)
    print(f"🚀 게시 완료! media id = {post_id}")
    time.sleep(5)
    post_fixed_comment(post_id, cfg.get("fixed_comment", ""))

    log["count"] += 1
    log["posts"].append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "slot": slot,
        "topic": topic,
        "cta_type": cta_type,
        "text": caption,
        "images": chosen,
        "format": "reel" if is_reel else "carousel",
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
