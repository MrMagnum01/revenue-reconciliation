# Third-party licences

Every library this project installs, and the licence it ships under, as
read from each package's installed distribution metadata. All are open
source; no closed-source or paid dependency is used.

## Direct dependencies (pinned in `requirements.txt`)

| Library | Pinned version | Licence | Used for |
|---|---|---|---|
| [duckdb](https://pypi.org/project/duckdb/) | 1.5.5 | MIT | Reconciled output store (orders/payments/refunds, daily KPIs, mismatch report) |
| [pytest](https://pypi.org/project/pytest/) | 9.1.1 | MIT | Test suite |

## Transitive dependencies (not pinned; versions from a clean install)

| Library | Resolved version | Licence | Pulled in by |
|---|---|---|---|
| [packaging](https://pypi.org/project/packaging/) | 26.3 | Apache-2.0 OR BSD-2-Clause | pytest |
| [pluggy](https://pypi.org/project/pluggy/) | 1.6.0 | MIT | pytest |
| [iniconfig](https://pypi.org/project/iniconfig/) | 2.3.0 | MIT | pytest |
| [Pygments](https://pypi.org/project/Pygments/) | 2.21.0 | BSD-2-Clause | pytest |

The mock orders API (`api_server.py`) and its client (`api_client.py`) use
only the Python standard library (`http.server`, `urllib`) - no HTTP
framework dependency was added for it.

## Notices

These licences require their copyright and licence notices to be kept with
any copy or redistribution. This repository does not vendor or redistribute
any of these packages; they are installed from PyPI. Each installed package
carries its own licence and notice files, and those must be preserved in
any distribution that includes them.

No paid or closed-source service is used anywhere in this project.
