# -*- coding: utf-8 -*-
"""
触觉社交手环 - 对话音频生成器（11.5 前置物料）

作用：
  读 dialogue_script.csv -> edge-tts 生成甲乙双角色语音 -> 按时间轴拼接
  -> 导出 dialogue.wav（16kHz/16bit/单声道，默认给云端 qwen3-asr-flash）与 dialogue.mp3（给视频）
  -> 写 dialogue_timeline.csv（触发时间轴，给 demo_chain.py / 字幕 / M3 验收）

怎么跑（在本文件所在文件夹）：
    python make_dialogue.py             首次生成
    python make_dialogue.py --force     全部重新生成（改了语音名或台词时用）

预期看到：
    [1/3] 逐句打印 [生成] 或 [复用]，以及每句结束于第几秒
    [2/3] dialogue.wav / dialogue.mp3 导出
    [3/3] dialogue_timeline.csv（触发点 N 个）
    完成：音频总时长 xx.x 秒

依赖：edge-tts / pydub / imageio-ffmpeg（双击 一键装环境.bat 一次装好）
注意：edge-tts 要联网。已生成的 audio_parts/*.mp3 会自动复用，断网重跑不会全丢。
"""

import asyncio
import csv
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
SCRIPT_CSV = BASE / "dialogue_script.csv"
TIMELINE_CSV = BASE / "dialogue_timeline.csv"
WAV_OUT = BASE / "dialogue.wav"
MP3_OUT = BASE / "dialogue.mp3"
PARTS_DIR = BASE / "audio_parts"

DEFAULT_VOICES = {"甲": "zh-CN-YunxiNeural", "乙": "zh-CN-XiaoxiaoNeural"}
RATE = 16000

TIMELINE_FIELDS = ["t_ms", "segment", "cue_id", "act", "source",
                   "evidence", "cmd_json", "speaker", "text", "note"]


def setup_console():
    """中文控制台兼容：代码页是 UTF-8 时让 Python 也输出 UTF-8，否则用系统默认（中文 Windows 默认 GBK，同样正常）"""
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
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print("[提示] config.json 读不了，改用默认配置：" + str(e))
        return {}


def read_script():
    if not SCRIPT_CSV.exists():
        raise SystemExit("[错误] 找不到 dialogue_script.csv，请确认它和本脚本在同一个文件夹。")
    with SCRIPT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("[错误] dialogue_script.csv 是空的。")
    for r in rows:
        cj = (r.get("cmd_json") or "").strip()
        if cj:
            try:
                json.loads(cj)
            except Exception:
                raise SystemExit("[错误] " + str(r.get("cue_id", "?")) +
                                 " 的 cmd_json 不是合法 JSON：" + cj)
    return rows


def get_audio_segment():
    try:
        from pydub import AudioSegment
        import imageio_ffmpeg
        AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()
        return AudioSegment
    except Exception as e:
        raise SystemExit("[错误] 音频依赖没装好，请双击 一键装环境.bat 后重试。原始信息：" + str(e))


async def synth_one(text, voice, out_path):
    import edge_tts
    comm = edge_tts.Communicate(text, voice)
    await comm.save(str(out_path))


def main():
    setup_console()
    print("=" * 56)
    print(" 触觉社交手环 - 对话音频生成器")
    print("=" * 56)

    force = "--force" in sys.argv
    rows = read_script()
    cfg = load_config()
    voices = dict(DEFAULT_VOICES)
    for k, v in cfg.get("voices", {}).items():
        if not k.startswith("_"):
            voices[k] = v

    AudioSegment = get_audio_segment()
    PARTS_DIR.mkdir(exist_ok=True)

    print("[1/3] 生成双角色语音（edge-tts 需要联网，已生成的会自动复用）")
    combined = AudioSegment.silent(duration=0, frame_rate=RATE).set_channels(1).set_sample_width(2)
    timeline = []
    t = 0

    for r in rows:
        cid = (r.get("cue_id") or "").strip()
        seg = (r.get("segment") or "").strip()
        text = (r.get("text") or "").strip()
        speaker = (r.get("speaker") or "").strip()
        cj = (r.get("cmd_json") or "").strip()
        gap = int((r.get("gap_ms") or "0").strip() or 0)

        if seg == "fullchain":
            if gap > 0:
                combined += AudioSegment.silent(duration=gap, frame_rate=RATE)
                t += gap
            if text and speaker in voices:
                part = PARTS_DIR / (cid + ".mp3")
                if force or not part.exists():
                    print("    [生成] " + cid + " " + speaker + "：" + text)
                    try:
                        asyncio.run(synth_one(text, voices[speaker], part))
                    except Exception as e:
                        raise SystemExit("[错误] " + cid +
                                         " 语音生成失败。检查网络后重跑（已生成的句子会复用）。\n" + str(e))
                else:
                    print("    [复用] " + part.name)
                clip = AudioSegment.from_file(str(part), format="mp3")
                clip = clip.set_frame_rate(RATE).set_channels(1).set_sample_width(2)
                combined += clip
                t += len(clip)
                print("    [时间轴] " + cid + " 结束于 " + format(t / 1000.0, ".1f") + " 秒")
        elif cj:
            print("    [特写段] " + cid + " 不进音频，只登记指令")

        if cj:
            timeline.append({
                "t_ms": t if seg == "fullchain" else -1,
                "segment": seg,
                "cue_id": cid,
                "act": (r.get("act") or "").strip(),
                "source": (r.get("source") or "").strip(),
                "evidence": (r.get("evidence") or "").strip(),
                "cmd_json": cj,
                "speaker": speaker,
                "text": text,
                "note": (r.get("note") or "").strip(),
            })

    print("[2/3] 导出音频")
    combined.export(str(WAV_OUT), format="wav")
    print("    dialogue.wav  (16kHz/16bit/单声道，默认给云端 qwen3 ASR)")
    try:
        combined.export(str(MP3_OUT), format="mp3")
        print("    dialogue.mp3  (给视频用)")
    except Exception as e:
        print("    [提示] mp3 没导出来，不影响使用（wav 已生成）：" + str(e))

    print("[3/3] 写触发时间轴")
    with TIMELINE_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TIMELINE_FIELDS)
        w.writeheader()
        for item in timeline:
            w.writerow(item)
    print("    dialogue_timeline.csv（触发点 " + str(len(timeline)) + " 个）")

    print("-" * 56)
    print("完成：音频总时长 " + format(len(combined) / 1000.0, ".1f") + " 秒")
    print("下一步：python demo_chain.py --mock")
    print("-" * 56)


if __name__ == "__main__":
    main()
