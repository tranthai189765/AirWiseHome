// AirWise Room Node - bản mô phỏng trên Wokwi (Project Checkpoint 1, Nhóm 6 - HustStu)
// Thiết bị thật: PMS7003 (bụi), MH-Z19C (CO2), SHT31 (nhiệt độ, độ ẩm), HC-SR501 (PIR), MC-38 (cửa sổ).
// Trên Wokwi: DHT22 thay SHT31; 2 biến trở trượt thay MH-Z19C và PMS7003 (Wokwi chưa có 2 cảm biến
// này); công tắc gạt thay công tắc từ cửa sổ; 3 LED thay quạt hút, máy lọc khí và đèn.
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHTesp.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <time.h>

// ---- cấu hình (bản thật đọc từ bộ nhớ, không viết cứng) ----
const char *WIFI_SSID = "Wokwi-GUEST";
const char *MQTT_HOST = "broker.hivemq.com";  // demo: broker trung gian, bridge về Mosquitto của hub
const int MQTT_PORT = 1883;
const char *BASE = "airwise/n6h01/aw-room-01/";  // airwise/<mã nhà>/<mã thiết bị>/<loại>
const char *DEVICE_ID = "aw-room-01";

// ---- chân GPIO ----
const int DHT_PIN = 15, CO2_PIN = 34, PM_PIN = 35;      // 34, 35: ADC1, dùng được khi bật Wi-Fi
const int PIR_PIN = 27, WINDOW_PIN = 26;                // tránh GPIO12 (chân strapping)
const int FAN_PIN = 19, PURIFIER_PIN = 18, LIGHT_PIN = 17;  // LED thay quạt hút, lọc khí, đèn

DHTesp dht;
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
WiFiClient net;
PubSubClient mqtt(net);

unsigned long intervalMs = 5000;   // thiết kế: 30 s; demo: 5 s, đổi được bằng lệnh set_interval
unsigned long lastSend = 0, lastState = 0;
uint32_t seq = 0;
bool fan = false, purifier = false, light = false;
int lastMotion = -1, lastWindow = -1;
float co2 = 400, pm25 = 0, temp = NAN, rh = NAN;

String topic(const char *type) { return String(BASE) + type; }

String isoNow() {                 // dấu thời gian UTC gán tại thiết bị (NTP)
  time_t now = time(nullptr);
  if (now < 1700000000) return "";
  char buf[25];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", gmtime(&now));
  return buf;
}

int aqiPm25(float c) {            // VN_AQI cho PM2.5 (nội suy tuyến tính giữa các điểm gãy)
  const float bp[] = {0, 25, 50, 80, 150, 250, 350, 500};
  const int ix[] = {0, 50, 100, 150, 200, 300, 400, 500};
  for (int i = 1; i < 8; i++)
    if (c <= bp[i]) return round(ix[i - 1] + (ix[i] - ix[i - 1]) * (c - bp[i - 1]) / (bp[i] - bp[i - 1]));
  return 500;
}

const char *co2Level(float c) { return c < 800 ? "tot" : c < 1000 ? "trung_binh" : c < 1500 ? "kem" : "xau"; }

float dewPoint(float t, float h) {  // công thức Magnus
  const float a = 17.62, b = 243.12;
  float g = log(h / 100.0) + a * t / (b + t);
  return b * g / (a - g);
}

void readSensors() {
  co2 = map(analogRead(CO2_PIN), 0, 4095, 400, 2000);   // biến trở thay MH-Z19C: 400-2000 ppm
  pm25 = map(analogRead(PM_PIN), 0, 4095, 0, 150);      // biến trở thay PMS7003: 0-150 µg/m³
  TempAndHumidity d = dht.getTempAndHumidity();
  temp = d.temperature;
  rh = d.humidity;
}

void publishTelemetry(const char *reason) {
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["ts"] = isoNow();
  doc["seq"] = ++seq;
  doc["co2"] = (int)co2;
  doc["pm25"] = (int)pm25;
  doc["aqi"] = aqiPm25(pm25);
  doc["co2_level"] = co2Level(co2);
  if (!isnan(temp)) {
    doc["temp"] = round(temp * 10) / 10.0;
    doc["rh"] = round(rh * 10) / 10.0;
    doc["dew_point"] = round(dewPoint(temp, rh) * 10) / 10.0;
  }
  doc["motion"] = lastMotion == 1;
  doc["window_open"] = lastWindow == 1;
  doc["reason"] = reason;          // "period" hoặc sự kiện (motion, window)
  char buf[384];
  serializeJson(doc, buf);
  mqtt.publish(topic("telemetry").c_str(), buf);
  Serial.printf("[pub] telemetry %s\n", buf);
}

void publishState() {
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["ts"] = isoNow();
  doc["actuators"]["fan_exhaust"] = fan ? "on" : "off";
  doc["actuators"]["purifier"] = purifier ? "on" : "off";
  doc["actuators"]["light"] = light ? "on" : "off";
  doc["report_interval_s"] = intervalMs / 1000;
  doc["rssi_dbm"] = WiFi.RSSI();
  doc["uptime_s"] = millis() / 1000;
  char buf[320];
  serializeJson(doc, buf);
  mqtt.publish(topic("state").c_str(), buf, true);
}

void applyActuators() {
  digitalWrite(FAN_PIN, fan);
  digitalWrite(PURIFIER_PIN, purifier);
  digitalWrite(LIGHT_PIN, light);
}

