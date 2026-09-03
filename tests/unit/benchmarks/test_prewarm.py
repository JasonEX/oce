import pytest

from benchmarks.blackbox.prewarm import _upload_batches, admitted_uploads, blob_name


def test_blob_name_matches_ace_path_content_contract() -> None:
    assert blob_name("src/a.py", "print('ok')\n") == (
        "7e509c9ce9399cd9d0ab99e610a91f4c2dec175d5119707ab2384d0205a85e33"
    )


def test_admitted_uploads_reads_only_client_paths(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/a.py").write_text("print('ok')\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

    uploads = admitted_uploads(root, ["src/a.py"])

    assert list(uploads.values()) == [{"path": "src/a.py", "content": "print('ok')\n"}]


def test_admitted_uploads_rejects_path_outside_workspace(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.py").write_text("secret\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsafe path"):
        admitted_uploads(root, ["../outside.py"])


def test_upload_batches_enforce_blob_and_byte_bounds() -> None:
    uploads = {name: {"path": name, "content": "123"} for name in ("a", "b", "c")}

    by_count = list(_upload_batches(uploads, uploads, max_blobs=2, max_bytes=100))
    by_bytes = list(_upload_batches(uploads, uploads, max_blobs=10, max_bytes=5))

    assert [[item["path"] for item in batch] for batch in by_count] == [
        ["a", "b"],
        ["c"],
    ]
    assert [[item["path"] for item in batch] for batch in by_bytes] == [
        ["a"],
        ["b"],
        ["c"],
    ]
