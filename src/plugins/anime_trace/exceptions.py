"""anime_trace 插件自定义异常"""


class AnimeTraceError(Exception):
    """基类异常"""


class ImageDownloadError(AnimeTraceError):
    """图片下载失败"""


class APIRequestError(AnimeTraceError):
    """API 请求错误"""


class APIResponseError(AnimeTraceError):
    """API 响应错误"""
