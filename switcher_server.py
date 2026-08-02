import asyncio
import binascii
import threading
import time
import json
import os
import sys
import shutil
import subprocess
import platform
from datetime import datetime
from bleak import BleakClient, BleakScanner
import base64
from flask import Flask, jsonify, request, Response

# ── 경로 설정 ────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SCHEDULE_FILE = os.path.join(BASE_DIR, "schedules.json")
BATTERY_STATE_FILE = os.path.join(BASE_DIR, "battery_state.json")
# {"url": ..., "token": ...} — 이 레포는 public이라 gitignore. 환경변수로도 줄 수 있다.
HUB_PUSH_FILE = os.path.join(BASE_DIR, "hub_push.json")

# ── BLE UUIDs (고정) ─────────────────────────────
CHAR_UUID    = "000015ba-0000-1000-8000-00805f9b34fb"
BATTERY_UUID = "000015aa-0000-1000-8000-00805f9b34fb"
ON_KEY1      = binascii.a2b_hex("00")  # 1구 ON
OFF_KEY1     = binascii.a2b_hex("01")  # 1구 OFF
ON_KEY2      = binascii.a2b_hex("05")  # 2구 ON
OFF_KEY2     = binascii.a2b_hex("03")  # 2구 OFF

PORT = 5001

# ── Config 로드/저장 ─────────────────────────────
def _atomic_save_json(path, data, indent=None):
    """쓰다가 죽어도 원본이 깨지지 않게 임시파일에 쓰고 교체."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"device_address": None, "device_name": None, "device_type": 1}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data):
    _atomic_save_json(CONFIG_FILE, data, indent=2)

config = load_config()

app = Flask("switcher_server")
lock = threading.Lock()
schedules = []
schedule_id_counter = 1

# ── 예약 저장/불러오기 ──────────────────────────
def save_schedules():
    _atomic_save_json(SCHEDULE_FILE, {"counter": schedule_id_counter, "schedules": schedules})

def load_schedules():
    global schedules, schedule_id_counter
    if not os.path.exists(SCHEDULE_FILE):
        return
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        schedule_id_counter = data.get("counter", 1)
        loaded = data.get("schedules", [])
        now = time.time()
        schedules[:] = [s for s in loaded if not (s["type"] == "timer" and s["trigger_at"] <= now)]
    except Exception as e:
        print(f"[예약 불러오기 실패] {e}")

# ── BLE: 이벤트 루프 헬퍼 ────────────────────────
def _run_ble(coro):
    """짧은 BLE 코루틴을 새 루프에서 동기 실행."""
    with lock:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

# ── BLE 명령 ────────────────────────────────────
async def _with_device(op, attempts=3):
    """저장된 기기에 연결해 op(client)를 실행. 실패 시 재시도.

    광고 주기가 느려 연결 순간 신호를 놓치면 BleakDeviceNotFoundError로
    바로 실패하므로, 잠깐 쉬었다 재시도하면 대부분 잡힌다.
    """
    addr = config.get("device_address")
    if not addr:
        raise RuntimeError("장치가 설정되지 않았습니다.")
    last_err = None
    for i in range(attempts):
        try:
            async with BleakClient(addr, timeout=15) as client:
                return await op(client)
        except Exception as e:
            last_err = e
            print(f"[BLE 실패 {i + 1}/{attempts}회차] {e}")
            if i < attempts - 1:
                await asyncio.sleep(2)
    raise last_err

async def _send_command(key):
    await _with_device(lambda client: client.write_gatt_char(CHAR_UUID, key))

def run_command(action):
    device_type = config.get("device_type", 1)
    if device_type == 2:
        key = ON_KEY2 if action == "on" else OFF_KEY2
    else:
        key = ON_KEY1 if action == "on" else OFF_KEY1
    _run_ble(_send_command(key))

# ── 배터리 조회 ─────────────────────────────────
async def _read_battery():
    async def op(client):
        val = await client.read_gatt_char(BATTERY_UUID)
        return val[0]
    return await _with_device(op)

def get_battery_level():
    return _run_ble(_read_battery())

# ── 폰 푸시 (허브 서버 경유) ──────────────────────
# 스위처는 http://switcher.local 로 열려서 iOS가 웹푸시를 아예 막는다(https가 아니면
# 보안 컨텍스트가 아니라 Push API 등록 자체가 안 됨). 그래서 알림은 직접 쏘지 않고,
# 이미 폰 구독을 들고 있는 공개 허브 서버(https)에 "대신 보내줘"라고 부탁한다.
def load_hub_push():
    url, token = os.environ.get("HUB_PUSH_URL"), os.environ.get("HUB_TOKEN")
    if url and token:
        return {"url": url, "token": token}
    if os.path.exists(HUB_PUSH_FILE):
        try:
            with open(HUB_PUSH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print(f"[푸시] 설정 파일을 못 읽음: {e}", flush=True)
    return {}

def hub_push(title, body, url="./"):
    """보냈으면 True. 실패해도 예외를 밖으로 안 흘린다 — 알림 때문에 조명 기능이 멈추면 안 된다."""
    import urllib.request
    cfg = load_hub_push()
    if not cfg.get("url") or not cfg.get("token"):
        print("[푸시] hub_push.json 없음 — 알림 건너뜀", flush=True)
        return False
    req = urllib.request.Request(
        cfg["url"], method="POST",
        data=json.dumps({"title": title, "body": body, "url": url}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Hub-Token": cfg["token"]})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        print(f"[푸시] {d.get('sent')}건 발송 — {title}", flush=True)
        return bool(d.get("ok"))
    except Exception as e:
        print(f"[푸시] 실패: {e}", flush=True)
        return False

# ── 배터리 자동 감시 ─────────────────────────────
# 조명 배터리는 하루에 1%도 잘 안 닳아서 하루 한 번이면 충분하다. 읽으려면 BLE로 기기에
# 붙어야 해서 공짜가 아니고 실제로 종종 실패한다(로그에 기록 있음) — 그래서 재시도를 둔다.
BATTERY_CHECK_HOUR = 21          # 매일 저녁 9시
BATTERY_STEPS = [15, 10, 5]      # 계단식 경고 — 단계마다 딱 한 번씩만 알림
BATTERY_RETRY_SEC = 30 * 60
BATTERY_MAX_TRIES = 3
BATTERY_FAIL_DAYS = 3            # 연속 이만큼 실패하면 "기기가 안 잡힌다" 알림

def load_battery_state():
    if os.path.exists(BATTERY_STATE_FILE):
        try:
            with open(BATTERY_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"level": None, "at": None, "alerted_step": None,
            "fail_streak": 0, "fail_alerted": False}

def save_battery_state(st):
    _atomic_save_json(BATTERY_STATE_FILE, st, indent=2)

def record_battery(level):
    """읽은 값 저장 + 계단식 경고 판정. 수동 확인(/battery)에서도 같은 규칙을 탄다."""
    st = load_battery_state()
    st.update(level=level, at=datetime.now().isoformat(timespec="seconds"),
              fail_streak=0, fail_alerted=False)
    reached = [s for s in BATTERY_STEPS if level <= s]
    step = min(reached) if reached else None
    prev = st.get("alerted_step")
    if step is None:
        # 15% 위로 회복 = 건전지를 갈았다는 뜻 → 계단 초기화, 다음에 또 닳으면 15%부터 다시 알림
        st["alerted_step"] = None
    elif prev is None or step < prev:
        if hub_push(f"🔦 조명 배터리 {level}%", "곧 건전지 갈아야 해요"):
            st["alerted_step"] = step   # 발송에 성공했을 때만 기록 → 실패하면 다음 검사에 재시도
    elif level > prev:
        st["alerted_step"] = step       # 값이 올라감 = 그 단계는 벗어남 (다시 내려오면 또 알림)
    save_battery_state(st)
    return st

def _record_battery_failure(err):
    st = load_battery_state()
    st["fail_streak"] = int(st.get("fail_streak") or 0) + 1
    if st["fail_streak"] >= BATTERY_FAIL_DAYS and not st.get("fail_alerted"):
        if hub_push("🔦 조명이 안 잡혀요",
                    f"{st['fail_streak']}일째 배터리를 못 읽었어요. 건전지가 다 됐을 수 있어요"):
            st["fail_alerted"] = True
    save_battery_state(st)
    print(f"[배터리] 검사 실패 {st['fail_streak']}일째: {err}", flush=True)

def battery_check(tries=BATTERY_MAX_TRIES, retry_sec=BATTERY_RETRY_SEC):
    """하루치 검사 한 번. 실패하면 간격을 두고 재시도하고, 끝까지 안 되면 실패로 센다."""
    last_err = None
    for i in range(tries):
        try:
            level = get_battery_level()
            st = record_battery(level)
            print(f"[배터리] {level}% · 경고단계 {st.get('alerted_step')}", flush=True)
            return level
        except Exception as e:
            last_err = e
            print(f"[배터리] 읽기 실패 {i + 1}/{tries}: {e}", flush=True)
            if i < tries - 1:
                time.sleep(retry_sec)
    _record_battery_failure(last_err)
    return None

def battery_watch_loop():
    """매일 BATTERY_CHECK_HOUR 시에 한 번. '>=' 로 비교해서 맥이 그 시각에 자고 있었어도
    깨어난 뒤 그날 안에 한 번은 검사한다(같은 날 중복은 last_date로 막는다)."""
    last_date = None
    while True:
        try:
            now = datetime.now()
            if now.hour >= BATTERY_CHECK_HOUR and last_date != now.date():
                last_date = now.date()
                battery_check()
        except Exception as e:
            print(f"[배터리 감시 오류, 계속] {e}", flush=True)
        time.sleep(60)

# ── BLE 스캔 ─────────────────────────────────────
async def _scan_ble_devices(duration=10):
    devices = await BleakScanner.discover(timeout=duration)
    result = []
    for d in devices:
        result.append({
            "address": d.address,
            "name": d.name or "(이름 없음)",
            "rssi": d.rssi if hasattr(d, "rssi") else None,
        })
    result.sort(key=lambda x: x["rssi"] or -999, reverse=True)
    return result

# ── BLE 연결 리셋 (멈춘 링크 정리) ───────────────
async def _reconnect_cycle(addr):
    """멈춘 GATT 링크를 강제로 연결→해제해 CoreBluetooth 상태를 정리."""
    client = BleakClient(addr)
    try:
        await asyncio.wait_for(client.connect(), timeout=8)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

def ble_reset():
    """
    BLE가 멈췄을 때 회복 시도.
    1) blueutil이 있으면 블루투스를 껐다 켠다 (가장 확실).
    2) 저장된 기기로 강제 연결→해제 사이클을 돌려 멈춘 링크를 정리.
    수행한 단계 목록을 돌려준다.
    """
    steps = []
    bu = shutil.which("blueutil")
    if bu:
        try:
            subprocess.run([bu, "-p", "0"], check=True, timeout=10)
            time.sleep(2)
            subprocess.run([bu, "-p", "1"], check=True, timeout=10)
            time.sleep(2)
            steps.append("bluetooth_off_on")
        except Exception as e:
            steps.append(f"blueutil_failed: {e}")
    addr = config.get("device_address")
    if addr:
        try:
            _run_ble(_reconnect_cycle(addr))
            steps.append("reconnect_cycle")
        except Exception as e:
            steps.append(f"cycle_skipped: {e}")
    if not steps:
        steps.append("noop")
    return steps

# ── 스케줄러 ────────────────────────────────────
def scheduler_loop():
    fired_daily = set()
    while True:
        now = datetime.now()
        weekday = now.weekday()
        # 날짜까지 키에 포함해야 "같은 1분 내 중복 방지"가 된다.
        # (시·분만 쓰면 다음 날 같은 시각이 영원히 스킵됨)
        minute_key_base = (now.date(), now.hour, now.minute)
        fired_daily = {k for k in fired_daily if k[1] == minute_key_base}
        to_remove = []
        for s in schedules[:]:
            if not s.get("enabled", True):
                continue
            if s["type"] == "daily":
                if weekday not in s.get("days", list(range(7))):
                    continue
                fire_key = (s["id"], minute_key_base)
                if now.hour == s["hour"] and now.minute == s["minute"] and fire_key not in fired_daily:
                    fired_daily.add(fire_key)
                    print(f"[예약] {s['action']} ({s['hour']:02d}:{s['minute']:02d})")
                    threading.Thread(target=run_command, args=(s["action"],), daemon=True).start()
            elif s["type"] == "timer":
                if time.time() >= s["trigger_at"]:
                    print(f"[타이머] {s['action']}")
                    threading.Thread(target=run_command, args=(s["action"],), daemon=True).start()
                    to_remove.append(s["id"])
        if to_remove:
            schedules[:] = [s for s in schedules if s["id"] not in to_remove]
            save_schedules()
        time.sleep(1)

# ── mDNS 등록 (선택적) ───────────────────────────
def _get_lan_ip():
    """실제 LAN 인터페이스 IP. gethostbyname(gethostname())은 macOS에서 127.0.0.1을
    주는 경우가 많아, UDP 소켓 트릭으로 나가는 인터페이스 주소를 얻는다(패킷은 안 나감)."""
    import socket
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        ip = _s.getsockname()[0]
        _s.close()
        return ip
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def _mdns_info(local_ip, port):
    from zeroconf import ServiceInfo
    import socket
    return ServiceInfo(
        "_http._tcp.local.",
        "switcher._http._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties={"path": "/"},
        server="switcher.local.",
    )


def _mdns_watch(zc, port, current_ip, interval=20):
    """Wi-Fi 재접속·DHCP 갱신 등으로 맥 IP가 바뀌면 switcher.local을 새 IP로 재등록.
    (예전엔 IP 바뀔 때마다 폰에서 접속이 끊겨 수동 재시작해야 했음)

    update_service()만 쓰면 안 됨: 레코드 속 IP는 바뀌지만 zeroconf가 잡아둔
    송신 소켓은 옛 IP에 묶인 채라, 폰이 "switcher.local 누구?"라고 물어도 응답이
    LAN으로 못 나간다(맥 자기 자신만 캐시로 열림). 그래서 Zeroconf를 통째로
    닫고 새로 만들어 소켓까지 새 IP로 다시 묶는다."""
    from zeroconf import Zeroconf
    while True:
        time.sleep(interval)
        try:
            ip = _get_lan_ip()
            if ip and not ip.startswith("127.") and ip != current_ip:
                try:
                    zc.close()
                except Exception:
                    pass
                zc = Zeroconf()
                zc.register_service(_mdns_info(ip, port))
                print(f"🔄 IP 변경 감지 {current_ip} → {ip} · switcher.local 재등록", flush=True)
                current_ip = ip
        except Exception as e:
            print(f"[mDNS 감시 오류, 계속] {e}", flush=True)


def register_mdns(port=PORT):
    try:
        from zeroconf import Zeroconf
        local_ip = _get_lan_ip()
        if local_ip.startswith("127."):
            raise RuntimeError(f"LAN IP를 못 찾음(감지값 {local_ip}) — mDNS 등록 건너뜀")
        zc = Zeroconf()
        zc.register_service(_mdns_info(local_ip, port))
        print(f"🌐 mDNS 등록 완료: http://switcher.local:{port} ({local_ip})", flush=True)
        # IP 변경 자동 감시 → switcher.local 스스로 따라감 (수동 재시작 불필요)
        threading.Thread(target=_mdns_watch, args=(zc, port, local_ip), daemon=True).start()
        return zc
    except Exception as e:
        print(f"[mDNS 등록 실패, 무시됨] {e}")
        return None

# ── HTML ────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#131114">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="스위처">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.min.css">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='24' fill='%23131114'/%3E%3Ccircle cx='50' cy='50' r='26' fill='none' stroke='%23f5a524' stroke-width='7'/%3E%3Cpath d='M50 24 A26 26 0 0 1 50 76 Z' fill='%23f5a524'/%3E%3C/svg%3E">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>스위처</title>
<style>
  /* ── yde 토큰: 야간 도구 ── */
  :root{
    --radius:16px; --bw:1px;
    --bg:#131114; --card:#1b181d; --card2:#211d24;
    --fg:#f3efe9; --dim:#9d97a3; --faint:#6f6a76; --border:#2a262e;
    --accent:#f5a524; --accent-fg:#221703; --accent-soft:rgba(245,165,36,.13);
    --good:#4ade80; --rose:#f87171;
    --ease:cubic-bezier(.2,.8,.2,1);
    --dur-micro:150ms; --dur-move:240ms; --dur-slow:360ms;
  }
  *{ box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
  html,body{ background:var(--bg); }
  body{ font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',system-ui,sans-serif;
    color:var(--fg); min-height:100dvh; max-width:460px; margin:0 auto; letter-spacing:-.01em;
    padding:calc(env(safe-area-inset-top) + 24px) 20px calc(env(safe-area-inset-bottom) + 24px);
    position:relative; overflow-x:hidden; }
  .ic{ width:1em; height:1em; display:inline-block; vertical-align:-.14em; fill:currentColor; flex:0 0 auto; }

  /* ── 방을 따라가는 글로우: 켜면 화면 위가 밝아진다 ── */
  .glow{ position:fixed; left:50%; transform:translateX(-50%); top:-26%; width:130%; max-width:640px; height:62%;
    pointer-events:none; z-index:0;
    background:radial-gradient(ellipse 70% 60% at 50% 22%, rgba(245,165,36,.32), rgba(245,165,36,.09) 46%, transparent 72%);
    opacity:0; transition:opacity var(--dur-slow) var(--ease); }
  body.on .glow{ opacity:1; }
  body>*:not(.glow){ position:relative; z-index:1; }

  /* ── 헤더 ── */
  header{ display:flex; align-items:center; justify-content:space-between; }
  .eyebrow{ font-size:11px; letter-spacing:.34em; color:var(--faint); font-weight:700; }
  h1.dev{ font-size:20px; font-weight:700; letter-spacing:-.02em; margin-top:4px; }
  .conn{ display:inline-flex; align-items:center; gap:7px; font-size:13px; font-weight:600; color:var(--dim);
    background:var(--card); border:var(--bw) solid var(--border); border-radius:999px; padding:9px 14px; }
  .conn .dot{ width:7px; height:7px; border-radius:50%; background:var(--faint); }
  .conn.live .dot{ background:var(--good); }

  /* ── 배너 ── */
  .banner{ display:none; background:var(--accent-soft); border:var(--bw) solid rgba(245,165,36,.35); border-radius:var(--radius);
    padding:15px 18px; margin-top:16px; font-size:13px; color:#e0be7f; line-height:1.55; cursor:pointer; }
  .banner strong{ display:block; font-size:14px; margin-bottom:3px; color:#f0d29a; }

  /* ── 상태 히어로 ── */
  .hero{ padding:40px 0 34px; }
  .hero .cap{ font-size:13px; color:var(--dim); }
  .hero .word{ font-size:60px; font-weight:800; line-height:1.12; letter-spacing:-.03em;
    transition:color var(--dur-slow) var(--ease); }
  body.on .hero .word{ color:var(--accent); text-shadow:0 0 44px rgba(245,165,36,.35); }
  .hero .sub{ margin-top:8px; font-size:13px; color:var(--faint); }
  .batbtn{ margin-top:18px; display:inline-flex; align-items:center; gap:8px; font-size:13px; font-weight:600; color:var(--dim);
    background:var(--card); border:var(--bw) solid var(--border); border-radius:999px; padding:10px 16px; cursor:pointer;
    font-family:inherit; transition:transform var(--dur-micro) var(--ease); }
  .batbtn:active{ transform:scale(.97); }
  .batbtn .ic{ color:var(--accent); font-size:15px; }
  .batbtn b{ color:var(--fg); font-weight:600; font-variant-numeric:tabular-nums; }
  .batbtn .when{ color:var(--faint); font-weight:500; }
  .batbtn.low b{ color:#ff7b72; }
  .batbtn.low .ic{ color:#ff7b72; }

  /* ── 카드 공통 ── */
  .sec{ margin-top:14px; }
  .card{ background:var(--card); border:var(--bw) solid var(--border); border-radius:var(--radius); padding:22px 20px;
    transition:border-color var(--dur-move) var(--ease); }
  .card h2{ font-size:13px; font-weight:700; letter-spacing:.14em; color:var(--faint); margin-bottom:16px; }
  .card.editing{ border-color:var(--accent); }
  .card.editing h2{ color:var(--accent); }

  /* ── 예약 목록 ── */
  .sch{ display:flex; align-items:center; justify-content:space-between; padding:15px 2px;
    border-top:var(--bw) solid var(--border); cursor:pointer; }
  .sch:first-child{ border-top:none; padding-top:2px; }
  .sch:last-child{ padding-bottom:2px; }
  .sch:active .t{ color:var(--accent); }
  .sch .t{ font-size:17px; font-weight:700; font-variant-numeric:tabular-nums; transition:color var(--dur-micro); }
  .sch.editing-row .t{ color:var(--accent); }
  .sch .onoff{ color:var(--accent); font-weight:600; font-size:14px; margin-left:6px; }
  .sch .onoff.is-off{ color:var(--dim); }
  .sch .d{ font-size:13px; color:var(--faint); margin-top:3px; }
  .sch .right{ display:flex; align-items:center; gap:10px; }
  .sch .chev{ color:var(--faint); font-size:16px; }
  .pill{ font-size:12px; font-weight:700; border-radius:999px; padding:8px 12px; border:var(--bw) solid var(--border);
    background:var(--card2); color:var(--dim); cursor:pointer; font-family:inherit; }
  .pill.live{ background:var(--accent-soft); border-color:transparent; color:var(--accent); }
  .empty{ padding:10px 2px; color:var(--faint); font-size:15px; }
  .list-hint{ font-size:12px; color:var(--faint); margin-top:14px; padding-top:12px; border-top:var(--bw) solid var(--border); }

  /* ── 폼 요소 ── */
  .days{ display:flex; gap:6px; }
  .day{ flex:1; min-width:0; height:46px; border-radius:11px; border:var(--bw) solid var(--border); background:var(--card2);
    color:var(--dim); font-size:14px; font-weight:600; cursor:pointer; font-family:inherit;
    transition:all var(--dur-micro) var(--ease); }
  .day.sel{ background:var(--accent-soft); border-color:var(--accent); color:var(--accent); }
  .presets{ display:flex; gap:8px; margin-top:10px; }
  .preset{ flex:1; height:38px; border-radius:10px; border:var(--bw) solid var(--border); background:none;
    font-size:12.5px; font-weight:600; color:var(--faint); cursor:pointer; font-family:inherit; }
  .preset:active{ color:var(--accent); }
  .field{ display:flex; gap:8px; align-items:center; margin-top:14px; }
  input[type=time], input[type=number]{ flex:1.4; height:52px; border-radius:12px; border:var(--bw) solid var(--border);
    background:var(--card2); color:var(--fg); font:inherit; font-size:16px; padding:0 14px; color-scheme:dark;
    appearance:none; min-width:0; }
  .seg{ flex:1; display:flex; border:var(--bw) solid var(--border); border-radius:12px; overflow:hidden; height:52px; }
  .seg button{ flex:1; border:none; background:var(--card2); color:var(--dim); font-size:14px; font-weight:600;
    cursor:pointer; font-family:inherit; transition:all var(--dur-micro) var(--ease); }
  .seg button.sel{ background:var(--fg); color:var(--bg); }
  .row-btn{ margin-top:14px; width:100%; height:56px; border-radius:12px; border:none; background:var(--fg); color:var(--bg);
    font-size:16px; font-weight:700; cursor:pointer; font-family:inherit;
    transition:transform var(--dur-micro) var(--ease), opacity var(--dur-micro); }
  .row-btn:active{ transform:scale(.98); }
  .row-btn:disabled{ opacity:.35; cursor:default; }
  .ghost-btn{ margin-top:10px; width:100%; height:44px; border:none; background:none; color:var(--faint);
    font-size:14px; font-weight:600; cursor:pointer; font-family:inherit; display:none; }
  .ghost-btn.danger{ color:var(--rose); }
  .card.editing .ghost-btn{ display:block; }

  /* ── 타이머 ── */
  .chips{ display:flex; gap:8px; }
  .chip{ flex:1; height:52px; border-radius:12px; border:var(--bw) solid var(--border); background:var(--card2);
    color:var(--dim); font-size:15px; font-weight:600; cursor:pointer; font-family:inherit;
    transition:all var(--dur-micro) var(--ease); }
  .chip:active{ transform:scale(.97); }
  .chip.sel{ background:var(--accent-soft); border-color:var(--accent); color:var(--accent); }
  .preview{ margin-top:14px; font-size:15px; color:var(--dim); min-height:22px; }
  .preview b{ color:var(--fg); font-weight:600; }
  .unit{ color:var(--faint); font-size:14px; white-space:nowrap; font-weight:600; }

  /* ── 기기 관리 ── */
  details.card > summary{ list-style:none; cursor:pointer; display:flex; justify-content:space-between; align-items:center;
    font-size:15px; font-weight:600; color:var(--dim); }
  details.card > summary::-webkit-details-marker{ display:none; }
  details.card > summary .cur{ font-size:12.5px; color:var(--faint); font-weight:600; margin-left:10px; }
  details.card > summary .chev{ color:var(--faint); transition:transform var(--dur-move) var(--ease); }
  details.card[open] > summary .chev{ transform:rotate(90deg); }
  details.card[open] > summary{ margin-bottom:16px; }
  .hint{ font-size:13px; color:var(--faint); line-height:1.6; }
  .wide{ width:100%; height:52px; border-radius:12px; border:var(--bw) solid var(--border); background:var(--card2);
    color:var(--fg); font-size:15px; font-weight:600; cursor:pointer; font-family:inherit;
    display:flex; align-items:center; justify-content:center; gap:8px; margin-top:12px;
    transition:transform var(--dur-micro) var(--ease); }
  .wide:active{ transform:scale(.98); }
  .wide:disabled{ opacity:.4; cursor:default; }
  .wide .ic{ color:var(--accent); }
  .wide.danger{ color:var(--rose); }
  .wide.danger .ic{ color:var(--rose); }
  .scan-note{ font-size:13px; color:var(--faint); text-align:center; min-height:16px; margin:12px 0 2px; }
  .dev-list{ display:flex; flex-direction:column; gap:8px; margin-top:4px; }
  .dev{ display:flex; align-items:center; justify-content:space-between; padding:13px 14px; border-radius:12px;
    border:var(--bw) solid var(--border); background:var(--card2); cursor:pointer;
    transition:all var(--dur-micro) var(--ease); }
  .dev.sel{ border-color:var(--accent); background:var(--accent-soft); }
  .dev .nm{ font-size:14px; font-weight:600; }
  .dev.sel .nm{ color:var(--accent); }
  .dev .ad{ font-size:10.5px; color:var(--faint); margin-top:2px; font-family:ui-monospace,monospace; }
  .dev .rs{ font-size:11.5px; color:var(--faint); }
  .save{ width:100%; height:52px; border-radius:12px; border:none; margin-top:12px; background:var(--accent);
    color:var(--accent-fg); font-size:15px; font-weight:700; cursor:pointer; font-family:inherit; }
  .save:disabled{ opacity:.3; cursor:default; }
  .divider{ height:1px; background:var(--border); margin:18px 0; }
  .subh{ font-size:11px; letter-spacing:.14em; color:var(--faint); font-weight:700; margin-bottom:10px; }
  .row2{ display:flex; gap:8px; }
  .type{ flex:1; height:46px; border-radius:11px; font-size:14px; font-weight:600; cursor:pointer; font-family:inherit;
    background:var(--card2); border:var(--bw) solid var(--border); color:var(--dim);
    transition:all var(--dur-micro) var(--ease); }
  .type.on{ background:var(--fg); border-color:var(--fg); color:var(--bg); }

  /* ── 켜기/끄기 (상단, 상태 히어로 바로 아래) ── */
  .dock{ margin:2px 0 22px; }
  .dock-in{ display:flex; gap:10px; }
  .big{ flex:1; height:72px; border-radius:var(--radius); border:var(--bw) solid var(--border);
    font-size:18px; font-weight:800; cursor:pointer; font-family:inherit;
    display:flex; align-items:center; justify-content:center; gap:9px;
    transition:all var(--dur-move) var(--ease); }
  .big:active{ transform:scale(.97); }
  .big .ic{ font-size:20px; }
  #btn-on{ background:var(--accent); border-color:var(--accent); color:var(--accent-fg);
    box-shadow:0 6px 30px -6px rgba(245,165,36,.45); }
  body.on #btn-on{ box-shadow:0 6px 40px -4px rgba(245,165,36,.65); }
  #btn-off{ background:var(--card); color:var(--dim); }
  body.off #btn-off{ color:var(--fg); }
  .big:disabled{ cursor:default; }
  .big.busy{ opacity:.9; }

  /* 전송 스피너 */
  .spin{ width:1em; height:1em; border:2.5px solid currentColor; border-right-color:transparent; border-radius:50%;
    display:inline-block; animation:sp .7s linear infinite; }
  @keyframes sp{ to{ transform:rotate(360deg); } }

  #toast{ position:fixed; left:50%; bottom:calc(env(safe-area-inset-bottom) + 28px); transform:translateX(-50%) translateY(18px);
    background:#2a262e; color:var(--fg); font-size:13.5px; font-weight:600; padding:12px 20px; border-radius:12px;
    box-shadow:0 16px 40px -14px rgba(0,0,0,.6); opacity:0; pointer-events:none; transition:all var(--dur-move) var(--ease);
    z-index:50; max-width:88vw; text-align:center; }
  #toast.show{ opacity:1; transform:translateX(-50%) translateY(0); }

  :is(button,summary,input):focus-visible{ outline:2px solid var(--accent); outline-offset:2px; }
  @media (prefers-reduced-motion: reduce){ *{ transition-duration:.01ms !important; animation-duration:.01ms !important; } }
  @media (max-width:350px){ .hero .word{ font-size:46px; } }
</style>
</head>
<body>
<div class="glow"></div>

<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-bulb" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M11.5 2C7.358 2 4 5.436 4 9.674c0 2.273.966 4.315 2.499 5.72c.51.467.889.814 1.157 1.066a15 15 0 0 1 .4.39l.033.036c.237.3.288.376.318.446s.053.16.112.54c.024.15.026.406.026 1.105v.03c0 .409 0 .762.026 1.051c.027.306.087.61.248.895c.18.319.438.583.75.767c.278.165.575.226.874.254c.283.026.628.026 1.028.026h.058c.4 0 .745 0 1.028-.026c.3-.028.595-.09.875-.254a2.07 2.07 0 0 0 .749-.767c.16-.285.22-.588.248-.895c.026-.29.026-.642.025-1.051v-.03c0-.699.003-.955.026-1.105c.06-.38.082-.47.113-.54c.03-.07.081-.147.318-.446l.005-.006l.025-.027l.088-.09q.112-.113.312-.3c.268-.252.647-.599 1.157-1.067A7.74 7.74 0 0 0 19 9.674C19 5.436 15.642 2 11.5 2m1.585 17.674q-.004.145-.015.258c-.018.21-.049.286-.07.324a.7.7 0 0 1-.25.255c-.037.023-.111.054-.316.073c-.214.02-.497.02-.934.02s-.72 0-.934-.02c-.205-.019-.279-.05-.316-.073a.7.7 0 0 1-.25-.255c-.021-.038-.052-.114-.07-.324a5 5 0 0 1-.015-.258zm-3.811-6.324a.75.75 0 0 1 1.025.274a1.25 1.25 0 0 0 2.166 0a.75.75 0 1 1 1.298.752a2.76 2.76 0 0 1-1.631 1.27V17a.75.75 0 0 1-1.5 0v-1.354A2.76 2.76 0 0 1 9 14.376a.75.75 0 0 1 .274-1.025" clip-rule="evenodd"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path fill="currentColor" d="M12 22c5.523 0 10-4.477 10-10c0-.463-.694-.54-.933-.143a6.5 6.5 0 1 1-8.924-8.924C12.54 2.693 12.463 2 12 2C6.477 2 2 6.477 2 12s4.477 10 10 10"/></symbol>
  <symbol id="i-battery" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M3.172 5.172C2 6.343 2 8.229 2 12s0 5.657 1.172 6.828S6.229 20 10 20h1.5c3.771 0 5.657 0 6.828-1.172c.944-.943 1.127-2.348 1.163-4.828H20c.943 0 1.414 0 1.707-.293S22 12.943 22 12s0-1.414-.293-1.707S20.943 10 20 10h-.509c-.036-2.48-.22-3.885-1.163-4.828C17.157 4 15.271 4 11.5 4H10C6.229 4 4.343 4 3.172 5.172M7 9c.65-.361.655-.365.656-.364l.002.004l.004.007l.01.018l.026.053q.03.064.075.175c.06.147.132.356.202.631c.142.551.274 1.364.274 2.474s-.132 1.923-.274 2.474c-.07.275-.143.484-.202.631a3 3 0 0 1-.102.228l-.01.018l-.004.007l-.001.002L7 15l-.656.363a.75.75 0 0 1-1.317-.72l.005-.01a4 4 0 0 0 .18-.534c.108-.424.226-1.111.226-2.101s-.118-1.677-.226-2.101a4 4 0 0 0-.18-.534l-.005-.01a.75.75 0 0 1 1.317-.719zm3.51-.5l.646.364l.001-.001a.75.75 0 0 0-1.317-.72l-.005.011l-.038.087a5 5 0 0 0-.142.447c-.108.424-.226 1.111-.226 2.101s.118 1.677.226 2.101c.055.212.107.36.142.447l.038.087l.005.01a.75.75 0 0 0 1.317-.719L10.5 15.5l.654.363l.002-.003l.003-.007l.01-.018a3 3 0 0 0 .102-.228c.06-.147.132-.356.202-.631c.142-.551.274-1.364.274-2.474s-.132-1.923-.273-2.474a5 5 0 0 0-.203-.631a3 3 0 0 0-.101-.228l-.01-.018l-.005-.007z" clip-rule="evenodd"/></symbol>
  <symbol id="i-restart" viewBox="0 0 24 24"><path fill="currentColor" d="M18.258 3.508a.75.75 0 0 1 .463.693v4.243a.75.75 0 0 1-.75.75h-4.243a.75.75 0 0 1-.53-1.28L14.8 6.31a7.25 7.25 0 1 0 4.393 5.783a.75.75 0 0 1 1.488-.187A8.75 8.75 0 1 1 15.93 5.18l1.51-1.51a.75.75 0 0 1 .817-.162"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path fill="currentColor" d="M3 6.524c0-.395.327-.714.73-.714h4.788c.006-.842.098-1.995.932-2.793A3.68 3.68 0 0 1 12 2a3.68 3.68 0 0 1 2.55 1.017c.834.798.926 1.951.932 2.793h4.788c.403 0 .73.32.73.714a.72.72 0 0 1-.73.714H3.73A.72.72 0 0 1 3 6.524"/><path fill="currentColor" fill-rule="evenodd" d="M11.596 22h.808c2.783 0 4.174 0 5.08-.886c.904-.886.996-2.34 1.181-5.246l.267-4.187c.1-1.577.15-2.366-.303-2.866c-.454-.5-1.22-.5-2.753-.5H8.124c-1.533 0-2.3 0-2.753.5s-.404 1.289-.303 2.866l.267 4.188c.185 2.906.277 4.36 1.182 5.245c.905.886 2.296.886 5.079.886m-1.35-9.811c-.04-.434-.408-.75-.82-.707c-.413.043-.713.43-.672.864l.5 5.263c.04.434.408.75.82.707c.413-.044.713-.43.672-.864zm4.329-.707c.412.043.713.43.671.864l-.5 5.263c-.04.434-.409.75-.82.707c-.413-.044-.713-.43-.672-.864l.5-5.264c.04-.433.409-.75.82-.707" clip-rule="evenodd"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M21.788 21.788a.723.723 0 0 0 0-1.022L18.122 17.1a9.157 9.157 0 1 0-1.022 1.022l3.666 3.666a.723.723 0 0 0 1.022 0" clip-rule="evenodd"/></symbol>
  <symbol id="i-arrow" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.6" d="m9 5l6 7l-6 7"/></symbol>
</svg>

<header>
  <div>
    <div class="eyebrow">SWITCHER</div>
    <h1 class="dev" id="dev-name">조명</h1>
  </div>
  <span class="conn" id="conn-badge"><span class="dot"></span><span id="conn-txt">연결 안 됨</span></span>
</header>

<div id="banner" class="banner" onclick="openDevBox()">
  <strong>기기가 아직 연결되지 않았어요</strong>
  여기를 눌러 스위처를 찾아 연결하세요.
</div>

<!-- 상태 히어로 -->
<div class="hero">
  <div class="cap">지금 조명은</div>
  <div class="word" id="st-lbl">대기</div>
  <div class="sub">방금 보낸 명령 기준이에요</div>
  <button class="batbtn" id="bat-btn" onclick="checkBattery()"><svg class="ic"><use href="#i-battery"/></svg>배터리 <b id="bat-val">탭해서 확인</b><span class="when" id="bat-when"></span></button>
</div>

<!-- 켜기/끄기 (맨 위, 엄지 안 뻗어도 되게) -->
<div class="dock">
  <div class="dock-in">
    <button class="big" id="btn-on" onclick="cmd('on')"><svg class="ic"><use href="#i-bulb"/></svg>켜기</button>
    <button class="big" id="btn-off" onclick="cmd('off')"><svg class="ic"><use href="#i-moon"/></svg>끄기</button>
  </div>
</div>

<!-- 예약 목록 -->
<div class="sec card">
  <h2>예약</h2>
  <div id="sch-list"><div class="empty">아직 예약이 없어요</div></div>
  <div class="list-hint">예약을 탭하면 바로 고칠 수 있어요</div>
</div>

<!-- 새 예약 -->
<div class="sec card" id="daily-card">
  <h2 id="daily-title">새 예약</h2>
  <div class="days" id="day-row"></div>
  <div class="presets">
    <button class="preset" onclick="setPreset([0,1,2,3,4])">주중</button>
    <button class="preset" onclick="setPreset([5,6])">주말</button>
    <button class="preset" onclick="setPreset([0,1,2,3,4,5,6])">매일</button>
  </div>
  <div class="field">
    <input type="time" id="daily-time" value="23:00" aria-label="예약 시간">
    <div class="seg" id="daily-seg">
      <button data-a="on" onclick="segPick(this)">켜기</button>
      <button data-a="off" class="sel" onclick="segPick(this)">끄기</button>
    </div>
  </div>
  <button class="row-btn" id="daily-save-btn" onclick="addDaily()">예약 추가</button>
  <button class="ghost-btn" id="daily-cancel-btn" onclick="cancelEditDaily()">수정 취소</button>
  <button class="ghost-btn danger" onclick="deleteEditing('daily')">이 예약 삭제</button>
</div>

<!-- 타이머 -->
<div class="sec card" id="timer-card">
  <h2 id="timer-title">타이머</h2>
  <div class="chips">
    <button class="chip" onclick="pickTimer(this,15)">15분</button>
    <button class="chip" onclick="pickTimer(this,30)">30분</button>
    <button class="chip" onclick="pickTimer(this,60)">1시간</button>
    <button class="chip" onclick="pickTimer(this,90)">90분</button>
  </div>
  <div class="field">
    <input type="number" id="timer-min" min="1" max="999" placeholder="직접 입력" aria-label="타이머 분" oninput="timerChanged()">
    <span class="unit">분 후</span>
    <div class="seg" id="timer-seg">
      <button data-a="on" onclick="segPick(this); timerChanged()">켜기</button>
      <button data-a="off" class="sel" onclick="segPick(this); timerChanged()">끄기</button>
    </div>
  </div>
  <div class="preview" id="t-preview">시간을 고르면 언제 실행되는지 보여드려요</div>
  <button class="row-btn" id="timer-save-btn" onclick="addTimer()">타이머 시작</button>
  <button class="ghost-btn" id="timer-cancel-btn" onclick="cancelEditTimer()">수정 취소</button>
  <button class="ghost-btn danger" onclick="deleteEditing('timer')">이 타이머 삭제</button>
</div>

<!-- 기기 관리 -->
<div class="sec">
  <details class="card" id="dev-box">
    <summary>
      <span>기기 관리 <span class="cur" id="dev-cur"></span></span>
      <svg class="ic chev"><use href="#i-arrow"/></svg>
    </summary>
    <div class="hint">스위처 전원을 켜고 블루투스 범위(약 10m) 안에 두세요. 스캔 후 목록에서 골라 저장합니다.</div>
    <button class="wide" id="scan-btn" onclick="startScan()"><svg class="ic"><use href="#i-search"/></svg>주변 기기 스캔</button>
    <div class="scan-note" id="scan-note"></div>
    <div class="dev-list" id="dev-list"></div>
    <button class="save" id="save-btn" onclick="saveDevice()" disabled>선택한 기기 저장</button>

    <div class="divider"></div>
    <div class="subh">기기 종류</div>
    <div class="row2">
      <button class="type on" id="type1" onclick="setDeviceType(1)">1구</button>
      <button class="type" id="type2" onclick="setDeviceType(2)">2구</button>
    </div>

    <div class="divider"></div>
    <div class="subh">연결 관리</div>
    <div class="hint" style="margin-bottom:10px">버튼이 계속 실패하면 연결부터 다시 잡아보세요.</div>
    <div class="row2">
      <button class="wide" style="margin-top:0" onclick="bleReset()"><svg class="ic"><use href="#i-restart"/></svg>연결 다시 잡기</button>
      <button class="wide danger" style="margin-top:0" onclick="forgetDevice()"><svg class="ic"><use href="#i-trash"/></svg>기기 잊기</button>
    </div>
  </details>
</div>

<div id="toast"></div>

<script>
let scanned=[], selectedIdx=-1, deviceType=1, busy=false;
let latestSchedules=[], editingDailyId=null, editingTimerId=null;
const DAY=['월','화','수','목','금','토','일'];

function esc(s){ return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg;
  t.classList.add('show');
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'), 2400);
}

/* ── 상태 ── */
function applyState(s){
  document.body.classList.remove('on','off');
  const lbl=document.getElementById('st-lbl');
  if(s==='on'){ document.body.classList.add('on'); lbl.textContent='켜짐'; }
  else if(s==='off'){ document.body.classList.add('off'); lbl.textContent='꺼짐'; }
  else lbl.textContent='대기';
}
applyState((location.hash==='#on'||location.hash==='#off')?location.hash.slice(1):(localStorage.getItem('lastState')||''));

async function cmd(action){
  if(busy) return; busy=true;
  const on=document.getElementById('btn-on'), off=document.getElementById('btn-off');
  const tapped = action==='on'?on:off;
  const orig = tapped.innerHTML;
  on.disabled=true; off.disabled=true; tapped.classList.add('busy');
  tapped.innerHTML='<span class="spin"></span>보내는 중';
  try{
    const d=await (await fetch('/switch/'+action,{method:'POST'})).json();
    if(d.ok){ applyState(action); localStorage.setItem('lastState',action); toast(action==='on'?'켰어요':'껐어요'); }
    else toast('실패: '+(d.error||''));
  }catch(e){ toast('연결에 실패했어요'); }
  finally{ tapped.innerHTML=orig; tapped.classList.remove('busy'); on.disabled=false; off.disabled=false; busy=false; }
}

/* 서버가 매일 저녁 9시에 알아서 재둔다 — 화면은 그 마지막 값을 바로 보여주고,
   탭하면 그 자리에서 다시 잰다. */
function batWhen(iso){
  if(!iso) return '';
  const d=new Date(iso), n=new Date();
  const days=Math.round((new Date(n.getFullYear(),n.getMonth(),n.getDate())
                       - new Date(d.getFullYear(),d.getMonth(),d.getDate()))/86400000);
  if(days===0) return '오늘 '+d.getHours()+'시';
  if(days===1) return '어제 '+d.getHours()+'시';
  return (d.getMonth()+1)+'/'+d.getDate();
}
function renderBattery(st){
  const el=document.getElementById('bat-val'), wh=document.getElementById('bat-when');
  const btn=document.getElementById('bat-btn');
  if(st.level==null){ el.textContent='탭해서 확인'; wh.textContent=''; btn.classList.remove('low'); return; }
  el.textContent=st.level+'%';
  wh.textContent=batWhen(st.at);
  btn.classList.toggle('low', st.level<=15);
}
async function loadBatteryState(){
  try{ renderBattery(await (await fetch('/battery/state')).json()); }catch(e){}
}
async function checkBattery(){
  const el=document.getElementById('bat-val'), wh=document.getElementById('bat-when');
  el.textContent='확인 중…'; wh.textContent='';
  try{
    const d=await (await fetch('/battery')).json();
    if(d.ok) loadBatteryState(); else el.textContent='실패';
  }catch(e){ el.textContent='에러'; }
}

/* ── 기기 ── */
async function loadDeviceInfo(){
  try{
    const d=await (await fetch('/config')).json();
    const banner=document.getElementById('banner'), name=document.getElementById('dev-name');
    const badge=document.getElementById('conn-badge'), ctxt=document.getElementById('conn-txt');
    const box=document.getElementById('dev-box'), cur=document.getElementById('dev-cur');
    if(d.device_address){
      name.textContent=(d.device_name||'스위처');
      banner.style.display='none';
      badge.classList.add('live'); ctxt.textContent='연결됨 · '+(d.device_type===2?'2구':'1구');
      deviceType=d.device_type||1; setDeviceType(deviceType);
      cur.textContent=(d.device_name||'스위처')+' · '+(d.device_type===2?'2구':'1구');
      if(!box.dataset.touched) box.open=false;
    } else {
      name.textContent='기기 없음'; banner.style.display='block';
      badge.classList.remove('live'); ctxt.textContent='연결 안 됨';
      cur.textContent='미설정';
      if(!box.dataset.touched) box.open=true;
    }
  }catch(e){}
}
function openDevBox(){
  const box=document.getElementById('dev-box');
  box.open=true; box.scrollIntoView({behavior:'smooth', block:'start'});
}
function setDeviceType(t){
  deviceType=t;
  document.getElementById('type1').classList.toggle('on',t===1);
  document.getElementById('type2').classList.toggle('on',t===2);
}
async function startScan(){
  const btn=document.getElementById('scan-btn'), note=document.getElementById('scan-note'), list=document.getElementById('dev-list');
  btn.disabled=true; btn.innerHTML='스캔 중… (10초)'; note.textContent='주변 블루투스 기기를 찾고 있어요';
  list.innerHTML=''; scanned=[]; selectedIdx=-1; document.getElementById('save-btn').disabled=true;
  try{
    const d=await (await fetch('/scan',{method:'POST'})).json();
    if(!d.ok){ note.textContent='스캔 실패: '+(d.error||''); return; }
    scanned=d.devices;
    if(!scanned.length){ note.textContent='기기를 찾지 못했어요. 스위처 전원을 확인하세요.'; return; }
    note.textContent=scanned.length+'개 발견';
    list.innerHTML=scanned.map((v,i)=>`
      <div class="dev" onclick="selectDevice(${i})" id="dev-${i}">
        <div><div class="nm">${esc(v.name)}</div><div class="ad">${esc(v.address)}</div></div>
        <div class="rs">${v.rssi!=null?v.rssi+' dBm':''}</div>
      </div>`).join('');
  }catch(e){ note.textContent='에러: '+e.message; }
  finally{ btn.disabled=false; btn.innerHTML='<svg class="ic"><use href="#i-search"/></svg>다시 스캔'; }
}
function selectDevice(i){
  selectedIdx=i;
  document.querySelectorAll('.dev').forEach(el=>el.classList.remove('sel'));
  document.getElementById('dev-'+i).classList.add('sel');
  document.getElementById('save-btn').disabled=false;
}
async function saveDevice(){
  if(selectedIdx<0) return;
  const dev=scanned[selectedIdx];
  const d=await (await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({address:dev.address,name:dev.name,device_type:deviceType})})).json();
  if(d.ok){ toast('"'+dev.name+'" 저장됨'); loadDeviceInfo(); }
  else toast('저장 실패');
}
async function bleReset(){
  toast('연결 정리 중…');
  try{
    const d=await (await fetch('/ble/reset',{method:'POST'})).json();
    toast(d.ok?'연결을 다시 잡았어요':'리셋 실패: '+(d.error||''));
  }catch(e){ toast('에러가 났어요'); }
}
async function forgetDevice(){
  if(!confirm('저장된 기기를 잊을까요? 다시 스캔해서 연결해야 합니다.')) return;
  const d=await (await fetch('/ble/forget',{method:'POST'})).json();
  if(d.ok){ toast('기기를 잊었어요'); loadDeviceInfo(); }
  else toast('실패했어요');
}

/* ── 공용 폼 ── */
function segPick(el){
  el.parentElement.querySelectorAll('button').forEach(b=>b.classList.remove('sel'));
  el.classList.add('sel');
}
function segValue(id){ return document.querySelector('#'+id+' .sel').dataset.a; }
function setSeg(id,a){
  document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('sel', b.dataset.a===a));
}
function fmtDays(ds){ return ds.length===7 ? '매일' : ds.slice().sort().map(i=>DAY[i]).join(' '); }
function fmtTime(h,m){ return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0'); }

/* ── 새 예약 (매일) ── */
function buildDays(){
  document.getElementById('day-row').innerHTML =
    DAY.map((d,i)=>`<button class="day" data-day="${i}" onclick="this.classList.toggle('sel')">${d}</button>`).join('');
}
function setPreset(days){
  document.querySelectorAll('#day-row .day').forEach(b=>b.classList.toggle('sel', days.includes(+b.dataset.day)));
}
function selectedDays(){
  return [...document.querySelectorAll('#day-row .day.sel')].map(b=>+b.dataset.day);
}
async function addDaily(){
  const days=selectedDays();
  if(!days.length){ toast('요일을 골라주세요'); return; }
  const t=document.getElementById('daily-time').value;
  if(!t){ toast('시간을 입력해주세요'); return; }
  const [h,m]=t.split(':').map(Number);
  const action=segValue('daily-seg');
  const body=JSON.stringify({hour:h,minute:m,action,days});
  const when=fmtDays(days)+' '+t;
  if(editingDailyId){
    const d=await (await fetch('/schedule/'+editingDailyId,{method:'PATCH',headers:{'Content-Type':'application/json'},body})).json();
    if(d.ok){ toast(when+(action==='on'?'에 켜는 걸로 고쳤어요':'에 끄는 걸로 고쳤어요')); cancelEditDaily(); }
    else toast('수정 실패: '+(d.error||''));
  } else {
    const d=await (await fetch('/schedule/daily',{method:'POST',headers:{'Content-Type':'application/json'},body})).json();
    if(d.ok){ toast(when+(action==='on'?'에 켤게요':'에 끌게요')); setPreset([]); }
    else toast('추가 실패: '+(d.error||''));
  }
  loadSchedules();
}
function cancelEditDaily(){
  editingDailyId=null;
  document.getElementById('daily-card').classList.remove('editing');
  document.getElementById('daily-title').textContent='새 예약';
  document.getElementById('daily-save-btn').textContent='예약 추가';
  setPreset([]);
  renderList();
}

/* ── 타이머 ── */
function pickTimer(el,m){
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('sel'));
  el.classList.add('sel');
  document.getElementById('timer-min').value=m;
  timerPreview();
}
function timerChanged(){
  const v=+document.getElementById('timer-min').value;
  document.querySelectorAll('.chip').forEach(c=>{
    c.classList.toggle('sel', c.textContent===(v===60?'1시간':v+'분'));
  });
  timerPreview();
}
function timerPreview(){
  const v=+document.getElementById('timer-min').value;
  const p=document.getElementById('t-preview');
  if(!v||v<1){ p.textContent='시간을 고르면 언제 실행되는지 보여드려요'; return; }
  const end=new Date(Date.now()+v*60000);
  const hh=end.getHours(), mm=String(end.getMinutes()).padStart(2,'0');
  p.innerHTML='<b>'+(hh<12?'오전':'오후')+' '+(hh%12||12)+':'+mm+'</b>'+(segValue('timer-seg')==='on'?'에 켜요':'에 꺼요');
}
async function addTimer(){
  const min=parseInt(document.getElementById('timer-min').value);
  if(!min||min<1){ toast('몇 분 뒤일지 골라주세요'); return; }
  const action=segValue('timer-seg');
  const body=JSON.stringify({minutes:min,action});
  if(editingTimerId){
    const d=await (await fetch('/schedule/'+editingTimerId,{method:'PATCH',headers:{'Content-Type':'application/json'},body})).json();
    if(d.ok){ toast(min+'분 뒤로 고쳤어요'); cancelEditTimer(); }
    else toast('수정 실패: '+(d.error||''));
  } else {
    const d=await (await fetch('/schedule/timer',{method:'POST',headers:{'Content-Type':'application/json'},body})).json();
    if(d.ok) toast(min+(action==='on'?'분 뒤에 켤게요':'분 뒤에 끌게요'));
    else toast('추가 실패: '+(d.error||''));
  }
  loadSchedules();
}
function cancelEditTimer(){
  editingTimerId=null;
  document.getElementById('timer-card').classList.remove('editing');
  document.getElementById('timer-title').textContent='타이머';
  document.getElementById('timer-save-btn').textContent='타이머 시작';
  document.getElementById('timer-min').value='';
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('sel'));
  timerPreview();
  renderList();
}

/* ── 목록 ── */
function editSchedule(id){
  const s=latestSchedules.find(x=>x.id===id);
  if(!s) return;
  if(s.type==='daily'){
    if(editingTimerId) cancelEditTimer();
    editingDailyId=id;
    setPreset(s.days);
    document.getElementById('daily-time').value=fmtTime(s.hour,s.minute);
    setSeg('daily-seg',s.action);
    const card=document.getElementById('daily-card');
    card.classList.add('editing');
    document.getElementById('daily-title').textContent=fmtTime(s.hour,s.minute)+' 예약 수정';
    document.getElementById('daily-save-btn').textContent='이대로 수정';
    renderList();
    card.scrollIntoView({behavior:'smooth',block:'center'});
  } else {
    if(editingDailyId) cancelEditDaily();
    editingTimerId=id;
    const remainMin=Math.max(1,Math.round((s.trigger_at-Date.now()/1000)/60));
    document.getElementById('timer-min').value=remainMin;
    setSeg('timer-seg',s.action);
    timerChanged();
    const card=document.getElementById('timer-card');
    card.classList.add('editing');
    document.getElementById('timer-title').textContent='타이머 수정';
    document.getElementById('timer-save-btn').textContent='이대로 수정';
    renderList();
    card.scrollIntoView({behavior:'smooth',block:'center'});
  }
}
async function deleteEditing(kind){
  const id = kind==='daily' ? editingDailyId : editingTimerId;
  if(!id) return;
  await fetch('/schedule/'+id,{method:'DELETE'});
  toast(kind==='daily'?'예약을 지웠어요':'타이머를 지웠어요');
  if(kind==='daily') cancelEditDaily(); else cancelEditTimer();
  loadSchedules();
}
async function toggleSchedule(ev,id,btn){
  ev.stopPropagation();
  const d=await (await fetch('/schedule/'+id+'/toggle',{method:'POST'})).json();
  if(d.ok){ btn.className='pill '+(d.enabled?'live':''); btn.textContent=d.enabled?'작동중':'중단됨'; }
}
function renderList(){
  const el=document.getElementById('sch-list');
  const d=latestSchedules;
  if(!d.length){ el.innerHTML='<div class="empty">아직 예약이 없어요</div>'; return; }
  el.innerHTML=d.map(s=>{
    let desc,sub;
    if(s.type==='daily'){
      desc=fmtTime(s.hour,s.minute)+' <span class="onoff '+(s.action==='off'?'is-off':'')+'">'+(s.action==='on'?'켜기':'끄기')+'</span>';
      sub=fmtDays(s.days);
    } else {
      desc=esc(s.label||'타이머')+' <span class="onoff '+(s.action==='off'?'is-off':'')+'">'+(s.action==='on'?'켜기':'끄기')+'</span>';
      sub='타이머';
    }
    const on=s.enabled!==false;
    const editing=(s.id===editingDailyId||s.id===editingTimerId)?' editing-row':'';
    return '<div class="sch'+editing+'" onclick="editSchedule('+s.id+')">'
      +'<div><div class="t">'+desc+'</div><div class="d">'+sub+'</div></div>'
      +'<div class="right">'
      +'<button class="pill '+(on?'live':'')+'" onclick="toggleSchedule(event,'+s.id+',this)">'+(on?'작동중':'중단됨')+'</button>'
      +'<svg class="ic chev"><use href="#i-arrow"/></svg>'
      +'</div></div>';
  }).join('');
}
async function loadSchedules(){
  try{
    latestSchedules=await (await fetch('/schedules')).json();
    renderList();
  }catch(e){}
}

document.getElementById('dev-box').addEventListener('toggle', e=>{ e.target.dataset.touched='1'; });
buildDays();
loadDeviceInfo();
loadSchedules();
loadBatteryState();
setInterval(loadSchedules,10000);
</script>
</body>
</html>"""

