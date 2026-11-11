@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 触觉社交手环 - 基础环境
echo ============================================
echo  触觉社交手环 . 基础依赖一键安装
echo ============================================
echo.
python --version
if errorlevel 1 (
  echo.
  echo [错误] 没找到 Python。请先到 python.org 安装 Python 3.10 以上，
  echo        安装第一个界面务必勾选 "Add python.exe to PATH"，
  echo        装完后重新双击本文件。
  echo.
  pause
  exit /b 1
)
echo.
echo [1/2] 升级 pip ...
python -m pip install --upgrade pip
echo.
echo [2/2] 安装 edge-tts / pydub / imageio-ffmpeg / pyserial / paho-mqtt ...
python -m pip install edge-tts pydub imageio-ffmpeg pyserial paho-mqtt
if errorlevel 1 (
  echo.
  echo [错误] 安装失败，多半是网络问题。换手机热点后重新双击本文件。
  echo.
  pause
  exit /b 1
)
echo.
echo ============================================
echo  基础环境装好了。
echo  接下来双击：一键跑演示.bat
echo  （想跑真实 ASR + LLM 就双击：一键跑全链路.bat，云端 qwen3，不用另装本地模型）
echo ============================================
echo.
pause
