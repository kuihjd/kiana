"""test_image_store 专用辅助。集中放测试用伪数据生成。"""


def make_image_bytes(size: int) -> bytes:
    """生成固定 size 的伪图片字节（非真实图片格式，仅测试用）。"""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * max(0, size - 8)
