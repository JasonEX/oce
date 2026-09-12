# Reference region contract v2

Status: development annotation contract with catalog revision 2. The historical
short-query evaluator and its scores remain unchanged. The contract was fixed
before labeling; the initial catalog and its later source-audit correction are
both retained. This is not an independent validation set.

A reference answer must show a source location that uses the requested symbol.
The location may share a file with the declaration. File identity neither proves
a use nor makes one invalid. A declaration's own name, an import without a use,
and prose mentioning the name are insufficient. Calls, type annotations,
construction, trait implementations and inheritance can establish references;
a question specifically asking for calls requires a call. A runtime prototype
mutation is also a use. Calls in a test count for a general reference question;
that does not make a type test the preferred answer to a runtime-test question.

Version 2 uses the existing region-based `project_cases` evaluator with a separate
[manifest](../blackbox/reference_regions_v2_2026_09_11.json) and unchanged questions.
It contains 18 English/Chinese questions, nine symbol families, seven source
languages and 130 reviewed use lines. Reviewed source-use lines are primary regions.
Declaration excerpts may provide supporting context. Unrelated files can remain
distractors; a file containing both a declaration and a valid use cannot be
excluded wholesale. The containing returned excerpt must overlap an actual use
line for the primary metric to count it.

Annotations record the pinned revision, file digest, exact use text and evidence
kind. Cases include both same-file and cross-file uses across languages, including
cases without a known C12 failure. They are a bounded reviewed catalog, not an
exhaustive static reference graph. Missing an unlisted valid use may still be a
label limitation and must be audited rather than treated as proof of irrelevance.

This revision responds to a known development-label defect and is not independent
validation. Scores from v1 and v2 must be reported separately. Label changes do
not establish a retrieval improvement; comparisons within v2 require identical
questions, snapshots and labels. The Flask prose head remains a negative example.

The first catalog covered 16 questions and 87 use lines. An audit against retained
historical responses found omitted real Flask type annotations, jq calls and Gson
type uses. Revision 2 adds the reviewed uses and the known `Blueprint` prose
control. [Initial audit and correction](retrieval-isolation-2026-09-11-data/reference-v2-initial-audit.json)
and [line-level source evidence](retrieval-isolation-2026-09-11-data/reference-v2-revision2-proof.json)
record this development process. Neither the original v1 truth nor its results
were edited, and a catalog miss is not automatically an irrelevant answer.

The existing scorer accepts `PreparedRequest.copy` constructing `PreparedRequest`
in `requests/models.py`, while rejecting the class's own declaration and docstring.
It also rejects the `Blueprint` class's prose-only span in `src/flask/blueprints.py`
and accepts the type use in `register_blueprint`. These are source-region checks,
not file-name exceptions in the product.

On the same 18 retained responses, the old file-level subset Top-1 was 1.000 for
the SQL control and 0.722 for C12. The revised catalog Top-1 is 0.611 and 0.833;
catalog Hit@3 is 0.833 and 1.000. This reversal shows that the two label contracts
measure different evidence. These are [post-hoc scores](retrieval-isolation-2026-09-11-data/reference-v2-historical-relabel.json),
not new retrieval improvements, and do not override C12's issue-ranking regression.
The final foundation wheel completed all 18 catalog requests through the
released client with Top-1 0.611 and Hit@3 0.833.
