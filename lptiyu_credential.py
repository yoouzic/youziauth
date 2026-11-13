"""乐跑凭证的导出/导入文件格式: PIN 加密的「密封件」。

场景: 成员在**自己的电脑**上用提取器(tools/lptiyu_credential_export.py)抓到
凭证, 生成一个文件经微信发给管理者; 管理者用导入端(tools/lptiyu_member_import.py
或界面的「导入凭证」)解密入档。文件会离开本机、经过聊天传输, 所以必须加密:
PIN 由双方口头约定, 不随文件走。

格式(明文 JSON):
    {"v": 1, "kind": "lptiyu-credential", "created_at": "ISO",
     "payload": "<base64(iv + AES-128-CBC(PKCS7) 明文)>"}
    明文 = "LPTCRED1|" + JSON(凭证 dict)  —— 魔数让错误 PIN 一试便知。

密钥 = SHA256(PIN) 前 16 字节(AES-128); IV 每次随机。强度定位是「防传输
中途拾取」, 不是防定向爆破 —— PIN 请用 6 位以上不常见的组合。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FILE_VERSION = 1
FILE_KIND = 'lptiyu-credential'
MAGIC = b'LPTCRED1|'
PREFIX = 'lptiyu-credential-'
REQUIRED_FIELDS = ('token', 'uid', 'student_num', 'school_id')


class CredentialFileError(ValueError):
    """文件不是本格式 / 版本不认识 / PIN 不对。"""


def now():
    return dt.datetime.now()


def _key(pin: str) -> bytes:
    return hashlib.sha256(pin.encode('utf-8')).digest()[:16]


def _pad(data: bytes) -> bytes:
    length = 16 - len(data) % 16
    return data + bytes([length]) * length


def _unpad(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise CredentialFileError('密文长度异常')
    return data[:-data[-1]]


def seal_credential(cred: dict, pin: str) -> dict:
    """凭证 dict + PIN -> 可写盘的文件 dict。"""
    pin = str(pin or '').strip()
    if len(pin) < 4:
        raise CredentialFileError('PIN 至少 4 位(建议 6 位以上)')
    plain = MAGIC + json.dumps(dict(cred), ensure_ascii=False).encode('utf-8')
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(_key(pin)), modes.CBC(iv)).encryptor()
    sealed = encryptor.update(_pad(plain)) + encryptor.finalize()
    return {'v': FILE_VERSION, 'kind': FILE_KIND,
            'created_at': now().isoformat(timespec='seconds'),
            'payload': base64.b64encode(iv + sealed).decode('ascii')}


def open_credential(file: dict, pin: str) -> dict:
    """文件 dict + PIN -> 凭证 dict; PIN 不对/格式坏抛 CredentialFileError。"""
    if not isinstance(file, dict) or file.get('kind') != FILE_KIND:
        raise CredentialFileError('这不是乐跑凭证文件')
    if file.get('v') != FILE_VERSION:
        raise CredentialFileError(f'凭证文件版本不认识(v={file.get("v")})')
    try:
        blob = base64.b64decode(str(file.get('payload') or ''), validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialFileError('凭证文件内容损坏') from exc
    if len(blob) < 32 or len(blob) % 16:
        raise CredentialFileError('凭证文件内容损坏')
    iv, sealed = blob[:16], blob[16:]
    decryptor = Cipher(algorithms.AES(_key(str(pin))), modes.CBC(iv)).decryptor()
    plain = _unpad(decryptor.update(sealed) + decryptor.finalize())
    if not plain.startswith(MAGIC):
        raise CredentialFileError('PIN 不对(或文件损坏)')
    try:
        cred = json.loads(plain[len(MAGIC):].decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CredentialFileError('凭证内容损坏') from exc
    if not isinstance(cred, dict) or not all(cred.get(field) for field in REQUIRED_FIELDS):
        raise CredentialFileError('凭证字段不全, 请让对方重新提取一次')
    return cred


def open_credential_file(path, pin: str) -> dict:
    """读路径上的凭证文件并解密(坏 JSON/坏格式统一为 CredentialFileError)。"""
    try:
        file = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise CredentialFileError(f'凭证文件读不出来: {exc}') from exc
    return open_credential(file, pin)


def export_file_name() -> str:
    """凭证文件名: 带日期时间, 多人同日导出也不撞名。"""
    return f'{PREFIX}{now():%Y%m%d-%H%M%S}.json'


def desktop_dir() -> Path:
    """桌面真实位置(常被重定向, Path.home() 会指错) —— 与 lptiyu_store 同款做法。"""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                        ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

        # FOLDERID_Desktop {B4BFCC3B-DB2C-424C-B029-7FE99A87C641}
        folder = GUID(0xB4BFCC3B, 0xDB2C, 0x424C,
                      (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                           0x9A, 0x87, 0xC6, 0x41))
        pointer = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder), 0, None, ctypes.byref(pointer)) == 0:
            path = Path(pointer.value)
            ctypes.windll.ole32.CoTaskMemFree(pointer)
            return path
    except Exception:               # noqa: BLE001 —— 解析不出就退回拼接
        pass
    return Path.home() / 'Desktop'


def candidate_dirs() -> list[Path]:
    """导入端扫描凭证文件的目录: 桌面 + 下载(微信收文件常落在这两处)。"""
    home = Path.home()
    dirs = [desktop_dir(), home / 'Downloads']
    return [d for d in dirs if d.is_dir()]


def scan_export_files() -> list[Path]:
    """候选目录里所有凭证文件, 按修改时间新->旧。"""
    found = []
    for directory in candidate_dirs():
        found.extend(directory.glob(f'{PREFIX}*.json'))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def find_latest_export() -> Path | None:
    """最新的一份凭证文件; 一份都没有返回 None。"""
    files = scan_export_files()
    return files[0] if files else None
