# software 目录 · 傻瓜使用说明

> 状态：**M1 固件 + M2 软件流水线 全部完成（2026-11-05 第二次修订）**，硬件未到也能全跑
> 方案说明：见仓库根 `README.md` 与 `docs/` ｜ 固件：`../firmware/hapticband/hapticband.ino`
> 目标：今晚就能跑通「音频 → 云端 qwen3 ASR → 规则层 / DeepSeek LLM → 七个输出指令」，一步都不依赖硬件。
> 11.5 第二次修订：**ASR 只用云端 `qwen3-asr-flash`**（本地 faster-whisper 已完全移除）；**「全流程预录音频、不赌现场」红线作废**（允许联网 ASR 与现场录音）；`warm` 泛化为「现在的气氛融洽温和」；一期不换讯飞，理由见定稿 §五。

---

## 0. 一期七个输出（定死）

`vib1` 振动一下 ｜ `vib2` 振动两下 ｜ `vib3` 振动三下 ｜ `strap1` 收紧 1 档 ｜ `strap2` 收紧 2 档 ｜ `cool` 凉 ｜ `warm` 热

**下行协议**：`wrist/cmd` → `{"act":"vib2"}`
**上行协议**：`wrist/bio` → `{"ch":"bio","hr":92}`（每 2 秒，无效 -1，保持原样）

---

## 1. 今晚做完能拿到什么

| 打勾 | 结果 |
|---|---|
| [ ] | `dialogue.wav` 双角色对话音频（16kHz/16bit/单声道，默认演示输入） |
| [ ] | `dialogue_timeline.csv` 触发时间轴，与音频严格对齐 |
| [ ] | `asr_segments.csv` 云端 ASR 真实转写文本 + `emotion` 情感标签 |
| [ ] | `triggers.csv` 视频字幕源（时间 + 来源 + act + 中文 + 依据 + 指令 + 通道 + ASR情感 + LLM 复核） |
| [ ] | 屏幕打印 7 条触发，顺序 `vib1 → vib2 → vib3 → strap1 → strap2 → cool → warm` |
| [ ] | 把打印的 JSON 粘进 Arduino 串口监视器，手环真的动（硬件到了之后） |

---

## 2. 装 Python（只做一次，5 分钟）

1. 打开 `https://www.python.org/downloads/windows/`，下载 **Windows installer (64-bit)**（3.11 或 3.12）
2. 双击安装，**第一个界面最下面 `Add python.exe to PATH` 一定要勾上**（最容易漏）
3. 点 Install Now
4. 验证：`Win + R` → 输入 `cmd` 回车 → 敲 `python --version`
   **预期**：`Python 3.12.x`

> 提示「不是内部或外部命令」→ 卸载重装，这次务必勾 `Add python.exe to PATH`。

---

## 3. 三个一键脚本，按顺序双击

| 顺序 | 双击这个 | 要联网吗 | 预期看到 |
|---|---|---|---|
| 1 | **`一键装环境.bat`** | 要（装 pip 包） | 最后打印 `基础环境装好了。` |
| 2 | **`一键跑演示.bat`** | 第 1 步 TTS 要，**第 2 步零网络** | 打印 7 条触发 + `完成：7 个触发` |
| 3（可选） | **`一键跑全链路.bat`** | 要（云端 qwen3 ASR + DeepSeek LLM） | `[ASR] 云端转写 ... 情感标签=...`、`[LLM] ... 一致/复核=...` |

**顺序 2 就足够验收 M2**，第 3 步只是把真实云端 ASR 和 LLM 接上。**不用再装任何本地语音模型**：`faster-whisper` 与 `一键装ASR.bat` 已删除。任何一环失败都会**自动降级并继续跑完**，不会中断。

### `一键跑演示.bat` 预期输出（时间数字以实际语音长度为准）

