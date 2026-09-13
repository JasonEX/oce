"""Blob aggregate tests."""

import pytest

from oce.domain.blob.blob import Blob, BlobStatus
from tests.conftest import make_blob, make_chunk_ref, make_sha256


class TestBlobCreation:
    """Construction."""

    def test_create_blob_with_valid_data(self):
        """A valid blob."""
        blob = make_blob(
            blob_name=make_sha256("test"),
            path="src/test.py",
        )

        assert blob.blob_name is not None
        assert blob.path == "src/test.py"
        assert blob.status == BlobStatus.PENDING
        assert len(blob.chunks) == 0

    def test_create_blob_with_invalid_blob_name(self):
        """An invalid blob_name is rejected."""
        with pytest.raises(ValueError, match="Invalid blob_name"):
            Blob(
                blob_name="not-a-sha256",
                path="src/test.py",
            )


class TestBlobChunkManagement:
    """Chunk references."""

    def test_chunks_default_to_empty_list_per_instance(self):
        first = make_blob()
        second = make_blob()

        first.chunks.append(make_chunk_ref())

        assert len(first.chunks) == 1
        assert second.chunks == []


class TestBlobStatusTransition:
    """State transitions."""

    def test_mark_ready_with_chunks(self):
        """A blob with chunks can be marked ready."""
        blob = make_blob()
        blob.chunks.append(make_chunk_ref())

        blob.mark_ready()

        assert blob.status == BlobStatus.READY
        assert blob.error_message is None

    def test_mark_ready_without_chunks_supports_empty_files(self):
        blob = make_blob()

        blob.mark_ready()

        assert blob.status == BlobStatus.READY

    def test_mark_error(self):
        """Marking an error."""
        blob = make_blob()
        error_msg = "Embedding failed"

        blob.mark_error(error_msg)

        assert blob.status == BlobStatus.ERROR
        assert blob.error_message == error_msg


class TestBlobTouch:
    """touch."""

    def test_touch_updates_last_seen(self, freezed_time):
        """touch updates last_seen."""
        from datetime import timedelta

        blob = make_blob()
        old_time = freezed_time - timedelta(hours=1)
        blob.last_seen = old_time

        blob.touch()

        assert blob.last_seen > old_time
