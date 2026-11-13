# -*- mode: python ; coding: utf-8 -*-
"""乐跑凭证提取器 —— 单文件控制台 exe, 发给成员在自己电脑上双击运行。

只做一件事: 抓本机乐跑小程序的凭证, 用 PIN 加密存到桌面。不带 youziauth 的
界面/存储/更新器; lptiyu 系模块沿用主程序的 hiddenimports 纪律(延迟导入必须
显式声明), 外加本提取器自己的 lptiyu_credential。

构建(仓库根目录):
    py -3 -m PyInstaller packaging\\lptiyu_extractor.spec --noconfirm ^
        --distpath .scratch\\lptiyu-extractor-dist --workpath .scratch\\lptiyu-extractor-build
产物: .scratch\\lptiyu-extractor-dist\\lptiyu-credential-export.exe
"""
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "tools" / "lptiyu_credential_export.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # 乐跑协议核心: 部分模块在函数内延迟 import, 静态分析看不见, 显式声明。
        "lptiyu", "lptiyu.spec", "lptiyu.cipher", "lptiyu.tls", "lptiyu.wire",
        "lptiyu.client", "lptiyu.tracks", "lptiyu.zones", "lptiyu.upfile", "lptiyu.flow",
        "lptiyu_store", "lptiyu_panel", "lptiyu_renew", "lptiyu_capture",
        # 提取器自身的凭证密封模块
        "lptiyu_credential",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "playwright", "webview",
               "winrt", "requests", "PIL", "captcha_ocr"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lptiyu-credential-export",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
)
