@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 触觉社交手环 - 二期实时依赖
echo ============================================
echo  安装「边说边出」需要的依赖
echo ============================================
echo.
echo  websocket-client ：后端代理 Qwen3-ASR-Flash-Realtime / 命令行讯飞 IAT（一般已随环境装好）
echo  sounddevice      ：读电脑麦克风（16kHz / 16bit / 单声道）
echo.
python --version
if errorlevel 1 (
  echo [错误] 没找到 Python，先装 Python 3.10 以上并勾选 Add python.exe to PATH。
  pause
  exit /b 1
)
echo.
python -m pip install websocket-client sounddevice
if errorlevel 1 (
  echo.
  echo [失败] 安装没成功，换手机热点后重新双击本文件。
  echo        没有麦克风也能跑：python realtime_asr.py --file dialogue.wav --mock
  pause
  exit /b 1
)
echo.
echo ============================================
echo  装好了。接下来双击：一键跑实时.bat
echo  说明：用的是电脑自带麦克风，不用另外买硬件。
echo ============================================
echo.
pause
