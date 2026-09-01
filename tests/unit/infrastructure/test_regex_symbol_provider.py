"""Regex symbol evidence remains conservative and deterministic."""

from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider


def test_endpoint_evidence_suppresses_duplicate_definition():
    provider = RegexSymbolProvider()

    occurrences = provider.extract(
        content="#[tauri::command]\npub async fn load_profile() {}",
        language="rust",
        start_line=20,
        end_line=21,
    )

    assert [(item.identifier, item.kind) for item in occurrences] == [
        ("load_profile", "endpoint")
    ]
    assert occurrences[0].start_line == 20
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
        start_line=1,
        end_line=4,
    )

    assert {(item.identifier, item.kind) for item in occurrences} == {
        ("WorkspaceContext", "definition"),
        ("RetrievalResult", "definition"),
        ("buildClient", "definition"),
    }


def test_provider_does_not_claim_references():
    occurrences = RegexSymbolProvider().extract(
        content="result = load_profile(user_id)",
        language="python",
        start_line=1,
        end_line=1,
    )

    assert occurrences == ()
