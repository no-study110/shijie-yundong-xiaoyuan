# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 - 运动世界校园 7.3.92 协议套件"""

block_cipher = None

# CLI 版本打包
cli_analysis = Analysis(
    ['cli.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.ini.example', '.'),
    ],
    hiddenimports=[
        'core.config',
        'core.session',
        'core.crypto',
        'core.client',
        'core.retry',
        'config_loader',
        'sport_client',
        'login.auth',
        'login.captcha',
        'running.client',
        'running.realism',
        'running.route_generator',
        'running.models',
        'policy.client',
        'policy.models',
        'history.client',
        'history.models',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

cli_pyz = PYZ(cli_analysis.pure, cli_analysis.zipped_data, cipher=block_cipher)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name='sport_cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# Web UI 版本打包
webui_analysis = Analysis(
    ['webui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.ini.example', '.'),
        ('webui.html', '.'),
    ],
    hiddenimports=[
        'core.config',
        'core.session',
        'core.crypto',
        'core.client',
        'core.retry',
        'config_loader',
        'sport_client',
        'login.auth',
        'login.captcha',
        'running.client',
        'running.realism',
        'running.route_generator',
        'running.models',
        'policy.client',
        'policy.models',
        'history.client',
        'history.models',
        'flask',
        'flask.json',
        'jinja2',
        'werkzeug',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

webui_pyz = PYZ(webui_analysis.pure, webui_analysis.zipped_data, cipher=block_cipher)

webui_exe = EXE(
    webui_pyz,
    webui_analysis.scripts,
    [],
    exclude_binaries=True,
    name='sport_webui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# 收集所有依赖到一个目录
coll = COLLECT(
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.zipfiles,
    cli_analysis.datas,
    webui_exe,
    webui_analysis.binaries,
    webui_analysis.zipfiles,
    webui_analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='sport_campus_7.3.92',
)