# ── API 라우트 ───────────────────────────────────
@app.route("/")
def index():
    return HTML

@app.route("/config", methods=["GET"])
def get_config():
    return jsonify(config)

@app.route("/config", methods=["POST"])
def set_config():
    data = request.json or {}
    try:
        device_type = int(data.get("device_type", 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "device_type이 잘못됐습니다."}), 400
    if device_type not in (1, 2):
        return jsonify({"ok": False, "error": "device_type은 1 또는 2여야 합니다."}), 400
    config["device_address"] = data.get("address")
    config["device_name"] = data.get("name")
    config["device_type"] = device_type
    save_config(config)
    return jsonify({"ok": True})

@app.route("/scan", methods=["POST"])
def scan():
    try:
        devices = _run_ble(_scan_ble_devices(10))
        return jsonify({"ok": True, "devices": devices})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/switch/<action>", methods=["POST"])
def switch(action):
    if action not in ("on", "off"):
        return jsonify({"ok": False, "error": "action은 on/off만 가능합니다."}), 400
    try:
        run_command(action)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/battery")
def battery():
    try:
        level = get_battery_level()
        record_battery(level)
        return jsonify({"ok": True, "level": level})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/battery/state")
def battery_state():
    """마지막으로 읽은 값 — 화면이 켜지자마자 보여주려고. BLE를 건드리지 않는다."""
    return jsonify(load_battery_state())

