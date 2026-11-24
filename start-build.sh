#!/bin/sh
# 파일 통돌이 — 배포본 빌드 (macOS). 저장소 최상위에서: ./start-build.sh
# 결과: src/dist/Tongdory-<버전>-macos-<아키텍처>.zip (압축을 풀어 Tongdory.app 실행)
# uv·Python·C 컴파일러가 없으면 설치를 물어본 뒤 이어서 빌드한다.
set -eu
cd "$(dirname "$0")"
. scripts/find_uv.sh

ask() { printf "%s [Y/n] " "$1"; read -r answer || answer=""; case "$answer" in [nN]*) return 1;; esac; }

# C 컴파일러 (Xcode Command Line Tools)
if ! xcode-select -p >/dev/null 2>&1; then
  echo "빌드에 필요한 Xcode Command Line Tools가 없습니다. 설치 창을 띄웁니다."
  xcode-select --install || true
  echo "설치 창에서 [설치]를 누르고, 설치가 끝나면 ./start-build.sh 를 다시 실행하세요."
  exit 1
fi

# Python 3.12 이상 (Nuitka는 uv가 내려받은 Python으로는 배포본을 만들 수 없어, 설치된 Python을 쓴다)
find_python() {
  for v in 3.14 3.13 3.12; do
    if "$UV" python find --no-managed-python "$v" >/dev/null 2>&1; then echo "$v"; return 0; fi
  done
  return 1
}
py=$(find_python || true)
if [ -z "$py" ]; then
  if command -v brew >/dev/null 2>&1 && ask "빌드에 필요한 Python 3.12가 없습니다. Homebrew로 설치할까요?"; then
    brew install python@3.12
    py=$(find_python || true)
  fi
  [ -n "$py" ] || { echo "Python 3.12 이상을 설치한 뒤 다시 실행하세요: https://www.python.org/downloads/macos/"; exit 1; }
fi

echo "[빌드] Python $py 사용. 수 분 걸립니다."
exec "$UV" run --isolated --locked --no-dev --with nuitka==4.3.5 --with zstandard --no-managed-python --python "$py" \
  python scripts/build_standalone.py "$@"