```
==============================================================
 触觉社交手环 - 对话音频生成器
==============================================================
[1/3] 生成双角色语音（edge-tts 需要联网，已生成的会自动复用）
    [生成] S01 甲：你们这个手环，续航能到多少？
    [时间轴] S01 结束于 3.4 秒
    ...
完成：音频总时长 30.8 秒

==============================================================
 触觉社交手环 · M2 主流水线（模式：demo，下发：mock，ASR：云端 qwen3）
==============================================================
[ASR] 已按 --no-asr 跳过
[规则层] 独立判定 3 条（不依赖 LLM）：vib1, vib2, cool
[LLM] 未启用或无 key，跳过证据复核（规则层照常独立运行）
[1] t=5.4s  vib1 (振动一下)  来源=规则层
     依据：对方说完出现 2 秒静音轮到佩戴者
     LLM 复核：未复核
     指令：{"act":"vib1"}
[2] t=13.2s  vib2 (振动两下)  来源=规则层+LLM
     ...
[7] t=32.6s  warm (热)  来源=LLM+云端ASR情感标签
     依据：对话气氛融洽温和：达成共识语气缓和且近期无打断
     指令：{"act":"warm"}
--------------------------------------------------------------
完成：7 个触发，已写 triggers.csv（视频字幕源）
触发顺序：vib1 -> vib2 -> vib3 -> strap1 -> strap2 -> cool -> warm
```

**顺序对上就说明期望表、时间轴、指令、字幕四者一致。**

### `一键跑全链路.bat` 额外会看到

```
[ASR] 上传 dialogue.wav 到 qwen3-asr-flash ...
[ASR] 云端转写 156 字，情感标签=neutral（气氛融洽温和）
      官方说明：qwen3-asr-flash 不返回时间戳，时间用期望表时间轴对齐
[LLM] 对 7 个触发点做证据复核（含云端 ASR 情感标签 neutral）...
[7] t=32.6s  warm (热)  来源=LLM+云端ASR情感标签
     ASR 情感：neutral（融洽温和）
```

> 云端 ASR 官方限制：**不返回时间戳**，但**固定返回 `emotion` 情感标签**。所以「带时间戳文本」= 云端真实转写文本 + `dialogue_timeline.csv` 期望表时间轴对齐；`emotion` 用于 `warm` 的「气氛融洽温和」判定。

---

## 4. 手动命令（想单独跑某一步时用）

先 `Win + R` → `cmd` → `cd /d "D:\workbuddy\2026-11-04-12-18-44\software"` 回车。

| 命令 | 作用 |
|---|---|
| `python make_dialogue.py` | 生成对话音频 + 精确时间轴 |
| `python make_dialogue.py --force` | 改了台词后全部重生成 |
| `python pipeline.py --no-asr --no-llm --mock` | **零网络零依赖**，纯规则层，秒出结果 |
| `python pipeline.py --mock` | 默认：跑云端 ASR + LLM，只打印 |
| `python pipeline.py --asr cloud --llm --mock` | 明确指定云端 qwen3 + DeepSeek |
| `python pipeline.py --audio 现场录音.wav --llm --mock` | **换成任意 wav/mp3**（含现场录音） |
| `python pipeline.py --serial --port COM3 --no-asr --no-llm` | 串口直发 ESP32 |
| `python pipeline.py --mqtt --no-asr --no-llm` | 发到 MQTT broker |
| `python pipeline.py --live --llm --mock` | live 模式：规则层+LLM 实时判定，不用期望表 |
| `python pipeline.py --mock --realtime` | 按音频真实时间推进，适合彩排 |
| `python demo_chain.py --mock` | 轻量版：只看七个输出的指令 |

> `--asr local` 只是兼容旧命令的别名，会打印「本地 ASR 已移除」并自动转云端 qwen3，**不要再装本地模型**。

---

## 5. 软件栈（11.5 修订后，不用申请讯飞）

| 环节 | 用什么 | 说明 |
|---|---|---|
| TTS | `edge-tts` | 甲乙两种声音，生成默认演示音频；**只是默认输入，不再是红线** |
| ASR | **qwen3-asr-flash（阿里云百炼，云端，唯一）** | `input_audio.data` 用 Data URI `data:audio/wav;base64,...`；`asr_options` 顶层传 `language:"zh"`、`enable_itn:false` |
| 时间对齐 | **`dialogue_timeline.csv` 期望表时间轴** | 官方明确 qwen3-asr-flash 不返回时间戳，时间戳由我们自己的脚本时间轴给出 |
| 情感 | ASR 固定返回的 `emotion` | `choices[].message.annotations[].emotion`；`happy`/`neutral` 视为气氛融洽温和 |
| LLM | **DeepSeek**，OpenAI 兼容接口 | 标准库 urllib 调用，不用装 SDK；强制只输出 `{"act":"...","evidence":"..."}` |
| 规则层 | 纯 Python，不依赖任何外部服务 | 独立判定 `vib1`（静音 ≥1.5s）、`vib2`（问句后 ≥5s 未答）、`cool`（心率 > 静息+15） |
| 下发 | mock / 串口 / MQTT | 三条路走同一套 `{"act":...}` |

