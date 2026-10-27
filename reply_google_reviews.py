# -*- coding: utf-8 -*-
"""
홍삼빌호텔 구글 리뷰 자동 답글
================================
매일 1회 구글 비즈니스 프로필 API 로 '아직 답글이 없는' 리뷰를 찾아,
Claude 가 총지배인이 직접 쓴 것처럼 답글을 쓰고 구글에 등록한다.

가장 중요한 원칙: AI 가 쓴 티가 나지 않을 것.
  - 짧고 구체적으로, 리뷰에 나온 내용 하나를 손님 표현 그대로 짚는다
  - 상투어·감성 비유·과한 감탄 금지 (BANNED 목록으로 검사, 걸리면 다시 쓰게 함)
  - 최근 답글과 첫 문장·표현이 겹치지 않게 최근 답글을 함께 보여 준다
  - 리뷰에 없는 사실, 호텔 사실 목록에 없는 약속은 쓰지 않는다

별점 규칙:
  - 4~5점: 자동 등록
  - 1~3점: 등록하지 않음 → 로그에 '수동 처리 필요'만 남김 (사람이 사정을 확인하고 답해야 함)

모드 (워크플로 수동 실행 입력 mode):
  reply   : 답글 작성 + 등록 (예약 실행은 항상 이 모드)
  preview : 답글을 써서 화면에만 출력, 등록하지 않음 (말투 확인용)
  list    : 답글 없는 리뷰 목록만 출력

필요한 GitHub Secrets (모두 이미 등록되어 있음):
  GBP_CLIENT_ID, GBP_CLIENT_SECRET, GBP_REFRESH_TOKEN, ANTHROPIC_API_KEY
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from fetch_google_reviews import (KST, access_token, find_locations, list_paged,
                                  original_comment, parse_time, GBP_REVIEWS, STAR)

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
MODE = (os.environ.get("MODE", "reply").strip().lower() or "reply")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REPLY_MODEL = os.environ.get("REPLY_MODEL", "claude-opus-5-5")
SIGNATURE = os.environ.get("REPLY_SIGNATURE", "홍삼빌호텔 총지배인 배상").strip()
AUTO_MIN_STARS = 4                 # 이 점수 이상만 자동 등록
WINDOW_DAYS = int(os.environ.get("REPLY_WINDOW_DAYS", "60"))   # 이 기간 안에 작성된 리뷰만 대상
MAX_PER_RUN = int(os.environ.get("REPLY_MAX_PER_RUN", "10"))
MAX_TRIES = 3                      # 검사 불합격 시 다시 쓰기 횟수
LOG_FILE = "gbp_reply_log.json"

# 호텔 사실 목록 — 답글에는 이 범위 안의 사실만 쓴다 (바뀌면 여기만 고치면 됨)
HOTEL_FACTS = """\
- 전북 진안 마이산 바로 아래, 진안 IC 가까이. 객실 43실, 가족 손님이 많음
- 마이산이 보이는 객실이 있음 (전 객실은 아님)
- 1층 로비 라운지: 커피·차·과자 무료, 워케이션 공간
- 무료 셀프 세탁실(세제 포함), 닌텐도 스위치 무료 대여, 객실 TV 로 OTT 무료 시청
- 조식은 별도 신청(07~09시). 소고기무국 한상, 샐러드 등 지역 식재료 사용
- 웰컴 드링크: 막걸리(성수주조장 딸기막걸리·쌀술), 콤부차
- 반려견 동반 객실 10실 운영, 펫 용품 비치, 야외 놀이 공간
- 바베큐장 16~22시 무료 이용 (숯은 판매, 도구 대여)
- 겨울 사우나(10월~3월): 호텔 앞 잔디밭 장작식 핀란드 배럴 사우나·텐트 사우나, 예약순
- 홍삼스파(옆 건물)는 현재 휴장 중. 재개장 날짜는 정해지지 않음 → 날짜를 말하지 말 것
- 주변: 마이산 탑사, 산약초타운 산책로, 돌담 정원"""

# 사람이 안 쓰는 상투 표현 — 하나라도 들어가면 다시 쓰게 한다
BANNED = [
    "소중한", "진심으로", "진심이", "여정", "선물 같", "선물같", "벅차", "화답", "힐링",
    "감동", "추억", "앞으로도 더욱", "최선을 다하", "다시 한번 감사", "다시 한 번 감사",
    "행복한 시간", "뜻깊", "따스한", "미소", "마음을 담아", "아낌없", "귀한 시간",
    "귀한 발걸음", "발걸음", "고객님의 따뜻한", "따뜻한 말씀", "따뜻한 후기", "정성 어린",
    "정성어린", "보람", "큰 힘이", "원동력", "기쁨", "영광", "만족스러운 시간",
    "편안한 휴식", "특별한 경험", "잊지 못할", "머무시는 동안", "더할 나위",
    "언제나 환영", "기억에 남", "설레", "빛나", "가득한", "함께해 주셔서", "해주셔서 감사합니다",
]
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐❤]")


# ─────────────────────────────────────────────
# 프롬프트
# ─────────────────────────────────────────────
SYSTEM = f"""너는 전북 진안 홍삼빌호텔의 총지배인이다. 매일 아침 휴대폰으로 구글 리뷰에 직접 답글을 단다.
50대 호텔리어이고, 손님 한 분 한 분을 기억하려는 사람이다. 말은 정중하고 단정하며, 꾸미지 않는다.

