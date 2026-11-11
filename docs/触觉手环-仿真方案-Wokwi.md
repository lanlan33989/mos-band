# 触觉手环 · 在线仿真方案（Wokwi）v1

> 2026-11-05，项目方确认：真硬件暂缓，开发与演示全部转在线仿真。
> 本方案由 开发方 起草。执行优先级：11.7 汇报出料之后、原 U1-U3 时段（11.10 后）开工。

## 1. 目标与边界

- **做什么**：用在线仿真器跑 ESP32 固件 + 虚拟外设，替代实物手环，与现有软件流水线（pipeline / realtime_asr / local_server 调试网页）完成全链路闭环
- **不做什么**：不做 PCB、不买元器件、不追求腕戴形态；仿真环境里「外观逼真」不加分，**协议和数据正确才加分**
- **恢复条件**：真硬件线随时可复活——固件同一份代码，仿真只加开关（见 §5），协议零差异

## 2. 选型：为什么是 Wokwi

| 候选 | ESP32 | WiFi/MQTT | 虚拟外设 | 结论 |
|---|---|---|---|---|
| **Wokwi** | 原生支持（Arduino + MicroPython） | 支持，可连公网 broker | 舵机、MAX30102、按键、LED、蜂鸣器等，元件库最全 | **选它** |
| Tinkercad Circuits | 只支持 Arduino 系，无 ESP32 | 无 | 少 | 否 |
| Proteus | 第三方弱支持 | 难 | 丰富但收费 | 否 |
| SimulIDE | 无 ESP32 | — | — | 否 |
| Espressif 官方 QEMU | 支持 | 支持 | 无图形外设（没有舵机/传感器可视化） | 否（录不出素材） |
| Renode | 部分支持 | 支持 | 无可视化外设 | 否 |

选 Wokwi 的三个决定性理由：
1. **外设可视化**：舵机转动、LED 变色、蜂鸣器响——录屏素材直接可用（剪辑剪辑要的就是画面）
2. **有 MAX30102 元件**（`wokwi-max30102`），心率通道不用造假数据
3. **wokwi-cli 命令行可跑**，能无头运行 + 录制，也能挂 GitHub 自动化

## 3. 架构：仿真板与现有软件栈的对接

```
[浏览器调试网页 :8765] ←→ [PC 后端 local_server.py / pipeline]
                              │ 通道切 mqtt（后端零改动，代码已支持）
                              ▼
                     公网 MQTT broker（broker.emqx.io）
                              ▲
        wrist/cmd ▼ 订阅        ▲ wrist/bio 发布（后续 wrist/ui 上行）
                     [Wokwi 仿真 ESP32 运行 hapticband.ino]
                              │
                     虚拟外设：舵机/LED×3/蜂鸣器/按键×4/MAX30102
```

关键点：**后端一行不改**。`local_server.py` 已有 mqtt 下发通道；仿真板订阅的 `wrist/cmd`、发布的 `wrist/bio` 与真机协议完全一致。将来真硬件复活，软件侧同样零改动。

## 4. 虚拟外设映射表（对照真机物料 19 项）

| 原型件（采购编号） | Wokwi 元件 | 引脚（与真机一致） | 仿真里长什么样 |
|---|---|---|---|
| ESP32 开发板（1） | `wokwi-esp32-devkit-v1` | — | 主板 |
| 震动马达模块（7） | `wokwi-buzzer` | GPIO25 | vib1/2/3 → 响 1/2/3 声，录屏能听 |
| 继电器 A 凉（3） | `wokwi-led` 蓝色 | GPIO26 | cool → 蓝灯亮 5 秒自动灭 |
| 继电器 B 热（3） | `wokwi-led` 红色 | GPIO14 | warm → 红灯亮 5 秒自动灭 |
| SG90 舵机（8） | `wokwi-servo` | GPIO27 | strap1→35°、strap2→70°，画面可见 |
| MAX30102（9） | 无对应元件（wokwi-max30102 不存在，2026-11-05 实测） | GPIO21/22 (I2C) | 固件 `SIM_FAKE_HR` 兜底：62-108 随机游走，wrist/bio 协议照发；真机版走真传感器 |
| 触摸电极 E1-E4（16/17） | `wokwi-pushbutton` ×4 | GPIO4 / GPIO13 / GPIO32 / GPIO33 | 按住=激活，对应手势状态机输入 |
| TEC + 散热片（4）、电源（5/6）、面包板杜邦线（10/11）、胶带扎带（13/14/15）、导电布铜箔硅胶线（16/17/18）、TTP223（19） | 不需要 | — | 全部消失，这正是仿真省下的钱 |

注意：触摸引脚真机是 touchRead（电容值），仿真里按钮是数字电平——固件用 `SIM_MODE` 开关切换读取方式（§5），**状态机与手势逻辑完全共用**。

