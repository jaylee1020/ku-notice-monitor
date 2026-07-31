# KU Notice Monitor

건국대학교 공지를 매시간 수집하고, 대상 조건·행동·손실·마감·관심사를 각각 분석해 텔레그램으로 알려주는 모니터입니다.

## 동작 방식

- RSS와 공지 본문을 비동기로 수집합니다.
- GPT‑5.6 Luna는 공지별로 사실을 독립 추출하고, Pydantic Structured Outputs가 결과 형식을 보장합니다.
- 대상 적격성, 관심도, 필수 행동, 놓쳤을 때의 손실을 하나의 관련도 점수로 합치지 않습니다.
- 결정론적 정책 엔진이 `immediate` / `digest` / `review` / `suppress`를 선택합니다.
- 대상이 불명확한 고위험 공지는 숨기지 않고 `review`로 보냅니다.
- 첨부파일은 핵심 판정에 필요하다고 나온 공지만 2차 분석해 비용과 지연을 줄입니다.
- OpenAI 호출이 실패하면 해당 공지만 보수적인 규칙 기반 판정으로 대체합니다.
- 동일 ID의 내용이 바뀌면 `[수정]` 공지로 다시 분석합니다.
- 기존 공지, 내용 해시, 일일 요약 대기열은 `state.json`에 저장합니다.

## 주요 기능

- 학사·장학·취창업·국제교류·학생생활·일반·채용 공지 통합
- 대상 적격성·관심사·의무·손실을 분리한 구조화 분석
- `immediate` / `digest` / `review` / `suppress` 전달 정책
- 신청·서류·납부·행사 날짜를 종류별로 추출하고 가장 가까운 행동 마감 D-day 표시
- 학생이 해야 할 구체적인 행동 요약
- 이미지와 PDF 등 파일 입력
- 키워드 폴백, 부분 실패 복구, 지수 백오프
- 수정 공지 감지와 피드 중복 제거
- GitHub Actions 매시간 자동 실행

## 설정

### GitHub Actions Secrets

저장소의 Actions Secret에 다음 값을 등록합니다.

| 이름 | 설명 | 필수 |
| --- | --- | --- |
| `OPENAI_API_KEY` | OpenAI Platform 프로젝트 API 키 | 권장 |
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급한 텔레그램 봇 토큰 | 필수 |
| `TELEGRAM_CHAT_ID` | 알림을 받을 텔레그램 채팅 ID | 필수 |
| `PROFILE_JSON` | 학생 프로필 JSON | 선택 |
| `KEYWORDS_JSON` | 폴백용 관심 키워드 JSON | 선택 |

`PROFILE_JSON` 예시:

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
  reasoning_effort: "low"
  max_concurrency: 4
  image_detail: "low"
  file_detail: "low"

classification:
  action_window_days: 21

notifications:
  digest_hour_kst: 21
  notify_empty_runs: false
```

- `reasoning_effort: low`: 사실 추출 정확도와 처리 비용의 균형값입니다.
- `action_window_days`: 관련 행동 마감이 이 기간 안이면 즉시 알림으로 승격합니다.
- 이미지와 PDF의 `low` detail은 2차 첨부 분석의 토큰 사용을 줄입니다.
- `notify_empty_runs: false`는 매시간 불필요한 “새 공지 없음” 메시지를 막습니다.

## 로컬 실행

Python 3.12 이상이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python main.py
```

로컬 시크릿은 커밋되지 않는 `.env.local`에 둡니다.

```dotenv
OPENAI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## 프로젝트 구조

```text
analysis_models.py   OpenAI Structured Outputs 스키마
classification.py    전달 결정을 내리는 정책 엔진
openai_classifier.py 공지별 Responses API·선택적 첨부 분석
prompts.py           축 분리형 사실 추출 프롬프트
matcher.py           AI/규칙 폴백과 정책 조율
feeds.py             RSS·본문·이미지·첨부 수집
notifier.py          긴급 알림과 일일 요약 메시지
state.py             확인 상태·수정 감지·요약 대기열
main.py              실행 파이프라인
```

## 검증

```bash
ruff check .
pytest -q
```

CI는 Python 3.12와 3.13에서 린트와 테스트를 실행합니다.

분류 원칙과 골든 케이스 운영법은
[`docs/classification-design.md`](docs/classification-design.md)에 정리되어 있습니다.
