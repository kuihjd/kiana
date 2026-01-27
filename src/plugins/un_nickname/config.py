from pydantic import BaseModel, Field


class Config(BaseModel):
    max_nickname_length: int = 15  # 最大昵称长度限制
    max_collection_name_length: int = 15  # 集合名最大长度
    max_collection_members: int = 50  # 单个集合最大成员数
    cache_ttl: int = Field(default=300, description="缓存有效期（秒）")
    empty_cache_ttl: int = Field(default=30, description="空结果缓存有效期（秒）")
