# mos-band · 触觉社交辅助手环

把面对面对话里值得知道的事件，翻译成手腕上的**七个离散触觉动作**。整套链路用 ESP32 固件 + [Wokwi](https://wokwi.com) 在线仿真 + 本地 Python 调试台即可复现，不需要先拿到真硬件。

> **状态**：样品验证阶段，未开模、未注册、未融资。仓库里的触发事件是占位演示规则，不是经过真人验证的有效信号；真机触控与光学心率在研。演示数据为 mock 与标准脚本，非真人实测。

---

## 1. 仓库里有什么

| 路径 | 内容 |
| --- | --- |
| `firmware/hapticband-sim/` | **Wokwi 仿真工程**：`sketch.ino`（Wokwi 入口）、`hapticband-sim.ino`（同一份固件正本）、`diagram.json`（虚拟电路）、`libraries.txt`（库依赖）、`W1上手步骤.md`（逐点击操作） |
| `firmware/hapticband/` | **真机 M1 固件**：七个输出版 `hapticband.ino` + `烧录与接线步骤.md`（装库、19 根线、编译上传、串口验收） |
| `software/` | **调试台后端 + 前端 + 流水线**：`local_server.py`（HTTP/SSE/MQTT/实时 ASR 代理）、`pipeline.py`、`realtime_asr.py`、`speaker_id.py`、`demo_chain.py`、`make_dialogue.py`、`web/index.html`、5 个一键 bat、配置模板 |
| `docs/Wokwi-导入全指引.md` | **Wokwi 全量导入指引**：三种导入方式、网络与 broker、库依赖、topic、验收清单、重启流程、常见问题 |
| `docs/触觉手环-仿真方案-Wokwi.md` | 仿真选型与架构说明（原始方案文档） |
| `docs/触觉上行通道-设计规范.md` | 六种上行手势语义与判定窗口 |
| `docs/说话人区分-设计规范.md` | 本地声纹方案、能力边界与官方依据 |
| `software/README.md` | 调试台完整使用说明、配置红线、运行与判读表 |

**未包含**：比赛材料、商业文档、物料资料、视频/音频/图片素材、内部决策文档、运行时日志与记忆数据、`config.json`。历史文档中若引用了这些内部文件，以本 README 和 `docs/` 为准。

---

## 2. 快速开始

### 2.1 先把 Wokwi 跑起来

按 [`docs/Wokwi-导入全指引.md`](docs/Wokwi-导入全指引.md) 操作。最快路径：

1. 打开 wokwi.com → New Project → ESP32
2. 把 `firmware/hapticband-sim/sketch.ino` 全文粘进左侧 `sketch.ino`
3. 右上角下拉 → New Tab，分别建 `diagram.json`、`libraries.txt`，粘入同目录两个文件的全文
4. 点绿色播放键编译运行

串口出现 `[固件版本] 2026-11-10c ...`、`[WiFi] 连上了`、`[MQTT] 已订阅 wrist/cmd` 即为成功。

### 2.2 再启本机调试台

```bash
pip install -r requirements.txt          # Windows 也可双击 software/一键装环境.bat
cp software/config.example.json software/config.json   # Windows: copy
python software/local_server.py --mqtt
```

浏览器打开 `http://127.0.0.1:8765`，在通道里选 **MQTT**。不填任何密钥也能用 demo / mock 模式跑完七个输出与心率演示；云端 ASR 与 LLM 需要自行在 `config.json` 填自己的 key。

> `config.json` 含密钥，**永远不要提交**。`.gitignore` 已排除；仓库只提供 `config.example.json` 模板。

---

## 3. 七个输出与协议

| `act` | 语义 | 仿真里的表现 |
| --- | --- | --- |
| `vib1` / `vib2` / `vib3` | 振动 1 / 2 / 3 下 | 蜂鸣器响 1 / 2 / 3 声 |
| `strap1` / `strap2` | 表带收紧 1 档 / 2 档 | 舵机转 35° / 70° |
| `cool` | 凉 | 蓝 LED 亮 5 秒 |
| `warm` | 热 | 红 LED 亮 5 秒 |

| Topic | 方向 | 内容 |
| --- | --- | --- |
| `wrist/cmd` | 后端 → 固件 | `{"act":"vib2"}`；维护指令 `{"ch":"sys","cmd":"long"}` |
| `wrist/bio` | 固件 → 后端 | `{"ch":"bio","hr":92}`，每 2 秒，无效值 -1 |
| `wrist/ui` | 固件 → 后端 | 六手势 `tap1` / `tap2` / `long` / `swipe_up` / `swipe_down` / `rub` |

**broker 统一用 `broker-cn.emqx.io:1883`**（公共 EMQX）。Wokwi 里国际端点 `broker.emqx.io` 连不上（返回 -2），真机固件默认用国际端点，如需与仿真联通请改成同一端点。

心率节律：`mock_hr ≥ 95` 触发固定 7 下 `vib1`（30 BPM 节律），打完后冻结 600 秒；调试台「解除冻结」按钮可立即重置。温度通道与振动/收紧互斥，`vib*` / `strap*` 到达时会先关掉温度 LED。

---

## 4. 真机

`firmware/hapticband/hapticband.ino` 是真机版：ESP32 + 震动马达 + 双继电器换向 TEC + SG90 舵机 + MAX30102。逐根接线、装库、编译上传与 115200 串口验收见 [`firmware/hapticband/烧录与接线步骤.md`](firmware/hapticband/烧录与接线步骤.md)。

真机与仿真的 4 处差异：`tone()` 蜂鸣器代马达、模拟心率代光学传感器、四按键代电容触摸区、LED 极性。仿真版额外带 SSD1306 表情屏（RoboEyes）。

---

## 5. 安全与隐私

- `software/config.json`（真实密钥）、`.env`、运行时 CSV / 日志 / 音频均被 `.gitignore` 排除，仓库只含空值模板。
- 本地说话人区分（`speaker_id.py`）的声纹模板**只存内存、不落盘、不上传、重启失效**；它只做演示级的本人/他人/未判定三态，不是身份认证。
- 云端 ASR / LLM 会把音频与文本发往你自己在 `config.json` 里配置的服务商；不填则使用离线 demo 数据。
- 请勿把任何真实密钥、页面快照或含凭据的截图提交进本仓库。

---

## 6. 许可证

[MIT](LICENSE)。硬件接线与固件按现状提供，用于学习与原型验证，不构成医疗或安全设备。

欢迎 issue / PR。若你fork后改了触发规则，建议同时更新 `docs/` 里的规范说明。
