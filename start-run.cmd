@echo off
rem 파일 통돌이 — 소스에서 실행 (Windows). 저장소 최상위에서: start-run  (또는 이 파일을 더블클릭)
rem uv가 없으면 설치를 물어본 뒤 이어서 실행한다. Python과 패키지는 uv가 받는다.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call scripts\find_uv.cmd || (pause & exit /b 1)
"%UV%" run --locked tongdory %*
if errorlevel 1 pause
