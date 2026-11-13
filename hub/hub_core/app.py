"""AirWise Home hub - bản Checkpoint 1.

Luồng: MQTT (Mosquitto trên hub) -> lưu SQLite -> trợ lý thông gió + luật tự động -> lệnh xuống
thiết bị qua MQTT -> thiết bị xác nhận (cmd/ack). Có REST API và dashboard web (FastAPI).
Chạy: uvicorn app:app --host 0.0.0.0 --port 8000
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
HOME = os.getenv("HOME_ID", "n6h01")
DB_PATH = os.getenv("DB_PATH", "data/airwise.db")
CONFIRM_S = int(os.getenv("CONFIRM_S", "10"))          # thiết kế: 120 s; bản demo: 10 s
OVERRIDE_S = int(os.getenv("OVERRIDE_S", "120"))       # thiết kế: 30 phút; bản demo: 2 phút
ACK_TIMEOUT_S = int(os.getenv("ACK_TIMEOUT_S", "10"))  # FR-08: chờ thiết bị xác nhận lệnh
OUTDOOR_PM25 = float(os.getenv("OUTDOOR_PM25", "18"))  # chưa có node ban công: giá trị giả lập
RULES = {"co2_on": 1000, "co2_off": 800, "pm_on": 35, "pm_off": 15}
NAMES = {"fan_exhaust": "quạt hút", "purifier": "máy lọc khí", "light": "đèn"}
STATIC = Path(__file__).parent / "static"

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
  id INTEGER PRIMARY KEY, device_id TEXT, ts TEXT, received_at TEXT, seq INTEGER,
  co2 INTEGER, pm25 INTEGER, aqi INTEGER, temp REAL, rh REAL, dew_point REAL,
  motion INTEGER, window_open INTEGER, reason TEXT, UNIQUE (device_id, seq, ts));
CREATE INDEX IF NOT EXISTS ix_telemetry ON telemetry (device_id, received_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT, device_id TEXT, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS commands (
  cmd_id TEXT PRIMARY KEY, ts TEXT, device_id TEXT, target TEXT, value TEXT,
  source TEXT, reason TEXT, status TEXT, ack_ts TEXT);
"""

lock = threading.RLock()
devices = {}          # device_id -> {"status", "last_seen", "telemetry", "actuators", "meta"}
pending = {}          # (device, target) -> (giá trị muốn đổi, thời điểm điều kiện bắt đầu)
override_until = {}   # (device, target) -> thời điểm hết ưu tiên lệnh tay (FR-09)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def log_event(device_id, kind, message):
    with db() as conn:
        conn.execute("INSERT INTO events (ts, device_id, kind, message) VALUES (?, ?, ?, ?)",
                     (now_iso(), device_id, kind, message))
    print(f"[{kind}] {device_id}: {message}", flush=True)


def topic(device_id, kind):
    return f"airwise/{HOME}/{device_id}/{kind}"


def send_command(device_id, target, value, source, reason):
    cmd_id = "c-" + uuid.uuid4().hex[:6]
    payload = {"cmd_id": cmd_id, "ts": now_iso(), "name": "set_actuator",
               "args": {"target": target, "value": value}, "source": source, "ttl_s": 30}
    with db() as conn:
        conn.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', NULL)",
                     (cmd_id, payload["ts"], device_id, target, value, source, reason))
    client.publish(topic(device_id, "cmd"), json.dumps(payload), qos=1)
    who = "Tự động" if source == "rule" else "Dashboard"
    log_event(device_id, "auto" if source == "rule" else "manual",
              f"{who}: {'bật' if value == 'on' else 'tắt'} {NAMES[target]} ({reason}) [{cmd_id}]")
    return cmd_id


def in_flight(device_id, target):
    with db() as conn:
        return conn.execute("SELECT 1 FROM commands WHERE device_id=? AND target=? AND status='sent'",
                            (device_id, target)).fetchone() is not None


