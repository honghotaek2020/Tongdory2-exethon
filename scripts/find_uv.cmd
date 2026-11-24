@echo off
rem start-run.cmd·start-build.cmd가 함께 쓰는 uv 찾기 (Windows). 없으면 물어본 뒤 공식 스크립트로 설치한다(관리자 권한 불필요).
rem 사용: call scripts\find_uv.cmd  → 변수 UV에 uv 경로가 들어간다. 실패하면 errorlevel 1.
set "UV="
for /f "delims=" %%p in ('where uv 2^>nul') do if not defined UV set "UV=%%p"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if defined UV exit /b 0
echo 실행에 필요한 uv가 없습니다. 공식 설치 스크립트로 설치합니다 (https://astral.sh/uv, 관리자 권한 불필요).
choice /c YN /m "설치할까요"
if errorlevel 2 (echo 설치하지 않았습니다. 직접 설치: https://docs.astral.sh/uv/getting-started/installation/ & exit /b 1)
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if exist "%USERPROFILE%\.local\bin\uv.exe" (set "UV=%USERPROFILE%\.local\bin\uv.exe") else (for /f "delims=" %%p in ('where uv 2^>nul') do if not defined UV set "UV=%%p")
if not defined UV (echo uv 설치에 실패했습니다. 직접 설치: https://docs.astral.sh/uv/getting-started/installation/ & exit /b 1)
echo uv를 설치했습니다: %UV%
exit /b 0
