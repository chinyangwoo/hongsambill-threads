# -*- coding: utf-8 -*-
"""
홍삼빌호텔 스레드(Threads) 완전 자동 포스팅 앱  v2 (후킹 강화판)
==================================================================
v1 대비 바뀐 점
  ① 글 길이 480자 → 330자 (약 70%). 첫 줄 42자 제한.
  ② 훅(hook) 유형 8종을 매 회차 하나씩 '강제 지정'해서 첫 줄을 만든다.
  ③ 게시 후 조회수를 Threads API로 자동 수집 → 훅 유형별 평균 조회수 계산
     → 성과 좋은 훅에 가중치를 주고 25%는 새 훅을 실험한다 (자가 학습).
  ④ 생성된 글을 규칙(길이·줄바꿈·금지어·첫줄)으로 검사해서 불합격이면 재생성.

v2.1 (2026-10-06) 빈 글 게시 방지
  - Claude 가 빈 응답을 주면 후보에서 빼고 다시 요청한다. (기존에는 빈 글이
    '문제 1건'으로 계산돼 오히려 최선안으로 뽑혀 사진만 게시되는 일이 있었음)
  - 끝까지 글이 안 나오면 사진만 올리지 않고 그 회차를 실패 처리한다.
  - Claude API 일시 오류(과부하 등)는 잠깐 쉬었다가 자동 재시도한다.

v2.2 (2026-10-06) "게시됐다는데 계정에서 안 보임" 진단 기능
  - 실행할 때마다 이 토큰이 실제로 연결된 스레드 계정(@아이디)을 확인해 기록한다.
  - 게시글마다 바로 열어볼 수 있는 주소(permalink)를 posted_log.json 에 남긴다.
  - 최근 글 15개도 주소를 채워 넣고, 조회가 안 되는 글은 '조회 실패'로 표시한다.
  - 오류 메시지에 토큰이 섞여 나오지 않도록 가린다.

v2.3 (2026-10-15) 댓글 유도 강화
  - Claude 의 '생각(thinking)' 단계가 토큰을 다 써서 글이 비던 문제 수정.
  - 게시물마다 조회수와 함께 '댓글 수'도 수집한다.
  - 댓글 유도 방식(CTA)도 훅처럼 성적표를 만들어, 댓글이 많이 달린 방식에
    가중치를 주고 25%는 새 방식을 실험한다 (자가 학습).
  - 댓글 유도 방식을 '읽는 사람이 1초 안에 자기 얘기로 답할 수 있는 것' 8종으로 교체.
  - 댓글 유도 문장은 마지막에 따로 한 줄로 쓰게 하고, 규칙으로 검사한다.
    (읽는 사람이 그 장소에 가봤다고 가정하는 질문, '어떠세요' 류 막연한 질문 금지)

동작 순서:
  1. 최근 게시물 조회수 수집 → posted_log.json 갱신
  2. topics.json 에서 주제 선택(순환) + 훅 유형 선택(성과 기반) + 댓글유도 선택
  3. Claude API 로 글 생성 → 검증 → 불합격 시 최대 3회 재생성
  4. images/ 폴더에서 랜덤 3장 → 캐러셀 게시
  5. 로그 저장 (훅 유형 함께 기록)
  6. 토큰 만료 임박 시 자동 갱신

필요한 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY      : Claude API 키
  THREADS_ACCESS_TOKEN   : Threads 장기 액세스 토큰
  THREADS_USER_ID        : Threads 사용자 ID (숫자)
  GH_PAT                 : (선택) 토큰 자동 갱신용
"""

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
THREADS_API = "https://graph.threads.net/v1.0"
IMAGE_DIR = "images"
LOG_FILE = "posted_log.json"
TOKEN_FILE = ".token_meta.json"
TOPICS_FILE = "topics.json"

IMAGE_COUNT = 3
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
KST = timezone(timedelta(hours=9))