@app.route("/ble/reset", methods=["POST"])
def ble_reset_route():
    try:
        steps = ble_reset()
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/ble/forget", methods=["POST"])
def ble_forget():
    config["device_address"] = None
    config["device_name"] = None
    save_config(config)
    return jsonify({"ok": True})

def _validate_daily(data, defaults):
    """매일 예약 필드 검증. (필드dict, None) 또는 (None, 오류문구)."""
    try:
        hour = int(data.get("hour", defaults["hour"]))
        minute = int(data.get("minute", defaults["minute"]))
    except (TypeError, ValueError):
        return None, "시간 값이 잘못됐습니다."
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, "시간 범위를 벗어났습니다."
    action = data.get("action", defaults["action"])
    if action not in ("on", "off"):
        return None, "action은 on/off만 가능합니다."
    days = data.get("days", defaults["days"])
    if not (isinstance(days, list) and days
            and all(isinstance(d, int) and 0 <= d <= 6 for d in days)):
        return None, "days가 잘못됐습니다."
    return {"hour": hour, "minute": minute, "action": action, "days": days}, None

def _validate_timer(data, default_action="off"):
    """타이머 필드 검증. ((minutes, action), None) 또는 (None, 오류문구)."""
    try:
        minutes = int(data.get("minutes", 30))
    except (TypeError, ValueError):
        return None, "minutes가 잘못됐습니다."
    if minutes < 1:
        return None, "minutes는 1 이상이어야 합니다."
    action = data.get("action", default_action)
    if action not in ("on", "off"):
        return None, "action은 on/off만 가능합니다."
    return (minutes, action), None

