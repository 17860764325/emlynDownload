@echo off
chcp 65001 >nul 2>&1
title EmlynDownload

rem ====================================================================
rem  EmlynDownload Launcher
rem  Just double-click this file to open the downloader.
rem
rem  NOTE: This file is intentionally ASCII-only.
rem        Chinese text inside .bat/.cmd is mis-decoded by cmd.exe
rem        (it reads .bat as the system ANSI codepage), which breaks
rem        the script. All messages here stay in English on purpose.
rem
rem  If Python cannot be found, edit the PY line below and point it
rem  to your own python.exe.
rem ====================================================================

set "PY=C:\Users\KK\.conda\envs\densefuse-gpu\python.exe"
set "APP=%~dp0EmlynDownload.py"

if not exist "%PY%" (
    echo.
    echo  [ERROR] Python interpreter not found:
    echo          %PY%
    echo.
    echo  Open this file in Notepad and change the PY line
    echo  to point at a valid python.exe.
    echo.
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo.
    echo  [ERROR] Main program not found:
    echo          %APP%
    echo.
    pause
    exit /b 1
)

rem ---- prefer pythonw.exe so no console window is shown ----
set "PYW=%PY:python.exe=pythonw.exe%"
if exist "%PYW%" (
    start "" "%PYW%" "%APP%"
) else (
    "%PY%" "%APP%"
    if errorlevel 1 pause
)
