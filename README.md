# 건국대학교 공지 모니터링 봇

건국대학교 RSS 공지사항을 자동으로 수집하고, Gemini AI로 사용자 프로필에 맞는 관련 공지를 분석하여 텔레그램으로 알림을 보내주는 봇입니다.

## 주요 기능

- **공지 자동 수집**: 건국대 RSS 피드에서 학사공지, 장학공지, 취창업공지 등 7개 게시판 모니터링
- **AI 관련도 분석**: Gemini API로 사용자 프로필(학과, 학년, 관심 키워드)에 맞는 공지를 1~5점으로 평가
- **텔레그램 알림**: 관련도 높은 공지만 선별하여 텔레그램으로 전송
- **검색 기능**: 텔레그램 메시지로 공지 검색 (Gemini AI 검색 + 키워드 폴백)
- **GitHub Actions**: 자동 스케줄링으로 주기적 모니터링 및 검색 응답

## 프로젝트 구조

```
├── main.py              # 메인 실행 파일 (모니터링 에이전트)
├── feeds.py             # RSS 피드 수집 및 파싱
├── matcher.py           # Gemini 기반 관련도 분석
├── notifier.py          # 텔레그램 알림 전송
├── search_handler.py    # 텔레그램 메시지 수신 → 검색 → 응답
├── config.yaml          # 피드/Gemini/프로필 설정
├── requirements.txt     # Python 의존성
├── .github/workflows/
│   ├── monitor.yml      # 공지 모니터링 워크플로우
│   └── search.yml       # 검색 응답 워크플로우
└── com.konkuk.monitor.plist  # macOS launchd 설정 (로컬 실행용)
```

## 설치 및 설정

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

### 2. 환경변수 설정

| 환경변수 | 설명 |
|---------|------|
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 알림 받을 텔레그램 채팅 ID |
| `PROFILE_JSON` | 사용자 프로필 (JSON) |
| `KEYWORDS_JSON` | 관심 키워드 (JSON) |

`PROFILE_JSON` 예시:
```json
{"major":"컴퓨터공학부","previous_major":"KU자유전공학부","year":2,"campus":"서울","status":"재학"}
```

`KEYWORDS_JSON` 예시:
```json
{"high":["장학","등록금","수강신청"],"medium":["취업","인턴","공모전"]}
```

### 3. 실행

```bash
# 공지 모니터링
python main.py

# 검색 응답 핸들러
python search_handler.py
```

### GitHub Actions 설정

1. 리포지토리 Settings > Secrets에 위 환경변수를 등록
2. `.github/workflows/monitor.yml`의 `on` 섹션에서 `schedule` 추가:
   ```yaml
   on:
     schedule:
       - cron: '0 0 * * *'  # 매일 오전 9시(KST)
     workflow_dispatch:
   ```

## 동작 흐름

1. RSS 피드에서 전체 공지 수집
2. `state.json`과 비교하여 새 공지 필터링
3. 새 공지 본문 크롤링
4. Gemini API로 사용자 프로필 기반 관련도 분석 (실패 시 키워드 매칭 폴백)
5. 관련도 기준 이상인 공지를 텔레그램으로 전송
6. 상태 저장 (90일 지난 기록 자동 정리)
