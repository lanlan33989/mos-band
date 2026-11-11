# Wokwi 导入全指引

本文是「把本仓库跑进 Wokwi」的单一入口，覆盖三种导入方式、网络配置、库依赖、MQTT 协议、验收清单、重启流程与踩过的坑。

> 仿真工程目录：[`firmware/hapticband-sim/`](../firmware/hapticband-sim/)

---

## 0. 三种方式怎么选

| 方式 | 适合 | 是否需要本地软件 | 是否需要编译产物 |
| --- | --- | --- | --- |
| **A. 浏览器 wokwi.com**（推荐） | 第一次跑、要肉眼看舵机/LED 动 | 不需要 | 不需要，Wokwi 自己编译 `.ino` |
| **B. wokwi-cli** | 无头运行 / 录屏 / 挂 CI | 需要 Node.js + CLI | 不需要，CLI 编译 `.ino` |
| **C. 从 GitHub 仓库导入** | 直接从公开仓库开一个项目 | 不需要 | 不需要，但需要把三个文件放到仓库根 |

**关于 `wokwi.toml`**：Wokwi 官方的 `wokwi.toml` 用于 VS Code 扩展与 wokwi-cli，作用是告诉 Wokwi **预编译固件**（`.bin` / `.elf` / `flasher_args.json`）在哪里，Wokwi 本身不编译代码。本项目提交的是 `.ino` 源码，浏览器版和 CLI 版都自己编译，因此**不需要 `wokwi.toml`**；随意写一个指向不存在 bin 的 toml 反而会报 `firmware binary not found`。官方文档：https://docs.wokwi.com/vscode/project-config

---

## 1. 需要用到的四个文件

| 文件 | 作用 |
| --- | --- |
| `sketch.ino` | Wokwi 浏览器工程的**默认入口文件名**，粘贴它就能编译 |
| `hapticband-sim.ino` | 同一份固件的正本文件名（内容与 `sketch.ino` 完全一致，便于本地归档） |
| `diagram.json` | 虚拟电路：ESP32 + 蜂鸣器 + 蓝/红 LED(各串 220Ω) + SG90 舵机 + SSD1306 OLED + 4 个按键 |
| `libraries.txt` | Wokwi 需要预装的 Arduino 库清单（6 行，见 §5） |

引脚：D25 马达、D26/D14 双继电器、D27 舵机、D21/D22 OLED I²C、D4/D13/D32/D33 四按键（E1–E4）。仿真里没有 MAX30102，心率由 `mock_hr` 注入。

---

## 2. 方式 A：浏览器 wokwi.com（逐步点击）

1. 打开 <https://wokwi.com>，注册并登录 → **New Project** → 选 **ESP32**。
2. 左侧编辑器里默认的 `sketch.ino`，把内容**整份替换**为 `firmware/hapticband-sim/sketch.ino` 全文（不要只贴片段，否则会残留旧代码导致 `long` / `swipe` 不生效）。
3. 点右上角编辑器旁的下拉箭头 → **New Tab**，建两个新标签页：
   - `diagram.json` ← 粘贴 `firmware/hapticband-sim/diagram.json` 全文
   - `libraries.txt` ← 粘贴 `firmware/hapticband-sim/libraries.txt` 全文
4. 点绿色**播放键**编译运行。
5. 打开 Serial Monitor，确认启动横幅：`[固件版本] 2026-11-10c · 输出互斥 / swipe 放宽 / long 远程开关`。**看不到这行说明跑的是旧固件**，回到第 2 步重贴并重启仿真。

> 更细的逐点击说明见 [`firmware/hapticband-sim/W1上手步骤.md`](../firmware/hapticband-sim/W1上手步骤.md)。

---

## 3. 方式 B：wokwi-cli

```bash
npm i -g @wokwi/cli
mkdir mos-wokwi && cd mos-wokwi
# 把 sketch.ino / diagram.json / libraries.txt 三个文件放进当前目录
cp /path/to/mos-band/firmware/hapticband-sim/{sketch.ino,diagram.json,libraries.txt} .
wokwi-cli .
```

Windows PowerShell：

```powershell
Copy-Item ..\mos-band\firmware\hapticband-sim\sketch.ino,..\mos-band\firmware\hapticband-sim\diagram.json,..\mos-band\firmware\hapticband-sim\libraries.txt .
wokwi-cli .
```

CLI 适合无头运行与自动化录屏；需要登录 Wokwi 账号获取 token，按 CLI 首次运行提示操作。

