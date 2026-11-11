# -*- coding: utf-8 -*-
"""
触觉社交手环 · M2 主流水线（硬件未到也能全跑）

链路：音频（默认预录，也可用现场录音）
      -> qwen3-asr-flash 云端 ASR（转写文本 + emotion 情感标签）
      -> 规则层 + DeepSeek LLM 证据复核
      -> 一期七个输出之一
      -> 下发（mock / 串口 / MQTT）
      -> triggers.csv（视频字幕源）

一期七个输出：vib1 vib2 vib3 strap1 strap2 cool warm
下行协议：wrist/cmd  {"act":"vib2"}      上行：wrist/bio  {"ch":"bio","hr":92}

重要事实（已核实阿里云官方文档）：
  qwen3-asr-flash 通过 OpenAI 兼容接口【不返回时间戳】，但【固定返回 emotion 情感标签】。
  因此  「带时间戳文本」= 云端真实转写 + 期望表时间轴对齐；emotion 用于 warm 的
  「气氛融洽温和」判定。要 ASR 自带时间戳需换 qwen3-asr-flash-filetrans（异步+公网 URL）。
  官方推荐业务空间专属域名，config 里 base_url_alt 填入后主域名失败会自动重试。

「全流程预录音频、不赌实时麦克风」红线已作废：允许联网云端 ASR 与现场录音输入。
默认仍用 dialogue.wav（可控、便于字幕），但 --audio 可指定任意 wav/mp3。

怎么跑（在 software 文件夹）：
    python pipeline.py --no-asr --no-llm --mock      纯本地零网络，秒出结果
    python pipeline.py --mock                        默认：跑云端 ASR + LLM，只打印
    python pipeline.py --asr cloud --llm --mock      明确指定云端 ASR + LLM
    python pipeline.py --audio 现场录音.wav --mock   换成任意音频（含现场录音）
    python pipeline.py --serial --port COM3          串口直发 ESP32
    python pipeline.py --mqtt                       发到 broker.emqx.io
    python pipeline.py --live --llm --mock           live 模式（不依赖期望表）

降级原则：音频不存在 / 云端不通 / LLM 不通 —— 任何一环失败都只打印提示，全流程继续跑完。
真实 key 只在 config.json，本文件不含任何密钥。
"""

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent

ACTS = {
    "vib1":   ("振动一下", "震动"),
    "vib2":   ("振动两下", "震动"),
    "vib3":   ("振动三下", "震动"),
    "strap1": ("收紧 1 档", "收紧"),
    "strap2": ("收紧 2 档", "收紧"),
    "cool":   ("凉", "温度"),
    "warm":   ("热", "温度"),
}

GENTLE_EMOTIONS = {"happy", "neutral"}

PROMPT = """你是触觉社交手环的对话事件判定器。
定位：不猜测对方心思，只断言对话中可验证的事实。
只输出 JSON：{"act":"...","evidence":"对话原文引用"}
act 只能是以下八选一：
vib1   对方一句话说完、出现 >=1.5 秒静音，轮到佩戴者
vib2   对方提问后 >=5 秒佩戴者未答
vib3   对话首次出现承诺或数字（时间、金额、保证）
strap1 佩戴者说话占比>70%，或对方第一次重复同一诉求
strap2 对方重复同一诉求>=2次，或连续打断2次
cool   心率高于静息+15（应激）
warm   对话气氛融洽温和：语气缓和、出现共识/致谢/肯定，音频情感标签为 happy 或 neutral，且近期无打断或重复诉求。判定对象是文本与音频里可观察到的信号，不是猜测心思
none   以上都不成立
evidence 必须是对话原文片段，不要解释、不要推测心理、不要输出 JSON 以外任何内容。"""


def setup_console():
    if os.name != "nt":
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleOutputCP() == 65001:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_config():
    p = BASE / "config.json"
    if not p.exists():
        print("[提示] 没有 config.json，改用 config.example.json（无 key，云端 ASR/LLM 自动跳过）")
        p = BASE / "config.example.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print("[降级] 配置读不了：" + str(e) + "，用空配置继续")
        return {}


def read_csv(name):
    p = BASE / name
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print("[降级] " + name + " 读不了：" + str(e))
        return []


def write_csv(name, fields, rows):
    p = BASE / name
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        w.writerows(rows)
    return p


def get_path(cfg, key, default):
    return cfg.get("paths", {}).get(key, default)


