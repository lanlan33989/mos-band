@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 触觉社交手环 - 边说边出（实时）
echo ============================================
echo  二期「边说边出」：电脑麦克风 - 讯飞语音听写流式版
echo              - 七个输出实时判定（只打印，不发指令）
echo ============================================
echo.
echo  对着电脑麦克风说话，屏幕上会边说边出字；
echo  触发七个输出时会打印 [触发] 与 {"act":"..."}。
echo  默认 55 秒自动收尾（讯飞单会话上限 60 秒），也可直接按 Ctrl+C 提前结束。
echo.
echo  没麦克风 / 想先验证协议：python realtime_asr.py --file dialogue.wav --mock
echo  真下发手环：  python realtime_asr.py --serial --port COM3
echo.
python realtime_asr.py --mock
echo.
echo ============================================
echo  跑完。字幕与触发记录在 realtime_triggers.csv
echo ============================================
echo.
pause