---

## 4. 方式 C：从 GitHub 仓库导入

Wokwi 的「Import from GitHub」读取的是**仓库根目录**的 `sketch.ino` + `diagram.json` + `libraries.txt`。本仓库把仿真工程放在 `firmware/hapticband-sim/` 下以保持源码目录清晰，所以导入有两种做法：

1. **推荐**：按方式 A 手动新建项目并粘贴三个文件（最稳，且能确认固件版本）。
2. **想直接从仓库开项目**：fork 本仓库后，在 fork 的根目录放一份这三个文件：

```bash
cp firmware/hapticband-sim/sketch.ino firmware/hapticband-sim/diagram.json firmware/hapticband-sim/libraries.txt .
git add sketch.ino diagram.json libraries.txt && git commit -m "wokwi: root project files"
```

然后在 wokwi.com 选择从 GitHub 导入该 fork。注意：**改动固件时根目录与 `firmware/hapticband-sim/` 两份要同步**，否则 Wokwi 会跑旧版。

---

## 5. 库依赖（`libraries.txt` 全文）

```
PubSubClient
ESP32Servo
SparkFun MAX3010x Pulse and Proximity Sensor Library
Adafruit GFX Library
Adafruit SSD1306
FluxGarage RoboEyes
```

前 3 个是下行链路（MQTT / 舵机 / 传感器头文件），后 3 个是 W2 新增的 SSD1306 表情屏。早期方案只写 3 行，会导致表情屏编译失败——以 `libraries.txt` 的 6 行为准。

真机版只需前 3 个库，见 [`firmware/hapticband/烧录与接线步骤.md`](../firmware/hapticband/烧录与接线步骤.md)。

---

## 6. 网络与 MQTT

| 项 | 值 |
| --- | --- |
| WiFi | `Wokwi-GUEST`，密码空，信道 6（`WiFi.begin("Wokwi-GUEST","",6)` 跳过扫描，省约 4 秒） |
| 网关 | 浏览器版**免费公共网关（Public Gateway）**默认可用，支持出网；不需要装 `wokwigw.exe`（私有网关是付费功能，只在需要从本机访问仿真里的服务时才用） |
| broker | **`broker-cn.emqx.io:1883`** |
| 订阅 | `wrist/cmd` |
| 发布 | `wrist/bio`（每 2 秒）、`wrist/ui`（手势） |
| 容错 | 20 秒连接超时 + 每 8 秒自动重连；断网时串口仍可完整演示七个输出与手势，日志显示「MQTT 未连，仅串口」属预期 |

> ⚠️ **broker 必须用国内端点**。`broker.emqx.io`（国际端点）在 Wokwi 里连不上，返回 `-2`；早期方案文档写的是国际端点，请以本文和 `W1上手步骤.md` 为准。真机固件 `hapticband.ino` 默认 `broker.emqx.io`，若要与仿真/调试台互通，把 `MQTT_BROKER` 改成 `broker-cn.emqx.io`，并保证 `config.json` 的 `mqtt.broker` 与固件一致。

判断连通：串口出现 `[WiFi] 连上了` → `[MQTT] 连接 broker-cn.emqx.io ... 成功` → `[MQTT] 已订阅 wrist/cmd`。

官方 ESP32 WiFi 指南：https://docs.wokwi.com/guides/esp32-wifi

---

## 7. 本机调试台侧

```bash
pip install -r requirements.txt
cp software/config.example.json software/config.json
python software/local_server.py --mqtt
```

浏览器打开 `http://127.0.0.1:8765`，通道选 **MQTT**（`.channel` 会记住选择）。不填密钥也能用 demo / mock 模式；云端 ASR 与 LLM 需自行填 key。

`wrist/cmd` 报文示例：

```json
{"act":"vib2"}
{"ch":"sys","cmd":"long"}
```

第二条是远程长按：调试台的 `long` 按钮会同时下发它，与 Wokwi 本地长按共用同一个 `toggleMute()`，实现「点一次静音 / 再点一次起来」。

---

## 8. 验收清单

