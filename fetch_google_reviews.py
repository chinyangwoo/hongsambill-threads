# -*- coding: utf-8 -*-
"""
홍삼빌호텔 구글 리뷰 자동 수집 → review_to_sns.py 로 3개 SNS 자동 게시
======================================================================
Google Business Profile API 승인(프로젝트 502238519910) 후 추가된 '앞단' 스크립트.
리뷰를 직접 붙여넣던 부분만 자동화하고, 글 생성·이미지·게시·로그는
기존 review_to_sns.py 를 그대로 호출해 재사용한다 (기존 파일 수정 없음).

동작 순서 (mode=post, 기본):
  1. OAuth 리프레시 토큰으로 액세스 토큰 발급
  2. 계정 → 지점 조회, 상호에 LOCATION_KEYWORD("홍삼빌")가 들어간 지점만 사용
     (같은 계정의 홍삼스파 등 다른 지점 리뷰가 호텔 후기로 올라가는 것 방지)
  3. 리뷰 조회 → 필터: 별점 4점 이상 / 본문 15자 이상 / 최근 MAX_AGE_DAYS 일 이내 /
     google_reviews_seen.json 에 처리 완료로 기록되지 않은 것
  4. 조건을 통과한 리뷰 중 '가장 오래된 1건'만 review_to_sns.py 로 게시 (1회 실행 = 최대 1건)
  5. review_sns_log.json 을 다시 읽어 3개 플랫폼 모두 성공했으면 '완료'로 기록,
     일부 실패면 다음 실행 때 실패한 플랫폼만 재시도 (최대 MAX_ATTEMPTS 회 후 포기)

다른 모드 (수동 실행 입력 mode):
  list     : 게시하지 않고 조회 결과만 출력 (API·권한 테스트용)
  baseline : 지금 있는 리뷰를 전부 '처리 완료'로 기록만 하고 게시하지 않음
             → google_reviews_seen.json 이 없으면 post 모드에서도 자동으로 baseline 부터 실행
             (도입 첫날 과거 리뷰 수십 건이 한꺼번에 올라가는 사고 방지)

안전장치:
  - 작성자 이름은 읽지도 넘기지도 않는다 (reviewer_label = "한 손님" 고정)
  - "(Translated by Google)" / "(Google 번역 제공)" 이 붙은 리뷰는 원문만 추출해 사용
  - 이미 수동으로 붙여넣어 게시한 리뷰는 review_to_sns.py 의 해시 중복검사로 걸러짐

필요한 GitHub Secrets:
  GBP_CLIENT_ID, GBP_CLIENT_SECRET, GBP_REFRESH_TOKEN   (신규 — OAuth Playground 로 발급)
  + review_to_sns.py 가 쓰는 기존 시크릿 전부 (워크플로에서 그대로 전달)
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
GBP_ACCOUNTS = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
GBP_LOCATIONS = "https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations"
GBP_REVIEWS = "https://mybusiness.googleapis.com/v4/{location}/reviews"

SEEN_FILE = "google_reviews_seen.json"
SNS_LOG_FILE = "review_sns_log.json"          # review_to_sns.py 가 쓰는 로그
ALL_PLATFORMS = ("threads", "instagram", "facebook")

LOCATION_KEYWORD = os.environ.get("LOCATION_KEYWORD", "홍삼빌").strip()
MIN_STARS = 4
MIN_COMMENT_LEN = 15
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "30"))
MAX_ATTEMPTS = 3
MAX_REVIEW_PAGES = 10                          # 50건 × 10쪽 = 최대 500건 조회
REVIEWER_LABEL = "한 손님"
KST = timezone(timedelta(hours=9))
STAR = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}

MODE = (os.environ.get("MODE", "post").strip().lower() or "post")

GBP_CLIENT_ID = os.environ["GBP_CLIENT_ID"]
GBP_CLIENT_SECRET = os.environ["GBP_CLIENT_SECRET"]
GBP_REFRESH_TOKEN = os.environ["GBP_REFRESH_TOKEN"]


def http_json(url, data=None, headers=None):
    if data is not None and not isinstance(data, bytes):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} 오류: {url.split('?')[0]}\n응답: {body}") from e


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


# ─────────────────────────────────────────────
# 1. 구글 리뷰 조회
# ─────────────────────────────────────────────
def access_token():
    res = http_json("https://oauth2.googleapis.com/token", {
        "client_id": GBP_CLIENT_ID,
        "client_secret": GBP_CLIENT_SECRET,
        "refresh_token": GBP_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    })
    return res["access_token"]


def list_paged(url, headers, key, params):
    items, token = [], None
    for _ in range(MAX_REVIEW_PAGES):
        p = dict(params)
        if token:
            p["pageToken"] = token
        res = http_json(f"{url}?{urllib.parse.urlencode(p)}", headers=headers)
        items += res.get(key, [])
        token = res.get("nextPageToken")
        if not token:
            break
    return items


def find_locations(h):
    accounts = list_paged(GBP_ACCOUNTS, h, "accounts", {"pageSize": 20})
    if not accounts:
        raise RuntimeError("접근 가능한 비즈니스 프로필 계정이 없습니다 (로그인한 구글 계정·권한 확인).")
    found, seen = [], set()
    for acc in accounts:
        locs = list_paged(GBP_LOCATIONS.format(account=acc["name"]), h, "locations",
                          {"readMask": "name,title", "pageSize": 100})
        for loc in locs:
            title = loc.get("title", "")
            loc_id = loc["name"]                                   # locations/456
            marker = "✅" if LOCATION_KEYWORD in title else "  "
            print(f"{marker} 지점: {title} ({acc['name']}/{loc_id})")
            if LOCATION_KEYWORD in title and loc_id not in seen:
                seen.add(loc_id)
                found.append((f"{acc['name']}/{loc_id}", title))    # accounts/123/locations/456
    if not found:
        raise RuntimeError(f"상호에 '{LOCATION_KEYWORD}'가 들어간 지점을 찾지 못했습니다.")
    return found


def fetch_reviews():
    h = {"Authorization": f"Bearer {access_token()}"}
    reviews = []
    for loc_path, title in find_locations(h):
        rs = list_paged(GBP_REVIEWS.format(location=loc_path), h, "reviews",
                        {"pageSize": 50, "orderBy": "updateTime desc"})
        print(f"⭐ {title}: 리뷰 {len(rs)}건 조회")
        reviews += rs
    return reviews


# ─────────────────────────────────────────────
# 2. 리뷰 정리 · 필터
# ─────────────────────────────────────────────
TRANSLATION_MARKERS = ("(Translated by Google)", "(Google 번역 제공)", "(Google에서 번역함)")
ORIGINAL_MARKERS = ("(Original)", "(원문)")


def original_comment(comment):
    """구글 자동번역이 붙은 리뷰는 원문 부분만 남긴다."""
    c = (comment or "").strip()
    for m in ORIGINAL_MARKERS:
        if m in c:
            return c.split(m, 1)[1].strip()
    for m in TRANSLATION_MARKERS:
        if c.startswith(m):
            return c[len(m):].strip()
    return c


def parse_time(s):
    if not s:
        return None
    s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def normalize(r):
    return {
        "id": r["reviewId"],
        "stars": STAR.get(r.get("starRating"), 0),
        "text": original_comment(r.get("comment")),
        "created": parse_time(r.get("createTime")),
        "created_raw": r.get("createTime", ""),
    }


def skip_reason(rv, seen):
    entry = seen["reviews"].get(rv["id"])
    if entry and entry.get("status") in ("done", "baseline", "skipped", "gave_up"):
        return "처리 완료"
    if rv["stars"] < MIN_STARS:
        return f"별점 {rv['stars']}점"
    if len(rv["text"]) < MIN_COMMENT_LEN:
        return f"본문 {len(rv['text'])}자"
    if rv["created"] is None:
        return "작성일 없음"
    if rv["created"] < datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS):
        return f"{MAX_AGE_DAYS}일 경과"
    return None


# ─────────────────────────────────────────────
# 3. 기록 파일
# ─────────────────────────────────────────────
def load_seen():
    if not os.path.exists(SEEN_FILE):
        return None
    with open(SEEN_FILE, encoding="utf-8") as f:
        seen = json.load(f)
    seen.setdefault("reviews", {})
    return seen


def save_seen(seen):
    seen["updated"] = now_kst()
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def posted_platforms_for(text):
    """review_to_sns.py 의 해시 규칙과 동일하게 계산해, 이미 성공한 플랫폼을 읽어온다."""
    import hashlib
    h = hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()
    if not os.path.exists(SNS_LOG_FILE):
        return set()
    with open(SNS_LOG_FILE, encoding="utf-8") as f:
        log = json.load(f)
    done = set()
    for p in log.get("posts", []):
        if p.get("hash") == h:
            for name, r in (p.get("results") or {}).items():
                if r.get("ok"):
                    done.add(name)
    return done


# ─────────────────────────────────────────────
# 4. 모드별 실행
# ─────────────────────────────────────────────
def run_list(reviews, seen):
    print(f"\n📋 조회 결과 (게시하지 않음) — 최근순")
    for rv in sorted(reviews, key=lambda r: r["created_raw"], reverse=True)[:20]:
        reason = skip_reason(rv, seen) if seen else None
        tag = f"제외: {reason}" if reason else "게시 대상"
        print(f"- {rv['created_raw'][:10]} ★{rv['stars']} [{tag}] {rv['text'][:70]!r}")
    if seen is None:
        print("\nℹ️ google_reviews_seen.json 이 아직 없습니다. post 모드 첫 실행 때 기준선이 자동으로 기록됩니다.")


def run_baseline(reviews, seen):
    seen = seen or {"reviews": {}}
    n = 0
    for rv in reviews:
        if rv["id"] not in seen["reviews"]:
            seen["reviews"][rv["id"]] = {"status": "baseline", "at": now_kst(), "stars": rv["stars"]}
            n += 1
    seen["baseline_at"] = seen.get("baseline_at") or now_kst()
    save_seen(seen)
    print(f"📌 기준선 기록: 기존 리뷰 {n}건을 '처리 완료'로 표시했습니다 (게시 안 함).")
    print("   이후 새로 달리는 리뷰부터 자동 게시됩니다.")


def run_post(reviews, seen):
    candidates = [rv for rv in reviews if skip_reason(rv, seen) is None]
    candidates.sort(key=lambda r: r["created_raw"])            # 가장 오래된 것부터
    print(f"🔎 게시 대상 {len(candidates)}건")
    if not candidates:
        print("ℹ️ 새로 게시할 리뷰가 없습니다. 종료.")
        return

    rv = candidates[0]
    entry = seen["reviews"].setdefault(rv["id"], {"status": "pending", "attempts": 0})
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["stars"] = rv["stars"]
    print(f"📝 선택: {rv['created_raw'][:10]} ★{rv['stars']} ({entry['attempts']}회차) {rv['text'][:80]!r}")

    env = dict(os.environ)
    env.update({
        "REVIEW_TEXT": rv["text"],
        "REVIEWER_LABEL": REVIEWER_LABEL,
        "PLATFORMS": "all",
        "STARS": str(rv["stars"]),
    })
    code = subprocess.run([sys.executable, "review_to_sns.py"], env=env).returncode

    done = posted_platforms_for(rv["text"])
    entry["posted"] = sorted(done)
    entry["at"] = now_kst()
    if set(ALL_PLATFORMS) <= done:
        entry["status"] = "done"
        print(f"✅ 3개 플랫폼 게시 완료 → 처리 완료로 기록")
    elif entry["attempts"] >= MAX_ATTEMPTS:
        entry["status"] = "gave_up"
        print(f"⚠️ {MAX_ATTEMPTS}회 시도 후 포기. 성공: {sorted(done) or '없음'} — 수동 확인 필요")
    else:
        entry["status"] = "pending"
        print(f"🔁 일부 미완료 (성공: {sorted(done) or '없음'}) — 다음 실행 때 실패한 플랫폼만 재시도")
    save_seen(seen)

    if code != 0 and not done:
        raise RuntimeError("review_to_sns.py 실행 실패 (모든 플랫폼 게시 실패)")


def main():
    print(f"▶️ 모드: {MODE} / 지점 키워드: {LOCATION_KEYWORD} / 최근 {MAX_AGE_DAYS}일")
    reviews = [normalize(r) for r in fetch_reviews()]
    seen = load_seen()

    if MODE == "list":
        run_list(reviews, seen)
    elif MODE == "baseline":
        run_baseline(reviews, seen)
    elif MODE == "post":
        if seen is None:
            print("ℹ️ 첫 실행입니다. 과거 리뷰가 한꺼번에 게시되지 않도록 기준선부터 기록합니다.")
            run_baseline(reviews, seen)
            return
        run_post(reviews, seen)
    else:
        raise RuntimeError(f"mode 값이 잘못됨: {MODE} (post / list / baseline)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 실행 실패: {e}", file=sys.stderr)
        sys.exit(1)
