"""Offline known-answer tests for the NetSecKit crypto layer (core/crypto.py)."""
import base64
import hashlib
import json
import unittest

from core.crypto import (
    aes128_encrypt_block,
    aes128_decrypt_block,
    aes128_cbc_encrypt,
    aes128_cbc_decrypt,
    key_expansion_128,
    response_kdf,
    compute_token_sign,
    NetSecClient,
)
from core.config import SIGN_SALT


class AesTests(unittest.TestCase):
    def test_nist_fips197_known_vector(self):
        """FIPS-197 附录 B 标准向量，验证自研 AES-128 块加解密正确性。"""
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        ciphertext = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        round_keys = key_expansion_128(key)
        self.assertEqual(aes128_encrypt_block(plaintext, round_keys), ciphertext)
        self.assertEqual(aes128_decrypt_block(ciphertext, round_keys), plaintext)

    def test_cbc_roundtrip_with_padding(self):
        key = bytes(range(16))
        iv = bytes(range(16, 32))
        for data in (b"", b"a", b"hello world", bytes(range(17)), bytes(range(64))):
            with self.subTest(data=data):
                enc = aes128_cbc_encrypt(data, key, iv)
                self.assertEqual(len(enc) % 16, 0)
                self.assertEqual(aes128_cbc_decrypt(enc, key, iv), data)

    def test_cbc_default_zero_iv_roundtrip(self):
        key = b"0123456789abcdef"
        data = "运动世界校园 NetSecKit".encode("utf-8")
        enc = aes128_cbc_encrypt(data, key)
        self.assertEqual(aes128_cbc_decrypt(enc, key), data)


class KdfTests(unittest.TestCase):
    def test_response_kdf_deterministic_and_length(self):
        a = response_kdf("kd1", "kd2", "kd3", "kd4")
        b = response_kdf("kd1", "kd2", "kd3", "kd4")
        self.assertEqual(a, b)
        # 4 个 u32 小端 = 16 字节 AES 密钥
        self.assertEqual(len(a), 16)

    def test_response_kdf_differs_on_input(self):
        a = response_kdf("kd1", "kd2", "kd3", "kd4")
        b = response_kdf("kd1", "kd2", "kd3", "kdX")
        self.assertNotEqual(a, b)


class SignTests(unittest.TestCase):
    def test_token_sign_matches_independent_canonicalization(self):
        params = {"timeStamp": "1700000000000", "token": "DUMMY", "uid": "1"}
        canonical = "timeStamp=1700000000000&token=DUMMY&uid=1" + SIGN_SALT
        expected = hashlib.md5(canonical.encode("utf-8")).hexdigest().lower()
        self.assertEqual(compute_token_sign(params), expected)

    def test_token_sign_skips_signature_and_none(self):
        params = {"a": "1", "signature": "X", "b": None, "c": "2"}
        canonical = "a=1&c=2" + SIGN_SALT
        expected = hashlib.md5(canonical.encode("utf-8")).hexdigest().lower()
        self.assertEqual(compute_token_sign(params), expected)


class NetSecClientTests(unittest.TestCase):
    def test_encrypt_request_wire_shape_and_randomness(self):
        client = NetSecClient()
        wire, _ = client.encrypt_request({"hello": "world"})
        self.assertEqual(set(wire), {"d", "h", "k", "p", "t"})
        self.assertEqual(wire["p"], "101")
        self.assertEqual(wire["t"], "0")
        # 每次请求使用新的随机 AES 密钥，k 字段应不同
        wire2, _ = client.encrypt_request({"hello": "world"})
        self.assertNotEqual(wire["k"], wire2["k"])
        self.assertNotEqual(wire["d"], wire2["d"])

    def test_decrypt_response_roundtrip(self):
        client = NetSecClient()
        resp_key = response_kdf("kd1", "kd2", "kd3", "kd4")
        biz = {"error": 10000, "message": "成功", "data": {"uid": 42}}
        inner = {"data": base64.b64encode(json.dumps(biz).encode("utf-8")).decode()}
        r_bytes = aes128_cbc_encrypt(json.dumps(inner).encode("utf-8"), resp_key)
        resp_dict = {"r": base64.b64encode(r_bytes).decode()}
        self.assertEqual(client.decrypt_response(resp_dict, resp_key), biz)


if __name__ == "__main__":
    unittest.main()
