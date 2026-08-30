# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ["src/kk_agent/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=["kk_agent"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="kk-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