@app.route("/schedule/timer", methods=["POST"])
def add_timer():
    global schedule_id_counter
    data = request.json or {}
    parsed, err = _validate_timer(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    minutes, action = parsed
    trigger_at = time.time() + minutes * 60
    trigger_dt = datetime.fromtimestamp(trigger_at)
    s = {
        "id": schedule_id_counter,
        "type": "timer",
        "action": action,
        "trigger_at": trigger_at,
        "label": f"{minutes}분 후 ({trigger_dt.strftime('%H:%M')} 실행)",
    }
    schedule_id_counter += 1
    schedules.append(s)
    save_schedules()
    return jsonify({"ok": True})

@app.route("/schedule/daily", methods=["POST"])
def add_daily():
    global schedule_id_counter
    data = request.json or {}
    fields, err = _validate_daily(data, {"hour": 23, "minute": 0,
                                         "action": "off", "days": list(range(7))})
    if err:
        return jsonify({"ok": False, "error": err}), 400
    s = {"id": schedule_id_counter, "type": "daily", "enabled": True, **fields}
    schedule_id_counter += 1
    schedules.append(s)
    save_schedules()
    return jsonify({"ok": True})

@app.route("/schedule/<int:sid>", methods=["DELETE"])
def delete_schedule(sid):
    schedules[:] = [s for s in schedules if s["id"] != sid]
    save_schedules()
    return jsonify({"ok": True})

@app.route("/schedule/<int:sid>", methods=["PATCH"])
def update_schedule(sid):
    data = request.json or {}
    for s in schedules:
        if s["id"] != sid:
            continue
        if s["type"] == "daily":
            fields, err = _validate_daily(data, s)
            if err:
                return jsonify({"ok": False, "error": err}), 400
            s.update(fields)
        elif s["type"] == "timer":
            parsed, err = _validate_timer(data, default_action=s["action"])
            if err:
                return jsonify({"ok": False, "error": err}), 400
            minutes, action = parsed
            trigger_at = time.time() + minutes * 60
            trigger_dt = datetime.fromtimestamp(trigger_at)
            s["action"] = action
            s["trigger_at"] = trigger_at
            s["label"] = f"{minutes}분 후 ({trigger_dt.strftime('%H:%M')} 실행)"
        save_schedules()
        return jsonify({"ok": True, "schedule": s})
    return jsonify({"ok": False, "error": "not found"}), 404

@app.route("/schedule/<int:sid>/toggle", methods=["POST"])
def toggle_schedule(sid):
    for s in schedules:
        if s["id"] == sid:
            s["enabled"] = not s.get("enabled", True)
            save_schedules()
            return jsonify({"ok": True, "enabled": s["enabled"]})
    return jsonify({"ok": False})

@app.route("/schedules")
def get_schedules():
    return jsonify(schedules)

# ── 앱 아이콘 (base64 내장: 파일 없이 단일파일·미러 배포 유지) ──
# 로고 = 하프디스크(빛↔어둠 = 켜짐↔꺼짐), 앰버 #f5a524 on #131114
_APPLE_ICON_180 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAADRElEQVR42u3dsVErQRBF0aVLAUCBTYSEowgVASF847uUgAKhft3n2jjMHs2spNnRw/PTyyF9VBkCwSE4BIfgEByCQ3AIDsEhOCQ4BIfgEByCQ3AIDsEhOASHBIfgEByCQ3AIDsEhOASH4JDgEBz6fqe1//nl/PjFv3x9e985RA9Ljn36OgVctuD4XRPblMzEcWsTS5SMwvH3JmYrGYKjA4t5ROJxdGMxiUgwjs4sZhCJxJHCIp1IHo7fkvHh1bopuzgfpz0y7n5tLufHLB+n8SxaXY///0UKkdNUGZ0vQMoUUvNkvL699x/6iHvqmjSIESyCfNSYb9UT3ys291HpAxfKIsJHpcs4Dl8MbcKxSkZnH5U4TOlLSYqPSpRxHPatLcOxXEZDH0UGH/HPrax9PmA7jk9fK9tkNJk8igw+muIgo7MPz8qqJQ7TRvPJo7w9UTsc118TZHSYPNxzqBkO00bE5GHmUCccpo2UycPMoRAcpo3VOBKfgV67spRpQ+451BuHNSVrAMuaIsuKGuOwpsQNY1lTZFkRHIJDcGgBjiv32O5G275hMXMIDsEhOASH4BAcgkNwCA7BIcEhOJSD48pXr/aWtt1VaeYQHIJDcMjZ51qCw17RuGG0rGjEj/FoMg4rS9YAWlY05QcANRmHlSVo6Mo5vXLPoQQc16dHk0ef5djMoWY4TB4Rd/FmDvXDYfLo/+a/nF+jjjg+fU3wcd/PDN1zqCsOk0fnrxqq/xDs9NHhS6iMs8+3+Wjy9WQ5fFMTfoxniY8+uxrCfgBwvI9W+13yfld2sI9uO6EqcYAu58d5RBrukavcYZrko+fuyYoerBk+2u6rrfQhS19iOu+4rhkDF0qk+V78mjR8WUT6P6VR8wYxgkjE8zsPz08vx+iPz78795CRh+PnF6/DJ2xZD/yF4fjF6/fhdbopjrhHQfNwhH68kfiQcCSOLCK5z44H4+hPJP1IgXgcPYnMOGliCI4+RCYdQDIKxx2VjDyUZiYOn1vAcTclSw6v2oLjJ1zWnmO2F4cOz8oKDsEhOASH4BAcgkNwCA5DIDgEh+AQHIJDcAgOwSE4BIcEh+AQHIJDcAgOwSE4BIfgkOAQHIJDN+gfwn07cTEV0IkAAAAASUVORK5CYII="
)

