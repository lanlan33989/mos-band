/*
 * ===========================================================================
 *  触觉社交手环 · ESP32 固件  【W2 · Wokwi 仿真版（含触摸上行）】
 *  文件：firmware/hapticband-sim/hapticband-sim.ino
 *  依据：《触觉手环-仿真方案-Wokwi.md》§5（同一份真机固件 + SIM_MODE 开关）
 *  与真机版 hapticband.ino 的差异（仅此 4 处，其余逐行相同）：
 *    1. SIM_MODE=1：WiFi 填 Wokwi-Guest；继电器触发极性翻转（LED 高电平亮）
 *    2. 震动输出走 tone()（Wokwi 蜂鸣器不认 digitalWrite）
 *    3. MAX30102 缺席时自动生成 62-108 的模拟心率（协议不变，wrist/bio 照发）
 *    4. W2 新增：4 按键手势状态机 → 六手势（tap1/tap2/long/swipe_up/swipe_down/rub）
 *       → wrist/ui 上行（协议见《触觉上行通道-设计规范.md》v1.1 §3/§4）
 *  恢复真机：改用 hapticband.ino 原文件即可，协议零差异
 * ===========================================================================
 */

#include <WiFi.h>
#include <Wire.h>
#include <string.h>
#include <ctype.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include "MAX30105.h"

// ===== 仿真开关（唯一与真机版的本质区别）=================================
#define SIM_MODE 1        // 1=Wokwi 仿真；0=真机（本文件按 1 配置）
#define SIM_FAKE_HR 1     // MAX30102 缺席时用模拟心率（62-108 随机游走）
// =========================================================================

// ===== 第 1 区：Wokwi 虚拟网络 ===========================================
#define WIFI_SSID   "Wokwi-GUEST"
#define WIFI_PASS   ""

#define MQTT_BROKER     "broker-cn.emqx.io"   // 2026-11-05：国际端点 broker.emqx.io 在 Wokwi 网络里连不上(-2)，换国内端点（W1 实测结论）；后端 config.json 已同步
#define MQTT_PORT       1883
#define MQTT_TOPIC_CMD  "wrist/cmd"
#define MQTT_TOPIC_BIO  "wrist/bio"
#define MQTT_TOPIC_UI   "wrist/ui"   // W2 上行手势（设计规范 §4）
// ========================================================================

// ===== 第 2 区：引脚与安全参数 ============================================
#define PIN_VIB       25      // 仿真=蜂鸣器；真机=震动马达模块 IN
#define PIN_RELAY_A   26      // 仿真=蓝 LED（凉）；真机=继电器 A
#define PIN_RELAY_B   14      // 仿真=红 LED（热）；真机=继电器 B
#define PIN_SERVO     27      // 表带收紧舵机
#define PIN_SDA       21
#define PIN_SCL       22

// 触摸/按钮 E1-E4（W1 只接线；W2 手势状态机从这里读）
// 仿真=按钮按下为 LOW（INPUT_PULLUP）；真机=touchRead 相对基线判据
#define PIN_BTN_E1    4
#define PIN_BTN_E2    13
#define PIN_BTN_E3    32
#define PIN_BTN_E4    33

#if SIM_MODE
  #define RELAY_ACTIVE_LOW 0   // 仿真 LED 高电平亮
#else
  #define RELAY_ACTIVE_LOW 1   // 真机多数继电器低电平触发
#endif
#define RELAY_OPEN_DRAIN 0

#define VIB_DUR_MS       120
#define STRAP1_ANGLE     35
#define STRAP2_ANGLE     70
#define TEC_PULSE_MS     5000UL
#define TEC_MAX_ON_MS    30000UL
#define TEC_REFRACTORY_MS 2000UL
#define BIO_INTERVAL_MS  2000UL
#define HR_SAMPLE_MS     20UL

// 震动输出封装：仿真蜂鸣器用 tone()，真机马达用 digitalWrite
#if SIM_MODE
  #define VIB_ON()   tone(PIN_VIB, 2000)
  #define VIB_OFF()  noTone(PIN_VIB)
#else
  #define VIB_ON()   digitalWrite(PIN_VIB, HIGH)
  #define VIB_OFF()  digitalWrite(PIN_VIB, LOW)
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

