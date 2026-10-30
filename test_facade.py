# -*- coding: utf-8 -*-
"""离线回归测试：门面对象的子客户端委派契约与凭据传播。

这些用例不触网，全部用桩对象替换网络调用。
"""
import unittest
from unittest.mock import MagicMock, patch

from sport_client import SportClient792


class FacadeDelegationTests(unittest.TestCase):
    """SportClient792 是一层门面，必须把请求委派给真正持有凭据的子客户端。"""

    def setUp(self):
        self.client = SportClient792(uid="123", token="TKN", unid="456")
        # 用桩替换网络出口，避免真实请求
        self.profile_execute = patch.object(
            self.client.profile, "execute_request", return_value={"error": 10000, "data": {}}
        ).start()
        self.addCleanup(patch.stopall)

    def test_cheat_blacklist_uses_netsec_capable_client(self):
        """曾经的 Bug：门面把自己当作 NetSec 客户端传给 telemetry，导致 AttributeError。"""
        self.client.get_cheat_blacklist()
        self.assertTrue(
            self.profile_execute.called,
            "get_cheat_blacklist 必须走具备 execute_request 的子客户端",
        )

    def test_barrier_free_policy_uses_netsec_capable_client(self):
        self.client.get_barrier_free_policy()
        self.assertTrue(self.profile_execute.called)

    def test_rank_list_uses_netsec_capable_client(self):
        self.client.get_ranks(rank_type=3)
        self.assertTrue(self.profile_execute.called)

    def test_credit_popup_uses_netsec_capable_client(self):
        self.client.get_credit_popup()
        self.assertTrue(self.profile_execute.called)

    def test_facade_exposes_execute_request(self):
        """防回归：门面本身不应被误当作 NetSec 客户端使用。"""
        self.assertFalse(
            hasattr(self.client, "execute_request"),
            "SportClient792 不具备 NetSec 传输能力，不应暴露 execute_request",
        )


class CredentialPropagationTests(unittest.TestCase):
    """set_credentials 必须同步到所有子客户端。"""

    def setUp(self):
        self.client = SportClient792()

    def test_credentials_reach_every_subclient(self):
        self.client.set_credentials(uid="999", token="NEW", unid="888")
        for name in ("profile", "policy", "running", "history", "ads"):
            sub = getattr(self.client, name)
            with self.subTest(subclient=name):
                self.assertEqual(sub.uid, "999")
                self.assertEqual(sub.token, "NEW")
                self.assertEqual(sub.unid, "888")

    def test_facade_fields_updated(self):
        self.client.set_credentials(uid="111", token="T2", unid="222")
        self.assertEqual(self.client.uid, "111")
        self.assertEqual(self.client.token, "T2")
        self.assertEqual(self.client.unid, "222")


class _FakeResult:
    success = True
    user = MagicMock(uid=7, token="TK", unid=8)


class LoginPropagationTests(unittest.TestCase):
    def test_successful_login_propagates_credentials(self):
        client = SportClient792()
        client.login_client.login = MagicMock(return_value=_FakeResult())
        client.login("u", "p")
        self.assertEqual(client.uid, "7")
        self.assertEqual(client.token, "TK")
        self.assertEqual(client.profile.uid, "7")


if __name__ == "__main__":
    unittest.main()
