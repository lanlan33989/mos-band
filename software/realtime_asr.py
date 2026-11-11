# -*- coding: utf-8 -*-
"""
触觉社交手环 · 二期「边说边出」（讯飞语音听写流式版 IAT + 电脑麦克风）

与一期的关系：互不改动，并行存在
  一期 demo(pipeline.py)  : 整段音频 -> 云端 qwen3-asr-flash 一次返回 -> 七个输出
  二期实时(本文件)        : 电脑麦克风持续音频流 -> 讯飞 IAT 边说边出 -> 同一套
                            {"act":"..."} -> 同一套下发(mock/串口/MQTT) -> 实时字幕 CSV
  方案定稿 §一 的七个输出与协议完全不变；一期仍用 qwen3（定稿 §五 结论不变）。

官方协议（已核实 https://www.xfyun.cn/doc/asr/voicedictation/API.html）：
  接口    wss://iat-api.xfyun.cn/v2/iat
  鉴权    APPID + APIKey + APISecret 三件套，hmac-sha256 签名，date 为 RFC1123 GMT
  音频    16k/16bit/单声道 PCM，建议每 40ms 发 1280 字节
  首帧    common + business + data(status=0)；中间帧 data(status=1)；末帧 data(status=2)
  上限    单会话最长 60s，超过 10s 不发数据会断开；eos 默认 2000ms 静音会自动结束
  返回    data.result.ws[].cw[].w 为增量文本，data.status=2 表示本片结束
  注意    官方 bg/ed 与 ws.bg 均为端点帧偏移（1帧=10ms），不是可靠的词级时间戳；
          因此本文件的时间戳用本地时钟与音频帧计数，不假装 ASR 给了词级时间

怎么跑（在 software 文件夹）：
    先双击 一键装实时.bat（装 sounddevice；websocket-client 通常已有）
    python realtime_asr.py --mock                       电脑麦克风，默认 55 秒，只打印不下发
    python realtime_asr.py --mic --seconds 30 --mock    指定录制时长
    python realtime_asr.py --file dialogue.wav --mock   用 wav 模拟实时流，不需要麦克风
    python realtime_asr.py --llm --mock                 额外用 DeepSeek 异步复核证据
    python realtime_asr.py --serial --port COM3         真下发 ESP32
    python realtime_asr.py --mqtt                       发到 broker.emqx.io

降级原则：没装 sounddevice / 没麦克风 -> 明确提示改用 --file；
          凭据为空 / 网络不通 / 会话超时 -> 只打印提示，不中断已拿到的转写与触发。
真实凭据只在 config.json，本文件不含任何密钥。
"""

import argparse
import base64
import csv
import hashlib
import hmac
import json
import math
import os
import queue
import re
import struct
import sys
import threading
import time
import wave
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlencode, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

ACTS = {
    "vib1":   ("振动一下", "震动"),
    "vib2":   ("振动两下", "震动"),
    "vib3":   ("振动三下", "震动"),
    "strap1": ("收紧 1 档", "收紧"),
    "strap2": ("收紧 2 档", "收紧"),
    "cool":   ("凉", "温度"),
    "warm":   ("热", "温度"),
}

IAT_URL = "wss://iat-api.xfyun.cn/v2/iat"
FORMAT = "audio/L16;rate=16000"
FRAME_BYTES = 1280          # 40ms * 16000Hz * 2 bytes
FRAME_MS = 40
SESSION_MAX_SEC = 55        # 官方 60s 上限，留 5s 余量后自动开新会话
SILENCE_RMS = 300           # 静音判定阈值（16bit PCM 幅值 RMS）

GENTLE_WORDS = ("好", "行", "可以", "就这么定", "谢谢", "感谢", "没问题", "那就", "同意", "辛苦")
REQUEST_WORDS = ("预算", "两千", "周五", "能不能", "到底", "听清", "保证", "多少")
NUMBER_RE = re.compile(r"[0-9０-９]|[一二两三四五六七八九十百千万]|周[一二三四五六日末]|预算|金额|保证|质保")
QUESTION_RE = re.compile(r"[？?]|吗|能不能|有没有|多少|怎么|为什么")


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
        print("[提示] 没有 config.json，改用 config.example.json（无 key）")
        p = BASE / "config.example.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print("[降级] 配置读不了：" + str(e) + "，用空配置继续")
        return {}