// ===== W2：触摸上行手势（设计规范 v1.1 §3/§4；仿真=按键按下为激活）=====
#define GEST_REFRACTORY_MS  300UL   // 手势后不应期（§3），也用作下行输出互扰窗口（§5.5）
#define TAP_MIN_MS          30UL    // tap 脉冲下限
#define TAP_MAX_MS          250UL   // tap 脉冲上限
#define TAP_GAP_MIN_MS      80UL    // tap2 间隔下限
#define TAP_GAP_MAX_MS      350UL   // tap2 间隔上限
#define TAP_CONFIRM_MS      2000UL  // tap 结算静默窗：2 秒无任何新事件才结算 tap（2026-11-05 用户确认：先 1.2s 实测鼠标挪位超时，放宽到 2s 留 1 秒余量；设计规范 §3 真机仍 400ms）
#define LONG_MIN_MS         800UL   // 长按阈值
#define LONG_AFTER_MS       1500UL  // 长按触发后强制静默期（§3）
#define SWIPE_STEP_MAX_MS   1800UL  // 2026-11-10 录屏实测：鼠标依次点 4 个按钮，首步常达 1.25s；放宽到 1.8s（仍小于 TAP_CONFIRM_MS 2000，不会被 tap 结算抢先）
#define SWIPE_SPAN_MAX_MS   6500UL  // swipe 全序列上限（3 步 × 1.8s + 余量）
#define RUB_WINDOW_MS       1500UL  // rub 统计窗口（2026-11-05 确认：1s→1.5s）
#define RUB_MIN_SEGS        3       // 方向段数：E1→E2→E1→E2 四次交替 = 3 段即可（原 RUB_MIN_FLIPS=3 按反转次数算需 5 次）
#define RUB_MIN_SPAN_MS     300UL   // 放宽下限（原 500）：稍慢的交替也能识别
#define RUB_MAX_SPAN_MS     2500UL

struct ActEv { int idx; unsigned long t0; unsigned long t1; };
ActEv evBuf[10];                  // 电极激活事件环形缓冲（旧在前）
int  evCount = 0;

bool muted = false;               // long 手势的本地静音开关（§4）
unsigned long muteUntilMs = 0;
unsigned long touchBlockUntilMs = 0;   // 下行输出后 300ms 内丢弃触摸（§5.5）
unsigned long refractoryUntilMs = 0;

int  pressedIdx = -1;             // 当前按下的电极 E1-E4 → 0-3，-1=无
unsigned long pressStartMs = 0;
bool longFired = false;           // 本次长按已触发（等松开复位）
unsigned long lastEvEndMs = 0;    // 最近一次激活事件的结束时刻（tap 结算的静默计时起点）

int btnPins[4] = { PIN_BTN_E1, PIN_BTN_E2, PIN_BTN_E3, PIN_BTN_E4 };

// 前向声明：调试台 long 按钮（wrist/cmd 维护指令）与 Wokwi 本地长按共用 toggleMute
void uiEmit(const char *g);
void faceNeutral();

// 心率
bool hrSensorOK = false;
bool fakeHR = false;          // 仿真且传感器缺席时置位
int  fakeBPM = 78;
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
  touchBlockUntilMs = millis() + GEST_REFRACTORY_MS;
  if (n < 1) n = 1;
  if (n > 3) n = 3;
  vibLeft = n;
  vibOn = true;
  vibActive = true;
  VIB_ON();
  vibPhaseEndMs = millis() + VIB_DUR_MS;
  Serial.printf("[动作] 振动 %d 下\n", n);
}

void updateVibe() {
  if (!vibActive || millis() < vibPhaseEndMs) return;
  unsigned long now = millis();
  if (vibOn) {
    VIB_OFF();
    vibOn = false;
    vibLeft--;
    if (vibLeft <= 0) { vibActive = false; return; }
    vibPhaseEndMs = now + VIB_DUR_MS;
  } else {
    VIB_ON();
    vibOn = true;
    vibPhaseEndMs = now + VIB_DUR_MS;
  }
}

// ===== 表情屏引擎 v1.5（SSD1306 OLED + FluxGarage RoboEyes，眼睛样式同 ZHINENG-xiaoche-C3MINI）=====
// 库能力全开：mood 四态 + 眼睛形状/圆角/间距 + 8 方位 + 大笑/困惑抖动 + 冒汗 + 单眼眨 + 独眼。
// 关键：关掉库自带 idle 满屏游走（那是眼睛不居中的原因），默认强制 setPosition(DEFAULT) 居中；
// 自主微表情只是短暂偏离，结束立刻回中央。表情库 GPL-3.0（FluxGarage RoboEyes）。
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <FluxGarage_RoboEyes.h>

