# -*- coding: utf-8 -*-
"""
触觉社交手环 · 本地可视化调试网页（软件链路 vs 硬件执行 一屏分清）

架构（2026-11-06：实时语音改 Qwen 实时版，密钥只在后端）：
  浏览器 getUserMedia 麦克风 -> 16k PCM -> 每 100ms 一块 POST /api/asr-rt/append
      -> 后端代理 qwen3-asr-flash-realtime WebSocket（Authorization 头不能由浏览器设置）
      -> .text(stash 临时句) / .completed(transcript 最终句 + emotion)
      -> SSE 推网页逐字上屏，最终句交给 Judge 判定（与喂入同一链路）
  后端 Judge（复用 realtime_asr.py 的实时规则）-> 触发 {"act":"..."}
      -> 下发（mock / 串口 / MQTT）+ SSE 推给网页可视化
  串口模式会读固件回显：出现「[动作]」即视为硬件确认执行；
      软件已发 + 无硬件确认 -> 问题在硬件侧（接线/供电/固件没跑）。
  ASR 的 emotion 按「句」绑定（PIECE_EMO）：同时进规则层 warm 门槛、LLM 复核、
      三个慢脑提示词；慢脑从队列取快照，不读全局 JUDGE.emotion，避免异步串句。
  本地声纹 speaker_id.py：录入佩戴者 8 秒 -> 每个 VAD turn 比对 ->
      wearer/other/unknown，同样按句绑定（PIECE_SPEAKER）进 LLM 与上屏字幕；
      模板只存内存、不落盘不上传；百炼实时版本身不提供说话人字段。
  LLM 复核异步进行，model 强制 deepseek-chat（deepseek-chat 本身无思考链，
      即默认关 thinking；若配置里写了 reasoner 会自动改回 chat）。

怎么跑：
    python local_server.py            浏览器开 http://127.0.0.1:8765
    python local_server.py --port 9000
    python local_server.py --serial --port COM3
    python local_server.py --mqtt

真实凭据只在 config.json；本文件与网页都不含密钥。
"""

import argparse
import csv
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import realtime_asr as ra
import speaker_id as spk

ra.setup_console()

HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 8765

CFG = ra.load_config()
spk.configure(CFG.get("speaker", {}))
# 相对路径锚定到本文件目录：服务从任意 CWD 启动，CSV 都落在 software/ 下
_PATH_ROOT = Path(__file__).resolve().parent
CFG.setdefault("paths", {})
for _k, _v in (("memory_csv", "memory.csv"), ("feedback_csv", "feedback.csv"),
               ("pins_csv", "pins.csv"), ("script_csv", "dialogue_script.csv"),
               ("timeline_csv", "dialogue_timeline.csv"), ("triggers_csv", "triggers.csv"),
               ("asr_csv", "asr_segments.csv")):
    _p = CFG["paths"].get(_k)
    CFG["paths"][_k] = str(_PATH_ROOT / _p) if (_p and not os.path.isabs(_p)) else str(_PATH_ROOT / (_p or _v))
STATE = {
    "channel": "mock",
    "serial_port": CFG.get("serial", {}).get("port", "COM3"),
    "llm": bool(CFG.get("llm", {}).get("enabled", True)),
    "brain_mode": CFG.get("brain_mode", "both"),   # both=双脑 / fast=纯快脑 / slow=纯慢脑
}
if CFG.get("llm", {}).get("model") in ("deepseek-reasoner", "deepseek-r1", "deepseek-chat"):
    CFG["llm"]["model"] = "deepseek-flash"
    STATE["llm_note"] = "配置里的模型名已下线，自动改用 deepseek-flash（实测你的账号可用，无思考链、延迟低）"

SHUTDOWN = threading.Event()
SSE_CLIENTS = []
SSE_LOCK = threading.Lock()
TALKING = threading.Event()
STOP_AT = {"t": 0.0}
SERIAL = {"s": None, "lock": threading.Lock()}
MQTT = {"c": None, "lock": threading.Lock()}
LOG = []
SLOWQ = queue.Queue()      # 慢脑流式补发队列：每完成一句入队，worker 串行处理
# 情感按「句」绑定：on_piece → on_sentence → on_sentence_cb 是同一线程的同步链路，
# 接收器在 on_piece 前写入，回调立刻取走快照入 SLOWQ，慢脑不读全局 JUDGE.emotion。
PIECE_EMO = {"cur": ""}
PIECE_SPEAKER = {"cur": "unknown"}   # 同理按句绑定：wearer / other / unknown
TURN_AUDIO = {"chunks": []}          # 当前 VAD turn 的 PCM 块；.completed 结算后清空
LAST_FIRED = []            # 最近触发的动作（供慢脑判断"快脑已触发过什么"）
LAST_FIRE_TS = 0.0         # 最近一次触发的时间戳（手势 3 秒反馈窗口用）


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    LOG.append(line)
    del LOG[:-200]
    print("[web] " + msg)
    broadcast("log", {"line": msg})


def broadcast(name, data):
    with SSE_LOCK:
        clients = list(SSE_CLIENTS)
    for q in clients:
        q.put((name, data))


# ---------------------------------------------------------------------------
# 下发通道
# ---------------------------------------------------------------------------
def serial_send(cmd):
    with SERIAL["lock"]:
        try:
            import serial
        except ImportError:
            return False, "未装 pyserial（双击 一键装环境.bat）"
        if SERIAL["s"] is None:
            try:
                SERIAL["s"] = serial.Serial(STATE["serial_port"], 115200, timeout=0.1)
                log("串口已打开 " + STATE["serial_port"])
                broadcast("state", {"channel": STATE["channel"],
                                    "serial": "已打开 " + STATE["serial_port"]})
            except Exception as e:
                SERIAL["s"] = None
                return False, "打不开 " + STATE["serial_port"] + "：" + str(e)[:80]
        s = SERIAL["s"]
        try:
            s.reset_input_buffer()
            s.write((cmd + "\n").encode("utf-8"))
            s.flush()
            time.sleep(0.4)
            echo = s.read(4096).decode("utf-8", "replace").strip()
            return True, echo
        except Exception as e:
            try:
                s.close()
            except Exception:
                pass
            SERIAL["s"] = None
            return False, "串口发送失败：" + str(e)[:80]


def mqtt_publish(topic, payload):
    with MQTT["lock"]:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return False, "未装 paho-mqtt（双击 一键装环境.bat）"
        m = CFG.get("mqtt", {})
        try:
            if MQTT["c"] is None:
                try:
                    c = mqtt.Client(client_id="web-pc-" + str(int(time.time())))
                except Exception:
                    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                    client_id="web-pc-" + str(int(time.time())))
                c.connect(m.get("broker", "broker.emqx.io"), m.get("port", 1883), 60)
                c.loop_start()
                MQTT["c"] = c
                log("MQTT 已连接 " + m.get("broker", "broker.emqx.io"))
            MQTT["c"].publish(topic, payload)
            return True, "已发布到 " + topic
        except Exception as e:
            MQTT["c"] = None
            return False, "MQTT 发布失败：" + str(e)[:80]


def mqtt_send(cmd):
    return mqtt_publish(CFG.get("mqtt", {}).get("topic_cmd", "wrist/cmd"), cmd)


