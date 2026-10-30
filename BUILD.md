# 打包说明

## 环境要求

- Python 3.8+
- PyInstaller 5.0+

## 安装 PyInstaller

```bash
pip install pyinstaller
```

## 打包步骤

### 1. 自动打包（推荐）

运行打包脚本：

```bash
python build.py
```

脚本会自动：
1. 检查 PyInstaller 是否安装
2. 清理旧的构建文件
3. 执行打包
4. 显示打包结果和使用说明

### 2. 手动打包

如果需要手动控制打包过程：

```bash
# 清理旧构建
rm -rf build dist

# 执行打包
pyinstaller build.spec --clean
```

## 打包输出

打包完成后，输出目录为 `dist/sport_campus_7.3.92/`，包含：

```
sport_campus_7.3.92/
├── sport_cli.exe        # CLI 命令行版本（Windows）
├── sport_webui.exe      # Web UI 版本（Windows）
├── config.ini.example   # 配置模板
└── _internal/           # 依赖库（自动生成，不要修改）
```

**Linux 平台**生成的是无后缀可执行文件：`sport_cli`、`sport_webui`。

## 打包大小

- **CLI 版本**: 约 20-30 MB
- **Web UI 版本**: 约 25-35 MB
- **总目录大小**: 约 40-50 MB

已启用 UPX 压缩，进一步减小体积。

## 分发打包

### 创建发行版

```bash
# Windows
cd dist
7z a sport_campus_7.3.92_win64.zip sport_campus_7.3.92/

# Linux
cd dist
tar czf sport_campus_7.3.92_linux64.tar.gz sport_campus_7.3.92/
```

### 发行版内容

建议在发行版中包含：

1. **可执行文件目录** (`sport_campus_7.3.92/`)
2. **配置模板** (`config.ini.example`)
3. **使用说明** (README.md 或单独的 USAGE.txt)

## 用户使用

用户无需安装 Python 环境，直接使用：

### Windows

```cmd
# 解压
unzip sport_campus_7.3.92_win64.zip
cd sport_campus_7.3.92

# 复制配置模板（可选）
copy config.ini.example config.ini

# 运行 CLI 版本
sport_cli.exe --help
sport_cli.exe -u 手机号 -p 密码
sport_cli.exe --run --distance 2150 --time-sec 760

# 运行 Web UI 版本
sport_webui.exe
# 然后访问 http://127.0.0.1:9999
```

### Linux

```bash
# 解压
tar xzf sport_campus_7.3.92_linux64.tar.gz
cd sport_campus_7.3.92

# 添加执行权限
chmod +x sport_cli sport_webui

# 复制配置模板（可选）
cp config.ini.example config.ini

# 运行 CLI 版本
./sport_cli --help
./sport_cli -u 手机号 -p 密码
./sport_cli --run --distance 2150 --time-sec 760

# 运行 Web UI 版本
./sport_webui
# 然后访问 http://127.0.0.1:9999
```

## 常见问题

### 1. Windows Defender 拦截

部分杀毒软件可能将打包的 `.exe` 误报为病毒。这是 PyInstaller 打包程序的常见问题，**不是真正的病毒**。

**解决方案**：
- 添加信任/白名单
- 或使用源码运行方式

### 2. Linux 执行权限

Linux 下需要手动添加执行权限：

```bash
chmod +x sport_cli sport_webui
```

### 3. 打包后体积较大

PyInstaller 会将所有依赖打包进去，这是正常现象。已启用 UPX 压缩减小体积。

### 4. macOS 打包

macOS 打包流程与 Linux 类似，但生成的是 `.app` 应用包或可执行文件。注意：
- 需要在 macOS 系统上打包
- 可能需要签名才能在其他 Mac 上运行

## 高级配置

### 修改打包配置

编辑 `build.spec` 文件可以自定义：

- 图标（`icon=`）
- 隐藏导入模块（`hiddenimports=`）
- 数据文件（`datas=`）
- 排除模块（`excludes=`）

### 单文件打包

如果希望打包成单个 `.exe` 文件（不推荐，启动较慢）：

修改 `build.spec`，将 `EXE()` 的 `exclude_binaries=True` 改为 `False`，并移除 `COLLECT()` 部分。

## 技术细节

### 打包原理

PyInstaller 打包流程：

1. **分析依赖**: 扫描 Python 代码，识别所有导入的模块
2. **收集文件**: 收集 Python 解释器、依赖库、资源文件
3. **生成引导器**: 创建可执行文件引导器
4. **打包压缩**: 将所有文件打包到一个目录或单个文件

### 隐藏导入

某些动态导入的模块需要手动声明，已在 `build.spec` 中配置：

```python
hiddenimports=[
    'core.config',
    'config_loader',
    'sport_client',
    # ... 其他模块
]
```

如果运行时提示模块缺失，需要添加到 `hiddenimports` 列表。

### 数据文件

以下文件会被打包到可执行文件旁边：

```python
datas=[
    ('config.ini.example', '.'),
    ('webui.html', '.'),
]
```

运行时这些文件可以通过相对路径访问。

## 参考资料

- [PyInstaller 官方文档](https://pyinstaller.org/)
- [PyInstaller GitHub](https://github.com/pyinstaller/pyinstaller)
- [UPX 压缩工具](https://upx.github.io/)