**降级阶梯**：云端 ASR 不通 / 无 key / 音频缺失 → 用期望表文本；LLM 不通 → 规则层 + 期望表照常出全部 7 个输出；精确时间轴不存在 → 按脚本 gap 估时间。**任何一环挂了都能完整演示。**

**为什么一期不用讯飞流式 ASR**：七个输出是事件级离散提示，触发时刻由期望表与规则层给出，讯飞最核心的词级时间戳优势用不上；qwen3 缺时间戳已用期望表时间轴补上，还白送 `emotion` 支撑 `warm`；接入成本差一个数量级且 key 现成。完整对比见 `方案定稿-2026-11-05.md` §五。**只有二期做 <1 秒真实时佩戴、需要逐字时间戳时才考虑流式 ASR。**

---

## 6. 配置文件与密钥

- `config.example.json` 是**模板，无 key**，可以随便看、可以进软著材料、可以镜像。
- `config.json` 是**真实配置**，已填好 DeepSeek 与云端 qwen3 ASR 凭据，**只在这个目录里存在一份**。

> **四条红线**：`config.json` 不要发给别人、不要进 git、不要进软著源码材料（软著要求删光 API key）、**不要复制到工作区镜像**。要换 key 只改这一个文件。

主要开关：

| 字段 | 作用 |
|---|---|
| `mode` | `demo`（期望表驱动，默认）／ `live`（实时判定） |
| `hr_source` | `mock`（默认，用 `mock_hr=108`）／ `mqtt`（11.7 真机联调订阅 `wrist/bio`） |
| `llm.enabled` | 关掉就完全不调 LLM |
| `asr.provider` | `cloud`（唯一 ASR）／ `none`（跳过）；`local` 为兼容别名，自动转 `cloud` |
| `asr.cloud.enabled` | 云端 ASR 总开关 |
| `asr.audio_path` | 默认演示音频 `dialogue.wav`；命令行 `--audio` 可覆盖 |
| `asr.cloud.base_url` | 通用域名 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `asr.cloud.base_url_alt` | 备用域名，填官方推荐的业务空间专属域名 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`；主域名失败自动重试。不知道 WorkspaceId 就留空，通用域名仍可用 |
| `serial.port` | 串口号，去 Arduino IDE「工具 → 端口」看 |

---

## 7. 让软件和硬件真对上（硬件到了之后）

### 方法 A：粘 JSON，最省事

1. Arduino IDE 打开串口监视器（115200、换行符），确认固件跑起来
2. 从流水线输出复制一行 `{"act":"vib2"}`
3. 粘进串口输入框按回车
4. **预期**：串口打印 `[动作] 振动 2 下`，马达震 2 下

### 方法 B：串口自动下发

1. **先关掉 Arduino IDE 串口监视器**（一个 COM 口只能被一个程序占用）
2. 记下 Arduino IDE「工具 → 端口」的 COM 号
3. `python pipeline.py --serial --port COM3 --no-asr --no-llm`
4. **预期**：打印板子开机日志 → `[串口] 固件已就绪` → 7 条指令依次下发，手环按序动作

### 方法 C：走 MQTT

1. 固件第 1 区填好 WiFi 重新上传，串口出现 `[MQTT] 连接 broker.emqx.io ... 成功`
2. `python pipeline.py --mqtt --no-asr --no-llm`
3. **预期**：串口打印 `[MQTT] 收到指令: {"act":"vib1"}`，手环按序动作

> 三条路走的是**同一套 JSON**，固件里 `handleCommand()` 是唯一入口。11.7 联调只换数据源，格式不用改。

---

## 8. 文件说明

| 文件 | 谁生成 | 干什么 |
|---|---|---|
| `dialogue_script.csv` | 手写 | **逐句触发期望表**，13 句 + 7 个触发，demo 模式唯一触发源 |
| `make_dialogue.py` | 已给 | TTS 生成 + 时间轴拼接 + 导出 wav/mp3/timeline |
| `pipeline.py` | 已给 | **M2 主流水线**：云端 ASR + LLM + 规则层 + 下发 + 字幕 CSV |
| `demo_chain.py` | 已给 | 轻量版指令下发 |
| `config.example.json` / `config.json` | 已给 | 模板 / 真实配置（含 key，不镜像） |
| `一键装环境.bat` | 已给 | 双击装依赖（**不需要装语音模型**） |
| `一键跑演示.bat` / `一键跑全链路.bat` | 已给 | 双击跑链路 |
| `dialogue.wav` / `dialogue.mp3` | 自动生成 | 默认演示对话音频 |
| `dialogue_timeline.csv` | 自动生成 | 每个触发的绝对时间 + act + 依据 + 指令 |
| `asr_segments.csv` | 自动生成 | 云端真实转写文本 + `emotion` + `language`（**无词级时间戳**） |
| `triggers.csv` | 自动生成 | **视频字幕源**，含 `ASR情感` 列 |
| `audio_parts/` | 自动生成 | 每句单独 mp3，断网重跑会复用 |

---

## 9. CSV 规范

### 9.1 `dialogue_script.csv` 列（10 列）

`cue_id, segment, speaker, text, gap_ms, act, source, evidence, cmd_json, note`

| 列 | 含义 |
|---|---|
| `cue_id` | 句子编号 S01~S13 |
| `segment` | 一律 `fullchain` |
| `speaker` | `甲` / `乙` 才会被 TTS 念出来；`系统` 表示纯停顿 |
| `text` | 台词；**留空 = 纯静音**（如 2 秒、6 秒） |
| `gap_ms` | 本句之前的静音毫秒数 |
| `act` | 七个输出 ID，留空 = 不触发 |
| `source` | `规则层` / `LLM` / `规则层+LLM` / `规则层(mock心率)` / `LLM+云端ASR情感标签`，字幕如实标注 |
| `evidence` | 对话原文引用（定位句：只断言可验证事实） |
| `cmd_json` | 标准 JSON `{"act":"..."}` |
| `note` | 备注，给验收和分镜看 |

### 9.2 七个动作在脚本里的位置

| act | 句子 | 可验证事实 |
|---|---|---|
| `vib1` | S02 | 对方说完出现 2 秒静音，轮到佩戴者 |
| `vib2` | S05 | 提问后 6 秒未答 |
| `vib3` | S07 | 首次出现金额硬信息 |
| `strap1` | S08 | 对方第一次重复同一诉求 |
| `strap2` | S10 | 诉求第三次重复 + 连续打断 |
| `cool` | S11 | mock 心率高于静息 +15 |
| `warm` | S13 | **现在的气氛融洽温和**：共识/致谢/肯定、语气缓和、ASR `emotion` 为 `happy`/`neutral`、近期无打断或重复诉求 |

> 为什么这样排：原任务书「60 秒心率 / 5 分钟应激」在 40 秒演示里不可能成立，且数字重复出现会污染触发顺序。现在七个动作各绑单一事件，按 1→7 顺序全出，字幕如实标注来源，**硬件是真动、不用动画假装**。`warm` 判的是文本与音频里可观察到的信号，不是猜测心思。

### 9.3 `triggers.csv` 列（视频字幕源，11 列）

`时间, 来源, act, 中文, 证据原文, 指令JSON, 通道, ASR情感, LLM证据, LLM复核, 备注`

### 9.4 `asr_segments.csv` 列（5 列）

`start_ms, end_ms, text, emotion, language`

> `start_ms` / `end_ms` 恒为 0：qwen3-asr-flash 官方不返回时间戳，时间由 `dialogue_timeline.csv` 对齐；`emotion` 只在第 1 行写入（整段音频一个标签）。要 ASR 自带时间戳得换 `qwen3-asr-flash-filetrans`（异步 + 公网 URL），同一把 key 即可，一期不需要。

`triggers.csv` 写的是 UTF-8 BOM，Excel 打开不乱码。11.8 剪视频按「时间」列铺字幕即可。

---

## 10. 常见故障

| 现象 | 原因 | 怎么做 |
|---|---|---|
| `[错误] 找不到 dialogue_script.csv` | 在别的目录运行的 | 双击 `一键跑演示.bat`（自动切到本目录） |
| 中文乱码 | 控制台代码页不一致 | 先敲 `chcp 65001` 再跑，或直接双击 `.bat` |
| `[生成] S01` 后报网络错 | edge-tts 连不上 | 换手机热点重跑，已生成句子会复用；**不管它也能跑完第 2 步** |
| `Couldnt find ffmpeg` | 依赖没装 | 双击 `一键装环境.bat`（`imageio-ffmpeg` 自带 ffmpeg，不用单独装） |
| `[降级] 云端 ASR 未启用或无 key` | `config.json` 没填 / 没复制到这个目录 | 检查 `asr.cloud.enabled` 与 `asr.cloud.api_key`；不确定就加 `--no-asr`，全流程照常跑完 |
| `[降级] 云端 ASR 调用失败` | 网络 / base_url / key 不对 | 核对 `asr.cloud.base_url`，换手机热点；不用切本地 ASR（已移除），加 `--no-asr` 即可 |
| `[降级] 音频 base64 后超过 10MB` | 音频太大 | 剪短或降采样；默认 `dialogue.wav` 远小于此 |
| `qwen3 没返回时间戳` | **官方行为，不是 bug** | 时间戳来自 `dialogue_timeline.csv`，字幕与触发不受影响 |
| `现场录音转写为空` | 格式/音量问题 | 转成 16kHz 单声道 wav 再跑；`--audio` 支持 wav/mp3 |
| `LLM 调用失败：timed out` | 网络慢 | 加 `--no-llm`，或用 `--no-asr --no-llm` 纯本地验证 |
| `打不开串口 COM3` | COM 号不对 / 被串口监视器占用 | 关掉串口监视器，去「工具 → 端口」看真实号 |
| 串口发了硬件没反应 | 行尾/波特率不对 | 手动贴一行 `{"act":"cool"}` 验证；确认 115200 + 换行符 |
| `config.json` 读不了 | JSON 格式错 | 检查是否少了逗号/引号；对照 `config.example.json` |
| `[降级] 握手失败：401 ... apikey not found`（二期） | `config.json` 的 `iat` 三件套抄错或没买服务 | **从控制台复制文本，别照截图手抄**（32 位密钥极易漏字符）；确认买的是「语音听写」而不是「实时语音转写」 |
| `[降级] 打不开麦克风` / `No module named 'sounddevice'`（二期） | 没装依赖或没有麦克风 | 双击 `一键装实时.bat`；没有麦克风就 `python realtime_asr.py --file dialogue.wav --mock` |
| IAT 跑到 60 秒断了（二期） | 官方单会话上限 | 程序已设 55 秒自动换会话，正常现象，不用管 |
| 转写为空但握手成功（二期） | 喂了纯静音/正弦波，或麦克风没声音 | 说中文试试；`code:0 success` 就说明协议没问题 |

---

## 11. 状态与后续

| 时间 | 状态 |
|---|---|
| 11.5 | M1 固件、接线文档、M2 软件流水线（云端 qwen3 版）—— **全部完成**；**二期「边说边出」按用户指令新增完成** |
| 11.6 | 源码冻结（软著 11.7 要交源码，之后只改配置不改代码）；文书线 |
| 11.7 | 只做真机联调 + 拍前彩排（硬件到了才做） |
| 11.8 | 拍剪视频 |
| 11.9 | 交珠海 + CCF |

**硬件未到期间可做**：Wokwi 跑真固件、`--mock` 全流程演示（含零网络版）、云端 ASR/LLM 实跑与 `emotion` 抽样、字幕 CSV 与分镜准备、接线照片级描述核对。

---

## 12. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-11-05 | 首版（参数化三通道 / 5 类 cue） |
| 2026-11-05 | **按《方案定稿》重做**：七个输出、协议改 `{"act":...}`、13 句新脚本、7 个触发；LLM 改 DeepSeek，取消讯飞；新增完整 `pipeline.py` 与全链路降级 |
| 2026-11-05 | **第二次修订**：ASR 全面改云端 `qwen3-asr-flash`（Data URI / 不返回时间戳 / 固定返回 `emotion`），删除本地 faster-whisper 与 `一键装ASR.bat`；「全流程预录、不赌现场」红线作废，新增 `--audio` 支持现场录音；`warm` 泛化为「气氛融洽温和」；`triggers.csv` 新增 `ASR情感` 列；明确一期不换讯飞 |
| 2026-11-05 | **按用户明确指令新增二期「边说边出」**：讯飞语音听写（流式版）IAT + 电脑麦克风，见 §13。一期 qwen3 路径完全不动 |
| 2026-11-06 | **调试台网页「按住说话」改为 `qwen3-asr-flash-realtime`**：后端代理 WebSocket，流式文字 + 7 类 emotion 一起回来，情感纳入 warm 判据；打字/文本/Word/音频上传与命令行 `realtime_asr.py`（讯飞 IAT）保持不变 |
| 2026-11-06 | **情感显式接入 LLM**：按句绑定 `emotion` 快照，注入触发后复核 `llm_review` 与三个慢脑提示词；`cool` 可用负向情感作应激佐证、`warm` 限 `happy`/`neutral`/无标签，其余五个输出与情感无关；七个输出 ID 与 `{"act":"..."}` 协议不变 |
| 2026-11-06 | **实验台新增说话人区分**：本地声纹模板（录入 8 秒 / 有效语音 ≥5 秒 / MFCC 32 维 / 余弦三态阈值），每句标 `[本人]` / `[对方]` / `[?]` 并按句绑定进 LLM 上下文；模板只存内存、重启失效；查证结论与契约见 `../说话人区分-设计规范.md`；七个输出与触发规则一律不变 |

---

## 13. 二期「边说边出」（讯飞语音听写，与一期并行）

> **用户 11.5 买了讯飞「语音听写」服务并明确指示开做。一期 `pipeline.py` 一行没改，两套并存。**

### 13.1 它解决什么

一期是「整段音频上传 → 一次返回 → 出七个输出」，适合拍摄验收；二期是「**对着电脑麦克风边说边出字 → 实时判定 → 立刻出七个输出**」，是真实对话的产品形态。两者共用同一套 `{"act":"..."}` 与固件。

### 13.2 三步跑起来

| 顺序 | 双击 | 说明 |
|---|---|---|
| 1 | `一键装实时.bat` | 装 `websocket-client` + `sounddevice`（前者通常已有） |
| 2 | `一键跑实时.bat` | 对着**电脑自带麦克风**说话，屏幕上边说边出字，触发时打印 `[触发]` 与指令 |
| 3 | 看 `realtime_triggers.csv` | 实时字幕与触发记录 |

不用买麦克风，手环上也没有麦克风（ESP32 侧只有 MAX30102 心率）。默认跑 55 秒自动收尾，也可按 Ctrl+C 提前结束。

### 13.3 手动命令

```
python realtime_asr.py --mock                       电脑麦克风，只打印
python realtime_asr.py --seconds 30 --mock          只录 30 秒
python realtime_asr.py --file dialogue.wav --mock   用已有 wav 模拟实时流，不需要麦克风
python realtime_asr.py --llm --mock                 加 DeepSeek 异步复核证据
python realtime_asr.py --serial --port COM3         真下发 ESP32
python realtime_asr.py --mqtt                       发到 broker.emqx.io
python realtime_asr.py --no-asr --mock              不连讯飞，只自检本地音频与规则层
```

`--file` 只接受 **16kHz / 16bit / 单声道 wav**；`dialogue.wav` 就是，`make_dialogue.py` 会生成。

### 13.4 官方协议与实测

- `wss://iat-api.xfyun.cn/v2/iat`，三件套 `APPID + APIKey + APISecret`，`hmac-sha256` 签名，date 为 RFC1123 GMT
- 40ms 发 1280 字节（16k/16bit/mono）；首帧带 `common+business+data(status=0)`，中间 `status=1`，末帧 `status=2`
- **单会话上限 60 秒**，程序 55 秒自动换新会话；`eos=10000` 防止一停顿就断
- 官方 `bg/ed` 与 `ws.bg` 只是端点帧偏移，**不是可靠词级时间戳**，所以本程序时间戳用本地时钟，不假装有
- **实测（11.5）**：三件套握手成功，首帧/中间帧/末帧全流程收到 `{"code":0,"message":"success",...,"data":{"status":2}}`。测试音频是纯正弦波所以转写为空，属正常；说中文就会出字

