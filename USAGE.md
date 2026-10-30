# 运动世界校园 7.3.92 - 使用说明

**版本**: 7.3.92 打包版  
**无需安装 Python 环境，开箱即用**

## 快速开始

### 1. 解压文件

将下载的压缩包解压到任意目录。

### 2. 配置（可选）

首次使用建议复制配置模板：

**Windows**: 复制 `config.ini.example` 为 `config.ini`  
**Linux**: `cp config.ini.example config.ini`

不配置也可以使用，会自动使用默认值。

### 3. 运行程序

提供两种使用方式：

#### 方式一：命令行版本（CLI）

**Windows**: 双击 `sport_cli.exe` 或在命令行运行  
**Linux**: `./sport_cli`

#### 方式二：网页控制台（Web UI）

**Windows**: 双击 `sport_webui.exe`  
**Linux**: `./sport_webui`

程序会自动打开浏览器访问 http://127.0.0.1:9999

## 命令行使用

### 登录

```bash
# Windows
sport_cli.exe -u 手机号 -p 密码

# Linux
./sport_cli -u 手机号 -p 密码
```

登录成功后，Token 会自动保存，下次无需重复登录。

### 跑步提交

**标准模式**（推荐，耗时约 13 分钟）：

```bash
# Windows
sport_cli.exe --run --distance 2150 --time-sec 760

# Linux
./sport_cli --run --distance 2150 --time-sec 760
```

**快速测试模式**（跳过物理时间，适合测试）：

```bash
# Windows
sport_cli.exe --run --no-physical-time --distance 2000 --time-sec 700

# Linux
./sport_cli --run --no-physical-time --distance 2000 --time-sec 700
```

### 查询功能

```bash
# 个人档案
sport_cli.exe --profile

# 跑步规则
sport_cli.exe --rules

# 历史记录
sport_cli.exe --history

# 学期进度
sport_cli.exe --semester

# 帮助信息
sport_cli.exe --help
```

## 网页控制台使用

1. 运行 `sport_webui.exe` (Windows) 或 `./sport_webui` (Linux)
2. 浏览器自动打开 http://127.0.0.1:9999
3. 在网页界面进行登录、跑步提交等操作
4. 实时查看日志输出

「账号」卡片最右侧的 **随机生成** 按钮会造一个 32 位指纹填进 DeviceId 输入框；
它**只填不写**，点登录并成功后才真正切换 —— 指纹换了等于换设备，而这一步
不该被一次误点触发。卡片下方那行「当前生效」显示的是**此刻真的会发出去**的
值及其来源（网页控制台 / `config.ini` / 未设置）。

### 手机端（Android APK）

`android/` 目录可打出一个 APK：里面跑同一套 Python，界面就是这个网页控制台。
构建与安装见 `android/README.md`。

登录要在电脑上做一次，因为滑块识别依赖 Node + Playwright，手机上跑不了：

1. 电脑登录：`python cli.py -u 手机号 -p 密码`
2. 取出项目目录下 `.session.json` 的内容
3. 手机 App「账号」卡片最底部，粘贴进输入框 → 点 **导入登录态**

导入后跑步、查历史、查规则都正常。密码不落盘，手机上只保存 token/uid/unid。

### 配速怎么看

全项目只有一种配速写法：`8′54″/km`（分′秒″/公里），与手机 App 记录页、服务端
拒绝文案（「配速控制在2′00″-10′00″」）完全一致。输入框旁的实时提示、历史记录表、
跑步结果、命令行回查显示的是同一个字符串，换算只在 `core/pace.py` 一处。

- **距离 + 耗时 → 配速**：`配速(秒/公里) = 耗时 ÷ 距离(公里)`。
  例：4080 米跑 2180 秒 → 2180 ÷ 4.08 = 534 秒 → `8′54″/km`。
- 历史记录以服务端保存的 `totalDis` / `totalTime` 为准。个别老记录的 `speed`
  字段是早期版本误填的速度值（如 1.872），不能直接当配速显示，只在里程/耗时
  缺失时才兜底。
