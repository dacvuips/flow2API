# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — Flow2API Agent (onedir). Build via build_release.bat.
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

SPECDIR = Path(SPECPATH).resolve()
APP_DIR = SPECDIR.parent
REPO_ROOT = APP_DIR.parent

block_cipher = None

datas: list = []
binaries: list = []
hiddenimports: list = []

# Heavy / binary-bound packages — collect everything for portable run
for pkg in (
    "uvicorn",
    "fastapi",
    "starlette",
    "anyio",
    "httpx",
    "httpcore",
    "curl_cffi",
    "sqlalchemy",
    "aiosqlite",
    "pydantic",
    "pydantic_core",
    "websockets",
    "multipart",
    "cv2",
    "PIL",
    "numpy",
    "imageio_ffmpeg",
    "playwright",
    "greenlet",
    "h11",
    "idna",
    "certifi",
    "sniffio",
    "click",
    "annotated_types",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # noqa: BLE001 — best-effort collect
        print(f"[spec] skip collect_all({pkg}): {exc}", file=sys.stderr)

# Package data (watermark alpha maps, etc.)
try:
    datas += collect_data_files("flow2api")
except Exception:
    pass

assets = APP_DIR / "flow2api" / "assets"
if assets.is_dir():
    datas.append((str(assets), "flow2api/assets"))

hiddenimports += [
    "flow2api",
    "flow2api.main",
    "flow2api.config",
    "flow2api.db",
    "flow2api.db.models",
    "flow2api.routes",
    "flow2api.routes.activity",
    "flow2api.routes.admin",
    "flow2api.routes.auth",
    "flow2api.routes.captcha_broker",
    "flow2api.routes.chatgpt",
    "flow2api.routes.chatgpt_broker",
    "flow2api.routes.flow_cdp",
    "flow2api.routes.requests",
    "flow2api.routes.settings",
    "flow2api.routes.system",
    "flow2api.routes.worker",
    "flow2api.routes.watermark",
    "flow2api.services",
    "flow2api.worker",
    "flow2api.worker.processor",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

a = Analysis(
    [str(APP_DIR / "run.py")],
    pathex=[str(APP_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "IPython"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Optional: strip .py source leftovers (keep .pyc only in archive)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Flow2API-Agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Flow2API-Agent",
)
