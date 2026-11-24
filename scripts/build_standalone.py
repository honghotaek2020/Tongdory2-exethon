"""Nuitka로 macOS .app / Windows 단일 exe를 빌드하고, 점검을 통과한 것만 ZIP으로 묶는다."""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src" / "dist"
PLATFORMS = {"darwin": "macos", "win32": "windows"}
ARCHES = {"aarch64": "arm64", "arm64": "arm64", "amd64": "x64", "x86_64": "x64"}
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
FORBIDDEN_SUFFIXES = {".py", ".pyw", ".pyc", ".pyi", ".db", ".db-shm", ".db-wal"}
GUIDE_NAME = "실행안내.txt"
_COMMON_GUIDE = """\
[Liner API 키]
화면 왼쪽 메뉴 [추가 기능] → [Liner API 키]에서 붙여 넣고 [저장]. 바로 적용됩니다.
(키 없이도 파일 검사와 직접 정리는 됩니다.)

[종료]
화면 왼쪽 아래 [앱 종료]

브라우저가 열리지 않으면 http://127.0.0.1:8765/ 로 접속하세요.
"""
GUIDES = {
    "macos": f"""\
파일 통돌이 (macOS)

[실행]
Tongdory.app을 더블클릭합니다. 브라우저가 열립니다.

처음 한 번, 보안 경고로 열리지 않으면 터미널에서 이 폴더로 이동해 실행하세요.
    xattr -dr com.apple.quarantine Tongdory.app

{_COMMON_GUIDE}""",
    "windows": f"""\
파일 통돌이 (Windows)

[실행]
Tongdory.exe를 더블클릭합니다. 브라우저가 열립니다.

처음 한 번, "Windows의 PC 보호" 창이 뜨면 [추가 정보] → [실행]을 누르세요.

{_COMMON_GUIDE}""",
}


def _has_msvc() -> bool:
    """Visual Studio Build Tools의 C++ 컴파일러가 설치되어 있는가 (Windows)."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return False
    result = subprocess.run([str(vswhere), "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                             "-property", "installationPath"], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip())


def _check_clean(root: Path) -> None:
    for path in list(root.rglob("*")):
        if path.name in JUNK_NAMES or path.name.startswith("._"):
            path.unlink()  # 소스 트리에서 함께 복사된 Finder·탐색기 메타데이터
        elif path.name == ".env" or path.suffix in FORBIDDEN_SUFFIXES:
            raise RuntimeError(f"[배포] 제외 대상 파일이 포함됐습니다. {path.relative_to(root)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="파일 통돌이 테스트 배포본 빌드")
    parser.add_argument("--os", choices=("auto", *PLATFORMS.values()), default="auto",
                        help="현재 빌드 머신의 대상 운영체제 확인 (기본값: 자동 감지)")
    parser.add_argument("--compiler", choices=("auto", "msvc", "zig"), default="auto",
                        help="Windows C 컴파일러: auto(기본값, Visual Studio C++가 있으면 msvc, 없으면 zig), msvc, zig")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 6), help="C 컴파일러 병렬 작업 수")
    args = parser.parse_args()
    os_name = PLATFORMS.get(sys.platform)
    arch = ARCHES.get(platform.machine().lower())
    if not os_name or not arch:
        parser.error("64비트 macOS 또는 Windows에서 대상 OS의 Python으로 빌드하세요.")
    if args.os not in ("auto", os_name):
        parser.error(f"현재 운영체제는 {os_name}입니다. --os {args.os}는 해당 운영체제에서 빌드하세요.")
    if args.jobs < 1:
        parser.error("작업 수는 1 이상이어야 합니다.")

    with (ROOT / "pyproject.toml").open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    name = f"Tongdory-{version}-{os_name}-{arch}"
    executable = "Tongdory.exe" if os_name == "windows" else "Tongdory"
    OUTPUT.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix=".tongdory-build-", dir=OUTPUT) as tmp:
        work = Path(tmp)
        # app 모드: macOS는 .app 번들, Windows는 실행 파일 하나(onefile). 둘 다 터미널·콘솔 창 없이 실행한다.
        command = [sys.executable, "-m", "nuitka", "--mode=app", "--include-package-data=tongdory2",
                   "--include-package=uvicorn", "--update-check=never", f"--jobs={args.jobs}",
                   f"--output-dir={work}", f"--output-filename={executable}"]
        if os_name == "windows":
            compiler = args.compiler if args.compiler != "auto" else ("msvc" if _has_msvc() else "zig")
            print(f"[빌드] C 컴파일러: {compiler}" + (" (Visual Studio C++ 없음, Nuitka가 Zig를 자동으로 받음)" if compiler == "zig" else ""))
            command += ["--zig" if compiler == "zig" else "--msvc=latest", "--assume-yes-for-downloads",
                        "--windows-console-mode=disable"]
        else:
            command += ["--disable-cache=ccache", "--macos-app-mode=background", "--macos-app-name=Tongdory"]
        subprocess.run([*command, str(ROOT / "src" / "tongdory2" / "__main__.py")], cwd=ROOT, check=True)

        package = work / "Tongdory"  # ZIP 최상위 폴더: 앱 하나 + 실행안내.txt
        package.mkdir()
        if os_name == "macos":
            (bundle,) = work.glob("*.app")
            app = bundle.rename(package / "Tongdory.app")
            _check_clean(app)
            with (app / "Contents" / "Info.plist").open("rb") as file:
                binary = app / "Contents" / "MacOS" / plistlib.load(file)["CFBundleExecutable"]
        else:
            for dist in work.glob("*.dist"):  # onefile에 묶이기 전 폴더로 포함 파일을 검사한다
                _check_clean(dist)
            binary = (work / executable).rename(package / executable)
        (package / GUIDE_NAME).write_text(GUIDES[os_name], encoding="utf-8-sig" if os_name == "windows" else "utf-8")

        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "LINER_API_KEY", "TONGDORY_DATA_DIR"}}
        env["LOCALAPPDATA"] = str(work / "appdata")  # Windows 점검 로그는 임시 폴더에 둔다
        subprocess.run([str(binary), "--os", os_name, "--check-installation"], cwd=work, env=env, check=True)

        archive = work / f"{name}.zip"
        if os_name == "macos":
            # ditto는 .app의 실행 권한·서명·확장 속성을 그대로 보존한다.
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(package), str(archive)], check=True)
        else:
            shutil.make_archive(str(archive.with_suffix("")), "zip", work, package.name)
        target = archive.replace(OUTPUT / archive.name)
    print(f"[배포] {target}")


if __name__ == "__main__":
    main()
