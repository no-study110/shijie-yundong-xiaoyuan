# 冒烟检查

一条命令回答「这份代码现在还能跑吗」：`python smoke.py`，约 2.5 秒。

## 为什么需要它

这个仓库交付过一次**语法合法但完全不可运行**的代码：`running/builder.py` 被截断成 353 字节，只剩一段 docstring，类定义全丢。当时的状态是：

- `python -m py_compile running/builder.py` —— 绿灯，文件是合法 Python；
- `python -m unittest discover` —— 报 `Ran 0 tests`，因为 8 个测试模块里有 6 个在**导入阶段**就 ImportError 了，`Ran 0 tests` 看起来像「没写测试」，不像「测试挂了」；
- 只有 `from running.builder import RunningTrackBuilder` 这一句能立刻暴露问题。

所以这道闸的核心不是覆盖率，而是**可运行性**：文件在不在、符号在不在、模块能不能 import。

## 怎么跑

```bash
python smoke.py              # 完整检查（静态 + 契约 + 导入 + 单元测试 + 配置）
python smoke.py --skip-tests # 只做静态与导入，0.5 秒，pre-commit 用
python smoke.py --verbose    # 更多细节
```

Windows 还可以直接双击 `check.cmd`，Linux/macOS 用 `./check.sh`，参数一样透传。

退出码：**0 全通过，1 有失败项**。CI 与 git 钩子都靠这个判断。

正常输出长这样：

```
运动世界校园 7.3.92 — 冒烟检查
====================================================

[1/5] 静态完整性
  OK   64 个源文件语法正确
  OK   没有只剩 docstring 的空壳模块
  OK   无 DEBUG 残留
  OK   无高于 Python 3.9 的 f-string 语法
  OK   没有与标准库同名的包或模块

[2/5] 关键契约
  OK   15 个关键文件，48 个公开符号齐备

[3/5] 模块导入
  OK   57 个模块全部可导入（跳过需第三方依赖的 webui）

[4/5] 单元测试
  OK   338 个用例全部通过（3.2s；Flask / Node 相关用例在干净环境自动跳过）

[5/5] 配置健康
  OK   config.ini 可解析，device_id 已生成（9 个段）

----------------------------------------------------
通过 5/5   耗时 2.45s
```

## 五个阶段各查什么

| 阶段 | 检查内容 | 能拦住的事故 |
|---|---|---|
| 1 静态完整性 | AST 可解析；非 `__init__.py` 且被其他模块 import 的模块必须有顶层符号；无 `[DEBUG]` 残留；无高于 Python 3.9 的 f-string 写法；顶层包名不与标准库重名 | 文件被截断成只剩 docstring；语法错误；调试 print 上库；本地 3.13 跑通但 CI 的 3.9 编译失败；业务包遮蔽标准库 |
| 2 关键契约 | `CONTRACTS` 表里每个文件必须存在且包含指定公开符号 | 类/函数被误删或改名，导致下游 ImportError |
| 3 模块导入 | 逐个 `importlib.import_module`，捕获任何异常；并把导入期 stdout 抓起来当作副作用报警 | 循环导入、缺失依赖、模块级代码崩溃 |
| 4 单元测试 | `unittest discover -p "test_*.py"` | 逻辑回归 |
| 5 配置健康 | `config.ini` 可被 configparser 解析；`device_id` 只出现一行 | `device_id` 重复写入导致 `DuplicateOptionError`，配置全量回退默认值 |

第 1 阶段的「空壳检测」不是写死文件名，而是通用判据：一个被别处 import 的模块，如果顶层只剩 docstring / import / pass，就判为疑似截断。这样将来任何一个文件被类似方式改坏都会被抓到。

第 1 阶段后两项同样是通用判据，各对应一次真实事故：

- **f-string 写法版本**：12 版之后 PEP 701 允许 f-string 内部复用外层引号（`f"...{record["speed"]}"`）。这种写法在 3.12+ 完全正常、在 3.9~3.11 是 SyntaxError，而且 `ast.parse(feature_version=(3,9))` 拦不住 —— 引号复用属于 tokenizer 层的变化。所以这里直接按 token 判，而不是靠语法产生式。
- **包名遮蔽标准库**：曾经有个业务包叫 `profile/`，导致 `import profile` 永远命中它，标准库的 profile 模块不可用，`import cProfile` 直接报 `module 'profile' has no attribute 'run'` —— 性能剖析和部分覆盖率工具全部失效，而测试一直是绿的。


第 5 阶段在 `config.ini` 不存在时只告警不失败 —— `.gitignore` 忽略了它，干净 clone 本来就没有。

## 三种触发方式

**手动**：改完东西提交前跑一次，最省事的习惯。

**pre-commit 钩子**（仓库初始化 git 后配置一次）：

```bash
git config core.hooksPath 协议/7.3.92/hooks
```

之后每次 `git commit` 会跑 `--skip-tests` 那一档，约 0.5 秒。确实要提交半成品时用 `git commit --no-verify` 旁路。

**GitHub Actions**：`.github/workflows/ci.yml`，push 与 PR 都触发，Python 3.9 / 3.12 双版本。

CI **故意不安装** `requirements.txt` —— 核心协议是零第三方依赖的，冒烟要验证的正是「干净环境能不能跑」。唯一需要第三方包的 `webui.py`（flask）在 `IMPORT_OPTIONAL` 里，会跳过导入但仍做语法检查。对应的 `test_webui.py` 用 `skipUnless` 做同样处理：装了 Flask 就跑满 16 个用例，没装则跳过 14 个（配置回退那 2 个与 Flask 无关，仍然会执行）。

## 怎么扩展

改一个模块的公开接口，顺手更新 `smoke.py` 顶部的 `CONTRACTS`：

```python
CONTRACTS: Dict[str, Sequence[str]] = {
    "running/builder.py": ["RunningTrackBuilder", "compute_sport_signature", ...],
    ...
}
```

规则：只列**别的模块真的会 import** 的符号，不要把私有函数塞进去 —— 契约表的作用是「改坏了立刻知道」，不是「冻结所有实现细节」。

新增需要第三方依赖的模块时，把它加进 `IMPORT_OPTIONAL`，并写清为什么。

## 它不做什么

- 不联网、不发真实请求 —— 全部离线，可在无网络环境跑；
- 不替代单元测试 —— 它只保证「能跑起来」，逻辑正确性靠 `test_*.py`；
- 不检查 `requirements.txt` 是否装全 —— 恰恰相反，它假设没装；
- 不检查代码风格 —— 这类事交给 formatter/linter，别塞进冒烟。

保持它**快且确定**，是它能不能长期活着的前提。一个 30 秒、时好时坏的检查，两周后就没人跑了。