Adafruit_SSD1306 display(128, 64, &Wire, -1);
RoboEyes<Adafruit_SSD1306> roboEyes(display);

#define FACE_NEUTRAL   0
#define FACE_HAPPY     1
#define FACE_LAUGH     2
#define FACE_ANGRY     3
#define FACE_CONFUSED  4
#define FACE_TIRED     5
#define FACE_SURPRISED 6
#define FACE_EXCITED   7
#define FACE_WINK      8
#define FACE_SWEAT     9
#define FACE_SHY       10
#define FACE_LOOK      11
#define FACE_CYCLOPS   12
#define FACE_BLINK     13

unsigned long faceUntilMs = 0;      // 表情回退时刻（0=中性）
unsigned long clockUntilMs = 0;     // 时钟显示截止时刻
unsigned long nextAutoMs = 0;       // 下一次自主微表情时刻
char        clockText[12] = "";

void faceNeutral() {
  roboEyes.setMood(DEFAULT);
  roboEyes.setWidth(36, 36);
  roboEyes.setHeight(36, 36);
  roboEyes.setBorderradius(8, 8);
  roboEyes.setSpacebetween(10);
  roboEyes.setCyclops(OFF);
  roboEyes.setSweat(OFF);
  roboEyes.setHFlicker(OFF, 0);
  roboEyes.setVFlicker(OFF, 0);
  roboEyes.setPosition(DEFAULT);    // 居中固定
  roboEyes.open();
}

void facePlay(byte expr, unsigned long durMs) {
  if (muted) return;                // 静音期间眼睛闭着，不演表情
  faceNeutral();
  switch (expr) {
    case FACE_HAPPY:     roboEyes.setMood(HAPPY); break;
    case FACE_LAUGH:     roboEyes.setMood(HAPPY); roboEyes.anim_laugh(); break;
    case FACE_ANGRY:     roboEyes.setMood(ANGRY); roboEyes.setWidth(40, 40); break;
    case FACE_CONFUSED:  roboEyes.anim_confused(); break;
    case FACE_TIRED:     roboEyes.setMood(TIRED); roboEyes.setHeight(30, 30); break;
    case FACE_SURPRISED: roboEyes.setWidth(30, 30); roboEyes.setHeight(44, 44);
                         roboEyes.setBorderradius(14, 14); break;
    case FACE_EXCITED:   roboEyes.setMood(HAPPY); roboEyes.setWidth(42, 42);
                         roboEyes.setHeight(40, 40); roboEyes.anim_laugh(); break;
    case FACE_WINK:      roboEyes.setMood(HAPPY); roboEyes.blink(1, 0);
                         roboEyes.anim_laugh(); break;
    case FACE_SWEAT:     roboEyes.setMood(ANGRY); roboEyes.setSweat(ON); break;
    case FACE_SHY:       roboEyes.setPosition(S); roboEyes.blink(); break;
    case FACE_LOOK:      roboEyes.setPosition(random(2) ? E : W); break;
    case FACE_CYCLOPS:   roboEyes.setCyclops(ON); roboEyes.setWidth(56, 56); break;
    case FACE_BLINK:     roboEyes.blink(); break;
    default: break;
  }
  faceUntilMs = millis() + durMs;
  clockUntilMs = 0;
  Serial.printf("[表情] #%d（%lu ms 后回中央）\n", expr, durMs);
}

void faceOnGesture(const char *g) {
  if      (strcmp(g, "tap1")      == 0) facePlay(FACE_LAUGH, 2500UL);      // 肯定→笑
  else if (strcmp(g, "tap2")      == 0) facePlay(FACE_CONFUSED, 2500UL);   // 否定→困惑
  else if (strcmp(g, "swipe_up")  == 0) facePlay(FACE_EXCITED, 2500UL);    // 加大频率→兴奋
  else if (strcmp(g, "swipe_down")== 0) facePlay(FACE_TIRED, 2500UL);      // 减小频率→困倦
  else if (strcmp(g, "rub")       == 0) facePlay(FACE_WINK, 3000UL);       // 摸头→眨眼大笑
}

