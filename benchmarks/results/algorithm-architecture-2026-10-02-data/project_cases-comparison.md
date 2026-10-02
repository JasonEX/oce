| Variant | OK | Primary Top-1 | Primary Hit@3 | MRR | Relation R | Supporting R | Hop R | Chain closed | Test R | Distractor head | Truth share | Chars | p50 ms | p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 35/35 | 88.6% | 97.1% | 0.929 | 93.4% | 93.8% | 98.3% | 94.3% | 100.0% | 0.0% | 32.1% | 19113 | 58 | 976 |
| candidate | 35/35 | 88.6% | 97.1% | 0.929 | 95.3% | 93.8% | 99.3% | 97.1% | 100.0% | 0.0% | 32.4% | 19085 | 86 | 581 |
| baseline-repeat | 35/35 | 88.6% | 97.1% | 0.929 | 93.4% | 93.8% | 98.3% | 94.3% | 100.0% | 0.0% | 32.1% | 19113 | 58 | 917 |
| candidate-repeat | 35/35 | 88.6% | 97.1% | 0.929 | 95.3% | 93.8% | 99.3% | 97.1% | 100.0% | 0.0% | 32.4% | 19085 | 65 | 600 |

| Variant | reference Hit@3 / RelR | call_chain Hit@3 / RelR | test_mapping Hit@3 / RelR | reexport Hit@3 / RelR | multi_impl Hit@3 / RelR |
| --- | --- | --- | --- | --- | --- |
| baseline | 100.0% / 100.0% | 100.0% / 86.0% | 85.7% / 88.1% | 100.0% / 90.0% | 100.0% / 100.0% |
| candidate | 100.0% / 100.0% | 100.0% / 90.7% | 85.7% / 92.9% | 100.0% / 90.0% | 100.0% / 100.0% |
| baseline-repeat | 100.0% / 100.0% | 100.0% / 86.0% | 85.7% / 88.1% | 100.0% / 90.0% | 100.0% / 100.0% |
| candidate-repeat | 100.0% / 100.0% | 100.0% / 90.7% | 85.7% / 92.9% | 100.0% / 90.0% | 100.0% / 100.0% |

| Variant | none | exact_miss | distractor | test_missing | relation_missing | redundant |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | 13 | 1 | 0 | 0 | 3 | 18 |
| candidate | 13 | 1 | 0 | 0 | 3 | 18 |
| baseline-repeat | 13 | 1 | 0 | 0 | 3 | 18 |
| candidate-repeat | 13 | 1 | 0 | 0 | 3 | 18 |

| Variant | Snapshot | Hit@3 | MRR | RelR | Distractor head |
| --- | --- | --- | --- | --- | --- |
| baseline | axum-v0.7.9 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline | express-4.21.2 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline | gin-v1.10.0 | 100.0% | 0.833 | 88.9% | 0.0% |
| baseline | gson-2.11.0 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline | pallets__flask-5014 | 100.0% | 1.000 | 93.3% | 0.0% |
| baseline | psf__requests-5414 | 100.0% | 1.000 | 94.4% | 0.0% |
| baseline | pydata__xarray-7233 | 100.0% | 0.833 | 91.7% | 0.0% |
| baseline | pylint-dev__pylint-7080 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline | pytest-dev__pytest-10356 | 75.0% | 0.750 | 75.0% | 0.0% |
| baseline | redux-toolkit-v2.2.7 | 100.0% | 0.875 | 100.0% | 0.0% |
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
| baseline-repeat | axum-v0.7.9 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline-repeat | express-4.21.2 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline-repeat | gin-v1.10.0 | 100.0% | 0.833 | 88.9% | 0.0% |
| baseline-repeat | gson-2.11.0 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline-repeat | pallets__flask-5014 | 100.0% | 1.000 | 93.3% | 0.0% |
| baseline-repeat | psf__requests-5414 | 100.0% | 1.000 | 94.4% | 0.0% |
| baseline-repeat | pydata__xarray-7233 | 100.0% | 0.833 | 91.7% | 0.0% |
| baseline-repeat | pylint-dev__pylint-7080 | 100.0% | 1.000 | 100.0% | 0.0% |
| baseline-repeat | pytest-dev__pytest-10356 | 75.0% | 0.750 | 75.0% | 0.0% |
| baseline-repeat | redux-toolkit-v2.2.7 | 100.0% | 0.875 | 100.0% | 0.0% |
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
