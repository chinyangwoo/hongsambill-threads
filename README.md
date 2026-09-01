# 홍삼빌호텔 스레드 완전 자동 포스팅 앱 🏨🤖

사진만 폴더에 넣어두면, Claude AI가 글을 쓰고 하루 3번(오전 8시 · 낮 12시 30분 · 저녁 7시) 랜덤 사진 3장과 함께 스레드에 자동 게시합니다. **서버 비용 0원** — GitHub Actions가 무료로 24시간 돌아갑니다. PC를 꺼놔도 됩니다.

---

## 동작 원리

```
[사장님]  images/ 폴더에 사진 업로드 (끝!)
    ↓
[하루 3회 자동 실행]
    1. topics.json 의 주제를 순서대로 선택
    2. Claude AI가 스레드 스타일 홍보 글 작성 (최근 글과 중복 방지)
    3. images/ 에서 랜덤 3장 추출
    4. 스레드에 글 + 사진 3장 캐러셀 게시
    5. 기록 저장 → 다음 글에서 같은 내용 반복 안 함
```

---

## 최초 설정 (약 30분, 한 번만 하면 됩니다)

### 1단계. GitHub 저장소 만들기
1. https://github.com 가입 후 **New repository** 클릭
2. 이름: `hongsambill-threads` (아무거나 가능)
3. **Public(공개)** 으로 설정 ← 중요! 스레드가 이미지를 가져가려면 공개여야 합니다
4. 이 폴더의 파일 전체를 저장소에 업로드 (웹에서 드래그&드롭 가능)

> ⚠️ 저장소가 공개이므로 **호텔 홍보용 사진만** 넣으세요. 개인 사진·문서는 넣지 마세요.

### 2단계. Meta 개발자 앱 만들기 (Threads API 권한)
1. https://developers.facebook.com 접속 → 로그인 → **내 앱 → 앱 만들기**
2. 사용 사례에서 **"Threads API 액세스"** 선택 → 앱 생성
3. 왼쪽 메뉴 **Threads API 사용 사례 → 설정**에서 권한 추가:
   - `threads_basic`
   - `threads_content_publish`
4. **역할 → Threads 테스터**에 호텔 스레드 계정(@계정명) 추가
5. 스레드 앱에서 **설정 → 계정 → 웹사이트 권한 → 초대** 수락
6. 개발자 대시보드로 돌아와 **액세스 토큰 생성** 클릭 → 긴 토큰 문자열 복사
   (본인 계정에 게시하는 용도라서 **앱 검수 없이** 바로 사용 가능합니다)

### 3단계. Threads 사용자 ID 확인
브라우저 주소창에 아래를 입력 (토큰 부분만 교체):
```
https://graph.threads.net/v1.0/me?fields=id,username&access_token=여기에토큰붙여넣기
```
→ 나오는 `"id": "숫자"` 를 복사해 둡니다.

### 4단계. Claude API 키 발급
1. https://console.anthropic.com 가입 → **API Keys → Create Key**
2. 발급된 `sk-ant-...` 키 복사
3. 결제 수단 등록 (하루 3회 글 생성 비용은 월 1천 원 미만 수준)

### 5단계. GitHub Secrets 등록
저장소 → **Settings → Secrets and variables → Actions → New repository secret** 에서 3개 등록:

| 이름 | 값 |
|---|---|
| `ANTHROPIC_API_KEY` | 4단계의 Claude API 키 |
| `THREADS_ACCESS_TOKEN` | 2단계의 액세스 토큰 |
| `THREADS_USER_ID` | 3단계의 숫자 ID |

**(선택) 토큰 완전 자동 갱신:** Threads 토큰은 60일마다 갱신이 필요합니다. 스크립트가 40일마다 자동 갱신하는데, 갱신된 토큰을 Secrets에 자동 저장하려면 GitHub **Settings(개인) → Developer settings → Fine-grained tokens**에서 이 저장소의 `Secrets: Read and write` 권한 토큰을 만들어 `GH_PAT` 라는 이름의 Secret으로 추가하세요. 안 하셔도 되지만, 그 경우 40일에 한 번 Actions 로그의 안내에 따라 토큰을 수동 교체해야 합니다.

### 6단계. 사진 넣고 테스트
1. `images/` 폴더에 호텔 사진을 업로드 (JPG/PNG, 8MB 이하, **파일명은 영문·숫자 권장**: `pool_01.jpg` 등)
2. 저장소 → **Actions → 스레드 자동 포스팅 → Run workflow** 클릭 → 1~2분 뒤 스레드 확인!

---

## 평소 사용법

- **사진 추가/교체**: `images/` 폴더에 파일 넣기 — 이게 전부입니다
- **주제 바꾸기**: `topics.json` 열어서 `topics` 목록 수정 (이벤트, 시즌 프로모션 등 자유롭게)
- **시간 바꾸기**: `.github/workflows/post.yml` 의 cron 수정 (UTC 기준 = 한국시간 −9시간)
- **글 톤 바꾸기**: `threads_auto_post.py` 안의 프롬프트(system 부분) 수정
- **하루 2회로 줄이기**: post.yml 에서 cron 한 줄 삭제

## 자주 묻는 질문

**Q. 게시가 안 돼요.**
Actions 탭 → 실패한 실행 클릭 → 로그 확인. 대부분 토큰 만료(재발급 후 Secret 교체) 또는 이미지 파일명의 한글/특수문자 문제입니다.

**Q. 사진이 3장 미만이면?**
게시를 건너뛰고 오류 로그를 남깁니다. 최소 3장, 넉넉히 20~30장을 넣어두면 매번 다른 조합이 나갑니다.

**Q. 같은 사진이 반복돼요.**
랜덤 추출이라 겹칠 수 있습니다. 사진 수를 늘릴수록 겹침이 줄어듭니다.

**Q. Threads API 게시 한도는?**
계정당 24시간에 250회 — 하루 3회는 여유가 아주 많습니다.
