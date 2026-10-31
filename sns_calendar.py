# -*- coding: utf-8 -*-
"""
sns_calendar.py — 홍삼빌호텔 SNS 공통 모듈 (2026-10-30)
=====================================================
네 파이프라인(홍삼빌 스레드·인스타·페북, 진안사우나 스레드)이 같은 파일을 쓴다.
표준 라이브러리만 사용. 창고(Supabase "Auto blog writer")에 못 닿으면
빈 문맥을 돌려주고, 호출한 스크립트는 지금처럼 동작한다 — 발행은 절대 멈추지 않는다.

함수 4개:
  load_context(channel, slot)                 → dict 또는 None
  prompt_block(ctx)                           → 프롬프트에 붙일 문자열 ('' 가능)
  check_cross(text, ctx)                      → 문제 목록(list[str])
  log_cross(channel, post_id, permalink, slot, ctx, text) → None (실패해도 예외 없음)

환경변수:
  SNS_SUPABASE_URL   (기본값 있음)
  SNS_SUPABASE_KEY   publishable 키 — 읽기 전용 (기본값 내장)
  SNS_WRITE_SECRET   창고 쓰기 비밀값 (GitHub Secrets)
  SLOT               회차 1~3, 없거나 0이면 한국시간으로 추정
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, date

KST = timezone(timedelta(hours=9))

SUPABASE_URL = os.environ.get("SNS_SUPABASE_URL", "https://keudubzdzechqommscxq.supabase.co").rstrip("/")
# publishable 키 — 공개용(읽기·RLS 적용). 링크 허브 페이지에도 들어가는 값이라 코드에 둔다
SUPABASE_KEY = os.environ.get("SNS_SUPABASE_KEY") or "sb_publishable_2Txh41lZBqgt7V0GShux7Q_cQJTUaDM"
WRITE_SECRET = os.environ.get("SNS_WRITE_SECRET", "")
TIMEOUT = 10

# ─────────────────────────────────────────────
# 규칙표 (기획서 5장)
# ─────────────────────────────────────────────
CHANNELS = ("ig_hotel", "threads_hotel", "threads_sauna", "fb_hotel")
CROSS_SLOT = {"ig_hotel": 1, "threads_hotel": 2, "threads_sauna": 3}   # 평상시 교차 유도 회차
TARGETS = {
    "ig_hotel": ["threads_hotel", "threads_sauna"],
    "threads_hotel": ["ig_hotel", "threads_sauna"],
    "threads_sauna": ["ig_hotel", "threads_hotel"],
}
HANDLE = {   # 글에 들어가야 하는 표기
    "ig_hotel": "인스타 @hongsamhotel",
    "threads_hotel": "스레드 @hongsamhotel",
    "threads_sauna": "스레드 @jinan.sauna",
}
AT = {"ig_hotel": "@hongsamhotel", "threads_hotel": "@hongsamhotel", "threads_sauna": "@jinan.sauna"}
PLATFORM_WORD = {"ig_hotel": "인스타", "threads_hotel": "스레드", "threads_sauna": "스레드"}
LABEL = {"ig_hotel": "인스타", "threads_hotel": "홍삼빌 스레드", "threads_sauna": "진안사우나 스레드"}
THERE = {   # "거기에 있는 것" — 교차 유도 문장이 가리킬 내용
    "ig_hotel": "호텔·마이산·사우나 사진",
    "threads_hotel": "객실·천둥이·직원 뒷이야기와 댓글 대화",
    "threads_sauna": "사우나 패키지 가격과 구성",
}

TIME_WORDS = ["방금", "오늘 아침", "조금 전", "지금 막", "오늘 올린", "방금 올린", "아까 올린", "오늘 새벽"]
NOTICE_WORDS = ["자세한 내용은", "참고하세요", "확인해 주세요", "확인해주세요", "안내드립니다", "참고 바랍니다"]
ALLOWED_PRICES = {"99,000", "99000", "20,000", "20000", "30,000", "30000", "2만", "3만"}
SAUNA_STALE = ["오픈 예정", "2027년 1월", "오픈 알림"]


# ─────────────────────────────────────────────
# 내부: HTTP
# ─────────────────────────────────────────────
def _rpc(name, body, key=None, retries=1):
    """Supabase RPC 호출. 실패하면 None (예외를 밖으로 내보내지 않는다)."""
    key = key or SUPABASE_KEY
    if not key:
        print("[sns_calendar] SNS_SUPABASE_KEY 없음 → 창고 없이 진행")
        return None
    url = SUPABASE_URL + "/rest/v1/rpc/" + name
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                raw = res.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:200]
            print("[sns_calendar] " + name + " HTTP " + str(e.code) + ": " + msg)
            if e.code < 500:
                return None
        except Exception as e:
            print("[sns_calendar] " + name + " 연결 실패: " + str(e)[:120])
        if attempt < retries:
            time.sleep(10)
    return None


# ─────────────────────────────────────────────
# 회차·대상 결정
# ─────────────────────────────────────────────
def resolve_slot(slot=None, now=None):
    """SLOT 환경변수 또는 인자. 0/없음이면 한국시간으로 추정 (12시 전 1, 17시 전 2, 그 외 3)."""
    s = slot if slot is not None else os.environ.get("SLOT", "0")
    try:
        s = int(s)
    except (TypeError, ValueError):
        s = 0
    if s in (1, 2, 3):
        return s
    h = (now or datetime.now(KST)).hour
    return 1 if h < 12 else (2 if h < 17 else 3)


def decide_cross(channel, slot, week, today):
    """이 글이 가리킬 계정 id 또는 None. 기획서 5장 규칙."""
    if channel not in TARGETS:
        return None
    special = (week or {}).get("special_channel")
    if special == channel:                       # 담당 주간: 받는 쪽이므로 쉼
        return None
    if special in TARGETS[channel]:              # 담당 계정을 하루 2회 가리킴
        base = CROSS_SLOT[channel]
        second = 3 if base != 3 else 1
        return special if slot in (base, second) else None
    if slot != CROSS_SLOT[channel]:              # 평상시 1회
        return None
    return TARGETS[channel][today.timetuple().tm_yday % 2]


# ─────────────────────────────────────────────
# 1. 문맥 읽기
# ─────────────────────────────────────────────
def load_context(channel, slot=None, today=None):
    if channel not in CHANNELS:
        print("[sns_calendar] 알 수 없는 채널 " + str(channel))
        return None
    today = today or datetime.now(KST).date()
    slot = resolve_slot(slot)
    res = _rpc("sns_context", {"p_channel": channel, "p_today": today.isoformat()})
    if not isinstance(res, dict):
        print("[sns_calendar] 창고 응답 없음 → 교차 유도 없이 발행")
        return None
    week = res.get("week") or None
    facts = res.get("facts") or []
    target = decide_cross(channel, slot, week, today)
    ctx = {
        "channel": channel, "slot": slot, "today": today.isoformat(),
        "week": week, "facts": facts, "special": bool(week and week.get("special_channel") == channel),
        "cross": None,
    }
    if target:
        ctx["cross"] = {"target": target, "handle": HANDLE[target], "at": AT[target],
                        "platform": PLATFORM_WORD[target], "there": THERE[target]}
    print("[sns_calendar] 채널 " + channel + " 회차 " + str(slot)
          + " / 주간테마 " + (week["theme_title"] if week else "없음")
          + " / 교차유도 " + (LABEL[target] if target else "없음"))
    return ctx


# ─────────────────────────────────────────────
# 2. 프롬프트 덩어리
# ─────────────────────────────────────────────
def prompt_block(ctx, position_hint=None):
    if not ctx:
        return ""
    ch = ctx["channel"]
    lines = []
    if ctx["facts"]:
        lines.append("【호텔 사실 원장 — 여기 있는 것만 사실로 쓴다. 없는 가격·시설·이벤트는 만들지 않는다】")
        for f in ctx["facts"]:
            lines.append("- " + str(f.get("value", "")).strip())
        lines.append("")
    w = ctx["week"]
    if w:
        lines.append("【이번 주 테마: " + w.get("theme_title", "") + "】")
        lines.append("설명: " + w.get("theme_desc", ""))
        if w.get("facts_note"):
            lines.append("이번 주 틀리면 안 되는 사실: " + w["facts_note"])
        if ctx["special"]:
            lines.append("이번 주는 이 계정이 '특별 공개' 담당이다. 테마의 핵심 정보를 이 계정에서 먼저, 구체적으로 공개한다.")
        lines.append("")
    c = ctx["cross"]
    others = [AT[k] for k in TARGETS if k != ch and AT[k] != AT.get(ch)]
    if c:
        pos = position_hint or {
            "ig_hotel": "마무리 한 줄(태그·저장 유도) 바로 앞 문단",
            "threads_hotel": "댓글 유도 문장 바로 앞",
            "threads_sauna": "본문 끝",
        }.get(ch, "본문 끝")
        lines += [
            "【교차 유도 — 이 글에서 반드시 한 번】",
            "- 글 안에서 \"" + c["handle"] + "\" 를 정확히 한 번 언급한다. 거기에 있는 것: " + c["there"] + ".",
            "- 위치: " + pos + ", 본문 흐름 안에 한 문장으로.",
            "- 시간에 묶지 마라: \"방금\", \"오늘 아침\" 금지. \"이번 주\", \"가면 있음\"처럼 며칠 뒤 읽어도 맞게.",
            "- 안내문 톤 금지: \"자세한 내용은 ○○ 참고\" 같은 문장은 쓰지 않는다.",
            "- 광고 티 없이. 예: \"이 사진 찍은 날 천둥이가 한 짓은 " + c["platform"] + "(" + c["at"] + ")에 써놨음\" 같은 톤.",
        ]
        exclude = [a for a in set(AT.values()) if a != c["at"]]
        if exclude:
            lines.append("- 다른 계정(" + ", ".join(sorted(exclude)) + ")은 이 글에서 언급하지 않는다.")
    else:
        lines.append("【이 글에서는 다른 계정(@hongsamhotel, @jinan.sauna)을 언급하지 않는다】")
    return "\n".join(lines).strip()


# ─────────────────────────────────────────────
# 3. 검사
# ─────────────────────────────────────────────
def _count_handle(text, target):
    """대상 계정 표기가 몇 번 나오는지. @hongsamhotel은 인스타/스레드 두 계정이 공유하므로
    앞 6자 안의 플랫폼 단어로 구분한다."""
    at = AT[target]
    n = 0
    for m in re.finditer(re.escape(at), text):
        head = text[max(0, m.start() - 6):m.start()]
        if at == "@jinan.sauna":
            n += 1
        elif PLATFORM_WORD[target] in head:
            n += 1
    return n


def _count_any_handles(text):
    return len(re.findall(r"@hongsamhotel|@jinan\.sauna", text))


def check_cross(text, ctx):
    probs = []
    if not text:
        return probs
    for w in TIME_WORDS:
        if w in text:
            probs.append("시간에 묶인 표현: " + w)
    for w in NOTICE_WORDS:
        if w in text:
            probs.append("안내문 톤: " + w)
    if "사우나" in text:
        for w in SAUNA_STALE:
            if w in text:
                probs.append("사우나 옛 정보: " + w)
    for p in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원", text):
        if p.replace(",", "") not in {x.replace(",", "") for x in ALLOWED_PRICES}:
            probs.append("사실 원장에 없는 가격: " + p + "원")
    if ctx and ctx.get("cross"):
        tgt = ctx["cross"]["target"]
        n = _count_handle(text, tgt)
        if n != 1:
            probs.append("교차 유도 대상 '" + HANDLE[tgt] + "' 이 " + str(n) + "회 (정확히 1회여야 함)")
        total = _count_any_handles(text)
        if total != 1:
            probs.append("계정 표기가 총 " + str(total) + "회 (대상 1회만 허용)")
    else:
        total = _count_any_handles(text)
        if total:
            probs.append("교차 유도 차례가 아닌데 계정 표기 " + str(total) + "회")
    return probs


def extract_phrase(text, ctx):
    """기록용: 대상 표기가 들어 있는 문장 한 줄."""
    if not ctx or not ctx.get("cross"):
        return None
    at = ctx["cross"]["at"]
    # '.'은 @jinan.sauna 안에도 있으므로 문장 끝(뒤에 공백/끝)일 때만 자른다
    for ln in re.split(r"\n|[。!?]|\.(?=\s|$)", text):
        if at in ln:
            return ln.strip()[:200]
    return None


# ─────────────────────────────────────────────
# 4. 기록
# ─────────────────────────────────────────────
def log_cross(channel, post_id, permalink, slot, ctx, text):
    if not WRITE_SECRET:
        print("[sns_calendar] SNS_WRITE_SECRET 없음 → 교차 유도 기록 생략")
        return
    target = ctx["cross"]["target"] if (ctx and ctx.get("cross")) else None
    phrase = extract_phrase(text, ctx) if target else None
    if target and not phrase:          # 최선안으로 발행됐는데 교차 문장이 빠진 경우 → 교차 없음으로 기록
        target = None
    body = {
        "p_secret": WRITE_SECRET, "p_channel": channel, "p_post_id": str(post_id),
        "p_permalink": permalink, "p_slot": int(slot) if slot else None,
        "p_cross_target": target, "p_phrase": phrase,
    }
    res = _rpc("sns_log_cross", body, retries=0)
    if res is None:
        print("[sns_calendar] 교차 유도 기록 실패 (발행은 완료됨)")
    else:
        print("[sns_calendar] 교차 유도 기록 완료 id=" + str(res))


if __name__ == "__main__":
    ch = sys.argv[1] if len(sys.argv) > 1 else "threads_hotel"
    c = load_context(ch)
    print(prompt_block(c))
