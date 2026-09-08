# -*- coding: utf-8 -*-
"""
성수주조장 인스타그램 성과 수집기
=========================
seongsu_instagram_posted_log.json 에 기록된 게시물의 조회수·도달·좋아요·댓글·저장·공유를
Instagram Graph API 에서 가져와 seongsu_instagram_insights.json 에 쌓습니다.

- 게시 12시간 이상 지난 게시물만 수집 (너무 이르면 수치가 낮게 잡힘)
- 14일 동안은 매일 갱신(조회수는 며칠씩 늘어남), 그 이후는 확정값 유지
- 마지막에 형식(릴스/캐러셀)·슬롯(AM/PM)·CTA 유형별 평균 조회수 요약을 출력

필요한 환경변수: SEONGSU_INSTAGRAM_ACCESS_TOKEN
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

IG_API = "https://graph.instagram.com/v21.0"
LOG_FILE = "seongsu_instagram_posted_log.json"
OUT_FILE = "seongsu_instagram_insights.json"
KST = timezone(timedelta(hours=9))
MIN_AGE_HOURS = 12      # 게시 후 이 시간이 지나야 수집
REFRESH_DAYS = 14       # 이 기간 동안은 매일 다시 수집해 갱신

ACCESS_TOKEN = os.environ["SEONGSU_INSTAGRAM_ACCESS_TOKEN"]


def http_json(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {url.split('?')[0]} \n{body}") from e


def fetch_media(post_id):
    fields = "id,media_type,media_product_type,like_count,comments_count,timestamp,permalink"
    return http_json(f"{IG_API}/{post_id}?fields={fields}&access_token={ACCESS_TOKEN}")


def fetch_insights(post_id, media_type):
    """미디어 유형별로 허용되는 지표가 달라서, 여러 조합을 순서대로 시도"""
    candidates = [
        "views,reach,saved,shares,total_interactions",
        "views,reach,saved,shares",
        "reach,saved,shares",
        "reach,saved",
    ]
    last_err = None
    for metrics in candidates:
        try:
            res = http_json(f"{IG_API}/{post_id}/insights?metric={metrics}&access_token={ACCESS_TOKEN}")
            out = {}
            for item in res.get("data", []):
                vals = item.get("values") or [{}]
                out[item["name"]] = vals[0].get("value", 0)
            return out
        except Exception as e:
            last_err = e
    print(f"  ⚠️ insights 실패 ({post_id}): {last_err}")
    return {}


def main():
    if not os.path.exists(LOG_FILE):
        print("게시 기록이 없습니다.")
        return
    with open(LOG_FILE, encoding="utf-8") as f:
        log = json.load(f)
    data = {}
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8") as f:
            data = json.load(f)

    now = datetime.now(KST)
    updated = 0
    for p in log.get("posts", []):
        pid = p.get("post_id")
        if not pid:
            continue
        posted_at = datetime.strptime(p["at"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        age_h = (now - posted_at).total_seconds() / 3600
        if age_h < MIN_AGE_HOURS:
            continue
        if pid in data and age_h > REFRESH_DAYS * 24:
            continue   # 확정값 유지
        try:
            media = fetch_media(pid)
        except Exception as e:
            print(f"  ⚠️ media 조회 실패 ({pid}): {e}")
            continue
        ins = fetch_insights(pid, media.get("media_type"))
        hook = (p.get("text") or "").strip().split("\n")[0]
        row = data.get(pid, {})
        row.update({
            "at": p["at"],
            "slot": p.get("slot"),
            "format": p.get("format", "carousel"),
            "cta_type": p.get("cta_type"),
            "topic": p.get("topic"),
            "hook": hook,
            "images": p.get("images", []),
            "permalink": media.get("permalink"),
            "media_type": media.get("media_product_type") or media.get("media_type"),
            "likes": media.get("like_count", 0),
            "comments": media.get("comments_count", 0),
            "views": ins.get("views", row.get("views", 0)),
            "reach": ins.get("reach", row.get("reach", 0)),
            "saved": ins.get("saved", row.get("saved", 0)),
            "shares": ins.get("shares", row.get("shares", 0)),
            "fetched_at": now.strftime("%Y-%m-%d %H:%M"),
        })
        data[pid] = row
        updated += 1
        print(f"  {p['at']} [{row['format']}/{row['slot']}/{row['cta_type']}] "
              f"조회 {row['views']} · 도달 {row['reach']} · 좋아요 {row['likes']} · 댓글 {row['comments']} · 저장 {row['saved']} | {hook}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n✅ {updated}건 갱신 → {OUT_FILE} (누적 {len(data)}건)")

    # 요약: 형식·슬롯·CTA 별 평균 조회수
    def avg(key):
        groups = {}
        for r in data.values():
            groups.setdefault(r.get(key), []).append(r.get("views", 0))
        return {k: round(sum(v) / len(v)) for k, v in groups.items() if v}
    if data:
        print("\n📊 평균 조회수")
        print("  형식:", avg("format"))
        print("  슬롯:", avg("slot"))
        print("  CTA :", avg("cta_type"))
        top = sorted(data.values(), key=lambda r: r.get("views", 0), reverse=True)[:3]
        print("  조회 상위 3:")
        for r in top:
            print(f"    {r['views']}회 | {r['at']} [{r['format']}] {r['hook']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
