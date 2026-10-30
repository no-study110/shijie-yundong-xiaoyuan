#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交前冒烟检查 —— 十秒内回答「这份代码还能跑吗」。

为什么需要这个脚本：本仓库出现过 ``running/builder.py`` 被截断成 353 字节
（只剩一段 docstring，类定义全丢）却仍然交付的情况。文件本身是合法 Python，
``python -m py_compile`` 一路绿灯，但全项目 6 个测试模块直接 ImportError、
其余大面积挂起。事后复盘：只要一条 ``import`` 检查就能拦住。

所以这里把「最小可运行性」固化成五个阶段，任何一项不过就以非 0 退出：

  1. 静态完整性   语法可解析、没有「只剩 docstring」的空壳文件、无 DEBUG 残留
  2. 关键契约     核心模块的公开符号还在（RunningTrackBuilder / NetSecClient …）
  3. 模块导入     每个模块真能被 import —— 这一步是拦截断的主闸
  4. 单元测试     unittest discover 全绿
  5. 配置健康     config.ini 可解析、device_id 唯一

用法::

    python smoke.py              # 完整检查
    python smoke.py --skip-tests # 只做静态与导入（pre-commit 用）
    python smoke.py --verbose    # 打印每个通过项

退出码：0 全通过，1 有失败项（CI 直接据此判定）。
"""

import argparse
import ast
import configparser
import contextlib
import importlib
import io
import os
import re
import sys
import time
import tokenize
import traceback
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent

# 递归扫描时跳过的目录：构建产物与虚拟环境里的 .py 不属于本仓库源码
SKIP_DIRS = {"__pycache__", "build", "dist", "venv", "env", ".git", ".idea", ".vscode",
             # android/ 下是打包时自动同步的源码副本，检查它等于把同一份代码数两遍
             "android"}

# 需要第三方包才能导入的模块：仍然参与语法检查，但不参与导入检查。
# webui.py 依赖 flask，而核心协议刻意保持零第三方依赖，缺 flask 不该判定失败。
IMPORT_OPTIONAL = {"webui"}

# 关键文件的公开契约。任何一个符号消失都说明该文件被改坏了 ——
# 这正是 builder.py 那次事故的形态：文件还在、语法合法、但类没了。
CONTRACTS: Dict[str, Sequence[str]] = {
    "sport_client.py": ["SportClient792"],
    "running/builder.py": [
        "RunningTrackBuilder", "compute_sport_signature", "gzip_base64_encode",
        "TrackLengthError", "TrackLengthWarning", "LengthReport",
        # speed 字段的单位换算只有这一处，写错就会「本地全绿、服务端判未达标」
        "pace_thousandths",
    ],
    "running/client.py": ["RunningClient"],
    "running/compliance.py": ["ComplianceValidator", "ComplianceReport", "CheckpointHit"],
    "running/consistency.py": ["sample_route", "time_buckets", "distance", "check_track"],
    "running/planner.py": ["FenceRoutePlanner"],
    "running/kinematics.py": ["validate_kinematics"],
    "running/riskguard.py": ["RiskReport", "RiskFinding", "preflight",
                             "extract_credit_score", "LEVEL_BLOCK"],
    "core/ledger.py": ["RunLedger", "DEVICE_NEW", "DEVICE_SAME", "DEVICE_CHANGED"],
    "core/crypto.py": ["NetSecClient", "response_kdf", "decode_response",
                       "aes128_cbc_encrypt", "compute_token_sign"],
    "core/client.py": ["BaseNetSecClient"],
    "policy/models.py": ["GeoPoint", "GeoFenceArea", "RunRule"],
    "config_loader.py": ["get_or_create_device_id", "get_device_id_from_file",
                         "set_device_id", "validate_device_id", "normalize_device_id",
                         "get_captcha_config"],
    # [captcha] 段曾只有定义没有调用方，填了也不生效 —— 这两个探测函数是
    # 唯一的接线点，删掉就等于让配置重新变成摆设。
    "login/captcha.py": ["detect_node_executable", "detect_node_paths", "GeetestV4Solver"],
    # 只列本文件自己定义的符号：validate_device_id 是从 config_loader 导入的，
    # 它归 config_loader 那条契约管，写在这里反而会误报。
    "device_id_tool.py": ["main", "generate", "resolve_effective"],
}

# 源码里不该出现的调试残留。历史上 policy/client.py 与 policy/models.py 各留过
# 一处 `print(f"[DEBUG] ...")`，会在真实跑批时把围栏原始报文刷到控制台。
# 用纯字符串而不是正则：本文件自己就要写出这个标记，正则会被自己的文档命中。
DEBUG_MARKERS = ("[DEBUG]",)

# 检查残留时跳过自身：脚本必须写下这些字面量才能做检测。
SELF_NAME = "smoke.py"

# 只含这些顶层语句的模块视为「空壳」：docstring / pass / import。
# 一个被别的文件 import 却什么都不定义的模块，几乎一定是被截断了。
EMPTY_STMT = (ast.Expr, ast.Import, ast.ImportFrom, ast.Pass)

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

class Console:
    """按终端能力降级：管道里跑就不要吐 ANSI 转义。"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        # 只在真终端上上色：Windows 控制台默认不解 ANSI，管道里也会污染日志
        self.use_color = sys.stdout.isatty() and not sys.platform.startswith("win")
        if os.environ.get("NO_COLOR"):
            self.use_color = False

    def _paint(self, text: str, color: str) -> str:
        return f"{color}{text}{RESET}" if self.use_color else text

    def ok(self, text: str) -> None:
        print(f"  {self._paint('OK  ', GREEN)} {text}")

    def fail(self, text: str) -> None:
        print(f"  {self._paint('FAIL', RED)} {text}")

    def warn(self, text: str) -> None:
        print(f"  {self._paint('WARN', YELLOW)} {text}")

    def info(self, text: str) -> None:
        print(f"       {self._paint(text, DIM)}")

    def stage(self, index: int, total: int, title: str) -> None:
        print(f"\n[{index}/{total}] {title}")


