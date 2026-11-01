#!/usr/bin/env python3
"""
홍삼빌 리뷰 분석 대시보드 빌드
  입력: reviews_archive.json (저장소), cls.json (분류 캐시: 리뷰ID → 분류), team_report.json (팀별 고지)
  출력: review_dashboard.html (데이터 내장, 비공개 아티팩트로 게시)
사용: python3 build.py ARCHIVE CLS TEAM_REPORT OUT_HTML [--stats-only]
"""
import json, sys, datetime as dt
from collections import Counter

ASPECTS = {
    "cleanliness": ("청결·위생", "객실팀"),
    "room": ("객실·침구·난방", "객실팀"),
    "amenity": ("어메니티·세탁·편의", "객실팀"),
    "pet": ("반려견 서비스", "객실팀"),
    "staff": ("직원 친절", "프런트팀"),
    "checkin": ("체크인·안내·소통", "프런트팀"),
    "breakfast": ("조식", "식음팀"),
    "sauna_bbq": ("사우나·바비큐", "식음팀"),
    "facility": ("시설·설비", "시설팀"),
    "environment": ("조망·환경·주차", "시설팀"),
    "noise": ("소음·방음", "시설팀"),
    "price": ("가격·가성비", "경영진"),
}
REOPEN = "2025-11-01"
KST = dt.timezone(dt.timedelta(hours=9))


def agg(rows, cls, since):
    rs = [r for r in rows if r["created"] >= since]
    n = len(rs)
    st = Counter(r["stars"] for r in rs)
    avg = round(sum(r["stars"] for r in rs) / n, 2) if n else 0
    txt = [r for r in rs if r["id"] in cls]
    asp = {}
    for k, (ko, team) in ASPECTS.items():
        pos = neg = mix = 0
        pe, ne, me = [], [], []
        for r in txt:
            a = cls[r["id"]].get("aspects", {}).get(k)
            if not a:
                continue
            if a["s"] == "pos":
                pos += 1; pe.append(a.get("ev", ""))
            elif a["s"] == "neg":
                neg += 1; ne.append(a.get("ev", ""))
            else:
                mix += 1; me.append(a.get("ev", ""))
        tot = pos + neg + mix
        asp[k] = {"ko": ko, "team": team, "n": tot, "pos": pos, "neg": neg, "mix": mix,
                  "sat": round((pos + mix * 0.5) / tot * 100) if tot else None,
                  "pe": [e for e in pe if e][:6], "ne": [e for e in ne + me if e][:8]}
    rev = Counter(cls[r["id"]].get("revisit", "unknown") for r in txt)
    guest = Counter(cls[r["id"]].get("guest", "unknown") for r in txt)
    low = [{"date": r["created"], "stars": r["stars"], "sum": cls.get(r["id"], {}).get("sum", "본문 없음"),
            "dis": cls.get(r["id"], {}).get("dis", [])}
           for r in sorted(rs, key=lambda x: x["created"], reverse=True) if r["stars"] <= 3]
    return {"since": since, "n": n, "n_text": len(txt), "avg": avg,
            "stars": {str(s): st.get(s, 0) for s in (5, 4, 3, 2, 1)},
            "aspects": asp, "revisit": dict(rev), "guest": dict(guest),
            "dis": [d for r in txt for d in cls[r["id"]].get("dis", [])],
            "req": [d for r in txt for d in cls[r["id"]].get("req", [])],
            "low": low}


def main():
    arch_p, cls_p, rep_p, out_p = sys.argv[1:5]
    arch = json.load(open(arch_p, encoding="utf-8"))
    cls = json.load(open(cls_p, encoding="utf-8"))
    rows = arch["reviews"]
    today = dt.datetime.now(KST).date()
    d30 = (today - dt.timedelta(days=30)).isoformat()
    d90 = (today - dt.timedelta(days=90)).isoformat()
    stats = {"d30": agg(rows, cls, d30), "d90": agg(rows, cls, d90), "reopen": agg(rows, cls, REOPEN)}
    if "--stats-only" in sys.argv:
        json.dump(stats, open(out_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return
    report = json.load(open(rep_p, encoding="utf-8"))
    data = {"date": today.isoformat(), "archive_updated": arch.get("updated"),
            "google_avg": arch.get("average_rating"), "google_count": arch.get("total_review_count"),
            "stats": stats, "report": report}
    tpl = open(__file__.replace("build.py", "template.html"), encoding="utf-8").read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    # 분류 캐시를 페이지에 함께 보관 → 다음 주 작업이 새 리뷰만 분류
    html = html.replace("<!--__CACHE__-->",
                        '<script type="application/json" id="cls-cache">' +
                        json.dumps(cls, ensure_ascii=False).replace("</", "<\\/") + "</script>")
    open(out_p, "w", encoding="utf-8").write(html)
    print("ok", len(cls), "classified;", stats["reopen"]["n"], "since reopen")


if __name__ == "__main__":
    main()
