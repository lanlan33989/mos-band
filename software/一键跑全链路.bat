@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 触觉社交手环 - 完整链路
echo ============================================
echo  完整链路：云端 qwen3-asr-flash ASR + DeepSeek LLM + 七个输出
echo  （只打印不发指令，不需要硬件；本地 ASR 已移除，不用装语音模型）
echo ============================================
echo.
echo  前提：本目录 config.json 里两把 key 已填好，且 dialogue.wav 已生成。
echo  想换现场录音：python pipeline.py --audio 现场录音.wav --llm --mock
echo  任何一环失败都会自动降级并继续跑完，不会中断。
echo.
python pipeline.py --asr cloud --llm --mock
echo.
echo ============================================
echo  跑完。查看 triggers.csv 里的字幕、ASR情感 与 LLM 复核结果。
echo  说明：qwen3 不返回时间戳，时间由 dialogue_timeline.csv 对齐。
echo ============================================
echo.
pause