# --------------------------------------------------------------------------- #
# 源码收集与解析
# --------------------------------------------------------------------------- #

def collect_sources() -> List[Path]:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return found


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def assigned_names(target: ast.AST) -> List[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: List[str] = []
        for element in target.elts:
            names.extend(assigned_names(element))
        return names
    return []


def top_level_symbols(tree: ast.Module) -> List[str]:
    names: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.extend(assigned_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.extend(assigned_names(node.target))
    return [name for name in names if name != "__all__"]


def imported_module_names(trees: Dict[Path, ast.Module]) -> set:
    """收集全仓库 import 过的模块名 —— 用来判断某个空壳模块是否真的有人用。"""
    names = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
    return names


# --------------------------------------------------------------------------- #
# 阶段 1：静态完整性
# --------------------------------------------------------------------------- #

def fstring_quote_reuse(text: str) -> List[Tuple[int, str]]:
    """找出只有 Python 3.12+ 才合法的 f-string 写法（PEP 701）。

    项目声明最低支持 3.9（见 ``.github/workflows/ci.yml`` 的版本矩阵）。
    PEP 701 放宽了 f-string 的引号规则，允许内部复用外层的定界引号：

        log(f"... 配速={record["speed"]}")      # 3.12+ 合法，3.9~3.11 语法错误

    这类写法在本地新版 Python 上跑得好好的，却会让 CI 的 3.9 任务直接编译失败。
    而且 ``ast.parse(feature_version=(3, 9))`` 拦不住 —— 引号复用属于 tokenizer
    层的变化，feature_version 只影响语法产生式。所以这里直接按 token 判：

      * 3.13 的 tokenizer 把 f-string 拆成 FSTRING_START / MIDDLE / END；
      * 若 MIDDLE（字符串正文）里出现外层定界引号且未转义，就是 PEP 701；
      * 若内部的字符串字面量起首就是同种引号，同理。

    返回 ``[(行号, 说明)]``。
    """
    hits: List[Tuple[int, str]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return hits

    stack: List[str] = []
    for token in tokens:
        if token.type == tokenize.FSTRING_START:
            if stack:
                hits.append((token.start[0], "f-string 嵌套（3.12+ 才有）"))
            stack.append(token.string.lstrip("fFrRbBuU"))
        elif token.type == tokenize.FSTRING_END:
            if stack:
                stack.pop()
        elif stack:
            outer = stack[-1]
            if len(outer) != 1:            # 三引号不涉及引号复用
                continue
            if token.type == tokenize.STRING:
                inner = token.string.lstrip("fFrRbBuU")
                if inner.startswith(outer):
                    hits.append((token.start[0],
                                 f"f-string 内部字符串复用了同种引号 {outer}"))
            elif token.type == tokenize.FSTRING_MIDDLE:
                position = token.string.find(outer)
                while position != -1:
                    if position == 0 or token.string[position - 1] != "\\":
                        hits.append((token.start[0],
                                     f"f-string 正文里出现未转义的 {outer}"))
                        break
                    position = token.string.find(outer, position + 1)
    return hits


def stdlib_module_names() -> set:
    """标准库的顶层模块名集合。

    Python 3.10+ 直接读 ``sys.stdlib_module_names``；3.9 没有这个属性，
    退化为扫描 stdlib 目录 —— 项目声明支持 3.9，冒烟自己也得能在 3.9 跑。
    """
    names = set(getattr(sys, "stdlib_module_names", ()) or ())
    if names:
        return names
    import sysconfig

    stdlib = Path(sysconfig.get_paths()["stdlib"])
    if stdlib.is_dir():
        names |= {path.stem for path in stdlib.glob("*.py")}
        names |= {path.name for path in stdlib.iterdir()
                  if path.is_dir() and (path / "__init__.py").exists()}
    names |= set(sys.builtin_module_names)
    return names


def shadowed_stdlib() -> List[str]:
    """找出与标准库同名的业务包/模块。

    项目里曾经有个业务包叫 ``profile``，于是任何 ``import profile`` 都会
    命中它，标准库的 profile 模块彻底不可用 —— ``import cProfile`` 直接报
    ``module 'profile' has no attribute 'run'``，性能剖析、部分覆盖率工具
    全部失效。这类冲突不会让测试变红，只会在你想用工具链时突然冒出来，
    所以必须在提交前静态拦下。

    只检查顶层：``running.xxx`` 这样的子模块即使叫 xxx 也不会遮蔽标准库，
    真正冲突的是能被 ``import xxx`` 直接命中那一层。
    """
    stdlib = stdlib_module_names()
    conflicts: List[str] = []
    for path in sorted(ROOT.iterdir()):
        if path.name in SKIP_DIRS or path.name.startswith("."):
            continue
        if path.is_dir() and (path / "__init__.py").exists():
            name = path.name
        elif path.is_file() and path.suffix == ".py":
            name = path.stem
        else:
            continue
        if name in stdlib:
            conflicts.append(f"{path.name}  与标准库模块 {name!r} 同名，"
                             f"会让标准库该模块不可导入")
    return conflicts


def stage_static(sources: List[Path], console: Console) -> Tuple[bool, Dict[Path, ast.Module]]:
    trees: Dict[Path, ast.Module] = {}
    syntax_errors: List[str] = []
    for path in sources:
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            syntax_errors.append(f"{path.relative_to(ROOT)}:{exc.lineno}  {exc.msg}")
        except UnicodeDecodeError as exc:
            syntax_errors.append(f"{path.relative_to(ROOT)}  无法按 UTF-8 解码: {exc}")

    if syntax_errors:
        console.fail(f"{len(syntax_errors)} 个文件语法错误")
        for line in syntax_errors:
            console.info(line)
        return False, trees
    console.ok(f"{len(sources)} 个源文件语法正确")

    # 空壳检测：非 __init__.py、被别的文件 import、却一个顶层符号都没有
    imported = imported_module_names(trees)
    hollow: List[str] = []
    for path, tree in trees.items():
        if path.name == "__init__.py":
            continue  # __init__.py 只做再导出，天然没有符号
        if top_level_symbols(tree):
            continue
        has_body = any(not isinstance(node, EMPTY_STMT) for node in tree.body)
        if has_body:
            continue
        name = module_name(path)
        if name in imported or any(ref.startswith(name + ".") for ref in imported):
            hollow.append(f"{path.relative_to(ROOT)}  只定义了 {len(tree.body)} 个空语句，"
                          f"但被其他模块 import —— 疑似被截断")

    if hollow:
        console.fail(f"{len(hollow)} 个模块疑似被截断（有语法但无任何内容）")
        for line in hollow:
            console.info(line)
    else:
        console.ok("没有只剩 docstring 的空壳模块")

    # 调试残留
    leftovers: List[str] = []
    for path, tree in trees.items():
        if path.name == SELF_NAME:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in DEBUG_MARKERS:
            start = text.find(marker)
            while start != -1:
                lineno = text.count("\n", 0, start) + 1
                leftovers.append(f"{path.relative_to(ROOT)}:{lineno}  {marker} 残留")
                start = text.find(marker, start + 1)
    if leftovers:
        console.warn(f"{len(leftovers)} 处调试残留（不阻断，但提交前应清掉）")
        for line in leftovers:
            console.info(line)
    else:
        console.ok("无 DEBUG 残留")

    # 最低版本语法：本地跑的是新版解释器，CI 还要跑 3.9
    version_hits: List[str] = []
    for path in sources:
        for lineno, reason in fstring_quote_reuse(path.read_text(encoding="utf-8")):
            version_hits.append(f"{path.relative_to(ROOT)}:{lineno}  {reason}")
    if version_hits:
        console.fail(f"{len(version_hits)} 处语法高于最低支持版本 Python 3.9")
        for line in version_hits:
            console.info(line)
    else:
        console.ok("无高于 Python 3.9 的 f-string 语法")

    # 包名遮蔽：业务包与标准库同名，会让标准库模块整体不可用
    shadowed = shadowed_stdlib()
    if shadowed:
        console.fail(f"{len(shadowed)} 个顶层包/模块与标准库同名")
        for line in shadowed:
            console.info(line)
    else:
        console.ok("没有与标准库同名的包或模块")

    return not hollow and not version_hits and not shadowed, trees


# --------------------------------------------------------------------------- #
# 阶段 2：关键契约
# --------------------------------------------------------------------------- #

def stage_contracts(trees: Dict[Path, ast.Module], console: Console) -> bool:
    missing: List[str] = []
    checked = 0
    for relative, required in CONTRACTS.items():
        path = ROOT / relative
        tree = trees.get(path)
        if tree is None:
            missing.append(f"{relative}  文件不存在")
            continue
        available = set(top_level_symbols(tree))
        checked += 1
        for symbol in required:
            if symbol not in available:
                missing.append(f"{relative}  缺少符号 {symbol}")

    if missing:
        console.fail(f"{len(missing)} 项契约缺失")
        for line in missing:
            console.info(line)
        return False
    console.ok(f"{checked} 个关键文件，{sum(len(v) for v in CONTRACTS.values())} 个公开符号齐备")
    return True


# --------------------------------------------------------------------------- #
# 阶段 3：模块导入
# --------------------------------------------------------------------------- #

def stage_import(sources: List[Path], console: Console) -> bool:
    modules = []
    skipped = []
    for path in sources:
        name = module_name(path)
        if not name or path.name.startswith("test_"):
            continue  # 测试模块由 discover 导入，这里不重复
        if name in IMPORT_OPTIONAL:
            skipped.append(name)
            continue
        modules.append(name)

    sys.path.insert(0, str(ROOT))
    importlib.invalidate_caches()

    failures: List[str] = []
    # 导入本身不该产生任何输出。捕起来既保持屏幕干净，也顺带当成
    # 「模块顶层有副作用」的探针 —— 这类 print 在真实跑批时会污染日志。
    noise = io.StringIO()
    for name in modules:
        try:
            with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
                importlib.import_module(name)
        except Exception as exc:                     # noqa: BLE001 - 冒烟要抓住一切
            failures.append(f"{name}  {type(exc).__name__}: {exc}")

    if failures:
        console.fail(f"{len(failures)}/{len(modules)} 个模块导入失败")
        for line in failures:
            console.info(line)
        return False

    note = f"（跳过需第三方依赖的 {', '.join(skipped)}）" if skipped else ""
    console.ok(f"{len(modules)} 个模块全部可导入{note}")

    chatter = [line for line in noise.getvalue().splitlines() if line.strip()]
    if chatter:
        console.warn(f"导入阶段产生了 {len(chatter)} 行输出，模块顶层疑似有副作用")
        for line in chatter[:5]:
            console.info(line)
    return True


# --------------------------------------------------------------------------- #
# 阶段 4：单元测试
# --------------------------------------------------------------------------- #

def stage_tests(console: Console) -> bool:
    loader = unittest.TestLoader()
    try:
        suite = loader.discover(str(ROOT), pattern="test_*.py", top_level_dir=str(ROOT))
    except Exception:                                # noqa: BLE001 - 导入期报错也要有干净输出
        console.fail("测试用例收集失败")
        console.info(traceback.format_exc(limit=3).strip())
        return False

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=0, buffer=True)
    started = time.time()
    result = runner.run(suite)
    elapsed = time.time() - started

    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    if bad:
        console.fail(f"{total} 个用例中 {bad} 个失败（{elapsed:.1f}s）")
        for case, _ in result.failures + result.errors:
            console.info(case.id())
        # 原始输出里带断言差异，直接透出最后 25 行，够定位
        detail = stream.getvalue().strip().splitlines()
        for line in detail[-25:]:
            console.info(line)
        return False

    extra = f"，跳过 {len(result.skipped)}" if result.skipped else ""
    console.ok(f"{total} 个用例全部通过（{elapsed:.2f}s{extra}）")
    return True


# --------------------------------------------------------------------------- #
# 阶段 5：配置健康
# --------------------------------------------------------------------------- #

def stage_config(console: Console) -> bool:
    config_path = ROOT / "config.ini"
    if not config_path.exists():
        # .gitignore 忽略了 config.ini，干净 clone 本来就没有它
        console.warn("config.ini 不存在（首次运行会自动生成，跳过检查）")
        return True

    text = config_path.read_text(encoding="utf-8")
    problems: List[str] = []

    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        problems.append(f"config.ini 解析失败: {exc}")

    # device_id 重复：历史上 loader 每运行一次就多写一行，累计几次后
    # configparser 直接抛 DuplicateOptionError，全量回退默认配置。
    duplicates = len(re.findall(r"^\s*device_id\s*=", text, flags=re.MULTILINE))
    if duplicates > 1:
        problems.append(f"config.ini 里有 {duplicates} 行 device_id（应只有 1 行）")

    if problems:
        console.fail(f"{len(problems)} 项配置问题")
        for line in problems:
            console.info(line)
        return False

    if parser.has_section("device"):
        device_id = (parser.get("device", "device_id", fallback="") or "").strip()
        state = "已生成" if device_id else "待首次生成"
        console.ok(f"config.ini 可解析，device_id {state}（{len(parser.sections())} 个段）")
    else:
        console.ok(f"config.ini 可解析（{len(parser.sections())} 个段，无 device 段）")
    return True


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="运动世界校园 7.3.92 提交前冒烟检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码 0 全通过；1 有失败项。",
    )
    parser.add_argument("--skip-tests", action="store_true", help="跳过单元测试（pre-commit 快速档）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出更多细节")
    args = parser.parse_args(argv)

    console = Console(verbose=args.verbose)
    started = time.time()

    print("运动世界校园 7.3.92 — 冒烟检查")
    print("=" * 52)

    stages: List[Tuple[str, bool]] = []

    console.stage(1, 5, "静态完整性")
    sources = collect_sources()
    static_ok, trees = stage_static(sources, console)
    stages.append(("静态完整性", static_ok))

    console.stage(2, 5, "关键契约")
    contract_ok = stage_contracts(trees, console) if static_ok else False
    if not static_ok:
        console.fail("静态检查未通过，跳过契约检查")
    stages.append(("关键契约", contract_ok))

    console.stage(3, 5, "模块导入")
    import_ok = stage_import(sources, console)
    stages.append(("模块导入", import_ok))

    if args.skip_tests:
        console.stage(4, 5, "单元测试")
        console.warn("已按 --skip-tests 跳过")
        stages.append(("单元测试", True))
    else:
        console.stage(4, 5, "单元测试")
        tests_ok = stage_tests(console)
        stages.append(("单元测试", tests_ok))

    console.stage(5, 5, "配置健康")
    config_ok = stage_config(console)
    stages.append(("配置健康", config_ok))

    elapsed = time.time() - started
    passed = sum(1 for _, ok in stages if ok)
    print("\n" + "-" * 52)
    if passed == len(stages):
        print(f"通过 {passed}/{len(stages)}   耗时 {elapsed:.2f}s")
        return 0

    failed = [name for name, ok in stages if not ok]
    print(f"通过 {passed}/{len(stages)}   失败：{'、'.join(failed)}   耗时 {elapsed:.2f}s")
    return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
