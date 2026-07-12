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
from flask import Flask, jsonify, request

# ── 경로 설정 ────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SCHEDULE_FILE = os.path.join(BASE_DIR, "schedules.json")

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
def register_mdns(port=PORT):
    try:
        from zeroconf import ServiceInfo, Zeroconf
        import socket
        # gethostbyname(gethostname())은 macOS에서 127.0.0.1을 주는 경우가 많아
        # 폰이 switcher.local→자기 자신으로 접속하게 됨. UDP 소켓 트릭으로
        # 실제 LAN 인터페이스 IP를 얻는다(패킷은 실제로 안 나감).
        try:
            _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))
            local_ip = _s.getsockname()[0]
            _s.close()
        except OSError:
            local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip.startswith("127."):
            raise RuntimeError(f"LAN IP를 못 찾음(감지값 {local_ip}) — mDNS 등록 건너뜀")
        info = ServiceInfo(
            "_http._tcp.local.",
            "switcher._http._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={"path": "/"},
            server="switcher.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        print(f"🌐 mDNS 등록 완료: http://switcher.local:{port}")
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
<meta name="theme-color" content="#eef0f3">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="스위처">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.min.css">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='24' fill='%23111317'/%3E%3Ccircle cx='50' cy='46' r='20' fill='%23f5a524'/%3E%3C/svg%3E">
<title>스위처</title>
<style>
  :root{ --ease:cubic-bezier(.16,1,.3,1);
    --bg:#eef0f3; --ink:#14161a; --dim:#7a808a; --faint:#9aa0aa; --hair:rgba(20,25,40,.08);
    --amber:#f5a524; --dark:#111317; --emer:#10b981; --rose:#ef4444;
    --float:0 24px 60px -18px rgba(20,25,40,.14), 0 2px 6px -2px rgba(20,25,40,.06); }
  *{ box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
  html,body{ background:var(--bg); }
  body{ font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',system-ui,sans-serif;
    color:var(--ink); min-height:100dvh; max-width:480px; margin:0 auto;
    padding:calc(env(safe-area-inset-top) + 26px) 18px calc(env(safe-area-inset-bottom) + 60px); }
  .ic{ width:1em; height:1em; display:inline-block; vertical-align:-.14em; fill:currentColor; flex:0 0 auto; }
  .keep{ word-break:keep-all; }

  .float{ background:#fff; border-radius:26px; box-shadow:var(--float); }
  .inset{ background:#f5f6f8; border-radius:18px; box-shadow:inset 0 0 0 1px var(--hair); }

  /* 헤더 */
  header{ display:flex; align-items:center; justify-content:space-between; margin-bottom:20px; padding:0 4px; }
  .eyebrow{ font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:var(--faint); font-weight:800; }
  h1.dev{ font-size:25px; font-weight:800; letter-spacing:-.02em; line-height:1.1; margin-top:3px; }
  .bt{ width:44px; height:44px; border-radius:15px; background:#fff; box-shadow:0 8px 20px -8px rgba(20,25,40,.18);
    display:flex; align-items:center; justify-content:center; color:var(--amber); font-size:20px; }

  /* 히어로 */
  .hero{ padding:26px 24px; margin-bottom:16px; }
  .meta{ display:flex; align-items:center; justify-content:space-between; margin-bottom:22px; }
  .badge{ display:inline-flex; align-items:center; gap:7px; font-size:12px; font-weight:600; color:var(--dim); }
  .dot{ width:7px; height:7px; border-radius:50%; background:var(--faint); }
  .badge.live .dot{ background:var(--emer); }
  .bat{ display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:600; color:var(--dim); }
  .state-row{ display:flex; align-items:center; gap:20px; margin-bottom:22px; }
  .disc{ width:84px; height:84px; border-radius:24px; display:flex; align-items:center; justify-content:center;
    font-size:38px; background:#eef0f3; color:#b6bcc6; box-shadow:inset 0 0 0 1px var(--hair);
    transition:all .6s var(--ease); flex:0 0 auto; }
  .on .disc{ background:var(--dark); color:var(--amber); box-shadow:0 16px 34px -14px rgba(0,0,0,.45); }
  .st-lbl-eye{ font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--faint); font-weight:800; margin-bottom:6px; }
  .st-lbl{ font-size:42px; font-weight:800; letter-spacing:-.03em; line-height:1; transition:color .5s var(--ease); }
  .off .st-lbl, body:not(.on):not(.off) .st-lbl{ color:var(--ink); }
  .on .st-lbl{ color:var(--ink); }

  .pwr-grid{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .pwr{ display:flex; flex-direction:column; align-items:center; gap:9px; padding:20px 0; border-radius:20px;
    font-size:14px; font-weight:700; cursor:pointer; border:1px solid transparent;
    transition:transform .4s var(--ease), box-shadow .5s var(--ease); }
  .pwr .ic{ font-size:26px; }
  .pwr:active{ transform:scale(.96); }
  .pwr-on{ background:#fff3dc; color:#b77608; border-color:rgba(245,165,36,.35); }
  .pwr-on .ic{ color:var(--amber); }
  .on .pwr-on{ box-shadow:0 12px 26px -14px rgba(245,165,36,.45); }
  .pwr-off{ background:#eef1f6; color:#6b7280; }
  .pwr-off .ic{ color:#9aa2af; }
  .off .pwr-off{ box-shadow:inset 0 0 0 1px var(--hair); }

  /* 섹션 헤더 */
  .sec{ margin-top:26px; }
  .sec-h{ display:flex; align-items:center; justify-content:space-between; margin:0 4px 12px; }
  .sec-h h2{ font-size:17px; font-weight:800; letter-spacing:-.02em; }
  .sec-h .r{ display:inline-flex; align-items:center; gap:5px; font-size:13px; font-weight:800; color:var(--amber);
    background:none; border:none; cursor:pointer; }

  /* 배너 */
  .banner{ display:none; background:#fff6e6; border:1px solid rgba(245,165,36,.32); border-radius:18px;
    padding:15px 18px; margin-bottom:14px; font-size:13px; color:#8a5a08; line-height:1.5; }
  .banner strong{ display:block; font-size:14px; margin-bottom:3px; color:#7a4e06; }

  /* 기기 */
  .pad{ padding:20px; }
  .hint{ font-size:12.5px; color:var(--faint); line-height:1.6; }
  .wide{ width:100%; padding:14px 0; border-radius:15px; border:none; font-size:14.5px; font-weight:700; cursor:pointer;
    display:flex; align-items:center; justify-content:center; gap:8px; transition:transform .4s var(--ease); }
  .wide:active{ transform:scale(.985); }
  .wide.dark{ background:var(--dark); color:#fff; }
  .wide.dark .ic{ color:var(--amber); }
  .wide:disabled{ opacity:.4; cursor:not-allowed; }
  .scan-note{ font-size:12.5px; color:var(--faint); text-align:center; min-height:16px; margin:10px 0 2px; }
  .dev-list{ display:flex; flex-direction:column; gap:8px; }
  .dev{ display:flex; align-items:center; justify-content:space-between; padding:12px 14px; border-radius:14px;
    border:1px solid var(--hair); background:#f5f6f8; cursor:pointer; transition:all .3s var(--ease); }
  .dev.sel{ border-color:var(--amber); background:#fff7e9; }
  .dev .nm{ font-size:14px; font-weight:700; }
  .dev .ad{ font-size:10.5px; color:var(--faint); margin-top:2px; font-family:ui-monospace,monospace; }
  .dev .rs{ font-size:11.5px; color:var(--faint); }
  .save{ width:100%; padding:14px 0; border-radius:15px; border:none; margin-top:12px; background:var(--emer);
    color:#04241a; font-size:14.5px; font-weight:800; cursor:pointer; }
  .save:disabled{ opacity:.35; cursor:not-allowed; }
  .divider{ height:1px; background:var(--hair); margin:18px 0; }
  .subh{ font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--faint); font-weight:800; margin-bottom:10px; }
  .row2{ display:flex; gap:10px; }
  .type{ flex:1; padding:12px 0; border-radius:13px; font-size:13.5px; font-weight:700; cursor:pointer;
    background:#f5f6f8; border:1px solid var(--hair); color:var(--faint); transition:all .3s var(--ease); }
  .type.on{ background:var(--dark); border-color:var(--dark); color:#fff; }
  .mng{ flex:1; padding:13px 0; border-radius:14px; border:none; background:#f5f6f8; box-shadow:inset 0 0 0 1px var(--hair);
    font-size:13px; font-weight:700; color:var(--dim); cursor:pointer; display:flex; align-items:center; justify-content:center; gap:7px;
    transition:transform .4s var(--ease); }
  .mng:active{ transform:scale(.97); }
  .mng .ic{ color:var(--amber); font-size:17px; }
  .mng.danger{ color:var(--rose); background:#fff0f0; box-shadow:inset 0 0 0 1px rgba(239,68,68,.18); }
  .mng.danger .ic{ color:var(--rose); }

  /* 예약/타이머 폼 */
  .tabs{ display:flex; background:#e7e9ed; border-radius:14px; padding:3px; margin-bottom:16px; }
  .tab{ flex:1; padding:9px 0; border:none; background:none; border-radius:11px; font-size:13.5px; font-weight:700;
    color:var(--dim); cursor:pointer; transition:all .3s var(--ease); }
  .tab.active{ background:#fff; color:var(--ink); box-shadow:0 1px 4px rgba(20,25,40,.1); }
  .days{ display:flex; gap:6px; margin-bottom:16px; }
  .day{ flex:1; min-width:0; padding:10px 0; border-radius:12px; border:1px solid var(--hair); background:#f5f6f8;
    color:var(--faint); font-weight:700; font-size:13px; text-align:center; cursor:pointer; transition:all .25s var(--ease); }
  .day.on{ background:var(--dark); color:#fff; border-color:var(--dark); }
  .presets{ display:flex; gap:8px; margin-bottom:14px; }
  .preset{ flex:1; padding:9px 0; border-radius:12px; border:1px solid var(--hair); background:#f5f6f8;
    font-size:12.5px; font-weight:700; color:var(--dim); cursor:pointer; }
  .field{ display:flex; gap:10px; align-items:center; }
  input[type=time], input[type=number]{ flex:1; padding:12px 14px; border-radius:13px; border:1px solid var(--hair);
    background:#f5f6f8; font-size:15px; color:var(--ink); font-family:inherit; appearance:none; }
  .seg{ display:flex; border-radius:13px; overflow:hidden; border:1px solid var(--hair); flex:1; }
  .seg button{ flex:1; padding:12px 0; border:none; background:#f5f6f8; font-size:13.5px; font-weight:800;
    color:var(--faint); cursor:pointer; transition:all .25s var(--ease); }
  .seg .on-active{ background:#fff3dc; color:#b77608; }
  .seg .off-active{ background:#e7e9ed; color:#3a3f48; }
  .add{ width:100%; margin-top:15px; padding:14px 0; border-radius:15px; border:none; background:var(--dark);
    color:#fff; font-size:14.5px; font-weight:800; cursor:pointer; transition:transform .4s var(--ease); }
  .add:active{ transform:scale(.985); }

  /* 목록 */
  .empty{ color:var(--faint); font-size:13.5px; text-align:center; padding:6px 0; }
  .sch{ display:flex; align-items:center; justify-content:space-between; padding:14px 4px; border-bottom:1px solid var(--hair); }
  .sch:last-child{ border-bottom:none; }
  .sch .d{ font-size:15px; font-weight:800; }
  .sch .d .mid{ color:#c2c7cf; font-weight:600; }
  .sch .s{ font-size:12px; color:var(--faint); margin-top:3px; letter-spacing:.4px; }
  .chip{ border:none; border-radius:999px; padding:7px 13px; font-size:12px; font-weight:800; cursor:pointer; }
  .chip-on{ background:#eafaf0; color:#1a8f52; }
  .chip-off{ background:#f0f1f4; color:var(--faint); }
  .del{ background:#fff0f0; border:none; color:var(--rose); border-radius:999px; padding:7px 12px; font-size:12px; font-weight:700; cursor:pointer; }
  .cancel-edit{ width:100%; margin-top:8px; padding:12px 0; border-radius:15px; border:none; background:#f0f1f4; color:var(--faint); font-size:13.5px; font-weight:700; cursor:pointer; }
  .sch>div:first-child:active{ opacity:.55; }

  /* 배터리 버튼 */
  .batbtn{ display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:700; color:var(--dim);
    background:#f5f6f8; border:1px solid var(--hair); border-radius:999px; padding:6px 13px; cursor:pointer;
    transition:transform .3s var(--ease); }
  .batbtn:active{ transform:scale(.95); }
  /* 전송 스피너 */
  .spin{ width:1em; height:1em; border:2.5px solid currentColor; border-right-color:transparent; border-radius:50%;
    display:inline-block; animation:sp .7s linear infinite; }
  @keyframes sp{ to{ transform:rotate(360deg); } }
  .pwr[disabled]{ cursor:default; }
  .pwr[disabled]:active{ transform:none; }
  .pwr.busy{ opacity:.9; }
  /* 기기 설정 접기 */
  .dev-box > summary{ list-style:none; cursor:pointer; display:flex; align-items:center; justify-content:space-between; margin:0 4px 12px; }
  .dev-box > summary::-webkit-details-marker{ display:none; }
  .dev-box summary .s-left{ display:flex; align-items:baseline; gap:10px; }
  .dev-box summary h2{ font-size:17px; font-weight:800; letter-spacing:-.02em; }
  .dev-box summary .cur{ font-size:12.5px; color:var(--faint); font-weight:600; }
  .dev-box summary .chev{ color:var(--faint); font-size:17px; transition:transform .4s var(--ease); }
  .dev-box[open] > summary .chev{ transform:rotate(90deg); }
  /* 타이머 프리셋 */
  .tpresets{ display:flex; gap:8px; margin-bottom:12px; }
  .tp{ flex:1; padding:9px 0; border-radius:12px; border:1px solid var(--hair); background:#f5f6f8;
    font-size:13px; font-weight:700; color:var(--dim); cursor:pointer; transition:transform .3s var(--ease); }
  .tp:active{ transform:scale(.97); background:#e7e9ed; }

  #toast{ position:fixed; left:50%; bottom:calc(env(safe-area-inset-bottom) + 22px); transform:translateX(-50%) translateY(18px);
    background:#14161a; color:#fff; font-size:13.5px; font-weight:600; padding:12px 20px; border-radius:14px;
    box-shadow:0 16px 40px -14px rgba(0,0,0,.5); opacity:0; pointer-events:none; transition:all .35s var(--ease); z-index:50;
    max-width:88vw; text-align:center; }
  #toast.show{ opacity:1; transform:translateX(-50%) translateY(0); }
  @media (prefers-reduced-motion: reduce){ *{ transition-duration:.01ms !important; animation-duration:.01ms !important; animation-iteration-count:1 !important; } }
</style>
</head>
<body>

<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-bulb-bolt" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M11.5 2C7.358 2 4 5.436 4 9.674c0 2.273.966 4.315 2.499 5.72c.51.467.889.814 1.157 1.066a15 15 0 0 1 .4.39l.033.036c.237.3.288.376.318.446s.053.16.112.54c.024.15.026.406.026 1.105v.03c0 .409 0 .762.026 1.051c.027.306.087.61.248.895c.18.319.438.583.75.767c.278.165.575.226.874.254c.283.026.628.026 1.028.026h.058c.4 0 .745 0 1.028-.026c.3-.028.595-.09.875-.254a2.07 2.07 0 0 0 .749-.767c.16-.285.22-.588.248-.895c.026-.29.026-.642.025-1.051v-.03c0-.699.003-.955.026-1.105c.06-.38.082-.47.113-.54c.03-.07.081-.147.318-.446l.008-.01l.025-.026l.088-.09q.112-.113.312-.3c.268-.252.647-.599 1.157-1.067A7.74 7.74 0 0 0 19 9.674C19 5.436 15.642 2 11.5 2m1.585 17.674h-3.17q.004.145.014.258c.019.21.05.286.071.324a.7.7 0 0 0 .25.255c.037.022.111.054.316.073c.214.02.497.02.934.02s.72 0 .934-.02c.205-.019.279-.05.316-.073a.7.7 0 0 0 .25-.255c.021-.038.052-.114.07-.324q.011-.113.015-.258M12.61 8.176c.307.224.378.66.159.974l-1.178 1.687h1.402a.68.68 0 0 1 .607.379a.71.71 0 0 1-.052.724L11.6 14.731a.67.67 0 0 1-.951.162a.71.71 0 0 1-.158-.973l1.178-1.687h-1.403a.68.68 0 0 1-.606-.379a.71.71 0 0 1 .051-.725l1.948-2.79a.67.67 0 0 1 .951-.163" clip-rule="evenodd"/></symbol>
  <symbol id="i-bulb" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M11.5 2C7.358 2 4 5.436 4 9.674c0 2.273.966 4.315 2.499 5.72c.51.467.889.814 1.157 1.066a15 15 0 0 1 .4.39l.033.036c.237.3.288.376.318.446s.053.16.112.54c.024.15.026.406.026 1.105v.03c0 .409 0 .762.026 1.051c.027.306.087.61.248.895c.18.319.438.583.75.767c.278.165.575.226.874.254c.283.026.628.026 1.028.026h.058c.4 0 .745 0 1.028-.026c.3-.028.595-.09.875-.254a2.07 2.07 0 0 0 .749-.767c.16-.285.22-.588.248-.895c.026-.29.026-.642.025-1.051v-.03c0-.699.003-.955.026-1.105c.06-.38.082-.47.113-.54c.03-.07.081-.147.318-.446l.005-.006l.025-.027l.088-.09q.112-.113.312-.3c.268-.252.647-.599 1.157-1.067A7.74 7.74 0 0 0 19 9.674C19 5.436 15.642 2 11.5 2m1.585 17.674q-.004.145-.015.258c-.018.21-.049.286-.07.324a.7.7 0 0 1-.25.255c-.037.023-.111.054-.316.073c-.214.02-.497.02-.934.02s-.72 0-.934-.02c-.205-.019-.279-.05-.316-.073a.7.7 0 0 1-.25-.255c-.021-.038-.052-.114-.07-.324a5 5 0 0 1-.015-.258zm-3.811-6.324a.75.75 0 0 1 1.025.274a1.25 1.25 0 0 0 2.166 0a.75.75 0 1 1 1.298.752a2.76 2.76 0 0 1-1.631 1.27V17a.75.75 0 0 1-1.5 0v-1.354A2.76 2.76 0 0 1 9 14.376a.75.75 0 0 1 .274-1.025" clip-rule="evenodd"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path fill="currentColor" d="M12 22c5.523 0 10-4.477 10-10c0-.463-.694-.54-.933-.143a6.5 6.5 0 1 1-8.924-8.924C12.54 2.693 12.463 2 12 2C6.477 2 2 6.477 2 12s4.477 10 10 10"/></symbol>
  <symbol id="i-battery" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M3.172 5.172C2 6.343 2 8.229 2 12s0 5.657 1.172 6.828S6.229 20 10 20h1.5c3.771 0 5.657 0 6.828-1.172c.944-.943 1.127-2.348 1.163-4.828H20c.943 0 1.414 0 1.707-.293S22 12.943 22 12s0-1.414-.293-1.707S20.943 10 20 10h-.509c-.036-2.48-.22-3.885-1.163-4.828C17.157 4 15.271 4 11.5 4H10C6.229 4 4.343 4 3.172 5.172M7 9c.65-.361.655-.365.656-.364l.002.004l.004.007l.01.018l.026.053q.03.064.075.175c.06.147.132.356.202.631c.142.551.274 1.364.274 2.474s-.132 1.923-.274 2.474c-.07.275-.143.484-.202.631a3 3 0 0 1-.102.228l-.01.018l-.004.007l-.001.002L7 15l-.656.363a.75.75 0 0 1-1.317-.72l.005-.01a4 4 0 0 0 .18-.534c.108-.424.226-1.111.226-2.101s-.118-1.677-.226-2.101a4 4 0 0 0-.18-.534l-.005-.01a.75.75 0 0 1 1.317-.719zm3.51-.5l.646.364l.001-.001a.75.75 0 0 0-1.317-.72l-.005.011l-.038.087a5 5 0 0 0-.142.447c-.108.424-.226 1.111-.226 2.101s.118 1.677.226 2.101c.055.212.107.36.142.447l.038.087l.005.01a.75.75 0 0 0 1.317-.719L10.5 15.5l.654.363l.002-.003l.003-.007l.01-.018a3 3 0 0 0 .102-.228c.06-.147.132-.356.202-.631c.142-.551.274-1.364.274-2.474s-.132-1.923-.273-2.474a5 5 0 0 0-.203-.631a3 3 0 0 0-.101-.228l-.01-.018l-.005-.007z" clip-rule="evenodd"/></symbol>
  <symbol id="i-bt" viewBox="0 0 24 24"><path fill="currentColor" d="m16.743 15.158l-4.441-3.154l.006-.004l-.007-.005l4.442-3.154c.54-.383 1.012-.718 1.341-1.033c.351-.336.666-.765.666-1.35s-.315-1.014-.666-1.349c-.33-.315-.801-.65-1.341-1.034L14.91 2.774c-.73-.518-1.346-.956-1.857-1.216c-.52-.266-1.155-.465-1.79-.14c-.637.325-.844.959-.93 1.535c-.083.566-.083 1.319-.083 2.21v5.397L6.43 7.886a.75.75 0 1 0-.86 1.228L9.692 12L5.57 14.886a.75.75 0 0 0 .86 1.229l3.82-2.674v5.396c0 .89 0 1.643.084 2.209c.085.577.292 1.21.93 1.536c.634.325 1.27.125 1.79-.14c.51-.261 1.126-.698 1.856-1.216l1.832-1.302c.54-.384 1.013-.719 1.342-1.034c.351-.335.666-.764.666-1.35c0-.584-.315-1.013-.666-1.348c-.33-.316-.801-.65-1.341-1.034"/></symbol>
  <symbol id="i-cal" viewBox="0 0 24 24"><path fill="currentColor" d="M7.75 2.5a.75.75 0 0 0-1.5 0v1.58c-1.44.115-2.384.397-3.078 1.092c-.695.694-.977 1.639-1.093 3.078h19.842c-.116-1.44-.398-2.384-1.093-3.078c-.694-.695-1.639-.977-3.078-1.093V2.5a.75.75 0 0 0-1.5 0v1.513C15.585 4 14.839 4 14 4h-4c-.839 0-1.585 0-2.25.013z"/><path fill="currentColor" fill-rule="evenodd" d="M2 12c0-.839 0-1.585.013-2.25h19.974C22 10.415 22 11.161 22 12v2c0 3.771 0 5.657-1.172 6.828S17.771 22 14 22h-4c-3.771 0-5.657 0-6.828-1.172S2 17.771 2 14zm15 2a1 1 0 1 0 0-2a1 1 0 0 0 0 2m0 4a1 1 0 1 0 0-2a1 1 0 0 0 0 2m-4-5a1 1 0 1 1-2 0a1 1 0 0 1 2 0m0 4a1 1 0 1 1-2 0a1 1 0 0 1 2 0m-6-3a1 1 0 1 0 0-2a1 1 0 0 0 0 2m0 4a1 1 0 1 0 0-2a1 1 0 0 0 0 2" clip-rule="evenodd"/></symbol>
  <symbol id="i-alarm" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M12 22c4.836 0 8.757-3.884 8.757-8.675c0-4.79-3.92-8.674-8.757-8.674s-8.757 3.883-8.757 8.674S7.163 22 12 22m0-13.253c.403 0 .73.324.73.723v3.556l2.218 2.198a.72.72 0 0 1 0 1.022a.735.735 0 0 1-1.032 0l-2.432-2.41a.72.72 0 0 1-.214-.51V9.47c0-.4.327-.723.73-.723M8.24 2.34a.72.72 0 0 1-.232.996l-3.891 2.41a.734.734 0 0 1-1.006-.23a.72.72 0 0 1 .232-.996l3.892-2.41a.734.734 0 0 1 1.006.23m7.519 0a.734.734 0 0 1 1.005-.23l3.892 2.41a.72.72 0 0 1 .232.996a.734.734 0 0 1-1.006.23l-3.891-2.41a.72.72 0 0 1-.233-.996" clip-rule="evenodd"/></symbol>
  <symbol id="i-restart" viewBox="0 0 24 24"><path fill="currentColor" d="M18.258 3.508a.75.75 0 0 1 .463.693v4.243a.75.75 0 0 1-.75.75h-4.243a.75.75 0 0 1-.53-1.28L14.8 6.31a7.25 7.25 0 1 0 4.393 5.783a.75.75 0 0 1 1.488-.187A8.75 8.75 0 1 1 15.93 5.18l1.51-1.51a.75.75 0 0 1 .817-.162"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path fill="currentColor" d="M3 6.524c0-.395.327-.714.73-.714h4.788c.006-.842.098-1.995.932-2.793A3.68 3.68 0 0 1 12 2a3.68 3.68 0 0 1 2.55 1.017c.834.798.926 1.951.932 2.793h4.788c.403 0 .73.32.73.714a.72.72 0 0 1-.73.714H3.73A.72.72 0 0 1 3 6.524"/><path fill="currentColor" fill-rule="evenodd" d="M11.596 22h.808c2.783 0 4.174 0 5.08-.886c.904-.886.996-2.34 1.181-5.246l.267-4.187c.1-1.577.15-2.366-.303-2.866c-.454-.5-1.22-.5-2.753-.5H8.124c-1.533 0-2.3 0-2.753.5s-.404 1.289-.303 2.866l.267 4.188c.185 2.906.277 4.36 1.182 5.245c.905.886 2.296.886 5.079.886m-1.35-9.811c-.04-.434-.408-.75-.82-.707c-.413.043-.713.43-.672.864l.5 5.263c.04.434.408.75.82.707c.413-.044.713-.43.672-.864zm4.329-.707c.412.043.713.43.671.864l-.5 5.263c-.04.434-.409.75-.82.707c-.413-.044-.713-.43-.672-.864l.5-5.264c.04-.433.409-.75.82-.707" clip-rule="evenodd"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M21.788 21.788a.723.723 0 0 0 0-1.022L18.122 17.1a9.157 9.157 0 1 0-1.022 1.022l3.666 3.666a.723.723 0 0 0 1.022 0" clip-rule="evenodd"/></symbol>
  <symbol id="i-add" viewBox="0 0 24 24"><path fill="currentColor" fill-rule="evenodd" d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2S2 6.477 2 12s4.477 10 10 10m.75-13a.75.75 0 0 0-1.5 0v2.25H9a.75.75 0 0 0 0 1.5h2.25V15a.75.75 0 0 0 1.5 0v-2.25H15a.75.75 0 0 0 0-1.5h-2.25z" clip-rule="evenodd"/></symbol>
  <symbol id="i-arrow" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.6" d="m9 5l6 7l-6 7"/></symbol>
</svg>

<header>
  <div>
    <div class="eyebrow">SWITCHER</div>
    <h1 class="dev" id="dev-name">거실 조명</h1>
  </div>
  <div class="bt"><svg class="ic"><use href="#i-bt"/></svg></div>
</header>

<div id="banner" class="banner">
  <strong>기기가 아직 연결되지 않았어요</strong>
  아래 <b>기기</b>에서 스위처를 찾아 연결하세요.
</div>

<!-- 히어로 -->
<div class="float hero">
  <div class="meta">
    <span class="badge" id="conn-badge"><span class="dot"></span><span id="conn-txt">연결 안 됨</span></span>
    <button class="batbtn" onclick="checkBattery()"><svg class="ic"><use href="#i-battery"/></svg><span id="bat-val">배터리 확인</span></button>
  </div>
  <div class="state-row">
    <div class="disc" id="disc"><svg class="ic"><use href="#i-bulb-bolt" id="disc-icon"/></svg></div>
    <div>
      <div class="st-lbl-eye">현재 상태</div>
      <div class="st-lbl" id="st-lbl">대기</div>
    </div>
  </div>
  <div class="pwr-grid">
    <button class="pwr pwr-on" id="btn-on" onclick="cmd('on')"><svg class="ic"><use href="#i-bulb"/></svg>켜기</button>
    <button class="pwr pwr-off" id="btn-off" onclick="cmd('off')"><svg class="ic"><use href="#i-moon"/></svg>끄기</button>
  </div>
</div>

<!-- 기기 -->
<div class="sec">
  <details class="dev-box" id="dev-box">
  <summary>
    <span class="s-left"><h2>기기</h2><span class="cur" id="dev-cur"></span></span>
    <svg class="ic chev"><use href="#i-arrow"/></svg>
  </summary>
  <div class="float pad">
    <div class="hint" style="margin-bottom:16px">스위처 전원을 켜고 블루투스 범위(약 10m) 안에 두세요. 스캔 후 목록에서 골라 저장합니다.</div>
    <button class="wide dark" id="scan-btn" onclick="startScan()"><svg class="ic"><use href="#i-search"/></svg>주변 기기 스캔</button>
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
    <div class="row2">
      <button class="mng" onclick="bleReset()"><svg class="ic"><use href="#i-restart"/></svg>연결 리셋</button>
      <button class="mng danger" onclick="forgetDevice()"><svg class="ic"><use href="#i-trash"/></svg>기기 잊기</button>
    </div>
    <div class="hint" style="margin-top:12px">조명이 응답하지 않으면 <b>연결 리셋</b>으로 멈춘 블루투스 연결을 정리하세요. 다른 기기로 바꾸려면 <b>기기 잊기</b> 후 다시 스캔합니다.</div>
  </div>
  </details>
</div>

<!-- 예약 -->
<div class="sec" id="sec-daily">
  <div class="sec-h"><h2>예약</h2></div>
  <div class="float pad">
    <div class="tabs">
      <button class="tab active" onclick="switchTab('weekday', this)">요일 선택</button>
      <button class="tab" onclick="switchTab('preset', this)">빠른 선택</button>
    </div>
    <div id="tab-weekday">
      <div class="days">
        <div class="day" onclick="toggleDay(this,0)">월</div>
        <div class="day" onclick="toggleDay(this,1)">화</div>
        <div class="day" onclick="toggleDay(this,2)">수</div>
        <div class="day" onclick="toggleDay(this,3)">목</div>
        <div class="day" onclick="toggleDay(this,4)">금</div>
        <div class="day" onclick="toggleDay(this,5)">토</div>
        <div class="day" onclick="toggleDay(this,6)">일</div>
      </div>
    </div>
    <div id="tab-preset" style="display:none">
      <div class="presets">
        <button class="preset" onclick="setPreset([0,1,2,3,4])">주중</button>
        <button class="preset" onclick="setPreset([5,6])">주말</button>
        <button class="preset" onclick="setPreset([0,1,2,3,4,5,6])">매일</button>
      </div>
      <div class="days" id="preset-display">
        <div class="day" data-day="0">월</div>
        <div class="day" data-day="1">화</div>
        <div class="day" data-day="2">수</div>
        <div class="day" data-day="3">목</div>
        <div class="day" data-day="4">금</div>
        <div class="day" data-day="5">토</div>
        <div class="day" data-day="6">일</div>
      </div>
    </div>
    <div class="field" style="margin-top:4px">
      <input type="time" id="daily-time" value="23:00">
      <div class="seg">
        <button id="seg-on" onclick="setAction('on')">켜기</button>
        <button id="seg-off" class="off-active" onclick="setAction('off')">끄기</button>
      </div>
    </div>
    <button class="add" id="daily-save-btn" onclick="addDaily()">예약 추가</button>
    <button class="cancel-edit" id="daily-cancel-btn" style="display:none" onclick="cancelEditDaily()">취소</button>
  </div>
</div>

<!-- 예약 목록 -->
<div class="sec">
  <div class="sec-h"><h2>예약 목록</h2></div>
  <div class="float pad"><div id="sch-list"><div class="empty">아직 예약이 없어요</div></div></div>
</div>

<!-- 타이머 -->
<div class="sec" id="sec-timer">
  <div class="sec-h"><h2>타이머</h2></div>
  <div class="float pad">
    <div class="tpresets">
      <button class="tp" onclick="setTimer(30)">30분</button>
      <button class="tp" onclick="setTimer(60)">1시간</button>
      <button class="tp" onclick="setTimer(120)">2시간</button>
    </div>
    <div class="field">
      <input type="number" id="timer-min" value="30" min="1" max="999" style="max-width:92px">
      <span style="color:var(--faint);font-size:13.5px;white-space:nowrap;font-weight:600">분 후</span>
      <div class="seg">
        <button id="tseg-on" onclick="setTimerAction('on')">켜기</button>
        <button id="tseg-off" class="off-active" onclick="setTimerAction('off')">끄기</button>
      </div>
    </div>
    <button class="add" id="timer-save-btn" onclick="addTimer()">타이머 추가</button>
    <button class="cancel-edit" id="timer-cancel-btn" style="display:none" onclick="cancelEditTimer()">취소</button>
  </div>
</div>

<div id="toast"></div>

<script>
let selectedDays=[], selectedAction='off', timerAction='off', scanned=[], selectedIdx=-1, deviceType=1, busy=false;
let latestSchedules=[], editingDailyId=null, editingTimerId=null;
const DAY=['월','화','수','목','금','토','일'];

function esc(s){ return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function toast(msg, kind){
  const t=document.getElementById('toast');
  t.textContent=msg;
  t.style.background = kind==='bad' ? '#3a1518' : kind==='good' ? '#0f2a1a' : '#14161a';
  t.classList.add('show');
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'), 2400);
}

function applyState(s){
  document.body.classList.remove('on','off');
  if(s==='on'||s==='off') document.body.classList.add(s);
  const lbl=document.getElementById('st-lbl'), icon=document.getElementById('disc-icon');
  if(s==='on'){ lbl.textContent='켜짐'; icon.setAttribute('href','#i-bulb-bolt'); }
  else if(s==='off'){ lbl.textContent='꺼짐'; icon.setAttribute('href','#i-moon'); }
  else { lbl.textContent='대기'; icon.setAttribute('href','#i-bulb-bolt'); }
}
applyState((location.hash==='#on'||location.hash==='#off')?location.hash.slice(1):(localStorage.getItem('lastState')||''));

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
      if(!box.dataset.touched) box.open=false;      // 연결되면 기본 접힘
    } else {
      name.textContent='기기 없음'; banner.style.display='block';
      badge.classList.remove('live'); ctxt.textContent='연결 안 됨';
      cur.textContent='미설정';
      if(!box.dataset.touched) box.open=true;        // 미설정이면 펼침
    }
  }catch(e){}
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
  if(d.ok){ toast('"'+dev.name+'" 저장됨','good'); loadDeviceInfo(); }
  else toast('저장 실패','bad');
}

async function cmd(action){
  if(busy) return; busy=true;
  const on=document.getElementById('btn-on'), off=document.getElementById('btn-off');
  const tapped = action==='on'?on:off;
  const orig = tapped.innerHTML;
  on.disabled=true; off.disabled=true; tapped.classList.add('busy');
  tapped.innerHTML='<span class="spin"></span>보내는 중';
  try{
    const d=await (await fetch('/switch/'+action,{method:'POST'})).json();
    if(d.ok){ applyState(action); localStorage.setItem('lastState',action); toast(action==='on'?'켜짐':'꺼짐','good'); }
    else toast('실패: '+(d.error||''),'bad');
  }catch(e){ toast('에러','bad'); }
  finally{ tapped.innerHTML=orig; tapped.classList.remove('busy'); on.disabled=false; off.disabled=false; busy=false; }
}

function setTimer(m){ document.getElementById('timer-min').value=m; }

async function checkBattery(){
  const el=document.getElementById('bat-val'); el.textContent='확인 중…';
  try{
    const d=await (await fetch('/battery')).json();
    el.textContent = d.ok ? d.level+'%' : '실패';
  }catch(e){ el.textContent='에러'; }
}

async function bleReset(){
  toast('연결 정리 중…');
  try{
    const d=await (await fetch('/ble/reset',{method:'POST'})).json();
    toast(d.ok?'연결을 재설정했어요':'리셋 실패: '+(d.error||''), d.ok?'good':'bad');
  }catch(e){ toast('에러','bad'); }
}

async function forgetDevice(){
  if(!confirm('저장된 기기를 잊을까요? 다시 스캔해서 연결해야 합니다.')) return;
  const d=await (await fetch('/ble/forget',{method:'POST'})).json();
  if(d.ok){ toast('기기를 잊었어요','good'); loadDeviceInfo(); }
  else toast('실패','bad');
}

function switchTab(name,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); btn.classList.add('active');
  document.getElementById('tab-weekday').style.display=name==='weekday'?'':'none';
  document.getElementById('tab-preset').style.display=name==='preset'?'':'none';
  selectedDays=[]; document.querySelectorAll('.day').forEach(b=>b.classList.remove('on'));
}
function toggleDay(el,day){
  el.classList.toggle('on');
  if(el.classList.contains('on')){ if(!selectedDays.includes(day)) selectedDays.push(day); }
  else selectedDays=selectedDays.filter(d=>d!==day);
}
function setPreset(days){
  selectedDays=[...days];
  document.querySelectorAll('#preset-display .day').forEach(b=>b.classList.toggle('on',days.includes(+b.dataset.day)));
}
function setAction(a){ selectedAction=a;
  document.getElementById('seg-on').className=a==='on'?'on-active':'';
  document.getElementById('seg-off').className=a==='off'?'off-active':''; }
function setTimerAction(a){ timerAction=a;
  document.getElementById('tseg-on').className=a==='on'?'on-active':'';
  document.getElementById('tseg-off').className=a==='off'?'off-active':''; }

async function addDaily(){
  if(!selectedDays.length){ toast('요일을 선택하세요','bad'); return; }
  const t=document.getElementById('daily-time').value;
  if(!t){ toast('시간을 입력하세요','bad'); return; }
  const [h,m]=t.split(':').map(Number);
  const body=JSON.stringify({hour:h,minute:m,action:selectedAction,days:selectedDays});
  const ds=selectedDays.slice().sort().map(i=>DAY[i]).join('');
  if(editingDailyId){
    await fetch('/schedule/'+editingDailyId,{method:'PATCH',headers:{'Content-Type':'application/json'},body});
    toast('['+ds+'] '+t+' '+(selectedAction==='on'?'켜기':'끄기')+'로 수정','good');
    cancelEditDaily();
  } else {
    await fetch('/schedule/daily',{method:'POST',headers:{'Content-Type':'application/json'},body});
    toast('['+ds+'] '+t+' '+(selectedAction==='on'?'켜기':'끄기')+' 예약','good');
  }
  loadSchedules();
}
async function addTimer(){
  const min=parseInt(document.getElementById('timer-min').value);
  if(!min||min<1){ toast('분을 입력하세요','bad'); return; }
  const body=JSON.stringify({minutes:min,action:timerAction});
  if(editingTimerId){
    await fetch('/schedule/'+editingTimerId,{method:'PATCH',headers:{'Content-Type':'application/json'},body});
    toast(min+'분 후 '+(timerAction==='on'?'켜기':'끄기')+'로 수정','good');
    cancelEditTimer();
  } else {
    await fetch('/schedule/timer',{method:'POST',headers:{'Content-Type':'application/json'},body});
    toast(min+'분 후 '+(timerAction==='on'?'켜기':'끄기')+' 예약','good');
  }
  loadSchedules();
}
function editSchedule(id){
  const s=latestSchedules.find(x=>x.id===id);
  if(!s) return;
  if(s.type==='daily'){
    switchTab('weekday', document.querySelector('.tab'));
    selectedDays=s.days.slice();
    document.querySelectorAll('#tab-weekday .day').forEach((el,i)=>el.classList.toggle('on',selectedDays.includes(i)));
    document.getElementById('daily-time').value=String(s.hour).padStart(2,'0')+':'+String(s.minute).padStart(2,'0');
    setAction(s.action);
    editingDailyId=id;
    document.getElementById('daily-save-btn').textContent='수정 저장';
    document.getElementById('daily-cancel-btn').style.display='';
    document.getElementById('sec-daily').scrollIntoView({behavior:'smooth',block:'start'});
  } else {
    const remainMin=Math.max(1,Math.round((s.trigger_at-Date.now()/1000)/60));
    document.getElementById('timer-min').value=remainMin;
    setTimerAction(s.action);
    editingTimerId=id;
    document.getElementById('timer-save-btn').textContent='수정 저장';
    document.getElementById('timer-cancel-btn').style.display='';
    document.getElementById('sec-timer').scrollIntoView({behavior:'smooth',block:'start'});
  }
}
function cancelEditDaily(){
  editingDailyId=null;
  document.getElementById('daily-save-btn').textContent='예약 추가';
  document.getElementById('daily-cancel-btn').style.display='none';
}
function cancelEditTimer(){
  editingTimerId=null;
  document.getElementById('timer-save-btn').textContent='타이머 추가';
  document.getElementById('timer-cancel-btn').style.display='none';
}
async function toggleSchedule(id,btn){
  const d=await (await fetch('/schedule/'+id+'/toggle',{method:'POST'})).json();
  if(d.ok){ btn.className='chip '+(d.enabled?'chip-on':'chip-off'); btn.textContent=d.enabled?'작동중':'중단됨'; }
}
async function deleteSchedule(id){ await fetch('/schedule/'+id,{method:'DELETE'}); loadSchedules(); }

async function loadSchedules(){
  const d=await (await fetch('/schedules')).json();
  latestSchedules=d;
  const el=document.getElementById('sch-list');
  if(!d.length){ el.innerHTML='<div class="empty">아직 예약이 없어요</div>'; return; }
  el.innerHTML=d.map(s=>{
    let desc,sub;
    if(s.type==='daily'){
      desc=String(s.hour).padStart(2,'0')+':'+String(s.minute).padStart(2,'0')+' <span class="mid">·</span> '+(s.action==='on'?'켜기':'끄기');
      sub=s.days.slice().sort().map(i=>DAY[i]).join(' ');
    } else { desc=s.label; sub=(s.action==='on'?'켜기':'끄기'); }
    const on=s.enabled!==false;
    return `<div class="sch">
      <div style="cursor:pointer" onclick="editSchedule(${s.id})"><div class="d">${desc}</div><div class="s">${sub}</div></div>
      <div style="display:flex;gap:7px;align-items:center">
        <button class="chip ${on?'chip-on':'chip-off'}" onclick="toggleSchedule(${s.id},this)">${on?'작동중':'중단됨'}</button>
        <button class="del" onclick="deleteSchedule(${s.id})">삭제</button>
      </div></div>`;
  }).join('');
}

document.getElementById('dev-box').addEventListener('toggle', e=>{ e.target.dataset.touched='1'; });
loadDeviceInfo();
loadSchedules();
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
        return jsonify({"ok": True, "level": level})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

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

if __name__ == "__main__":
    import socket
    load_schedules()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    register_mdns(PORT)
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "127.0.0.1"
    print(f"\n✅ 서버 시작!")
    print(f"📱 폰에서 접속: http://switcher.local:{PORT}")
    print(f"   (IP 직접 접속도 가능: http://{local_ip}:{PORT})\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
