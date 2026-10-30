# 打包发布清单

## 打包前准备

1. 确保所有代码已提交到 Git（如使用版本控制）
2. 更新 README.md 中的版本号
3. **跑冒烟检查，必须 5/5 通过**：

   ```bash
   python smoke.py
   ```

   它会验证源文件没被截断、关键符号还在、59 个模块都能导入、343 个用例全绿、
   `config.ini` 没被写坏。任何一项不过都不要打包 —— 打出来也是坏包。详见 [CI.md](CI.md)。
4. 测试所有功能正常工作

## 打包步骤

### 1. 安装依赖

```bash
pip install pyinstaller
```

### 2. 执行打包

```bash
python build.py
```

### 3. 测试打包结果

```bash
cd dist/sport_campus_7.3.92

# Windows
sport_cli.exe --help
sport_webui.exe

# Linux
./sport_cli --help
./sport_webui
```

## 发行版文件清单

打包完成后，发行版应包含以下文件：

### Windows 版本

```
sport_campus_7.3.92_win64/
├── sport_cli.exe          # CLI 命令行版本 ✅
├── sport_webui.exe        # Web UI 版本 ✅
├── config.ini.example     # 配置模板 ✅
├── webui.html             # Web UI 前端页面 ✅
├── _internal/             # 依赖库目录（自动生成）✅
├── USAGE.md               # 用户使用说明 ⭐ 需手动复制
├── 启动.bat               # Windows 快速启动脚本 ⭐ 需手动复制
└── README.txt             # 简要说明（可选）
```

### Linux 版本

```
sport_campus_7.3.92_linux64/
├── sport_cli              # CLI 命令行版本 ✅
├── sport_webui            # Web UI 版本 ✅
├── config.ini.example     # 配置模板 ✅
├── webui.html             # Web UI 前端页面 ✅
├── _internal/             # 依赖库目录（自动生成）✅
├── USAGE.md               # 用户使用说明 ⭐ 需手动复制
├── 启动.sh                # Linux 快速启动脚本 ⭐ 需手动复制
└── README.txt             # 简要说明（可选）
```

## 手动复制文件

打包脚本只会自动打包可执行文件和 `config.ini.example`，以下文件需要手动复制到 `dist/sport_campus_7.3.92/` 目录：

```bash
# Windows
copy USAGE.md dist\sport_campus_7.3.92\
copy 启动.bat dist\sport_campus_7.3.92\

# Linux
cp USAGE.md dist/sport_campus_7.3.92/
cp 启动.sh dist/sport_campus_7.3.92/
chmod +x dist/sport_campus_7.3.92/启动.sh
```

## 创建压缩包

### Windows

```bash
cd dist
7z a sport_campus_7.3.92_win64.zip sport_campus_7.3.92/
```

或使用 Windows 资源管理器右键 → 发送到 → 压缩文件夹

### Linux

```bash
cd dist
tar czf sport_campus_7.3.92_linux64.tar.gz sport_campus_7.3.92/
```

## 发布检查清单

发布前请确认：

- [ ] 所有可执行文件正常运行
- [ ] `config.ini.example` 包含所有配置项
- [ ] `USAGE.md` 说明清晰完整
- [ ] 快速启动脚本可用
- [ ] 压缩包命名规范（包含版本号和平台信息）
- [ ] 测试过在全新环境下解压运行

## 版本命名规范

```
sport_campus_{版本号}_{平台}{架构}.{压缩格式}

示例:
sport_campus_7.3.92_win64.zip
sport_campus_7.3.92_linux64.tar.gz
sport_campus_7.3.92_macos_arm64.dmg
```

## 发布说明模板

```markdown
# 运动世界校园 7.3.92 协议套件

## 版本: 7.3.92
## 发布日期: YYYY-MM-DD

### 特性

- ✅ 完整协议实现（登录、跑步、查询）
- ✅ 拟真增强（配速波动、生理字段、物理时间）
- ✅ 灵活配置（INI + 环境变量 + 默认值）
- ✅ 双模式（CLI + Web UI）
- ✅ 跨平台（Windows / Linux）

### 下载

- [Windows x64](sport_campus_7.3.92_win64.zip)
- [Linux x64](sport_campus_7.3.92_linux64.tar.gz)

### 使用方法

见压缩包内的 `USAGE.md` 文件。

### 免责声明

本工具仅供学习交流使用，请勿用于非法用途。
```

## 技术细节

### 打包配置

打包配置文件: `build.spec`

关键配置项:
- `datas`: 需要打包的数据文件
- `hiddenimports`: 动态导入的模块
- `excludes`: 排除的模块
- `upx`: UPX 压缩（减小体积）

### 文件大小参考

- CLI 版本: ~20-30 MB
- Web UI 版本: ~25-35 MB
- 完整目录: ~40-50 MB
- 压缩后: ~15-25 MB

### 依赖库

主要依赖:
- requests
- pycryptodome
- flask (仅 Web UI)
- jinja2 (仅 Web UI)

所有依赖已打包到 `_internal/` 目录，用户无需安装。

## 故障排查

### 打包失败

1. 检查 PyInstaller 版本: `pip install --upgrade pyinstaller`
2. 清理缓存: `rm -rf build dist __pycache__`
3. 检查 `build.spec` 语法
4. 查看详细错误日志

### 运行时缺少模块

添加到 `build.spec` 的 `hiddenimports` 列表：

```python
hiddenimports=[
    '缺失的模块名',
]
```

### 文件过大

1. 检查是否启用 UPX 压缩
2. 排除不必要的模块: `excludes=['tkinter', 'matplotlib', ...]`
3. 使用单目录模式而非单文件模式

## 自动化脚本

可以创建一个自动化脚本完成整个发布流程：

```bash
#!/bin/bash
# release.sh - 自动化发布脚本

VERSION="7.3.92"
PLATFORM="linux64"

# 1. 清理
rm -rf build dist

# 2. 打包
python build.py

# 3. 复制文档
cp USAGE.md dist/sport_campus_7.3.92/
cp 启动.sh dist/sport_campus_7.3.92/
chmod +x dist/sport_campus_7.3.92/启动.sh

# 4. 创建压缩包
cd dist
tar czf sport_campus_${VERSION}_${PLATFORM}.tar.gz sport_campus_7.3.92/

echo "发布包创建完成: sport_campus_${VERSION}_${PLATFORM}.tar.gz"
```

保存为 `release.sh`，然后运行: `bash release.sh`
