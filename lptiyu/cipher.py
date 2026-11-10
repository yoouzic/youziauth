"""信封加密与签名(spec 常量的唯一消费方之一)。"""
import base64
import hashlib
import secrets

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from . import spec


class CipherError(ValueError):
    pass


def seal(plaintext: str) -> str:
    """AES-128-CBC(PKCS7) → base64, 与小程序 wx.request 数据信封逐字节同构。"""
    box = AES.new(spec.AES_KEY, AES.MODE_CBC, spec.AES_IV)
    return base64.b64encode(box.encrypt(pad(plaintext.encode("utf-8"), 16))).decode()


def unseal(data_b64: str) -> str:
    """seal 的逆; 失败抛 CipherError(调用方决定吞不吞)。"""
    try:
        box = AES.new(spec.AES_KEY, AES.MODE_CBC, spec.AES_IV)
        return unpad(box.decrypt(base64.b64decode(data_b64)), 16).decode("utf-8")
    except Exception as e:            # noqa: BLE001 —— 统一成领域异常
        raise CipherError(f"unseal failed: {e}") from e


def sign(params: dict) -> str:
    """SignMD5: 键升序拼接 k+v, 末尾拼 secret。空值也参与(与端上一致)。"""
    concat = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hashlib.md5((concat + spec.SIGN_SECRET).encode("utf-8")).hexdigest()


def nonce6() -> str:
    """6 位数字 nonce。CSPRNG。"""
    return f"{secrets.randbelow(1000000):06d}"
