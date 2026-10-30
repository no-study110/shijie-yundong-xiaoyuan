# -*- coding: utf-8 -*-
"""冒烟检查自身的回归用例。

``smoke.py`` 里的两项静态判据（f-string 版本、包名遮蔽标准库）都是
「检查器」—— 如果检查器自己判错，它就会安静地失效：要么漏报（
真实问题照样上库），要么误报（每次提交都变红，最后被人关掉）。
所以检查器本身也需要用例。
"""
import unittest

from smoke import fstring_quote_reuse, shadowed_stdlib


class FStringVersionTests(unittest.TestCase):
    """PEP 701（3.12+ 才合法）的识别。"""

    def test_quote_reuse_is_flagged(self):
        """f-string 内部用与外层相同的引号 —— 3.9~3.11 会 SyntaxError。"""
        hits = fstring_quote_reuse('log(f"配速={record["speed"]}")\n')
        self.assertEqual(len(hits), 1)
        self.assertIn("同种引号", hits[0][1])

    def test_other_quote_inside_is_allowed(self):
        """内部换成另一种引号，3.9 也合法，不应报警。"""
        self.assertEqual(fstring_quote_reuse('log(f"配速={record[\'speed\']}")\n'), [])

    def test_escaped_quote_inside_is_allowed(self):
        """正文里转义过的引号是合法的老写法。"""
        self.assertEqual(fstring_quote_reuse('x = f"say \\"hi\\" {v}"\n'), [])

    def test_plain_strings_are_ignored(self):
        """非 f-string 的双引号与本次检查无关。"""
        self.assertEqual(fstring_quote_reuse('x = "a" + "b"\n'), [])

    def test_nested_fstring_is_flagged(self):
        """3.11 之前不允许 f-string 嵌套。"""
        hits = fstring_quote_reuse('x = f"{f"{y}"}"\n')
        self.assertTrue(any("嵌套" in reason for _, reason in hits))

    def test_broken_source_returns_empty(self):
        """源文件本身解析不了时不抛异常 —— 语法错误由前一阶段负责报。"""
        self.assertEqual(fstring_quote_reuse('def broken(:\n    pass\n'), [])


class StdlibShadowingTests(unittest.TestCase):
    """顶层包名遮蔽标准库的识别。"""

    def test_repository_has_no_shadowing(self):
        """当前仓库不应存在与标准库同名的顶层包/模块。

        历史上 ``profile/`` 就撞过标准库，导致 ``cProfile`` 完全不可用。
        """
        self.assertEqual(shadowed_stdlib(), [])

    def test_known_stdlib_names_cover_historical_case(self):
        """回归锚点：``profile`` 必须在标准库里，否则检查形同虚设。"""
        from smoke import stdlib_module_names

        names = stdlib_module_names()
        self.assertIn("profile", names)
        self.assertIn("json", names)


if __name__ == "__main__":
    unittest.main()
