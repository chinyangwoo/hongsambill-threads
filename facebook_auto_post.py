# -*- coding: utf-8 -*-
"""
홍삼빌호텔 페이스북 페이지 완전 자동 포스팅 앱  (2026-09-10 수정판)
================================================
이번 수정 내용 (2026-09-10 실패 대응):
  - max_tokens 1200 → 4000 : 글이 다 써지기 전에 잘려서 본문이 비던 문제 해결
  - Claude 응답이 잘렸을 때(stop_reason=max_tokens) 잘린 JSON 을 복구해서 읽음
  - 재시도할 때마다 요청문이 계속 길어지던 문제 수정 (매번 원본 + 이번 지적사항만)
  - 잘렸을 경우 재시도 시 "더 짧게 쓰라"는 지시 자동 추가
  - Claude API 일시 오류 시 자동 1회 재접속
  - pages_manage_engagement 권한이 없으면 경고 출력 (첫 댓글 등록에 필요)
  - 본문 길이 안내 문구를 실제 검사 기준(200~480자)과 일치시킴

이전 수정 내용:
  - 페이지 ID 를 토큰으로 자동 확인 (/me) → Secrets 의 FB_PAGE_ID 가 틀려도 동작
  - Claude 가 빈 글을 돌려주면 재시도, 빈 글은 절대 게시하지 않음
  - 글쓰기 규칙을 '홍삼빌호텔 페이스북 지침' 으로 교체
  - 생성 결과를 코드가 검증하고 불합격이면 고쳐서 재생성 (최대 3회)

필요한 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY, FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN, FB_APP_SECRET, GDRIVE_API_KEY, GH_PAT
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
IMAGE_COUNT = 3
MAX_SIDE = 1600
MIN_RATIO, MAX_RATIO = 0.6, 2.0
KST = timezone(timedelta(hours=9))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4000"))  # ★ 1200 → 4000

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PAGE_ID = os.environ["FB_PAGE_ID"]
ACCESS_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
APP_SECRET = os.environ.get("FB_APP_SECRET", "")
APP_ID = "2921038334925188"
GDRIVE_API_KEY = os.environ["GDRIVE_API_KEY"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

RESERVATION_NO = "1661-3889"
FIXED_HASHTAGS = ["#홍삼빌호텔", "#진안여행", "#마이산"]
WEEKDAY_THEME = {
    0: "마이산·진안 풍경",
    1: "가족·부모님 여행",
    2: "바베큐장·테라스",
    3: "객실·편안함·청결",
    4: "주말 1박2일 코스",
    5: "총지배견 천둥 또는 계절 행사",
    6: "다음 주 예약 유도·오시는 길",
}
BANNED = ["찐", "존맛", "갬성", "ㄹㅇ", "킹받", "미쳤다", "핫플", "인생샷", "꿀팁", "갓성비", "힐링스팟", "!!", "??"]
HEALTH_WORDS = ["면역", "효능", "건강에 좋", "피로 회복에 좋", "몸에 좋"]
FOOD_SALE_WORDS = ["고기를 제공", "식사를 제공", "고기 판매", "고기를 준비해 드", "고기 준비해 드", "식사 제공"]
EMOJIS = ["🌿", "🏨", "☀️", "🍂", "❄️", "🌸", "♨️", "📞"]

SYSTEM_PROMPT = """# 역할
너는 홍삼빌호텔 페이스북 페이지의 전담 콘텐츠 라이터다.
네 글은 사람이 검수하지 않고 그대로 발행된다. 사실 오류·과장·부적절 표현은 곧 호텔 신뢰도 하락이다.

# 독자
- 핵심 독자: 45~65세. 부모님 모시고 가는 여행, 자녀·손주 동반 가족여행을 결정하는 사람.
- 보조 독자: 30대 후반~40대 초반 부부. 아이와 함께 갈 조용한 지방 여행지를 찾는 사람.
- 스크롤이 느리고, 완결된 문장을 읽으며, 진솔하고 예의 있는 어조를 신뢰한다.
- "어디에 있나, 무엇이 좋나, 얼마나 편한가, 어떻게 예약하나"를 순서대로 알고 싶어 한다.