# ---------------------------------------------------------------------------
# 时间轴：优先用 make_dialogue.py 生成的精确时间轴；没有就按脚本 gap 估算
# ---------------------------------------------------------------------------
def load_timeline(cfg, script):
    rows = read_csv(get_path(cfg, "timeline_csv", "dialogue_timeline.csv"))
    if rows:
        out = []
        for r in rows:
            act = (r.get("act") or r.get("cue") or "").strip()
            cmd = (r.get("cmd_json") or "").strip()
            out.append({
                "t_ms": int((r.get("t_ms") or "-1").strip() or -1),
                "cue_id": (r.get("cue_id") or "").strip(),
                "act": act,
                "source": (r.get("source") or "").strip(),
                "evidence": (r.get("evidence") or "").strip(),
                "cmd_json": cmd,
                "text": (r.get("text") or "").strip(),
                "note": (r.get("note") or "").strip(),
            })
        return out

    print("[降级] 没有 dialogue_timeline.csv，按脚本 gap 估时间轴（跑 make_dialogue.py 后有精确值）")
    t = 0
    out = []
    for r in script:
        t += int((r.get("gap_ms") or "0").strip() or 0)
        if (r.get("text") or "").strip():
            t += 3000
        out.append({
            "t_ms": t, "cue_id": (r.get("cue_id") or "").strip(),
            "act": (r.get("act") or "").strip(),
            "source": (r.get("source") or "").strip(),
            "evidence": (r.get("evidence") or "").strip(),
            "cmd_json": (r.get("cmd_json") or "").strip(),
            "text": (r.get("text") or "").strip(),
            "note": (r.get("note") or "").strip(),
        })
    return out


