/*
 * ===========================================================================
 *  触觉社交手环 · ESP32 固件  【M1 · 七个输出版】
 *  文件：firmware/hapticband/hapticband.ino
 *  状态：M1 已完成 2026-11-05（按《方案定稿》七个输出重定）
 *        M2 软件流水线 已完成（硬件未到也能跑）｜ M3 联调 待硬件到货
 *  说明：真机 M1 固件 · 七个输出版；接线见同目录《烧录与接线步骤.md》
 * ===========================================================================
 *
 *  【一期七个输出（定死）】
 *    vib1 振动一下 / vib2 振动两下 / vib3 振动三下
 *    strap1 收紧 1 档(35 度) / strap2 收紧 2 档(70 度)
 *    cool 凉 / warm 热        —— TEC 靠两个继电器换向，电流方向决定冷热
 *
 *  【协议（重定）】
 *    订阅 wrist/cmd : {"act":"vib2"}      act 只能是上面七个 ID
 *    发布 wrist/bio : {"ch":"bio","hr":92}   每 2 秒，无效 -1（保持原样）
 *
 *  【要装的库：Arduino IDE「工具 → 管理库」搜名字安装，共 3 个】
 *    1. PubSubClient                                          by Nick O'Leary
 *    2. ESP32Servo                                           by Kevin Harrington
 *    3. SparkFun MAX3010x Pulse and Proximity Sensor Library  by SparkFun Electronics
 *    WiFi.h / Wire.h / string.h / ctype.h 是系统自带，不用装。
 *
 *  【接线】
 *    GPIO25 → 震动马达模块 IN
 *    GPIO26 → 继电器 A 的 IN（凉）    GPIO14 → 继电器 B 的 IN（热）
 *      两个继电器的 COM 各接 TEC 一端；NO 全部接外部 5V+；NC 全部接 GND
 *    GPIO27 → SG90 舵机信号线
 *    GPIO21 → MAX30102 SDA          GPIO22 → MAX30102 SCL
 *    舵机 / TEC / 继电器用外部 5V；外部 GND 必须与 ESP32 的 GND 连一根线
 *    逐根线的插法见同目录《烧录与接线步骤.md》
 *
 *  【第一次用只改第 1 区两行 WiFi；不改也能用串口演示模式】
 * ===========================================================================
 */

#include <WiFi.h>
#include <Wire.h>
#include <string.h>
#include <ctype.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include "MAX30105.h"

// ===== 第 1 区：唯一需要你改的两行 =======================================
#define WIFI_SSID   "改成你的WiFi名字"     // 例："CMCC-2.4G"；ESP32 只认 2.4GHz
#define WIFI_PASS   "改成你的WiFi密码"

#define MQTT_BROKER     "broker.emqx.io"
#define MQTT_PORT       1883
#define MQTT_TOPIC_CMD  "wrist/cmd"
#define MQTT_TOPIC_BIO  "wrist/bio"
// ========================================================================

// ===== 第 2 区：引脚与安全参数 ============================================
#define PIN_VIB       25
#define PIN_RELAY_A   26      // 吸合 = TEC 端1 接 5V → 凉
#define PIN_RELAY_B   14      // 吸合 = TEC 端2 接 5V → 热
#define PIN_SERVO     27
#define PIN_SDA       21
#define PIN_SCL       22

#define RELAY_ACTIVE_LOW 1   // 1=低电平触发（多数继电器）；高电平触发模块改成 0
#define RELAY_OPEN_DRAIN 0   // 若继电器「关不掉」，改成 1 再上传一次

#define VIB_DUR_MS       120      // 每下震动时长
#define STRAP1_ANGLE     35       // 收紧 1 档
#define STRAP2_ANGLE     70       // 收紧 2 档
#define TEC_PULSE_MS     5000UL   // 凉/热是脉冲提示：通电 5 秒自动断
#define TEC_MAX_ON_MS    30000UL  // 绝对硬上限 30 秒（二次保险，不要调大）
#define TEC_REFRACTORY_MS 2000UL  // 同方向 2 秒内重复触发忽略
#define BIO_INTERVAL_MS  2000UL   // 心率上报间隔
#define HR_SAMPLE_MS     20UL

#if RELAY_ACTIVE_LOW
  #define RELAY_ON  LOW
  #define RELAY_OFF HIGH
#else
  #define RELAY_ON  HIGH
  #define RELAY_OFF LOW
#endif
// ========================================================================

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);
Servo        strapServo;
MAX30105     particleSensor;

