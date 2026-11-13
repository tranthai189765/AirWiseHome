# AirWise Home – Nhóm 6 HustStu (IT4735 IoT và Ứng dụng)

Hệ thống nhà thông minh tự động thông gió, lọc khí và cảnh báo an toàn dựa trên chất lượng không khí
trong và ngoài nhà.

Thành viên: Trần Quốc Thái (20230065), Phạm Bảo Yến (20230077), Đỗ Ngọc Hoàng Hải (20230026).

## Cấu trúc kho mã

| Thư mục | Nội dung |
|---|---|
| `firmware/room-node-wokwi/` | Firmware node phòng (ESP32), bản mô phỏng Wokwi: `sketch.ino`, `diagram.json`, `libraries.txt` |
| `hub/` | Phần mềm chạy trên hub: `docker-compose.yml`, `mosquitto/` (cấu hình broker), `hub_core/` (FastAPI, luật tự động, dashboard), `tools/dbview.py` |
| `docs/` | Tài liệu: đề xuất, thiết kế, báo cáo tiến độ |

## Chạy demo (Project Checkpoint 1)

1. Hub (cần Docker):

   ```bash
   cd hub
   docker compose up -d --build
   ```

   Dashboard: <http://localhost:8000>

   Chạy hub không cần build image (chỉ cần Python 3.10+ và broker Mosquitto ở cổng 1883):

   ```bash
   docker run -d --name airwise-mosquitto -p 1883:1883 \
     -v "$PWD/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2
   cd hub_core && pip install -r requirements.txt
   DB_PATH=../data/airwise.db python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```

2. Thiết bị: mở <https://wokwi.com/projects/new/esp32>, dán nội dung `sketch.ino` và `diagram.json`,
   thêm các thư viện trong `libraries.txt` bằng Library Manager, rồi bấm Start.
3. Kịch bản:
   - Xoay biến trở CO₂ (bên trái) quá khoảng nửa vòng: CO₂ ≥ 1.000 ppm. Sau 10 s hub tự bật quạt hút (LED xanh dương).
   - Xoay biến trở bụi (bên phải): PM2.5 > 35 µg/m³ thì hub bật máy lọc khí (LED xanh lá).
   - Bấm "Tắt" máy lọc khí trên dashboard: lệnh tay được ưu tiên, luật tự động tạm dừng với thiết bị đó.
   - Vặn hai biến trở về thấp: các thiết bị tự tắt (ngưỡng tắt thấp hơn ngưỡng bật).
4. Xem bản tin MQTT và dữ liệu:

   ```bash
   docker exec airwise-mosquitto mosquitto_sub -v -t 'airwise/n6h01/#'
   python3 tools/dbview.py data/airwise.db
   ```

Wokwi không truy cập được mạng LAN, nên bản demo dùng broker công cộng `broker.hivemq.com` và bridge về
Mosquitto của hub. Cách này chỉ dùng cho dữ liệu giả lập. Bản chạy với thiết bị thật dùng MQTT qua TLS (cổng 8883),
mỗi node một tài khoản và ACL riêng.

Nếu đổi mạng (Wi-Fi khác) mà log broker báo `Error creating bridge: Try again` (container còn giữ DNS cũ), tạo lại
container: `docker compose up -d --force-recreate mosquitto`.

## Chủ đề MQTT

`airwise/<mã nhà>/<mã thiết bị>/<loại>`, với loại là `telemetry`, `state`, `status` (Last Will), `meta`, `cmd`,
`cmd/ack`.

## Cấu hình hub (biến môi trường)

| Biến | Ý nghĩa | Mặc định |
|---|---|---|
| `MQTT_HOST`, `MQTT_PORT` | Broker | `localhost`, `1883` |
| `HOME_ID` | Mã nhà | `n6h01` |
| `DB_PATH` | Tệp SQLite | `data/airwise.db` |
| `CONFIRM_S` | Thời gian điều kiện phải duy trì trước khi luật kích hoạt (thiết kế 120 s) | `10` |
| `OVERRIDE_S` | Thời gian ưu tiên lệnh tay (thiết kế 1.800 s) | `120` |
| `ACK_TIMEOUT_S` | Hết thời gian chờ thiết bị xác nhận lệnh | `10` |
| `OUTDOOR_PM25` | PM2.5 ngoài trời giả lập khi chưa có node ban công | `18` |
