# gotify

将 Gotify 自托管推送服务的消息实时转发到 QQ。通过 WebSocket 长连接订阅 Gotify 消息流，收到消息后自动转发给配置的 QQ 用户或群组。

## 功能特性

- WebSocket 长连接实时接收 Gotify 消息
- 支持同时转发到多个 QQ 用户和群组
- 断线自动重连，指数退避
- 启动时自动校验配置，缺少必要配置不启动
- `gotify列表` 命令查询所有应用信息（仅超级用户）

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
| `gotify_forward_users` | list | [] | **默认**转发目标 QQ 用户 ID 列表 |
| `gotify_forward_groups` | list | [] | **默认**转发目标 QQ 群号列表 |
| `gotify_app_rules` | list | [] | 按 appid 配置的转发规则 |
| `gotify_reconnect_interval` | int | 5 | 基础重连间隔（秒），实际会指数退避 |

### 配置示例

#### 示例 1：使用默认转发（向后兼容）

```env
gotify_plugin_enabled=true
gotify_url=https://gotify.example.com
gotify_client_token=Cxxxxxxxxxx
gotify_forward_users=["123456"]
gotify_forward_groups=["789012"]
```

#### 示例 2：按 appid 配置转发规则

```env
gotify_plugin_enabled=true
gotify_url=https://gotify.example.com
gotify_client_token=Cxxxxxxxxxx
gotify_app_rules=[
  {"app_id": "1", "forward_users": ["123456"], "forward_groups": []},
  {"app_id": "2", "forward_users": [], "forward_groups": ["789012"]},
  {"app_id": "3", "forward_users": ["111111", "222222"], "forward_groups": ["333333"]}
]
```

#### 示例 3：混合使用（默认 + 按 appid）

```env
gotify_plugin_enabled=true
gotify_url=https://gotify.example.com
gotify_client_token=Cxxxxxxxxxx
gotify_forward_users=["123456"]  # 默认转发目标
gotify_forward_groups=["789012"]
gotify_app_rules=[
  {"app_id": "1", "forward_users": ["111111"], "forward_groups": []}  # appid=1 的专用规则
]
```

> **注意**：
> - `gotify_client_token` 必须是 **Client Token**（CLIENTS 页面），不是 Application Token。
> - `gotify_app_rules` 优先级高于默认转发目标。
> - 如果某个 appid 没有匹配的规则，且配置了默认转发目标，则使用默认目标。
> - 如果既没有匹配规则也没有默认目标，消息将被丢弃（记录 debug 日志）。

## 命令

### `gotify列表`

查询 Gotify 服务器上的所有应用（id、名称、描述、最后活跃时间）。仅超级用户（`.env` 中 `SUPERUSERS`）可使用。

输出示例：

```
[Gotify 应用列表]
[1] MAA
    描述: (无)
    最后活跃: 2026-04-21 18:14
[2] SRC
    描述: (无)
    最后活跃: 从未活跃
```
