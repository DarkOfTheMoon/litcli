# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the binary installers.

Directory build (``--onedir``): the installer extracts it once, and start-up stays fast.

``playwright`` is included whole — it carries a node driver, so the cnki/douban backends work
without Python on the machine. The browsers themselves are *not* bundled (they are ~150 MB per
engine); ``litcli backends install-browser`` downloads the one the user wants, and the
installers offer that step. ``jieba`` is the CNKI keyword splitter.
"""
import os
import sys

from PyInstaller.utils.hooks import collect_all

# PyInstaller resolves Analysis paths against this spec's own directory.
ROOT = os.path.dirname(SPECPATH)
sys.path.insert(0, ROOT)

datas, binaries, hiddenimports = [], [], []
for package in ("playwright", "jieba"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="litcli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="litcli",
)