> 三件套**只写在 `config.json` 的 `iat` 段**，模板留空，不镜像、不进文档、不进 git、不进软著源码材料。**从控制台复制文本，别照截图手抄**：32 位密钥漏一个字符就会 `401 apikey not found`。

### 13.5 实时判定依据（仍只断言可验证事实）

| act | 实时可观察信号 |
|---|---|
| `vib1` | 一句说完、静音 ≥1.5 秒 |
| `vib2` | 问句后 ≥5 秒无新文本 |
| `vib3` | 首次出现金额/时间/承诺硬信息 |
| `strap1` / `strap2` | 同一诉求重复第 2 次 / 第 3 次以上 |
| `cool` | mock 心率超阈值（11.7 换 `wrist/bio`） |
| `warm` | 出现共识/致谢/肯定且近期无打断或重复诉求 |

每个动作有 8 秒冷却，避免连续刷屏；`--llm` 会把 DeepSeek 复核结论补写进 CSV，且不阻塞音频流。

---

## 14. 本地可视化调试网页（软硬分工一屏分清）

> 双击级入口：`python local_server.py`，浏览器开 `http://127.0.0.1:8765`。
> 用途：**现场排查时一眼分清是软件问题还是硬件问题**。

### 14.1 它是什么

一个本地网页（零前端框架，Python 标准库起服务）：