MAX_RETRY = 3            # 글 품질 미달 시 재생성 횟수
EMPTY_RETRY = 3          # Claude 가 빈 응답을 줬을 때 추가로 다시 요청하는 횟수
API_RETRY = 3            # Claude API 일시 오류(과부하·속도제한) 시 재시도 횟수
EXPLORE_RATE = 0.25      # 25%는 성과와 무관하게 새 훅 실험
MIN_SAMPLES = 2          # 훅 유형별 최소 표본 수(이하면 무조건 실험 대상)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
USER_ID = os.environ["THREADS_USER_ID"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

# 댓글 유도 방식 (topics.json 의 cta_types 가 옛날 형식(문장 목록)이면 이걸 쓴다)
# 원칙: 읽는 사람이 '지금 그 자리에서' '자기 경험·취향으로' '1초 안에' 답할 수 있어야 한다.
DEFAULT_CTAS = [
    {"id": "ab", "name": "둘 중 하나 고르기",
     "rule": "글 내용과 직결된 선택지 2개를 주고 A/B(또는 1/2)로만 답하게 한다. 두 선택지 모두 그럴듯해서 갈리게 만든다.",
     "samples": ["새벽 등산 후 낮잠 vs 늦잠 자고 스파, 너네는 뭐 고름? A/B로만", "여행 가면 1번 맛집 먼저 2번 숙소 먼저, 번호만"]},
    {"id": "number", "name": "숫자로 답하기",
     "rule": "읽는 사람 자신의 습관·경험을 숫자 하나로 답하게 한다.",
     "samples": ["너네 여행 가면 하루에 몇 시간 걸음? 숫자만", "1년에 국내여행 몇 번 감? 솔직하게 숫자로"]},
    {"id": "metoo", "name": "나만 그런지 확인",
     "rule": "글에 나온 내 습관·실수가 나만 그런지 묻는다. 'ㅇㅇ' 한 글자로도 답할 수 있게.",
     "samples": ["이거 나만 그런 거 아니지? 맞으면 ㅇㅇ", "여행 가서 숙소에만 있는 사람 나 말고 또 있음?"]},
    {"id": "debate", "name": "반박 유도",
     "rule": "살짝 갈릴 만한 내 의견을 단정적으로 던지고 아니라는 사람을 부른다. 지역·업체·사람 비하 금지.",
     "samples": ["단풍은 무조건 10월 말이라고 봄, 아니라는 사람?", "여행은 1박이 제일 좋다고 생각함, 반박 받음"]},
    {"id": "recommend", "name": "추천 하나만 받기",
     "rule": "읽는 사람이 아는 정보를 딱 하나만 알려달라고 한다. 범위를 좁혀서 바로 떠오르게 한다.",
     "samples": ["비 오는 날 국내여행 필살기 하나만 알려줘", "1박2일 근교 여행지 너만 아는 데 하나만"]},
    {"id": "tag", "name": "같이 갈 사람 태그",
     "rule": "글 내용에 딱 맞는 특정한 사람(누구인지 구체적으로)을 한 명만 태그하게 한다.",
     "samples": ["맨날 피곤하다는 친구 한 명 태그", "강아지 있는 친구 이거 보여줘"]},
    {"id": "blank", "name": "빈칸 채우기",
     "rule": "읽는 사람 자신의 이야기로 빈칸을 채우게 한다. 빈칸은 ___ 로 표시하고 한 단어로 답할 수 있게.",
     "samples": ["여행 가면 제일 먼저 하는 거: ___ 한 단어로", "내 인생 최고의 여행지는 ___ 지역명만"]},
    {"id": "handsup", "name": "경험 있는 사람 손",
     "rule": "글에 나온 상황을 겪어본 사람만 손들게 한다. 상황은 많은 사람이 겪어봤을 만큼 흔해야 한다.",
     "samples": ["여행 가서 길 헤매다 반나절 날려본 사람 손", "쉬러 갔다가 더 피곤해서 온 사람 손"]},
]
CTA_EXPLORE_RATE = 0.25   # 25%는 댓글 성적과 무관하게 새 방식 실험


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
        # 오류 문구에 토큰이 그대로 찍히지 않도록 가린다
        safe_url = url.replace(urllib.parse.quote(ACCESS_TOKEN), "***").replace(ACCESS_TOKEN, "***")
        raise RuntimeError("HTTP " + str(e.code) + " 오류: " + safe_url + "\n응답: " + body) from e


# ─────────────────────────────────────────────
# 0. 로그 / 설정 로드
# ─────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
    else:
        log = {"count": 0, "posts": []}
    log.setdefault("count", 0)
    log.setdefault("posts", [])
    return log


def load_cfg():
    with open(TOPICS_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    ctas = cfg.get("cta_types") or []
    if not ctas or not all(isinstance(c, dict) and c.get("id") for c in ctas):
        print("[안내] topics.json 의 cta_types 가 옛날 형식이라 내장 댓글유도 8종을 사용합니다.")
        cfg["cta_types"] = DEFAULT_CTAS
    return cfg


def pick_topic(log, cfg):
    topics = cfg["topics"]
    return topics[log["count"] % len(topics)]


# ─────────────────────────────────────────────
# 0-1. 진단: 어느 계정에 올라가는지 / 글 주소 확인
# ─────────────────────────────────────────────
def check_account(log):
    """이 토큰이 실제로 연결된 스레드 계정을 확인해서 로그에 남긴다. 실패해도 게시는 계속한다."""
    try:
        res = http_json(THREADS_API + "/me?fields=id,username&access_token="
                        + urllib.parse.quote(ACCESS_TOKEN))
        username = res.get("username")
        id_match = str(res.get("id")) == str(USER_ID)
        print("▶ 이 토큰이 연결된 스레드 계정: @" + str(username)
              + " (THREADS_USER_ID 일치: " + ("예" if id_match else "아니오") + ")")
        log["account"] = {
            "username": username,
            "id_match": id_match,
            "checked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        }
    except Exception as e:
        print("계정 확인 실패 (게시는 계속 진행): " + str(e)[:200])
    return log


def fetch_permalink(post_id):
    """게시글을 바로 열 수 있는 주소를 가져온다."""
    res = http_json(THREADS_API + "/" + str(post_id) + "?fields=id,permalink&access_token="
                    + urllib.parse.quote(ACCESS_TOKEN))
    return res.get("permalink")


def update_permalinks(log):
    """최근 15개 글 중 주소가 없는 글에 주소를 채운다. 조회가 안 되면 '조회 실패'로 표시."""
    ok = 0
    fail = 0
    for post in log["posts"][-15:]:
        pid = post.get("post_id")
        if not pid or post.get("permalink"):
            continue
        try:
            link = fetch_permalink(pid)
            if link:
                post["permalink"] = link
                post.pop("check", None)
                ok += 1
            else:
                post["check"] = "조회 실패"
                fail += 1
        except Exception:
            # 오류 원문은 파일에 남기지 않는다 (공개 저장소이므로)
            post["check"] = "조회 실패"
            fail += 1
        time.sleep(1)
    if ok or fail:
        print("▶ 최근 글 주소 확인: 정상 " + str(ok) + "건 / 조회 실패 " + str(fail) + "건")
    return log


# ─────────────────────────────────────────────
# 1. 조회수 수집 (훅 성과 학습용)
# ─────────────────────────────────────────────
def fetch_insights(post_id):
    """Threads Insights API 로 조회수(views)와 댓글 수(replies)를 가져온다."""
    url = (THREADS_API + "/" + str(post_id) + "/insights"
           + "?metric=views,replies&access_token=" + urllib.parse.quote(ACCESS_TOKEN))
    res = http_json(url)
    out = {}
    for item in res.get("data", []):
        name = item.get("name")
        if name not in ("views", "replies"):
            continue
        # 응답 형태가 두 가지라 둘 다 대응
        if "total_value" in item:
            out[name] = int(item["total_value"].get("value", 0))
        else:
            values = item.get("values") or []
            if values:
                out[name] = int(values[0].get("value", 0))
    return out


def update_views(log):
    """최근 15개 게시물의 조회수·댓글 수를 갱신한다."""
    updated = 0
    for post in log["posts"][-15:]:
        pid = post.get("post_id")
        if not pid:
            continue
        # 이미 수집했고, 게시 후 3일이 지났으면 더 안 봐도 됨(숫자 거의 고정)
        if (post.get("views") is not None and post.get("views_final")
                and post.get("replies") is not None):
            continue
        try:
            ins = fetch_insights(pid)
        except Exception as e:
            print("조회수·댓글 수집 건너뜀 (" + str(pid) + "): " + str(e))
            continue
        if not ins:
            continue
        if "views" in ins:
            post["views"] = ins["views"]
        if "replies" in ins:
            post["replies"] = ins["replies"]
        try:
            posted_at = datetime.strptime(post["at"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            if (datetime.now(KST) - posted_at).days >= 3:
                post["views_final"] = True
        except Exception:
            pass
        updated += 1
        time.sleep(1)
    if updated:
        print("조회수·댓글 수 " + str(updated) + "건 갱신 완료")
    return log


# ─────────────────────────────────────────────
# 2. 훅 유형 선택 (성과 기반 + 실험)
# ─────────────────────────────────────────────
def hook_stats(log, hook_ids):
    """훅 유형별 (표본수, 평균 조회수)"""
    stats = {}
    for hid in hook_ids:
        stats[hid] = {"n": 0, "sum": 0}
    for post in log["posts"]:
        hid = post.get("hook")
        v = post.get("views")
        if hid in stats and isinstance(v, int):
            stats[hid]["n"] += 1
            stats[hid]["sum"] += v
    for hid in stats:
        n = stats[hid]["n"]
        stats[hid]["avg"] = (stats[hid]["sum"] / n) if n else 0.0
    return stats


def pick_hook(log, cfg):
    hooks = cfg["hook_types"]
    hook_ids = [h["id"] for h in hooks]
    stats = hook_stats(log, hook_ids)

    recent_hooks = [p.get("hook") for p in log["posts"][-3:]]  # 직전 3회와 같은 훅 금지
    candidates = [h for h in hooks if h["id"] not in recent_hooks] or hooks

    # (1) 표본이 부족한 훅이 있으면 그것부터 실험
    untested = [h for h in candidates if stats[h["id"]]["n"] < MIN_SAMPLES]
    if untested:
        chosen = random.choice(untested)
        return chosen, "실험(표본부족)", stats

    # (2) 25% 확률로 무작위 실험
    if random.random() < EXPLORE_RATE:
        chosen = random.choice(candidates)
        return chosen, "실험(랜덤)", stats

    # (3) 나머지는 평균 조회수 가중 추첨
    weights = [max(stats[h["id"]]["avg"], 1.0) ** 2 for h in candidates]
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    return chosen, "성과기반", stats


def cta_stats(log, cta_ids):
    """댓글 유도 방식별 (표본수, 평균 댓글 수)"""
    stats = {cid: {"n": 0, "sum": 0, "avg": 0.0} for cid in cta_ids}
    for post in log["posts"]:
        cid = post.get("cta")
        r = post.get("replies")
        if cid in stats and isinstance(r, int):
            stats[cid]["n"] += 1
            stats[cid]["sum"] += r
    for cid in stats:
        n = stats[cid]["n"]
        stats[cid]["avg"] = (stats[cid]["sum"] / n) if n else 0.0
    return stats


def pick_cta(log, cfg):
    ctas = cfg["cta_types"]
    stats = cta_stats(log, [c["id"] for c in ctas])
    recent = [p.get("cta") for p in log["posts"][-3:]]  # 직전 3회와 같은 방식 금지
    pool = [c for c in ctas if c["id"] not in recent] or ctas

    untested = [c for c in pool if stats[c["id"]]["n"] < MIN_SAMPLES]
    if untested:
        return random.choice(untested), "실험(표본부족)", stats
    if random.random() < CTA_EXPLORE_RATE:
        return random.choice(pool), "실험(랜덤)", stats
    # 댓글이 0개인 방식도 가끔은 뽑히도록 +0.5
    weights = [(stats[c["id"]]["avg"] + 0.5) ** 2 for c in pool]
    return random.choices(pool, weights=weights, k=1)[0], "댓글성과기반", stats


# ─────────────────────────────────────────────
# 3. 글 생성 + 품질 검증
# ─────────────────────────────────────────────
def build_system_prompt(cfg, hook, cta):
    cta_samples = " / ".join(cta.get("samples", []))
    L = cfg["length"]
    hashtags = " ".join(cfg["hashtags"])
    banned = ", ".join(cfg["banned_phrases"])
    samples = " / ".join(hook["samples"])

    return (
        "당신은 대한민국 스레드(Threads)에서 조회수가 터지는 글을 쓰는 20대 SNS 크리에이터입니다.\n"
        "전북 진안군 마이산 근처의 '" + cfg["brand_name"] + "'을 홍보하는 글을 씁니다.\n\n"
        "호텔 기본 정보:\n" + cfg["brand_info"] + "\n\n"
        "【이번 글의 훅 유형 — 반드시 이 방식으로만 시작】\n"
        "유형: " + hook["name"] + "\n"
        "규칙: " + hook["rule"] + "\n"
        "느낌 예시(그대로 베끼지 말고 톤만 참고): " + samples + "\n\n"
        "【첫 줄 규칙 — 가장 중요】\n"
        "- 스레드는 첫 줄만 미리보기로 보인다. 첫 줄에서 손가락을 멈추게 못 하면 조회수는 0이다.\n"
        "- 첫 줄은 공백 포함 " + str(L["first_line_max"]) + "자 이내, 한 문장.\n"
        "- 첫 줄에 장소명·호텔명·해시태그를 넣지 말 것. 정보가 아니라 '감정·궁금증'을 던진다.\n"
        "- 첫 줄은 결론을 말하지 말고, 결론을 궁금하게 만든 채로 끊는다.\n\n"
        "【전체 구조】\n"
        "1) 첫 줄: 위 훅 유형대로 한 방.\n"
        "2) 본문: 훅에서 던진 궁금증을 실제 경험담으로 풀어준다. 구체적인 장면 하나, 구체적인 숫자 하나는 꼭 넣는다.\n"
        "3) 댓글 유도 문장 1개 — 본문과 줄을 바꿔서 따로 한 줄로 쓴다 (아래 【댓글 유도】 참고).\n"
        "4) 맨 마지막 줄에 해시태그 2~3개. 후보: " + hashtags + "\n\n"
        "【댓글 유도 — 조회수만큼 중요하다. 댓글이 달려야 글이 퍼진다】\n"
        "- 이번 회차 방식: " + cta["name"] + "\n"
        "- 규칙: " + cta["rule"] + "\n"
        "- 느낌 예시(그대로 베끼지 말고 이 글 내용에 맞게 새로 쓸 것): " + cta_samples + "\n"
        "- 읽는 사람이 '지금 그 자리에서', '자기 경험이나 취향으로', '1초 안에' 답할 수 있어야 한다.\n"
        "- 읽는 사람이 이 장소에 가봤다거나 오늘 뭘 했다고 가정하는 질문 금지 (예: '오늘 별 보면서 떠오른 단어' X).\n"
        "- '어떠세요', '어때?', '궁금하다', '의견 남겨줘' 처럼 막연한 질문 금지. 선택지나 답 형식을 정해준다.\n"
        "- 공백 포함 50자 이내, 반말, 존댓말 금지.\n"
        "- 본문 끝에 일부러 한 가지를 말하지 않고 남겨두거나, 갈릴 만한 내 의견을 하나 넣으면 댓글이 더 달린다.\n\n"
        "【분량 — 반드시 지킬 것】\n"
        "- 공백 포함 " + str(L["min"]) + "자 이상 " + str(L["max"]) + "자 이내. 짧고 빠르게 읽히는 게 핵심이다.\n"
        "- 곁가지 설명, 미사여구, 마무리 인사 전부 삭제하고 알맹이만 남긴다.\n\n"
        "【편집 규칙 — 광고 티 제거】\n"
        "- 문장마다 줄바꿈하는 광고식 편집 절대 금지. 사람이 폰으로 쭉 쓴 글처럼 보여야 한다.\n"
        "- 줄바꿈은 이야기 흐름이 바뀔 때만. 빈 줄은 글 전체에서 최대 1번.\n"
        "- 해시태그 줄 포함해서 전체가 2~4덩어리로만 보이게 한다.\n\n"
        "【말투】\n"
        "- 친구한테 카톡 보내듯 편한 반말. 구어체 허용(근데, 아 그리고, ㄹㅇ, 진짜).\n"
        "- 유행어는 1~2개까지만. 도배하면 오히려 없어 보인다.\n"
        "- 이모지 1~2개.\n\n"
        "【절대 금지】\n"
        "- 다음 표현 사용 금지: " + banned + "\n"
        "- 과장 광고, 노골적인 예약 유도, 존댓말 공지 톤\n"
        "- 최근 게시글과 비슷한 소재·문장·훅·마무리 반복\n"
        "- 본문 외의 설명, 따옴표, 머리말 출력 금지. 게시글 본문만 출력한다."
    )


def call_claude(system, user):
    # 2026-10-15 수정: 모델이 '생각(thinking)'에 토큰 1500개를 전부 써버려서
    # 본문이 비는 문제 → ① 생각 기능을 끄고 ② 토큰 한도를 넉넉히 올림.
    # 만약 API가 '생각 끄기' 옵션을 거부하면(400 오류) 그 옵션만 빼고 자동 재시도.
    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 8000,
        "temperature": 1.0,
        "thinking": {"type": "disabled"},
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    body = json.dumps(payload).encode()

    data = None
    for attempt in range(1, API_RETRY + 1):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                data = json.loads(res.read().decode())
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # 429(속도제한)·5xx(서버 과부하)만 잠깐 쉬었다가 재시도, 그 외는 바로 실패
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < API_RETRY:
                print("Claude API 일시 오류 HTTP " + str(e.code) + " → 20초 후 재시도 ("
                      + str(attempt) + "/" + str(API_RETRY) + ")")
                time.sleep(20)
                continue
            if e.code == 400 and "thinking" in payload and "thinking" in detail:
                print("  'thinking 끄기' 옵션이 거부됨 → 옵션 빼고 다시 요청")
                payload.pop("thinking", None)
                body = json.dumps(payload).encode()
                continue
            raise RuntimeError("Claude API 오류 HTTP " + str(e.code) + ": " + detail) from e
        except urllib.error.URLError as e:
            if attempt < API_RETRY:
                print("Claude API 연결 실패 → 20초 후 재시도 (" + str(attempt) + "/" + str(API_RETRY) + ")")
                time.sleep(20)
                continue
            raise RuntimeError("Claude API 연결 실패: " + str(e)) from e

    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    if not text:
        # 원인 추적용: 왜 비었는지 실행 로그에 남긴다
        print("  ※ Claude 빈 응답 — stop_reason=" + str(data.get("stop_reason"))
              + ", 블록=" + str([b.get("type") for b in blocks])
              + ", 출력토큰=" + str((data.get("usage") or {}).get("output_tokens")))
    return text


CTA_VAGUE = ["어떠세요", "어때요", "어떨까", "궁금하다", "궁금함", "의견 남겨", "생각 남겨", "여러분"]
CTA_ASSUME = ["오늘 ", "다녀온 사람", "가본 사람만", "묵어본"]
CTA_CUES = ["?", "골라", "고름", "A/B", "1번", "2번", "번호", "숫자", "ㅇㅇ", "손", "태그",
            "알려", "한 단어", "___", "반박", "아니라는", "댓글", "한 명", "하나만", "말해"]


def cta_problems(chunks):
    """해시태그 줄 바로 앞 줄 = 댓글 유도 문장. 규칙에 맞는지 검사한다."""
    body = [ln.strip() for ln in chunks if not re.fullmatch(r"(#[^\s#]+\s*)+", ln.strip())]
    if len(body) < 2:
        return ["댓글 유도 문장이 본문과 따로 한 줄로 없음"]
    cta = body[-1]
    probs = []
    if len(cta) > 60:
        probs.append("댓글 유도 문장이 본문에 붙어 있거나 김 (" + str(len(cta)) + "자) — 따로 한 줄, 50자 이내")
    if "#" in cta:
        probs.append("댓글 유도 문장에 해시태그가 섞임")
    for w in CTA_VAGUE:
        if w in cta:
            probs.append("댓글 유도가 막연함: " + w)
    for w in CTA_ASSUME:
        if w in cta:
            probs.append("댓글 유도가 읽는 사람이 이미 가봤다/오늘 했다고 가정함: " + w.strip())
    if not any(c in cta for c in CTA_CUES):
        probs.append("댓글 유도 문장에 답하는 방법(질문·선택지·손·태그 등)이 없음")
    return probs


def validate(text, cfg):
    """글이 규칙을 지켰는지 검사. 통과하면 빈 리스트, 아니면 문제 목록을 돌려준다."""
    L = cfg["length"]
    problems = []

    if not text:
        return ["빈 글"]

    if len(text) > L["max"]:
        problems.append("너무 김 (" + str(len(text)) + "자 > " + str(L["max"]) + "자)")
    if len(text) < L["min"]:
        problems.append("너무 짧음 (" + str(len(text)) + "자 < " + str(L["min"]) + "자)")

    lines = [ln for ln in text.split("\n")]
    first = lines[0].strip()
    if len(first) > L["first_line_max"]:
        problems.append("첫 줄이 김 (" + str(len(first)) + "자)")
    if first.startswith("#") or "#" in first:
        problems.append("첫 줄에 해시태그")

    # 빈 줄(연속 개행) 최대 1번
    blank = len(re.findall(r"\n\s*\n", text))
    if blank > 1:
        problems.append("빈 줄 " + str(blank) + "개 (최대 1개)")

    # 내용이 있는 줄 = 덩어리 수. 해시태그 줄 포함 4줄 이내
    chunks = [ln for ln in lines if ln.strip()]
    if len(chunks) > 4:
        problems.append("줄 덩어리 " + str(len(chunks)) + "개 (최대 4개) — 광고식 편집")

    for bad in cfg["banned_phrases"]:
        if bad in text:
            problems.append("금지 표현 사용: " + bad)

    problems += cta_problems(chunks)

    tags = re.findall(r"#[^\s#]+", text)
    if len(tags) < 2 or len(tags) > 3:
        problems.append("해시태그 " + str(len(tags)) + "개 (2~3개여야 함)")

    if text.startswith('"') or text.startswith("'"):
        problems.append("따옴표로 시작")

    return problems


def generate_post(topic, cfg, log, hook, cta):
    # 과거에 빈 글로 기록된 회차는 참고 목록에서 뺀다
    recent = [p["text"] for p in log["posts"][-8:] if p.get("text")][-6:]
    recent_block = "\n---\n".join(recent) if recent else "(없음)"

    system = build_system_prompt(cfg, hook, cta)
    base_user = (
        "오늘의 소재: " + topic + "\n\n"
        "최근 게시글 (이것과 겹치지 않게):\n" + recent_block + "\n\n"
        "위 소재로 스레드 게시글 본문만 출력해 주세요."
    )

    best = None
    user = base_user
    attempt = 0
    empty_left = EMPTY_RETRY
    while attempt < MAX_RETRY:
        text = call_claude(system, user)
        # 혹시 따옴표로 감싸서 오면 벗겨낸다
        text = text.strip().strip('"').strip("'").strip()

        # 빈 응답은 후보로 치지 않는다. 기본 요청으로 되돌려 다시 받는다.
        if not text:
            if empty_left <= 0:
                print("빈 응답이 계속됨 → 재요청 중단")
                break
            empty_left -= 1
            print("빈 응답 → 5초 후 다시 요청 (남은 재요청 " + str(empty_left) + "회)")
            user = base_user
            time.sleep(5)
            continue

        attempt += 1
        problems = validate(text, cfg)
        print("생성 " + str(attempt) + "회차: " + str(len(text)) + "자, 문제 " + str(len(problems)) + "건")
        if not problems:
            return text
        print("  → " + " / ".join(problems))
        if best is None or len(problems) < len(best[1]):
            best = (text, problems)
        # 반려된 글을 먼저 보여주고, 맨 끝은 반드시 '다시 써라'는 지시로 끝낸다
        user = (
            base_user + "\n\n"
            "[직전 시도가 반려되었습니다]\n"
            "반려된 글:\n" + text + "\n\n"
            "반려 사유:\n"
            "- " + "\n- ".join(problems) + "\n\n"
            "위 사유를 전부 고쳐서 게시글 본문을 처음부터 다시 써서 본문만 출력하세요."
        )

    # 쓸 수 있는 글이 하나도 없으면 사진만 올리지 않고 이번 회차를 실패 처리한다
    if best is None:
        raise RuntimeError(
            "글 생성 실패: Claude 가 빈 응답만 돌려주었습니다. "
            "사진만 올라가는 것을 막기 위해 이번 회차는 게시하지 않습니다."
        )

    # 규칙을 완전히 통과한 글이 없으면 그나마 문제 적은 글을 쓰되 길이만 강제로 자른다
    text = best[0]
    print("규칙 완전 통과 실패 → 최선안 사용 후 길이 보정")
    if len(text) > cfg["length"]["max"]:
        text = text[: cfg["length"]["max"]].rstrip()
    return text


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
            "images/ 폴더에 이미지가 " + str(len(files)) + "장뿐입니다. 최소 "
            + str(IMAGE_COUNT) + "장이 필요합니다."
        )
    chosen = random.sample(files, IMAGE_COUNT)
    urls = [
        "https://raw.githubusercontent.com/" + REPO + "/" + BRANCH + "/"
        + IMAGE_DIR + "/" + urllib.parse.quote(f)
        for f in chosen
    ]
    return chosen, urls


# ─────────────────────────────────────────────
# 5. Threads 캐러셀 게시
# ─────────────────────────────────────────────
def post_to_threads(text, image_urls):
    # 마지막 안전장치: 글이 비어 있으면 절대 게시하지 않는다
    if not text or not text.strip():
        raise RuntimeError("게시 중단: 글 내용이 비어 있습니다.")

    child_ids = []
    for url in image_urls:
        res = http_json(THREADS_API + "/" + USER_ID + "/threads", {
            "media_type": "IMAGE",
            "image_url": url,
            "is_carousel_item": "true",
            "access_token": ACCESS_TOKEN,
        })
        child_ids.append(res["id"])
        time.sleep(3)

    res = http_json(THREADS_API + "/" + USER_ID + "/threads", {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "text": text,
        "access_token": ACCESS_TOKEN,
    })
    creation_id = res["id"]

    time.sleep(35)
    res = http_json(THREADS_API + "/" + USER_ID + "/threads_publish", {
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
        if days < 40:
            return None

    try:
        res = http_json(
            "https://graph.threads.net/refresh_access_token"
            "?grant_type=th_refresh_token&access_token=" + urllib.parse.quote(ACCESS_TOKEN)
        )
        new_token = res["access_token"]
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"refreshed_at": datetime.now(timezone.utc).isoformat()}, f)
        print("토큰이 갱신되었습니다.")
        return new_token
    except Exception as e:
        print("토큰 갱신 실패 (다음 실행에서 재시도): " + str(e))
        return None


def update_github_secret(new_token):
    pat = os.environ.get("GH_PAT")
    if not pat or not new_token:
        if new_token:
            print("GH_PAT 미설정: GitHub Secrets 의 THREADS_ACCESS_TOKEN 을 수동으로 교체해 주세요.")
        return
    try:
        from base64 import b64encode
        from nacl import encoding, public

        def gh_api(path, method="GET", body=None):
            req = urllib.request.Request(
                "https://api.github.com" + path,
                data=json.dumps(body).encode() if body else None,
                method=method,
                headers={
                    "Authorization": "Bearer " + pat,
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read().decode()
                return json.loads(raw) if raw else {}

        key = gh_api("/repos/" + REPO + "/actions/secrets/public-key")
        pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pk).encrypt(new_token.encode())
        gh_api(
            "/repos/" + REPO + "/actions/secrets/THREADS_ACCESS_TOKEN",
            method="PUT",
            body={"encrypted_value": b64encode(sealed).decode(), "key_id": key["key_id"]},
        )
        print("새 토큰이 GitHub Secrets 에 자동 저장되었습니다.")
    except Exception as e:
        print("Secrets 자동 저장 실패, 수동 교체 필요: " + str(e))


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    cfg = load_cfg()
    log = load_log()

    # 0) 진단: 어느 계정에 올라가는지, 최근 글이 실제로 살아 있는지 확인
    log = check_account(log)
    log = update_permalinks(log)

    # 1) 지난 글들의 조회수부터 수집 (훅 성과 학습 재료)
    log = update_views(log)

    # 2) 이번 회차 재료 선택
    topic = pick_topic(log, cfg)
    hook, reason, stats = pick_hook(log, cfg)
    cta, cta_reason, cstats = pick_cta(log, cfg)

    print("이번 회차 소재: " + topic)
    print("이번 회차 훅: " + hook["name"] + " [" + hook["id"] + "] — 선택근거: " + reason)
    print("이번 회차 댓글유도: " + cta["name"] + " [" + cta["id"] + "] — 선택근거: " + cta_reason)
    print("훅별 성적표 (표본수 / 평균조회수):")
    for h in cfg["hook_types"]:
        s = stats[h["id"]]
        print("  - " + h["id"].ljust(9) + " n=" + str(s["n"]) + " avg=" + str(round(s["avg"], 1)))
    print("댓글유도별 성적표 (표본수 / 평균댓글수):")
    for c in cfg["cta_types"]:
        s = cstats[c["id"]]
        print("  - " + c["id"].ljust(9) + " n=" + str(s["n"]) + " avg=" + str(round(s["avg"], 2)))

    # 3) 글 생성
    text = generate_post(topic, cfg, log, hook, cta)
    print("\n최종 글 (" + str(len(text)) + "자):\n" + text + "\n")

    # 4) 이미지 + 게시
    chosen, urls = pick_images()
    print("선택된 이미지: " + str(chosen))
    post_id = post_to_threads(text, urls)
    print("게시 완료! post id = " + str(post_id))

    # 방금 올린 글의 주소 확인 (실패해도 넘어간다)
    permalink = None
    try:
        time.sleep(5)
        permalink = fetch_permalink(post_id)
        print("▶ 게시글 주소: " + str(permalink))
    except Exception as e:
        print("게시글 주소 확인 실패: " + str(e)[:200])

    # 5) 로그 저장
    log["count"] += 1
    log["posts"].append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "topic": topic,
        "hook": hook["id"],
        "cta": cta["id"],
        "text": text,
        "len": len(text),
        "images": chosen,
        "post_id": post_id,
        "permalink": permalink,
        "views": None,
        "replies": None,
        "views_final": False,
    })
    log["posts"] = log["posts"][-60:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    # 6) 토큰 갱신 체크
    new_token = refresh_token_if_needed()
    update_github_secret(new_token)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("실행 실패: " + str(e), file=sys.stderr)
        sys.exit(1)
