from pydantic import BaseModel, Field


class Config(BaseModel):
    """Plugin Config Here"""

    trace_moe_api_url: str = Field(
        default="https://api.trace.moe/search?anilistInfo&cutBorders",
        description="trace.moe API URL",
    )
    api_request_timeout: float = Field(
        default=30.0,
        description="API 请求超时时间（秒）",
    )
    download_timeout: float = Field(
        default=30.0,
        description="图片下载超时时间（秒）",
    )