- **按住说话**（鼠标 / 触摸 / 空格键）→ 浏览器麦克风 16k PCM → 每 100ms 一块 POST 到后端 → 后端代理 **`qwen3-asr-flash-realtime`**（2026-11-06 切换；浏览器 WebSocket 不能自定义 `Authorization` 头，故密钥只在后端）→ 流式文字 + **7 类情感** 一并回来
- 增量文字实时上屏；最终句（带标点）走 `Judge.on_piece`，与打字/文本/音频喂入完全同一条判定与字幕链路
- **情感按句绑定并显式喂给 LLM**（2026-11-06 二次修订）：接收器在 `on_piece` 前把 `emotion` 写入 `PIECE_EMO`，`on_sentence_cb` 立刻取快照入 `SLOWQ`，慢脑 worker 不读全局值（避免异步串句）。三处 LLM 输入都带「该句音频情感标签」：① 触发后异步复核 `llm_review(act, emotion)`；② 双脑 `SLOW_STREAM_SYSTEM`；③ 纯慢脑 `SLOW_FULL_SYSTEM` / `SLOW_SILENT_SYSTEM`。规则层 `warm` 门槛仍保留（`happy` / `neutral` / 无标签才放行）
- **说话人区分（2026-11-06 新增，规范见 `../说话人区分-设计规范.md`）**：先点「录入本人声音（8 秒）」建立本地声纹模板（有效语音 ≥5 秒），之后每个 VAD 成句用 `software/speaker_id.py` 做 MFCC 余弦比对，字幕标 `[本人]` / `[对方]` / `[?]` 并显示 `score`。标签按句绑定进 `feed_piece` / `asr_text` 与四处 LLM 提示词。模板只存内存、不落盘不上传、重启失效；未录入一律 `unknown`，**不影响任何触发与下发**。百炼实时版本身不提供说话人字段，云端 diarization 与 ISI 声纹识别的官方规定与不适用理由见规范 §一
- 打字 / txt·md / Word / 音频上传链路**未改动**（音频仍走云端 `qwen3-asr-flash` 整段版；命令行 `realtime_asr.py` 仍为讯飞 IAT）
- 触发时：对应动作卡片高亮 + 指令日志 + 「软件侧已发」状态
- **串口模式会读固件回显**：出现 `[动作]` 即「固件已确认执行」→ 硬件侧绿灯
- 七个手动测试按钮：不经过语音直接下发，单独验硬件
- LLM 复核异步进行（deepseek-flash，**thinking 已关**），结论写进日志不阻塞