def gesture_sub_loop():
    """订阅 wrist/ui（W2 触摸上行）：Wokwi 固件或调试台发的手势都会在这里收到并推给网页。
    连不上 broker 就 30 秒后重试，不阻塞主流程（验收不需要任何外部网页工具）。"""
    while True:
        try:
            import paho.mqtt.client as mqtt
            m = CFG.get("mqtt", {})
            topic = m.get("topic_ui", "wrist/ui")

            def _on_msg(client, userdata, msg):
                try:
                    d = json.loads(msg.payload.decode("utf-8", "replace"))
                except Exception:
                    d = {"raw": msg.payload.decode("utf-8", "replace")[:120]}
                d["t"] = time.strftime("%H:%M:%S")
                broadcast("gesture_rx", d)
                log("[触摸上行·收到] " + str(d.get("g", "?")) + " <- " + msg.topic)

            c = mqtt.Client(client_id="web-sub-" + str(int(time.time())))
            c.on_message = _on_msg
            c.connect(m.get("broker", "broker.emqx.io"), m.get("port", 1883), 60)
            c.subscribe(topic)
            log("已订阅 " + topic + "（" + m.get("broker", "broker.emqx.io") + "）")
            c.loop_forever()   # 内部自动重连；只有连接阶段失败才会掉出循环
        except Exception as e:
            log("wrist/ui 订阅暂不可用（30 秒后重试）：" + str(e)[:60])
            time.sleep(30)


def downlink(cmd, manual=False):
    """下发指令并广播；返回是否成功与硬件回显。"""
    ch = STATE["channel"]
    if ch == "serial":
        ok, echo = serial_send(cmd)
    elif ch == "mqtt":
        ok, echo = mqtt_send(cmd)
    else:
        ok, echo = True, "（mock 只打印，不下发）"
    hw = {"confirmed": bool(ok and "[动作]" in echo),
          "echo": echo[:300], "manual": manual}
    broadcast("sent", {"cmd": cmd, "channel": ch, "ok": ok, "hw": hw,
                       "ts": time.strftime("%H:%M:%S")})
    if manual:
        log("手动下发 " + cmd + " -> " + ch + ("，硬件确认" if hw["confirmed"]
                                              else ("，无硬件确认" if ok else "，下发失败")))
    return ok, hw


# ---------------------------------------------------------------------------
# 判定（复用 realtime_asr.Judge）
# ---------------------------------------------------------------------------
def on_fire(act, cn, evidence, cmd, source, tstr):
    global LAST_FIRE_TS
    broadcast("trigger", {"act": act, "cn": cn, "evidence": evidence,
                          "source": source, "t": tstr})
    LAST_FIRE_TS = time.time()
    LAST_FIRED.append(act)
    del LAST_FIRED[:-5]
    memory_append([time.strftime("%Y-%m-%d %H:%M:%S"), "触发", act,
                   evidence[:60], "未复核", JUDGE.full_text[-80:]])
    if STATE["llm"]:
        # 触发瞬间同步捕获情感与说话人：异步线程若读全局会被下一句覆盖，故随 act 一起传参
        emo = str(getattr(JUDGE, "emotion", "") or "")
        who = PIECE_SPEAKER["cur"]
        threading.Thread(target=llm_review, args=(act, emo, who), daemon=True).start()


JUDGE = ra.Judge(CFG, lambda cmd: downlink(cmd), argparse.Namespace(port=""),
                 on_fire=on_fire)
JUDGE.rules_enabled = (STATE["brain_mode"] != "slow")


REVIEW_SYSTEM = (
    "你是触觉社交手环的慢脑复核员。会给你：动作、触发依据、触发那一刻的对话句子快照、"
    "该句音频情感标签（qwen3-asr-flash-realtime 的 7 类 emotion；无标签=打字/文件等非语音输入）、"
    "该句说话人（本人=佩戴者 / 对方 / 未判定）。"
    "只判断这一次触发是否有充分的文本依据，不要建议其他动作；"
    "情感标签与说话人只能给文本依据做佐证，不能单独充当触发理由。"
    "输出 JSON：{\"support\":true/false,\"reason\":\"一句话依据\"}。"
)


def llm_review(act, emotion="", speaker="unknown"):
    try:
        import pipeline
        # 复核用"触发那一刻"的句子快照（rows 触发行已存档），并针对该动作定向提问——
        # 旧法（全文尾部+挑最显眼动作）在多触发场景会张冠李戴（2026-11-05 实测修正）
        text, ev = "", ""
        for r in reversed(JUDGE.rows):
            if r[2] == act and r[8] == "未复核":
                text, ev = r[7], r[4]
                break
        if not text:
            text = (JUDGE.full_text[-200:] or JUDGE.buffer[-100:]).strip()
        if not text:
            return
        verdict, reason = "", ev
        content = llm_chat(REVIEW_SYSTEM,
                           "动作：" + act + "\n触发依据：" + ev +
                           "\n触发时句子快照：" + text +
                           _emo_line(emotion, speaker))
        if content:
            obj = pipeline.extract_json(content)
            support = bool(obj.get("support"))
            reason = str(obj.get("reason", ""))[:70] or ev
            verdict = "一致" if support else "存疑：" + reason
        else:
            verdict = "LLM 未响应"
            reason = ""
        broadcast("llm", {"act": act, "evidence": reason, "verdict": verdict})
        memory_append([time.strftime("%Y-%m-%d %H:%M:%S"), "复核", act,
                       reason[:60], verdict, text[:80]])
        for r in JUDGE.rows:
            if r[2] == act and r[8] == "未复核":
                r[8] = reason
                r[9] = verdict
                break
    except Exception as e:
        broadcast("llm", {"act": act, "evidence": "", "verdict": "LLM 不可用：" + str(e)[:60]})


def tick_loop():
    """松开说话后的 6 秒窗口里模拟静音 tick，触发 vib1 / vib2 / warm。"""
    last_slow_silent = {"t": 0.0}
    while not SHUTDOWN.is_set():
        time.sleep(0.2)
        if SHUTDOWN.is_set():
            break
        if TALKING.is_set():
            continue
        if STOP_AT["t"] and time.time() - STOP_AT["t"] < 6:
            JUDGE.on_silence(True, 200)
            # 心率过快不再由 tick_hr 单发 cool：节律振动由 hr_rhythm_loop 全程驱动
            # 纯慢脑模式：静音收尾的三个时机型动作（vib1/vib2/warm）交给慢脑判断
            if STATE["brain_mode"] == "slow":
                elapsed = time.time() - STOP_AT["t"]
                if 5.0 < elapsed < 5.5 and last_slow_silent["t"] != STOP_AT["t"]:
                    last_slow_silent["t"] = STOP_AT["t"]
                    q_ms = -1
                    if JUDGE.question_at >= 0:
                        q_ms = JUDGE.elapsed_ms() - JUDGE.question_at
                    SLOWQ.put({"kind": "silent", "silent_ms": JUDGE.silence_ms,
                               "question_ms": q_ms, "gentle": JUDGE.pending_warm,
                               "tension": JUDGE.elapsed_ms() < JUDGE.tension_until,
                               "emotion": str(getattr(JUDGE, "emotion", "") or ""),
                               "speaker": PIECE_SPEAKER["cur"]})


# ---------------------------------------------------------------------------
# 心率过快 → 平静心跳节律振动（2026-11-06 用户确认 v2）
# 一次触发固定 7 下 vib1，按 hr_calm_bpm（默认 30 BPM = 每 2 秒一下，比 v1 慢一倍）；
# 启动后无论心率是否回落，7 下必须打完；打完冻结 hr_rhythm_freeze_sec（默认 600 秒），
# 冻结期间心率再高也不再触发。不下发 cool，也不受 8 秒动作冷却限制。
# ---------------------------------------------------------------------------
HR_RHYTHM = {"active": False, "next": 0.0, "beats": 0, "freeze_until": 0.0}