# 가장 중요한 것
손님이 읽었을 때 "총지배인이 내 리뷰를 직접 읽고 정중하게 답했구나" 싶어야 한다.
AI 나 홍보팀이 쓴 티가 조금이라도 나면 실패다.

# 형식 (시스템이 붙이는 부분 — 너는 쓰지 않는다)
- 맨 앞 호칭 "홍길* 고객님," 과 맨 끝 "감사합니다. / 홍삼빌호텔 총지배인 배상" 은 시스템이 붙인다.
- 그러니 본문에서 손님 이름을 부르지 말고, 본문 끝을 "감사합니다"로 맺지 말 것. 본문 안에 "감사"는 많아야 한 번.

# 쓰는 법
- 정중한 존댓말(합쇼체 "~습니다", "~겠습니다")을 기본으로 쓴다. 반말투·명령조("해 보세요", "들러 주세요")는 피하고
  "~해 보시길 권해 드립니다", "~다시 모실 수 있기를 바랍니다"처럼 공손하게 쓴다. "~셨네요"는 한 번 정도는 괜찮다.
- 리뷰에서 손님이 실제로 말한 것 한두 가지를 손님 표현 그대로 받아서 답한다. 나머지는 언급하지 않는다.
- 길이는 리뷰에 맞춘다. 한 줄 리뷰엔 2문장, 긴 리뷰엔 3~5문장.
- 문장은 짧게. 형용사·부사를 아낀다.
- 직원 이야기, 계절 이야기, 다음 방문 때 권할 만한 것 한 가지 정도는 자연스러우면 넣어도 된다.
- 마지막 문장은 다음 방문을 정중히 청하는 말이면 자연스럽다 ("다음에도 편히 모시겠습니다", "또 뵙기를 바랍니다" 등, 매번 다르게).
- 아쉬운 점을 말한 리뷰는 변명하지 말고, 무엇이 불편했는지 짚고 정중히 사과한 뒤 할 수 있는 만큼만 말한다.
  ("바로 고치겠다", "전 객실 교체" 같은 약속은 하지 않는다. "직원들과 확인해 보겠습니다" 정도.)
- 리뷰가 외국어면 본문은 그 언어로 쓴다(일본어 리뷰는 일본어, 영어 리뷰는 영어, 중국어 리뷰는 중국어).
  이때도 호칭·끝인사(Thank you 등)·서명은 시스템이 붙이니 본문만 쓴다.

# 절대 하지 말 것
- "소중한 리뷰", "진심으로 감사", "따뜻한 말씀", "여정", "선물 같은", "힐링", "감동", "추억", "행복한 시간",
  "최선을 다하겠습니다", "앞으로도 더욱", "다시 한번 감사" 같은 상투어, 감성 비유, 시적인 표현