def evaluate_rules(device_id, t):
    """Luật tự động theo Bảng 1 của đề xuất: có trễ (ngưỡng bật khác ngưỡng tắt) và thời gian xác nhận."""
    act = devices[device_id].get("actuators", {})
    co2, pm, window = t.get("co2", 0), t.get("pm25", 0), t.get("window_open", False)
    rules = [
        ("fan_exhaust", co2 >= RULES["co2_on"], co2 <= RULES["co2_off"],
         f"CO₂ {co2} ppm ≥ {RULES['co2_on']} ppm", f"CO₂ {co2} ppm ≤ {RULES['co2_off']} ppm"),
        ("purifier", pm > RULES["pm_on"] and not window, pm <= RULES["pm_off"],
         f"PM2.5 {pm} µg/m³ > {RULES['pm_on']} µg/m³, cửa sổ đóng", f"PM2.5 {pm} µg/m³ ≤ {RULES['pm_off']} µg/m³"),
    ]
    now = time.time()
    for target, on_cond, off_cond, on_reason, off_reason in rules:
        key = (device_id, target)
        is_on = act.get(target) == "on"
        want = "on" if (not is_on and on_cond) else "off" if (is_on and off_cond) else None
        if want is None:
            pending.pop(key, None)
            continue
        if pending.get(key, (None,))[0] != want:
            pending[key] = (want, now)            # điều kiện vừa xuất hiện: bắt đầu đếm thời gian
            continue
        held = now - pending[key][1]
        if held < CONFIRM_S or override_until.get(key, 0) > now or in_flight(device_id, target):
            continue
        reason = (on_reason if want == "on" else off_reason) + f", duy trì {held:.0f} s"
        send_command(device_id, target, want, "rule", reason)
        pending.pop(key, None)


def advise(t):
    """Trợ lý thông gió (FR-06): khuyến nghị kèm lý do dễ hiểu."""
    tips, out = [], OUTDOOR_PM25
    co2, pm = t.get("co2", 0), t.get("pm25", 0)
    if co2 >= RULES["co2_on"]:
        if out <= 35:
            tips.append(f"CO₂ trong phòng {co2} ppm, PM2.5 ngoài trời {out:.0f} µg/m³ → nên mở cửa sổ "
                        "hoặc bật quạt hút")
        else:
            tips.append(f"CO₂ {co2} ppm nhưng ngoài trời ô nhiễm (PM2.5 {out:.0f} µg/m³) → giữ cửa đóng, "
                        "thông gió ngắn bằng quạt hút và bật lọc khí")
    if pm > RULES["pm_on"] and pm > out:
        if t.get("window_open"):
            tips.append(f"PM2.5 trong nhà {pm} µg/m³ cao hơn ngoài trời → mở cửa sổ để thông gió")
        else:
            tips.append(f"PM2.5 trong nhà {pm} µg/m³ > 35 µg/m³ khi cửa sổ đóng → bật máy lọc khí")
    elif pm > RULES["pm_on"] and t.get("window_open"):
        tips.append(f"Ngoài trời ô nhiễm, PM2.5 trong nhà {pm} µg/m³ → đóng cửa sổ, bật lọc khí")
    if (t.get("rh") or 0) > 70:
        tips.append(f"Độ ẩm {t['rh']} % > 70 % → nên bật hút ẩm")
    return tips or ["Không khí trong phòng đang tốt, chưa cần thông gió"]


def on_connect(cl, userdata, flags, rc, props=None):
    cl.subscribe(f"airwise/{HOME}/#", qos=1)
    print(f"hub connected to MQTT {MQTT_HOST}:{MQTT_PORT}, subscribed airwise/{HOME}/#", flush=True)


def on_message(cl, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) < 4 or parts[2] == "+":
        return
    device_id, kind = parts[2], "/".join(parts[3:])
    text = msg.payload.decode("utf-8", "replace")
    with lock:
        dev = devices.setdefault(device_id, {"status": "unknown", "actuators": {}})
        dev["last_seen"] = now_iso()
        if kind == "status":
            if dev["status"] != text:
                log_event(device_id, "status", "Thiết bị " + ("trực tuyến" if text == "online" else "MẤT KẾT NỐI"))
            dev["status"] = text
            return
        if kind == "cmd":
            return                                   # lệnh do chính hub gửi
        try:
            data = json.loads(text)
        except ValueError:
            log_event(device_id, "warn", f"Bản tin sai định dạng trên {kind}")
            return
        if kind == "telemetry":
            dev["telemetry"], dev["status"] = data, "online"
            with db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO telemetry (device_id, ts, received_at, seq, co2, pm25, aqi, temp, rh,"
                    " dew_point, motion, window_open, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id, data.get("ts"), now_iso(), data.get("seq"), data.get("co2"), data.get("pm25"),
                     data.get("aqi"), data.get("temp"), data.get("rh"), data.get("dew_point"),
                     int(bool(data.get("motion"))), int(bool(data.get("window_open"))), data.get("reason")))
            evaluate_rules(device_id, data)
        elif kind == "state":
            old = dev.get("actuators", {})
            dev["actuators"] = data.get("actuators", {})
            changed = [f"{NAMES.get(k, k)} {v.upper()}" for k, v in dev["actuators"].items() if old.get(k) != v]
            if changed and old:
                log_event(device_id, "state", "Thiết bị báo trạng thái: " + ", ".join(changed))
        elif kind == "meta":
            dev["meta"] = data
        elif kind == "cmd/ack":
            with db() as conn:
                row = conn.execute("SELECT ts, target FROM commands WHERE cmd_id=?", (data.get("cmd_id"),)).fetchone()
                if row is None:
                    return
                conn.execute("UPDATE commands SET status=?, ack_ts=? WHERE cmd_id=?",
                             (data.get("result"), now_iso(), data.get("cmd_id")))
            sent = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            delay = (datetime.now(timezone.utc) - sent).total_seconds()
            log_event(device_id, "ack", f"Thiết bị xác nhận {data.get('cmd_id')}: {json.dumps(data.get('applied'))} "
                                        f"({data.get('result')}, sau {delay:.1f} s)")