def write_csv(name, fields, rows):
    p = BASE / name
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        w.writerows(rows)
    return p


# ---------------------------------------------------------------------------
# IAT 鉴权与会话
# ---------------------------------------------------------------------------
def build_auth_url(iat):
    u = urlparse(iat.get("host_url", IAT_URL))
    host = u.netloc
    date = formatdate(usegmt=True)
    origin = "host: " + host + "\ndate: " + date + "\nGET " + u.path + " HTTP/1.1"
    sig = base64.b64encode(
        hmac.new(iat["api_secret"].encode("utf-8"), origin.encode("utf-8"),
                 hashlib.sha256).digest()).decode("ascii")
    auth_origin = ('api_key="' + iat["api_key"] + '", algorithm="hmac-sha256", '
                   'headers="host date request-line", signature="' + sig + '"')
    auth = base64.b64encode(auth_origin.encode("utf-8")).decode("ascii")
    return iat.get("host_url", IAT_URL) + "?" + urlencode(
        {"host": host, "date": date, "authorization": auth})


class IatSession:
    """一个 IAT WebSocket 会话（最长 SESSION_MAX_SEC 秒，到点由外层换新会话）。"""

    def __init__(self, cfg, on_piece, on_error):
        self.iat = cfg.get("iat", {})
        self.on_piece = on_piece
        self.on_error = on_error
        self.ws = None
        self.stop = threading.Event()
        self.recv_thread = None
        self.finished = threading.Event()
        self.sid = ""

    def open(self):
        import websocket
        url = build_auth_url(self.iat)
        try:
            self.ws = websocket.create_connection(url, timeout=12, enable_multithread=True)
        except Exception as e:
            self.on_error("握手失败：" + str(e))
            return False
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()
        return True

    def _recv_loop(self):
        while not self.stop.is_set():
            try:
                raw = self.ws.recv()
            except Exception as e:
                if not self.stop.is_set():
                    self.on_error("接收中断：" + str(e)[:100])
                self.finished.set()
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("code") != 0:
                self.on_error("错误码 " + str(msg.get("code")) + "：" + str(msg.get("message", "")))
                self.finished.set()
                return
            self.sid = self.sid or str(msg.get("sid", ""))
            d = msg.get("data") or {}
            res = d.get("result") or {}
            if res:
                text = "".join(
                    str(cw.get("w", ""))
                    for w in res.get("ws", [])
                    for cw in (w.get("cw", []) or [])
                )
                if text:
                    self.on_piece(text, d.get("status", 1))
            if d.get("status") == 2:
                self.finished.set()

    def send_first(self, frame):
        body = {
            "common": {"app_id": self.iat.get("appid", "")},
            "business": {
                "language": self.iat.get("language", "zh_cn"),
                "domain": self.iat.get("domain", "iat"),
                "accent": self.iat.get("accent", "mandarin"),
                "eos": self.iat.get("eos_ms", 10000),
                "ptt": 1,
                "nunum": 1,
            },
            "data": {"status": 0, "format": FORMAT, "encoding": "raw",
                     "audio": base64.b64encode(frame).decode("ascii")},
        }
        self.ws.send(json.dumps(body, ensure_ascii=False))

    def send_mid(self, frame):
        self.ws.send(json.dumps(
            {"data": {"status": 1, "format": FORMAT, "encoding": "raw",
                      "audio": base64.b64encode(frame).decode("ascii")}},
            ensure_ascii=False))

    def send_last(self):
        try:
            self.ws.send(json.dumps({"data": {"status": 2}}))
        except Exception:
            pass

    def close(self):
        self.stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 音频源：电脑麦克风 / wav 文件，统一吐出 1280 字节帧与 RMS