byte faceFromName(const char *f) {
  if      (strcmp(f, "happy")     == 0) return FACE_HAPPY;
  else if (strcmp(f, "laugh")     == 0) return FACE_LAUGH;
  else if (strcmp(f, "angry")     == 0) return FACE_ANGRY;
  else if (strcmp(f, "confused")  == 0) return FACE_CONFUSED;
  else if (strcmp(f, "tired")     == 0) return FACE_TIRED;
  else if (strcmp(f, "sleepy")    == 0) return FACE_TIRED;   // 库无 SLEEPY，用 TIRED
  else if (strcmp(f, "surprised") == 0) return FACE_SURPRISED;
  else if (strcmp(f, "excited")   == 0) return FACE_EXCITED;
  else if (strcmp(f, "wink")      == 0) return FACE_WINK;
  else if (strcmp(f, "sweat")     == 0) return FACE_SWEAT;
  else if (strcmp(f, "shy")       == 0) return FACE_SHY;
  else if (strcmp(f, "look")      == 0) return FACE_LOOK;
  else if (strcmp(f, "cyclops")   == 0) return FACE_CYCLOPS;
  else if (strcmp(f, "blink")     == 0) return FACE_BLINK;
  return FACE_NEUTRAL;
}

void showClock(const char *t, unsigned long durMs) {
  strncpy(clockText, t, sizeof(clockText) - 1);
  clockText[sizeof(clockText) - 1] = '\0';
  clockUntilMs = millis() + durMs;
  Serial.printf("[表情屏] 时间 %s（5 秒后回眼睛）\n", clockText);
}

void updateFace() {
  unsigned long now = millis();
  if (clockUntilMs > 0) {
    if (now >= clockUntilMs) { clockUntilMs = 0; }
    else {
      display.clearDisplay();
      display.setTextSize(2);
      display.setTextColor(SSD1306_WHITE);
      display.setCursor(10, 24);
      display.print(clockText);
      display.display();
      return;                       // 时钟模式下不让眼睛覆屏
    }
  }
  if (muted) { roboEyes.update(); return; }   // 静音：闭眼，不演表情
  if (faceUntilMs > 0 && now >= faceUntilMs) {
    faceNeutral();                 // 表情结束 → 立刻回到正中央
    faceUntilMs = 0;
  }
  if (faceUntilMs == 0 && now >= nextAutoMs) {
    // 没人给指令也活灵活现：每 6-14 秒随机一个小动作，1 秒内回中央
    static const byte pool[] = {FACE_BLINK, FACE_LOOK, FACE_LAUGH, FACE_CONFUSED,
                                FACE_WINK, FACE_SHY, FACE_SURPRISED, FACE_CYCLOPS};
    facePlay(pool[random(sizeof(pool))], 900UL);
    nextAutoMs = now + 6000UL + random(8000UL);
  }
  if (faceUntilMs == 0) {
    roboEyes.setSweat(currentHR > 95 ? ON : OFF);   // 心率高了自然冒汗
  }
  roboEyes.update();
}

