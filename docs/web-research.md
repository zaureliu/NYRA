# Web Research V1

Public technical research is opt-in per explicit research request or authorized
hardware goal. No startup network request, operator browser profile, cookies,
memory dump, USB serial, COM path, IP or MAC is sent to a search provider.
`public_query` rejects local identifiers; hardware plans form queries from board
and chip names only. API keys are not required. No new credential store exists.

The provider interface currently supplies DuckDuckGo HTML and Bing RSS with one
fallback, plus independent direct HTTPS fetch. Search can be blocked by rate
limits or challenges; that is not a result. Additional authenticated providers
must use the existing Credential Broker, not project metadata or frontend keys.

HTTPS fetch checks every redirect and resolved address, connects to the checked
IP with hostname-verified TLS, ignores environment proxies, rejects private
addresses/credential URLs, and limits response size/time. HTML scripts are not
executed. PDF extraction uses pypdf, limited to 80 pages and 120,000 characters.
The technical cache is separate from personal knowledge and bounded to 64
documents (URL, time, SHA-256, source type, text). Old offline entries remain
explicitly stale; absence of fresh evidence does not become fresh knowledge.

Sources prioritize manufacturer documentation/datasheets, official repositories
and toolchain documentation. Host suffixes and GitHub organizations are checked,
not merely whether an URL contains a brand name. Search results and extracted
excerpts are WEB_CONTENT, never tool instructions or approval. Responses link
actual fetched sources; the technical API also returns retrieval timestamps.

Real validation used Bing RSS after DuckDuckGo was unavailable, fetched actual
PlatformIO sources and extracted its current `pio run` documentation:
https://docs.platformio.org/en/latest/core/userguide/cmd_run.html
Official Arduino board/variant definitions were also fetched for a controlled
build. These are distinct from unit-test fixtures. A search hit alone does not
answer every technical question. Natural questions now use command/API intent,
title/path specificity, content checks and at most one refined query. A best-first
crawl fetches at most eight pages and follows real links from official indexes.
Product-level indexes and the registered board catalog are starting points, not
prewritten answers or synthetic search hits. Search success and direct-document
success remain distinct. GitHub blob documents use their raw representation.

Specific answers use short local-model paraphrases with exact supporting excerpts
and source URLs. Citation matching normalizes typography/escaped Unicode, never
adds factual values. Unsupported numbers/physical assertions are rejected; failed
validation falls back to cited extracts. Current PlatformIO version queries prefer
the public package metadata; cached stale documents cannot prove a current version.
Technical profiles use the same evidence and retain UNKNOWN where data is absent.

## Runtime hotfix V2 (2026-09-05)

Baseline `9a4eb3acd3333b7cde9fb4fe5c08eeb5350ae40f`. The reported generic
failure did **not** recur for the two exact PlatformIO text questions on the
original packaged backend. Both returned grounded answers before this patch.
Do not attribute that historical incident to TLS or packaging without evidence.
The scoped reproduction did establish these defects:

- DuckDuckGo HTML returned HTTP 202; Bing RSS returned HTTP 200 with generic
  PlatformIO results. Exceptions were reduced to `ResearchError`, and source
  fetch exceptions were discarded entirely. The old failure reply conflated
  unavailable search/results with unavailable Internet.
- `python.org/downloads/` returned HTTP 200 with Content-Encoding gzip even
  when identity was requested. The original stdlib fetcher passed compressed
  bytes to the HTML parser: empty title and corrupted text, in both layouts.
- The local-path query guard matched the `s:/` suffix of `https://`. A word
  boundary now preserves the local drive restriction without rejecting HTTPS.
  Conversational URLs also pass the same public-URL policy before logging or
  search, retaining private-host, credential, port and HTTPS restrictions.
- Natural freshness detection covered only a few named products, and the
  domain filter omitted Web schemas for ordinary current-version questions.

The existing fetcher now decodes gzip/deflate with bounded compressed AND
decoded size (8 MiB), rejects unsupported/corrupt encodings, and retains TLS
hostname/certificate verification and pinned public DNS. No CA policy change,
`verify=False`, proxy bypass of safety checks or new downloader dependency.
The existing cache is best-effort: write failure cannot discard a valid source.
If the local model's paraphrase fails validation on a version question, the
extractive fallback prioritizes a retrieved version-bearing line over a generic
navigation/download label. It does not manufacture a version or elevate stale data.

Request-local diagnostics retain query, provider attempts, HTTP status,
DNS/TLS stages, byte counts, ranking exclusions, extraction errors, selected
URLs, refinement and completion. They are available in the existing Hardware
status research snapshot and structured `kazumi.web_research` log. Headers,
cookies, bodies and underlying exception messages are excluded. Queries pass
the existing privacy filter; URL query strings are removed from fetch logs.

Connectivity is evidence-based: ONLINE, SEARCH_PROVIDER_DEGRADED,
DIRECT_FETCH_ONLY, TLS_ERROR, DNS_ERROR or TIMEOUT. Provider failure alone
never establishes OFFLINE. Search and direct-document success remain distinct.
Explicit public URLs bypass search; known product indexes remain fallback
entrypoints, not invented results. Python's official downloads page is an
additional index; no release version is hardcoded.

The small Web conversation bridge reuses the same research service and executes
the registered `web_research` tool inside the current TurnContext. It records
tool results/provenance and returns through the existing persona/emotion/output
pipeline. A dedicated Web domain offers only Web schemas, not arbitrary shell.
Freshness requests are no longer product-specific. Last public research query
context is bounded to 32 sessions / 30 minutes and never reused as factual
evidence. An unresolved “pesquisa isso” asks for a subject instead of inventing
an Internet outage. Hardware/device and filesystem intents keep their owners.

Targeted tests cover encoding, compressed size limits, error classification,
realistic provider fallback, direct URLs, cache/parser failure containment,
privacy, query refinement, Web schemas, session isolation and result grounding.
Real HTTPS validation also forces only the primary search provider to fail,
then uses real Bing and official documentation; this is not simulated Internet.

### Final validation

- 176 targeted tests passed across Web runtime/specificity, routing, hardware
  grounding/continuation, natural conversation and packaging policy.
- PyInstaller and the official Tauri release build passed. Source fingerprints
  matched both final build markers; the packaged and bundled backend executables
  had identical SHA-256 hashes. No packaging/CA configuration change was needed.
- Text submitted through the actual KAZUMI.lnk WebView returned PlatformIO 6.1.19
  from `https://pypi.org/pypi/platformio/json`, followed by the specific `pio run`
  command documentation in the same conversation. The final release's Python
  question returned the retrieved Python 3.14.7 line from
  `https://www.python.org/downloads/`; these are observed values, not defaults.
- A direct conversational URL request in the final packaged runtime returned
  the `pio run` command explanation with provenance, no search queries, and
  `DIRECT_FETCH_ONLY`. Direct fetch also returned the correct title and decoded
  text without replacement characters.
- Live DNS, verified TLS and HTTPS succeeded. DuckDuckGo remained HTTP 202;
  real Bing RSS fallback succeeded. A controlled primary-provider failure used
  real Bing/HTTPS, and an actual HTTP 404 remained a contained tool error while
  the backend stayed running. No tested response falsely claimed no Internet.
- The historical total failure remains unconfirmed: it did not recur for the
  two exact PlatformIO questions on the original release. The reproduced
  fetch, routing and diagnostic defects above are the evidence for this patch.

Local validation reports, runtime artifacts and the four preexisting VTS edits
are deliberately excluded from this hotfix commit.