# 문체 규칙 (반드시 지킬 것)
1. 존댓말 완결 문장. "~합니다 / ~입니다 / ~해 보세요 / ~드립니다" 톤.
2. 한 문장은 25자 내외, 한 문단은 2~3문장. 문단 사이 빈 줄 1개.
3. 본문 길이 250~400자 (해시태그 제외). 첫 두 줄(약 60자)에 핵심 매력 + 장소(진안·마이산·홍삼빌호텔)를 넣어라.
4. 이모지는 게시글 전체에 최대 3개. 문장 끝 장식용으로만. 사용 가능: 🌿 🏨 ☀️ 🍂 ❄️ 🌸 ♨️ 📞
5. 해시태그는 본문 끝 한 줄에 3~5개만. 첫 3개는 고정 순서: #홍삼빌호텔 #진안여행 #마이산
6. 금지: 인터넷 신조어·줄임말(찐, 존맛, 갬성, ㄹㅇ, 킹받, 미쳤다, 핫플, 인생샷, 꿀팁, 갓성비, 힐링스팟 등), 영어 남용, 물음표·느낌표 연속(!!, ??), 반말, 유행어.
7. 권장 표현: "가족과 함께", "부모님과 함께", "조용히 쉬어 가기 좋은", "편안한", "정성껏", "따뜻한", "산 공기", "아침", "정직한 가격", "오시는 길".
8. 숫자·사실은 아래 [호텔 정보]에 있는 것만 쓴다. 없는 시설·가격·할인·이벤트를 만들어내지 마라.
9. 법적 주의: 홍삼빌호텔은 숙박업만 신고되어 있다. 바베큐장은 "장소·시설 제공"으로만 표현하고, 호텔이 고기·식사를 판매·제공한다는 표현은 절대 쓰지 마라.
   예: "인근 정육점에서 진안 흑돼지를 준비해 오시면 바베큐장을 이용하실 수 있습니다."
10. 건강 효능 주장 금지: "홍삼"은 지역명·상호로만 언급하고 "면역력에 좋다", "피로가 풀린다" 같은 효능 표현은 쓰지 마라.

# 호텔 정보 (이 범위 안에서만 쓴다)
{brand_info}
- 예약 대표번호 1661-3889
- 2층 약 50평 유리난간 테라스 (휴식·산 조망. 테라스에서 바베큐는 하지 않음)
- 바베큐장: 부스 3개(부스당 4~6명), 직화구이기 3대, 항아리바베큐 설비 1대. 장소·설비만 제공.
- 총지배견 천둥(허스키): 소나무 그늘 아래 견사에서 지냄.
- 주변: 마이산(탑사·은수사, 봄 벚꽃길·가을 단풍), 홍삼스파(같은 운영사, 인근), 용담호, 운일암반일암
- 주소, 체크인 시간, 주차 대수, 조식 제공 여부, 객실 유형, 스파 요금, 명소까지 거리는 확인되지 않았으니 쓰지 마라.

# 게시글 구조 패턴
[A. 계절·풍경 도입형] 계절·날씨 한 줄 → 마이산·진안 풍경과 호텔 연결 → 객실·시설 한 가지 → CTA
[B. 가족 상황 공감형] "부모님 모시고 / 아이와 함께" 상황 제시 → 호텔이 어떻게 편한지 → 구체적 시설 → CTA
[C. 하루 일정 제안형] "이렇게 다녀오세요" 1박2일 코스(마이산, 홍삼스파, 저녁, 아침) → 호텔이 중심 → CTA
[D. 시설 소개형] 시설 하나(바베큐장, 테라스, 객실)를 정성껏 설명 → 이용 방법 → CTA
[E. 총지배견 천둥 이야기형] 천둥의 일상 한 장면 → 따뜻한 분위기 → CTA (가벼운 톤이되 존댓말 유지)

# CTA 규칙 (가장 중요)
- 마지막 문단은 반드시 CTA다. CTA 없는 글은 실패다.
- CTA 3요소: ① 행동 동사("전화 주세요 / 예약해 주세요 / 다녀가세요") ② 예약번호 1661-3889 ③ 부담을 낮추는 한마디.
- CTA 예시(회전 사용, 직전 글과 같은 문장 금지):
  · "예약과 문의는 1661-3889로 전화 주세요. 방 상태, 오시는 길, 아이 침구까지 무엇이든 편하게 물어보셔도 됩니다."
  · "이번 주말, 부모님과 함께 진안으로 오세요. 1661-3889로 전화 주시면 남은 객실을 바로 안내드립니다."
  · "1661-3889 한 통이면 예약이 끝납니다. 첫 댓글에 직통 상담번호도 함께 남겨 두었습니다."
