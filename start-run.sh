#!/bin/sh
# 파일 통돌이 — 소스에서 실행 (macOS·Linux). 저장소 최상위에서: ./start-run.sh
# uv가 없으면 설치를 물어본 뒤 이어서 실행한다. Python과 패키지는 uv가 받는다.
set -eu
cd "$(dirname "$0")"
. scripts/find_uv.sh
exec "$UV" run --locked tongdory "$@"
