# 스위처 (Switcher)

BLE 조명 스위치 제어 웹앱. 폰에서 집 조명을 켜고/끄고, 예약·타이머·배터리 확인. bleak(CoreBluetooth)로 BLE 기기에 명령 전송하는 로컬 Flask 서버(`switcher_server.py`).

## 실행 구조
- 포트 5001, `switcher.local:5001`로 폰 접속(mDNS/zeroconf).
- 상태파일(같은 폴더, **둘 다 gitignore — 개인 런타임 상태라 레포엔 없음**): `config.json`(기기주소·이름·1구/2구), `schedules.json`(예약·타이머).
- **배포 = launchd가 단일 관리자**: `~/Library/LaunchAgents/com.switcher.local.plist`(RunAtLoad+KeepAlive)가 `python3.11 switcher_server.py` 실행. 코드 바꾸면 `launchctl kickstart -k gui/$(id -u)/com.switcher.local`로 재시작. plist 경로 자체를 바꿨을 땐 `launchctl bootout` 후 `bootstrap`으로 재적재.
- 로그: `switcher.log` (같은 폴더).

## 이 레포는 public
사용자가 명시적으로 공개 요청(2026-07-12). config.json/schedules.json이 처음부터 gitignore라 기기주소·집 정보는 커밋된 적 없음 — 확인 완료.

## BLE 키 (실기기 검증됨)
1구 ON=00/OFF=01, 2구 ON=05/OFF=03. (초기 CLI `switcher.py`에서 검증된 값 — 다른 리팩터본은 키가 다를 수 있어 신뢰하지 말 것.)

## 디자인
"Clean Structural" 톤(밝은 실버 배경, 부유 흰 카드, 큰 볼드 타이포) + ON/OFF 버튼만 다크(앰버 발광 "켜기"). 앰버 액센트 `#f5a524`. 이모지 대신 Solar 아이콘 SVG 인라인(오프라인 안전). Tailwind CDN 안 씀(집 IoT 신뢰성 위해 인라인 CSS만).

## 남은 것
실기기 on/off·스캔·배터리는 헤드리스로 검증 불가 — 변경 후 항상 실폰 테스트 필요.