char mqttClientId[32];
bool wifiOK = false;
unsigned long lastMqttRetryMs = 0;
unsigned long lastBioMs = 0;
int  lastBioHr = -999;

// 震动：非阻塞状态机
bool  vibActive = false;
int   vibLeft = 0;
bool  vibOn = false;
unsigned long vibPhaseEndMs = 0;

// TEC：方向 -1=凉 0=关 +1=热
int  tecDir = 0;
int  tecLastDir = 0;
unsigned long tecOnSinceMs = 0;
unsigned long tecLastTriggerMs = 0;

// 舵机当前角度（给 status 看）
int  strapAngle = 0;

// demo 串烧
int  demoStep = 0;
unsigned long demoNextMs = 0;

// 心率
bool hrSensorOK = false;
int  currentHR = -1;

// 演示级 BPM 估计：IR 动态阈值峰值检测，非医疗精度，无信号输出 -1
struct BeatDet {
  bool  inited;
  long  irMin, irMax;
  unsigned long rangeResetMs;
  bool  aboveThresh;
  unsigned long lastBeatMs;
  unsigned long lastValidMs;
  float bpmAcc[4];
  int   bpmCount;
  int   bpmIdx;
} beat;

// ---------------------------------------------------------------------------
// JSON 小工具：只取固定字段，不引入 ArduinoJson，少装一个库
// ---------------------------------------------------------------------------
int findColonAfterKey(const String &s, const char *key) {
  String k = String("\"") + key + "\"";
  int p = s.indexOf(k);
  if (p < 0) return -1;
  return s.indexOf(':', p + k.length());
}

bool jsonGetStr(const String &s, const char *key, char *out, size_t cap) {
  int c = findColonAfterKey(s, key);
  if (c < 0) return false;
  int i = c + 1;
  while (i < (int)s.length() && (s[i] == ' ' || s[i] == '\t')) i++;
  if (i >= (int)s.length() || s[i] != '"') return false;
  i++;
  size_t n = 0;
  while (i < (int)s.length() && s[i] != '"' && n + 1 < cap) { out[n++] = s[i]; i++; }
  out[n] = '\0';
  return n > 0;
}

// ---------------------------------------------------------------------------
// 七个输出的执行器
// ---------------------------------------------------------------------------
void doVibe(int n) {
  if (n < 1) n = 1;
  if (n > 3) n = 3;                       // 一期只有 1/2/3 下
  vibLeft = n;
  vibOn = true;
  vibActive = true;
  digitalWrite(PIN_VIB, HIGH);
  vibPhaseEndMs = millis() + VIB_DUR_MS;
  Serial.printf("[动作] 振动 %d 下\n", n);
}

void updateVibe() {
  if (!vibActive || millis() < vibPhaseEndMs) return;
  unsigned long now = millis();
  if (vibOn) {
    digitalWrite(PIN_VIB, LOW);
    vibOn = false;
    vibLeft--;
    if (vibLeft <= 0) { vibActive = false; return; }
    vibPhaseEndMs = now + VIB_DUR_MS;
  } else {
    digitalWrite(PIN_VIB, HIGH);
    vibOn = true;
    vibPhaseEndMs = now + VIB_DUR_MS;
  }
}

void doStrap(int angle) {
  if (angle < 0) angle = 0;
  if (angle > 90) angle = 90;
  strapAngle = angle;
  strapServo.write(angle);
  Serial.printf("[动作] 表带收紧 -> 舵机 %d 度\n", angle);
}

void relayWrite(int pin, bool on) {
#if RELAY_ACTIVE_LOW
  if (on) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  } else if (RELAY_OPEN_DRAIN) {
    pinMode(pin, INPUT);      // 高阻：治 3.3V 驱动 5V 继电器关不掉
  } else {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, HIGH);
  }
#else
  pinMode(pin, OUTPUT);
  digitalWrite(pin, on ? HIGH : LOW);
#endif
}

// dir: -1 凉 / 0 关 / +1 热。先写全关再写目标，机械换向天然错开，无短路风险
void tecDrive(int dir) {
  relayWrite(PIN_RELAY_A, dir < 0);
  relayWrite(PIN_RELAY_B, dir > 0);
  tecDir = dir;
}

void doTec(int dir) {
  unsigned long now = millis();
  if (dir != 0 && dir == tecLastDir && tecDir != 0 &&
      now - tecLastTriggerMs < TEC_REFRACTORY_MS) {
    Serial.println("[安全] 同一温度动作 2 秒内重复，已忽略");
    return;
  }
  tecDrive(0);
  if (dir != 0) {
    tecDrive(dir);
    tecOnSinceMs = now;
    if (dir < 0) Serial.println("[动作] 凉：TEC 制冷方向通电 5 秒");
    else         Serial.println("[动作] 热：TEC 制热方向通电 5 秒");
  } else {
    Serial.println("[动作] 温度通道关闭");
  }
  tecLastDir = dir;
  tecLastTriggerMs = now;
}

