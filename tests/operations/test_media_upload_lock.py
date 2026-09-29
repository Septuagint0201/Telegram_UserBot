"""Real process death releases the media lock; erasure then fences old work."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid7

import pytest

from telegram_userbot.adapters.media.storage import PrivateMediaStore


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="POSIX production process and flock test")
def test_upload_process_crash_releases_lock_for_erasure(tmp_path: Path) -> None:
    account, object_id = uuid7(), uuid7()
    root = tmp_path / "media"
    script = tmp_path / "writer.py"
    script.write_text("""
import sys
from pathlib import Path
from telegram_userbot.adapters.media.storage import PrivateMediaStore
root, account, object_id = sys.argv[1:]
store = PrivateMediaStore(Path(root))
with store._quota_guard():
    target = Path(root) / account / (object_id + '.png')
    target.parent.mkdir()
    target.write_bytes(b'private-unregistered-upload')
    print('locked', flush=True)
    sys.stdin.read()
""")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    child = subprocess.Popen(  # noqa: S603 - fixed interpreter, local synthetic fixture
        [sys.executable, str(script), str(root), str(account), str(object_id)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        store = PrivateMediaStore(root)
        with ThreadPoolExecutor(max_workers=1) as pool:
            erase = pool.submit(
                store.erase_object,
                account_id=account,
                object_id=object_id,
                storage_key=None,
                expected_sha256=None,
            )
            assert not erase.done()
            child.kill()
            child.wait(timeout=5)
            assert erase.result(timeout=5)
        assert not (root / str(account) / f"{object_id}.png").exists()
        assert (root / ".erased" / str(account) / str(object_id)).is_file()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
