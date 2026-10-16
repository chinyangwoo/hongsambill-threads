# -*- coding: utf-8 -*-
"""
홍삼빌호텔 스레드 '댓글 자동 답글' 앱  v1.0 (2026-10-15)
==============================================================
하는 일
  1. 최근 7일 동안 올린 글(posted_log.json)에 달린 댓글을 모은다.
  2. 아직 답하지 않은 댓글마다 Claude 가 답글이 필요한지 판단하고, 필요하면 짧은 답글을 쓴다.
     - 일반 댓글        → 짧고 친근한 답글 (가끔 가벼운 질문으로 대화를 이어감)
     - 예약·문의 댓글   → 확인된 정보(대표번호 등)로만 답함, 모르는 건 지어내지 않음
     - 불만·항의·민감   → 자동 답글을 달지 않고 '사람 확인 필요'로 남김
     - 스팸·광고·욕설   → 무시
  3. 답글을 게시하고 replied_log.json 에 기록해서 같은 댓글에 두 번 답하지 않는다.

안전장치
  - 한 번 실행에 최대 8개까지만 답글 (계정이 기계처럼 보이지 않게)
  - 같은 사람과 한 글에서 최대 2번까지만 주고받음 (끝없는 대화 방지)
  - 답글은 120자 이내, 해시태그·링크·가격 금액 금지 → 어기면 다시 쓰게 하고, 그래도 안 되면 건너뜀
  - 시험 모드(REPLY_DRY_RUN=1): 답글을 만들어서 로그에만 보여주고 실제로 게시하지 않음

필요한 권한 (Meta 앱): threads_basic, threads_read_replies, threads_manage_replies
필요한 환경변수 (GitHub Secrets): ANTHROPIC_API_KEY / THREADS_ACCESS_TOKEN / THREADS_USER_ID
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# 설정 (여기 숫자·문구만 바꾸면 됩니다)
# ─────────────────────────────────────────────
THREADS_API = "https://graph.threads.net/v1.0"
POST_LOG_FILE = "posted_log.json"     # 게시 앱이 남기는 기록 (읽기만 함)
REPLY_LOG_FILE = "replied_log.json"   # 이 앱이 남기는 기록
TOPICS_FILE = "topics.json"

LOOKBACK_DAYS = 7               # 며칠 전 글까지 댓글을 확인할지
MAX_REPLIES_PER_RUN = 8         # 한 번 실행에 최대 답글 수
MAX_TURNS_PER_PERSON = 2        # 같은 사람과 한 글에서 주고받는 최대 횟수
PUBLISH_WAIT = 30               # 답글 준비 후 게시까지 대기(초) — Meta 권장 30초
REPLY_MAX_LEN = 120             # 답글 최대 글자 수
SKIP_HIDE_STATUS = {"HIDDEN", "BLOCKED", "RESTRICTED", "COVERED"}

# 답글에서 써도 되는 '확인된 정보'. 여기에 없는 가격·시간·이벤트는 절대 지어내지 않게 한다.
# (전화번호 등을 바꾸려면 이 부분만 수정)
REPLY_FACTS = """- 호텔명: 홍삼빌호텔 (전북 진안군, 마이산 도보권). 객실 40개, 가족 손님이 많음.
- 예약·문의 대표번호: 1661-3889
- 같은 운영사의 홍삼스파는 2027년 1월 오픈 예정 (지금은 이용 불가)
- 2층 테라스는 화단으로 쓰여 이용 불가 — 언급하지 않음
- 바베큐장 있음: 부스 3개(부스당 4~6명). 호텔은 장소·설비만 제공, 고기·식자재는 손님이 직접 준비
- 마스코트: 총지배견 허스키 '천둥이' (소나무 그늘 아래 견사에서 지냄)
- 구글 리뷰 4.6점
- 마이산 탑사·은수사, 봄 벚꽃길, 가을 단풍이 대표 볼거리"""

REPLY_TONE = (
    "홍삼빌호텔 공식 계정을 운영하는 20~30대 직원 느낌. 짧고 친근한 '~요'체 존댓말. "
    "ㅋㅋ, ㅠㅠ 같은 표현은 자연스러우면 1번까지. 이모지는 0~1개. 딱딱한 고객센터 말투 금지."
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN", "")
USER_ID = os.environ.get("THREADS_USER_ID", "")
DRY_RUN = os.environ.get("REPLY_DRY_RUN", "0") == "1"
KST = timezone(timedelta(hours=9))

DEFAULT_BANNED = ["예약 문의", "지금 바로 예약", "최고의", "최상의", "완벽한 서비스",
                  "강력 추천드립니다", "많은 관심 부탁", "여러분", "방문해 보세요"]


class PermissionProblem(Exception):
    pass


def check_env():
    missing = [n for n, v in (("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
                              ("THREADS_ACCESS_TOKEN", ACCESS_TOKEN),
                              ("THREADS_USER_ID", USER_ID)) if not v]
    if missing:
        raise RuntimeError("GitHub Secrets 가 비어 있습니다: " + ", ".join(missing))


def http_json(url, data=None):
    if data is not None:
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        safe_url = url.replace(urllib.parse.quote(ACCESS_TOKEN), "***").replace(ACCESS_TOKEN, "***")
        low = body.lower()
        if "permission" in low or '"code":10' in low or "(#10)" in low or '"code":200' in low:
            raise PermissionProblem(body[:300])
        raise RuntimeError("HTTP " + str(e.code) + " 오류: " + safe_url + "\n응답: " + body[:300]) from e


# ─────────────────────────────────────────────
# 1. 기록 읽기 / 저장
# ─────────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(path + " 을 읽지 못해 새로 시작합니다: " + str(e))
    return default


def load_reply_log():
    log = load_json(REPLY_LOG_FILE, {})
    if not isinstance(log, dict):
        log = {}
    log.setdefault("handled", {})      # 댓글 id → 처리 결과(replied/skip/human)
    log.setdefault("needs_human", [])  # 사람이 직접 봐야 하는 댓글 목록
    log.setdefault("total_replied", 0)
    return log


def save_reply_log(log):
    # 오래된 기록은 정리 (최근 1500건만)
    items = list(log["handled"].items())[-1500:]
    log["handled"] = dict(items)
    log["needs_human"] = log["needs_human"][-50:]
    with open(REPLY_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def recent_posts():
    plog = load_json(POST_LOG_FILE, {})
    posts = plog.get("posts", []) if isinstance(plog, dict) else []
    cutoff = datetime.now(KST) - timedelta(days=LOOKBACK_DAYS)
    out = []
    for p in posts:
        if not p.get("post_id"):
            continue
        try:
            at = datetime.strptime(p["at"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except Exception:
            continue
        if at >= cutoff:
            out.append(p)
    return out


# ─────────────────────────────────────────────
# 2. 댓글 가져오기
# ─────────────────────────────────────────────
def fetch_conversation(post_id):
    fields = "id,text,username,timestamp,is_reply_owned_by_me,replied_to,hide_status"
    url = (THREADS_API + "/" + str(post_id) + "/conversation?fields=" + fields
           + "&reverse=false&access_token=" + urllib.parse.quote(ACCESS_TOKEN))
    items = []
    for _ in range(5):  # 최대 5페이지
        res = http_json(url)
        items += res.get("data", [])
        nxt = (res.get("paging") or {}).get("next")
        if not nxt:
            break
        url = nxt
    return items


def find_candidates(post, items, rlog):
    """답글을 달아야 할 댓글을 고른다."""
    root_id = str(post["post_id"])
    by_id = {str(i["id"]): i for i in items}
    mine = [i for i in items if i.get("is_reply_owned_by_me")]
    mine_ids = {str(i["id"]) for i in mine}
    # 내가 이미 답한 댓글 id
    answered = {str((i.get("replied_to") or {}).get("id")) for i in mine}

    # 사람별로 내가 몇 번 답했는지
    turns = {}
    for m in mine:
        target = by_id.get(str((m.get("replied_to") or {}).get("id")))
        who = (target or {}).get("username") or "?"
        turns[who] = turns.get(who, 0) + 1

    out = []
    for c in items:
        cid = str(c["id"])
        if c.get("is_reply_owned_by_me"):
            continue
        if cid in answered or cid in rlog["handled"]:
            continue
        if c.get("hide_status") in SKIP_HIDE_STATUS:
            continue
        if not (c.get("text") or "").strip():
            continue  # 사진·스티커만 있는 댓글
        parent = str((c.get("replied_to") or {}).get("id"))
        # 내 글에 직접 단 댓글, 또는 내 답글에 다시 단 댓글만 대상
        if parent != root_id and parent not in mine_ids:
            continue
        who = c.get("username") or "?"
        if turns.get(who, 0) >= MAX_TURNS_PER_PERSON:
            rlog["handled"][cid] = "skip:대화횟수초과"
            continue
        # 대화 흐름 (내 답글에 다시 단 댓글이면 앞 대화를 같이 넘김)
        chain = []
        cur = by_id.get(parent)
        while cur is not None and len(chain) < 4:
            chain.insert(0, ("나(호텔)" if cur.get("is_reply_owned_by_me") else "@" + str(cur.get("username") or "손님"))
                         + ": " + (cur.get("text") or ""))
            cur = by_id.get(str((cur.get("replied_to") or {}).get("id")))
        out.append({"comment": c, "chain": chain, "post": post})
    return out


# ─────────────────────────────────────────────
# 3. Claude 로 답글 쓰기
# ─────────────────────────────────────────────
def call_claude(system, user):
    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 4000,
        "temperature": 0.9,
        "thinking": {"type": "disabled"},
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    for attempt in range(1, 4):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                data = json.loads(res.read().decode())
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 400 and "thinking" in payload and "thinking" in detail:
                payload.pop("thinking", None)
                continue
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < 3:
                time.sleep(20)
                continue
            raise RuntimeError("Claude API 오류 HTTP " + str(e.code) + ": " + detail) from e
    return ""


def build_system(banned):
    return (
        "당신은 스레드(Threads)에서 홍삼빌호텔 공식 계정(@hongsamhotel)의 댓글 답글을 담당합니다.\n"
        "목표: 댓글 단 사람이 '답이 오네?' 하고 기분 좋아서 또 댓글을 달고 싶게 만드는 것.\n\n"
        "【말투】\n" + REPLY_TONE + "\n\n"
        "【써도 되는 확인된 정보 — 이것 말고는 절대 지어내지 말 것】\n" + REPLY_FACTS + "\n\n"
        "【답글 규칙】\n"
        "- " + str(REPLY_MAX_LEN) + "자 이내, 1~2문장. 댓글 내용을 콕 집어서 반응한다 (복붙 같은 답 금지).\n"
        "- 댓글이 A/B·숫자·한 단어 답이면 그 답에 진짜로 반응한다 (예: 'B 고르셨군요 ㅋㅋ 저도...').\n"
        "- 절반 정도는 끝에 가벼운 질문을 하나 붙여 대화를 이어간다. 단, 이미 두 번째 대화면 질문 없이 마무리.\n"
        "- 예약 유도·홍보 멘트 금지. 예약 방법을 직접 물어볼 때만 대표번호를 알려준다.\n"
        "- 가격·할인·이벤트·체크인 시간처럼 위 정보에 없는 걸 물으면 '대표번호로 물어보시면 정확하게 알려드려요' 식으로 안내.\n"
        "- 호텔이 음식·고기를 판매하거나 제공한다는 표현 금지. 홍삼 효능 주장 금지.\n"
        "- 해시태그·링크 금지. 다음 표현 금지: " + ", ".join(banned) + "\n\n"
        "【판단】\n"
        "- reply: 일반 댓글, 질문, 칭찬, 맞장구 → 답글을 쓴다\n"
        "- human: 불만·항의·환불·안전사고·위생·직원 태도 문제, 개인정보 요청, 애매하고 민감한 내용 → 답하지 않고 사람에게 넘김\n"
        "- skip: 스팸·광고·욕설·의미 없는 글자, 다른 사람끼리 나누는 대화\n\n"
        "【출력 형식 — 반드시 이 JSON 한 줄만 출력】\n"
        '{"action": "reply 또는 human 또는 skip", "reason": "판단 이유 짧게", "text": "답글 내용(reply일 때만)"}'
    )


def parse_json(raw):
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def reply_problems(text, banned):
    bad = []
    if not text:
        return ["빈 답글"]
    if len(text) > REPLY_MAX_LEN:
        bad.append("너무 김 (" + str(len(text)) + "자)")
    if "#" in text:
        bad.append("해시태그 사용")
    if re.search(r"https?://|www\.", text):
        bad.append("링크 사용")
    if re.search(r"\d[\d,]*\s*(원|만원|천원)", text):
        bad.append("가격 금액 언급 (확인되지 않은 정보)")
    for b in banned:
        if b in text:
            bad.append("금지 표현: " + b)
    return bad


def draft_reply(cand, system, banned):
    c = cand["comment"]
    user = (
        "[내가 올린 원글]\n" + (cand["post"].get("text") or "") + "\n\n"
        + ("[지금까지의 대화]\n" + "\n".join(cand["chain"]) + "\n\n" if len(cand["chain"]) > 1 else "")
        + "[이번에 답할 댓글] @" + str(c.get("username") or "손님") + ": " + c.get("text", "") + "\n\n"
        "판단하고 JSON 한 줄만 출력하세요."
    )
    for attempt in range(2):
        res = parse_json(call_claude(system, user))
        if not res or res.get("action") not in ("reply", "human", "skip"):
            continue
        if res["action"] != "reply":
            return res
        text = (res.get("text") or "").strip().strip('"').strip()
        probs = reply_problems(text, banned)
        if not probs:
            res["text"] = text
            return res
        user += ("\n\n[직전 답글이 반려됨: " + ", ".join(probs) + "]\n반려된 답글: " + text
                 + "\n고쳐서 다시 JSON 한 줄만 출력하세요.")
    return {"action": "skip", "reason": "규칙에 맞는 답글을 못 만듦"}


# ─────────────────────────────────────────────
# 4. 답글 게시
# ─────────────────────────────────────────────
def publish_reply(reply_to_id, text):
    res = http_json(THREADS_API + "/me/threads", {
        "media_type": "TEXT",
        "text": text,
        "reply_to_id": reply_to_id,
        "access_token": ACCESS_TOKEN,
    })
    time.sleep(PUBLISH_WAIT)
    res = http_json(THREADS_API + "/" + USER_ID + "/threads_publish", {
        "creation_id": res["id"],
        "access_token": ACCESS_TOKEN,
    })
    return res.get("id")


PERMISSION_GUIDE = """
❌ 댓글 읽기·답글 권한이 없습니다.
   → Meta 앱에 threads_read_replies, threads_manage_replies 권한을 추가한 뒤
     '새 토큰'을 발급받아 GitHub Secrets 의 THREADS_ACCESS_TOKEN 을 교체해야 합니다.
     (권한만 추가하고 옛 토큰을 그대로 쓰면 계속 이 오류가 납니다)
