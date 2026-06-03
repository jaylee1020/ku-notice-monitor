# 건국대학교 공지 모니터링 에이전트

건국대학교 공지사항 RSS 피드를 자동으로 수집하고, Gemini AI로 관련도를 분석하여 텔레그램으로 알림을 보내는 에이전트입니다.

## 주요 기능

- **자동 수집**: 학사/장학/취창업/국제교류/학생생활/일반 공지 RSS 피드 수집
- **AI 분석**: Google Gemini API로 사용자 프로필 기반 관련도 1-5점 평가
- **키워드 폴백**: Gemini API 실패 시 키워드 매칭으로 자동 대체
- **텔레그램 알림**: 관련 공지만 필터링하여 텔레그램 메시지로 전송
- **GitHub Actions**: 매일 오전 10시(KST) 자동 실행

## 시작하기

### 1. 저장소 Fork

이 저장소를 자신의 GitHub 계정으로 Fork합니다.

### 2. GitHub Secrets 설정

Fork한 저장소의 `Settings > Secrets and variables > Actions`에서 아래 시크릿을 등록합니다.

| Secret 이름 | 설명 | 필수 여부 |
|------------|------|---------|
| `GEMINI_API_KEY` | Google AI Studio API 키 | 권장 (없으면 키워드 매칭 사용) |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 | 필수 |
| `TELEGRAM_CHAT_ID` | 텔레그램 채팅 ID | 필수 |
| `PROFILE_JSON` | 사용자 프로필 JSON | 선택 |
| `KEYWORDS_JSON` | 관심 키워드 JSON | 선택 |

#### PROFILE_JSON 예시
```json
{
  "major": "컴퓨터공학부",
  "previous_major": "KU자유전공학부",
  "year": 2,
  "campus": "서울",
  "status": "재학"
}
```

#### KEYWORDS_JSON 예시
```json
{
  "high": ["장학", "등록금", "수강신청"],
  "medium": ["취업", "인턴", "공모전", "해외"]
}
```

### 3. 텔레그램 봇 생성

1. 텔레그램에서 `@BotFather`에게 `/newbot` 명령어 전송
2. 봇 이름과 아이디를 설정하면 **봇 토큰**을 발급받습니다
3. 봇과 대화를 시작한 뒤, `@userinfobot`으로 자신의 **채팅 ID**를 확인합니다

### 4. Gemini API 키 발급 (선택)

1. [Google AI Studio](https://aistudio.google.com/)에 접속
2. `Get API key`에서 무료 API 키 발급

### 5. GitHub Actions 활성화

Fork한 저장소의 `Actions` 탭에서 워크플로우를 활성화합니다.

## 로컬 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
export GEMINI_API_KEY="your-api-key"
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
export PROFILE_JSON='{"major":"컴퓨터공학부","year":2,"campus":"서울","status":"재학"}'
export KEYWORDS_JSON='{"high":["장학","등록금"],"medium":["취업","인턴"]}'

# 실행
python main.py
```

## 설정 파일 (config.yaml)

```yaml
# RSS 피드 활성화/비활성화
feeds:
  학사공지:
    id: 234
    enabled: true
  입찰공고공지:
    id: 239
    enabled: false  # 불필요한 게시판은 비활성화

# Gemini 관련도 임계값 (1-5점 중 이 점수 이상이면 알림)
gemini:
  model: "gemini-flash-latest"
  relevance_threshold: 3

# SSL 인증서 검증 (보안을 위해 기본 활성화)
# 건국대 서버 인증서 문제가 있을 경우에만 false로 변경
settings:
  ssl_verify: true
  # 최초 실행(state.json 없음) 시 기존 공지를 알림 없이 '확인함' 처리할지 여부.
  # true(기본): 과거 공지 대량 알림/크롤링 폭발을 방지하고 이후 새 공지부터 분석.
  seed_on_first_run: true
```

> 참고: 관련 공지는 점수 내림차순으로 정렬되며, 동점일 경우 발행일시가 최신인
> 공지가 먼저 표시됩니다. 본문/이미지/첨부 크롤링은 동시 요청 수가 제한되어
> 있어 서버 부담과 차단 위험을 줄입니다.

## 프로젝트 구조

```
ku-notice-monitor/
├── main.py          # 메인 실행 파일 (워크플로우 조율)
├── config.py        # config.yaml 로딩 및 유효성 검증
├── feeds.py         # RSS 피드 수집 (비동기 aiohttp, BeautifulSoup 본문 파싱)
├── matcher.py       # Gemini API 관련도 분석 (tenacity 재시도 로직)
├── notifier.py      # 텔레그램 알림 (오류 알림 포함)
├── net.py           # 공통 HTTP/SSL 유틸리티 (feeds/matcher 공유)
├── state.py         # 처리된 공지 ID 상태 관리 (원자적 쓰기)
├── models.py        # Article/Attachment 데이터 모델
├── constants.py     # 전역 상수
├── config.yaml      # 설정 파일
├── state.json       # 처리된 공지 ID 상태 (자동 생성)
└── .github/
    └── workflows/
        ├── ci.yml       # 린트 + 테스트 (push/PR)
        └── monitor.yml  # GitHub Actions 모니터링 워크플로우
```

## 트러블슈팅

### 텔레그램 알림이 오지 않아요
- GitHub Secrets에 `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`가 올바르게 설정되어 있는지 확인하세요.
- 봇과 직접 대화를 시작했는지 확인하세요 (봇에게 먼저 메시지를 보내야 합니다).

### Gemini 분석 없이 키워드 매칭만 사용돼요
- `GEMINI_API_KEY`가 설정되어 있는지 확인하세요.
- API 할당량을 초과하지 않았는지 Google AI Studio에서 확인하세요.
- `KEYWORDS_JSON`에 관심 키워드를 설정하면 폴백 정확도가 높아집니다.

### Actions 로그를 보고 싶어요
- 저장소의 `Actions` 탭에서 최근 실행 기록과 로그를 확인할 수 있습니다.
- 실패 시 텔레그램으로 Actions 로그 링크가 자동 전송됩니다.

### SSL 오류가 발생해요
- 기본값은 `ssl_verify: true`(보안 권장)입니다.
- 건국대 서버 인증서 체인 문제로 실패할 경우에만 `config.yaml`에서 `ssl_verify: false`로 일시 전환하세요.
- `ssl_verify: false`로 운영 중이면 매 실행마다 `check_ssl_health()`가 현재 인증서 상태를 점검하고 로그에 기록하므로, 문제가 해결되면 다시 `true`로 되돌릴 수 있습니다.