void updateTec() {
  if (tecDir == 0) return;
  unsigned long on = millis() - tecOnSinceMs;
  if (on >= TEC_PULSE_MS) {
    tecDrive(0);
    Serial.println("[安全] 温度脉冲 5 秒到，自动断电");
  } else if (on >= TEC_MAX_ON_MS) {
    tecDrive(0);
    Serial.println("[安全] 触发 30 秒硬上限，已强制断电");
  }
}

// ---------------------------------------------------------------------------
// 指令分发：只认七个 act ID
// ---------------------------------------------------------------------------
void handleCommand(const char *jsonText) {
  String s(jsonText);
  char buf[16] = "";

  char chbuf[12] = "";
  if (jsonGetStr(s, "ch", chbuf, sizeof(chbuf)) && strcmp(chbuf, "bio") == 0) return;

  if (!jsonGetStr(s, "act", buf, sizeof(buf))) {
    Serial.println("[错误] 指令里没有 act 字段，已忽略");
    return;
  }
  // 输出互斥：振动 / 收紧类动作下发时先关掉温度通道，避免上一条 cool / warm 的
  // 5 秒脉冲残留，与调试台「最近一次触发」的卡片保持一致（与仿真固件同一份逻辑）
  if (strncmp(buf, "vib", 3) == 0 || strncmp(buf, "strap", 5) == 0) tecDrive(0);
  if      (strcmp(buf, "vib1")   == 0) doVibe(1);
  else if (strcmp(buf, "vib2")   == 0) doVibe(2);
  else if (strcmp(buf, "vib3")   == 0) doVibe(3);
  else if (strcmp(buf, "strap1") == 0) doStrap(STRAP1_ANGLE);
  else if (strcmp(buf, "strap2") == 0) doStrap(STRAP2_ANGLE);
  else if (strcmp(buf, "cool")   == 0) doTec(-1);
  else if (strcmp(buf, "warm")   == 0) doTec(1);
  else if (strcmp(buf, "bio")    == 0) { /* 上报回声，忽略 */ }
  else Serial.printf("[错误] 不认识的动作 act=%s，已忽略\n", buf);
}

// ---------------------------------------------------------------------------
// 心率（不依赖库里的 heartRate.h，单文件即可编译）
// ---------------------------------------------------------------------------
void feedIR(long ir) {
  unsigned long now = millis();
  if (!beat.inited) {
    beat.inited = true;
    beat.irMin = ir; beat.irMax = ir;
    beat.rangeResetMs = now;
    return;
  }
  if (now - beat.rangeResetMs > 3000UL) {
    beat.irMin = ir; beat.irMax = ir;
    beat.rangeResetMs = now;
  }
  if (ir < beat.irMin) beat.irMin = ir;
  if (ir > beat.irMax) beat.irMax = ir;

  long span = beat.irMax - beat.irMin;
  if (span < 500) {
    if (now - beat.lastValidMs > 3000UL) currentHR = -1;
    beat.aboveThresh = false;
    return;
  }
  long thresh = beat.irMin + span * 6 / 10;
  bool above = (ir > thresh);
  if (above && !beat.aboveThresh) {
    unsigned long dt = now - beat.lastBeatMs;
    if (beat.lastBeatMs > 0 && dt >= 333UL && dt <= 1500UL) {
      float bpm = 60000.0f / (float)dt;
      beat.bpmAcc[beat.bpmIdx] = bpm;
      beat.bpmIdx = (beat.bpmIdx + 1) % 4;
      if (beat.bpmCount < 4) beat.bpmCount++;
      float sum = 0.0f;
      for (int i = 0; i < beat.bpmCount; i++) sum += beat.bpmAcc[i];
      currentHR = (int)(sum / (float)beat.bpmCount + 0.5f);
      beat.lastValidMs = now;
    }
    beat.lastBeatMs = now;
  }
  beat.aboveThresh = above;
  if (now - beat.lastValidMs > 5000UL) currentHR = -1;
}

void updateHeartRate() {
  if (!hrSensorOK) { currentHR = -1; return; }
  static unsigned long lastSampleMs = 0;
  if (millis() - lastSampleMs < HR_SAMPLE_MS) return;
  lastSampleMs = millis();
  long ir = (long)particleSensor.getIR();
  feedIR(ir);
}