// Lệnh từ hub: set_actuator {target, value}, set_interval {seconds}; trả kết quả trên .../cmd/ack
void onMessage(char *t, byte *payload, unsigned int len) {
  JsonDocument cmd, ack;
  if (deserializeJson(cmd, payload, len)) return;
  const char *name = cmd["name"] | "";
  ack["cmd_id"] = cmd["cmd_id"] | "";
  ack["result"] = "ok";
  if (!strcmp(name, "set_actuator")) {
    String target = cmd["args"]["target"] | "";
    bool on = String(cmd["args"]["value"] | "off") == "on";
    if (target == "fan_exhaust") fan = on;
    else if (target == "purifier") purifier = on;
    else if (target == "light") light = on;
    else ack["result"] = "unknown_target";
    applyActuators();                                      // thực thi xong mới xác nhận
    ack["applied"][target] = on ? "on" : "off";
  } else if (!strcmp(name, "set_interval")) {
    intervalMs = constrain((int)(cmd["args"]["seconds"] | 30), 2, 300) * 1000UL;
    ack["applied"]["report_interval_s"] = intervalMs / 1000;
  } else {
    ack["result"] = "unknown_command";
  }
  ack["ts"] = isoNow();
  char buf[200];
  serializeJson(ack, buf);
  mqtt.publish(topic("cmd/ack").c_str(), buf);
  Serial.printf("[cmd] %s -> %s\n", name, buf);
  publishState();
}

void connectMqtt() {
  String id = String("aw-room-01-") + String((uint32_t)ESP.getEfuseMac(), HEX);
  // Last Will: broker tự công bố "offline" nếu thiết bị mất kết nối đột ngột
  if (mqtt.connect(id.c_str(), topic("status").c_str(), 1, true, "offline")) {
    mqtt.publish(topic("status").c_str(), "online", true);
    mqtt.publish(topic("meta").c_str(),
                 "{\"device_id\":\"aw-room-01\",\"model\":\"AirWise Room Node (Wokwi)\","
                 "\"fw_version\":\"0.3.0\",\"location\":{\"home\":\"n6h01\",\"room\":\"phong-ngu\"}}",
                 true);
    mqtt.subscribe(topic("cmd").c_str(), 1);
    publishState();
    Serial.println("MQTT connected");
  } else {
    Serial.printf("MQTT connect failed, state=%d\n", mqtt.state());
  }
}

void showOled() {
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);
  oled.printf("AirWise  %s\n", mqtt.connected() ? "MQTT OK" : "offline");
  oled.printf("CO2  %4d ppm %s\n", (int)co2, co2 >= 1000 ? "!" : "");
  oled.printf("PM2.5 %3d AQI %3d\n", (int)pm25, aqiPm25(pm25));
  if (!isnan(temp)) oled.printf("T %.1fC  RH %.0f%%\n", temp, rh);
  oled.printf("Cua so: %s\n", lastWindow == 1 ? "MO" : "DONG");
  oled.printf("Quat %s Loc %s\n", fan ? "ON " : "OFF", purifier ? "ON" : "OFF");
  oled.printf("Den %s  PIR %s", light ? "ON " : "OFF", lastMotion == 1 ? "YES" : "NO");
  oled.display();
}

void setup() {
  Serial.begin(115200);
  dht.setup(DHT_PIN, DHTesp::DHT22);
  pinMode(PIR_PIN, INPUT);
  pinMode(WINDOW_PIN, INPUT);
  pinMode(FAN_PIN, OUTPUT);
  pinMode(PURIFIER_PIN, OUTPUT);
  pinMode(LIGHT_PIN, OUTPUT);
  oled.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  Serial.print("Connecting to WiFi");
  WiFi.begin(WIFI_SSID, "", 6);
  for (int i = 1; WiFi.status() != WL_CONNECTED; i++) {
    delay(250);
    Serial.print(".");
    if (i % 40 == 0) {                         // 10 s chưa vào được mạng: thử kết nối lại
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, "", 6);
    }
  }
  Serial.println(" WiFi connected");
  configTime(0, 0, "pool.ntp.org");
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(60);                       // mô phỏng có lúc chạy chậm hơn thời gian thực
  mqtt.setCallback(onMessage);
}

unsigned long lastTry = 0;

void loop() {
  if (!mqtt.connected() && (lastTry == 0 || millis() - lastTry >= 3000)) {   // thử lại sau 3 s
    lastTry = millis();
    connectMqtt();
  }
  mqtt.loop();
  int m = digitalRead(PIR_PIN), w = digitalRead(WINDOW_PIN);
  if (m != lastMotion || w != lastWindow) {    // sự kiện chuyển động, cửa sổ: gửi ngay
    const char *why = (lastMotion < 0) ? "boot" : (m != lastMotion ? "motion" : "window");
    lastMotion = m;
    lastWindow = w;
    readSensors();
    if (mqtt.connected()) publishTelemetry(why);
  }
  if (millis() - lastSend >= intervalMs) {     // gửi định kỳ, không dùng delay()
    lastSend = millis();
    readSensors();
    if (mqtt.connected()) publishTelemetry("period");
    showOled();
  }
  if (millis() - lastState >= 60000) {
    lastState = millis();
    if (mqtt.connected()) publishState();
  }
}
