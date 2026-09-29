"""Filesystem erasure covers unregistered writes and prevents resurrection."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid7

import pytest

from telegram_userbot.adapters.media.storage import PrivateMediaStore
from telegram_userbot.adapters.media.validation import ImageIngestor
from tests.unit.adapters.test_media import image_bytes


@pytest.mark.unit
def test_inventory_rejects_orphans_and_restored_deleted_files(tmp_path: Path) -> None:
    account, erased, surviving = uuid7(), uuid7(), uuid7()
    store = PrivateMediaStore(tmp_path)
    image = ImageIngestor().validate_bytes(image_bytes(), declared_mime="image/png")
    original = store.store_original(account_id=account, object_id=erased, image=image)
    store.store_original(account_id=account, object_id=surviving, image=image)
    objects = {erased: True, surviving: False}
    with pytest.raises(ValueError, match="media_inventory_unattributed"):
        store.verify_erasure_inventory(account_id=account, objects=objects, account_wipe=False)
    # Even a failed check has fenced a stale writer admitted before erasure.
    with pytest.raises(RuntimeError, match="media_object_erased"):
        store.store_original(account_id=account, object_id=erased, image=image)
    store.resolve_key(original.storage_key).unlink()
    store.verify_erasure_inventory(account_id=account, objects=objects, account_wipe=False)
    with pytest.raises(ValueError, match="media_inventory_unattributed"):
        store.verify_erasure_inventory(account_id=account, objects=objects, account_wipe=True)


@pytest.mark.unit
def test_unregistered_final_and_named_temporary_are_erased_and_cannot_return(
    tmp_path: Path,
) -> None:
    account, object_id, sibling = uuid7(), uuid7(), uuid7()
    store = PrivateMediaStore(tmp_path)
    image = ImageIngestor().validate_bytes(image_bytes(), declared_mime="image/png")
    original = store.store_original(account_id=account, object_id=object_id, image=image)
    other = store.store_original(account_id=account, object_id=sibling, image=image)
    final = store.resolve_key(original.storage_key)
    temporary = final.parent / f".ingest-{object_id}-crashed"
    temporary.write_bytes(b"partial-private-image")
    assert store.erase_object(
        account_id=account, object_id=object_id, storage_key=None, expected_sha256=None
    )
    assert not temporary.exists()
    assert not final.exists()
    assert store.resolve_key(other.storage_key).is_file()
    restarted = PrivateMediaStore(tmp_path)
    assert not restarted.erase_object(
        account_id=account, object_id=object_id, storage_key=None, expected_sha256=None
    )
    with pytest.raises(RuntimeError, match="media_object_erased"):
        restarted.store_original(account_id=account, object_id=object_id, image=image)


@pytest.mark.unit
def test_anonymous_legacy_upload_requires_account_wipe(tmp_path: Path) -> None:
    account, other_account, object_id = uuid7(), uuid7(), uuid7()
    store = PrivateMediaStore(tmp_path)
    for identity in (account, other_account):
        directory = tmp_path / str(identity) / "aa"
        directory.mkdir(parents=True)
        (directory / ".ingest-legacy").write_bytes(b"private")
    with pytest.raises(ValueError, match="media_legacy_temporary_unattributed"):
        store.erase_object(
            account_id=account, object_id=object_id, storage_key=None, expected_sha256=None
        )
    store.erase_legacy_account_uploads(account)
    assert not (tmp_path / str(account) / "aa/.ingest-legacy").exists()
    assert (tmp_path / str(other_account) / "aa/.ingest-legacy").exists()
    image = ImageIngestor().validate_bytes(image_bytes(), declared_mime="image/png")
    with pytest.raises(RuntimeError, match="media_object_erased"):
        store.store_original(account_id=account, object_id=uuid7(), image=image)
    assert not store.erase_object(
        account_id=account, object_id=object_id, storage_key=None, expected_sha256=None
    )


@pytest.mark.unit
def test_cleanup_serializes_with_active_writer_then_fences_late_writer(tmp_path: Path) -> None:
    account, object_id = uuid7(), uuid7()
    store = PrivateMediaStore(tmp_path)
    image = ImageIngestor().validate_bytes(image_bytes(), declared_mime="image/png")
    entered, release = threading.Event(), threading.Event()

    def writer() -> None:
        with store._quota_guard():
            entered.set()
            assert release.wait(5)
            # Model a process dying after a final rename but before DB registration.
            path = tmp_path / str(account) / f"{object_id}.png"
            path.parent.mkdir()
            path.write_bytes(image_bytes())

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        assert entered.wait(5)
        erasing = pool.submit(
            store.erase_object,
            account_id=account,
            object_id=object_id,
            storage_key=None,
            expected_sha256=None,
        )
        assert not erasing.done()
        release.set()
        writing.result(5)
        assert erasing.result(5)
    with pytest.raises(RuntimeError, match="media_object_erased"):
        store.store_provider_copy(account_id=account, object_id=object_id, image=image)


@pytest.mark.unit
def test_object_cleanup_rejects_cross_account_keys_and_changed_registered_file(
    tmp_path: Path,
) -> None:
    account, object_id = uuid7(), uuid7()
    store = PrivateMediaStore(tmp_path)
    image = ImageIngestor().validate_bytes(image_bytes(), declared_mime="image/png")
    stored = store.store_original(account_id=account, object_id=object_id, image=image)
    path = store.resolve_key(stored.storage_key)
    with pytest.raises(ValueError, match="media_object_namespace_mismatch"):
        store.erase_object(
            account_id=uuid7(),
            object_id=object_id,
            storage_key=stored.storage_key,
            expected_sha256=stored.sha256,
        )
    path.write_bytes(b"unexpected replacement")
    with pytest.raises(ValueError, match="media_hash_mismatch"):
        store.erase_object(
            account_id=account,
            object_id=object_id,
            storage_key=stored.storage_key,
            expected_sha256=stored.sha256,
        )
    assert path.read_bytes() == b"unexpected replacement"


@pytest.mark.unit
def test_object_cleanup_rejects_symlink_and_keeps_external_target(tmp_path: Path) -> None:
    account, object_id = uuid7(), uuid7()
    store = PrivateMediaStore(tmp_path / "media")
    account_path = tmp_path / "media" / str(account)
    account_path.mkdir()
    external = tmp_path / "external"
    external.write_bytes(b"must survive")
    try:
        (account_path / f"{object_id}.png").symlink_to(external)
    except OSError:
        pytest.skip("host does not permit unprivileged symlink creation")
    with pytest.raises(ValueError, match="media_storage_key_symlink"):
        store.erase_object(
            account_id=account, object_id=object_id, storage_key=None, expected_sha256=None
        )
    assert external.read_bytes() == b"must survive"
