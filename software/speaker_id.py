# -*- coding: utf-8 -*-
"""
触觉社交手环 · 本地说话人区分（演示级，2026-11-06）

定位：给「按住说话」的每个 VAD 成句打上 wearer(本人) / other(对方) / unknown(未判定)。
零网络、零模型文件、只用 numpy；声纹模板只存内存，不落盘、不上传，重启即失效。

为什么不用云端（详见 ../说话人区分-设计规范.md）：
  百炼 qwen3-asr-flash / qwen3-asr-flash-realtime 都不支持说话人分离；
  filetrans 系的 diarization_enabled 是「音频内聚类」，跨会话不保证同一人，而且是异步文件转写；
  阿里云 ISI 的声纹识别要读 8 位数字串做 1:1 身份认证，官方明确「不适用于多人对话中识别不同说话人」。

算法（可解释、演示够用，不是身份认证级）：
  16k PCM16 -> 25ms 汉明窗 / 10ms 帧移 -> 40 维 mel 对数能量 -> DCT 取 16 维 MFCC
  -> 有效语音帧的「均值 + 标准差」拼 32 维 -> L2 归一化
  注册模板与每个 VAD turn 的向量做余弦相似度：
  >= threshold 判本人，< unknown_below 判对方，中间判未判定。
  句级对齐依据：Qwen 实时版用 server_vad(400ms) 断句，一个 .completed 对应一个 turn，
  后端按 append 到达顺序累积该 turn 的 PCM，事件到达时结算并清空。
"""

import base64
import time

import numpy as np

SR = 16000
FRAME = 400                 # 25 ms
HOP = 160                   # 10 ms
N_FFT = 512
N_MELS = 40
N_MFCC = 16
VAD_RMS = 0.012             # 有效语音能量门槛

DEFAULTS = {
    "enroll_sec": 8,        # 录入总时长（秒）
    "min_active_sec": 5,    # 有效语音下限（对齐阿里 ISI 声纹基础版 reg_active_speech_length=5）
    "threshold": 0.82,      # 余弦 >= 此值判本人
    "unknown_below": 0.65,  # 余弦 < 此值判对方，中间为未判定
    "min_tag_sec": 0.4,     # 单句有效语音太短则不下结论
}
P = dict(DEFAULTS)

STATE = {"mode": "idle", "buf": [], "active_ms": 0, "template": None,
         "enrolled_at": "", "last": {"speaker": "unknown", "score": 0.0}}


def configure(d):
    for k in DEFAULTS:
        if k in d:
            P[k] = d[k]


def _mel(f):
    return 2595.0 * np.log10(1.0 + f / 700.0)