- 上报报文里的 `speed` 字段是「配速 × 1000」（4080m/2180s → `8905`），
  与界面上的 `8′54″` 是同一个数的两种写法。

### 历史记录的状态怎么看

网页「历史记录」表与 `--history` 显示的状态是**服务端真实判定**，不是估的。

列表接口（`/runnings/records`）只给 `complete` 标记，**不返回判定明细**。实测 10 条
记录里列表 `complete` 全是 `true`，其中 2 条真实结论是「未达标: 配速控制在2′00″-10′00″」。
所以查看历史时会按 `rrid` 逐条回查详情（`get_one_record`）补上结论与原因：

- 10 条约 4 秒。网页日志会先打一行「逐条回查判定详情」，表格里「状态」列给结论、
  「原因」列给原话（如「配速控制在2′00″-10′00″」）。
- 命令行 `history_cli --no-detail` 可跳过回查：更快，但只能看到列表字段，
  未达标原因看不到。
- 回查失败（网络抖动 / 接口变更）时状态退回列表字段、原因留空 —— 不编造理由，
  也不会因为一条回查失败让整张表报错。

## 配置说明

### 基本配置

打开 `config.ini`，可以修改以下内容：

#### 设备指纹（避免 10121 错误）

```ini
[device]
device_id = 你的32位DeviceId
```

留空是**推荐做法**：首次运行时自动生成一个随机指纹并**写回 config.ini**，之后一直复用它。注意是「生成一次、长期复用」，不是每次运行都换——每次换会被服务端当成新设备登录，正是 10121 的成因。

需要手动处理时用工具，不要手写：

```bash
python device_id_tool.py            # 生成一个随机值（只打印，不写入）
python device_id_tool.py --show     # 当前生效值 + 来源
python device_id_tool.py --check    # 校验格式（不合法退出码 1）
python device_id_tool.py --write    # 生成并写入
python device_id_tool.py --set <值> # 校验后写入指定值（--force 跳过校验）
```

格式是 32 位小写十六进制。带连字符的 UUID 会自动去横线，但**大小写不会被自动修正**（服务端按字符比对，大小写不同即两台设备）。

#### Web UI 端口

```ini
[webui]
host = 127.0.0.1
port = 9999
```

#### 拟真增强

```ini
[realism]
enabled = True                    # 启用配速波动和生理字段
simulate_physical_time = True     # 启用物理时间模拟（推荐）
```

- `enabled = True`: 配速会随机波动，更真实
- `simulate_physical_time = True`: 跑步会模拟真实耗时（跑前准备 + 跑步 + 上传），避免风控

#### 代理设置

```ini
[proxy]
proxy_url = http://127.0.0.1:8080
```

留空则直连。

完整配置说明见 `config.ini.example` 文件注释。

## 常见问题

### 1. 登录报错 10121「账号已在其他设备登录」

**原因**: 服务端检测到设备变化。

**解决方案**:
1. 在手机 App 内「退出登录」后重试
2. 或获取原手机的 DeviceId，填入 `config.ini` 的 `device_id` 配置

先跑 `python device_id_tool.py --show` 看清当前生效的指纹和它的来源——网页控制台存的那份优先级高于 `config.ini`，只改配置文件可能不生效。

在网页控制台里也可以直接点「随机生成」：新值只填进输入框，登录成功后才会生效，不会因为点了一下就把设备换掉。页面下方会显示**当前生效**的那个指纹和它的来源。

### 2. 登录报「未找到 playwright 模块」

滑块验证码跑在 Node + Playwright 上，机器上找不到 playwright 模块就会这样报。

在 `config.ini` 的 `[captcha]` 段填一行即可 —— 指向**装了 playwright 的那个
`node_modules` 目录**（不是 node 可执行文件所在目录）：

```ini
[captcha]
playwright_path = C://某处//node_modules
```

不知道它在哪：先跑 `npm root -g` 看全局目录，那里没有的话搜一下
`node_modules/playwright/package.json`。

