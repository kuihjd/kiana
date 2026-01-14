# doubili

视频解析插件。支持自动解析 B站、抖音、小红书链接，下载并发送视频或图片内容。

## 功能特性

- **B站解析**：支持 BV号、AV号、短链接（b23.tv）、分享卡片
- **抖音解析**：支持抖音分享链接、短链接
- **小红书解析**：支持笔记链接、短链接（xhslink）、分享卡片
- 自动识别链接，无需命令触发
- 支持腾讯卡片消息解析
- 小红书图片并发下载优化
- 支持各平台独立的分群开关控制

## 支持的链接格式

### B站

- 完整链接：`https://www.bilibili.com/video/BV1xxxxxxxxx`
- 短链接：`https://b23.tv/xxxxxx`
- AV号：`av170001`
- BV号：`BV1xxxxxxxxx`
- 分享卡片消息

### 抖音

- 分享链接：`https://v.douyin.com/xxxxxx`
- 完整链接：`https://www.douyin.com/video/xxxxxx`

### 小红书

- 笔记链接：`https://www.xiaohongshu.com/explore/xxxxx`
- 短链接：`https://xhslink.com/xxxxx`
- 分享卡片消息

## 使用示例

### B站视频

```
用户: https://www.bilibili.com/video/BV1xx411c7mD
Bot: [视频]
```

### 抖音视频

```
用户: https://v.douyin.com/iRxxxxxx/
Bot: 视频标题
Bot: [视频]
```

### 小红书笔记

```
用户: https://www.xiaohongshu.com/explore/xxxxx
Bot: [合并转发消息]
     ├── 标题
     │   作者: xxx
     ├── [图片1]
     ├── [图片2]
     └── ...
```

## 配置项

在 `.env` 或 `.env.prod` 中配置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_bilibili` | bool | true | 是否启用B站解析 |
| `enable_douyin` | bool | true | 是否启用抖音解析 |
| `enable_xiaohongshu` | bool | true | 是否启用小红书解析 |
| `xiaohongshu_cookie` | str | "" | 小红书 Cookie（必填才能解析） |

### B站分群配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `bilibili_group_mode` | str | "all" | 群组控制模式：all/whitelist/blacklist |
| `bilibili_group_whitelist` | list | [] | 白名单群组 |
| `bilibili_group_blacklist` | list | [] | 黑名单群组 |

### 抖音分群配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `douyin_group_mode` | str | "all" | 群组控制模式：all/whitelist/blacklist |
| `douyin_group_whitelist` | list | [] | 白名单群组 |
| `douyin_group_blacklist` | list | [] | 黑名单群组 |

### 小红书分群配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `xiaohongshu_group_mode` | str | "all" | 群组控制模式：all/whitelist/blacklist |
| `xiaohongshu_group_whitelist` | list | [] | 白名单群组 |
| `xiaohongshu_group_blacklist` | list | [] | 黑名单群组 |

### 视频限制配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `MAX_VIDEO_SIZE` | int | 52428800 | 最大视频大小（字节），默认 50MB |
| `MAX_VIDEO_DURATION` | int | 300 | 最大视频时长（秒），默认 5 分钟 |
| `VIDEO_QUALITY` | int | 64 | B站视频清晰度参数 |

## 注意事项

- 小红书解析需要配置有效的 Cookie 才能使用
- 视频大小和时长有限制，超出限制的视频可能无法发送
- B站视频清晰度受限于免登录可访问的最高画质
- 部分平台可能因接口变化导致解析失败
- 私聊和群聊均支持解析
