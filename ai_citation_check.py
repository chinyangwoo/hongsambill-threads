# -*- coding: utf-8 -*-
"""
AI 인용 점검기 (홍보 대시보드용)
=================================
손님이 AI에 물어볼 만한 질문(ai_questions.json)을 매일 Claude 웹 검색으로 묻고,
답변에 우리 업장이 나오는지(언급), 우리 블로그·홈페이지가 출처로 인용됐는지(인용)를 기록합니다.
결과는 metrics/ai_citations.json 에 쌓이고 Supabase 를 거쳐 대시보드에 나옵니다.

필요한 환경변수: ANTHROPIC_API_KEY
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

KST = timezone(timedelta(hours=9))
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("AI_CHECK_MODEL", "claude-sonnet-5")
QFILE = "ai_questions.json"
OUT = os.path.join("metrics", "ai_citations.json")
KEEP_DAYS = 180


def load_cfg():
    with open(QFILE, encoding="utf-8") as f:
        return json.load(f)


def ask(question):
    body = {
        "model": MODEL,
        "max_tokens": 1500,
        "system": "당신은 한국 여행·음식 정보를 찾아 주는 검색 도우미입니다. 웹 검색으로 최신 정보를 찾아 한국어로 답하고, 구체적인 업체·장소 이름을 들어 추천하세요.",
        "messages": [{"role": "user", "content": question}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2, "user_location": {"type": "approximate", "country": "KR", "timezone": "Asia/Seoul"}}],
    }
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as res:
        return json.loads(res.read().decode())


def parse(resp):
    text, cited, searched = [], [], []
    for b in resp.get("content", []):
        if b.get("type") == "text":
            text.append(b.get("text", ""))
            for c in b.get("citations") or []:
                if c.get("url"):
                    cited.append(c["url"])
        elif b.get("type") == "web_search_tool_result":
            for r in b.get("content") or []:
                if isinstance(r, dict) and r.get("url"):
                    searched.append(r["url"])
    return "".join(text), list(dict.fromkeys(cited)), list(dict.fromkeys(searched))


def ours(url, domains):
    u = url.lower().replace("://www.", "://")
    return any(d in u for d in domains)


def main():
    if not API_KEY:
        sys.exit("ANTHROPIC_API_KEY 가 없습니다.")
    cfg = load_cfg()
    domains = [d.lower().replace("www.", "") for d in cfg["our_domains"]]
    names = cfg["our_names"]          # {"hsb": ["홍삼빌호텔", ...], ...}
    os.makedirs("metrics", exist_ok=True)
    data = {"checks": []}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    done = {(c["date"], c["question"]) for c in data["checks"]}
    # 하루에 업장별 질문 1개씩: 날짜에 따라 목록을 순서대로 돌아가며 고른다
    picks = []
    for biz in names:
        qs = [q for q in cfg["questions"] if q.get("business") == biz]
        if qs:
            picks.append(qs[datetime.now(KST).toordinal() % len(qs)])
    wrong = cfg.get("wrong_facts", {})
    for q in picks:
        if (today, q["q"]) in done:
            continue
        try:
            resp = ask(q["q"])
        except urllib.error.HTTPError as e:
            print(f"⚠️ {q['q']}: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
            continue
        except Exception as e:
            print(f"⚠️ {q['q']}: {e}")
            continue
        answer, cited, searched = parse(resp)
        mentioned = sorted({biz for biz, ns in names.items() for n in ns if n in answer})
        # 추천 목록에서 우리 업장이 처음 나온 순서 (번호 목록 기준, 대략)
        rank = 0
        items = [ln for ln in answer.split("\n") if re.match(r"^\s*(\d+[.)]|[-*•]|#+)\s", ln)]
        for i, ln in enumerate(items, 1):
            if any(n in ln for ns in names.values() for n in ns):
                rank = i
                break
        our_cited = [u for u in cited if ours(u, domains)]
        our_found = [u for u in searched if ours(u, domains)]
        bad = sorted({w for biz in mentioned for w in wrong.get(biz, []) if w in answer})
        rec = {
            "date": today, "question": q["q"], "business": q.get("business", ""), "engine": "Claude 웹검색",
            "mentioned": mentioned, "rank": rank,
            "cited": bool(our_cited), "cited_urls": our_cited,
            "found_in_search": bool(our_found), "found_urls": our_found,
            "wrong": bad,
            "sources": sorted({urlparse(u).netloc.replace("www.", "") for u in cited})[:10],
            "answer": answer[:1500],
        }
        data["checks"].append(rec)
        print(f"{'🟢' if mentioned else '⚪'} {q['q']} — 언급 {mentioned or '없음'} · 틀린 정보 {bad or '없음'} · 출처 인용 {len(our_cited)} · 검색 노출 {len(our_found)}")
        time.sleep(2)
    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    data["checks"] = [c for c in data["checks"] if c["date"] >= cutoff]
    data["updated"] = datetime.now(KST).isoformat()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