@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return Response(_APPLE_ICON_180, mimetype="image/png")


# ── 명령 우체통 (허브 서버 경유) ───────────────────
# 푸시(hub_push)의 반대 방향이다. 볼트 서버에서 도는 헤르메스가 조명을 끄고 싶어도
# 이 맥으로는 직접 못 닿는다 — 맥은 공유기 뒤에 있고, 닿게 하려면 외부 문을 열어야
# 하는데 집 안 기기에 그건 하면 안 된다.
# 그래서 방향을 뒤집는다. 헤르메스는 허브 우체통에 넣기만 하고, 여기서 주기적으로
# 가지러 간다. 나가는 통신만 하므로 문을 하나도 열지 않는다.
CMDBOX_INTERVAL = 10        # 초. 조명은 "말하고 몇 초 뒤"면 충분히 즉각적이다
CMDBOX_NAME = "switcher"    # 우체통에서 내 앞으로 온 것만 골라내는 이름


def _cmdbox_urls():
    """hub_push.json 의 푸시 주소에서 우체통 주소를 만든다 — 설정을 두 벌 두지 않으려고."""
    cfg = load_hub_push()
    if not cfg.get("url") or not cfg.get("token"):
        return None, None, None
    base = cfg["url"].replace("/api/push/notify", "/api/mailbox")
    return base, base + "/%s/done", cfg["token"]


