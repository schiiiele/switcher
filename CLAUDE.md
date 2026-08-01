# 스위처 (Switcher)

BLE 조명 스위치 제어 웹앱. 폰에서 집 조명을 켜고/끄고, 예약·타이머·배터리 확인. bleak(CoreBluetooth)로 BLE 기기에 명령 전송하는 로컬 Flask 서버(`switcher_server.py`).

## 실행 구조
- 포트 5001, `switcher.local:5001`로 폰 접속(mDNS/zeroconf).
- 상태파일(같은 폴더, **전부 gitignore — 개인 런타임 상태/비밀이라 레포엔 없음**): `config.json`(기기주소·이름·1구/2구), `schedules.json`(예약·타이머), `battery_state.json`(마지막 배터리값·경고단계·실패연속일), `hub_push.json`(허브 푸시 URL+HUB_TOKEN).
- **배포 = launchd가 단일 관리자**: `~/Library/LaunchAgents/com.switcher.local.plist`(RunAtLoad+KeepAlive)가 `python3.11 switcher_server.py` 실행. 코드 바꾸면 `launchctl kickstart -k gui/$(id -u)/com.switcher.local`로 재시작. plist 경로 자체를 바꿨을 땐 `launchctl bootout` 후 `bootstrap`으로 재적재.
- 로그: `switcher.log` (같은 폴더).

## 이 레포는 public
사용자가 명시적으로 공개 요청(2026-07-12). config.json/schedules.json이 처음부터 gitignore라 기기주소·집 정보는 커밋된 적 없음 — 확인 완료.

## 배터리 자동 감시 → 폰 푸시 (2026-08-02)
- `battery_watch_loop` 스레드가 **매일 21시**에 1회 배터리를 읽는다. `now.hour >= 21` + `last_date` 비교라, 맥이 21시에 자고 있었어도 깨어난 뒤 그날 안에 한 번은 검사한다.
- 읽기 실패 시 **30분 간격 3회** 재시도. 그래도 실패하면 `fail_streak` +1, **3일 연속이면** "조명이 안 잡혀요" 푸시 1회(`fail_alerted`로 도배 방지). 성공하면 둘 다 초기화.
- 경고는 **계단식** `BATTERY_STEPS = [15, 10, 5]` — 단계마다 딱 한 번. 15% 위로 회복하면(=건전지 갈았음) `alerted_step`을 None으로 되돌려 다음 방전 때 15%부터 다시 알린다. 부분 회복(예: 4%→12%)도 `level > prev` 가지에서 단계가 풀린다.
- **푸시 발송 실패 시 `alerted_step`을 기록하지 않는다** → 다음 검사에서 자동 재시도.
- 수동 확인(`GET /battery`)도 `record_battery`를 타서 같은 규칙을 적용. `GET /battery/state`는 BLE를 안 건드리고 저장값만 준다(화면 초기 렌더용).

### 왜 허브 서버를 경유하나
스위처는 `http://switcher.local`이라 **iOS가 Push API 자체를 막는다**(https가 아니면 보안 컨텍스트 아님 → 서비스워커/구독 등록 불가). 그래서 직접 못 쏘고, 이미 폰 구독을 들고 있는 허브(https)의 `POST /hub/api/push/notify {title, body, url?}`에 `X-Hub-Token` 붙여 부탁한다. 설정은 `hub_push.json` 또는 환경변수 `HUB_PUSH_URL`+`HUB_TOKEN`. 설정이 없으면 조용히 건너뛴다(알림 때문에 조명 기능이 멈추면 안 됨).

## BLE 키 (실기기 검증됨)
1구 ON=00/OFF=01, 2구 ON=05/OFF=03. (초기 CLI `switcher.py`에서 검증된 값 — 다른 리팩터본은 키가 다를 수 있어 신뢰하지 말 것.)

## 디자인
"Clean Structural" 톤(밝은 실버 배경, 부유 흰 카드, 큰 볼드 타이포) + ON/OFF 버튼만 다크(앰버 발광 "켜기"). 앰버 액센트 `#f5a524`. 이모지 대신 Solar 아이콘 SVG 인라인(오프라인 안전). Tailwind CDN 안 씀(집 IoT 신뢰성 위해 인라인 CSS만).

## 남은 것
실기기 on/off·스캔·배터리는 헤드리스로 검증 불가 — 변경 후 항상 실폰 테스트 필요.
