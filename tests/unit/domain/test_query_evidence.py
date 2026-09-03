"""Issue text yields deterministic recall evidence."""

from oce.domain.services.query_evidence import extract_query_evidence

_ISSUE = """Calling `Dataset.to_zarr` fails.
Traceback (most recent call last):
  File "/usr/lib/python3.11/site-packages/xarray/core/dataset.py", line 2089, in to_zarr
    return to_zarr(self, store=store)
  File "/home/u/xarray/xarray/backends/api.py", line 1620, in <module>
    zstore = backends.ZarrStore.open_group(
ValueError: invalid compressor for dtype 'int64'
Also see src/config/loader.py and pyproject.toml. Version 3.13.
"""


def test_traceback_frames_become_paths_and_identifiers():
    evidence = extract_query_evidence(_ISSUE)

    assert "xarray/core/dataset.py" in evidence.paths
    assert "home/u/xarray/xarray/backends/api.py" in evidence.paths
    assert "src/config/loader.py" in evidence.paths
    assert "to_zarr" in evidence.identifiers
    assert "ValueError" in evidence.identifiers
    # ``<module>`` is not a symbol.
    assert not any(name.startswith("<") for name in evidence.identifiers)


def test_error_line_and_filenames_are_recovered():
    evidence = extract_query_evidence(_ISSUE)

    assert evidence.phrases == ("invalid compressor for dtype 'int64'",)
    assert {"dataset.py", "api.py", "loader.py", "pyproject.toml"} <= set(
        evidence.filenames
    )
    # A version number is not a filename.
    assert "3.13" not in evidence.filenames
    assert "compressor" in evidence.terms


def test_node_stack_frames():
    evidence = extract_query_evidence(
        "TypeError: x is not a function\n"
        "    at handleRequest (/app/src/server/router.ts:42:11)\n"
        "    at /app/src/server/index.ts:10:3"
    )
    assert "handleRequest" in evidence.identifiers
    assert "app/src/server/router.ts" in evidence.paths
    assert "app/src/server/index.ts" in evidence.paths


def test_plain_questions_yield_terms_only():
    evidence = extract_query_evidence("config.json 在哪里？")
    assert evidence.filenames == ("config.json",)
    assert evidence.paths == ()
    assert evidence.identifiers == ()

    evidence = extract_query_evidence("Where is bearer token auth implemented?")
    assert evidence.has_path_evidence is False
    assert "bearer" in evidence.terms and "token" in evidence.terms


def test_quoted_phrases_skip_single_identifiers_and_urls():
    evidence = extract_query_evidence(
        'The log says "connection pool exhausted" and links https://x.y/z. `retry_once` fails.'
    )
    assert evidence.phrases == ("connection pool exhausted",)
    assert evidence.identifiers == ("retry_once",)
