# State-machine simplification: frozen validation protocol

Frozen before production edits against `879bc2e5322edd2f3763c7d71deeee891550f5c7`.
The companion `blackbox/swe_validation_2026_09_10.json` records selection,
dataset checksums, native case checksums and repository exclusions.

- Existing short, semantic, project, SWE development, CSN, upstream, heldout,
  query-variant and layout suites are development regression evidence.
- The new batch uses original SWE-bench issue text and pinned SWE-Explore
  truth, without rewritten questions or new labels. Select up to three cases
  per repository from all seven repositories absent from the current JSON
  corpora and SWE development profile. Selection is by a fixed hash, independent
  of query contents, retrieval outputs and relevance labels.
- During implementation, inspect only selection metadata and operational
  status. Do not inspect new questions, truth regions, individual retrievals
  or failures. Run the baseline and final candidate on the same frozen batch,
  and open aggregate metrics only after freezing the candidate source.
- Report native Top-1, nDCG, line recall, character cost, p50 and failures
  separately. Do not collapse them into a score. Existing development
  tolerances remain visible; complexity reduction does not erase regressions.
- Do not tune on the new batch's aggregate result. Further changes require a
  separate development cycle and a fresh validation batch; this batch then
  becomes historical validation evidence.

This is an operator-blinded public-data check for the current change, not an
independent human audit or proof of no prior model exposure. It measures Python
issue-context retrieval and does not establish cross-language reference or
call-chain generalization. Exclusions describe the inspected development
corpora, not a complete provenance audit of every historical experiment.
