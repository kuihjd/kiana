from pydantic import BaseModel, Field


class Config(BaseModel):
    gotify_plugin_enabled: bool = Field(default=False, description="是否启用 Gotify 转发插件")
    gotify_url: str = Field(default="", description="Gotify 服务器地址")
    gotify_client_token: str = Field(default="", description="Gotify Client Token")
    gotify_forward_users: list[str] = Field(default=[], description="转发目标 QQ 用户 ID 列表")
    gotify_forward_groups: list[str] = Field(default=[], description="转发目标 QQ 群号列表")
    gotify_reconnect_interval: int = Field(default=5, description="重连间隔（秒）")
