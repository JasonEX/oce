"""Contract tests of CastChunker chunk boundaries.

Minimum-size merging: astchunk may emit two windows with the same start for
one construct (the first holding only the text before the split column);
merging must handle both the nested and the too-small case without losing
lines. Declaration preservation: a declaration slightly over the window must
stay whole, otherwise recursion hands back its statements, greedy packing
cuts through them, and the result is a named half without a body and a half
that opens with ``return {``.
"""

from __future__ import annotations

from oce.domain.chunk.recursive_chunker import RecursiveChunker
from oce.infrastructure.astchunk.cast_chunker import CastChunker


def make_chunker(min_chunk_chars: int = 300) -> CastChunker:
    return CastChunker(
        max_chunk_size=1_500,
        fallback=RecursiveChunker(),
        min_chunk_chars=min_chunk_chars,
    )


def lines_of(count: int, width: int = 40) -> list[str]:
    return [f"line{index:03d}".ljust(width, "x") for index in range(1, count + 1)]


class TestMergeSmall:
    def test_range_sharing_a_start_line_is_absorbed(self):
        """The prefix window astchunk emits for one construct is not its own chunk."""
        lines = lines_of(40)
        ranges = [(1, 1), (1, 20), (21, 40)]
        merged = make_chunker()._merge_small(ranges, lines)
        assert (1, 1) not in merged
        assert merged == [(1, 20), (21, 40)]

    def test_fully_contained_range_is_dropped(self):
        lines = lines_of(40)
        ranges = [(1, 30), (5, 12), (31, 40)]
        merged = make_chunker()._merge_small(ranges, lines)
        assert merged == [(1, 30), (31, 40)]

    def test_trailing_fragment_attaches_to_previous_range(self):
        """A lone closing bracket joins the previous chunk."""
        lines = lines_of(41)
        ranges = [(1, 40), (41, 41)]
        merged = make_chunker()._merge_small(ranges, lines)
        assert merged == [(1, 41)]

    def test_leading_fragment_absorbs_the_next_range(self):
        """A tiny first chunk absorbs the next so it carries enough context."""
        lines = lines_of(40)
        ranges = [(1, 2), (3, 30), (31, 40)]
        merged = make_chunker()._merge_small(ranges, lines)
        assert merged[0] == (1, 30)

    def test_merging_preserves_line_coverage(self):
        lines = lines_of(60)
        ranges = [
            (1, 1),
            (2, 3),
            (4, 25),
            (26, 26),
            (27, 60),
        ]
        merged = make_chunker()._merge_small(ranges, lines)
        covered: set[int] = set()
        for start, end in merged:
            covered.update(range(start, end + 1))
        assert covered == set(range(1, 61))

    def test_zero_floor_disables_merging(self):
        lines = lines_of(40)
        ranges = [(1, 1), (1, 20)]
        assert make_chunker(min_chunk_chars=0)._merge_small(ranges, lines) == ranges


class TestMergeThroughPublicApi:
    def test_signature_only_chunk_does_not_survive(self):
        """A header split from its body must not leave a signature-only chunk."""
        content = (
            "export type UiState = {\n"
            + "\n".join(f"  field{index}: string;" for index in range(80))
            + "\n};\n"
            + "export function build(): UiState {\n"
            + "\n".join(f"  const step{index} = {index};" for index in range(80))
            + "\n  return null as unknown as UiState;\n}\n"
        )
        chunks = make_chunker().chunk(content, "src/ui.ts")
        assert chunks
        tiny = [chunk for chunk in chunks if len(chunk.content) < 300]
        assert tiny == [], [chunk.content for chunk in tiny]

    def test_merged_chunks_still_match_their_line_range(self):
        content = (
            "class Service {\n"
            + "\n".join(
                f"  method{index}() {{\n    return {index};\n  }}"
                for index in range(60)
            )
            + "\n}\n"
        )
        lines = content.splitlines()
        for chunk in make_chunker().chunk(content, "src/service.ts"):
            expected = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
            assert chunk.content == expected

    def test_merged_chunks_stay_within_the_char_budget(self):
        content = (
            "function run() {\n"
            + "\n".join(f"  const value{index} = {'q' * 90};" for index in range(200))
            + "\n}\n"
        )
        chunker = CastChunker(
            max_chunk_size=100_000,
            fallback=RecursiveChunker(),
            max_chunk_chars=4_000,
            min_chunk_chars=300,
        )
        chunks = chunker.chunk(content, "src/run.ts")
        assert chunks
        assert all(len(chunk.content) <= 4_000 for chunk in chunks)


class TestIntactDeclarations:
    """A declaration slightly over the window is not split into signature and orphaned body."""

    @staticmethod
    def _oversized_case(name: str, statements: int = 40) -> str:
        body = "\n".join(
            f'    expect(result.field{index}).toBe("value{index}");'
            for index in range(statements)
        )
        return f'  it("{name}", async () => {{\n{body}\n  }});\n'

    def test_oversized_test_case_stays_whole(self):
        content = (
            'describe("suite", () => {\n'
            + self._oversized_case("handles the first branch")
            + self._oversized_case("handles the second branch")
            + "});\n"
        )
        chunker = CastChunker(
            max_chunk_size=300,
            fallback=RecursiveChunker(),
        )
        chunks = chunker.chunk(content, "src/suite.test.ts")
        bodies = [chunk.content for chunk in chunks if "it(" in chunk.content]
        assert bodies, [chunk.content[:60] for chunk in chunks]
        for body in bodies:
            # a chunk with it( carries its body and close, not one signature line
            assert body.count("expect(") > 1, body[:120]

    def test_declaration_without_field_names_stays_whole(self):
        """Kotlin names no child fields, so the body is found by child type.

        A whitelist of node type names regressed here: Kotlin's declaration
        types were missing from the table derived from TypeScript, and an
        oversized declaration was silently split.
        """
        members = "\n".join(
            f'    fun member{index}(): String = "value{index}"' for index in range(40)
        )
        content = f"class Repository {{\n{members}\n}}\n"
        chunker = CastChunker(
            max_chunk_size=300,
            fallback=RecursiveChunker(),
        )
        chunks = chunker.chunk(content, "src/Repository.kt")
        assert len(chunks) == 1, [chunk.content[:60] for chunk in chunks]
        assert chunks[0].content.startswith("class Repository {")
        assert chunks[0].content.rstrip().endswith("}")

    def test_character_budget_bounds_what_is_kept_whole(self):
        """The keep-whole limit follows the character budget, or cap_span splits the kept chunk again."""
        content = (
            'describe("suite", () => {\n'
            + self._oversized_case("single case")
            + "});\n"
        )
        roomy = CastChunker(
            max_chunk_size=300,
            fallback=RecursiveChunker(),
        ).chunk(content, "src/suite.test.ts")
        tight = CastChunker(
            max_chunk_size=300,
            fallback=RecursiveChunker(),
            max_chunk_chars=800,
        ).chunk(content, "src/suite.test.ts")
        assert len(tight) > len(roomy)

    def test_runaway_wrapper_is_still_split(self):
        """A file wrapped in one describe must still be split."""
        content = (
            'describe("giant", () => {\n'
            + "".join(self._oversized_case(f"case {index}") for index in range(12))
            + "});\n"
        )
        chunker = CastChunker(
            max_chunk_size=300,
            fallback=RecursiveChunker(),
        )
        chunks = chunker.chunk(content, "src/giant.test.ts")
        assert len(chunks) > 1
        lines = content.splitlines()
        for chunk in chunks:
            assert chunk.content == "\n".join(
                lines[chunk.start_line - 1 : chunk.end_line]
            )
