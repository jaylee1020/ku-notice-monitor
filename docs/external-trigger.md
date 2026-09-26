# 매시간 실행 보장: 외부 트리거 설정

GitHub Actions의 `schedule`은 부하가 많을 때 실행을 건너뛰거나 늦춥니다. 이 저장소도
매시간(`17 * * * *`)으로 예약했지만 실제로는 하루 4~6회, 간격이 최대 6시간까지
벌어졌습니다. 외부 스케줄러가 매시간 GitHub API로 `workflow_dispatch`를 호출하면
예약 누락 없이 실행됩니다.

- 비용: 모두 무료입니다. 공개 저장소의 Actions 사용 시간은 과금되지 않고, AI 분석은
  새 공지가 있을 때만 하므로 실행 횟수가 늘어도 OpenAI 비용은 그대로입니다.
- 기존 `schedule`은 그대로 둡니다. 외부 트리거가 멈춰도(토큰 만료 등) 지금처럼 하루
  몇 번은 실행됩니다.
- 두 트리거가 겹쳐도 워크플로의 `concurrency` 설정으로 한 번에 하나씩만 실행되고,
  상태의 전송 기록 덕분에 같은 알림을 두 번 보내지 않습니다.

## 1. GitHub 토큰 발급 (약 2분)

1. GitHub → **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**
   (<https://github.com/settings/personal-access-tokens/new>)
2. 다음처럼 입력합니다.

   | 항목 | 값 |
   | --- | --- |
   | Token name | `ku-notice-monitor dispatch` |
   | Expiration | 1년 (만료일을 달력에 적어 두세요) |
   | Repository access | **Only select repositories** → `ku-notice-monitor` |
   | Permissions → Repository → **Actions** | **Read and write** |

   다른 권한은 주지 않습니다(Metadata 읽기는 자동으로 붙습니다). 이 토큰으로는
   워크플로 실행만 할 수 있고 코드나 Secrets는 건드릴 수 없습니다.
3. **Generate token**을 누르고 `github_pat_...` 값을 복사합니다. 이 화면을 벗어나면
   다시 볼 수 없습니다.

## 2. cron-job.org 설정 (약 3분)

1. <https://cron-job.org> 가입(무료) 후 **Cronjobs → Create cronjob**
2. **Common** 탭

   | 항목 | 값 |
   | --- | --- |
   | Title | `KU notice monitor` |
   | URL | `https://api.github.com/repos/jaylee1020/ku-notice-monitor/actions/workflows/monitor.yml/dispatches` |
   | Execution schedule | **Every hour**, 분은 `7` 등 정각이 아닌 값 |

3. **Advanced** 탭

   | 항목 | 값 |
   | --- | --- |
   | Request method | `POST` |
   | Headers | `Authorization: Bearer github_pat_...` (1단계 토큰) |
   | | `Accept: application/vnd.github+json` |
   | | `X-GitHub-Api-Version: 2022-11-28` |
   | | `Content-Type: application/json` |
   | Request body | `{"ref":"main"}` |
   | Time zone | `Asia/Seoul` |

4. **Notifications**에서 **실행 실패 시 이메일 알림**을 켭니다. 토큰이 만료되거나
   취소되면 GitHub이 401을 돌려주고, 이 알림으로 바로 알 수 있습니다.
5. 저장한 뒤 **Test run**을 눌러 응답 코드가 `204`(또는 `200`)인지 확인합니다.

## 3. 동작 확인

- GitHub → **Actions → 건국대 공지 모니터링**에 이벤트가 `workflow_dispatch`인 실행이
  매시간 생기는지 봅니다.
- 응답 코드별 원인

  | 코드 | 원인 |
  | --- | --- |
  | 204 / 200 | 정상 |
  | 401 | 토큰이 틀렸거나 만료됨 → 1단계로 새 토큰 발급 |
  | 403 | 토큰에 Actions 쓰기 권한이 없거나 다른 저장소만 선택됨 |
  | 404 | URL 오타, 또는 토큰의 저장소 선택에 `ku-notice-monitor`가 빠짐 |
  | 422 | 요청 본문의 `ref`가 잘못됨 (`{"ref":"main"}`인지 확인) |

## 토큰 갱신

만료 전에 1단계로 새 토큰을 만들고, cron-job.org의 `Authorization` 헤더만 바꾼 뒤
**Test run**으로 확인합니다. 기존 토큰은 GitHub 토큰 목록에서 삭제합니다.