- 리뷰 내용을 줄줄이 요약하거나 칭찬을 하나하나 되풀이하기
- 이모지, 해시태그, 느낌표 2개 이상, 줄표(—), 따옴표로 단어 강조
- 리뷰에 없는 손님 사정 지어내기, 아래 사실 목록에 없는 호텔 정보·날짜·가격·약속
- 홍보 문구, 예약 유도, 전화번호, 링크
- 호칭·서명·끝인사 "감사합니다" (시스템이 붙인다)

# 호텔 사실 (이 범위 안에서만 언급)
{HOTEL_FACTS}

# 이런 느낌이다 (그대로 베끼지 말 것)
리뷰 ★5 "친절했고 시설은 깨끗했어요 사우나도 넘 좋았어요"
답글: 사우나까지 이용해 보셨다니 반갑습니다. 날이 추워질수록 장작 사우나의 열기가 더 좋습니다. 겨울에 다시 모실 수 있기를 바랍니다.

리뷰 ★5 "깨끗하고 잘 정돈된 호텔"
답글: 깨끗하고 잘 정돈됐다고 봐 주셔서 감사드립니다. 객실을 정리하는 직원들에게 그대로 전하겠습니다. 다음에도 같은 모습으로 모시겠습니다.

리뷰 ★5 "마이산 일정이 갑자기 잡혀서 당일에 찾은 호텔. 온돌방 하나 남아 선택권 없이 잡았는데 기대 이상… 웰컴드링크, 세탁, 셀프카페… 산책로도 좋고, 스파가 휴장이라 아쉬움"
답글: 당일에 급히 잡으신 방이 마음에 드셨다니 다행입니다. 저녁 산책로까지 걸어 보셨다니 반갑습니다. 스파가 아직 쉬고 있어 아쉬우셨을 텐데, 다음에는 스파까지 함께 즐기실 수 있도록 준비하겠습니다.

리뷰 ★4 "깨끗하고 수건도 뽀송… 근데 주차장이 작고 방 창에 블라인드가 없어서 저녁에 신경 쓰여서 별 하나 뺐어요"
답글: 블라인드가 없어 저녁에 신경 쓰이셨다니 죄송합니다. 말씀하신 부분은 직원들과 객실별로 다시 살펴보겠습니다. 주차도 붐비는 날에는 안내에 더 신경 쓰겠습니다. 3대가 함께하신 여행에 불편을 드려 송구하며, 다음에는 더 편히 모시겠습니다.

리뷰 ★5 (내용 없음)
답글: 별점을 남겨 주셔서 감사드립니다. 다음에 오시면 로비에서 커피 한잔 여유롭게 드시고 가시길 권해 드립니다.