### 14.2 判读表（网页上也有）

| 现象 | 结论 |
|---|---|
| 出了字 | 实时 ASR（Qwen3-ASR-Flash-Realtime）通了（软件 OK） |
| 卡片亮 + 指令日志出现 | 判定与下发链路通（软件 OK） |
| 「固件已确认」但实物没动 | **硬件问题**（接线 / 供电 / 继电器极性） |
| 已发但无固件回显 | 固件没跑 / COM 号错 / 被串口监视器占用 |
| 连字都没有 | ASR / 网络问题（软件侧） |

### 14.3 启动参数

```
python local_server.py                    默认 mock，只可视化
python local_server.py --serial --port COM3   串口真下发 + 固件回显确认
python local_server.py --mqtt             MQTT 下发（无固件回执，验硬件请用串口模式）
python local_server.py --port 9000        换端口
```

### 14.4 LLM 关 thinking（11.5 实测）

DeepSeek 新 API（`deepseek-flash` / `deepseek-v4-pro`，**`deepseek-chat` / `deepseek-reasoner` 已下线**）**默认开思考链**。实时场景关思考：`config.json` 里 `llm.thinking: "disabled"`（默认已是），代码传官方参数 `"thinking": {"type":"disabled"}`。

**实测（11.5）**：同一句「预算是两千，周五要」——关思考 **0.67 秒**，开思考 2.26 秒，判定结果一致。