也可以让项目自带一份：在项目根目录执行 `npm install playwright`，
首次还要 `npx playwright install chromium` 下载浏览器内核。

报错信息本身会列出**已经尝试过哪些路径**和当前的 `NODE_PATH`，照着填就行。

### 3. 跑步几秒完成会被风控吗？

**会**。建议启用物理时间模拟（默认开启），完整跑步流程约 13 分钟：
- 跑前准备: 1-3 分钟
- 跑步进行中: 按指定时长
- 跑后上传: 10-30 秒

快速测试时可用 `--no-physical-time` 跳过。

### 4. Windows Defender 拦截

部分杀毒软件可能误报，这是打包程序的常见问题，**不是病毒**。

**解决方案**: 添加信任/白名单。

### 5. Linux 提示权限不足

需要添加执行权限：

```bash
chmod +x sport_cli sport_webui
```

### 6. 多台电脑使用

可以复制整个文件夹到其他电脑，各自独立配置 `config.ini` 即可。

### 7. 清除登录信息

删除 `.session.json` 文件即可清除本地缓存的登录凭据。

## 参数说明

### 跑步参数

- `--mode 1`: 计分跑（默认）
- `--mode 6`: 自由跑
- `--distance 2150`: 跑步距离（米）
- `--time-sec 760`: 跑步耗时（秒）
- `--no-physical-time`: 跳过物理时间模拟（快速测试用）
- `--no-realism`: 禁用配速波动（纯均分）
- `--match-history`: 先拉最近 20 条历史，把时长调整到个人配速分布内。历史不足 3 条、拉取失败或调整后低于学校最低时长时自动放弃并保持原时长
- `--no-risk-check`: 跳过提交前的风控预检（默认启用，见下节）

```bash
# 让本次配速落在自己历史区间内（2150m 时把手填的 760s 交给画像判断）
python cli.py -t <Token> --uid <UID> --unid <UNID> --run --distance 2150 --time-sec 760 --match-history
```

### 风控预检与本地账本

提交前会做一次预检，逐项打印结论（`[通过]` / `[注意]` / `[阻断]`）：

| 检查项 | 判据 | 阻断条件 |
|---|---|---|
| 控制信令 | 服务端心跳是否下发封禁/冻结类公告 | 下发内容含处置关键词 |
| 信用分 | 与账本上一次记录比对涨跌 | 单次下跌 ≥10 分 |
| 设备指纹 | 与账本登记过的指纹比对 | 不阻断（10121 由服务端返回） |
| 提交频次 | **自然日**内成功提交次数 | 今日已 ≥2 次 |
| 时段 | 与历史提交小时的中位数比对 | 不阻断（偏差 ≥4 小时提示） |
| 作弊特征库 / 无障碍策略 | 拉取服务端名单并落盘 | 不阻断（本实现不注入、不注册无障碍） |

原则是**查不到不是罪证**：任何一项网络失败都只降级为提示，不会阻断提交。
只有确认有问题（控制信令点名封禁、今日已提交 2 次、信用分断崖下跌）才中止。

账本在 `state/run_ledger.json`，只记时间、距离、时长、信用分、设备指纹，
不存 Token 与轨迹。`state/` 已在 `.gitignore` 中。删除该文件等于重置基线，
下一次运行会重新登记设备与信用分起点。服务端下发的黑名单原文会落到
`state/cheat_blacklist.json` 与 `state/barrier_free_policy.json`，便于部署时自查。

### 查询参数

- `--profile`: 查询个人档案
- `--rules`: 查询跑步规则与围栏
- `--checkpoints`: 查询官方点位（不上传）
- `--history`: 查询历史记录
- `--semester`: 查询学期进度

## 技术支持

- 完整文档: 见 `README.md`
- 打包说明: 见 `BUILD.md`

---

**免责声明**: 本工具仅供学习交流使用，请勿用于非法用途。使用本工具所产生的一切后果由使用者自行承担。