# ---------------------------------------------------------------------------
# 云端 ASR：qwen3-asr-flash（阿里云百炼 OpenAI 兼容接口）
# 官方明确：本模型不返回时间戳；但固定返回 emotion 情感标签
# input_audio.data 必须是 Data URI：data:audio/wav;base64,<...>
# ---------------------------------------------------------------------------
def run_asr_cloud(cfg, audio_override=""):
    cloud = cfg.get("asr", {}).get("cloud", {})
    if not cloud.get("enabled") or not cloud.get("api_key"):
        print("[降级] 云端 ASR 未启用或无 key，跳过（规则层照常独立运行）")
        return [], {}

    name = audio_override or cfg.get("asr", {}).get("audio_path", "dialogue.wav")
    audio = Path(name)
    if not audio.is_absolute():
        audio = BASE / audio
    if not audio.exists():
        print("[降级] 找不到音频 " + audio.name + "，跳过 ASR（先跑 make_dialogue.py，或用 --audio 指定）")
        return [], {}

    max_mb = float(cfg.get("asr", {}).get("audio_max_mb", 10))
    b64 = base64.b64encode(audio.read_bytes()).decode("ascii")
    if len(b64) / 1024 / 1024 > max_mb:
        print("[降级] 音频 base64 后超过 " + str(max_mb) + "MB 上限，跳过 ASR")
        return [], {}

    mime = "audio/wav" if audio.suffix.lower() == ".wav" else "audio/mpeg"
    data_uri = "data:" + mime + ";base64," + b64
    body = {
        "model": cloud.get("model", "qwen3-asr-flash"),
        "messages": [{
            "role": "user",
            "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
        }],
        "stream": False,
        "asr_options": {
            "language": cloud.get("language", "zh"),
            "enable_itn": bool(cloud.get("enable_itn", False)),
        },
    }
    bases = [b.rstrip("/") for b in
             [cloud.get("base_url", ""), cloud.get("base_url_alt", "")] if b.strip()]
    if not bases:
        bases = ["https://dashscope.aliyuncs.com/compatible-mode/v1"]
    payload = json.dumps(body).encode("utf-8")
    data = None
    for base in bases:
        url = base + "/chat/completions"
        try:
            print("[ASR] 上传 " + audio.name + " 到 qwen3-asr-flash（" + base + "）...")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + cloud["api_key"]})
            with urllib.request.urlopen(req, timeout=cloud.get("timeout_sec", 90)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:
            print("[降级] " + base + " 调用失败：" + str(e) + " —— 尝试备用域名或直接降级")
    if data is None:
        print("[降级] 云端 ASR 全部域名调用失败 —— 用期望表文本继续，全流程不中断")
        return [], {}

    # 兼容 OpenAI 兼容格式 与 DashScope 同步格式
    msg = {}
    try:
        msg = data["choices"][0]["message"]
    except Exception:
        try:
            msg = data["output"]["choices"][0]["message"]
        except Exception:
            pass
    content = msg.get("content", "")
    if isinstance(content, list):
        text = "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    else:
        text = str(content)
    text = text.strip()

    emotion = ""
    language = ""
    for a in msg.get("annotations", []) or []:
        if isinstance(a, dict):
            emotion = emotion or str(a.get("emotion", ""))
            language = language or str(a.get("language", ""))

    if not text:
        print("[降级] 云端 ASR 返回空文本，用期望表文本继续")
        return [], {}

    parts = [p.strip() for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]
    segments = [{"start_ms": 0, "end_ms": 0, "text": p} for p in parts] or [
        {"start_ms": 0, "end_ms": 0, "text": text}]
    write_csv(get_path(cfg, "asr_csv", "asr_segments.csv"),
              ["start_ms", "end_ms", "text", "emotion", "language"],
              [[s["start_ms"], s["end_ms"], s["text"],
                emotion if i == 0 else "", language if i == 0 else ""]
               for i, s in enumerate(segments)])
    meta = {"emotion": emotion, "language": language, "text": text,
            "gentle": emotion in GENTLE_EMOTIONS}
    print("[ASR] 云端转写 " + str(len(text)) + " 字，情感标签=" + (emotion or "无") +
          ("（气氛融洽温和）" if meta["gentle"] else ""))
    print("      官方说明：qwen3-asr-flash 不返回时间戳，时间用期望表时间轴对齐")
    return segments, meta


def run_asr(cfg, args):
    if args.no_asr:
        print("[ASR] 已按 --no-asr 跳过")
        return [], {}
    provider = args.asr_provider or cfg.get("asr", {}).get("provider", "cloud")
    if provider == "none":
        print("[ASR] 已按参数跳过")
        return [], {}
    if provider == "local":
        print("[提示] 本地 faster-whisper 已移除（判定不可靠），自动改用云端 qwen3-asr-flash")
    return run_asr_cloud(cfg, args.audio)


# ---------------------------------------------------------------------------
# LLM：DeepSeek，OpenAI 兼容接口，标准库即可；强制 JSON 输出
# ---------------------------------------------------------------------------
def extract_json(text):
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            pass
    return {}


def llm_review(cfg, text, expect_act, emotion=""):
    llm = cfg.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key"):
        return "", "", "未配置"
    extra = ("音频情感标签：" + emotion + "（happy/neutral 表示气氛融洽温和）。") if emotion else ""
    body = {
        "model": llm.get("model", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": extra + "对话原文：" + text + "\n只输出 JSON。"},
        ],
        "temperature": llm.get("temperature", 0.2),
        "response_format": {"type": "json_object"},
        # DeepSeek 新 API（deepseek-flash / v4-pro）默认开思考链，实时场景太慢；
        # 官方参数 thinking.type=disabled 即关闭，可用 llm.thinking 覆盖（disabled/enabled）。
        "thinking": {"type": llm.get("thinking", "disabled")},
    }
    url = llm.get("base_url", "").rstrip("/") + "/chat/completions"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + llm["api_key"]})
        with urllib.request.urlopen(req, timeout=llm.get("timeout_sec", 20)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        obj = extract_json(data["choices"][0]["message"]["content"])
        act = str(obj.get("act", "")).strip()
        ev = str(obj.get("evidence", "")).strip()
        if act not in ACTS and act != "none":
            act = ""
        verdict = "一致" if act == expect_act else ("复核=" + (act or "无"))
        return act, ev, verdict
    except Exception as e:
        return "", "", "调用失败：" + str(e)[:60]


def run_llm_reviews(cfg, timeline, args, emotion=""):
    if args.no_llm:
        return {}
    llm = cfg.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key"):
        print("[LLM] 未启用或无 key，跳过证据复核（规则层照常独立运行）")
        return {}
    n = sum(1 for t in timeline if t["act"] in ACTS)
    print("[LLM] 对 " + str(n) + " 个触发点做证据复核" +
          ("（含云端 ASR 情感标签 " + emotion + "）" if emotion else "") + "...")
    out = {}
    for t in timeline:
        if t["act"] not in ACTS:
            continue
        text = t["text"] or t["evidence"] or t["act"]
        a, e, v = llm_review(cfg, text, t["act"], emotion)
        out[t["cue_id"]] = (a, e, v)
        print("    " + t["cue_id"] + " 期望=" + t["act"] + " " + v)
    return out


# ---------------------------------------------------------------------------
# 规则层：完全不依赖 ASR / LLM，独立推导 vib1 / vib2 / cool
# ---------------------------------------------------------------------------
def rule_layer(script, cfg):
    hr = cfg.get("mock_hr", 108)
    resting = cfg.get("resting_hr", 80)
    delta = cfg.get("threshold_hr_delta", 15)
    source = cfg.get("hr_source", "mock")
    hits = []
    prev_text = ""
    prev_speaker = ""
    for r in script:
        act = (r.get("act") or "").strip()
        gap = int((r.get("gap_ms") or "0").strip() or 0)
        text = (r.get("text") or "").strip()
        speaker = (r.get("speaker") or "").strip()
        reason = ""
        ok = False
        if act == "vib1":
            ok = (prev_speaker == "甲" and gap >= 1500)
            reason = "对方说完后静音 " + str(gap) + " 毫秒"
        elif act == "vib2":
            ok = ("？" in prev_text or "?" in prev_text) and gap >= 5000
            reason = "问句后 " + str(gap) + " 毫秒未答"
        elif act == "cool":
            if source == "mock":
                ok = (hr - resting) >= delta
                reason = "mock 心率 " + str(hr) + " 高于静息 " + str(resting) + " 达 " + str(delta)
            else:
                ok = True
                reason = "心率来自 wrist/bio 订阅（11.7 真机联调）"
        if act and ok:
            hits.append({"cue_id": (r.get("cue_id") or "").strip(),
                         "act": act, "reason": reason})
        if text:
            prev_text = text
            prev_speaker = speaker
    return hits


# ---------------------------------------------------------------------------
# 下发：mock / 串口 / MQTT
# ---------------------------------------------------------------------------
def make_sender(mode, cfg, args):
    if mode == "mock":
        return lambda cmd: None
    if mode == "serial":
        try:
            import serial
        except ImportError:
            print("[降级] 缺 pyserial，改为只打印")
            return lambda cmd: None
        port = args.port or cfg.get("serial", {}).get("port", "COM3")
        baud = cfg.get("serial", {}).get("baud", 115200)
        try:
            s = serial.Serial(port, baud, timeout=0.5)
        except Exception as e:
            print("[降级] 打不开 " + port + "：" + str(e) + "，改为只打印")
            return lambda cmd: None
        print("[串口] 等待固件启动日志（最多 20 秒）...")
        deadline = time.time() + 20
        while time.time() < deadline:
            line = s.readline().decode("utf-8", errors="ignore")
            if "七个输出" in line or "演示模式命令" in line:
                print("[串口] 固件已就绪")
                break
        def send(cmd):
            s.write((cmd + "\n").encode("utf-8"))
            s.flush()
        return send
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[降级] 缺 paho-mqtt，改为只打印")
        return lambda cmd: None
    m = cfg.get("mqtt", {})
    broker = m.get("broker", "broker.emqx.io")
    topic = m.get("topic_cmd", "wrist/cmd")
    cid = m.get("client_id", "pipeline-pc") + "-" + str(int(time.time()))
    try:
        try:
            c = mqtt.Client(client_id=cid)
        except Exception:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)
        c.connect(broker, m.get("port", 1883), 60)
        c.loop_start()
    except Exception as e:
        print("[降级] MQTT 连不上：" + str(e) + "，改为只打印")
        return lambda cmd: None
    print("[MQTT] 已连接 " + broker + "，发布 " + topic)
    return lambda cmd: c.publish(topic, cmd)