网页「事件日志与 LLM 复核」面板里有**思考链实时开关**（`POST /api/llm-thinking`）：点一下立即生效，不用刷新、不用重启、不改 `config.json`；默认关。


## 15. 整段喂入（不打字说话也能测：打字 / txt·md / Word / 音频）

> 2026-11-05 新增。入口在调试网页「语音输入」面板下半区。后端把全文拆成句子，
> 逐句喂进 Judge 判定层（流式吞入、每句即时触发），喂入结束后复用 6 秒静音窗口
> 落地 vib1 / warm / vib2。字幕区逐句显示，触发动作的那句标红。

### 15.1 三种入口

| 入口 | 端点 | 说明 |
|---|---|---|
| 手动打字 | POST /api/feed {text, source} | 文本框 + 确认喂入，≤5 万字 |
| txt / md | 同上（前端 FileReader 读文本） | 前端直接读，不走解析 |
| Word .docx | POST /api/feed-docx（二进制 + X-Filename 头） | 后端 zipfile 抽 word/document.xml，零第三方依赖；≤10MB |
| 音频 mp3/wav/m4a | POST /api/feed-audio（二进制 + X-Filename 头） | 复用 pipeline.run_asr_cloud（qwen3-asr-flash 整段上传），转写完自动逐句喂入；≤8MB |