void doStrap(int angle) {
  touchBlockUntilMs = millis() + GEST_REFRACTORY_MS;
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
    pinMode(pin, INPUT);
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
  touchBlockUntilMs = millis() + GEST_REFRACTORY_MS;
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
// long = 静音 / 起来开关：Wokwi 本地长按与调试台同一个按钮共用此入口
// ---------------------------------------------------------------------------
void toggleMute() {
  unsigned long now = millis();
  uiEmit("long");
  if (muted) {
    muted = false;
    faceNeutral();               // 起来：睁眼回中央
    faceUntilMs = 0;
    Serial.println("[静音] long -> 解除静音");
  } else {
    muted = true;
    muteUntilMs = now + 600000UL;
    faceUntilMs = 0;
    roboEyes.close();            // 静音：闭眼
    Serial.println("[静音] long -> 静音 10 分钟（闭眼）");
  }
  evCount = 0;
  refractoryUntilMs = now + LONG_AFTER_MS;
}

// ---------------------------------------------------------------------------
// 指令分发：只认七个 act ID
// ---------------------------------------------------------------------------
void handleCommand(const char *jsonText) {
  String s(jsonText);
  char buf[16] = "";

  char chbuf[12] = "";
  if (jsonGetStr(s, "ch", chbuf, sizeof(chbuf))) {
    if (strcmp(chbuf, "bio") == 0) return;
    // 调试台 long 按钮：固件只订阅 wrist/cmd，后端把 long 作为维护指令下发到这里
    if (strcmp(chbuf, "sys") == 0) {
      char cbuf[16] = "";
      jsonGetStr(s, "cmd", cbuf, sizeof(cbuf));
      if (strcmp(cbuf, "long") == 0) toggleMute();
      else Serial.printf("[错误] 不认识的维护指令 cmd=%s\n", cbuf);
      return;
    }
  }

  if (!jsonGetStr(s, "act", buf, sizeof(buf))) {
    Serial.println("[错误] 指令里没有 act 字段，已忽略");
    return;
  }
  // 输出互斥：振动 / 收紧类动作下发时先关掉温度通道，避免上一条 cool / warm 的
  // 5 秒脉冲残留在 Wokwi 画面里，与调试台「最近一次触发」的卡片不一致
  if (strncmp(buf, "vib", 3) == 0 || strncmp(buf, "strap", 5) == 0) tecDrive(0);
  //  AI 自己输出的七个信号也有表情（人类没下指令时屏幕照样活）
  if      (strcmp(buf, "vib1")   == 0) { doVibe(1); facePlay(FACE_BLINK, 1200UL); }
  else if (strcmp(buf, "vib2")   == 0) { doVibe(2); facePlay(FACE_CONFUSED, 1800UL); }
  else if (strcmp(buf, "vib3")   == 0) { doVibe(3); facePlay(FACE_SURPRISED, 1800UL); }
  else if (strcmp(buf, "strap1") == 0) { doStrap(STRAP1_ANGLE); facePlay(FACE_ANGRY, 2000UL); }
  else if (strcmp(buf, "strap2") == 0) { doStrap(STRAP2_ANGLE); facePlay(FACE_SWEAT, 2500UL); }
  else if (strcmp(buf, "cool")   == 0) { doTec(-1); facePlay(FACE_SHY, 2000UL); }
  else if (strcmp(buf, "warm")   == 0) { doTec(1); facePlay(FACE_HAPPY, 2000UL); }
  else if (strcmp(buf, "face")   == 0) {
    // {"act":"face","f":"happy|laugh|angry|confused|tired|surprised|excited|wink|sweat|shy|look|cyclops|blink"}
    char fbuf[16] = "default";
    jsonGetStr(s, "f", fbuf, sizeof(fbuf));
    facePlay(faceFromName(fbuf), 5000UL);
  }
  else if (strcmp(buf, "time")   == 0) {
    // {"act":"time","t":"HH:MM:SS"}：屏显时间 5 秒（固件无 RTC，时间由软件端下发）
    char tbuf[12] = "--:--";
    jsonGetStr(s, "t", tbuf, sizeof(tbuf));
    showClock(tbuf, 5000UL);
  }
  else if (strcmp(buf, "bio")    == 0) { }
  else Serial.printf("[错误] 不认识的动作 act=%s，已忽略\n", buf);
}

// ---------------------------------------------------------------------------
// 心率：真传感器 + 仿真兜底
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
  if (fakeHR) {
    // 仿真兜底：每秒一次随机游走，模拟佩戴中 62-108 BPM
    static unsigned long lastFakeMs = 0;
    if (millis() - lastFakeMs < 1000UL) return;
    lastFakeMs = millis();
    fakeBPM += random(-3, 4);
    if (fakeBPM < 62)  fakeBPM = 62;
    if (fakeBPM > 108) fakeBPM = 108;
    currentHR = fakeBPM;
    return;
  }
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
#if SIM_MODE && SIM_FAKE_HR
    fakeHR = true;
    randomSeed(micros());
    Serial.println("[心率] 仿真无 MAX30102 —— 启用模拟心率（62-108 随机游走），协议不变");
#else
    Serial.println("[心率] 没检测到 MAX30102 —— 上报 -1，另外三个通道不受影响");
#endif
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
unsigned long lastWifiRetryMs = 0;

void setupWifi() {
  Serial.printf("[WiFi] 正在连接 %s（channel 6）...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  // 官方 ESP32 WiFi 指南：Wokwi-GUEST 在 channel 6，显式指定可跳过扫描阶段、省约 4 秒
  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000UL) {
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
    Serial.println("[WiFi] 20 秒没连上 —— 免费公共网关偶发慢，8 秒后自动重试；也可刷新页面重播");
    Serial.println("       （公共网关免费且开箱即用；Private Gateway/wokwigw.exe 仅付费用户与本机服务场景需要）");
  }
  lastWifiRetryMs = millis();
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
  // 公共网关偶发抖动：WiFi 没连上就每 8 秒重试一次，不再「一失败永久离线」
  if (!wifiOK) {
    if (millis() - lastWifiRetryMs < 8000UL) return;
    setupWifi();
    return;
  }
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
  Serial.println("  vib1 / vib2 / vib3    振动 1 / 2 / 3 下（仿真=蜂鸣器响）");
  Serial.println("  strap1 / strap2       收紧 1 档(35度) / 2 档(70度)");
  Serial.println("  happy/tired/angry/laugh/wink/confused/surprised/cyclops  表情演示（5 秒）");
  Serial.println("  time                  表情屏显示运行时长（5 秒）");
  Serial.println("  cool / warm           凉 5 秒 / 热 5 秒（仿真=蓝/红 LED）");
  Serial.println("-------- 辅助命令 --------");
  Serial.println("  demo      七个输出依次演一遍（拍视频用）");
  Serial.println("  strap0    松开表带（维护用）");
  Serial.println("  off       关闭温度（维护用）");
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
  Serial.printf("  心率模块: %s\n", fakeHR ? "仿真模拟心率" : (hrSensorOK ? "已检测到" : "未检测到(上报 -1 属正常)"));
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
  if (cmd == "happy")  { facePlay(FACE_HAPPY, 5000UL);     return; }
  if (cmd == "tired")  { facePlay(FACE_TIRED, 5000UL);     return; }
  if (cmd == "angry")  { facePlay(FACE_ANGRY, 5000UL);     return; }
  if (cmd == "laugh")  { facePlay(FACE_LAUGH, 5000UL);     return; }
  if (cmd == "wink")   { facePlay(FACE_WINK, 5000UL);      return; }
  if (cmd == "confused"){ facePlay(FACE_CONFUSED, 5000UL); return; }
  if (cmd == "surprised"){ facePlay(FACE_SURPRISED, 5000UL); return; }
  if (cmd == "cyclops"){ facePlay(FACE_CYCLOPS, 5000UL);   return; }
  if (cmd == "time")   {
    unsigned long s = millis() / 1000UL;
    char tb[12];
    snprintf(tb, sizeof(tb), "%02lu:%02lu:%02lu", (s / 3600UL) % 100, (s / 60UL) % 60, s % 60);
    showClock(tb, 5000UL);
    return;
  }

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
// W2：触摸上行手势状态机
// 仿真=4 按键（按下 LOW=激活）；真机=touchRead 相对基线判据，状态机共用
// ---------------------------------------------------------------------------
void uiEmit(const char *g) {
  if (muted && strcmp(g, "long") != 0) {
    Serial.printf("[静音中] 丢弃手势 %s\n", g);
    return;
  }
  char payload[80];
  snprintf(payload, sizeof(payload), "{\"ch\":\"ui\",\"g\":\"%s\",\"ms\":%lu}", g, millis());
  if (mqtt.connected()) {
    mqtt.publish(MQTT_TOPIC_UI, payload);
    Serial.printf("[上行] %s : %s\n", g, payload);
  } else {
    Serial.printf("[上行] (MQTT 未连，仅串口) %s %s\n", g, payload);
  }
  faceOnGesture(g);   // 六种手势各有本地即时表情（不等后端回传）
}

void clearEv() { evCount = 0; }

void finishGesture() {
  clearEv();
  refractoryUntilMs = millis() + GEST_REFRACTORY_MS;
}

// tap 结算：只在「1.2 秒全局无新事件」时走到这里（swipe/rub 已在事件时刻先行判定并清缓冲）。
// 缓冲里恰好是同一电极的两次脉冲、间隔 80-350ms → tap2；其余一律 tap1（多按的那几下视为误触，只报最新一次）
void settleTaps() {
  if (evCount == 0) return;
  bool isTap2 = (evCount == 2 &&
      evBuf[0].idx == evBuf[1].idx &&
      evBuf[1].t0 - evBuf[0].t1 >= TAP_GAP_MIN_MS &&
      evBuf[1].t0 - evBuf[0].t1 <= TAP_GAP_MAX_MS);
  Serial.printf("[tap 结算] 缓冲 %d 个事件，静默 %lu ms -> %s\n",
                evCount, millis() - lastEvEndMs, isTap2 ? "tap2" : "tap1");
  if (isTap2) { uiEmit("tap2"); } else { uiEmit("tap1"); }
  finishGesture();
}

void pushEv(int idx, unsigned long t0, unsigned long t1) {
  if (evCount == 10) {
    for (int i = 0; i < 9; i++) evBuf[i] = evBuf[i + 1];
  } else {
    evCount++;
  }
  evBuf[evCount - 1].idx = idx;
  evBuf[evCount - 1].t0 = t0;
  evBuf[evCount - 1].t1 = t1;
  lastEvEndMs = t1;
  Serial.printf("[缓冲] E%d（第 %d 个事件，%lu ms）\n", idx + 1, evCount, t1);
}

void evalGestures(int idx, unsigned long t0, unsigned long t1) {
  unsigned long now = t1;

  // 1) rub：1.5s 窗口内方向段 ≥3（E1→E2→E1→E2 四次交替即满足），总时长 0.3-2.5s（2026-11-05 放宽）
  if (evCount >= 4) {
    int start = evCount - 1;
    while (start > 0 && now - evBuf[start - 1].t1 <= RUB_WINDOW_MS) start--;
    int segs = 0, lastDir = 0;
    for (int i = start; i < evCount - 1; i++) {
      int d = evBuf[i + 1].idx - evBuf[i].idx;
      if (d == 0) continue;
      int dir = d > 0 ? 1 : -1;
      if (dir != lastDir) segs++;
      lastDir = dir;
    }
    unsigned long span = now - evBuf[start].t0;
    if (segs >= RUB_MIN_SEGS && span >= RUB_MIN_SPAN_MS && span <= RUB_MAX_SPAN_MS) {
      uiEmit("rub");             // 表情由 uiEmit -> faceOnGesture 统一处理
      finishGesture();
      return;
    }
  }

  // 2) swipe：最近 4 个激活严格单向（允许跳步）；节奏按步计——相邻两次激活 ≤1.8 秒，
  //    全序列 ≤6.5 秒（仿真专用放宽；设计规范 §3 原值全程 ≤600ms，真机不变）
  if (evCount >= 4) {
    int a = evBuf[evCount - 4].idx, b = evBuf[evCount - 3].idx;
    int c = evBuf[evCount - 2].idx, d = evBuf[evCount - 1].idx;
    unsigned long s1 = evBuf[evCount - 3].t1 - evBuf[evCount - 4].t1;
    unsigned long s2 = evBuf[evCount - 2].t1 - evBuf[evCount - 3].t1;
    unsigned long s3 = evBuf[evCount - 1].t1 - evBuf[evCount - 2].t1;
    unsigned long span = now - evBuf[evCount - 4].t0;
    bool oneWay = (b > a && c > b && d > c) || (b < a && c < b && d < c);
    bool paceOK = s1 <= SWIPE_STEP_MAX_MS && s2 <= SWIPE_STEP_MAX_MS &&
                  s3 <= SWIPE_STEP_MAX_MS && span <= SWIPE_SPAN_MAX_MS;
    if (paceOK && oneWay) {
      if (b > a) { uiEmit("swipe_up");   finishGesture(); return; }
      else       { uiEmit("swipe_down"); finishGesture(); return; }
    }
    if (oneWay && !paceOK) {
      Serial.printf("[swipe] 方向成立但节奏超窗：步 %lu/%lu/%lu ms，全序列 %lu ms（上限 %lu / %lu）\n",
                    s1, s2, s3, span, SWIPE_STEP_MAX_MS, SWIPE_SPAN_MAX_MS);
    }
  }
  // tap1/tap2 不在这里判：一律等 2 秒全局静默后由 settleTaps() 结算（见 gestureUpdate），
  // 保证 tap 永不抢占 swipe/rub 的判定路径
}

void gestureUpdate() {
  unsigned long now = millis();

  // 静音到期自动解除（§4：静音 10 分钟）
  if (muted && now >= muteUntilMs) {
    muted = false;
    faceNeutral();                 // 睁眼回中央
    faceUntilMs = 0;
    Serial.println("[静音] 10 分钟到，自动解除");
  }

  // tap 结算：1.2 秒无任何新事件且没按住任何键才确认（2026-11-05 用户确认：tap 让路 swipe/rub）
  if (evCount > 0 && pressedIdx < 0 && now - lastEvEndMs >= TAP_CONFIRM_MS) {
    settleTaps();
  }

  // 下行输出后 300ms 内丢弃触摸（§5.5 输出互扰）
  bool blocked = now < touchBlockUntilMs;

  // 长按阈值：持续按住 ≥800ms 立即触发（宁漏勿误：阈值从严不会误触）
  if (pressedIdx >= 0 && !longFired && !blocked &&
      now - pressStartMs >= LONG_MIN_MS) {
    longFired = true;
    toggleMute();
    return;
  }

  // 读当前按下状态（按下 = LOW = 激活）
  int cur = -1;
  if (!blocked && now >= refractoryUntilMs) {
    for (int i = 0; i < 4; i++) {
      if (digitalRead(btnPins[i]) == LOW) { cur = i; break; }
    }
  }

  if (cur != pressedIdx) {
    if (pressedIdx >= 0) {
      // 松开：结算一次激活事件
      int idx = pressedIdx;
      unsigned long st = pressStartMs;
      unsigned long dur = now - st;
      bool wasLong = longFired;
      longFired = false;
      pressedIdx = -1;
      if (!wasLong && now >= refractoryUntilMs && dur >= TAP_MIN_MS) {
        pushEv(idx, st, now);
        evalGestures(idx, st, now);
      }
    } else if (cur >= 0) {
      pressedIdx = cur;
      pressStartMs = now;
      longFired = false;
    }
  }
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(800);
  Serial.println();
  Serial.println("============================================");
  Serial.println(" 触觉社交手环 ESP32 固件  W2  Wokwi 仿真版（含触摸上行）");
  Serial.println("[固件版本] 2026-11-10c · 输出互斥 / swipe 放宽 / long 远程开关");
  Serial.println("============================================");

  VIB_OFF();
  pinMode(PIN_VIB, OUTPUT);
  pinMode(PIN_BTN_E1, INPUT_PULLUP);   // W2 手势用，W1 先就位
  pinMode(PIN_BTN_E2, INPUT_PULLUP);
  pinMode(PIN_BTN_E3, INPUT_PULLUP);
  pinMode(PIN_BTN_E4, INPUT_PULLUP);
  relayWrite(PIN_RELAY_A, false);
  relayWrite(PIN_RELAY_B, false);
  strapServo.setPeriodHertz(50);
  strapServo.attach(PIN_SERVO, 500, 2400);
  strapServo.write(0);

  // 表情屏：SSD1306 128x64 @ I2C 0x3C（SDA=21 / SCL=22）
  Wire.begin(PIN_SDA, PIN_SCL);
  display.begin(SSD1306_SWITCHCAPVCC, 0x3C);
  roboEyes.begin(128, 64, 30);
  roboEyes.setAutoblinker(ON, 2, 3);     // 自动眨眼：约 2 秒 + 随机 0-3 秒
  roboEyes.setIdleMode(OFF);             // 关掉库自带的满屏游走（否则眼睛不在中央）
  roboEyes.setCuriosity(ON);             // 左右看时外眼略挑
  faceNeutral();                         // 强制居中 + 标准眼睛形状
  nextAutoMs = millis() + 4000UL;        // 4 秒后开始第一波自主微表情
  Serial.println("[表情屏] SSD1306 就绪：眼睛居中，自主微表情已开");
  Serial.println("[初始化] 三通道就绪（震动=关 / TEC=关 / 舵机=松开）");

  setupHeartRate();

  uint64_t chipId = ESP.getEfuseMac();
  snprintf(mqttClientId, sizeof(mqttClientId), "wrist-sim-%06X",
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
  Serial.println("--------------------------------------------");
}

void loop() {
  updateMqtt();
  updateVibe();
  updateTec();
  updateFace();      // 表情屏：眼睛动画 / 时钟 / 表情回退
  updateDemo();
  updateHeartRate();
  updateBio();
  gestureUpdate();   // W2：触摸上行手势状态机

  if (Serial.available() > 0) {
    String line = Serial.readStringUntil('\n');
    runSerialLine(line);
  }
}
