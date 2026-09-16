@echo off
setlocal enabledelayedexpansion

:: Set desired recording duration in seconds (80 min ≈ 4800s)
set "duration_sec=4800"
set /a index=0

:loop
echo.
echo ======================================================
echo   Starting new HLS recording... Index: !index!
echo ======================================================

ffmpeg -listen 1 -i rtmp://0.0.0.0:1935/live/stream ^
-t !duration_sec! ^
-c:v libx265 -preset veryfast -b:v 5000k -maxrate 6000k -bufsize 10000k -tag:v hvc1 ^
-c:a aac -b:a 160k ^
-f hls -hls_time 6 ^
-hls_segment_filename "hls_output/segment_!index!_%%05d.ts" ^
hls_output/stream!index!.m3u8

echo.
echo Recording segment !index! complete. Waiting 3 seconds...
timeout /t 3 >nul
set /a index+=1
goto loop