### 15.2 实现要点（改这里先看）

- `local_server.py`：`split_sentences()`（按 。！？；\n 切句）、`docx_extract()`、
  `feed_session()`（0.6 秒/句的流式节奏；`FEED_ACTIVE` 防并发，会话进行中再喂返回 409）、
  三个新端点在 `do_POST`。**注意：do_POST 的 body 只读一次**（raw + json 两用），
  再加端点时不要重复读 `self.rfile`（会永久阻塞，2026-11-05 踩过）。
- 延迟型规则（vib1 / warm / vib2）依赖静音 tick：喂入结束设 `STOP_AT` 复用 tick_loop，
  因此会话结束标记 `feed_done` 延迟 6.5 秒发出，让字幕标红窗口覆盖延迟触发。
- 前端：`feed_start / feed_piece / feed_done` 三个 SSE 事件 + `trigger` 到达时给当前句加 `.hit`（红）。
- 手动测试按钮现在也广播 trigger（来源标"手动测试"），卡片会亮但不走 LLM 复核。

### 15.3 已知限制

- 音频转写是整段上传（十几秒等待），不是流式；转写完成才开始逐句判定
- qwen3-asr-flash 单次 ≤8MB（base64 后 10MB 上限），长录音先切分
- 同时只允许一个喂入会话；按住说话时喂入会被拒
- docx 里的表格文字按 XML 顺序抽出，复杂排版可能有粘连