# 출력
답글 본문만 출력한다. 따옴표, 머리말, 설명, 호칭, 끝인사, 서명 없이."""


def build_user_prompt(rv, recent, feedback=None):
    lines = [
        f"별점: ★{rv['stars']}",
        f"작성일: {rv['created'].astimezone(KST).strftime('%Y-%m-%d') if rv['created'] else '모름'}",
        f"표시 이름: {rv['reviewer'] or '(없음)'}",
        f"리뷰 본문: {rv['text'] if rv['text'] else '(본문 없이 별점만 남김)'}",
    ]
    if recent:
        lines.append("\n# 최근에 단 답글들 (첫 문장·표현·마무리가 겹치지 않게 다르게 쓸 것)")
        lines += [f"- {r}" for r in recent]
    if feedback:
        lines.append(f"\n# 방금 쓴 답글의 문제 (고쳐서 다시 쓸 것)\n{feedback}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Claude 호출 · 검사
# ─────────────────────────────────────────────
def call_claude(system, user):
    body = json.dumps({
        "model": REPLY_MODEL,
        "max_tokens": 1200,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    for attempt in (1, 2, 3):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=180) as res:
                data = json.loads(res.read().decode())
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
        except Exception as e:
            print(f"⚠️ Claude 호출 실패({attempt}/3): {e}")
            time.sleep(5 * attempt)
    return ""


def clean(text):
    t = re.sub(r"^(답글|Reply)\s*[:：]\s*", "", text.strip())
    # 모델이 서명·끝인사·호칭을 붙였으면 떼어 낸다 (시스템이 정해진 형식으로 붙임)
    t = re.sub(r"\n+\s*[-—–]*\s*홍삼빌.*$", "", t, flags=re.S).strip()
    t = re.sub(r"\n+\s*Master\..*$", "", t, flags=re.S).strip()
    t = t.strip('"“”\'').strip()
    t = re.sub(r"^\S{1,20}\s*\*?\s*고객님\s*,?\s*\n?", "", t).strip()
    t = re.sub(r"\s*감사합니다[.!]?\s*$", "", t).strip()
    t = re.sub(r"\s*(Thank you( very much)?|ありがとうございました|谢谢)[.!。！]?\s*$", "", t, flags=re.I).strip()
    t = re.sub(r"^(Dear [^\n,]{1,40},|[^\n]{1,20}様|尊敬的[^\n，]{1,20}，)\s*\n?", "", t).strip()
    return t


def is_korean(s):
    return len(re.findall(r"[가-힣]", s)) >= max(3, len(s) * 0.2)


def check(reply, rv):
    """문제점 목록을 돌려준다. 빈 목록이면 통과."""
    probs = []
    if not reply:
        return ["답글이 비어 있음"]
    hit = [w for w in BANNED if w in reply]
    if hit:
        probs.append(f"상투 표현 사용: {', '.join(hit)} → 다른 평범한 말로 바꿀 것")
    if EMOJI_RE.search(reply):
        probs.append("이모지 사용 금지")
    if "#" in reply:
        probs.append("해시태그 금지")
    if reply.count("!") > 1:
        probs.append("느낌표는 많아야 1개")
    if "—" in reply or "–" in reply:
        probs.append("줄표(—) 금지")
    if "*" in reply:
        probs.append("별표(*) 금지")
    if re.search(r"\d{2,4}-\d{3,4}-\d{4}|https?://|www\.", reply):
        probs.append("전화번호·링크 금지")
    if re.search(r"\bAI\b|인공지능|ChatGPT|Claude", reply, re.I):
        probs.append("AI 언급 금지")
    # 길이: 리뷰보다 길면 안 됨 (짧은 리뷰도 최소 한두 문장은 허용)
    limit = max(140, int(len(rv["text"]) * 1.2)) if rv["text"] else 110
    limit = min(limit, 450)
    if len(reply) > limit:
        probs.append(f"너무 김 ({len(reply)}자) → {limit}자 이내로 줄일 것")
    if rv["text"] and is_korean(rv["text"]) and not is_korean(reply):
        probs.append("한국어 리뷰에는 한국어로 답할 것")
    return probs


def write_reply(rv, recent):
    feedback, reply = None, ""
    for n in range(1, MAX_TRIES + 1):
        reply = clean(call_claude(SYSTEM, build_user_prompt(rv, recent, feedback)))
        probs = check(reply, rv)
        if not probs:
            return reply, n
        feedback = "\n".join(f"- {p}" for p in probs)
        print(f"   ↺ {n}회차 불합격: {'; '.join(probs)}")
    return None, MAX_TRIES


def masked_name(name):
    """대표님이 정한 호칭 규칙: 마지막 글자를 * 로 가린다.
    홍길동 → 홍길*, 이준 → 이*, Seunghun Kang → Seunghun *, 이름 없음 → '' """
    name = (name or "").strip()
    if not name:
        return ""
    if re.search(r"[가-힣]", name):
        name = name.replace(" ", "")
        return name[:-1] + "*" if len(name) >= 2 else name + "*"
    parts = name.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]) + " *"
    return name[:-1] + "*" if len(name) >= 2 else name + "*"


FOREIGN_SIGNATURE = os.environ.get("REPLY_SIGNATURE_FOREIGN", "Master. Haward Jeoung").strip()


def review_lang(text):
    """리뷰 언어: ko / ja / zh / en(그 밖의 외국어 포함)"""
    if not text or is_korean(text):
        return "ko"
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if len(re.findall(r"[\u4e00-\u9fff]", text)) >= 3:
        return "zh"
    return "en"


def with_signature(reply, rv):
    """한국어: '홍길* 고객님,' + 본문 + '감사합니다.' + 빈 줄 + '홍삼빌호텔 총지배인 배상'
    외국어: 그 언어의 호칭 + 본문 + 그 언어의 감사 인사 + 빈 줄 + 'Master. Haward Jeoung'"""
    who = masked_name(rv.get("reviewer"))
    lang = review_lang(rv.get("text"))
    if lang == "ja":
        head, thanks = (f"{who}様" if who else "お客様"), "ありがとうございました。"
    elif lang == "zh":
        head, thanks = (f"尊敬的{who}，" if who else "尊敬的客人，"), "谢谢。"
    elif lang == "en":
        head, thanks = (f"Dear {who}," if who else "Dear Guest,"), "Thank you."
    else:
        return f"{who + ' 고객님,' if who else '고객님,'}\n{reply}\n감사합니다.\n\n{SIGNATURE}"
    return f"{head}\n{reply}\n{thanks}\n\n{FOREIGN_SIGNATURE}"


# ─────────────────────────────────────────────
# 구글 리뷰 조회 · 답글 등록
# ─────────────────────────────────────────────
def fetch_unreplied():
    h = {"Authorization": f"Bearer {access_token()}"}
    out = []
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    for loc_path, title, _ in find_locations(h):
        meta = {}
        rs = list_paged(GBP_REVIEWS.format(location=loc_path), h, "reviews",
                        {"pageSize": 50, "orderBy": "updateTime desc"}, first=meta)
        try:
            save_stats(rs, meta)
        except Exception as e:             # 통계 기록 실패가 답글 작업을 막지 않게
            print(f"⚠️ 평점 통계 기록 실패: {e}")
        for r in rs:
            if r.get("reviewReply"):
                continue
            created = parse_time(r.get("createTime"))
            if created and created < since:
                continue
            out.append({
                "id": r["reviewId"],
                "name": r.get("name") or f"{loc_path}/reviews/{r['reviewId']}",
                "stars": STAR.get(r.get("starRating"), 0),
                "text": original_comment(r.get("comment")),
                "reviewer": ((r.get("reviewer") or {}).get("displayName") or "").strip(),
                "created": created,
            })
    out.sort(key=lambda x: x["created"] or datetime.min.replace(tzinfo=timezone.utc))
    return out, h


def post_reply(review_name, comment, h):
    url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
    req = urllib.request.Request(
        url, data=json.dumps({"comment": comment}).encode(), method="PUT",
        headers={**h, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"답글 등록 실패 HTTP {e.code}: {e.read().decode(errors='replace')}") from e


# ─────────────────────────────────────────────
# 평점 통계 — 4.7 / 4.8 까지 5점 리뷰가 몇 개 더 필요한지
# ─────────────────────────────────────────────
STATS_FILE = "gbp_stats.json"


def needed_fives(total, s, target):
    """구글 표시 평점이 target 이 되려면(반올림 기준 target-0.05 이상) 필요한 5점 리뷰 수"""
    floor = target - 0.05
    if total and s / total >= floor - 1e-9:
        return 0
    if floor >= 5:
        return None
    n = (floor * total - s) / (5 - floor)
    k = max(0, int(n))
    while (s + 5 * k) / (total + k) < floor - 1e-9:
        k += 1
    return k


def save_stats(rs, meta):
    dist = {str(i): 0 for i in range(1, 6)}
    for r in rs:
        st = STAR.get(r.get("starRating"), 0)
        if st:
            dist[str(st)] += 1
    total = sum(dist.values())
    s = sum(int(k) * v for k, v in dist.items())
    avg = s / total if total else 0
    today = datetime.now(KST).strftime("%Y-%m-%d")
    row = {
        "date": today, "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "count": total, "sum": s, "avg": round(avg, 4), "display": round(avg + 1e-9, 1),
        "api_count": meta.get("totalReviewCount"), "api_avg": meta.get("averageRating"),
        "dist": dist,
        "need_4_7": needed_fives(total, s, 4.7), "need_4_8": needed_fives(total, s, 4.8),
    }
    data = {"history": []}
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    data["history"] = [h for h in data.get("history", []) if h.get("date") != today] + [row]
    data["latest"] = row
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"📈 평점 통계: 리뷰 {total}개 · 평균 {avg:.3f} (API {meta.get('averageRating')}/{meta.get('totalReviewCount')}) "
          f"→ 4.7까지 5점 {row['need_4_7']}개, 4.8까지 {row['need_4_8']}개")


# ─────────────────────────────────────────────
# 기록
# ─────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"replies": {}}


def save_log(log):
    log["updated"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def recent_replies(log, n=8):
    items = [v for v in log["replies"].values() if v.get("reply")]
    items.sort(key=lambda v: v.get("at", ""), reverse=True)
    out = []
    for v in items[:n]:
        body = re.sub(r"\n감사합니다\.\s*\n\n.*$", "", v["reply"], flags=re.S)   # 끝인사·서명 제거
        body = re.sub(r"^[^\n]*고객님,\s*\n", "", body)                         # 호칭 제거
        out.append(body.split("\n\n")[0][:160])
    return out


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────
def show_recent(n=12):
    """최근 리뷰와 거기 달린 답글(사람이 고친 것 포함)을 그대로 출력 — 말투 점검용"""
    h = {"Authorization": f"Bearer {access_token()}"}
    for loc_path, title, _ in find_locations(h):
        rs = list_paged(GBP_REVIEWS.format(location=loc_path), h, "reviews",
                        {"pageSize": 50, "orderBy": "updateTime desc"})
        for r in rs[:n]:
            rep = r.get("reviewReply") or {}
            print(f"\n★{STAR.get(r.get('starRating'), 0)} {r.get('createTime', '')[:10]} "
                  f"{(r.get('reviewer') or {}).get('displayName', '')}\n"
                  f"리뷰: {original_comment(r.get('comment'))!r}\n"
                  f"답글({rep.get('updateTime', '')[:16]}): {rep.get('comment', '(없음)')!r}")


def main():
    if MODE == "recent":
        show_recent()
        return
    print(f"▶️ 모드: {MODE} / 최근 {WINDOW_DAYS}일 / 1회 최대 {MAX_PER_RUN}건 / 모델 {REPLY_MODEL}")
    reviews, h = fetch_unreplied()
    log = load_log()
    print(f"🔎 답글 없는 리뷰 {len(reviews)}건")

    if MODE == "list":
        for rv in reviews:
            print(f"- {rv['created'].astimezone(KST):%Y-%m-%d} ★{rv['stars']} {rv['text'][:70]!r}")
        return

    done = 0
    for rv in reviews:
        if done >= MAX_PER_RUN:
            print(f"⏸ 1회 한도 {MAX_PER_RUN}건 도달 — 나머지는 다음 실행 때")
            break
        tag = f"{rv['created'].astimezone(KST):%Y-%m-%d} ★{rv['stars']}"
        if rv["stars"] < AUTO_MIN_STARS:
            print(f"🙋 {tag} 저평점 리뷰 → 자동 답글 안 함 (사람이 확인 후 직접 답글 필요)")
            log["replies"].setdefault(rv["id"], {"status": "manual", "stars": rv["stars"],
                                                 "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M")})
            continue

        reply, tries = write_reply(rv, recent_replies(log))
        if not reply:
            print(f"⚠️ {tag} {MAX_TRIES}회 모두 검사 불합격 → 이번엔 건너뜀 (다음 실행 때 다시 시도)")
            continue
        final = with_signature(reply, rv)
        print(f"\n✍️ {tag} ({tries}회차 통과)\n   리뷰: {rv['text'][:120]!r}\n   답글: {final!r}")

        if MODE == "preview":
            done += 1
            continue

        post_reply(rv["name"], final, h)
        log["replies"][rv["id"]] = {
            "status": "replied", "stars": rv["stars"], "reply": final,
            "review_date": rv["created"].astimezone(KST).strftime("%Y-%m-%d") if rv["created"] else "",
            "at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        }
        save_log(log)
        print("   ✅ 구글에 등록 완료")
        done += 1
        time.sleep(3)

    if MODE == "reply":
        save_log(log)
    manual = [k for k, v in log["replies"].items() if v.get("status") == "manual"]
    print(f"\n📊 이번 실행: {'미리보기' if MODE == 'preview' else '등록'} {done}건 / 수동 처리 대기 저평점 {len(manual)}건")


if __name__ == "__main__":
    try:
        if MODE not in ("reply", "preview", "list", "recent"):
            raise RuntimeError(f"mode 값이 잘못됨: {MODE} (reply / preview / list)")
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