void setupHeartRate() {
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  if (!particleSensor.begin(Wire, 100000)) {
    hrSensorOK = false;
    Serial.println("[心率] 没检测到 MAX30102 —— 上报 -1，另外三个通道不受影响");
    return;
  }
  particleSensor.setup();
  particleSensor.setPulseAmplitudeRed(0x0B);
  particleSensor.setPulseAmplitudeIR(0x1F);
  hrSensorOK = true;
  Serial.println("[心率] MAX30102 就绪（手指放上去 3-5 秒出数）");
}

void updateBio() {
  if (millis() - lastBioMs < BIO_INTERVAL_MS) return;
  lastBioMs = millis();
  char payload[64];
  snprintf(payload, sizeof(payload), "{\"ch\":\"bio\",\"hr\":%d}", currentHR);
  if (mqtt.connected()) {
    mqtt.publish(MQTT_TOPIC_BIO, payload);
    if (currentHR != lastBioHr) {
      Serial.printf("[上报] %s -> %s\n", MQTT_TOPIC_BIO, payload);
      lastBioHr = currentHR;
    }
  } else {
    Serial.printf("[上报] (MQTT 未连，仅串口) %s\n", payload);
  }
}

// ---------------------------------------------------------------------------
// WiFi / MQTT：连不上也绝不卡死
// ---------------------------------------------------------------------------
void setupWifi() {
  if (strcmp(WIFI_SSID, "改成你的WiFi名字") == 0 || strlen(WIFI_SSID) == 0) {
    Serial.println("[WiFi] 还没填 WiFi 名字 —— 跳过联网，直接用串口演示模式");
    return;
  }
  Serial.printf("[WiFi] 正在连接 %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 12000UL) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    wifiOK = true;
    Serial.print("[WiFi] 连上了，IP: ");
    Serial.println(WiFi.localIP().toString());
  } else {
    wifiOK = false;
    Serial.println("[WiFi] 12 秒没连上（密码错？只支持 2.4GHz？）—— 不影响串口演示模式");
  }
}

void onMqttMessage(char *topic, byte *payload, unsigned int length) {
  char buf[257];
  unsigned int n = length;
  if (n > 256) n = 256;
  memcpy(buf, payload, n);
  buf[n] = '\0';
  Serial.print("[MQTT] 收到指令: ");
  Serial.println(buf);
  handleCommand(buf);
}

void updateMqtt() {
  if (!wifiOK) return;
  if (mqtt.connected()) { mqtt.loop(); return; }
  if (lastMqttRetryMs != 0 && millis() - lastMqttRetryMs < 5000UL) return;
  lastMqttRetryMs = millis();
  Serial.print("[MQTT] 连接 ");
  Serial.print(MQTT_BROKER);
  Serial.print(" ... ");
  if (mqtt.connect(mqttClientId)) {
    Serial.println("成功");
    mqtt.subscribe(MQTT_TOPIC_CMD);
    Serial.printf("[MQTT] 已订阅 %s\n", MQTT_TOPIC_CMD);
  } else {
    Serial.printf("失败(错误码 %d)，5 秒后重试\n", mqtt.state());
  }
}

// ---------------------------------------------------------------------------
// 串口演示模式：不依赖 WiFi / MQTT / broker
// ---------------------------------------------------------------------------
void printHelp() {
  Serial.println("-------- 七个输出（输完按回车）--------");
  Serial.println("  vib1 / vib2 / vib3    振动 1 / 2 / 3 下");
  Serial.println("  strap1 / strap2       收紧 1 档(35度) / 2 档(70度)");
  Serial.println("  cool / warm           凉 5 秒 / 热 5 秒（自动断电）");
  Serial.println("-------- 辅助命令 --------");
  Serial.println("  demo      七个输出依次演一遍（拍视频用）");
  Serial.println("  strap0    松开表带（维护用，不属于七个输出）");
  Serial.println("  off       关闭温度（维护用，不属于七个输出）");
  Serial.println("  status    查看 WiFi / MQTT / 心率 / 温度状态");
  Serial.println("  help      显示本帮助");
  Serial.println("  也可直接粘贴 JSON：{\"act\":\"cool\"}");
  Serial.println("--------------------------------------");
}

