@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 触觉社交手环 - 一键跑演示
echo ============================================
echo  第 1 步：生成对话音频（这一步要联网）
echo ============================================
echo.
python make_dialogue.py
if errorlevel 1 (
  echo.
  echo [提示] 音频没生成（多半是没网）。不用管，第 2 步会用估算时间轴继续跑完。
  echo.
)
echo.
echo ============================================
echo  第 2 步：M2 流水线（纯本地、不调 ASR/LLM、不发指令）
echo ============================================
echo.
python pipeline.py --no-asr --no-llm --mock
if errorlevel 1 (
  echo.
  echo [失败] 流水线没跑通，把上面的红字截图发出来。
  echo.
  pause
  exit /b 1
)
echo.
echo ============================================
echo  完成！本文件夹里多了：
echo    dialogue.wav / dialogue.mp3    对话音频
echo    dialogue_timeline.csv          触发时间轴
echo    triggers.csv                   视频字幕源（七个输出）
echo ============================================
echo.
echo  想跑完整 ASR + LLM：双击 一键跑全链路.bat（云端 qwen3，不用装本地模型）
echo  想驱动真硬件：把打印的 JSON 粘进 Arduino 串口监视器
echo.
pause
