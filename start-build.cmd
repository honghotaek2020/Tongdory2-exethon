@echo off
rem 파일 통돌이 — 배포본 빌드 (Windows). 저장소 최상위에서: start-build  (또는 이 파일을 더블클릭)
rem 결과: src\dist\Tongdory-<버전>-windows-<아키텍처>.zip (압축을 풀어 Tongdory.exe 실행)
rem uv·Python이 없으면 설치를 물어본 뒤 이어서 빌드한다. C 컴파일러는 빌드 중 자동으로 받는다.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call scripts\find_uv.cmd || (pause & exit /b 1)
rem Python 3.12 이상 (Nuitka는 uv가 내려받은 Python으로는 배포본을 만들 수 없어, 설치된 Python을 쓴다)
call :find_python
if defined PY goto build
echo 빌드에 필요한 Python 3.12가 없습니다. winget으로 설치합니다.
choice /c YN /m "설치할까요"
if errorlevel 2 (echo Python 3.12 이상을 설치한 뒤 다시 실행하세요: https://www.python.org/downloads/windows/ & pause & exit /b 1)
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
call :find_python
if not defined PY (echo Python 설치를 확인하지 못했습니다. 창을 닫고 다시 실행하세요. & pause & exit /b 1)
:build
echo [빌드] Python %PY% 사용. 오래 걸릴 수 있습니다(첫 빌드는 1시간 이상).
"%UV%" run --isolated --locked --no-dev --with nuitka==4.3.5 --with zstandard --no-managed-python --python %PY% python scripts/build_standalone.py %*
if errorlevel 1 (echo [빌드 실패] 위 오류를 확인하세요.) else (echo [빌드 완료] src\dist 폴더의 ZIP을 확인하세요.)
pause
exit /b

:find_python
set "PY="
for %%v in (3.14 3.13 3.12) do if not defined PY "%UV%" python find --no-managed-python %%v >nul 2>nul && set "PY=%%v"
exit /b 0
