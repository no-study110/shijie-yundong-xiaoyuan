# 运动世界校园 7.3.92 协议套件

纯 Python 实现的运动世界校园 7.3.92 协议客户端，支持跑步提交、拟真增强、Web 控制台等功能。

## 来源与声明

- **协议出处**：全部接口路径、加密方式（易盾 NetSecKit AES/Feistel/RSA）、报文字段，
  均来自对「运动世界校园」**Android 客户端 7.3.92 版本** 的抓包与逆向分析；
  单位口径（如 `speed` 字段 = 配速 × 1000、规则 `speedTop/speedBottom` = 分/公里）
  由真实接口返回数据实测校准。
- **参考资料**：反编译与接口分析参考了开源仓库
  [Inverx/com.zjwh.android_wh_physicalfitness](https://github.com/Inverx/com.zjwh.android_wh_physicalfitness)
  （「运动世界校园」`com.zjwh.android_wh_physicalfitness` 的逆向资料），感谢原作者的贡献。
- **独立重实现**：本仓库为协议学习性质的独立重写，**不包含任何官方 APK 的代码、
  资源或二进制**；与「运动世界校园」官方及开发商无任何关联。
- **用途限制**：仅供网络协议学习与安全研究。请遵守所在学校规定与相关法律法规，
  因使用本代码产生的一切后果由使用者自行承担。
- **侵权处理**：若内容涉及权利方权益，请提 Issue 或联系仓库所有者，会及时删除。

## 特性

- ✅ **完整协议实现**：登录认证、规则查询、围栏获取、轨迹生成、跑步提交、历史记录
- ✅ **拟真增强**：配速波动、生理字段（心率/步频/触地时间/垂直振幅/VO2max）、分桶抖动、物理时间模拟
- ✅ **灵活配置**：INI 配置文件 + 环境变量 + 代码默认值三级回退
- ✅ **命令行工具**：支持批量测试、自定义设备指纹、跳过物理时间
- ✅ **Web 控制台**：本地网页界面，可视化操作
- ✅ **手机 App**：可打包成 Android APK（Python 后端 + WebView，与桌面端同一套代码）
- ✅ **可移植性强**：统一配置系统，无硬编码值，支持多环境部署

## 快速开始

### 方式一：直接使用打包版本（推荐）

**无需安装 Python 环境**，下载打包版本直接运行：

1. 下载对应平台的打包版本（`sport_campus_7.3.92.zip`）
2. 解压到任意目录
3. 复制 `config.ini.example` 为 `config.ini`（可选，不复制则使用默认配置）
4. 运行可执行文件：
   - **Windows**: 双击 `sport_cli.exe` 或 `sport_webui.exe`
   - **Linux**: `./sport_cli` 或 `./sport_webui`

### 方式二：从源码运行

需要 Python 3.8+ 环境。

#### 1. 安装依赖

```bash
pip install -r requirements.txt
```

#### 2. 配置（可选）

复制配置模板并按需修改：

```bash
cp config.ini.example config.ini
```

配置文件说明见下方「配置系统」章节。不创建 `config.ini` 时自动使用内置默认值。

### 3. 命令行使用

#### 登录

```bash
python cli.py -u 手机号 -p 密码
```

成功后 Token 自动缓存到 `.session.json`，后续无需重复登录。

#### 跑步提交（标准拟真模式）

```bash
python cli.py --run --mode 1 --distance 2150 --time-sec 760
```

**默认启用物理时间模拟**：
- 跑前准备：1-3 分钟（查看规则、GPS 定位）
- 跑步进行中：按 `--time-sec` 指定时长（模拟真实跑步）
- 跑后上传：10-30 秒

完整流程耗时 ≈ 3 + 760 + 20 = 783 秒（约 13 分钟），避免「几秒上传」被风控拦截。

#### 快速测试模式

```bash
python cli.py --run --no-physical-time --distance 2000 --time-sec 700
```

跳过物理时间模拟，仅保留网络延迟，适用于批量测试或验证逻辑。

#### 禁用拟真增强

```bash
python cli.py --run --no-realism --distance 2000 --time-sec 700
```

使用纯均分配速和固定步频，不生成生理字段波动，适用于特殊测试场景。

#### 查询规则与历史

```bash
# 查询个人档案
python cli.py --profile

# 查询跑步规则与围栏
python cli.py --rules

# 查询历史记录
python cli.py --history

# 查询学期进度
python cli.py --semester

# 查询官方计分跑点位（不上传）
python cli.py --checkpoints
```

#### 设备指纹复用（规避 10121 错误）

```bash
# 方式一：命令行指定
python cli.py -u 手机号 -p 密码 --device-id 你的32位DeviceId

# 方式二：写入 config.ini
[device]
device_id = 你的32位DeviceId
```

### 4. Web 控制台

```bash
python webui.py
```

自动打开浏览器访问 http://127.0.0.1:9999，支持可视化登录、跑步提交、规则查询、历史记录等操作。

## 配置系统

项目采用**三级配置回退**机制，优先级从高到低：

1. **环境变量**（仅部分字段支持，如 `HOST_RUN`）
2. **INI 配置文件**（`config.ini`）
3. **代码默认值**（`config_loader.py` 硬编码）

### 配置文件示例

`config.ini.example` 是完整配置模板，包含所有可配置项及注释。主要配置段：

#### `[server]` 服务端网关

```ini
[server]
host_run = https://run.gxapp.iydsj.com
host_discovery = https://discovery.gxapp.iydsj.com
host_datapoint = https://datapoint.gxapp.iydsj.com
host_partner = https://partner.iydsj.com
```

支持自建中转代理或第三方镜像。

#### `[device]` 设备指纹

```ini
[device]
app_version = 7.3.92
physic_pixel = 1080x2400
logic_pixel = 393x873
device_name = Android Device
device_id =
os_version = 13
user_agent = okhttp/4.12.0
```

- `device_id` 留空则每次自动生成随机 UUID（新设备）
- 填写 32 位 16 进制可复用原手机设备指纹（避免 10121 错误）

#### `[realism]` 拟真增强

```ini
[realism]
enabled = True
simulate_physical_time = True
```

- `enabled`：启用配速波动、生理字段、分桶抖动
- `simulate_physical_time`：启用物理时间模拟（跑前准备 + 跑步时长 + 跑后上传）

#### `[webui]` Web 控制台

```ini
[webui]
host = 127.0.0.1
port = 9999
```

#### `[proxy]` 代理设置

```ini
[proxy]
proxy_url = http://127.0.0.1:8080
```

支持 HTTP/HTTPS/SOCKS5 代理，留空则直连。

#### `[captcha]` Node.js 环境

```ini
[captcha]
node_executable = C:\Program Files\nodejs\node.exe
node_path = C:\nodejs\node_modules
playwright_path = C:\nodejs\node_modules
```

滑块验证码由 Node + Playwright 求解，三项都留空则自动探测：

- 探测顺序：环境变量 → 本段配置 → `npm root -g` → Node 安装布局 → 常见位置
- 登录报「未找到 playwright 模块」时，报错里会列出**已经尝试过哪些路径**，照着填 `playwright_path` 即可
- 想让项目自带一份依赖：在项目根目录执行 `npm install playwright`

`node_path` 是模块搜索路径（等价于 `NODE_PATH`），多个目录用 `;` 分隔（Linux/macOS 用 `:`）。

#### `[compliance]` 合规检查

```ini
[compliance]
checkpoint_tolerance_m = 15.0
```

围栏与打卡点自检容差（米）。

### 环境变量覆盖

支持通过环境变量覆盖部分配置（优先级高于 `config.ini`）：

```bash
export HOST_RUN=https://your-mirror.com
export HOST_DISCOVERY=https://your-discovery.com
python cli.py --run
```

完整支持的环境变量见 `core/config.py`。

## 文件结构

```
├── cli.py                   # 命令行入口
├── webui.py                 # Web 控制台（需 flask）
├── sport_client.py          # 协议客户端封装（门面）
├── config_loader.py         # 统一配置加载器（唯一读 config.ini 的地方）
├── device_id_tool.py        # DeviceId 生成 / 查看 / 校验工具
├── config.ini.example       # 配置模板（复制后修改）
├── smoke.py                 # 提交前冒烟检查（静态/契约/导入/测试/配置）
├── check.cmd / check.sh     # 冒烟一键入口
├── test_*.py                # 离线单元测试（343 个用例）
├── build.py / build.spec    # PyInstaller 打包
├── core/
│   ├── config.py            # 把 config_loader 的取值固化为模块常量
│   ├── crypto.py            # 易盾 NetSecKit：AES-128-CBC / Feistel KDF / RSA 封装
│   ├── client.py            # 基础 HTTP 客户端（设备指纹、签名注入）
│   ├── pace.py              # 配速唯一口径（分/公里 ⇄ 千分位 ⇄ m/s，8′54″/km 格式）
│   ├── retry.py             # 重试、退避与错误分类
│   ├── ledger.py            # 本地运行账本（设备/频次/信用分纵向记录）
│   └── session.py           # 会话凭据持久化（token / uid / unid）
├── login/
│   ├── client.py            # 登录认证
│   ├── captcha.py           # 极验 v4 滑块（Node + Playwright）
│   ├── crypto.py            # 登录侧加密
│   └── models.py            # 登录结果模型
├── running/
│   ├── client.py            # 跑步提交流程编排
│   ├── builder.py           # 轨迹构建 + 31 字段结算报文组装
│   ├── planner.py           # 围栏内路线规划（最近邻串联打卡点）
│   ├── geometry.py          # 校园尺度平面几何（点多边形、内缩）
│   ├── consistency.py       # 采样、扰动、长度归一与总计自洽
│   ├── kinematics.py        # 运动学分桶与信号一致性校验
│   ├── compliance.py        # 围栏与打卡点合规校验
│   ├── realism.py           # 配速曲线、生理画像、桶抖动
│   ├── riskguard.py         # 跑前风控预检（设备/频次/信用分/时段）
│   └── models.py            # GpsPoint / FivePoint / 分桶模型
├── policy/
│   ├── client.py            # 规则与围栏查询
│   └── models.py            # 规则、围栏、打卡点模型
├── account/ · history/ · ads/ · telemetry/
│   ├── client.py            # 档案 / 历史 / 广告 / 埋点
│   └── models.py            # 各自的数据模型
├── android/                 # 打包成手机 App（Chaquopy + WebView，见 android/README.md）
│   ├── build_apk.py         # 一键构建：同步源码 → 调 Gradle → 输出 dist/*.apk
│   ├── sync_python.py       # 把业务源码同步进 APK 的 python 源目录（增量）
│   └── app/                 # Android 工程（MainActivity 起 Python，再加载网页控制台）
├── android_entry.py         # Android 启动入口：设数据目录 → 起 Flask（桌面端不用）
└── examples/proxy_example.py # 代理配置示例
```

## 自检与测试

改动代码后先跑冒烟，约 2.5 秒，能拦住「文件被改坏 / 模块导入不了」这类事故：

```bash
python smoke.py              # 静态完整性 + 关键契约 + 模块导入 + 单元测试 + 配置健康
python smoke.py --skip-tests # 快速档，0.5 秒，pre-commit 用
```

退出码 0 表示全通过。Windows 可直接双击 `check.cmd`，Linux/macOS 用 `./check.sh`。

单元测试全部离线、不触网，也可以单独跑全量：

```bash
python -m unittest discover -p "test_*.py"
```

详见 [CI.md](CI.md)。

## 打包发布

### 自行打包

需要安装 PyInstaller：

```bash
pip install pyinstaller
```

然后运行打包脚本：

```bash
python build.py
```

打包完成后，输出目录为 `dist/sport_campus_7.3.92/`，包含以下文件：

- `sport_cli.exe` / `sport_cli` — 命令行版本
- `sport_webui.exe` / `sport_webui` — Web UI 版本
- `config.ini.example` — 配置模板

将整个 `dist/sport_campus_7.3.92/` 目录打包为 ZIP 即可分发。

### 打包特性

- ✅ **单目录模式**：所有依赖打包到一个文件夹，便于分发
- ✅ **跨平台支持**：Windows 打包为 `.exe`，Linux 打包为可执行文件
- ✅ **配置文件独立**：`config.ini` 不打包，用户可自定义
- ✅ **UPX 压缩**：减小可执行文件体积

## 反风控增强

### 1. 拟真增强（`realism.py`）

- **配速波动**：7-11 段非均匀分段，每段速度在目标配速 ±12% 范围波动
- **生理字段**：
  - 心率：52-203 bpm，随配速动态调整
  - 步频：155-195 步/分
  - 触地时间：205-310 ms
  - 垂直振幅：4.8-8.5 cm
  - VO2max：38-60 ml/kg/min
- **分桶抖动**：每个轨迹点步数/距离在目标值 8%-40% 范围随机抖动

### 2. 物理时间模拟（`running/client.py`）

- **跑前准备**：1-3 分钟（模拟查看规则、GPS 定位、准备活动）
- **跑步进行中**：按 `target_time_sec` 指定时长，分段进度打印
- **跑后上传**：10-30 秒（模拟保存数据、等待网络）

避免「几秒完成跑步」被服务端识别为异常行为。

### 3. 配置灵活性

- 快速测试时可通过 `--no-physical-time` 跳过时间模拟
- 可通过 `--no-realism` 禁用拟真增强
- 批量测试时建议关闭物理时间，单次真实提交时建议开启

## 常见问题

### 1. 登录报错 10121「账号已在其他设备登录」

**原因**：服务端检测到 `DeviceId` 变化，认为是新设备登录。

**解决方案**：
1. 在手机 App 内「退出登录」后重试
2. 或获取原手机的 `DeviceId`（32 位 16 进制），通过 `--device-id` 或 `config.ini` 指定

**DeviceId 的生成与校验**统一走工具，不要手写：

```bash
python device_id_tool.py            # 生成一个随机值（只打印，不写入）
python device_id_tool.py --show     # 看当前生效值和它的来源
python device_id_tool.py --check    # 校验当前值格式（不合法退出码 1）
python device_id_tool.py --write    # 生成并写入 config.ini
python device_id_tool.py --set <值> # 校验后写入指定值
```

不想开终端的话，网页控制台的「账号」卡片里也有同一个入口：点**随机生成**，新指纹会填进 DeviceId 输入框；**登录成功后**才真正生效。生成这个动作本身不写任何配置——点了不用、或者又改成别的值，都不会留下一个"账号已换设备"的坑。

有效格式是 **32 位小写十六进制**。带连字符的 UUID 会自动去掉横线；但**大小写不会被自动修正**——服务端按字符比对指纹，大小写不同就是两台设备，所以抄原机值时必须逐字符一致。真机抓到的值如果不符合这个格式，可加 `--force` 跳过校验。

> `config.ini` 里留空同样是合法的：首次运行会自动生成并回填，之后一直复用。手抄错一位反而更糟——它会被写回配置文件长期生效。

### 2. 跑步提交秒完成会被风控吗？

**会**。服务端会检测时间戳合理性，建议：
- 正常提交时**启用物理时间模拟**（默认开启）
- 快速测试时可用 `--no-physical-time` 跳过

### 3. 如何在多台服务器部署？

项目已实现配置与代码分离：
1. 复制整个项目目录
2. 根据环境修改 `config.ini`（服务器地址、设备指纹、代理等）
3. 各环境独立运行，互不干扰

### 4. 为什么要三级配置回退？

- **代码默认值**：开箱即用，无需配置
- **INI 文件**：便于版本控制、批量部署、环境隔离
- **环境变量**：适用于容器化部署、CI/CD 流水线

### 5. 配置文件会被 Git 提交吗？

不会。`.gitignore` 已排除 `config.ini`、`.session.json`、`.webui_config.json` 等敏感文件。

## 许可证

MIT License

---

**免责声明**：本项目仅供学习交流使用,请勿用于非法用途。使用本项目所产生的一切后果由使用者自行承担。