def _bank():
    hz = 700.0 * (10 ** (np.linspace(_mel(0), _mel(SR / 2), N_MELS + 2) / 2595.0) - 1.0)
    bins = np.floor((N_FFT + 1) * hz / SR).astype(int)
    b = np.zeros((N_MELS, N_FFT // 2 + 1))
    for m in range(1, N_MELS + 1):
        l = int(bins[m - 1])
        c = max(int(bins[m]), l + 1)
        r = max(int(bins[m + 1]), c + 1)
        for k in range(l, min(c, b.shape[1])):
            b[m - 1, k] = (k - l) / (c - l)
        for k in range(c, min(r, b.shape[1])):
            b[m - 1, k] = (r - k) / (r - c)
    return b


_BANK = _bank()
_DCT = np.cos(np.pi * np.arange(N_MFCC)[:, None] *
              (2 * np.arange(N_MELS) + 1) / (2 * N_MELS))


def decode(b64):
    """前端每块是 100ms = 3200B PCM16/16k 小端，base64。"""
    raw = base64.b64decode(str(b64 or ""))
    if len(raw) % 2:
        raw = raw[:-1]
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


def _frames(x):
    if x.size < FRAME:
        return None
    n = 1 + (x.size - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    f = x[idx] * np.hamming(FRAME)[None, :]
    return f, np.sqrt((f * f).mean(axis=1))


def _active_ms(x):
    fr = _frames(x)
    return int((fr[1] > VAD_RMS).sum() * 10) if fr else 0


def embed(x):
    """有效语音帧的 MFCC 均值+标准差 -> 32 维 L2 归一化向量。"""
    fr = _frames(np.asarray(x, dtype=np.float64))
    if fr is None:
        return None
    f, rms = fr
    keep = rms > VAD_RMS
    if keep.sum() < 5:
        return None
    spec = np.abs(np.fft.rfft(f, n=N_FFT, axis=1)) ** 2
    mel = np.log(np.maximum(spec @ _BANK.T, 1e-10))
    mf = (mel @ _DCT.T)[keep]
    v = np.concatenate([mf.mean(axis=0), mf.std(axis=0)])
    norm = np.linalg.norm(v)
    return (v / norm) if norm > 1e-8 else None


def enroll_start():
    STATE.update(mode="enroll", buf=[], active_ms=0)
    return {"ok": True, "seconds": P["enroll_sec"],
            "min_active_sec": P["min_active_sec"]}


def enroll_append(b64):
    if STATE["mode"] != "enroll":
        return {"ok": False, "error": "没有进行中的录入"}
    x = decode(b64)
    STATE["buf"].append(x)
    STATE["active_ms"] += _active_ms(x)
    return {"ok": True, "active_ms": STATE["active_ms"],
            "total_ms": int(sum(len(c) for c in STATE["buf"]) * 1000 / SR)}


def enroll_finish():
    STATE["mode"] = "idle"
    total_ms = int(sum(len(c) for c in STATE["buf"]) * 1000 / SR)
    active_ms = STATE["active_ms"]
    x = np.concatenate(STATE["buf"]) if STATE["buf"] else np.zeros(0)
    STATE["buf"] = []
    if active_ms < P["min_active_sec"] * 1000:
        return {"ok": False,
                "error": "有效语音 " + ("%.1f" % (active_ms / 1000.0)) + " 秒，不足 " +
                         str(P["min_active_sec"]) + " 秒，请靠近麦克风重录"}
    v = embed(x)
    if v is None:
        return {"ok": False, "error": "音频太短或太轻，无法建立声纹模板"}
    STATE["template"] = v
    STATE["enrolled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return {"ok": True, "active_sec": round(active_ms / 1000.0, 1),
            "total_sec": round(total_ms / 1000.0, 1), "dim": int(v.size)}


def clear():
    STATE.update(mode="idle", buf=[], active_ms=0, template=None,
                 enrolled_at="", last={"speaker": "unknown", "score": 0.0})
    return {"ok": True}


def tag_turn(chunks):
    """一个 VAD turn 的 base64 PCM 块列表 -> (speaker, score, active_ms)。"""
    if STATE["template"] is None or not chunks:
        return "unknown", 0.0, 0
    x = np.concatenate([decode(c) for c in chunks])
    act = _active_ms(x)
    v = embed(x)
    if v is None or act < P["min_tag_sec"] * 1000:
        return "unknown", 0.0, act
    score = float(np.clip(np.dot(STATE["template"], v), -1.0, 1.0))
    if score >= P["threshold"]:
        sp = "wearer"
    elif score < P["unknown_below"]:
        sp = "other"
    else:
        sp = "unknown"
    STATE["last"] = {"speaker": sp, "score": round(score, 3)}
    return sp, round(score, 3), act


def status():
    return {"enrolled": STATE["template"] is not None,
            "enrolled_at": STATE["enrolled_at"],
            "mode": STATE["mode"], "active_ms": STATE["active_ms"],
            "enroll_sec": P["enroll_sec"], "min_active_sec": P["min_active_sec"],
            "threshold": P["threshold"], "unknown_below": P["unknown_below"],
            "last": dict(STATE["last"])}
