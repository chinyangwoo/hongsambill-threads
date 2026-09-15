# -*- coding: utf-8 -*-
"""
페이스북 토큰 교체 도우미
=========================
그래프 API 탐색기를 쓰지 않고 페이스북 토큰을 교체하기 위한 도구.
Actions 의 "페이스북 토큰 교체" 워크플로(fb_token.yml)가 실행한다.

입력(FB_TOKEN_INPUT):
  페이스북 로그인 승인 후 주소창의 전체 URL 을 그대로 붙여넣어도 되고,
  액세스 토큰 문자열만 붙여넣어도 된다. 둘 다 알아서 처리한다.

동작:
  1. 입력에서 access_token 을 추출
  2. facebook_auto_post.ensure_permanent_token() 재사용
     → 사용자 토큰을 영구 페이지 토큰으로 변환하고 GH_PAT 로 Secrets 자동 갱신
  3. 최종 토큰에 첫 댓글 권한(pages_manage_engagement)이 있는지 확인해서 출력
"""

import os
import re
import sys
import urllib.parse

raw = os.environ.get("FB_TOKEN_INPUT", "").strip()
m = re.search(r"access_token=([^&#\s]+)", raw)
token = urllib.parse.unquote(m.group(1)) if m else raw

if not token or len(token) < 30 or any(c in token for c in " /?:#&="):
    print("❌ 입력에서 토큰을 찾지 못했습니다. 승인 후 주소창의 전체 URL "
          "(access_token=... 이 포함된 것) 또는 토큰 문자열을 그대로 붙여넣어 주세요.", file=sys.stderr)
    sys.exit(1)

# facebook_auto_post 가 import 시점에 환경변수를 읽으므로 먼저 넣어 준다
os.environ["FB_PAGE_ACCESS_TOKEN"] = token

import facebook_auto_post as fb  # noqa: E402

if not fb.APP_SECRET:
    print("❌ FB_APP_SECRET Secrets 가 비어 있어 영구 토큰 변환을 할 수 없습니다.", file=sys.stderr)
    sys.exit(1)

# 사용자 토큰 → 장기 토큰 → 영구 페이지 토큰 변환 + Secrets 자동 저장 (기존 로직 재사용)
fb.ensure_permanent_token()
fb.resolve_page_id()

info = fb.http_json(
    f"{fb.FB_API}/debug_token?input_token={fb.ACCESS_TOKEN}"
    f"&access_token={fb.APP_ID}|{fb.APP_SECRET}"
)["data"]
scopes = info.get("scopes", [])
print(f"최종 토큰 권한: {scopes}")
if "pages_manage_engagement" in scopes:
    print("✅ 첫 댓글 권한(pages_manage_engagement) 확인 완료 — 다음 게시부터 예약 안내 댓글이 자동 등록됩니다.")
else:
    print("⚠️ 새 토큰에도 pages_manage_engagement 가 없습니다. "
          "로그인 승인 화면에서 권한을 모두 허용했는지 확인하고 다시 시도해 주세요.")
    sys.exit(1)
