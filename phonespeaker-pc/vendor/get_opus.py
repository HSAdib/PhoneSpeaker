"""
vendor/get_opus.py — Download/extract opus.dll for 64-bit Windows.

Tries multiple sources in order:
  1. pip download pyogg wheel and extract opus.dll from the wheel ZIP
  2. Download from GitHub releases (mozillarebuilds or xiph)
  3. Try to find opus.dll already in system/PATH
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
import urllib.request
import urllib.error

SCRIPT_DIR = Path(__file__).resolve().parent
DEST = SCRIPT_DIR / "opus.dll"

PYOGG_WHEEL_URLS = [
    # PyOGG 0.9.x ships opus.dll inside the wheel under PyOGG/_pyogg/
    # Try multiple python versions
    "https://files.pythonhosted.org/packages/py3/p/pyogg/",
]

# Direct download candidates (confirmed Windows 64-bit DLL bundles)
DIRECT_URLS = [
    # From the opus-tools Windows binary releases on xiph.org CDN
    "https://downloads.xiph.org/releases/opus/opus-1.4-win64.zip",
    # GitHub mozilla release (opus-tools binary)
    "https://github.com/xiph/opus/releases/download/v1.5.2/opus-1.5.2-win.zip",
]

PYOGG_PYPI_URL = "https://pypi.org/pypi/PyOGG/json"


def log(msg: str) -> None:
    print(f"[get_opus] {msg}", flush=True)


def _try_pip_download_pyogg() -> bool:
    """
    Use pip download to fetch the PyOGG win_amd64 wheel and extract opus.dll.
    """
    log("Attempting: pip download PyOGG (win_amd64 wheel)...")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        cmd = [
            sys.executable, "-m", "pip", "download",
            "PyOGG",
            "--dest", str(tmpdir),
            "--platform", "win_amd64",
            "--python-version", "311",
            "--only-binary", ":all:",
            "--no-deps",
            "-q",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as exc:
            log(f"pip download failed: {exc}")
            return False

        wheels = list(tmpdir.glob("*.whl"))
        if not wheels:
            log("No PyOGG wheel downloaded")
            return False

        for wheel in wheels:
            log(f"Inspecting wheel: {wheel.name}")
            try:
                with zipfile.ZipFile(str(wheel), "r") as zf:
                    for name in zf.namelist():
                        lower = name.lower()
                        if lower.endswith("opus.dll"):
                            log(f"Found opus.dll at {name} in wheel")
                            data = zf.read(name)
                            DEST.write_bytes(data)
                            log(f"Extracted opus.dll → {DEST} ({len(data)} bytes)")
                            return True
            except Exception as exc:
                log(f"Error reading wheel {wheel.name}: {exc}")
                continue

    log("opus.dll not found in any PyOGG wheel")
    return False


def _try_direct_download() -> bool:
    """Try downloading opus DLL from known direct URLs."""
    for url in DIRECT_URLS:
        log(f"Trying direct download: {url}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                zip_path = tmpdir / "opus.zip"
                req = urllib.request.Request(url, headers={"User-Agent": "PhoneSpeaker/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        zip_path.write_bytes(resp.read())
                except urllib.error.HTTPError as exc:
                    log(f"HTTP {exc.code} from {url}")
                    continue
                except urllib.error.URLError as exc:
                    log(f"URL error: {exc}")
                    continue

                if not zipfile.is_zipfile(str(zip_path)):
                    log("Downloaded file is not a zip archive")
                    continue

                with zipfile.ZipFile(str(zip_path), "r") as zf:
                    for name in zf.namelist():
                        if name.lower().endswith("opus.dll"):
                            data = zf.read(name)
                            DEST.write_bytes(data)
                            log(f"Extracted from {url}: opus.dll ({len(data)} bytes) → {DEST}")
                            return True
                log("opus.dll not found inside the archive")
        except Exception as exc:
            log(f"Error with {url}: {exc}")
            continue
    return False


def _try_requests_download() -> bool:
    """
    Try using requests library to fetch PyPI JSON and find a win_amd64 wheel URL,
    then extract opus.dll.
    """
    try:
        import requests
    except ImportError:
        return False

    log("Fetching PyOGG metadata from PyPI...")
    try:
        resp = requests.get(PYOGG_PYPI_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log(f"PyPI fetch failed: {exc}")
        return False

    # Find latest win_amd64 wheel URL
    releases = data.get("releases", {})
    all_versions = sorted(releases.keys(), reverse=True)
    for ver in all_versions:
        files = releases[ver]
        for f in files:
            fname = f.get("filename", "")
            url = f.get("url", "")
            if "win_amd64" in fname and fname.endswith(".whl"):
                log(f"Found win_amd64 wheel: {fname}")
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        tmpdir = Path(tmp)
                        wheel_path = tmpdir / fname
                        r2 = requests.get(url, timeout=60)
                        r2.raise_for_status()
                        wheel_path.write_bytes(r2.content)
                        with zipfile.ZipFile(str(wheel_path), "r") as zf:
                            for name in zf.namelist():
                                if name.lower().endswith("opus.dll"):
                                    dll_data = zf.read(name)
                                    DEST.write_bytes(dll_data)
                                    log(f"Extracted opus.dll ({len(dll_data)} bytes) → {DEST}")
                                    return True
                        log("opus.dll not found in that wheel")
                except Exception as exc:
                    log(f"Failed to process wheel {fname}: {exc}")
                    continue
    return False


def _try_system_opus() -> bool:
    """Try to locate opus.dll on the system (PATH, System32, etc.)."""
    log("Searching for opus.dll on system PATH / System32...")
    search_dirs = [
        Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32",
        Path(os.environ.get("SystemRoot", "C:\\Windows")) / "SysWOW64",
    ]
    # Also check PATH
    for d in os.environ.get("PATH", "").split(os.pathsep):
        search_dirs.append(Path(d))

    for d in search_dirs:
        candidate = d / "opus.dll"
        if candidate.exists():
            try:
                shutil.copy2(str(candidate), str(DEST))
                log(f"Copied system opus.dll from {candidate} → {DEST}")
                return True
            except Exception as exc:
                log(f"Could not copy from {candidate}: {exc}")

    # Try ctypes find
    import ctypes.util
    found = ctypes.util.find_library("opus")
    if found:
        fp = Path(found)
        if fp.exists():
            try:
                shutil.copy2(str(fp), str(DEST))
                log(f"Copied opus library from {fp} → {DEST}")
                return True
            except Exception as exc:
                log(f"Could not copy from {fp}: {exc}")

    return False


def _try_opuslib_install() -> bool:
    """
    Install opuslib via pip and find the bundled DLL.
    """
    log("Trying: pip install opuslib ...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "opuslib", "-q"],
            capture_output=True, timeout=60
        )
    except Exception as exc:
        log(f"pip install opuslib failed: {exc}")
        return False

    # Find it
    try:
        import importlib.util
        spec = importlib.util.find_spec("opuslib")
        if spec and spec.origin:
            pkg_dir = Path(spec.origin).parent
            for p in pkg_dir.rglob("opus.dll"):
                shutil.copy2(str(p), str(DEST))
                log(f"Copied opuslib opus.dll from {p} → {DEST}")
                return True
    except Exception as exc:
        log(f"Error finding opuslib DLL: {exc}")

    return False


def main() -> None:
    if DEST.exists():
        log(f"opus.dll already present at {DEST} ({DEST.stat().st_size} bytes) — skipping download")
        return

    log(f"Acquiring opus.dll → {DEST}")

    strategies = [
        ("PyOGG pip wheel", _try_pip_download_pyogg),
        ("requests+PyPI", _try_requests_download),
        ("direct URL download", _try_direct_download),
        ("system PATH search", _try_system_opus),
        ("opuslib install", _try_opuslib_install),
    ]

    for name, fn in strategies:
        log(f"--- Strategy: {name} ---")
        try:
            if fn():
                log(f"SUCCESS via '{name}'. opus.dll is ready.")
                # Validate it loads
                try:
                    import ctypes
                    lib = ctypes.CDLL(str(DEST))
                    ver = lib.opus_get_version_string()
                    log(f"Opus DLL loaded OK. Version: {ver}")
                except Exception as exc:
                    log(f"WARNING: DLL loaded but version check failed: {exc}")
                return
        except Exception as exc:
            log(f"Strategy '{name}' raised exception: {exc}")

    log("FAILED: Could not acquire opus.dll from any source.")
    log("The app will run in PCM-only mode.")
    log("To manually install: download opus.dll for Windows 64-bit and place it at:")
    log(f"  {DEST}")
    sys.exit(1)


if __name__ == "__main__":
    main()
