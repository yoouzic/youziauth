"""把提取器生成的凭证文件导入本机乐跑成员库。

用法:
    py -3 tools/lptiyu_member_import.py <文件路径> <PIN>
    py -3 tools/lptiyu_member_import.py auto <PIN>     # 自动找桌面/下载里最新的一份

导入后:
  * 凭证按 uid/学号自动归档(新成员建档或覆盖旧号)
  * 界面里的成员栏几秒内自动出现/更新(不用重启程序)
  * 若是新成员且未自动切换, 在界面的成员下拉里切到 TA 即可
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lptiyu_credential import (CredentialFileError,  # noqa: E402
                               find_latest_export, open_credential_file)
from lptiyu_store import Store  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    if len(args) != 2:
        print(__doc__)
        return 2
    source, pin = args
    if source == 'auto':
        path = find_latest_export()
        if path is None:
            print('没有找到凭证文件: 让对方把 lptiyu-credential-*.json 发给你, '
                  '存到桌面或下载文件夹后再试。')
            return 2
    else:
        path = Path(source)
        if not path.is_file():
            print(f'文件不存在: {path}')
            return 2

    try:
        cred = open_credential_file(path, pin)
    except CredentialFileError as exc:
        print(f'导入失败: {exc}')
        return 1

    cred['source'] = 'export-file'
    store = Store()
    filed = store.file_captured_credential(cred)
    print(f'已导入「{filed.get("name")}」的凭证(文件: {path.name})')
    if filed.get('is_active'):
        print('TA 已是活动成员, 探活/续期会自动接管。')
    else:
        print('未切换活动成员: 在界面成员栏里切到 TA(或用 lptiyu_member_switch)。')
    members = store.members_status()
    for item in members['items']:
        mark = '← 活动' if item['active'] else ('  有凭证' if item['has_token'] else '  无凭证')
        print(f"  - {item['name']}({item['student_num'] or '?'}) {mark}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
