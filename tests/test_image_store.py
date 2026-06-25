import pytest


def test_extract_file_hash_from_md5_filename() -> None:
    """file 字段为 HASH.ext 时，取主名作 hash。"""
    from src.plugins.message_archive.image_store import extract_file_hash

    assert (
        extract_file_hash("CFCD5C99971951CE54DD1335C41BD6CC.jpg")
        == "cfcd5c99971951ce54dd1335c41bd6cc"
    )


def test_extract_file_hash_from_guid_filename() -> None:
    """file 字段为 {GUID}.png 时，去花括号取主名。"""
    from src.plugins.message_archive.image_store import extract_file_hash

    assert (
        extract_file_hash("{6622EC01-DDB1-B6BC-D9C8-9C695750A32E}.png")
        == "6622ec01-ddb1-b6bc-d9c8-9c695750a32e"
    )


def test_extract_file_hash_strips_query_and_fragments() -> None:
    """file 字段可能带杂乱内容，取首个路径段主名。"""
    from src.plugins.message_archive.image_store import extract_file_hash

    assert extract_file_hash("ABC123.png?foo=1") == "abc123"


def test_resolve_ext_from_url() -> None:
    """从 url 后缀推断扩展名。"""
    from src.plugins.message_archive.image_store import resolve_ext

    assert resolve_ext("https://example.com/a/b.png", "") == ".png"


def test_resolve_ext_from_content_type() -> None:
    """url 无后缀时，从 Content-Type 推断。"""
    from src.plugins.message_archive.image_store import resolve_ext

    assert resolve_ext("https://example.com/img", "image/jpeg") == ".jpg"


def test_resolve_ext_default_jpg() -> None:
    """都无法推断时兜底 .jpg。"""
    from src.plugins.message_archive.image_store import resolve_ext

    assert resolve_ext("https://example.com/img", "") == ".jpg"


def test_get_image_path_returns_none_when_absent(tmp_path, monkeypatch) -> None:
    """文件不存在时返回 None。"""
    from src.plugins.message_archive.image_store import get_image_path

    monkeypatch.setattr("src.plugins.message_archive.image_store.image_dir", tmp_path)
    assert get_image_path("deadbeef") is None


@pytest.mark.asyncio
async def test_store_image_downloads_and_persists(tmp_path, monkeypatch) -> None:
    """store_image 下载图片并落盘到散列目录。"""
    from src.plugins.message_archive import image_store
    from tests.message_store_helpers import make_image_bytes

    monkeypatch.setattr(image_store, "image_dir", tmp_path)

    async def _fake_fetch(url: object) -> bytes:
        return make_image_bytes(8)

    monkeypatch.setattr(image_store, "_fetch_bytes", _fake_fetch)

    path = await image_store.store_image(
        url="https://example.com/a.png",
        file_hash="abcdef0123456789",
        max_size_bytes=1024,
    )
    assert path is not None
    assert path.read_bytes() == make_image_bytes(8)
    assert path.parent.name == "ab"
    assert path.name == "abcdef0123456789.png"


@pytest.mark.asyncio
async def test_store_image_dedup_skips_existing(tmp_path, monkeypatch) -> None:
    """同一 hash 已存在时不重复下载。"""
    from src.plugins.message_archive import image_store
    from tests.message_store_helpers import make_image_bytes

    monkeypatch.setattr(image_store, "image_dir", tmp_path)

    call_count = 0

    async def _fake_fetch(url: str) -> bytes:
        nonlocal call_count
        call_count += 1
        return make_image_bytes(4)

    monkeypatch.setattr(image_store, "_fetch_bytes", _fake_fetch)

    first = await image_store.store_image(
        "https://example.com/a.png", "abcdef", max_size_bytes=1024
    )
    second = await image_store.store_image(
        "https://example.com/a.png", "abcdef", max_size_bytes=1024
    )
    assert first == second
    assert call_count == 1


@pytest.mark.asyncio
async def test_store_image_returns_none_when_too_large(tmp_path, monkeypatch) -> None:
    """超过大小上限时返回 None。"""
    from src.plugins.message_archive import image_store
    from tests.message_store_helpers import make_image_bytes

    monkeypatch.setattr(image_store, "image_dir", tmp_path)

    async def _fake_fetch(url: object) -> bytes:
        return make_image_bytes(2048)

    monkeypatch.setattr(image_store, "_fetch_bytes", _fake_fetch)

    path = await image_store.store_image(
        "https://example.com/a.png", "abcdef", max_size_bytes=1024
    )
    assert path is None


@pytest.mark.asyncio
async def test_record_image_meta_inserts_row() -> None:
    """record_image_meta 写入元数据，可被 get_image_meta 查到。"""
    from src.plugins.message_archive.image_store import get_image_meta, record_image_meta

    await record_image_meta(
        file_hash="deadbeef",
        file_path="data/message_archive_images/de/deadbeef.png",
        expire_at=2000,
    )
    row = await get_image_meta("deadbeef")
    assert row is not None
    assert row["file_hash"] == "deadbeef"
    assert row["expire_at"] == 2000


@pytest.mark.asyncio
async def test_record_image_meta_replaces_on_duplicate() -> None:
    """同 hash 重复登记时刷新 expire_at。"""
    from src.plugins.message_archive.image_store import get_image_meta, record_image_meta

    await record_image_meta("cafe", "p", expire_at=1000)
    await record_image_meta("cafe", "p", expire_at=2000)
    row = await get_image_meta("cafe")
    assert row is not None and row["expire_at"] == 2000


@pytest.mark.asyncio
async def test_purge_expired_removes_expired_files_and_rows(tmp_path, monkeypatch) -> None:
    """purge_expired 删除过期文件 + 元数据行。"""
    from src.plugins.message_archive.image_store import purge_expired, record_image_meta

    monkeypatch.setattr("src.plugins.message_archive.image_store.image_dir", tmp_path)

    # 过期：expire_at=1000（当前时间远大于）
    expired_path = tmp_path / "de" / "deadbeef.png"
    expired_path.parent.mkdir(parents=True)
    expired_path.write_bytes(b"x")
    await record_image_meta("deadbeef", str(expired_path), expire_at=1000)

    # 未过期：用足够远的未来时间戳（远大于当前 2026 年）
    fresh_path = tmp_path / "ca" / "cafe.png"
    fresh_path.parent.mkdir(parents=True)
    fresh_path.write_bytes(b"y")
    await record_image_meta("cafe", str(fresh_path), expire_at=9999999999)

    count = await purge_expired()
    assert count == 1
    assert not expired_path.exists()
    assert fresh_path.exists()


@pytest.mark.asyncio
async def test_purge_orphans_removes_files_without_meta(tmp_path, monkeypatch) -> None:
    """purge_orphans 删除无元数据引用的孤儿文件。"""
    from src.plugins.message_archive.image_store import purge_orphans, record_image_meta

    monkeypatch.setattr("src.plugins.message_archive.image_store.image_dir", tmp_path)

    legit = tmp_path / "ca" / "cafe.png"
    legit.parent.mkdir(parents=True)
    legit.write_bytes(b"y")
    await record_image_meta("cafe", str(legit), expire_at=9999999999)

    orphan = tmp_path / "or" / "orphandata.png"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"z")

    count = await purge_orphans()
    assert count == 1
    assert not orphan.exists()
    assert legit.exists()
