# -*- coding: utf-8 -*-
"""
触觉社交手环 - 轻量指令下发 / mock 假全链路（硬件未到也能跑）

读 dialogue_timeline.csv（make_dialogue.py 生成），按时间顺序把七个输出指令：
    --mock              只打印，不需要硬件、不需要网络（默认）
    --serial --port COM3 从串口发给 ESP32
    --mqtt              发到 broker.emqx.io 的 wrist/cmd
无论哪种模式都写 triggers.csv（视频字幕源）

怎么跑：
    python demo_chain.py --mock
    python demo_chain.py --mock --realtime
    python demo_chain.py --serial --port COM3
    python demo_chain.py --mqtt

完整的 ASR + LLM 流水线见 pipeline.py。
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TIMELINE_CSV = BASE / "dialogue_timeline.csv"
TRIGGERS_CSV = BASE / "triggers.csv"

ACTS = {
    "vib1":   ("振动一下", "震动"),
    "vib2":   ("振动两下", "震动"),
    "vib3":   ("振动三下", "震动"),
    "strap1": ("收紧 1 档", "收紧"),
    "strap2": ("收紧 2 档", "收紧"),
    "cool":   ("凉", "温度"),
    "warm":   ("热", "温度"),
}


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


def load_timeline():
    if not TIMELINE_CSV.exists():
        raise SystemExit("[错误] 找不到 dialogue_timeline.csv，请先运行 python make_dialogue.py")
    with TIMELINE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        act = (r.get("act") or r.get("cue") or "").strip()
        if act not in ACTS:
            continue
        cmd = (r.get("cmd_json") or "").strip()
        if not cmd:
            cmd = json.dumps({"act": act}, ensure_ascii=False)
        out.append({
            "t_ms": int((r.get("t_ms") or "-1").strip() or -1),
            "cue_id": (r.get("cue_id") or "").strip(),
            "act": act,
            "source": (r.get("source") or "").strip(),
            "evidence": (r.get("evidence") or "").strip(),
            "cmd_json": cmd,
            "note": (r.get("note") or "").strip(),
        })
    return out


def make_sender(mode, port, baud, broker, topic):
    if mode == "mock":
        return lambda cmd: None

    if mode == "serial":
        try:
            import serial
        except ImportError:
            raise SystemExit("[错误] 缺 pyserial，请双击 一键装环境.bat")
        try:
            s = serial.Serial(port, baud, timeout=0.5)
        except Exception as e:
            raise SystemExit("[错误] 打不开串口 " + port +
                             "。去 Arduino IDE 的 工具-端口 看真实 COM 号；"
                             "串口监视器要先关掉。\n" + str(e))
        print("[串口] 等待固件启动日志（最多 20 秒）...")
        deadline = time.time() + 20
        while time.time() < deadline:
            line = s.readline().decode("utf-8", errors="ignore")
            if line:
                print("    [板子] " + line.strip())
            if "七个输出" in line:
                print("[串口] 固件已就绪")
                break

        def send(cmd):
            s.write((cmd + "\n").encode("utf-8"))
            s.flush()
        return send

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        raise SystemExit("[错误] 缺 paho-mqtt，请双击 一键装环境.bat")
    cid = "demo-chain-" + str(int(time.time()))
    try:
        c = mqtt.Client(client_id=cid)
    except Exception:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=cid)
    try:
        c.connect(broker, 1883, 60)
        c.loop_start()
    except Exception as e:
        raise SystemExit("[错误] 连不上 " + broker + "：" + str(e))
    print("[MQTT] 已连接 " + broker + "，发布 " + topic)
    return lambda cmd: c.publish(topic, cmd)


def main():
    setup_console()
    ap = argparse.ArgumentParser(description="触觉社交手环 七个输出 指令下发")
    ap.add_argument("--mock", action="store_true", help="只打印（默认）")
    ap.add_argument("--serial", action="store_true", help="串口发给 ESP32")
    ap.add_argument("--mqtt", action="store_true", help="发到 MQTT")
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--broker", default="broker.emqx.io")
    ap.add_argument("--topic", default="wrist/cmd")
    ap.add_argument("--realtime", action="store_true", help="按音频真实时间推进")
    args = ap.parse_args()

    mode = "mqtt" if args.mqtt else ("serial" if args.serial else "mock")
    triggers = load_timeline()
    if not triggers:
        raise SystemExit("[错误] 时间轴里没有七个输出的触发点，检查 dialogue_timeline.csv。")

    print("=" * 60)
    print(" 触觉社交手环 - 七个输出 指令下发（模式：" + mode + "）")
    print("=" * 60)
    send = make_sender(mode, args.port, args.baud, args.broker, args.topic)

    t0 = time.time()
    rows = []
    for i, t in enumerate(triggers, 1):
        if args.realtime and t["t_ms"] >= 0:
            wait = t0 + t["t_ms"] / 1000.0 - time.time()
            if wait > 0:
                time.sleep(wait)
        tstr = format(t["t_ms"] / 1000.0, ".1f") + "s" if t["t_ms"] >= 0 else "-"
        cn, ch = ACTS[t["act"]]
        print("[" + str(i) + "] t=" + tstr + "  " + t["act"] + " (" + cn + ")  来源=" +
              (t["source"] or "期望表"))
        print("     依据：" + (t["evidence"] or "无"))
        print("     指令：" + t["cmd_json"])
        if t["note"]:
            print("     备注：" + t["note"])
        send(t["cmd_json"])
        rows.append([tstr, t["source"] or "期望表", t["act"], cn,
                     t["evidence"], t["cmd_json"], ch, t["note"]])

    with TRIGGERS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "来源", "act", "中文", "证据原文", "指令JSON", "通道", "备注"])
        w.writerows(rows)

    if mode == "mqtt":
        time.sleep(0.5)
    print("-" * 60)
    print("完成：" + str(len(rows)) + " 个触发，顺序 " + " -> ".join(r[2] for r in rows))
    if mode == "mock":
        print("mock 模式只打印。把上面的 JSON 粘进 Arduino 串口监视器就能驱动真硬件。")
    print("-" * 60)


if __name__ == "__main__":
    main()