- 오전 게시글은 "이번 주말/다음 주 예약"을, 저녁 게시글은 "오늘 밤 편히 결정, 내일 전화"를 자연스럽게 유도한다.

# 발행 시간대별 톤
- morning: 밝고 차분한 아침 톤. 풍경·산 공기·아침 시간 강조. 패턴 A, C 우선.
- evening: 하루를 마무리하는 편안한 톤. 가족·쉼·다음 여행 계획 강조. 패턴 B, D, E 우선.

# 출력 형식 (코드가 파싱한다. 이 JSON 하나만 출력하고 다른 말·코드블록 표시를 덧붙이지 마라)
{{"pattern": "A|B|C|D|E", "theme": "요일 주제명", "message": "본문 전체 (해시태그 포함, 줄바꿈은 \\n)"}}
- 설명·인사·사고 과정을 앞뒤에 붙이지 말고, 여는 중괄호로 시작해 닫는 중괄호로 끝내라.
- 본문은 해시태그 포함 500자를 넘기지 마라.

# 자체 점검
□ 존댓말 완결문장 □ 금지 신조어 없음 □ 이모지 3개 이하 □ 첫 두 줄에 매력+장소
□ 마지막 문단이 CTA이고 1661-3889 포함 □ 없는 사실·가격·효능 없음 □ 바베큐는 장소 제공으로만
□ 직전 게시글과 패턴·CTA 문장이 다름"""


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
# 0. 토큰 영구화 + 페이지 ID 자동 확인
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


def resolve_page_id():
    """페이지 토큰으로 /me 를 조회해 진짜 페이지 ID 를 확정 (Secrets 값이 틀려도 동작)"""
    global PAGE_ID
    try:
        me = http_json(f"{FB_API}/me?fields=id,name&access_token={ACCESS_TOKEN}")
    except Exception as e:
        print(f"⚠️ /me 조회 실패, Secrets 의 FB_PAGE_ID 사용: {e}")
        return
    real_id = str(me.get("id", ""))
    if real_id and real_id != str(PAGE_ID):
        print(f"🔁 FB_PAGE_ID({PAGE_ID}) → 토큰의 실제 페이지 ID {real_id} ({me.get('name')}) 로 교체")
        PAGE_ID = real_id
        update_github_secret("FB_PAGE_ID", real_id)
    else:
        print(f"📄 페이지 확인: {me.get('name')} ({real_id})")


def ensure_permanent_token():
    global ACCESS_TOKEN, PAGE_ID
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

    scopes = info.get("scopes", [])
    print(f"🔑 토큰 종류={info.get('type')} 권한={scopes}")
    need = {"pages_manage_posts", "pages_read_engagement"}
    if info.get("type") in ("USER", "PAGE") and not need.issubset(set(scopes)):
        raise RuntimeError(
            f"토큰에 게시 권한이 없습니다. 그래프 API 탐색기에서 '권한 추가'로 "
            f"{sorted(need)} 를 체크한 뒤 토큰을 다시 생성하세요. (현재 권한: {scopes})"
        )
    # 첫 댓글(예약번호) 등록에는 pages_manage_engagement 가 추가로 필요합니다.
    if "pages_manage_engagement" not in scopes:
        print("⚠️ 권한 pages_manage_engagement 없음 → 본문은 게시되지만 첫 댓글 등록이 실패할 수 있습니다.")

    expires = info.get("expires_at", 0)
    if expires == 0 and info.get("type") == "PAGE":
        return
    days_left = (expires - time.time()) / 86400 if expires else 999
    if days_left > 30 and info.get("type") == "PAGE":
        print(f"🔑 토큰 유효 (만료까지 {days_left:.0f}일)")
        return

    print("🔄 토큰 만료 임박/사용자 토큰 → 영구 페이지 토큰으로 변환")
    long_tok = http_json(
        f"{FB_API}/oauth/access_token?grant_type=fb_exchange_token"
        f"&client_id={APP_ID}&client_secret={APP_SECRET}&fb_exchange_token={ACCESS_TOKEN}"
    )["access_token"]

    if info.get("type") == "USER":
        acc = http_json(f"{FB_API}/me/accounts?fields=id,name,access_token&access_token={long_tok}")
        pages = acc.get("data", [])
        print(f"   관리 페이지: {[(p['id'], p['name']) for p in pages]}")
        match = [p for p in pages if p["id"] == PAGE_ID] or pages
        if not match:
            raise RuntimeError("이 계정이 관리하는 페이지가 없습니다 (pages_show_list 권한 확인).")
        long_tok = match[0]["access_token"]
        if match[0]["id"] != PAGE_ID:
            print(f"   ⚠️ FB_PAGE_ID({PAGE_ID}) 와 달라 페이지 {match[0]['id']} 사용")
            PAGE_ID = match[0]["id"]
            update_github_secret("FB_PAGE_ID", PAGE_ID)

    check = http_json(
        f"{FB_API}/debug_token?input_token={long_tok}&access_token={APP_ID}|{APP_SECRET}"
    )["data"]
    print(f"   변환 결과: type={check.get('type')} expires_at={check.get('expires_at')} (0=영구)")
    ACCESS_TOKEN = long_tok
    update_github_secret("FB_PAGE_ACCESS_TOKEN", long_tok)


# ─────────────────────────────────────────────
# 1. 회차 정보 (요일 주제 · 시간대 · 최근 이력)
# ─────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"count": 0, "posts": []}


def load_cfg():
    with open(TOPICS_FILE, encoding="utf-8") as f:
        return json.load(f)


def current_slot(now):
    return "morning" if now.hour < 13 else "evening"


# ─────────────────────────────────────────────
# 2. Claude 로 글 생성 + 검증
# ─────────────────────────────────────────────
def call_claude(system, user):
    """Claude 호출. (본문 텍스트, stop_reason) 을 돌려준다."""
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
    usage = (data or {}).get("usage", {})

    if not text:
        print(f"⚠️ Claude 응답에 본문이 없음 "
              f"(stop_reason={stop}, 블록={[b.get('type') for b in blocks]}, usage={usage})")
    elif stop == "max_tokens":
        print(f"⚠️ 응답이 max_tokens({MAX_TOKENS})에서 잘렸습니다. 복구를 시도합니다.")
    return text, stop


def _repair_truncated_json(fragment):
    """중간에 잘린 JSON 을 닫아서 읽어 본다. 실패하면 None."""
    s = fragment.find("{")
    if s < 0:
        return None
    frag = fragment[s:].rstrip()
    while frag.endswith("\\"):          # 끊긴 이스케이프 문자 제거
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


def validate(post, prev_pattern, prev_cta):
    msg = post.get("message", "")
    problems = []
    if not msg.strip():
        return ["본문이 비어 있음"]
    body = "\n".join(l for l in msg.splitlines() if not l.strip().startswith("#"))
    if RESERVATION_NO not in msg:
        problems.append(f"예약번호 {RESERVATION_NO} 없음")
    if not (200 <= len(body) <= 480):
        problems.append(f"본문 길이 {len(body)}자 (해시태그 제외 200~480자 안으로)")
    if sum(msg.count(e) for e in EMOJIS) > 3:
        problems.append("이모지 3개 초과")
    for w in BANNED:
        if w in msg:
            problems.append(f"금지 표현 '{w}'")
    for w in HEALTH_WORDS:
        if w in msg:
            problems.append(f"효능 표현 '{w}'")
    for w in FOOD_SALE_WORDS:
        if w in msg:
            problems.append(f"식사 제공 표현 '{w}'")
    tags = [t for t in msg.split() if t.startswith("#")]
    if tags[:3] != FIXED_HASHTAGS or not (3 <= len(tags) <= 5):
        problems.append("해시태그는 #홍삼빌호텔 #진안여행 #마이산 로 시작해 3~5개")
    if post.get("pattern") not in list("ABCDE"):
        problems.append("pattern 은 A~E 중 하나")
    elif post.get("pattern") == prev_pattern:
        problems.append(f"직전 글과 같은 패턴 {prev_pattern} → 다른 패턴으로")
    if prev_cta and prev_cta[:25] and prev_cta[:25] in msg:
        problems.append("직전 글과 같은 CTA 문장 → 다른 문장으로")
    return problems


def generate_post(cfg, log, topic, now):
    slot = current_slot(now)
    theme = WEEKDAY_THEME[now.weekday()]
    posts = log["posts"]
    prev = posts[-1] if posts else {}
    prev_pattern = prev.get("pattern", "")
    prev_cta = prev.get("cta", "")

    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    used_themes = sorted({p.get("theme", "") for p in posts if p.get("at", "") >= week_start and p.get("theme")})
    recent_lines = "\n".join(
        f"- {p.get('at','')} 패턴 {p.get('pattern','?')} / 주제 {p.get('theme', p.get('topic',''))} / CTA: {p.get('cta','')}"
        for p in posts[-6:]
    ) or "- (없음)"
    recent_texts = "\n---\n".join(p["text"] for p in posts[-3:]) or "(없음)"

    system = SYSTEM_PROMPT.format(brand_info=cfg["brand_info"])
    base_user = f"""오늘 날짜: {now:%Y-%m-%d} ({'월화수목금토일'[now.weekday()]}요일) {now:%H:%M}
