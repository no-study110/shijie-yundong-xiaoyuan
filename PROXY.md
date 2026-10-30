# 代理配置说明

## 概述

协议套件已集成代理支持，可通过 HTTP/HTTPS/SOCKS5 代理服务器访问运动世界校园服务端，有效避免因 IP 地址重复使用引起的风控检测。

## 使用方式

### 1. Web UI 配置（推荐）

启动 `webui.py` 后，访问 `http://127.0.0.1:9999`，在设置页面配置代理地址：

- **HTTP 代理**：`http://代理服务器地址:端口`
- **HTTPS 代理**：`https://代理服务器地址:端口`
- **SOCKS5 代理**：`socks5://代理服务器地址:端口`（需安装 `PySocks`）

配置示例：
```
http://127.0.0.1:7890
socks5://127.0.0.1:1080
```

保存后重新启动任务即生效。配置持久化在 `.webui_config.json` 中。

### 2. 代码调用

```python
from sport_client import SportClient792

# 创建客户端时指定代理
client = SportClient792(
    uid="your_uid",
    token="your_token",
    unid="your_unid",
    proxy="http://127.0.0.1:7890"  # 或 socks5://127.0.0.1:1080
)

# 正常使用
result = client.execute_run(
    target_distance_m=2100,
    target_time_sec=760,
    sport_type=1,
    realism=True
)
```

### 3. API 配置

**获取当前代理配置：**
```bash
curl http://127.0.0.1:9999/api/config
```

**设置代理：**
```bash
curl -X POST http://127.0.0.1:9999/api/config \
  -H "Content-Type: application/json" \
  -d '{"proxy": "http://127.0.0.1:7890"}'
```

**清除代理：**
```bash
curl -X POST http://127.0.0.1:9999/api/config \
  -H "Content-Type: application/json" \
  -d '{"proxy": ""}'
```

## 常见代理服务

### Clash

默认 HTTP 代理端口：`7890`
```
http://127.0.0.1:7890
```

### V2Ray / V2RayN

默认 SOCKS5 代理端口：`10808`
```
socks5://127.0.0.1:10808
```

### Shadowsocks

默认 SOCKS5 代理端口：`1080`
```
socks5://127.0.0.1:1080
```

### Privoxy（HTTP 代理）

默认端口：`8118`
```
http://127.0.0.1:8118
```

## 注意事项

1. **SOCKS5 代理**需要安装 `PySocks` 库：
   ```bash
   pip install PySocks
   ```

2. **代理延迟**：使用代理会增加网络延迟，建议使用低延迟代理服务器

3. **代理稳定性**：确保代理服务稳定运行，避免中途断开导致请求失败

4. **代理认证**：当前版本仅支持无认证代理，如需认证代理支持请联系开发者

5. **IP 轮换**：配合支持 IP 轮换的代理服务，可进一步降低风控风险

## 故障排查

### 代理无法连接

1. 检查代理服务是否正常运行
2. 确认代理地址和端口是否正确
3. 检查防火墙是否拦截连接

### 请求超时

1. 增加 `timeout` 参数（默认 20 秒）
2. 更换延迟更低的代理服务器
3. 检查代理服务器带宽是否充足

### SOCKS5 代理报错

确保已安装 `PySocks`：
```bash
pip install PySocks
```

## 推荐配置

- **低频使用**（每天 1-2 次）：无需代理，直接连接即可
- **中频使用**（每天 3-10 次）：建议配置 HTTP 代理
- **高频使用**（每天 10+ 次）：建议配置支持 IP 轮换的代理池

## 技术实现

- 登录客户端（`LoginClient`）和所有继承 `BaseNetSecClient` 的客户端均支持代理
- 代理通过 `urllib.request.ProxyHandler` 实现
- 自动适配 HTTP/HTTPS 协议
- SOCKS5 代理需要 `PySocks` 库支持

## 更新日志

**v1.0.0** (2024-10-27)
- 初始代理支持
- Web UI 配置界面
- HTTP/HTTPS/SOCKS5 协议支持