# ---------------------------------------------------------------------------
def main():
    setup_console()
    ap = argparse.ArgumentParser(description="触觉社交手环 M2 主流水线（七个输出 / 云端 ASR）")
    ap.add_argument("--mock", action="store_true", help="只打印不发指令（默认）")
    ap.add_argument("--serial", action="store_true", help="从串口发给 ESP32")
    ap.add_argument("--mqtt", action="store_true", help="发到 MQTT broker")
    ap.add_argument("--port", default="", help="串口号，如 COM3")
    ap.add_argument("--no-asr", action="store_true", help="跳过云端 ASR")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM")
    ap.add_argument("--asr", dest="asr_provider", choices=["cloud", "local", "none"], default="",
                    help="cloud=云端 qwen3（默认）；local=已移除会自动转 cloud；none=跳过")
    ap.add_argument("--audio", default="", help="指定任意 wav/mp3（含现场录音），覆盖 config 的 audio_path")
    ap.add_argument("--llm", action="store_true", help="显式启用 LLM 复核")
    ap.add_argument("--live", action="store_true", help="live 模式：规则层+LLM 实时判定")
    ap.add_argument("--realtime", action="store_true", help="按音频真实时间推进")
    args = ap.parse_args()

    mode = "mqtt" if args.mqtt else ("serial" if args.serial else "mock")
    if args.llm:
        args.no_llm = False

    print("=" * 62)
    print(" 触觉社交手环 · M2 主流水线（模式：" + ("live" if args.live else "demo") +
          "，下发：" + mode + "，ASR：云端 qwen3）")
    print("=" * 62)

    cfg = load_config()
    script = read_csv(get_path(cfg, "script_csv", "dialogue_script.csv"))
    if not script:
        raise SystemExit("[错误] 找不到 dialogue_script.csv。请确认它和本脚本在同一文件夹。")
    timeline = load_timeline(cfg, script)

    asr_segments, asr_meta = run_asr(cfg, args)
    emotion = asr_meta.get("emotion", "")

    rule_hits = rule_layer(script, cfg)
    print("[规则层] 独立判定 " + str(len(rule_hits)) + " 条（不依赖 LLM）：" +
          ", ".join(h["act"] for h in rule_hits))

    reviews = run_llm_reviews(cfg, timeline, args, emotion)

    triggers = [t for t in timeline if t["act"] in ACTS and t["cmd_json"]]
    if args.live and asr_segments:
        triggers = []
        for i, seg in enumerate(asr_segments):
            if args.no_llm:
                continue
            act, ev, _v = llm_review(cfg, seg["text"], "", emotion)
            if act in ACTS:
                triggers.append({
                    "t_ms": seg["start_ms"], "cue_id": "L" + str(i + 1), "act": act,
                    "source": "LLM", "evidence": ev or seg["text"],
                    "cmd_json": json.dumps({"act": act}, ensure_ascii=False),
                    "text": seg["text"], "note": "live 模式实时判定",
                })
        print("[live] 实时判定得到 " + str(len(triggers)) + " 条触发")

    send = make_sender(mode, cfg, args)
    t0 = time.time()
    rows = []
    for i, t in enumerate(triggers, 1):
        if args.realtime and t["t_ms"] >= 0:
            wait = t0 + t["t_ms"] / 1000.0 - time.time()
            if wait > 0:
                time.sleep(wait)
        tstr = format(t["t_ms"] / 1000.0, ".1f") + "s" if t["t_ms"] >= 0 else "-"
        cn, ch = ACTS.get(t["act"], (t["act"], ""))
        rv = reviews.get(t["cue_id"], ("", "", "未复核"))
        cmd = t["cmd_json"] or json.dumps({"act": t["act"]}, ensure_ascii=False)
        print("[" + str(i) + "] t=" + tstr + "  " + t["act"] + " (" + cn + ")  来源=" +
              (t["source"] or "期望表"))
        print("     依据：" + (t["evidence"] or "无"))
        if emotion:
            print("     ASR 情感：" + emotion + ("（融洽温和）" if asr_meta.get("gentle") else "（不温和）"))
        print("     LLM 复核：" + rv[2])
        print("     指令：" + cmd)
        send(cmd)
        rows.append([tstr, t["source"] or "期望表", t["act"], cn,
                     t["evidence"], cmd, ch, emotion, rv[1] or "", rv[2], t["note"]])

    out = write_csv(get_path(cfg, "triggers_csv", "triggers.csv"),
                    ["时间", "来源", "act", "中文", "证据原文", "指令JSON", "通道",
                     "ASR情感", "LLM证据", "LLM复核", "备注"], rows)
    if mode == "mqtt":
        time.sleep(0.5)

    print("-" * 62)
    print("完成：" + str(len(rows)) + " 个触发，已写 " + out.name + "（视频字幕源）")
    print("触发顺序：" + " -> ".join(r[2] for r in rows))
    if mode == "mock":
        print("mock 模式只打印没发指令。把 JSON 粘进 Arduino 串口就能驱动真硬件。")
    print("-" * 62)


if __name__ == "__main__":
    main()
