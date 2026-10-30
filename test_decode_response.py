# -*- coding: utf-8 -*-
"""离线回归测试：统一响应解包器 decode_response。

core.client 与 login.client 现在共用 core.crypto.decode_response，
本用例锁定其行为，防止两条链路再次分叉。
"""
import base64
import json
import unittest

from core.crypto import (
    aes128_cbc_encrypt,
    decode_response,
    response_kdf,
)


def _wrap(business_payload, resp_key):
    """把业务响应按网关格式打包成 {"r": ...} 信封。"""
    inner = {"data": base64.b64encode(json.dumps(business_payload).encode("utf-8")).decode("ascii")}
    r_bytes = aes128_cbc_encrypt(json.dumps(inner).encode("utf-8"), resp_key)
    return {"r": base64.b64encode(r_bytes).decode("ascii")}


class DecodeResponseTests(unittest.TestCase):
    def setUp(self):
        self.key = response_kdf("kd1", "kd2", "kd3", "kd4")

    def test_unwraps_json_business_payload(self):
        biz = {"error": 10000, "message": "成功", "data": {"uid": 42}}
        self.assertEqual(decode_response(_wrap(biz, self.key), self.key), biz)

    def test_unwraps_non_json_text_payload(self):
        """业务体不是 JSON 时应回退为文本，而不是抛异常。"""
        text = "运动世界校园"
        inner = {"data": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        r_bytes = aes128_cbc_encrypt(json.dumps(inner).encode("utf-8"), self.key)
        envelope = {"r": base64.b64encode(r_bytes).decode("ascii")}
        self.assertEqual(decode_response(envelope, self.key), text)

    def test_passthrough_when_no_envelope(self):
        """没有 r 字段时原样返回（例如网关直接返回业务码）。"""
        plain = {"error": 10121, "message": "设备保护"}
        self.assertEqual(decode_response(plain, self.key), plain)

    def test_non_dict_passthrough(self):
        self.assertEqual(decode_response([1, 2, 3], self.key), [1, 2, 3])

    def test_inner_without_data_returns_inner_object(self):
        inner = {"code": 0, "note": "no data field"}
        r_bytes = aes128_cbc_encrypt(json.dumps(inner).encode("utf-8"), self.key)
        envelope = {"r": base64.b64encode(r_bytes).decode("ascii")}
        self.assertEqual(decode_response(envelope, self.key), inner)


if __name__ == "__main__":
    unittest.main()
