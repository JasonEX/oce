"""Regex symbol evidence remains conservative and deterministic."""

from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider


def test_endpoint_evidence_suppresses_duplicate_definition():
    provider = RegexSymbolProvider()

    occurrences = provider.extract(
        content="\n" * 19 + "#[tauri::command]\npub async fn load_profile() {}",
        language="rust",
    )

    assert [(item.identifier, item.kind) for item in occurrences] == [
        ("load_profile", "endpoint")
    ]
    # Lines are absolute file lines: the ``fn`` sits on line 21.
    assert occurrences[0].start_line == 21
    assert occurrences[0].end_line == 21


def test_provider_extracts_supported_definition_shapes():
    provider = RegexSymbolProvider()

    occurrences = provider.extract(
        content=(
            "class WorkspaceContext:\n"
            "    pass\n"
            "export interface RetrievalResult {}\n"
            "export const buildClient = () => {}\n"
        ),
        language=None,
    )

    assert {(item.identifier, item.kind, item.start_line) for item in occurrences} == {
        ("WorkspaceContext", "definition", 1),
        ("RetrievalResult", "definition", 3),
        ("buildClient", "definition", 4),
    }


def test_provider_does_not_claim_references():
    occurrences = RegexSymbolProvider().extract(
        content="result = load_profile(user_id)",
        language="python",
    )

    assert occurrences == ()