"""


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    print("=== 홍삼빌호텔 스레드 댓글 자동 답글 v1.0 ===")
    if DRY_RUN:
        print("※ 시험 모드: 답글을 만들어서 보여주기만 하고 실제로 게시하지 않습니다.")
    check_env()

    cfg = load_json(TOPICS_FILE, {})
    banned = cfg.get("banned_phrases") or DEFAULT_BANNED
    rlog = load_reply_log()
    posts = recent_posts()
    print("최근 " + str(LOOKBACK_DAYS) + "일 게시글 " + str(len(posts)) + "개 확인")

    candidates = []
    for p in posts:
        try:
            items = fetch_conversation(p["post_id"])
        except PermissionProblem as e:
            print(PERMISSION_GUIDE)
            print("   (Meta 응답: " + str(e)[:200] + ")")
            sys.exit(1)
        except Exception as e:
            print("  댓글 조회 실패 (" + str(p["post_id"]) + "): " + str(e)[:150])
            continue
        found = find_candidates(p, items, rlog)
        if items:
            print("  " + p["at"] + " 글: 댓글 " + str(len(items)) + "개 / 새로 답할 것 " + str(len(found)) + "개")
        candidates += found
        time.sleep(1)

    # 오래된 댓글부터
    candidates.sort(key=lambda x: x["comment"].get("timestamp") or "")
    if not candidates:
        print("새로 답할 댓글이 없습니다.")
        save_reply_log(rlog)
        return

    system = build_system(banned)
    done = 0
    for cand in candidates:
        if done >= MAX_REPLIES_PER_RUN:
            print("이번 실행 한도(" + str(MAX_REPLIES_PER_RUN) + "개) 도달 → 나머지는 다음 실행에서")
            break
        c = cand["comment"]
        cid = str(c["id"])
        print("\n▶ 댓글 @" + str(c.get("username") or "?") + ": " + (c.get("text") or "")[:60])
        try:
            res = draft_reply(cand, system, banned)
        except Exception as e:
            print("  답글 생성 실패 (다음 실행에서 재시도): " + str(e)[:150])
            continue
        action = res.get("action")
        print("  판단: " + str(action) + " — " + str(res.get("reason", ""))[:80])

        if action == "skip":
            if not DRY_RUN:
                rlog["handled"][cid] = "skip"
            continue
        if action == "human":
            print("  ⚠️ 사람 확인 필요 → 자동 답글 안 함. 원글: " + str(cand["post"].get("permalink") or ""))
            if not DRY_RUN:
                rlog["handled"][cid] = "human"
                rlog["needs_human"].append({
                    "comment_id": cid,
                    "post_link": cand["post"].get("permalink"),
                    "reason": str(res.get("reason", ""))[:80],
                    "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
                })
            continue

        print("  답글: " + res["text"])
        if DRY_RUN:
            done += 1
            continue
        try:
            rid = publish_reply(cid, res["text"])
            rlog["handled"][cid] = "replied"
            rlog["total_replied"] = rlog.get("total_replied", 0) + 1
            done += 1
            print("  ✅ 게시 완료 (" + str(rid) + ")")
        except PermissionProblem as e:
            print(PERMISSION_GUIDE)
            print("   (Meta 응답: " + str(e)[:200] + ")")
            save_reply_log(rlog)
            sys.exit(1)
        except Exception as e:
            print("  답글 게시 실패 (다음 실행에서 재시도): " + str(e)[:200])

    save_reply_log(rlog)
    print("\n=== 완료: 답글 " + str(done) + "개" + (" (시험 모드라 실제 게시 안 함)" if DRY_RUN else "") + " ===")
    if rlog["needs_human"]:
        print("※ 사람 확인이 필요한 댓글이 replied_log.json 의 needs_human 에 " + str(len(rlog["needs_human"])) + "건 있습니다.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print("\n❌ 실행 실패: " + str(e), file=sys.stderr)
        sys.exit(1)
