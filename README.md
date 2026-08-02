# KU Notice Monitor

건국대학교 공지를 매시간 수집하고, 대상 조건·행동·손실·마감·관심사를 각각 분석해 텔레그램으로 알려주는 모니터입니다.

## 동작 방식

- RSS와 공지 본문을 비동기로 수집합니다.
- GPT‑5.6 Luna는 공지별로 사실을 독립 추출하고, Pydantic Structured Outputs가 결과 형식을 보장합니다.
- 대상 적격성, 관심도, 필수 행동, 놓쳤을 때의 손실을 하나의 관련도 점수로 합치지 않습니다.
- 결정론적 정책 엔진이 `immediate` / `digest` / `review` / `suppress`를 선택합니다.
- 대상이 불명확한 고위험 공지는 숨기지 않고 `review`로 보냅니다.
- 첨부파일은 핵심 판정에 필요하다고 나온 공지만 2차 분석해 비용과 지연을 줄입니다.
- OpenAI 호출이 실패하면 해당 공지만 보수적으로 판정하고 백오프로 재분류합니다.
- RSS가 그대로여도 최근 공지의 상세 본문을 다시 확인해 `[수정]`으로 감지합니다.
- 알림은 영구 outbox에 먼저 기록하고, 실제 전송에 성공한 조각만 완료 처리합니다.
- 런타임 상태는 `main`이 아닌 전용 `monitor-state` 브랜치에 저장합니다.

## 주요 기능

- 학사·장학·취창업·국제교류·학생생활·일반·채용 공지 통합
- 대상 적격성·관심사·의무·손실을 분리한 구조화 분석
- `immediate` / `digest` / `review` / `suppress` 전달 정책
- 신청·서류·납부·행사 날짜를 종류별로 추출하고 가장 가까운 행동 마감 D-day 표시
- 학생이 해야 할 구체적인 행동 요약
- 이미지와 첨부 분석, 격리 변환된 HWP/HWPX 입력
- 텍스트 PDF는 `pdf-inspector`로 로컬 Markdown 변환하고 스캔·혼합 PDF는 원본 입력
- 허용 도메인·공인 IP·리디렉션 검증과 스트리밍 다운로드 상한
- 키워드 폴백, 부분 실패 복구, 지수 백오프
- 본문 수정 감지, 피드별 상태 검사와 중복 제거
- AI 토큰·fallback·억제·전송 실패를 포함한 구조화 실행 요약
- GitHub Actions 매시간 자동 실행

## 설정

### GitHub Actions Secrets

저장소의 Actions Secret에 다음 값을 등록합니다.

| 이름 | 설명 | 필수 |
| --- | --- | --- |
| `OPENAI_API_KEY` | OpenAI Platform 프로젝트 API 키 | 권장 |
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급한 텔레그램 봇 토큰 | 필수 |
| `TELEGRAM_CHAT_ID` | 알림을 받을 텔레그램 채팅 ID | 필수 |
| `PROFILE_TEXT` | 자연어로 작성한 사용자 사실·알림 선호 문서 | 권장 |
| `PROFILE_JSON` | 기존 학생 프로필 JSON(마이그레이션 호환) | 선택 |
| `KEYWORDS_JSON` | 폴백용 관심 키워드 JSON | 선택 |

`PROFILE_TEXT` 예시:

```text
나는 건국대학교 서울캠퍼스 컴퓨터공학부 2학년이다.
서울특별시에 거주한다. 다른 지역 주민 전용 사업은 보내지 마라.
부모 주소, 소득, 장학재단 이용 여부처럼 적지 않은 조건은 추측하지 마라.
장학금, 수강신청, 복학, 병역 관련 공지는 중요하게 알려줘.
```

사용자가 쓴 문서는 실행 중에만 구조화되며 원문과 프로필 스냅샷은
`monitor-state` 브랜치에 저장하지 않습니다. 내용이 바뀌었는지 확인하는 해시만
상태에 보관합니다. `PROFILE_TEXT`가 없으면 기존 `PROFILE_JSON`과
`KEYWORDS_JSON`을 사용합니다.

기존 `PROFILE_JSON` 예시:

```json
{
  "major": "컴퓨터공학부",
  "previous_major": "KU자유전공학부",
  "year": 2,
  "campus": "서울",
  "status": "재학"
}
```

`KEYWORDS_JSON` 예시:

```json
{
  "high": ["장학", "등록금", "수강신청"],
  "medium": ["취업", "인턴", "공모전", "해외"]
}
```

### `config.yaml`

```yaml
ai:
  model: "gpt-5.6-luna"
  reasoning_effort: "medium"
  max_concurrency: 4
  request_timeout_seconds: 45
  image_detail: "low"
  file_detail: "low"

classification:
  action_window_days: 21
  suppress_speculative_opportunities: true

notifications:
  digest_hour_kst: 21
  notify_empty_runs: false
```

- `reasoning_effort: medium`: Luna의 표준 thinking 기본값으로, 정확도와 비용의 균형점입니다.
- `action_window_days`: 관련 행동 마감이 이 기간 안이면 즉시 알림으로 승격합니다.
- `suppress_speculative_opportunities`: 프로필로 뒷받침되는 자격 경로가 없는
  선택적 기회를 알림에서 제외합니다.
- 이미지와 PDF의 `low` detail은 2차 첨부 분석의 토큰 사용을 줄입니다.
- `notify_empty_runs: false`는 매시간 불필요한 “새 공지 없음” 메시지를 막습니다.

## 로컬 실행

Python 3.12 이상이 필요합니다.

```bash
uv sync --locked --extra dev
uv run ku-notice-monitor
```

로컬 시크릿은 커밋되지 않는 `.env.local`에 둡니다.

```dotenv
OPENAI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## 프로젝트 구조

```text
src/ku_notice_monitor/
  analysis_models.py   OpenAI Structured Outputs 스키마
  classification.py    전달 결정을 내리는 정책 엔진
  openai_classifier.py Responses API·선택적 첨부 분석
  document_extract.py  HWP/HWPX·PDF 격리 변환
  pdf_extract_worker.py  텍스트 PDF 판별·Markdown 추출 워커
  prompts.py           근거 중심 사실 추출 프롬프트
  matcher.py           AI/규칙 폴백·근거 검증·정책 조율
  feeds.py             RSS·본문·이미지·첨부 수집
  net.py               SSRF 방어와 제한 다운로드
  notifier.py          텔레그램 메시지와 전송 결과
  state.py             상태·outbox·재시도·수정 감지
  main.py              실행 파이프라인
evals/                 정책·근거 검증 회귀 사례
tests/                 단위·장애·종단간 테스트
```

## 검증

```bash
uv run --no-sync ruff check .
uv run --no-sync mypy src
uv run --no-sync pytest --cov=ku_notice_monitor --cov-branch
```

CI는 잠긴 의존성으로 Python 3.12와 3.13에서 린트·타입 검사·분기
커버리지 테스트를 실행합니다.

분류 원칙과 골든 케이스 운영법은
[`docs/classification-design.md`](docs/classification-design.md)에 정리되어 있습니다.