def hr_rhythm_loop():
    while not SHUTDOWN.is_set():
        time.sleep(0.05)
        try:
            now = time.time()
            hr = int(CFG.get("mock_hr", 80))
            resting = int(CFG.get("resting_hr", 80))
            delta = int(CFG.get("threshold_hr_delta", 15))
            calm = max(10, min(100, int(CFG.get("hr_calm_bpm", 30))))
            total = max(1, int(CFG.get("hr_rhythm_beats", 7)))
            freeze_sec = max(0, int(CFG.get("hr_rhythm_freeze_sec", 600)))
            hot = (hr - resting) >= delta

            if HR_RHYTHM["active"]:
                # 启动后不看 hot：心跳已恢复正常也照样把 7 下打完
                if now >= HR_RHYTHM["next"]:
                    n = HR_RHYTHM["beats"] + 1
                    HR_RHYTHM["beats"] = n
                    downlink(json.dumps({"act": "vib1"}, ensure_ascii=False))
                    broadcast("trigger", {"act": "vib1", "cn": "振动一下",
                                          "evidence": "心率过快 → 节律第 " + str(n) + "/" +
                                                      str(total) + " 下（" + str(calm) +
                                                      " BPM，每 " + format(60.0 / calm, ".1f") + " 秒一下）",
                                          "source": "心率节律", "t": time.strftime("%H:%M:%S")})
                    broadcast("hr_beat", {"n": n, "total": total, "bpm": calm, "hr": hr})
                    if n >= total:
                        HR_RHYTHM["active"] = False
                        HR_RHYTHM["freeze_until"] = now + freeze_sec
                        log("节律振动 " + str(total) + " 下完成 -> 冻结 " +
                            str(freeze_sec // 60) + " 分钟（期间心率再高也不触发）")
                        memory_append([time.strftime("%Y-%m-%d %H:%M:%S"), "触发", "vib1",
                                       "心率过快 → " + str(total) + " 下节律振动完成，冻结 " +
                                       str(freeze_sec // 60) + " 分钟", "未复核", ""])
                        broadcast("hr_rhythm", {"active": False, "reason": "completed",
                                                "freeze_sec": freeze_sec, "total": total})
                    else:
                        HR_RHYTHM["next"] = now + 60.0 / calm
                continue

            if now < HR_RHYTHM["freeze_until"] or not hot:
                continue

            HR_RHYTHM["active"] = True
            HR_RHYTHM["beats"] = 0
            HR_RHYTHM["next"] = now
            log("心率 " + str(hr) + " 过快 -> 启动 " + str(total) + " 下节律振动（" +
                str(calm) + " BPM，每 " + format(60.0 / calm, ".1f") +
                " 秒一下；打完冻结 " + str(freeze_sec // 60) + " 分钟）")
            broadcast("hr_rhythm", {"active": True, "bpm": calm, "total": total, "hr": hr})
        except Exception as e:
            print("[节律] 异常：" + str(e)[:60])


# ---------------------------------------------------------------------------
# 实时语音 ASR：qwen3-asr-flash-realtime（2026-11-06 用户确认）
# 浏览器 WebSocket 无法自定义 Authorization / OpenAI-Beta 头，故由后端维持与
# DashScope 的长连接：网页 POST 音频块 -> 后端 input_audio_buffer.append ->
# 服务端 .text/.completed 事件 -> SSE 推网页（逐字 + emotion）+ 喂 Judge 判定层。
# 打字 / txt·md / Word / 音频上传链路完全不动。
# ---------------------------------------------------------------------------
_cloud_cfg = CFG.get("asr", {}).get("cloud", {})
QWEN_RT_URL = (_cloud_cfg.get("realtime_url", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
               + "?model=" + _cloud_cfg.get("realtime_model", "qwen3-asr-flash-realtime"))
ASR_RT = {"ws": None, "sid": "", "seq": 0, "closed": True,
          "last_emotion": "", "lock": threading.Lock()}


def qwen_rt_close():
    with ASR_RT["lock"]:
        if ASR_RT["closed"]:
            return
        ASR_RT["closed"] = True
        ws = ASR_RT["ws"]
        ASR_RT["ws"] = None
    if ws:
        try:
            ws.close()
        except Exception:
            pass
    broadcast("asr_status", {"state": "closed"})


def qwen_rt_send(obj):
    with ASR_RT["lock"]:
        ws = ASR_RT["ws"]
        if ASR_RT["closed"] or not ws:
            return False, "实时 ASR 会话未建立"
        try:
            ws.send(json.dumps(obj, ensure_ascii=False))
            return True, ""
        except Exception as e:
            return False, str(e)[:80]


def qwen_rt_start():
    qwen_rt_close()
    key = CFG.get("asr", {}).get("cloud", {}).get("api_key", "")
    if not key:
        return False, "config.json 的 asr.cloud.api_key 没填"
    try:
        import websocket
        ws = websocket.create_connection(
            QWEN_RT_URL, timeout=10,
            header=["Authorization: Bearer " + key, "OpenAI-Beta: realtime=v1"])
    except Exception as e:
        return False, "连接失败：" + str(e)[:100]
    with ASR_RT["lock"]:
        ASR_RT["ws"] = ws
        ASR_RT["closed"] = False
        ASR_RT["seq"] += 1
        sid = "rt" + str(ASR_RT["seq"])
        ASR_RT["sid"] = sid
        ASR_RT["last_emotion"] = ""
        ASR_RT["emo_logged"] = ""
    JUDGE.emotion = ""
    PIECE_EMO["cur"] = ""
    PIECE_SPEAKER["cur"] = "unknown"
    TURN_AUDIO["chunks"] = []
    qwen_rt_send({"event_id": "evt_session", "type": "session.update",
                  "session": {"modalities": ["text"], "input_audio_format": "pcm",
                              "sample_rate": 16000,
                              "input_audio_transcription": {"language": "zh"},
                              "turn_detection": {"type": "server_vad",
                                                 "threshold": 0.2,
                                                 "silence_duration_ms": 400}}})
    threading.Thread(target=qwen_rt_receiver, args=(sid,), daemon=True).start()
    log("实时 ASR 已连接 qwen3-asr-flash-realtime（VAD 断句 + 7 类情感）")
    broadcast("asr_status", {"state": "connected", "sid": sid})
    return True, sid


def qwen_rt_receiver(sid):
    while not SHUTDOWN.is_set():
        with ASR_RT["lock"]:
            ws = ASR_RT["ws"]
            cur = ASR_RT["sid"]
            closed = ASR_RT["closed"]
        if closed or cur != sid or not ws:
            return
        try:
            msg = ws.recv()
        except Exception as e:
            # 说话停顿期间的 recv 超时不是断线，继续等
            if "Timeout" in type(e).__name__ or "timed out" in str(e).lower():
                continue
            if not ASR_RT["closed"]:
                log("实时 ASR 接收中断：" + str(e)[:60])
            qwen_rt_close()
            return
        if not msg:
            continue
        try:
            d = json.loads(msg)
        except Exception:
            continue
        typ = str(d.get("type", ""))
        emo = str(d.get("emotion", "") or "")
        is_final = typ == "conversation.item.input_audio_transcription.completed"
        if emo:
            ASR_RT["last_emotion"] = emo
            # 只有成句事件才写判定层：.text 临时句的情感仅用于网页实时上屏，
            # 避免未成句的中间标签污染规则层 warm 门槛
            if is_final:
                JUDGE.emotion = emo
            broadcast("asr_emotion", {"emotion": emo})
        if typ == "conversation.item.input_audio_transcription.text":
            # 实测（2026-11-06）：中间结果的 text 字段为空，临时全文在 stash（累积值）；
            # 官方示例里的 text 仅在某些语种/版本出现，两者都兼容
            live = str(d.get("text", "") or "") or str(d.get("stash", "") or "")
            if live:
                broadcast("asr_text", {"text": live, "final": False,
                                       "cumulative": True, "emotion": emo})
        elif is_final:
            transcript = str(d.get("transcript", "") or "").strip()
            if transcript:
                # 取走本 turn 的 PCM 并结算说话人：一个 .completed 对应一个 server_vad 片段
                chunks = TURN_AUDIO["chunks"]
                TURN_AUDIO["chunks"] = []
                try:
                    who, score, act_ms = spk.tag_turn(chunks)
                except Exception as e:
                    who, score, act_ms = "unknown", 0.0, 0
                    log("声纹比对失败，降级为未判定：" + str(e)[:60])
                PIECE_SPEAKER["cur"] = who
                broadcast("speaker_tag", {"speaker": who, "score": score,
                                          "text": transcript[:40], "active_ms": act_ms})
                broadcast("asr_text", {"text": transcript, "final": True, "emotion": emo,
                                       "speaker": who, "score": score})
                # 最终句带标点：走与喂入/命令行完全相同的判定层链路（字幕、快脑、慢脑）
                # 情感按「句」绑定：回调在同步链路里立刻取快照入慢脑队列，避免异步串句
                PIECE_EMO["cur"] = emo
                JUDGE.on_piece(transcript, 2)
                if emo and emo != ASR_RT.get("emo_logged", ""):
                    memory_append([time.strftime("%Y-%m-%d %H:%M:%S"), "ASR情感", emo,
                                   "实时语音情感标签（qwen3-asr-flash-realtime）", "-", transcript[:60]])
                    ASR_RT["emo_logged"] = emo
        elif typ in ("session.finished", "error"):
            if typ == "error":
                log("实时 ASR 服务端错误：" + str(d.get("error", d))[:100])
            break
    # 收尾：把还没凑成句的残余文字落地（字幕、慢脑都不丢最后一句）
    try:
        JUDGE.flush_tail()
    except Exception:
        pass
    qwen_rt_close()


# ---------------------------------------------------------------------------
# 整段喂入（手动打字 / txt·md / Word / 音频 ASR）→ 拆句 → 逐句流式判定
# 设计：LLM 一次性收到全文没有实时性可言，所以拆成句子排队"流式吞入"，
#       每句立即跑判定层（触发即下发），喂入结束后复用静音窗口落地 vib1/warm/vib2。
# ---------------------------------------------------------------------------
SENT_SPLIT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
FEED_ACTIVE = threading.Event()


def split_sentences(text):
    return [s.strip() for s in SENT_SPLIT_RE.findall(text) if s.strip()]


def docx_extract(data):
    """docx = zip + word/document.xml，不引第三方库直接抽文本。"""
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = xml.replace("</w:p>", "\n")
    return re.sub(r"<[^>]+>", "", xml).strip()


def feed_session(text, source):
    if FEED_ACTIVE.is_set():
        broadcast("log", {"line": "已有喂入会话在进行中，忽略本次请求"})
        return
    if TALKING.is_set():
        broadcast("log", {"line": "正在按住说话，先松开再喂入"})
        return
    FEED_ACTIVE.set()
    # 打字 / 文件 / 音频喂入没有语音情感：清掉上一场实时语音的残留，
    # 否则规则层 warm 门槛与慢脑提示词会误用旧情感
    PIECE_EMO["cur"] = ""
    PIECE_SPEAKER["cur"] = "unknown"
    JUDGE.emotion = ""
    try:
        sents = split_sentences(text)
        if not sents:
            broadcast("log", {"line": "喂入内容没有可判定的句子"})
            return
        broadcast("feed_start", {"total": len(sents), "source": source})
        log("整段喂入开始：" + source + "，共 " + str(len(sents)) + " 句（伪流式：逐句进判定层，与实时语音同路径）")
        for i, s in enumerate(sents):
            broadcast("feed_piece", {"i": i, "text": s, "source": source})
            JUDGE.on_piece(s, 2)      # 快脑逐句判定；慢脑流式补发由 on_sentence_cb 异步跟上
            time.sleep(2.0)          # 伪流式节奏：每句停留 2 秒（2026-11-05 确认）
        # 复用静音窗口：vib1 / warm / vib2 这些延迟型规则随后照常落地
        TALKING.clear()
        STOP_AT["t"] = time.time()
        time.sleep(6.5)              # 等延迟型触发跑完，再宣告结束（字幕标红窗口）
        broadcast("feed_done", {"total": len(sents), "source": source})
        log("整段喂入会话结束")
    finally:
        FEED_ACTIVE.clear()


# ---------------------------------------------------------------------------
# 慢脑（云端 LLM）：参考 bibo 端云协同架构升级
#   快脑 = Judge 规则层（毫秒级保底，永不拆除）
#   慢脑 = LLM，三件事：①喂入模式预标注，规则层漏的由慢脑补发
#                      ②触发/复核结论沉淀 memory.csv（长期记忆）
#                      ③标注时带最近记忆摘要（记忆反哺，成长性雏形）
# ---------------------------------------------------------------------------
SLOW_STREAM_SYSTEM = (
    "你是触觉社交手环的慢脑，实时流式工作：对话一句句到来，你要跟得上节奏。"
    "会给你：刚说完的一句话、快脑已触发的动作、最近记忆摘要、该句音频情感标签（7 类 emotion，无标签=非语音输入）。"
    "判断这句话是否还有快脑遗漏的、有明确文本依据的触发。"
    "可选动作只有：vib3=首次出现金额/时间/承诺等硬信息；"
    "cool=说话人情绪明显应激（文本有应激/冲突表述；音频情感 angry/sad/fearful/disgusted 可作佐证，仅凭情感标签不得触发）。"
    "其余动作（vib1/vib2/warm/strap）由快脑计时/计数负责，一律不要标。"
    "宁缺勿滥，没有就空数组。"
    "输出 JSON：{\"marks\":[{\"act\":\"动作\",\"why\":\"一句话依据\"}]}"
)

SLOW_FULL_SYSTEM = (
    "你是触觉社交手环的慢脑（纯慢脑演示模式：快脑已关闭，七个动作全部由你判断）。"
    "动作全集：vib1=对方说完且静音≥1.5秒，轮到佩戴者；vib2=提问后≥5秒无人回应；"
    "vib3=首次出现金额/时间/承诺等硬信息；strap1=同一诉求第2次重复；strap2=第3次重复；"
    "cool=说话人情绪明显应激（文本应激/冲突表述；音频情感 angry/sad/fearful/disgusted 可作佐证，仅凭情感不触发）；"
    "warm=出现共识/致谢且气氛融洽（音频情感须为 happy/neutral 或无标签；angry/sad/fearful/disgusted/surprised 一律不报 warm）。"
    "给你：刚说完的一句话、该句音频情感标签、参考状态（诉求重复计数以此为准、已触发动作、记忆摘要）。"
    "宁缺勿滥，输出 JSON：{\"marks\":[{\"act\":\"动作\",\"why\":\"一句话依据\"}]}"
)

SLOW_SILENT_SYSTEM = (
    "纯慢脑演示模式的静音收尾判断。给出 JSON 状态：silent_ms=已静音毫秒；"
    "question_ms=距上一问句毫秒（-1=没有待答问句）；gentle=此前出现的温和信号文本；"
    "tension=是否处于紧张锁定（15 秒内刚发生过 strap2/vib2）；"
    "emotion=最近一句的音频情感标签（空=非语音输入）。"
    "判定规则：silent_ms≥1500 且本场有对话 → 可报 vib1；question_ms≥5000 → 可报 vib2；"
    "gentle 非空且 tension=false 且 emotion 为空/happy/neutral → 可报 warm；"
    "emotion 为 angry/sad/fearful/disgusted/surprised 时不报 warm。每个动作整场只报一次，条件不满足就不报。"
    "输出 JSON：{\"marks\":[{\"act\":\"动作\",\"why\":\"一句话依据\"}]}，可空数组。"
)


def llm_chat(system, user):
    """慢脑通用调用（deepseek-flash，thinking 按 config）。失败返回 None。"""
    llm = CFG.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key"):
        return None
    body = {
        "model": llm.get("model", "deepseek-flash"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": llm.get("temperature", 0.2),
        "response_format": {"type": "json_object"},
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
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log("慢脑调用失败：" + str(e)[:60])
        return None


def memory_append(row):
    """慢脑记忆沉淀：触发与复核结论统一写 memory.csv（端云闭环的数据底座）。"""
    try:
        import pipeline
        path = pipeline.get_path(CFG, "memory_csv", "memory.csv")
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "kind", "act", "evidence", "verdict", "text"])
            w.writerow(row)
    except Exception as e:
        print("[记忆] 写入失败：" + str(e)[:60])


def memory_summary(limit=12):
    try:
        import pipeline
        path = pipeline.get_path(CFG, "memory_csv", "memory.csv")
        with open(path, encoding="utf-8-sig") as f:
            return "".join(f.readlines()[-limit:])
    except Exception:
        return "（暂无历史记忆）"


def _csv_append(key, default_name, header, row):
    """通用小 CSV 追加（feedback.csv 训练反馈 / pins.csv 戳）：演示级落盘，手机回看后续接 App 层。"""
    try:
        import pipeline
        path = pipeline.get_path(CFG, key, default_name)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)
    except Exception as e:
        print("[落盘] " + key + " 写入失败：" + str(e)[:60])


# swipe_up/down 调输出频率 = 缩放触发冷却（默认 8s，来自 config iat.cooldown_sec）
BASE_COOLDOWN = float(CFG.get("iat", {}).get("cooldown_sec", 8))

def gesture_tempo(direction):
    """up=冷却 x0.6（更爱输出），down=冷却 x1.5（更克制）；返回频率倍率。"""
    if direction == "up":
        JUDGE.cooldown = max(2.0, JUDGE.cooldown * 0.6)
    else:
        JUDGE.cooldown = min(30.0, JUDGE.cooldown * 1.5)
    return round(JUDGE.cooldown / BASE_COOLDOWN, 2)


# 记忆压缩：memory.csv 每新增 20 行，LLM 压成一条摘要行（2026-11-05 用户确认）
COMPRESS_EVERY = 20
MEM_SEEN = {"rows": 0}          # 已压缩到的行数水位；首轮启动时对齐现有行数
COMPRESS_SYSTEM = (
    "你是手环 AI 的记忆压缩器。下面是这段时间的触发/复核/手势反馈原始记录（CSV 行）。"
    "压缩成 100 字以内的要点：保留用户偏好、反馈倾向（哪些动作被肯定/否定）、重要事件。"
    "直接输出摘要正文。"
)

def compressor_loop():
    """定期把 memory.csv 新增段落压缩成一条摘要行；慢脑提示词里天然可见（memory_summary 取尾部）。"""
    while not SHUTDOWN.is_set():
        time.sleep(15)
        try:
            if not STATE.get("llm"):
                continue
            import pipeline
            path = pipeline.get_path(CFG, "memory_csv", "memory.csv")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8-sig") as f:
                rows = [r for r in csv.reader(f) if r]
            n = max(0, len(rows) - 1)                     # 去表头
            if MEM_SEEN["rows"] == 0:
                MEM_SEEN["rows"] = n                      # 首轮：不压历史存量，只压新增
                continue
            if n - MEM_SEEN["rows"] < COMPRESS_EVERY:
                continue
            block = "\n".join(",".join(r) for r in rows[1 + MEM_SEEN["rows"]:1 + n])
            MEM_SEEN["rows"] = n
            summary = llm_chat(COMPRESS_SYSTEM, block)
            if summary:
                memory_append([time.strftime("%Y-%m-%d %H:%M:%S"), "压缩摘要", "-",
                               summary.strip()[:160], "-", "-"])
                log("记忆压缩：新增 " + str(COMPRESS_EVERY) + "+ 行已压成一条摘要（慢脑可见）")
        except Exception as e:
            print("[压缩] 失败：" + str(e)[:60])


SPK_CN = {"wearer": "本人（佩戴者）", "other": "对方", "unknown": "未判定"}


def _emo_line(emo, speaker="unknown"):
    """LLM 复核 / 慢脑共用：情感 + 说话人两行；无标签时明确写出，避免模型自己猜。"""
    emo = str(emo or "").strip()
    who = SPK_CN.get(str(speaker or "unknown"), "未判定")
    return ("\n该句音频情感标签：" + (emo or "无标签（非语音输入）") +
            "（happy/neutral=温和，可报 warm；angry/sad/fearful/disgusted/surprised=不温和，不报 warm）" +
            "\n该句说话人：" + who + "（本地声纹比对，只作上下文，不单独触发动作）")


def slow_on_sentence(s, emo="", speaker="unknown"):
    """每完成一句入队（语音模式与喂入模式统一走这里，天然伪流式）。
    情感与说话人随句子一起入队成快照，慢脑 worker 不读全局值。"""
    llm = CFG.get("llm", {})
    if not llm.get("slow_brain", True):
        return
    if not llm.get("enabled") or not llm.get("api_key"):
        return
    SLOWQ.put({"kind": "sentence", "text": s,
               "emotion": str(emo or ""), "speaker": str(speaker or "unknown")})


def slow_worker():
    """慢脑流式 worker：串行处理（约 1 秒/项）。句子项=补判/全判；静音项=纯慢脑收尾。"""
    while not SHUTDOWN.is_set():
        try:
            item = SLOWQ.get(timeout=1)
        except queue.Empty:
            continue
        try:
            if STATE["brain_mode"] == "fast":
                continue   # 纯快脑：慢脑停用，队列项直接丢弃
            if isinstance(item, str):
                item = {"kind": "sentence", "text": item, "emotion": "",
                        "speaker": "unknown"}
            meta_line = _emo_line(item.get("emotion", ""), item.get("speaker", "unknown"))
            if item.get("kind") == "silent":
                # 纯慢脑模式：静音收尾的 vib1/vib2/warm 由慢脑判断
                content = llm_chat(SLOW_SILENT_SYSTEM,
                                   json.dumps(item, ensure_ascii=False) + meta_line)
                acts = _parse_marks(content, allow_all=True)
                src = "慢脑(LLM)"
            elif STATE["brain_mode"] == "slow":
                # 纯慢脑模式：七个动作全部由慢脑判
                content = llm_chat(
                    SLOW_FULL_SYSTEM,
                    "诉求重复计数：" + json.dumps(JUDGE.repeat, ensure_ascii=False) +
                    "\n已触发动作：" + ("、".join(LAST_FIRED[-5:]) or "无") +
                    "\n最近记忆摘要：\n" + memory_summary(6) + meta_line +
                    "\n刚说完的这句话：" + str(item.get("text", "")))
                acts = _parse_marks(content, allow_all=True)
                src = "慢脑(LLM)"
            else:
                # 双脑模式：慢脑只补快脑漏的 vib3 / cool
                content = llm_chat(
                    SLOW_STREAM_SYSTEM,
                    "快脑刚触发的动作：" + ("、".join(LAST_FIRED[-5:]) or "无") +
                    "\n最近记忆摘要：\n" + memory_summary(6) + meta_line +
                    "\n刚说完的这句话：" + str(item.get("text", "")))
                acts = _parse_marks(content, allow_all=False)
                src = "慢脑(LLM)"
            for act, why in acts:
                JUDGE.fire(act, "慢脑流式补发：" + why, src)
        except Exception as e:
            print("[慢脑] 流式补发出错：" + str(e)[:60])


def _parse_marks(content, allow_all):
    """解析慢脑 marks；allow_all=False 时只放行 vib3/cool（双脑补漏模式）。"""
    if not content:
        return []
    try:
        import pipeline
        obj = pipeline.extract_json(content)
    except Exception:
        return []
    allowed = set(ra.ACTS) if allow_all else {"vib3", "cool"}
    out = []
    for m in (obj.get("marks", []) if isinstance(obj, dict) else []):
        act = str(m.get("act", "")).strip()
        if act in allowed:
            out.append((act, str(m.get("why", ""))[:60]))
    return out


# 接线：Judge 每完成一句 → 统一字幕区 + 慢脑队列；worker 常驻
# 喂入模式由 feed_session 自己逐句广播，这里只补实时语音（与喂入同路径、不重复显示）
VOICE_SEQ = {"i": 1000000}

def on_sentence_ui(s):
    # 同步链路里取本句情感与说话人快照；入队后即与全局状态解耦
    emo = PIECE_EMO["cur"]
    who = PIECE_SPEAKER["cur"]
    if not FEED_ACTIVE.is_set():
        VOICE_SEQ["i"] += 1
        broadcast("feed_piece", {"i": VOICE_SEQ["i"], "text": s,
                                 "source": "实时语音", "emotion": emo, "speaker": who})
    slow_on_sentence(s, emo, who)

JUDGE.on_sentence_cb = on_sentence_ui
threading.Thread(target=slow_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            p = BASE / "web" / "index.html"
            if not p.exists():
                self._json({"error": "web/index.html 不存在"}, 404)
                return
            body = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/iat-url":
            iat = CFG.get("iat", {})
            if not all(iat.get(k) for k in ("appid", "api_key", "api_secret")):
                self._json({"error": "config.json 的 iat 三件套没填齐"}, 500)
                return
            try:
                url = ra.build_auth_url(iat)
            except Exception as e:
                self._json({"error": "签名失败：" + str(e)[:80]}, 500)
                return
            self._json({"url": url, "appid": iat.get("appid", "")})
        elif path == "/api/state":
            self._json({
                "channel": STATE["channel"],
                "serial_port": STATE["serial_port"],
                "llm": STATE["llm"],
                "llm_model": CFG.get("llm", {}).get("model", ""),
                "llm_thinking": CFG.get("llm", {}).get("thinking", "disabled") == "enabled",
                "slow_brain": bool(CFG.get("llm", {}).get("slow_brain", True)),
                "brain_mode": STATE["brain_mode"],
                "mock_hr": int(CFG.get("mock_hr", 80)),
                "tempo": round(JUDGE.cooldown / BASE_COOLDOWN, 2) if BASE_COOLDOWN else 1.0,
                "hr_rhythm_active": HR_RHYTHM["active"],
                "hr_rhythm_freeze": max(0, int(HR_RHYTHM["freeze_until"] - time.time())),
                "llm_note": STATE.get("llm_note", ""),
                "speaker": spk.status(),
                "acts": {k: v[0] for k, v in ra.ACTS.items()},
            })
        elif path == "/api/ports":
            # 枚举本机真实串口，供网页下拉框动态填充（不再写死 COM3~COM8）
            try:
                import serial.tools.list_ports as lp
                ports = [{"port": p.device,
                          "desc": (p.description or "")[:48]}
                         for p in lp.comports()]
            except Exception as e:
                ports = []
                log("枚举串口失败：" + str(e)[:60])
            self._json({"ports": ports})
        elif path == "/api/events":
            self.handle_sse()
        else:
            self._json({"error": "not found"}, 404)

    def handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = queue.Queue()
        with SSE_LOCK:
            SSE_CLIENTS.append(q)
        try:
            while not SHUTDOWN.is_set():
                try:
                    name, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = ("event: " + name + "\ndata: " +
                           json.dumps(data, ensure_ascii=False) + "\n\n")
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with SSE_LOCK:
                if q in SSE_CLIENTS:
                    SSE_CLIENTS.remove(q)

    def do_POST(self):
        path = self.path.split("?")[0]
        # body 只读一次：JSON 端点解析 body，二进制上传端点（docx/音频）直接用 raw
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {}
        if path == "/api/piece":
            piece = str(body.get("text", ""))
            status = int(body.get("status", 1))
            if piece:
                JUDGE.on_piece(piece, status)
            self._json({"ok": True, "full": JUDGE.full_text[-300:]})
        elif path == "/api/asr-rt/start":
            ok, info = qwen_rt_start()
            self._json({"ok": ok, "sid": info if ok else "",
                        "error": "" if ok else info}, 200 if ok else 500)
        elif path == "/api/asr-rt/append":
            audio = str(body.get("audio", ""))
            # 同一块 PCM 留一份给本地声纹：.text 临时句不结算，.completed 时整段取走
            TURN_AUDIO["chunks"].append(audio)
            if len(TURN_AUDIO["chunks"]) > 600:
                del TURN_AUDIO["chunks"][:-600]   # 60 秒防御上限，最长会话 55 秒
            ok, err = qwen_rt_send({"event_id": "evt_" + str(int(time.time() * 1000)),
                                    "type": "input_audio_buffer.append", "audio": audio})
            self._json({"ok": ok, "error": err}, 200 if ok else 409)
        elif path == "/api/asr-rt/stop":
            qwen_rt_send({"event_id": "evt_finish", "type": "session.finish"})
            # 收尾结果仍由接收线程处理，收到 session.finished 后自动关连接
            self._json({"ok": True})
        elif path == "/api/speaker/enroll/start":
            r = spk.enroll_start()
            PIECE_SPEAKER["cur"] = "unknown"
            broadcast("speaker_state", spk.status())
            self._json(r)
        elif path == "/api/speaker/enroll/append":
            r = spk.enroll_append(str(body.get("audio", "")))
            if r.get("ok") and r.get("active_ms", 0) % 1000 < 150:
                broadcast("speaker_state", spk.status())   # 进度节流，约每秒一次
            self._json(r)
        elif path == "/api/speaker/enroll/finish":
            r = spk.enroll_finish()
            broadcast("speaker_state", spk.status())
            log("声纹录入" + ("成功：有效语音 " + str(r.get("active_sec")) + " 秒"
                              if r.get("ok") else "失败：" + str(r.get("error", ""))))
            self._json(r, 200 if r.get("ok") else 409)
        elif path == "/api/speaker/clear":
            r = spk.clear()
            PIECE_SPEAKER["cur"] = "unknown"
            broadcast("speaker_state", spk.status())
            self._json(r)
        elif path == "/api/talk":
            ev = body.get("event")
            if ev == "start":
                TALKING.set()
                broadcast("talk", {"talking": True})
            elif ev == "stop":
                TALKING.clear()
                STOP_AT["t"] = time.time()
                JUDGE.flush_tail()
                broadcast("talk", {"talking": False})
            self._json({"ok": True})
        elif path == "/api/llm-thinking":
            # 实时开关：只改内存里的 CFG，下一次 LLM 复核立即生效，无需刷新/重启
            on = bool(body.get("on"))
            CFG.setdefault("llm", {})["thinking"] = "enabled" if on else "disabled"
            log("LLM 思考链 -> " + ("开（约 2.3 秒，更稳）" if on else "关（约 0.7 秒，实时）"))
            broadcast("state", {"llm_thinking": on})
            self._json({"ok": True, "thinking": on})
        elif path == "/api/act":
            global LAST_FIRE_TS
            act = str(body.get("act", ""))
            if act not in ra.ACTS:
                self._json({"ok": False, "error": "未知动作 " + act}, 400)
                return
            ok, hw = downlink(json.dumps({"act": act}, ensure_ascii=False), manual=True)
            LAST_FIRE_TS = time.time()   # 手动测试也算 AI 输出：3 秒内 tap 可作反馈
            LAST_FIRED.append(act)
            del LAST_FIRED[:-5]
            # 手动测试也点亮卡片（来源标明"手动测试"，与判定触发区分，不走 LLM）
            broadcast("trigger", {"act": act, "cn": ra.ACTS[act][0],
                                  "evidence": "手动测试按钮直接下发", "source": "手动测试",
                                  "t": time.strftime("%H:%M:%S")})
            self._json({"ok": ok, "hw": hw})
        elif path == "/api/hr":
            # 手动控制模拟心率：联动快脑 cool 规则（mock_hr 与静息差 ≥ 阈值即触发）
            try:
                hr = int(body.get("hr"))
            except Exception:
                hr = 0
            if hr < 40 or hr > 180:
                self._json({"ok": False, "error": "心率超出合理范围（40-180 BPM）"}, 400)
                return
            CFG["mock_hr"] = hr   # 只改本会话内存，config.json 不动
            broadcast("state", {"mock_hr": hr})
            tip = ("（≥95，启动 " + str(CFG.get("hr_rhythm_beats", 7)) + " 下节律振动，" +
                   str(CFG.get("hr_calm_bpm", 30)) + " BPM）") if hr - int(CFG.get("resting_hr", 80)) >= int(CFG.get("threshold_hr_delta", 15)) else "（心率已回到平静，7 下打完仍会照常完成）"
            if HR_RHYTHM["active"]:
                tip = "（节律振动进行中，第 " + str(HR_RHYTHM["beats"]) + " 下）"
            elif time.time() < HR_RHYTHM["freeze_until"]:
                tip = "（冻结中，剩余 " + str(int(HR_RHYTHM["freeze_until"] - time.time())) + " 秒）"
            log("模拟心率手动调整 -> " + str(hr) + " BPM " + tip)
            self._json({"ok": True, "hr": hr,
                        "rhythm_active": HR_RHYTHM["active"],
                        "freeze_remaining": max(0, int(HR_RHYTHM["freeze_until"] - time.time()))})
        elif path == "/api/hr/unfreeze":
            # 录屏重拍用：只清内存里的节律冻结与进行中的 7 下，
            # config.json、七个输出、触发规则一律不动；下次心率 ≥95 可重新触发
            left = max(0, int(HR_RHYTHM["freeze_until"] - time.time()))
            was_active = HR_RHYTHM["active"]
            HR_RHYTHM["active"] = False
            HR_RHYTHM["beats"] = 0
            HR_RHYTHM["next"] = 0.0
            HR_RHYTHM["freeze_until"] = 0.0
            log("手动解除心率节律冻结（原剩余 " + str(left) + " 秒，振动中=" +
                str(was_active) + "）；心率 ≥95 可重新触发")
            broadcast("hr_unfreeze", {"ok": True})
            self._json({"ok": True, "cleared_freeze": left, "was_active": was_active})
        elif path == "/api/gesture":
            # 触摸上行手势（语义 v1.3，2026-11-05 用户确认）：
            #   tap1 肯定/戳 · tap2 否定/戳 · long 静音 · swipe 调输出频率 · rub 摇尾巴
            # 所有手势写入 memory.csv（LLM 慢脑提示词可见）；反馈窗口 3 秒内 AI 刚触发过 → 训练数据
            g = str(body.get("g", ""))
            if g not in ("tap1", "tap2", "long", "swipe_up", "swipe_down", "rub"):
                self._json({"ok": False, "error": "未知手势 " + g}, 400)
                return
            gesture_cn = {"tap1": "肯定/戳", "tap2": "否定/戳", "long": "静音开关",
                          "swipe_up": "加大输出频率", "swipe_down": "减小输出频率",
                          "rub": "逗 AI（开心表情）"}
            ms = int(time.monotonic() * 1000)
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            payload = json.dumps({"ch": "ui", "g": g, "ms": ms})
            topic = CFG.get("mqtt", {}).get("topic_ui", "wrist/ui")
            ok, note = mqtt_publish(topic, payload)
            sem = gesture_cn[g]
            if g == "long":
                sem += "（点一次静音 / 再点一次起来）"
            extra = ""
            snap = JUDGE.full_text[-120:]
            if g in ("tap1", "tap2"):
                recent = LAST_FIRE_TS > 0 and (time.time() - LAST_FIRE_TS) <= 3.0
                if recent:
                    # 3 秒窗口内 AI 刚输出过 → 用户对 AI 行为的肯定/否定 = 训练数据
                    kind = "肯定" if g == "tap1" else "否定"
                    act = LAST_FIRED[-1] if LAST_FIRED else "?"
                    _csv_append("feedback_csv", "feedback.csv",
                                ["time", "gesture", "feedback", "act", "text"],
                                [now_str, g, kind, act, snap])
                    memory_append([now_str, "手势反馈", kind + "->" + act,
                                   "AI 触发后 3 秒内的用户反馈（训练数据）", "-", snap])
                    sem += "（对 " + act + " 的" + kind + "，已存训练集）"
                else:
                    # 窗口外 → 戳：记下此刻，供之后在手机上回看转写/音频
                    _csv_append("pins_csv", "pins.csv",
                                ["time", "gesture", "kind", "text"],
                                [now_str, g, "戳", snap])
                    memory_append([now_str, "戳", g, "用户戳住此刻（可回看）", "-", snap])
                    sem += "（无 AI 输出，已戳住此刻）"
            elif g in ("swipe_up", "swipe_down"):
                tempo = gesture_tempo("up" if g == "swipe_up" else "down")
                broadcast("state", {"tempo": tempo})
                memory_append([now_str, "手势", sem,
                               "触发冷却 -> " + format(JUDGE.cooldown, ".1f") +
                               "s（频率倍率 x" + str(tempo) + "）", "-", snap])
                extra = " 冷却=" + format(JUDGE.cooldown, ".1f") + "s（x" + str(tempo) + "）"
            elif g == "rub":
                memory_append([now_str, "手势", "rub", "用户摸头互动（屏幕开心表情）", "-", snap])
            elif g == "long":
                # 固件只订阅 wrist/cmd，wrist/ui 上行它收不到；
                # 故额外下发一条维护指令，让调试台同一个 long 按钮也能静音 / 起来
                downlink(json.dumps({"ch": "sys", "cmd": "long"}, ensure_ascii=False))
                memory_append([now_str, "手势", "long",
                               "调试台 long → 固件静音开关（点一次静音 / 再点一次起来）", "-", snap])
                extra = " 已下发固件（静音开关）"
            # 所有手势驱动表情屏（long 除外：它由固件本地做闭眼/睁眼开关机，别再睁开）
            face = {"tap1": "laugh", "tap2": "confused", "swipe_up": "excited",
                    "swipe_down": "tired", "rub": "wink"}.get(g)
            if face:
                downlink(json.dumps({"act": "face", "f": face}, ensure_ascii=False))
                extra += " 表情=" + face
            broadcast("gesture", {"g": g, "cn": sem, "payload": payload,
                                  "published": ok, "t": time.strftime("%H:%M:%S")})
            log("[触摸上行] " + g + " " + sem + extra +
                " -> " + topic + " " + ("已发布" if ok else "MQTT 未连，仅调试台可视化"))
            self._json({"ok": True, "published": ok, "payload": payload, "sem": sem})
        elif path == "/api/brain":
            mode = str(body.get("mode", "both"))
            if mode not in ("both", "fast", "slow"):
                self._json({"ok": False, "error": "未知脑模式"}, 400)
                return
            STATE["brain_mode"] = mode
            JUDGE.rules_enabled = (mode != "slow")   # 纯慢脑：快脑只记账不触发
            log("脑模式切换为 " + {"both": "双脑（快脑保底+慢脑补漏）",
                                   "fast": "纯快脑（慢脑停用）",
                                   "slow": "纯慢脑（快脑只记账，七动作全归慢脑）"}[mode])
            broadcast("state", {"brain_mode": mode})
            self._json({"ok": True, "brain_mode": mode})
        elif path == "/api/feed":
            if FEED_ACTIVE.is_set():
                self._json({"ok": False, "error": "已有喂入会话进行中，等字幕区跑完再喂"}, 409)
                return
            text = str(body.get("text", "")).strip()
            source = str(body.get("source", "手动输入"))[:80]
            if not text:
                self._json({"ok": False, "error": "空文本"}, 400)
                return
            if len(text) > 50000:
                self._json({"ok": False, "error": "文本太长（>5 万字），先拆分"}, 400)
                return
            threading.Thread(target=feed_session, args=(text, source), daemon=True).start()
            self._json({"ok": True, "note": "逐句流式判定进行中，看字幕区与触发卡片"})
        elif path == "/api/feed-docx":
            if FEED_ACTIVE.is_set():
                self._json({"ok": False, "error": "已有喂入会话进行中，等字幕区跑完再喂"}, 409)
                return
            fname = self.headers.get("X-Filename", "")
            if not fname.lower().endswith(".docx"):
                self._json({"ok": False, "error": "只支持 .docx（旧版 .doc 请先另存为 docx）"}, 400)
                return
            if not raw or len(raw) > 10 * 1024 * 1024:
                self._json({"ok": False, "error": "文件为空或超过 10MB"}, 400)
                return
            try:
                text = docx_extract(raw)
            except Exception as e:
                self._json({"ok": False, "error": "docx 解析失败：" + str(e)[:80]}, 400)
                return
            if not text:
                self._json({"ok": False, "error": "docx 里没有可提取的文本"}, 400)
                return
            threading.Thread(target=feed_session, args=(text, fname), daemon=True).start()
            self._json({"ok": True, "note": "docx 已提取 " + str(len(text)) + " 字，逐句流式判定进行中"})
        elif path == "/api/feed-audio":
            if FEED_ACTIVE.is_set():
                self._json({"ok": False, "error": "已有喂入会话进行中，等字幕区跑完再喂"}, 409)
                return
            fname = self.headers.get("X-Filename", "feed.mp3")
            suffix = fname.lower()[-4:]
            if suffix not in (".mp3", ".wav", ".m4a"):
                self._json({"ok": False, "error": "只支持 mp3 / wav / m4a"}, 400)
                return
            if not raw or len(raw) > 8 * 1024 * 1024:
                self._json({"ok": False, "error": "文件为空或超过 8MB（qwen3-asr 单次上限）"}, 400)
                return
            tmp = BASE / ("_feed_audio" + suffix)
            tmp.write_bytes(raw)

            def audio_job():
                try:
                    broadcast("log", {"line": "音频 ASR 开始（qwen3-asr-flash 整段上传，约需十几秒）…"})
                    import pipeline
                    _segs, meta = pipeline.run_asr_cloud(CFG, str(tmp))
                    text = (meta.get("text") or "").strip()
                    if text:
                        log("音频转写完成：" + str(len(text)) + " 字，开始逐句喂入")
                        feed_session(text, fname)
                    else:
                        broadcast("log", {"line": "音频 ASR 没有返回文本（检查 config 的 asr.cloud.key / 文件是否有效语音）"})
                finally:
                    try:
                        tmp.unlink()
                    except Exception:
                        pass

            threading.Thread(target=audio_job, daemon=True).start()
            self._json({"ok": True, "note": "音频转写进行中，完成后自动逐句喂入"})
        elif path == "/api/channel":
            ch = str(body.get("channel", "mock"))
            if ch not in ("mock", "serial", "mqtt"):
                self._json({"ok": False, "error": "未知通道"}, 400)
                return
            if ch != STATE["channel"]:
                if SERIAL["s"] is not None:
                    try:
                        SERIAL["s"].close()
                    except Exception:
                        pass
                    SERIAL["s"] = None
                if MQTT["c"] is not None:
                    try:
                        MQTT["c"].loop_stop()
                        MQTT["c"].disconnect()
                    except Exception:
                        pass
                    MQTT["c"] = None
            STATE["channel"] = ch
            STATE["serial_port"] = str(body.get("port") or STATE["serial_port"])
            # 通道选择落盘：服务重启后不再回到 mock（之前反复踩坑）
            try:
                (_PATH_ROOT / ".channel").write_text(ch, encoding="utf-8")
            except Exception:
                pass
            log("下发通道切换为 " + ch + ("/" + STATE["serial_port"] if ch == "serial" else ""))
            broadcast("state", {"channel": ch, "serial": "未打开"})
            self._json({"ok": True, "channel": ch})
        else:
            self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="触觉社交手环本地可视化调试网页")
    ap.add_argument("--host", default=HOST_DEFAULT)
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--serial", action="store_true", help="启动即用串口下发")
    ap.add_argument("--mqtt", action="store_true", help="启动即用 MQTT 下发")
    ap.add_argument("--port-name", default="", help="串口号，如 COM3")
    args = ap.parse_args()

    # 命令行参数优先；没有参数时恢复上次保存的通道（默认仍为 mock）
    if args.mqtt:
        STATE["channel"] = "mqtt"
    elif args.serial:
        STATE["channel"] = "serial"
    else:
        try:
            saved = (_PATH_ROOT / ".channel").read_text(encoding="utf-8").strip()
            if saved in ("mock", "serial", "mqtt"):
                STATE["channel"] = saved
        except Exception:
            pass
    if args.port_name:
        STATE["serial_port"] = args.port_name

    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=hr_rhythm_loop, daemon=True).start()
    threading.Thread(target=gesture_sub_loop, daemon=True).start()
    threading.Thread(target=compressor_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://" + args.host + ":" + str(args.port)
    print("=" * 66)
    print(" 触觉社交手环 · 本地可视化调试网页")
    print(" 打开浏览器：" + url)
    print(" 下发通道：" + STATE["channel"] +
          ("（" + STATE["serial_port"] + "）" if STATE["channel"] == "serial" else ""))
    print(" LLM：" + CFG.get("llm", {}).get("model", "?") + "（thinking 已关）" if STATE["llm"]
          else " LLM：关闭")
    print(" 说明：按住网页按钮说话 -> Qwen 实时版边说边出字 + 7 类情感 -> 规则层/LLM 判定七个输出")
    print("       mock=只可视化；serial=真下发并读固件回显确认硬件执行")
    print(" Ctrl+C 退出")
    print("=" * 66)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[结束] Ctrl+C")
    finally:
        SHUTDOWN.set()
        if SERIAL["s"]:
            try:
                SERIAL["s"].close()
            except Exception:
                pass
        if MQTT["c"]:
            try:
                MQTT["c"].loop_stop()
                MQTT["c"].disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    main()
