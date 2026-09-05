"""Path conventions that mark a file as a test, shared by priors and lanes.

Test files answer "which tests cover X" and must not lead "where is X
implemented"; both rules need one definition of what a test file is.
"""

from __future__ import annotations

import re

# Benchmarks and performance harnesses exercise the API the way tests do:
# they call everything and define nothing a request is looking for.
TEST_DIRECTORIES = frozenset(
    {
        "test",
        "tests",
        "testing",
        "__tests__",
        "__testfixtures__",
        "testfixtures",
        "asv_bench",
        "bench",
        "benches",
        "benchmark",
        "benchmarks",
        "perf",
    }
)
# foo.test.ts, foo.spec.js, foo.test-d.ts (type tests), foo_test.go
TEST_FILE = re.compile(r"\.(?:test|spec)(?:-d)?\.|_test\.go$")


def is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        any(f"/{part}/" in f"/{normalized}" for part in TEST_DIRECTORIES)
        or name.startswith("test_")
        or name == "conftest.py"
        or TEST_FILE.search(name) is not None
    )
