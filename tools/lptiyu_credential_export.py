"""乐跑凭证提取器 —— 在成员**自己的电脑**上运行, 生成可发给管理者的加密凭证文件。

命令行或打包后的 EXE 接收 --capture-proxy-port PORT。请事先在支持按域名
分流的客户端中只将乐跑 HTTPS 发往 127.0.0.1:PORT。采集结束即关闭监听。

纪律: 凭证文件 = 你的乐跑登录态, 只发给约定好 PIN 的那个人; PIN 用口头/
电话告知, 不要跟文件走同一条聊天。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import proxy_guard  # noqa: E402
from lptiyu_capture import CaptureError, CaptureSession  # noqa: E402
from lptiyu_credential import (desktop_dir, export_file_name,  # noqa: E402
                               seal_credential)
from lptiyu_panel import open_mini_program, wechat_installed  # noqa: E402

WORK_ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) \
    / 'lptiyu-extractor'
TIMEOUT_SECONDS = 540


def ask_pin() -> str:
    if not sys.stdin.isatty():
        print('需要键盘输入 PIN: 请双击运行本工具(或在一个真的命令行窗口里运行)。')
        raise SystemExit(2)
    print('输入和管理员约定好的 PIN(建议 6 位以上, 口头/电话告知, 别走聊天):')
    for _ in range(3):
        try:
            first = getpass.getpass('PIN: ').strip()
        except (EOFError, OSError):
            print('读不到键盘输入, 请双击运行本工具。')
            raise SystemExit(2) from None
        if len(first) < 4:
            print('PIN 至少 4 位, 再试一次。')
            continue
        second = getpass.getpass('再输一次确认: ').strip()
        if first != second:
            print('两次不一致, 再试一次。')
            continue
        return first
    raise SystemExit('PIN 没输入成功, 退出。')


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--proxy-guard']:
        # 采集代理看门狗: 冻结包用自身 exe 再起一个独立进程。必须最先分派,
        # 否则看门狗会掉进下面的交互流程去问 PIN。
        return proxy_guard.main(argv[1:])
    parser = argparse.ArgumentParser(description='通过本机的乐跑专用代理导出凭证')
    parser.add_argument('--capture-proxy-port', type=int)
    args = parser.parse_args(argv)
    port = args.capture_proxy_port
    if port is None or not 1024 <= port <= 65535:
        print('请先配置支持按域名分流的客户端，并使用 --capture-proxy-port 指定端口（1024–65535）。')
        return 2
    print('=' * 56, flush=True)
    print('乐跑凭证提取器(在自己的电脑上运行)', flush=True)
    print('=' * 56, flush=True)
    print(f'只接收专用客户端送到 127.0.0.1:{port} 的乐跑 HTTPS 请求。凭证用 PIN 加密存到桌面。')
    print()

    if not wechat_installed():
        print('没有找到微信: 先在本机安装微信并登录, 再运行本工具。')
        return 2

    try:
        pin = ask_pin()
    except SystemExit as exc:
        print(exc)
        return 2

    capture_root = WORK_ROOT / 'capture'
    session = CaptureSession(capture_root, timeout_s=TIMEOUT_SECONDS)
    print()
    print('[1/3] 检查采集证书…')
    cert = session.cert_status()
    if not cert.get('installed'):
        print('还没安装采集证书(只需要一次)。Windows 会弹「根证书存储」确认, 请点「是」。')
        try:
            session.ca.install()
        except CaptureError as exc:
            print(f'证书安装失败: {exc}')
            return 2
    print('      采集证书就绪。')

    print('[2/3] 开始采集: 稍后会自动打开微信小程序, 请在里面进入一次跑步首页。')
    try:
        session.start(port=port)
    except CaptureError as exc:
        print(f'启动失败: {exc}')
        return 2
    try:
        launched = open_mini_program()
        if not launched:
            print('      没能自动唤起小程序, 请自己打开微信搜索「数体智慧体育」进跑步首页。')

        print('[3/3] 等待小程序请求(最多 9 分钟, 抓到就自动收尾)…')
        last_hint = time.time()
        while True:
            cred = session.poll()
            if cred:
                break
            status = session.status()
            if status.get('state') != 'waiting':
                print(f'采集结束: {status.get("message") or "已停止"}')
                return 1
            if time.time() - last_hint > 30:
                last_hint = time.time()
                print(f'      还在等…(已连接 {getattr(session, "connects", 0)} 次) '
                      '请核对专用客户端的乐跑分流配置及小程序是否已打开。')
            time.sleep(1.0)

        session.stop('captured', '已抓到凭证')
    finally:
        session.stop('idle', '')

    file_dict = seal_credential(cred, pin)
    target = desktop_dir() / export_file_name()
    target.write_text(json.dumps(file_dict, ensure_ascii=False), encoding='utf-8')
    print()
    print('完成! 凭证文件已存到桌面:')
    print(f'    {target}')
    print('把它发给管理员, 并口头告知 PIN。')
    if session.status()['state'] == 'error':
        print(session.status()['message'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