def ack_watchdog():
    """FR-08: lệnh không được xác nhận trong 10 s thì báo lỗi trên giao diện."""
    while True:
        time.sleep(2)
        limit = datetime.now(timezone.utc).timestamp() - ACK_TIMEOUT_S
        with db() as conn:
            rows = conn.execute("SELECT cmd_id, device_id, ts FROM commands WHERE status='sent'").fetchall()
            for r in rows:
                if datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp() < limit:
                    conn.execute("UPDATE commands SET status='timeout' WHERE cmd_id=?", (r["cmd_id"],))
                    log_event(r["device_id"], "warn", f"Lệnh {r['cmd_id']} không được thiết bị xác nhận")


Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
with db() as _conn:
    _conn.executescript(SCHEMA)
    _conn.execute("PRAGMA journal_mode=WAL")
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"airwise-hub-{HOME}")
client.on_connect = on_connect
client.on_message = on_message
client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
client.loop_start()
threading.Thread(target=ack_watchdog, daemon=True).start()

app = FastAPI(title="AirWise Home hub", version="0.3.0")


class Command(BaseModel):
    target: str
    value: str


@app.get("/api/overview")
def overview():
    with lock:
        result = []
        for dev_id, dev in devices.items():
            t = dev.get("telemetry") or {}
            result.append({"device_id": dev_id, "status": dev.get("status"), "last_seen": dev.get("last_seen"),
                           "telemetry": t, "actuators": dev.get("actuators", {}), "meta": dev.get("meta"),
                           "advice": advise(t) if t else [],
                           "override": {k[1]: round(v - time.time()) for k, v in override_until.items()
                                        if k[0] == dev_id and v > time.time()}})
    return {"home": HOME, "devices": result, "outdoor": {"pm25": OUTDOOR_PM25, "source": "giả lập"},
            "rules": RULES, "confirm_s": CONFIRM_S}


@app.get("/api/history")
def history(device_id: str = "aw-room-01", minutes: int = 10):
    since = datetime.now(timezone.utc).timestamp() - minutes * 60
    since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat().replace("+00:00", "Z")
    with db() as conn:
        rows = conn.execute("SELECT received_at, co2, pm25, temp, rh FROM telemetry WHERE device_id=? "
                            "AND received_at >= ? ORDER BY received_at", (device_id, since_iso)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/events")
def events(limit: int = 20):
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


@app.post("/api/devices/{device_id}/actuators")
def command(device_id: str, cmd: Command):
    if cmd.target not in NAMES or cmd.value not in ("on", "off"):
        raise HTTPException(400, "target hoặc value không hợp lệ")
    override_until[(device_id, cmd.target)] = time.time() + OVERRIDE_S    # FR-09: ưu tiên lệnh tay
    cmd_id = send_command(device_id, cmd.target, cmd.value, "dashboard",
                          f"ưu tiên lệnh tay {OVERRIDE_S // 60} phút")
    return {"cmd_id": cmd_id}


@app.get("/api/commands/{cmd_id}")
def command_status(cmd_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM commands WHERE cmd_id=?", (cmd_id,)).fetchone()
    if row is None:
        raise HTTPException(404)
    return dict(row)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