# ---------------------------------------------------------------------------
def rms_of(frame):
    n = len(frame) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack("<" + str(n) + "h", frame[:n * 2])
    return math.sqrt(sum(v * v for v in vals) / n)


def frames_from_mic(stop, seconds):
    try:
        import sounddevice as sd
    except ImportError:
        raise SystemExit("[错误] 没装 sounddevice。双击 一键装实时.bat，或改用 --file dialogue.wav（不需要麦克风）。")
    q = queue.Queue()

    def cb(indata, frames, time_info, status):
        q.put(bytes(indata))

    print("[麦克风] 使用电脑默认输入设备，16kHz / 16bit / 单声道")
    try:
        stream = sd.RawInputStream(samplerate=16000, blocksize=640, channels=1,
                                   dtype="int16", callback=cb)
    except Exception as e:
        raise SystemExit("[错误] 打不开麦克风：" + str(e) + "。可改用 --file dialogue.wav 验证协议。")
    t0 = time.time()
    with stream:
        while not stop.is_set():
            if seconds and time.time() - t0 >= seconds:
                break
            try:
                data = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if len(data) >= FRAME_BYTES:
                frame = data[:FRAME_BYTES]
            else:
                frame = data + b"\x00" * (FRAME_BYTES - len(data))
            yield frame, rms_of(frame)


def frames_from_wav(path, stop, seconds):
    p = Path(path)
    if not p.is_absolute():
        p = BASE / p
    if not p.exists():
        raise SystemExit("[错误] 找不到音频 " + str(p))
    with wave.open(str(p), "rb") as w:
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        if rate != 16000 or width != 2 or ch != 1:
            raise SystemExit("[错误] IAT 只接受 16kHz/16bit/单声道 wav，当前为 " +
                             str(rate) + "Hz/" + str(width * 8) + "bit/" + str(ch) + "声道。先用 make_dialogue.py 生成。")
        t0 = time.time()
        while not stop.is_set():
            if seconds and time.time() - t0 >= seconds:
                break
            data = w.readframes(640)
            if not data:
                break
            if len(data) < FRAME_BYTES:
                data += b"\x00" * (FRAME_BYTES - len(data))
            yield data[:FRAME_BYTES], rms_of(data[:FRAME_BYTES])