- [ ] 串口出现 `2026-11-10c` 版本横幅
- [ ] `[WiFi] 连上了` + `[MQTT] 已订阅 wrist/cmd`
- [ ] 调试台「手动测试」依次点 `vib1/vib2/vib3` → 蜂鸣器响 1/2/3 声，串口 `[动作] 振动 X 下`
- [ ] `strap1/strap2` → 舵机 35° / 70°
- [ ] `cool` → 蓝 LED 亮 5 秒；`warm` → 红 LED 亮 5 秒
- [ ] 下发 `vib1` 时红/蓝 LED 立即熄灭（输出互斥，避免上一个 `warm` 脉冲残留）
- [ ] 心率滑杆拨到 ≥95 → 固定 7 下 `vib1`（30 BPM 节律），完成后冻结 600 秒；点「解除冻结」可立刻重来
- [ ] 四按键依次触发 `tap1` / `tap2` / `long` / `swipe_up` / `swipe_down` / `rub`，`wrist/ui` 有上报
- [ ] 调试台点 `long` 一次 → 表情屏闭眼 / 静音 10 分钟；再点一次 → 恢复

七个输出与协议总表见仓库根 `README.md` §3，手势判定窗口见 [`docs/触觉上行通道-设计规范.md`](触觉上行通道-设计规范.md)。

---

## 9. 改了固件之后：完整重启流程

Wokwi 不会热加载源码，必须重新编译：

1. 点**停止**（红色方块）结束当前仿真。
2. 在 `sketch.ino` 里改完并整份保存；如果同时改了 `diagram.json` / `libraries.txt`，也要重新粘贴。
3. 点绿色**播放键**，等待编译完成（首次约十几秒）。
4. 打开 Serial Monitor，先看版本横幅是否变成新版本号——**这是判断"到底跑没跑上新固件"的唯一可靠办法**。
5. 清一次串口输出（或刷新页面重播），再开始录屏 / 验收。
6. 若心跳段 NG 要重拍：调试台点「解除冻结」→ 滑杆回 80 → 再拨 ≥95。

只改调试台前端或后端时不需要动 Wokwi，刷新本机网页即可；但改了 `local_server.py` / `mock_server.py` 一类后端必须**重启 Python 进程**。

---

## 10. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `[错误]指令里没有 act 字段，已忽略` | Wokwi 跑的是**旧固件**，不认识 `{"ch":"sys","cmd":"long"}`。整份重贴 `sketch.ino` 并按 §9 重启，确认版本横幅 |
| `swipe` 总被判成 `tap1` | 四步方向成立但节奏超窗，2 秒静默后被 `settleTaps()` 兜底成单击。固件已放宽 `SWIPE_STEP_MAX_MS=1800` / `SWIPE_SPAN_MAX_MS=6500`，串口会打印 `[swipe] 方向成立但节奏超窗：...` 诊断 |
| 心率拨高后红灯亮、但调试台显示 `vib1` | `warm` 的红蓝 LED 是 5 秒脉冲，录完温度段立刻拨心率会看到残留。新固件在 `vib*`/`strap*` 到达时先 `tecDrive(0)` 关温度 LED；旧固件则等红灯熄灭再录 |
| 心跳拨高后既没红灯也没振动 | 不是坏了，是 7 下打完进入 **10 分钟冻结**。点调试台「解除冻结」，或重启后端清内存冻结状态 |
| MQTT 一直重连 / 返回 -2 | broker 用了国际端点，改成 `broker-cn.emqx.io`；确认 `config.json` 的 `mqtt.broker` 与固件一致 |
| 串口没有任何输出 | 没有点播放键 / 编译失败滚到 Serial Monitor 上方看报错 / `libraries.txt` 少了后 3 个表情屏库 |
| 调试台改了没生效 | 后端 Python 进程没重启，或浏览器缓存了旧页面（强制刷新） |

---

## 11. 仿真与真机差异

| 项 | Wokwi 仿真 | 真机 M1 |
| --- | --- | --- |
| 振动 | `tone()` 蜂鸣器响 1–3 声 | GPIO25 震动马达 |
| 收紧 | SG90 舵机 35° / 70° | 同左（微型舵机） |
| 冷热 | 蓝 / 红 LED 各亮 5 秒 | 双继电器换向 TEC，真实冷热 |
| 心率 | `mock_hr` 注入 | MAX30102 光学心率 |
| 上行手势 | 4 个按键 E1–E4 | 表带外表面电容触摸区（在研） |
| 表情 | SSD1306 + RoboEyes | 无 |
| broker | `broker-cn.emqx.io` | 默认 `broker.emqx.io`，联调时改一致 |

真机接线与烧录见 `firmware/hapticband/烧录与接线步骤.md`，方案背景见 [`docs/触觉手环-仿真方案-Wokwi.md`](触觉手环-仿真方案-Wokwi.md)。
