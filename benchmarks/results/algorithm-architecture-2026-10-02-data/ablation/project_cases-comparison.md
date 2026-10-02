| Variant | OK | Primary Top-1 | Primary Hit@3 | MRR | Relation R | Supporting R | Hop R | Chain closed | Test R | Distractor head | Truth share | Chars | p50 ms | p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| candidate | 35/35 | 88.6% | 97.1% | 0.929 | 95.3% | 93.8% | 99.3% | 97.1% | 100.0% | 0.0% | 32.4% | 19085 | 86 | 581 |
| legacy_retrieval_on_symbols6 | 35/35 | 88.6% | 97.1% | 0.929 | 94.3% | 93.8% | 98.3% | 94.3% | 100.0% | 0.0% | 32.2% | 19073 | 58 | 823 |
| candidate-repeat | 35/35 | 88.6% | 97.1% | 0.929 | 95.3% | 93.8% | 99.3% | 97.1% | 100.0% | 0.0% | 32.4% | 19085 | 65 | 600 |
| legacy_retrieval_on_symbols6-repeat | 35/35 | 88.6% | 97.1% | 0.929 | 94.3% | 93.8% | 98.3% | 94.3% | 100.0% | 0.0% | 32.2% | 19073 | 52 | 899 |

| Variant | reference Hit@3 / RelR | call_chain Hit@3 / RelR | test_mapping Hit@3 / RelR | reexport Hit@3 / RelR | multi_impl Hit@3 / RelR |
| --- | --- | --- | --- | --- | --- |
| candidate | 100.0% / 100.0% | 100.0% / 90.7% | 85.7% / 92.9% | 100.0% / 90.0% | 100.0% / 100.0% |
| legacy_retrieval_on_symbols6 | 100.0% / 100.0% | 100.0% / 86.0% | 85.7% / 92.9% | 100.0% / 90.0% | 100.0% / 100.0% |
| candidate-repeat | 100.0% / 100.0% | 100.0% / 90.7% | 85.7% / 92.9% | 100.0% / 90.0% | 100.0% / 100.0% |
| legacy_retrieval_on_symbols6-repeat | 100.0% / 100.0% | 100.0% / 86.0% | 85.7% / 92.9% | 100.0% / 90.0% | 100.0% / 100.0% |

| Variant | none | exact_miss | distractor | test_missing | relation_missing | redundant |
| --- | --- | --- | --- | --- | --- | --- |
| candidate | 13 | 1 | 0 | 0 | 3 | 18 |
| legacy_retrieval_on_symbols6 | 13 | 1 | 0 | 0 | 3 | 18 |
| candidate-repeat | 13 | 1 | 0 | 0 | 3 | 18 |
| legacy_retrieval_on_symbols6-repeat | 13 | 1 | 0 | 0 | 3 | 18 |

| Variant | Snapshot | Hit@3 | MRR | RelR | Distractor head |
| --- | --- | --- | --- | --- | --- |
| candidate | axum-v0.7.9 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate | express-4.21.2 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate | gin-v1.10.0 | 100.0% | 0.833 | 100.0% | 0.0% |
| candidate | gson-2.11.0 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate | pallets__flask-5014 | 100.0% | 1.000 | 93.3% | 0.0% |
| candidate | psf__requests-5414 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate | pydata__xarray-7233 | 100.0% | 0.833 | 91.7% | 0.0% |
| candidate | pylint-dev__pylint-7080 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate | pytest-dev__pytest-10356 | 75.0% | 0.750 | 75.0% | 0.0% |
| candidate | redux-toolkit-v2.2.7 | 100.0% | 0.875 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6 | axum-v0.7.9 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6 | express-4.21.2 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6 | gin-v1.10.0 | 100.0% | 0.833 | 88.9% | 0.0% |
| legacy_retrieval_on_symbols6 | gson-2.11.0 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6 | pallets__flask-5014 | 100.0% | 1.000 | 93.3% | 0.0% |
| legacy_retrieval_on_symbols6 | psf__requests-5414 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6 | pydata__xarray-7233 | 100.0% | 0.833 | 91.7% | 0.0% |
| legacy_retrieval_on_symbols6 | pylint-dev__pylint-7080 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6 | pytest-dev__pytest-10356 | 75.0% | 0.750 | 75.0% | 0.0% |
| legacy_retrieval_on_symbols6 | redux-toolkit-v2.2.7 | 100.0% | 0.875 | 100.0% | 0.0% |
| candidate-repeat | axum-v0.7.9 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate-repeat | express-4.21.2 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate-repeat | gin-v1.10.0 | 100.0% | 0.833 | 100.0% | 0.0% |
| candidate-repeat | gson-2.11.0 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate-repeat | pallets__flask-5014 | 100.0% | 1.000 | 93.3% | 0.0% |
| candidate-repeat | psf__requests-5414 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate-repeat | pydata__xarray-7233 | 100.0% | 0.833 | 91.7% | 0.0% |
| candidate-repeat | pylint-dev__pylint-7080 | 100.0% | 1.000 | 100.0% | 0.0% |
| candidate-repeat | pytest-dev__pytest-10356 | 75.0% | 0.750 | 75.0% | 0.0% |
| candidate-repeat | redux-toolkit-v2.2.7 | 100.0% | 0.875 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | axum-v0.7.9 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | express-4.21.2 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | gin-v1.10.0 | 100.0% | 0.833 | 88.9% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | gson-2.11.0 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | pallets__flask-5014 | 100.0% | 1.000 | 93.3% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | psf__requests-5414 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | pydata__xarray-7233 | 100.0% | 0.833 | 91.7% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | pylint-dev__pylint-7080 | 100.0% | 1.000 | 100.0% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | pytest-dev__pytest-10356 | 75.0% | 0.750 | 75.0% | 0.0% |
| legacy_retrieval_on_symbols6-repeat | redux-toolkit-v2.2.7 | 100.0% | 0.875 | 100.0% | 0.0% |
