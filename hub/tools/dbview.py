"""Xem nhanh dữ liệu trong CSDL của hub: python3 tools/dbview.py [data/airwise.db] [số dòng]"""
import sqlite3
import sys
from datetime import datetime

db = sys.argv[1] if len(sys.argv) > 1 else "data/airwise.db"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
conn = sqlite3.connect(db)
print(f"Bảng telemetry: {conn.execute('SELECT COUNT(*) FROM telemetry').fetchone()[0]} bản ghi; {n} bản ghi mới nhất:")
print(f"{'nhận lúc (UTC)':<24}{'seq':>5}{'co2':>6}{'pm25':>6}{'aqi':>5}{'temp':>6}{'rh':>6}{'cửa':>5}")
for r in conn.execute("SELECT received_at, seq, co2, pm25, aqi, temp, rh, window_open FROM telemetry "
                      "ORDER BY id DESC LIMIT ?", (n,)).fetchall()[::-1]:
    print(f"{r[0][:23]:<24}{r[1]:>5}{r[2]:>6}{r[3]:>6}{r[4]:>5}{r[5]:>6}{r[6]:>6}{r[7]:>5}")
print("\nBảng commands (lệnh gần nhất):")
iso = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
for r in conn.execute("SELECT cmd_id, ts, target, value, source, status, ack_ts FROM commands "
                      "ORDER BY ts DESC LIMIT 6").fetchall()[::-1]:
    lat = f"xác nhận sau {(iso(r[6]) - iso(r[1])).total_seconds():.2f} s" if r[6] else ""
    print(f"  {r[1][11:19]}  {r[0]}  {r[2]:<12}{r[3]:<4} nguồn={r[4]:<9} trạng thái={r[5]:<8} {lat}")
