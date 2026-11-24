# start-run·start-build가 함께 쓰는 uv 찾기 (macOS·Linux). 없으면 물어본 뒤 공식 스크립트로 설치한다(관리자 권한 불필요).
# 사용: . scripts/find_uv.sh  → 변수 UV에 uv 경로가 들어간다.
UV=$(command -v uv 2>/dev/null || true)
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  echo "실행에 필요한 uv가 없습니다. 공식 설치 스크립트로 설치합니다 (https://astral.sh/uv, 관리자 권한 불필요)."
  printf "설치할까요? [Y/n] "
  read -r answer || answer=""
  case "$answer" in [nN]*) echo "설치하지 않았습니다. 직접 설치: https://docs.astral.sh/uv/getting-started/installation/"; exit 1;; esac
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV=$(command -v uv 2>/dev/null || true)
  [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
  [ -n "$UV" ] || { echo "uv 설치에 실패했습니다. 직접 설치: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
  echo "uv를 설치했습니다: $UV"
fi
