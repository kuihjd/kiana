from typing import Literal

from pydantic import BaseModel, Field


class Config(BaseModel):
    message_archive_plugin_enabled: bool = Field(
        default=True,
        description="是否启用消息归档",
    )

    message_archive_group_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="群组控制模式: all(全部群启用) | whitelist(仅白名单群) | blacklist(黑名单外的群)",
    )
    message_archive_group_whitelist: list[str] = Field(
        default=[],
        description="白名单群组(仅在 whitelist 模式生效)",
    )
    message_archive_group_blacklist: list[str] = Field(
        default=[],
        description="黑名单群组(仅在 blacklist 模式生效)",
    )

    message_archive_image_enabled: bool = Field(
        default=True,
        description="是否将收到的图片持久化到本地以支持历史回放",
    )
    message_archive_image_retention_days: int = Field(
        default=7,
        description="本地图片保留天数，超期自动清理",
    )
    message_archive_image_max_size_mb: int = Field(
        default=10,
        description="单张图片大小上限(MB)，超限跳过持久化",
    )
    message_archive_image_max_count: int = Field(
        default=10,
        description="单条消息最多持久化的图片数量",
    )