# ---------------------------------------------------------------------------
# 实时事件判定：只断言对话与音频里可验证的事实，不猜心思
# ---------------------------------------------------------------------------
class Judge:
    def __init__(self, cfg, sender, args, on_fire=None):
        self.cfg = cfg
        self.sender = sender
        self.args = args
        # on_fire(act, cn, evidence, cmd, source, tstr)：触发后的回调钩子，
        # 供本地网页等上层把可视化事件推出去；不影响命令行用法。
        self.on_fire = on_fire
        self.rows = []
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.last_fire = {}
        self.cooldown = float(cfg.get("iat", {}).get("cooldown_sec", 8))
        self.buffer = ""
        self.full_text = ""
        self.silence_ms = 0
        self.last_voice_ms = 0
        self.question_at = -1
        self.seen_number = False
        self.repeat = {}
        self.tension_until = 0
        self.last_hr_check = 0
        self.pending_warm = ""
        self.emotion = ""     # 最近成句的 ASR 情感标签（qwen3-asr-flash-realtime，7 类）；空=无标签
                              # 规则层 warm 门槛读这里；LLM 侧由 local_server 按句绑定传递，不读本字段
        # 慢脑流式补充钩子（可选）：每完成一句回调一次 on_sentence_cb(s)，
        # 供上层让云端 LLM 流式补判快脑漏掉的触发；不影响命令行用法。
        self.on_sentence_cb = None
        # 快脑开关（纯慢脑演示模式用）：False 时规则层只记账不触发，全部决策交慢脑
        self.rules_enabled = True

    def elapsed_ms(self):
        return int((time.time() - self.t0) * 1000)

    def fire(self, act, evidence, source="规则层(实时)"):
        now = self.elapsed_ms()
        with self.lock:
            if now - self.last_fire.get(act, -100000) < self.cooldown * 1000:
                return
            self.last_fire[act] = now
            cn, ch = ACTS[act]
            cmd = json.dumps({"act": act}, ensure_ascii=False)
            tstr = format(now / 1000.0, ".1f") + "s"
            print("\n  >>> [触发] t=" + tstr + "  " + act + " (" + cn + ")  来源=" + source)
            print("      依据：" + evidence)
            print("      指令：" + cmd)
            self.sender(cmd)
            self.rows.append([tstr, source, act, cn, evidence, cmd, ch,
                              self.full_text[-120:], "未复核", ""])
            if self.on_fire:
                self.on_fire(act, cn, evidence, cmd, source, tstr)
            if act in ("strap2", "vib2"):
                self.tension_until = now + 15000

    def on_piece(self, piece, status):
        """每片增量文本：立刻上屏（边说边出），并按标点切句做判定。"""
        self.full_text += piece
        self.buffer += piece
        print(piece, end="", flush=True)
        self.last_voice_ms = self.elapsed_ms()
        self.silence_ms = 0
        while True:
            m = re.search(r"[。！？!?；;]", self.buffer)
            if not m:
                break
            sentence = self.buffer[:m.end()].strip()
            self.buffer = self.buffer[m.end():]
            if sentence:
                self.on_sentence(sentence)

    def on_sentence(self, s):
        now = self.elapsed_ms()
        first_hard = False
        rep_hits = []
        # —— 记账（无论快脑开关都执行：纯慢脑模式靠这些状态做判断）——
        if not self.seen_number and NUMBER_RE.search(s):
            self.seen_number = True
            first_hard = True
        if QUESTION_RE.search(s):
            self.question_at = now
        for kw in REQUEST_WORDS:
            if kw in s:
                self.repeat[kw] = self.repeat.get(kw, 0) + 1
                rep_hits.append((kw, self.repeat[kw]))
        if any(w in s for w in GENTLE_WORDS) and now > self.tension_until:
            self.pending_warm = s[:40]
        # —— 快脑触发（受开关控制）——
        if self.rules_enabled:
            if first_hard:
                self.fire("vib3", "首次出现金额/时间硬信息：" + s[:40], "LLM+规则层(实时)")
            for kw, cnt in rep_hits:
                if cnt == 2:
                    self.fire("strap1", "对方第一次重复同一诉求「" + kw + "」：" + s[:40])
                elif cnt >= 3:
                    self.fire("strap2", "同一诉求「" + kw + "」第 " +
                              str(cnt) + " 次重复：" + s[:40])
        # 慢脑流式补充钩子：句子完成即回调（快脑规则已同步跑完）
        if self.on_sentence_cb:
            try:
                self.on_sentence_cb(s)
            except Exception:
                pass

    def on_silence(self, frame_silent, frame_ms):
        # 记账永远执行（纯慢脑模式也要知道静音了多久）；触发部分受快脑开关控制
        if frame_silent:
            self.silence_ms += frame_ms
        else:
            self.silence_ms = 0
        now = self.elapsed_ms()
        if not self.rules_enabled:
            return
        # vib1：一句说完、静音 >=1.5 秒，轮到佩戴者
        if self.silence_ms >= 1500 and self.full_text.strip():
            self.fire("vib1", "对方说完后静音 " + str(self.silence_ms) + " 毫秒，轮到佩戴者")
            self.silence_ms = 0
        # vib2：问句后 >=5 秒没有新文本
        if self.question_at >= 0 and now - self.question_at >= 5000:
            self.fire("vib2", "提问后 " + str(now - self.question_at) + " 毫秒未答")
            self.question_at = -1
        # warm：静音收尾时把之前攒到的共识信号落地
        # 音频情感标签（qwen3-asr-flash-realtime）：happy/neutral 视为温和，其余压住 warm
        # 无标签（打字/文件/命令行）时保持原行为，不降级
        gentle_emo = self.emotion in ("", "happy", "neutral")
        if self.pending_warm and gentle_emo and self.silence_ms >= 800 and now > self.tension_until:
            self.fire("warm", "对话气氛融洽温和：出现共识/致谢/肯定且近期无打断或重复诉求：" +
                      self.pending_warm + "；音频情感=" + (self.emotion or "无标签"),
                      "LLM+规则层(实时)+ASR情感")
            self.pending_warm = ""

    def tick_hr(self):
        if not self.rules_enabled:
            return
        now = self.elapsed_ms()
        if now - self.last_hr_check < 10000:
            return
        self.last_hr_check = now
        if self.cfg.get("hr_source", "mock") == "mock":
            hr = self.cfg.get("mock_hr", 108)
            resting = self.cfg.get("resting_hr", 80)
            delta = self.cfg.get("threshold_hr_delta", 15)
            if hr - resting >= delta:
                # 2026-11-05 用户确认：心率过快不再给凉，改为「平静心跳节律」的规律振动（vib1）。
                # 调试台由 local_server.hr_rhythm_loop 按 hr_calm_bpm 持续驱动双拍；这里是命令行版单拍兜底。
                self.fire("vib1", "mock 心率 " + str(hr) + " 高于静息 " + str(resting) +
                          " 达 " + str(delta) + "：按平静心跳节律振动引导（实时版暂用 mock，11.7 换 wrist/bio）")

    def flush_tail(self):
        if self.buffer.strip():
            self.on_sentence(self.buffer.strip() + "。")


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
        try:
            s = serial.Serial(port, cfg.get("serial", {}).get("baud", 115200), timeout=0.5)
        except Exception as e:
            print("[降级] 打不开 " + port + "：" + str(e) + "，改为只打印")
            return lambda cmd: None
        return lambda cmd: (s.write((cmd + "\n").encode("utf-8")), s.flush())
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[降级] 缺 paho-mqtt，改为只打印")
        return lambda cmd: None
    m = cfg.get("mqtt", {})
    try:
        try:
            c = mqtt.Client(client_id=m.get("client_id", "realtime-pc") + "-" + str(int(time.time())))
        except Exception:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                            client_id=m.get("client_id", "realtime-pc") + "-" + str(int(time.time())))
        c.connect(m.get("broker", "broker.emqx.io"), m.get("port", 1883), 60)
        c.loop_start()
    except Exception as e:
        print("[降级] MQTT 连不上：" + str(e) + "，改为只打印")
        return lambda cmd: None
    print("[MQTT] 已连接，发布 " + m.get("topic_cmd", "wrist/cmd"))
    return lambda cmd: c.publish(m.get("topic_cmd", "wrist/cmd"), cmd)


