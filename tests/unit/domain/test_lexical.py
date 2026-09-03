"""Lexical tokenization keeps document and query token shapes identical."""

from oce.domain.services.lexical import (
    build_lexical_document,
    lexical_tokens,
    phrase_tokens,
    query_terms,
    split_identifier,
)


def test_identifiers_split_into_subwords_and_keep_a_surrogate():
    assert split_identifier("parseConfigV2") == ["parse", "config", "v", "2"]
    assert lexical_tokens("parse_config ParseConfig") == [
        "parseconfig",
        "parse",
        "config",
        "parseconfig",
        "parse",
        "config",
    ]


def test_document_and_query_agree_on_error_text():
    document = build_lexical_document(
        'raise ValueError("invalid compressor for dtype")'
    )
    assert phrase_tokens("invalid compressor for dtype") == (
        "invalid",
        "compressor",
        "for",
        "dtype",
    )
    assert "valueerror" in document.split()


def test_query_terms_drop_stopwords_and_keep_identifiers_first():
    terms = query_terms(
        "How does the request authentication middleware validate a token?",
        extra=["verify_api_key"],
    )
    # Extra identifiers and their sub-words outrank plain question words.
    assert terms[0] == "verifyapikey"
    assert set(terms[:4]) == {"verifyapikey", "verify", "api", "key"}
    assert terms[4:6] == ("authentication", "middleware")
    assert "the" not in terms and "does" not in terms


def test_query_terms_are_bounded():
    text = " ".join(f"token{index}" for index in range(100))
    assert len(query_terms(text, limit=10)) == 10