def _cmdbox_call(url, token, payload=None):
    import urllib.request
    req = urllib.request.Request(
        url, method="POST" if payload is not None else "GET",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json", "X-Hub-Token": token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def cmdbox_run(cmd, args):
    """우체통 명령을 실제 동작으로 옮긴다. 여기 없는 명령은 거부한다 —
    바깥에서 온 문자열이 그대로 실행되면 안 되므로 목록에 있는 것만 처리한다."""
    if cmd == "switch":
        action = str(args.get("action", "")).lower()
        if action not in ("on", "off"):
            return False, "action은 on/off만 가능"
        run_command(action)
        return True, "조명 %s" % ("켬" if action == "on" else "끔")
    if cmd == "battery":
        level = get_battery_level()
        record_battery(level)
        return True, "배터리 %s%%" % level
    if cmd == "schedules":
        return True, json.dumps(load_schedules() or [], ensure_ascii=False)[:500]
    return False, "모르는 명령: %s" % cmd


def cmdbox_loop():
    box, done_tpl, token = _cmdbox_urls()
    if not box:
        print("[우체통] hub_push.json 없음 — 원격 명령 안 받음", flush=True)
        return
    print(f"[우체통] {CMDBOX_INTERVAL}초마다 확인", flush=True)
    quiet_fail = 0
    while True:
        try:
            items = _cmdbox_call(f"{box}?to={CMDBOX_NAME}", token).get("items", [])
            quiet_fail = 0
            for m in items:
                try:
                    ok, result = cmdbox_run(m.get("cmd", ""), m.get("args") or {})
                except Exception as e:
                    ok, result = False, str(e)
                print(f"[우체통] {m.get('cmd')} → {result}", flush=True)
                try:
                    _cmdbox_call(done_tpl % m["id"], token, {"ok": ok, "result": result})
                except Exception as e:
                    print(f"[우체통] 결과 보고 실패: {e}", flush=True)
        except Exception as e:
            # 인터넷이 끊기거나 허브가 배포 중이면 실패한다. 10초마다 같은 줄을
            # 찍으면 로그가 못 쓰게 되니 처음 한 번과 가끔만 남긴다.
            quiet_fail += 1
            if quiet_fail == 1 or quiet_fail % 60 == 0:
                print(f"[우체통] 확인 실패({quiet_fail}회): {e}", flush=True)
        time.sleep(CMDBOX_INTERVAL)


if __name__ == "__main__":
    import socket
    load_schedules()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=battery_watch_loop, daemon=True).start()
    threading.Thread(target=cmdbox_loop, daemon=True).start()
    register_mdns(PORT)
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "127.0.0.1"
    print(f"\n✅ 서버 시작!")
    print(f"📱 폰에서 접속: http://switcher.local:{PORT}")
    print(f"   (IP 직접 접속도 가능: http://{local_ip}:{PORT})\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
