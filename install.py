#!/usr/bin/env python3
"""cc-explorer 설치 — 전역 명령어 'cc-explorer' 를 PATH 에 등록 (크로스플랫폼).

  python install.py              # 설치  (Windows: python, macOS/Linux: python3)
  python install.py --uninstall  # 제거

의존성 없음. 파일은 레포에 그대로 두고:
  - macOS/Linux: PATH 상의 bin 디렉토리에 심볼릭 링크 생성
  - Windows:     PATH 상의 디렉토리에 'cc-explorer.bat' 래퍼 생성
레포를 옮기면 다시 실행해야 한다.
"""
import os
import sys
from pathlib import Path

CMD = "cc-explorer"
HERE = Path(__file__).resolve().parent   # cc-explorer/ — install.py 와 server.py 가 같은 폴더
SRC = HERE / "server.py"
IS_WIN = os.name == "nt"


def path_dirs():
    return [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def candidate_bins():
    """설치 후보 디렉토리 선택 — 임의 PATH 항목이 아니라 '선호 목록' 안에서만 고른다.

    (PATH 의 첫 쓰기가능 디렉토리를 그냥 쓰면 앱 번들 bin 같은 엉뚱한 곳에 들어갈 수 있음)
    """
    home = Path.home()
    if IS_WIN:
        prefer = [home / "bin", home / ".local" / "bin"]
    else:
        prefer = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
                  home / ".local" / "bin", home / "bin"]
    on_path = set(path_dirs())
    # 1) 선호 목록 중 PATH 에 있고 쓰기 가능
    for d in prefer:
        if d in on_path and d.is_dir() and os.access(d, os.W_OK):
            return d, True
    # 2) 선호 목록 중 (존재 + 쓰기 가능)
    for d in prefer:
        if d.is_dir() and os.access(d, os.W_OK):
            return d, d in on_path
    # 3) 첫 선호 디렉토리 생성해서 사용
    return prefer[0], prefer[0] in on_path


def installed_targets():
    """설치되어 있을 만한 위치들 (제거용)."""
    names = [CMD + ".bat", CMD] if IS_WIN else [CMD]
    dirs = path_dirs() + [Path.home() / "bin", Path.home() / ".local" / "bin",
                          Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    seen, out = set(), []
    for d in dirs:
        if d in seen:
            continue
        seen.add(d)
        for n in names:
            p = d / n
            if p.exists() or p.is_symlink():
                out.append(p)
    return out


def uninstall():
    targets = installed_targets()
    if not targets:
        print("설치된 항목을 찾지 못했습니다.")
        return
    for p in targets:
        try:
            p.unlink()
            print("제거됨:", p)
        except OSError as e:
            print("제거 실패:", p, "-", e)


def install():
    if not SRC.is_file():
        print(f"[!] 서버 파일을 찾을 수 없음: {SRC}", file=sys.stderr)
        sys.exit(1)
    bindir, on_path = candidate_bins()
    bindir.mkdir(parents=True, exist_ok=True)

    if IS_WIN:
        # python.exe 경로로 server.py 를 부르는 .bat 래퍼
        target = bindir / (CMD + ".bat")
        py = sys.executable or "python"
        target.write_text(
            "@echo off\r\n"
            f'"{py}" "{SRC}" %*\r\n',
            encoding="utf-8")
        print(f"설치됨: {target}")
        print(f"   -> {py} {SRC}")
    else:
        target = bindir / CMD
        try:
            os.chmod(SRC, 0o755)
        except OSError:
            pass
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink(SRC, target)
        print(f"설치됨: {target} -> {SRC}")

    if on_path:
        print(f"이제 어디서든 '{CMD}' 로 실행할 수 있습니다.")
    else:
        print()
        print(f"[주의] '{bindir}' 가 PATH 에 없습니다. PATH 에 추가하세요:")
        if IS_WIN:
            print(f'  setx PATH "%PATH%;{bindir}"')
            print("  (새 PowerShell/cmd 창을 열어야 적용됩니다)")
        else:
            print(f'  echo \'export PATH="{bindir}:$PATH"\' >> ~/.zshrc && source ~/.zshrc')
        print(f"그 후 '{CMD}' 로 실행할 수 있습니다.")


def main():
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()


if __name__ == "__main__":
    main()
