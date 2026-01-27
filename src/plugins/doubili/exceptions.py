"""doubili 插件自定义异常"""


class DoubiliError(Exception):
    """基类异常"""


class VideoFetchError(DoubiliError):
    """视频获取失败"""


class VideoSizeExceededError(VideoFetchError):
    """视频大小超限"""

    def __init__(self, size_mb: float, max_size_mb: float):
        self.size_mb = size_mb
        self.max_size_mb = max_size_mb
        super().__init__(f"视频大小超过{max_size_mb:.1f}MB，无法下载")


class VideoDurationExceededError(VideoFetchError):
    """视频时长超限"""

    def __init__(self, duration_sec: int, max_duration_sec: int):
        self.duration_sec = duration_sec
        self.max_duration_sec = max_duration_sec
        super().__init__(f"视频时长超过{max_duration_sec / 60:.1f}分钟，无法下载")


class APIError(DoubiliError):
    """API 请求错误"""

    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message)


class ParseError(DoubiliError):
    """解析错误"""
