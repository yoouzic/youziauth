# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path.cwd()
VERSION_DIR = ROOT / "build" / "version"

datas = [
    (str(ROOT / "VERSION"), "."),
    (str(ROOT / "config.example.ini"), "."),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "desktop_ui"), "desktop_ui"),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    (str(ROOT / "third_party_licenses"), "third_party_licenses"),
    # 统一认证验证码识别模型（约 578 KB）。
    # 仅 GUI 应用需要：后台 agent 只做校园网 ePortal 认证，不涉及验证码，
    # 因此不把模型与 numpy 打进 youziauth-agent，避免让常驻进程变重。
    (str(ROOT / "data" / "captcha" / "model.npz"), "data/captcha"),
]

gui_analysis = Analysis(
    [str(ROOT / "campus_auth_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["playwright.sync_api", "webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
                   "winrt.runtime", "winrt.windows.foundation", "winrt.windows.devices.geolocation",
                   "winrt._winrt", "winrt._winrt_windows_foundation", "winrt._winrt_windows_devices_geolocation",
                   # 验证码识别相关的延迟导入：captcha_ocr 只在真正需要识别时才 import，
                   # 静态分析可能漏掉，显式声明以保证打进包里。
                   # idm_http 是纯 HTTP 提交登录（绕开站点 WAF 对浏览器 POST 的拦截），
                   # 由 idm_login 在函数内 import，同样需要显式声明。
                   # dorm_accounts 由 CampusAuthGui 在函数内 import（账号档案目录 + 账号清单）。
                   "captcha_ocr", "idm_login", "idm_http", "idm_credentials", "dorm_accounts"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
agent_analysis = Analysis(
    [str(ROOT / "campus_auth_agent.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

MERGE(
    (gui_analysis, "youziauth", "youziauth"),
    (agent_analysis, "youziauth-agent", "youziauth-agent"),
)

gui_pyz = PYZ(gui_analysis.pure)
agent_pyz = PYZ(agent_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="youziauth",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "yuzu_app.ico"),
    version=str(VERSION_DIR / "youziauth.version"),
)

agent_exe = EXE(
    agent_pyz,
    agent_analysis.scripts,
    [],
    exclude_binaries=True,
    name="youziauth-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "yuzu_app.ico"),
    version=str(VERSION_DIR / "youziauth-agent.version"),
)

coll = COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    agent_exe,
    agent_analysis.binaries,
    agent_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="youziauth",
)