## 5. 固件复用策略：同一份 .ino，只加开关

在 `firmware/hapticband/hapticband.ino` 第 2 区加一段：

```cpp
#define SIM_MODE 1   // 1=Wokwi 仿真（按钮代触摸、蜂鸣器代马达）；0=真机。上传前只改这一处
```

- `SIM_MODE 1`：触摸读数走 `digitalRead`（按钮按下=激活，与 touchRead 判据同语义）；WiFi 填 Wokwi 网关提供的虚拟网络
- `SIM_MODE 0`：恢复 touchRead + 真机 WiFi 凭据，代码与现在完全一致
- 舵机/继电器/TEC 脉冲逻辑不用动：`digitalWrite(GPIO26/14)` 驱动 LED 和驱动继电器是同一段代码
- 上行协议 `wrist/ui`（`触觉上行通道-设计规范.md` v1.1 §4）在仿真里先跑通，真机上线直接继承

## 6. Wokwi 运行方式（两条路，推荐 A）

**方案 A：wokwi-cli（推荐）**
1. `npm i -g @wokwi/cli`，项目文件夹放 `diagram.json` + `hapticband.ino` + `libraries.txt`
2. `wokwi-cli .` 无头运行；`wokwi-cli . --scenario` 可做自动化
3. 好处：能挂 CI、能录屏、不需要开浏览器

**方案 B：纯浏览器（推荐先用这个）**
1. wokwi.com 注册（免费档够用），新建 ESP32 项目，粘贴 diagram.json 和代码
2. WiFi 用 `Wokwi-GUEST`（全大写 GUEST，密码留空）——免费公共网关支持出网连接，**不需要装任何本地软件**，直接点播放
3. Serial Monitor 看 `[动作]` 回显

> 网络说明（2026-11-05 已验证）：本地桌面网关 wokwigw 是「私有网关」= 付费功能，仅本地网络/端口转发需要；本项目连公网 broker.emqx.io 属于出网连接，免费档直接通。

## 7. 里程碑（对应原 U1-U3，11.10 后开工）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| W1 | diagram.json 搭好虚拟电路 + `SIM_MODE` 开关合入 + 七个输出跑通 | 网页调试台切 mqtt，发 vib1/2/3、strap1/2、cool/warm，蜂鸣器/舵机/LED 全部正确响应，串口打印 `[动作]` |
| W2 | 四按键手势状态机跑通 + `wrist/ui` 上行 | 六手势在 Wokwi 按键上全识别；连续 10 分钟快速乱按，误上行 <1 次 |
| W3 | 双向闭环演示 | 网页说「我很紧张」→ rub 语义下发安抚（cool 蓝灯亮）；再演示按键 rub → 软件收到应激事件 → 微凉通道触发——**双向闭环在录屏里完整可见** |

每步完成即录屏存档（给 BP 和神仙湖 PPT），素材要求：Wokwi 左、调试网页右，同屏分录。

## 8. 风险与预案

| 风险 | 概率 | 预案 |
|---|---|---|
| `wokwi-max30102` 与 SparkFun MAX3010x 库兼容问题 | **已发生**（元件不存在，整画布静默加载失败） | diagram.json 已移除 MAX30102，仿真版固件走随机心率发生器（62-108），协议不变；真机版仍走真库 |
| Wokwi 免费档限制（私有项目数） | 低 | 项目公开即可；或用 wokwi-cli 本地跑 |
| broker.emqx.io 公网延迟抖动 | 中 | 演示前 ping 一次；备用 broker：test.mosquitto.org 或本机 mosquitto（`local_server.py` 的 broker 参数可配） |
| touchRead 行为仿真失真 | 确认 | 已用按钮映射规避——这是仿真做不了电容感应的已知限制，写进说明文档 |
| 舵机 35°/70° 在画面上看不清 | 低 | 加两个角度标签文本元件，或串口同时打印角度值 |

## 9. 开工清单（W1 第一步，照做即可）

1. 注册 wokwi.com（或 `npm i -g @wokwi/cli`）
2. 新建 ESP32 项目，`libraries.txt` 写三行：`PubSubClient`、`ESP32Servo`、`SparkFun MAX3010x Pulse and Proximity Sensor Library`
3. 把 `firmware/hapticband/hapticband.ino` 复制进去，按 §5 加 `SIM_MODE`
4. 按 §4 映射表搭 `diagram.json`（开发方 可直接生成 starter 文件）
5. WiFi 填 Wokwi 虚拟网络凭据（Gateway 提供），broker 不变
6. 本机 `python local_server.py --mqtt`，网页调试台点「MQTT」通道
7. 对着网页手动测试按钮逐个点七个动作 → Wokwi 画面逐个核对

## 10. 真机物料说明

真硬件线暂缓；物料规格与选型注意事项见 `firmware/hapticband/烧录与接线步骤.md`，恢复硬件线时按该文档执行。