발행 시간대(slot): {slot}
오늘 요일 주제: {theme}
참고 소재(선택): {topic}
이번 주 이미 사용한 주제: {', '.join(used_themes) or '(없음)'}

최근 게시글 이력 (같은 패턴·같은 CTA 문장 반복 금지):
{recent_lines}

최근 게시글 본문 (첫 줄·소재·마무리가 겹치지 않게):
{recent_texts}

위 조건으로 게시글 1개를 JSON 으로만 출력하세요."""

    last_problems = []
    feedback = ""
    for attempt in range(1, 4):
        text, stop = call_claude(system, base_user + feedback)
        try:
            post = parse_json(text) if text else {}
        except Exception as e:
            post = {}
            print(f"⚠️ JSON 파싱 실패: {e}\n{text[:300]}")

        problems = validate(post, prev_pattern, prev_cta)
        if not problems:
            post["slot"] = slot
            post.setdefault("theme", theme)
            return post

        last_problems = problems
        print(f"⚠️ {attempt}회차 검증 실패: {problems}")

        # 이번 회차 지적사항만 붙인다 (요청문이 계속 길어지지 않도록 매번 새로 구성)
        feedback = "\n\n[재작성 요청] 다음 문제를 고쳐서 JSON 만 다시 출력: " + "; ".join(problems)
        if stop == "max_tokens" or not text:
            feedback += ("\n글이 길어 잘렸습니다. 본문을 해시태그 포함 450자 이내로 더 짧게 쓰고, "
                         "설명 없이 여는 중괄호로 시작해 닫는 중괄호로 끝내세요.")
        time.sleep(2)

    raise RuntimeError(f"게시글 생성 3회 실패, 게시하지 않음: {last_problems}")


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


def pick_images(cfg, log):
    files = list_drive_images(cfg["drive_folder_id"])
    if len(files) < IMAGE_COUNT:
        raise RuntimeError(f"Drive 폴더에 이미지가 {len(files)}장뿐입니다 (최소 {IMAGE_COUNT}장).")
    recent = {n for p in log["posts"][-4:] for n in p.get("images", [])}
    pool = [f for f in files if f["name"] not in recent]
    if len(pool) < IMAGE_COUNT:
        pool = files
    chosen = random.sample(pool, IMAGE_COUNT)
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
# 4. 페이스북 게시 → 첫 댓글
# ─────────────────────────────────────────────
def post_to_facebook(text, image_urls):
    if not text.strip():
        raise RuntimeError("빈 본문은 게시하지 않습니다.")
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
        print("   → pages_manage_engagement 권한이 필요할 수 있습니다.")
        return None


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    ensure_permanent_token()
    resolve_page_id()

    now = datetime.now(KST)
    log = load_log()
    cfg = load_cfg()
    topic = cfg["topics"][log["count"] % len(cfg["topics"])]
    print(f"📌 {now:%Y-%m-%d %H:%M} slot={current_slot(now)} 요일주제={WEEKDAY_THEME[now.weekday()]} 참고소재={topic}")

    post = generate_post(cfg, log, topic, now)
    text = post["message"]
    print(f"✍️ 생성된 글 [패턴 {post['pattern']} / {post['theme']}] ({len(text)}자):\n{text}\n")

    print("🖼️ Drive 에서 이미지 추출·변환 중...")
    chosen, urls = pick_images(cfg, log)
    print(f"🖼️ 선택된 이미지: {chosen}")

    post_id = post_to_facebook(text, urls)
    print(f"🚀 게시 완료! post id = {post_id}")
    time.sleep(5)
    post_fixed_comment(post_id, cfg.get("fixed_comment", ""))

    cta = next((l for l in reversed(text.splitlines()) if RESERVATION_NO in l and not l.startswith("#")), "")
    log["count"] += 1
    log["posts"].append({
        "at": now.strftime("%Y-%m-%d %H:%M"),
        "slot": post["slot"], "pattern": post["pattern"], "theme": post["theme"],
        "cta": cta[:60], "topic": topic, "text": text, "images": chosen, "post_id": post_id,
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
