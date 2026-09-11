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
EXPLORE_RATE = 0.25      # 25%는 성과와 무관하게 새 훅 실험
MIN_SAMPLES = 2          # 훅 유형별 최소 표본 수(이하면 무조건 실험 대상)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
USER_ID = os.environ["THREADS_USER_ID"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
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
        raise RuntimeError("HTTP " + str(e.code) + " 오류: " + url + "\n응답: " + body) from e


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
        return json.load(f)


def pick_topic(log, cfg):
    topics = cfg["topics"]
    return topics[log["count"] % len(topics)]


# ─────────────────────────────────────────────
# 1. 조회수 수집 (훅 성과 학습용)
# ─────────────────────────────────────────────
def fetch_views(post_id):
    """Threads Insights API 로 해당 게시물의 조회수(views)를 가져온다."""
    url = (THREADS_API + "/" + str(post_id) + "/insights"
           + "?metric=views&access_token=" + urllib.parse.quote(ACCESS_TOKEN))
    res = http_json(url)
    for item in res.get("data", []):
        if item.get("name") != "views":
            continue
        # 응답 형태가 두 가지라 둘 다 대응
        if "total_value" in item:
            return int(item["total_value"].get("value", 0))
        values = item.get("values") or []
        if values:
            return int(values[0].get("value", 0))
    return None


def update_views(log):
    """최근 15개 게시물 중 조회수가 아직 없거나 오래되지 않은 건을 갱신한다."""
    updated = 0
    for post in log["posts"][-15:]:
        pid = post.get("post_id")
        if not pid:
            continue
        # 이미 수집했고, 게시 후 3일이 지났으면 더 안 봐도 됨(조회수 거의 고정)
        if post.get("views") is not None and post.get("views_final"):
            continue
        try:
            v = fetch_views(pid)
        except Exception as e:
            print("조회수 수집 건너뜀 (" + str(pid) + "): " + str(e))
            continue
        if v is None:
            continue
        post["views"] = v
        try:
            posted_at = datetime.strptime(post["at"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            if (datetime.now(KST) - posted_at).days >= 3:
                post["views_final"] = True
        except Exception:
            pass
        updated += 1
        time.sleep(1)
    if updated:
        print("조회수 " + str(updated) + "건 갱신 완료")
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


def pick_cta(log, cfg):
    ctas = cfg["cta_types"]
    recent = [p.get("cta") for p in log["posts"][-4:]]
    pool = [c for c in ctas if c not in recent] or ctas
    return random.choice(pool)


# ─────────────────────────────────────────────
# 3. 글 생성 + 품질 검증
# ─────────────────────────────────────────────
def build_system_prompt(cfg, hook, cta):
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
        "3) 마지막: 댓글 유도 장치 1개 — 이번 회차 지정 방식은 '" + cta + "' 이다. 이 방식으로만 마무리한다.\n"
        "4) 맨 마지막 줄에 해시태그 2~3개. 후보: " + hashtags + "\n\n"
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
    body = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 700,
        "temperature": 1.0,
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
    return "".join(b["text"] for b in data["content"] if b["type"] == "text").strip()


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

    tags = re.findall(r"#[^\s#]+", text)
    if len(tags) < 2 or len(tags) > 3:
        problems.append("해시태그 " + str(len(tags)) + "개 (2~3개여야 함)")

    if text.startswith('"') or text.startswith("'"):
        problems.append("따옴표로 시작")

    return problems


def generate_post(topic, cfg, log, hook, cta):
    recent = [p["text"] for p in log["posts"][-6:]]
    recent_block = "\n---\n".join(recent) if recent else "(없음)"

    system = build_system_prompt(cfg, hook, cta)
    base_user = (
        "오늘의 소재: " + topic + "\n\n"
        "최근 게시글 (이것과 겹치지 않게):\n" + recent_block + "\n\n"
        "위 소재로 스레드 게시글 본문만 출력해 주세요."
    )

    best = None
    user = base_user
    for attempt in range(1, MAX_RETRY + 1):
        text = call_claude(system, user)
        # 혹시 따옴표로 감싸서 오면 벗겨낸다
        text = text.strip().strip('"').strip("'").strip()
        problems = validate(text, cfg)
        print("생성 " + str(attempt) + "회차: " + str(len(text)) + "자, 문제 " + str(len(problems)) + "건")
        if not problems:
            return text
        print("  → " + " / ".join(problems))
        if best is None or len(problems) < len(best[1]):
            best = (text, problems)
        user = (
            base_user + "\n\n"
            "[직전 시도가 아래 문제로 반려되었습니다. 반드시 고쳐서 다시 쓰세요]\n"
            "- " + "\n- ".join(problems) + "\n\n"
            "반려된 글:\n" + text
        )

    # 3회 모두 실패하면 그나마 문제 적은 글을 쓰되 길이만 강제로 자른다
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

    # 1) 지난 글들의 조회수부터 수집 (훅 성과 학습 재료)
    log = update_views(log)

    # 2) 이번 회차 재료 선택
    topic = pick_topic(log, cfg)
    hook, reason, stats = pick_hook(log, cfg)
    cta = pick_cta(log, cfg)

    print("이번 회차 소재: " + topic)
    print("이번 회차 훅: " + hook["name"] + " [" + hook["id"] + "] — 선택근거: " + reason)
    print("이번 회차 댓글유도: " + cta)
    print("훅별 성적표 (표본수 / 평균조회수):")
    for h in cfg["hook_types"]:
        s = stats[h["id"]]
        print("  - " + h["id"].ljust(9) + " n=" + str(s["n"]) + " avg=" + str(round(s["avg"], 1)))

    # 3) 글 생성
    text = generate_post(topic, cfg, log, hook, cta)
    print("\n최종 글 (" + str(len(text)) + "자):\n" + text + "\n")

    # 4) 이미지 + 게시
    chosen, urls = pick_images()
    print("선택된 이미지: " + str(chosen))
    post_id = post_to_threads(text, urls)
    print("게시 완료! post id = " + str(post_id))

    # 5) 로그 저장
    log["count"] += 1
    log["posts"].append({
        "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "topic": topic,
        "hook": hook["id"],
        "cta": cta,
        "text": text,
        "len": len(text),
        "images": chosen,
        "post_id": post_id,
        "views": None,
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
