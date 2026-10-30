# -*- coding: utf-8 -*-
"""向后兼容 re-export：登录加解密已统一收敛到 core.crypto。

原 login/crypto.py 是 core/crypto.py 的逐字节副本，为消除重复改为薄别名层，
保留 `from login.crypto import ...` / `from crypto import ...` 的调用方不变。
"""

from core.crypto import (
    NetSecClient,
    compute_token_sign,
    aes128_cbc_decrypt,
    aes128_cbc_encrypt,
    response_kdf,
)

__all__ = [
    "NetSecClient",
    "compute_token_sign",
    "aes128_cbc_decrypt",
    "aes128_cbc_encrypt",
    "response_kdf",
]
