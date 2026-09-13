"""Chain aggregate tests."""

import pytest

from oce.domain.chain.chain import Chain
from tests.conftest import make_chain, make_sha256


class TestChainInvariants:
    def test_members_are_kept_as_given(self):
        members = [make_sha256("file1"), make_sha256("file2")]

        chain = make_chain(members=members)

        assert chain.version == 1
        assert chain.members == set(members)

    def test_invalid_uuid_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid chain_id"):
            Chain(chain_id="not-a-uuid", version=1)

    def test_version_below_one_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid version"):
            make_chain(version=0)


class TestCheckpointToken:
    """Checkpoint tokens."""

    def test_get_checkpoint_token(self):
        """Formatting a token."""
        chain = make_chain()

        token = Chain.format_checkpoint_token(chain.chain_id, chain.version)

        assert ":" in token
        assert token.startswith(chain.chain_id)
        assert token.endswith(str(chain.version))

    def test_parse_valid_checkpoint_token(self):
        """Parsing a valid token."""
        chain = make_chain()
        token = Chain.format_checkpoint_token(chain.chain_id, chain.version)

        parsed = Chain.parse_checkpoint_token(token)

        assert parsed is not None
        chain_id, version = parsed
        assert chain_id == chain.chain_id
        assert version == chain.version

    def test_parse_invalid_checkpoint_token(self):
        """Parsing invalid tokens."""
        invalid_tokens = [
            "",
            "no-colon",
            "invalid-uuid:1",
            "valid-uuid-format-but-not-uuid:not-a-number",
        ]

        for token in invalid_tokens:
            assert Chain.parse_checkpoint_token(token) is None
