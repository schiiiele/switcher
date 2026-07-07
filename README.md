# 스위처 (Switcher)

BLE 스위치로 집 조명을 제어하는 로컬 웹앱. 폰에서 켜고·끄고, 요일 예약·타이머·배터리 확인을 한다. 맥이 BLE 게이트웨이 역할을 하고, 같은 와이파이의 폰에서 접속한다.

## 구성
- `switcher_server.py` — Flask 단일 서버(포트 5001). bleak로 BLE 명령 전송, 웹 UI(인라인) 서빙, 예약 스케줄러 포함.
- `config.json` — 연결 기기(주소·이름·1구/2구). *런타임 상태, git 제외.*
- `schedules.json` — 예약·타이머. *런타임 상태, git 제외.*
- `com.switcher.local.plist` — 자동 실행용 LaunchAgent(참고용 사본).
- `start_switcher.sh` — 수동 실행 스크립트(평소엔 launchd가 관리).

## 실행
```bash
pip install -r requirements.txt        # bleak, flask, zeroconf
python3.11 switcher_server.py
```
폰에서 `http://switcher.local:5001` (또는 맥 IP:5001) 접속.

## 자동 시작 (macOS launchd)
```bash
cp com.switcher.local.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.switcher.local.plist
```
`RunAtLoad`+`KeepAlive`로 로그인 시 자동 실행·크래시 자동 복구. 코드 갱신 후 재시작:
```bash
launchctl kickstart -k gui/$(id -u)/com.switcher.local
```

## 기능
- **켜기/끄기** — 마지막 동작을 화면에 낙관적 표시(서버는 실제 전구 상태를 모름). 전송 중 스피너 표시.
- **기기** — 주변 BLE 스캔 → 선택·저장. 1구/2구 지원. (연결되면 접힘)
- **연결 관리** — `연결 리셋`(멈춘 BLE 링크 정리, `blueutil` 있으면 블루투스 토글) / `기기 잊기`(설정 초기화).
- **예약** — 요일별 시각 켜기/끄기. **타이머** — N분 후 실행(30분·1시간·2시간 프리셋).
- **배터리** — 탭하면 조회.

## 디자인
밝은 미니멀(Clean Structural) + 다크 택타일 ON/OFF 버튼. Pretendard(폴백 Apple SD Gothic), Solar 아이콘 SVG 인라인(오프라인에서도 동작), 이모지 미사용. 외부 CDN 런타임 의존 없음(Pretendard 폰트만 CDN, 실패 시 시스템 폰트).

## BLE 키 매핑
1구 ON=`00` OFF=`01` · 2구 ON=`05` OFF=`03` (최초 CLI에서 검증). CHAR UUID `000015ba-…`, BATTERY UUID `000015aa-…`.