void printStatus() {
  String ip = WiFi.localIP().toString();
  Serial.println("-------- 当前状态 --------");
  Serial.printf("  WiFi    : %s\n", (WiFi.status() == WL_CONNECTED) ? "已连接" : "未连接");
  Serial.printf("  MQTT    : %s\n", mqtt.connected() ? "已连接" : "未连接");
  Serial.printf("  IP      : %s\n", ip.c_str());
  Serial.printf("  心率模块: %s\n", hrSensorOK ? "已检测到" : "未检测到(上报 -1 属正常)");
  Serial.printf("  当前心率: %d\n", currentHR);
  Serial.printf("  舵机    : %d 度\n", strapAngle);
  Serial.printf("  温度    : %s\n", tecDir < 0 ? "凉" : (tecDir > 0 ? "热" : "关"));
  Serial.println("--------------------------");
}

void startDemo() {
  demoStep = 1;
  demoNextMs = 0;
  Serial.println("[演示] 七个输出串烧开始（约 20 秒）");
}

void updateDemo() {
  if (demoStep == 0 || millis() < demoNextMs) return;
  switch (demoStep) {
    case 1: doVibe(1);          demoNextMs = millis() + 2500UL; demoStep = 2;  break;
    case 2: doVibe(2);          demoNextMs = millis() + 2500UL; demoStep = 3;  break;
    case 3: doVibe(3);          demoNextMs = millis() + 2500UL; demoStep = 4;  break;
    case 4: doStrap(STRAP1_ANGLE); demoNextMs = millis() + 2500UL; demoStep = 5; break;
    case 5: doStrap(STRAP2_ANGLE); demoNextMs = millis() + 2500UL; demoStep = 6; break;
    case 6: doTec(-1);          demoNextMs = millis() + 2500UL; demoStep = 7;  break;
    case 7: doTec(1);           demoNextMs = millis() + 2500UL; demoStep = 8;  break;
    case 8: doStrap(0); doTec(0); demoNextMs = millis() + 1000UL; demoStep = 9; break;
    case 9: Serial.println("[演示] 七个输出串烧结束，已复位"); demoStep = 0; break;
    default: demoStep = 0; break;
  }
}

void runSerialLine(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;
  for (unsigned int i = 0; i < cmd.length(); i++) cmd[i] = (char)tolower((unsigned char)cmd[i]);

  Serial.print("> "); Serial.println(cmd);

  if (cmd == "help")   { printHelp();   return; }
  if (cmd == "status") { printStatus(); return; }
  if (cmd == "demo")   { startDemo();   return; }
  if (cmd == "strap0") { doStrap(0);    return; }
  if (cmd == "off")    { doTec(0);      return; }

  if (cmd.startsWith("{")) { handleCommand(cmd.c_str()); return; }

  const char *acts[] = {"vib1", "vib2", "vib3", "strap1", "strap2", "cool", "warm"};
  for (int i = 0; i < 7; i++) {
    if (cmd == acts[i]) {
      char json[48];
      snprintf(json, sizeof(json), "{\"act\":\"%s\"}", acts[i]);
      Serial.print("[演示] 等同 MQTT 指令: "); Serial.println(json);
      handleCommand(json);
      return;
    }
  }
  Serial.println("不认识这条命令。输入 help 看七个输出。");
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(800);
  Serial.println();
  Serial.println("============================================");
  Serial.println(" 触觉社交手环 ESP32 固件  M1  七个输出版");
  Serial.println("============================================");

  digitalWrite(PIN_VIB, LOW);
  pinMode(PIN_VIB, OUTPUT);
  relayWrite(PIN_RELAY_A, false);          // 上电两个继电器全关，TEC 不通电
  relayWrite(PIN_RELAY_B, false);
  strapServo.setPeriodHertz(50);
  strapServo.attach(PIN_SERVO, 500, 2400);
  strapServo.write(0);
  Serial.println("[初始化] 三通道就绪（震动=关 / TEC=关 / 舵机=松开）");

  setupHeartRate();

  uint64_t chipId = ESP.getEfuseMac();
  snprintf(mqttClientId, sizeof(mqttClientId), "wrist-%06X",
           (unsigned int)(chipId & 0xFFFFFFULL));

  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(256);
  mqtt.setCallback(onMqttMessage);

  setupWifi();

  memset(&beat, 0, sizeof(beat));
  lastBioMs = millis();
  lastMqttRetryMs = 0;

  Serial.println("--------------------------------------------");
  printHelp();
  Serial.println("串口监视器右下角：波特率 115200，行尾选「换行符」");
  Serial.println("--------------------------------------------");
}

void loop() {
  updateMqtt();
  updateVibe();
  updateTec();
  updateDemo();
  updateHeartRate();
  updateBio();

  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    runSerialLine(line);
  }
}