def llm_review_async(cfg, sentence, act, rows):
    """可选：DeepSeek 异步复核，不阻塞音频流；只把结论补写进 CSV。"""
    try:
        import importlib
        pipe = importlib.import_module("pipeline")
        _a, ev, verdict = pipe.llm_review(cfg, sentence, act)
        for r in rows:
            if r[2] == act and r[8] == "未复核":
                r[8] = ev
                r[9] = verdict
                break
    except Exception:
        pass


def main():
    setup_console()
    ap = argparse.ArgumentParser(description="触觉社交手环二期：讯飞 IAT 边说边出（七个输出）")
    ap.add_argument("--mic", action="store_true", help="用电脑麦克风（默认）")
    ap.add_argument("--file", default="", help="用已有 16k/16bit/单声道 wav 模拟实时流，不需要麦克风")
    ap.add_argument("--seconds", type=float, default=55, help="总录制秒数，默认 55（单会话 <60s 上限）")
    ap.add_argument("--mock", action="store_true", help="只打印不发指令（默认）")
    ap.add_argument("--serial", action="store_true", help="从串口发给 ESP32")
    ap.add_argument("--mqtt", action="store_true", help="发到 MQTT broker")
    ap.add_argument("--port", default="", help="串口号，如 COM3")
    ap.add_argument("--llm", action="store_true", help="额外用 DeepSeek 异步复核证据")
    ap.add_argument("--no-asr", action="store_true", help="不连讯飞，只用本地规则层自检音频链路")
    args = ap.parse_args()

    mode = "mqtt" if args.mqtt else ("serial" if args.serial else "mock")
    cfg = load_config()
    iat = cfg.get("iat", {})
    print("=" * 66)
    print(" 触觉社交手环 · 二期「边说边出」  下发=" + mode + "  ASR=讯飞语音听写(流式版 IAT)")
    print(" 音频源=" + ("文件 " + args.file if args.file else "电脑麦克风") +
          "  时长=" + str(args.seconds) + "s")
    print("=" * 66)

    if args.no_asr:
        print("[自检] --no-asr：不连讯飞，只跑本地音频帧与规则层")
    elif not all(iat.get(k) for k in ("appid", "api_key", "api_secret")):
        print("[提示] config.json 的 iat 三件套没填齐，改用 --no-asr 自检模式")
        args.no_asr = True

    sender = make_sender(mode, cfg, args)
    judge = Judge(cfg, sender, args)
    stop = threading.Event()
    frames = (frames_from_wav(args.file, stop, args.seconds) if args.file
              else frames_from_mic(stop, args.seconds))

    session = None
    first_frame = True
    session_start = time.time()
    frame_count = 0
    # 麦克风回调本身按 40ms 推帧，不能再额外 sleep 否则会越积越延迟；
    # 只有文件模式需要按 40ms 节流来模拟实时。
    pace = bool(args.file)
    try:
        for frame, rms in frames:
            frame_count += 1
            silent = rms < SILENCE_RMS
            judge.on_silence(silent, FRAME_MS)
            judge.tick_hr()

            if not args.no_asr:
                # 会话到 55 秒就换一条新连接，规避 IAT 60s 硬上限
                if session is None or time.time() - session_start >= SESSION_MAX_SEC:
                    if session is not None:
                        session.send_last()
                        session.finished.wait(timeout=3)
                        session.close()
                        print("\n[会话] 达到 " + str(SESSION_MAX_SEC) + "s，自动换新会话继续听")
                    session = IatSession(cfg, judge.on_piece,
                                         lambda e: print("\n[降级] " + e))
                    if not session.open():
                        print("[降级] 连不上讯飞，后续只做本地静音/规则层判定")
                        session = None
                        args.no_asr = True
                    else:
                        first_frame = True
                        session_start = time.time()

                if session is not None:
                    try:
                        if first_frame:
                            session.send_first(frame)
                            first_frame = False
                        else:
                            session.send_mid(frame)
                    except Exception as e:
                        print("\n[降级] 发送中断：" + str(e)[:100] + "，改跑本地规则层")
                        session.close()
                        session = None
                        args.no_asr = True
            if pace:
                time.sleep(FRAME_MS / 1000.0)
    except KeyboardInterrupt:
        print("\n[结束] 收到 Ctrl+C")
    except SystemExit:
        raise
    except Exception as e:
        print("\n[降级] 音频源异常：" + str(e))

    stop.set()
    if session is not None:
        session.send_last()
        session.finished.wait(timeout=3)
        session.close()
    judge.flush_tail()

    if args.llm and judge.rows:
        t = threading.Thread(target=llm_review_async,
                             args=(cfg, judge.full_text[-200:], judge.rows[-1][2], judge.rows),
                             daemon=True)
        t.start()
        t.join(timeout=8)

    out = write_csv("realtime_triggers.csv",
                    ["时间", "来源", "act", "中文", "证据原文", "指令JSON", "通道",
                     "转写文本", "LLM证据", "LLM复核"], judge.rows)
    print("\n" + "-" * 66)
    print("完成：处理 " + str(frame_count) + " 帧音频，转写 " + str(len(judge.full_text)) +
          " 字，触发 " + str(len(judge.rows)) + " 个动作")
    print("转写全文：" + (judge.full_text[:200] or "（无，静音或没连上 ASR）"))
    print("触发顺序：" + (" -> ".join(r[2] for r in judge.rows) or "（无）"))
    print("已写 " + out.name + "（实时字幕源）")
    if mode == "mock":
        print("mock 模式只打印没发指令。把 JSON 粘进 Arduino 串口就能驱动真硬件。")
    print("-" * 66)


if __name__ == "__main__":
    main()
