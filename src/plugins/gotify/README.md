# gotify

将 Gotify 自托管推送服务的消息实时转发到 QQ。通过 WebSocket 长连接订阅 Gotify 消息流，收到消息后自动转发给配置的 QQ 用户或群组。

## 功能特性

- WebSocket 长连接实时接收 Gotify 消息
- 支持同时转发到多个 QQ 用户和群组
- 断线自动重连，指数退避
- 启动时自动校验配置，缺少必要配置不启动

## 消息格式

```
📨 Gotify 通知
标题: xxx
消息内容
```

若消息无标题则只显示消息内容。

## 配置项

在 `.env` 或 `.env.prod` 中配置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `gotify_plugin_enabled` | bool | false | 是否启用 Gotify 转发插件 |
| `gotify_url` | str | "" | Gotify 服务器地址，如 `https://gotify.example.com` |
| `gotify_client_token` | str | "" | Gotify **Client** Token（用于接收消息） |
| `gotify_forward_users` | list | [] | 转发目标 QQ 用户 ID 列表 |
| `gotify_forward_groups` | list | [] | 转发目标 QQ 群号列表 |
| `gotify_reconnect_interval` | int | 5 | 基础重连间隔（秒），实际会指数退避 |

### 配置示例

```env
gotify_plugin_enabled=true
gotify_url=https://gotify.example.com
gotify_client_token=Cxxxxxxxxxx
gotify_forward_users=["123456"]
gotify_forward_groups=["789012"]
```

> **注意**：`gotify_client_token` 必须是 **Client Token**（CLIENTS 页面），不是 Application Token。
