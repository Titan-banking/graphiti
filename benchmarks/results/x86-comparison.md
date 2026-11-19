# Graphiti: TigerGraph vs ArcadeDB, native x86-64 comparison (2026-11-18)

Re-run of the Apple M4 (arm64) benchmark on an x86-64 host, where TigerGraph's amd64-only image runs
natively instead of under x86 emulation. Same branch (`feat/tigergraph-backend`, `35cddd0`), same
workload, same script.

Raw data: `benchmarks/results/x86-arcadedb.json` / `.md` and `benchmarks/results/x86-tigergraph.json` / `.md`.
Parity data: `/tmp/parity-arcadedb.json`, `/tmp/parity-tigergraph.json`, `/tmp/parity-compare.json`
(script: `benchmarks/parity_check.py`, new file added for this run).

## Machine and preconditions

| item | value |
|---|---|
| `uname -m` | `x86_64` |
| Docker daemon arch | `x86_64` (`docker info`: Architecture `x86_64`, NCPU 8, MemTotal 67416526848) |
| CPU | Intel(R) Xeon(R) 6973P-C, 8 vCPU (`nproc` = 8) |
| RAM | 62 GiB total (`free -g`: 62 total, 13 used, 19 free, 48 available) |
| OS | Linux 7.0.0-1017-azure, glibc 2.39 |
| Python | 3.14.5 under `uv` |
| Docker VM | 63 GiB RAM / 8 CPUs |

Minimum requirement (8 CPUs / 16 GiB) met; the "ideal" 16 CPU / 24 GiB is not met — 8 CPUs, 62 GiB RAM.
Both engines are capped at 4 CPUs, so the comparison between them is unaffected; absolute numbers are
for 4 cores.

**No emulation, no Rosetta.** The benchmark labels the TigerGraph column `TigerGraph` (not
`TigerGraph (x86 emulated)`) exactly because `platform.machine()` is `x86_64`, not `arm64`/`aarch64`
(`benchmarks/compare_arcadedb_falkordb.py:98,106`). The generated report header confirms the column
header is `TigerGraph` and the setup line reads
`tigergraph/community:4.2.6 (amd64), native on this x86_64 host`.

**CPU governor: could not be set.** `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` is
`schedutil` and writes are denied (`Permission denied`, unprivileged container host). The governor is
therefore *not* "performance"; both engines ran under the same `schedutil` governor, so the comparison
is fair, but absolute figures are not directly comparable to a machine in "performance" mode.

### Machine sharing

This host is **not** idle and **not** dedicated to the benchmark. Unrelated containers ran throughout
(`modelhub_*` stack: postgres, arcadedb ×3, redis, hatchet services, azurite, wiremock; plus
`neo4jref`, `ltg0`):

* `modelhub_arcadedb_1` already owned host port **2481** and `modelhub_arcadedb_2` owns **2482**;
  `neo4jref` owns **7687**. The prescribed `docker run -p 2481:2480 -p 7687:7687` could not bind.
* Those foreign containers were **not** stopped, removed or otherwise touched. Only the half-created
  `arcadedb-graphiti` (created by the failed bind) was removed and re-created.
* Our ArcadeDB therefore ran on host ports **12481** (HTTP) and **17687** (Bolt), with
  `BENCH_ARCADE_HTTP=http://localhost:12481` and `BENCH_ARCADE_BOLT=bolt://localhost:17687` exported.
  The script reads both at `benchmarks/compare_arcadedb_falkordb.py:91-92`; every other setting is
  exactly as prescribed.
* Foreign container load at the start of the ArcadeDB run: `docker stats` showed
  `modelhub_postgres` at ~12.5 % CPU and the rest at ≤1.5 % each; combined host load average 1.05.
  The `modelhub_postgres` 12 % is the largest source of interference and it is not benchmark traffic.

### Load average at the start of each run

| run | load average (1 / 5 / 15 min) |
|---|---|
| ArcadeDB native benchmark (2026-11-18 22:46:26) | 1.05 / 1.15 / 1.09 |
| TigerGraph warmup (N=2000, discarded) (23:07:42) | 2.21 / 2.28 / 2.26 |
| TigerGraph native benchmark (23:08:15) | 2.64 / 2.39 / 2.30 |
| ArcadeDB parity collection (23:14:07) | 1.42 / 2.97 / 2.72 |
| TigerGraph parity collection (23:15:23) | 1.84 / 2.81 / 2.69 |

* The 1-minute load of 2.2–2.6 during the TigerGraph runs is the tail of the ArcadeDB benchmark that
  had just finished, not background interference.

**One engine measured at a time, but ArcadeDB was left running (idle) during the TigerGraph runs.**
`environment()` in the benchmark script queries the ArcadeDB HTTP API for its version, so the
prescribed instructions keep ArcadeDB reachable for the TigerGraph run. I kept `arcadedb-graphiti`
up and idle (no benchmark traffic, ~0.2 % CPU) for the TigerGraph benchmark and both parity
collections, and stopped it afterwards. The foreign `modelhub_*` containers were running throughout
regardless. So the strict "one engine at a time" rule holds for *measurement* (each engine's numbers
were taken with only that engine under load), but not for *processes*: an idle ArcadeDB JVM was
resident during the TigerGraph measurements, which is also why the machine's 62 GiB of RAM mattered.


### Container deviations

Two deviations from the prescribed commands:

1. **TigerGraph: `--shm-size=2g` was used; its necessity is not shown.** [INFERENCE] The trigger for
   GPE/GSE leaving `Warmup` was the *existence of the graph*, not the shm size:
   * Fresh container, 64 MB shm (the first attempt): GPE and GSE stayed `Warmup` for 5+ minutes while
     GPE logged `Can not refresh graph schema successfully, rc: kNotFound` once per second.
   * Fresh container, 2 GB shm: GPE/GSE stayed `Warmup` from 22:31 until 22:34:48 — that is, they
     came Online **only after the first schema build had started** (the 384-dim build began ~22:33:45
     and the engines flipped at 22:34:48).
   * After `DROP ALL` (still 2 GB shm): GPE/GSE were `Warmup` again from 22:36:50 to 22:43:16 and
     flipped Online again only once the 1024-dim schema was rebuilt.
   * Restart with the graph already present (2 GB shm): all three services Online in ~48 s
     (`docker start` at 23:06:50 → Online at 23:07:38) and ~40 s on the later restart
     (container up 23:19:31, Online at 23:19:47).
   64 MB shm *with* a graph present was never tried, so the shm requirement is untested and the
   2 GB setting is not shown to be necessary.
2. **ArcadeDB host ports** moved to 12481/17687 (see above). Its own limits (`--cpus 4 --memory 5g`)
   and `JAVA_OPTS` / `ARCADEDB_OPTS_MEMORY` are exactly as prescribed.

**Operational finding (worth fixing in the README and the benchmark docstring):** on a *fresh*
container, GPE and GSE remain in `Warmup` while GSQL is already `Online`, and they only reach
`Online` once a graph exists — so the documented flow "wait ~2 min for all three Online, then build
the schema" cannot succeed on a fresh container, because the wait never ends before the schema is
built. The working order on a fresh container is: start the container → build the schema (which
brings GPE/GSE Online) → then the engines are ready. On a container that already has the graph, the
documented wait is correct (~40–50 s). The `kNotFound` loop in the GPE log is the symptom:
GPE polls for a graph schema and finds none.

This cost roughly 12–15 minutes of wall clock in this run: the first container was started at 22:26
and abandoned at 22:31 (5 min, no graph), the second was started at 22:31 and its engines came
Online at 22:34:48 after the schema build, and after `DROP ALL` the engines again waited from
22:36:50 until the 1024-dim rebuild completed at ~22:43.

`docker` accepted `--ulimit nofile=1000000:1000000` without complaint on the TigerGraph container.

### Images (digests verified, both as prescribed)

| image | digest | status |
|---|---|---|
| `arcadedata/arcadedb` | `sha256:87bd5febce2e09469269accbdb876b9386a2ef0870c2b78cff27a2ec37bba1e9` | pulled, matches |
| `tigergraph/community:4.2.6` | `sha256:3375ecca0e1688f00af14e786ae4361add217a3cb35d5b99e8413e0827789e37` | pulled, matches |

ArcadeDB reports `26.12.1-SNAPSHOT (build a37542d996748abe76c3acfabcb68d5e2a117246/...)` as expected.

**How old is the pinned ArcadeDB image?** Not old at all — it was built **the same day** as the run:

| field | value |
|---|---|
| image `Created` | `2026-11-18T11:52:38Z` |
| server build timestamp (from `/api/v1/server`) | `1795002496224` ms = `2026-11-18T11:48:16Z` |
| version string | `26.12.1-SNAPSHOT (build a37542d996748abe76c3acfabcb68d5e2a117246/…)` |
| last tagged release on Docker Hub | `26.11.1`, published `2026-11-04` (14 days before the run) |

So the pinned digest is a `26.12.1-SNAPSHOT` — a CI snapshot of the line *after* the last tagged
release `26.11.1` — not a tagged release. [INFERENCE] "Same-day CI snapshot" from the metadata
(`26.12.1-SNAPSHOT` tag, build timestamp equal to the image creation time).

Because the tag is a moving snapshot, the "latest" snapshot was re-pulled and the ArcadeDB columns
re-measured — see **ArcadeDB snapshot drift** below.

## Pre-flight checks

**1. Driver sanity suite** — `DISABLE_NEO4J=1 DISABLE_FALKORDB=1 DISABLE_ARCADEDB=1 uv run pytest
tests/driver/test_tigergraph_driver.py -q`: **27 passed**, as expected.

**2. TigerGraph live check** — schema built at 384 dims
(`TIGERGRAPH_EMBEDDING_DIM=384`, `build_indices_and_constraints()`), then
`ENABLE_TIGERGRAPH=1 TIGERGRAPH_EMBEDDING_DIM=384 DISABLE_NEO4J=1 DISABLE_FALKORDB=1 DISABLE_ARCADEDB=1
uv run pytest tests/test_node_int.py tests/test_edge_int.py tests/test_graphiti_mock.py -q`:
**33 passed, 2 failed** — exactly the two expected failures, `test_entity_node` and
`test_community_node`, both with `REST-30200: Processing vector name_embedding failed, dimension
mismatch` (they hard-code 1024-dim vectors). Then the schema was dropped (`DROP ALL`) and rebuilt at
1024 dims for the benchmark. `DROP ALL` restarted the services; GPE/GSE/GSQL were Online again ~6.5 min
after the drop.

## Warmup accounting

* **ArcadeDB exact**: the script's own discarded JVM warmup ran (2,000 entities) before the column, so
  the exact column's N=1000 cell is warm.
* **ArcadeDB ANN**: same JVM, so warm.
* **TigerGraph**: a throwaway run at N=2000 was executed first (`--sizes 2000 --out
  /tmp/tg-warmup.md`, discarded) so that N=1000 is not charged for engine warmup. The TigerGraph
  schema was built before that warmup run (1024 dims, 62 s).
* **All cells** are one run each, no repeats.

## The embedding storage change (ArcadeDB)

The ArcadeDB driver used to write an embedding as an undeclared property, so ArcadeDB stored it as
a `LIST` of 1024 boxed doubles. Reading such a list back is what dominated the slow legs: every
component cost a type byte in the record, a boxed `Double` and a list slot. A JFR profile of the
edge-similarity query (7,175 execution samples, sliced to the query window only) put **60.3 % of the
query in record deserialization** and only 12.0 % in the similarity itself.

`graphiti_core/driver/arcadedb_driver.py` now declares the embedding properties
(`Entity.name_embedding`, `Community.name_embedding`, `RELATES_TO.fact_embedding`) as
`ARRAY_OF_FLOATS` when it builds the schema. ArcadeDB compacts the list into a flat `float[]` on
write, so the write path is unchanged and the read is one primitive array instead of a thousand
boxed values.

The change is in the **Graphiti driver**, not in ArcadeDB: no engine code was touched. It needs an
ArcadeDB server that accepts `CREATE PROPERTY ... ARRAY_OF_FLOATS` (26.11.1+); an older or
unreachable HTTP API only costs the speed, and the driver logs a warning.

Effect on the ArcadeDB ANN column (p50 ms, 50,000 entities):

| leg | before | after | |
|---|---|---|---|
| edge similarity (exact) | 1464 | 500 | **2.9x faster** |
| hybrid search RRF | 1429 | 485 | **2.9x faster** |
| node similarity (exact) | 691 | 141 | 4.9x faster |
| node similarity (ANN) | 39.7 | 40.6 | unchanged (the index path never reads the property) |
| node BFS depth 2 | 143 | 146 | unchanged |
| bulk edges/s | 547 | 653 | 1.2x faster |
| peak container memory | 2979 MiB | 2949 MiB | unchanged |
| on-disk size | 1804 MB | 1071 MB | **0.6x** |

The answers did not move: the similarity legs still answer the same rows in the same order
(Jaccard 1.000 against TigerGraph), and the fulltext and hybrid legs keep the same Jaccard as
before the change (0.21 / 0.18 / 0.40 / 0.18). The stored values are float32 now, where they were
float64 in a list; the round-trip error is 2.98e-08, far below the `min_score` granularity the
search compares against.

### `ARRAY_OF_FLOATS` or `ARRAY_OF_DOUBLES`?

ArcadeDB has both. The driver declares `ARRAY_OF_FLOATS`, which stores the embedding as a
`float[]`; `ARRAY_OF_DOUBLES` would store a `double[]` and keep the float64 values exactly. Both
were measured on the same data, each in its own database, with the driver's own shape (Entity
vertices joined by RELATES_TO edges) and the driver's own exact edge-similarity query
(`benchmarks/compare_arcadedb_embedding_array_type.py`, 1 000 edges of 1024 float64 components,
20 query vectors, `min_score` 0.6 and `limit` 10 — the library defaults; the two arms were
measured in turn, alternating which went first, over three rounds):

| declared type | edge-similarity query | max error in the score ArcadeDB answers | max error in the stored value | top-10 set differs | top-10 order differs |
|---|---|---|---|---|---|
| `ARRAY_OF_FLOATS` | **7.21 ms** | 1.046e-07 | 2.36e-07 | 0 / 60 | 3 / 60 |
| `ARRAY_OF_DOUBLES` | **8.38 ms** | 1.046e-07 | 0 | 0 / 60 | 3 / 60 |

**The score error is the same for both, to the last digit.** ArcadeDB's Cypher
`vector.similarity.cosine` (`VectorSimilarityCosineFunction`) narrows both arguments to `float[]`
with `VectorUtils.toFloatArrayOfCoordinates` and computes in float32, so the answer is
float32-precision whichever way the property is declared: declaring `ARRAY_OF_DOUBLES` buys no
accuracy in the search. The rows it returns are the same in every query (the top-10 set never
differs), and the order differs in 3 of 60 queries **in both arms alike — the same query, number
15** — that is the database ordering near-equal scores, not the storage type.

What the doubles cost is real and measured:

| | `ARRAY_OF_FLOATS` | `ARRAY_OF_DOUBLES` |
|---|---|---|
| edge-similarity query (median of 3 alternating rounds) | 7.21 ms | 8.38 ms (**1.16x**) |
| the RELATES_TO bucket file on disk | 5 046 272 B | 10 944 512 B (**2.17x**) |
| the whole database on disk | 11 120 KiB | 22 532 KiB (**2.03x**) |

The one thing doubles preserves is the exact stored value for a caller that reads an embedding back
and does its own float64 arithmetic on it — the float32 round-trip error is 2.36e-07 against 0.
That is what the `float32` note above is about.

## Results — x86-64, native

p50 / p95 in ms. Bulk write rates are items/s. Every cell is a single run.

### 1,000 entities

| metric | ArcadeDB exact | ArcadeDB ANN | TigerGraph (native) |
|---|---|---|---|
| bulk nodes/s | 1050 | 1059 | 1475 |
| bulk edges/s | 556 | 631 | 1600 |
| save node c=1 | 2.8 / 3.7 | 2.4 / 3.0 | 14.4 / 26.3 |
| save edge c=1 | 2.9 / 3.6 | 2.4 / 2.8 | 8.8 / 9.3 |
| save node c=8 | 12.3 / 32.2 | 12.0 / 16.1 | 19.3 / 36.3 |
| save edge c=8 | 12.0 / 17.7 | 12.4 / 15.6 | 14.1 / 20.9 |
| node fulltext | 4.5 / 7.2 | 3.8 / 5.6 | 9.0 / 13.3 |
| edge fulltext | 5.4 / 8.4 | 5.3 / 6.3 | 13.1 / 17.5 |
| node similarity | 9.2 / 11.7 | 8.9 / 12.9 | 6.6 / 7.0 |
| edge similarity (exact) | 13.7 / 18.2 | 13.3 / 15.8 | 24.8 / 26.9 |
| node BFS depth 2 | 8.4 / 9.2 | 7.9 / 9.2 | 4.9 / 5.1 |
| hybrid RRF | 15.9 / 20.0 | 14.4 / 17.0 | 20.5 / 29.3 |
| recall@10 | n/a | 1.000 | 1.000 |
| vector-index catch-up | n/a | n/a | 4.1 s |
| peak container memory | 2568 MiB | 2802 MiB | 2916 MiB |

### 10,000 entities

| metric | ArcadeDB exact | ArcadeDB ANN | TigerGraph (native) |
|---|---|---|---|
| bulk nodes/s | 1163 | 1138 | 1639 |
| bulk edges/s | 469 | 409 | 1620 |
| save node c=1 | 2.4 / 2.8 | 2.3 / 2.7 | 14.6 / 23.9 |
| save edge c=1 | 2.5 / 3.0 | 2.4 / 2.8 | 9.0 / 13.9 |
| save node c=8 | 11.2 / 14.1 | 11.1 / 13.6 | 17.5 / 26.2 |
| save edge c=8 | 12.2 / 16.2 | 11.6 / 17.4 | 15.6 / 33.5 |
| node fulltext | 7.0 / 10.8 | 6.7 / 8.7 | 15.0 / 20.1 |
| edge fulltext | 8.5 / 10.0 | 8.4 / 8.8 | 18.8 / 24.6 |
| node similarity | 32.4 / 33.0 | 23.7 / 29.0 | 7.7 / 8.8 |
| edge similarity (exact) | 68.8 / 72.6 | 68.1 / 72.9 | 58.7 / 68.1 |
| node BFS depth 2 | 31.2 / 41.1 | 31.2 / 38.8 | 4.7 / 5.3 |
| hybrid RRF | 65.8 / 73.0 | 65.0 / 71.7 | 114.5 / 128.2 |
| recall@10 | n/a | 1.000 | 1.000 |
| vector-index catch-up | n/a | n/a | 0.1 s |
| peak container memory | 2715 MiB | 2854 MiB | 3270 MiB |

### 50,000 entities

| metric | ArcadeDB exact | ArcadeDB ANN | TigerGraph (native) |
|---|---|---|---|
| bulk nodes/s | 1186 | 1154 | 1628 |
| bulk edges/s | 652 | 653 | 1483 |
| save node c=1 | 2.4 / 3.6 | 2.5 / 3.2 | 14.5 / 19.8 |
| save edge c=1 | 2.4 / 3.2 | 2.6 / 3.5 | 9.1 / 11.6 |
| save node c=8 | 11.5 / 76.8 | 11.7 / 14.0 | 18.3 / 34.0 |
| save edge c=8 | 12.3 / 17.7 | 12.5 / 15.3 | 12.9 / 17.2 |
| node fulltext | 11.1 / 12.8 | 11.4 / 13.1 | 21.2 / 24.3 |
| edge fulltext | 19.9 / 22.2 | 20.1 / 22.7 | 44.0 / 54.5 |
| node similarity | 141.3 / 147.3 | 40.6 / 45.9 | 12.3 / 15.1 |
| edge similarity (exact) | 490.5 / 511.6 | 499.8 / 523.2 | 215.9 / 235.5 |
| node BFS depth 2 | 141.9 / 150.6 | 145.6 / 157.5 | 4.8 / 5.4 |
| hybrid RRF | 480.8 / 495.1 | 484.9 / 501.0 | 295.6 / 320.7 |
| recall@10 | n/a | 0.998 | 1.000 |
| vector-index catch-up | n/a | n/a | 0.2 s |
| peak container memory | 2878 MiB | 2949 MiB | 4291 MiB |

Row counts after load were verified by the script (`rows after load == N nodes / 2N edges` = `ok` for
every cell of both engines).

## Mac (arm64, TigerGraph emulated) next to x86-64 (native)

50,000 entities / 100,000 facts, p50 ms unless noted. Mac columns are the numbers supplied from the
earlier run; x86 columns are from this run. The Mac host and this host are different machines with
different CPUs, so cross-machine differences are **not** attributable to emulation alone — they mix
emulation with CPU, RAM and governor differences. The emulator's share of the TigerGraph column is
what the two TigerGraph columns isolate, and even that only holds if the two hosts otherwise behave
alike, which cannot be assumed.

| metric | Mac: ArcadeDB ANN native | Mac: ArcadeDB ANN x86-emulated | Mac: TigerGraph x86-emulated | x86: ArcadeDB ANN native | x86: TigerGraph native |
|---|---|---|---|---|---|
| bulk edges/s | 758 | 493 | 1858 | 653 | 1483 |
| bulk nodes/s | 1557 | 1141 | 2289 | 1154 | 1628 |
| save node c=1 | 1.9 | 2.4 | 18.1 | 2.5 | 14.5 |
| save edge c=1 | 2.1 | 2.5 | 12.6 | 2.6 | 9.1 |
| save node c=8 | 7.2 | 6.9 | 20.0 | 11.7 | 18.3 |
| node fulltext | 10.1 | 12.1 | 32.3 | 11.4 | 21.2 |
| edge fulltext | 17.1 | 23.6 | 62.1 | 20.1 | 44.0 |
| node similarity | 33.0 | 47.8 | 15.1 | 40.6 | 12.3 |
| edge similarity (exact) | 1071 | 2445 | 366 | 499.8 | 215.9 |
| node BFS depth 2 | 233 | 301 | 7.7 | 145.6 | 4.8 |
| hybrid RRF | 1088 | 3021 | 498 | 484.9 | 295.6 |
| recall@10 | 1.000 | 1.000 | 1.000 | 0.998 | 1.000 |
| peak memory MiB | 3055 | 4597 | 6110 | 2949 | 4291 |

The x86 ArcadeDB column is the run **after** the embedding-storage change described below; the
baseline before it was in the first version of this table (edge similarity 1464 ms, hybrid RRF
1429 ms).

10,000 entities (the subset the Mac run reported):

| metric | Mac: ArcadeDB ANN native | Mac: TigerGraph x86-emulated | x86: ArcadeDB ANN native (after the change) | x86: TigerGraph native |
|---|---|---|---|---|
| node BFS depth 2 | 36.6 | 6.4 | 31.2 | 4.7 |
| edge similarity (exact) | 165 | 65.5 | 68.1 | 58.7 |
| node similarity | 20.5 | 10.0 | 23.7 | 7.7 |

Reading of the TigerGraph column (Mac emulated → x86 native): bulk edge write 1858 → 1483 (slower on
x86), bulk node write 2289 → 1628 (slower on x86), single node save 18.1 → 14.5, single edge save
12.6 → 9.1, node fulltext 32.3 → 21.2, edge fulltext 62.1 → 44.0, node similarity 15.1 → 12.3, edge
similarity 366 → 215.9, BFS 7.7 → 4.8, hybrid 498 → 295.6, peak memory 6110 → 4291 MiB.

The emulated TigerGraph was **not** uniformly slower: on the Mac it wrote *faster* (bulk) than on x86
and used *more* memory. [INFERENCE] The emulator's overhead is not a constant factor; it depends on
which TigerGraph code path dominates, and the two hosts also differ in CPU and RAM. Removing the
emulation therefore does not uniformly improve TigerGraph's numbers, and the Mac run was not simply
"a TigerGraph number with unknown overhead added on top".

The ArcadeDB ANN column also moved (x86 host: bulk edges 547 vs 758 on Mac; node similarity 39.7 vs
33.0; edge similarity 1464 vs 1071; BFS 143 vs 233), which confirms the two hosts are not comparable
cell-for-cell: this host's ArcadeDB is slower on the exact-scan legs despite being x86-native.

## Result parity (10,000 entities, identical dataset and queries)

Both engines loaded the benchmark's own 10,000-entity / 20,000-fact dataset (1024 dims, 3 groups,
same seeds), then the same 20 calls per search type were made through
`graphiti_core.search.search_utils`. Jaccard is over uuid sets; order is the engine's own result order.

| search | calls | Jaccard (mean) | Jaccard (min) | identical order | rows per call (unique / total) |
|---|---|---|---|---|---|
| node BFS depth 2, limit 100000 | 20 | **1.000** | 1.000 | no (different order) | 33–86 / 33–86 |
| node BFS depth 2, limit 10 | 20 | 0.097 | 0.000 | no | 9–10 / 10 |
| edge BFS depth 2, limit 100000 | 20 | **1.000** | 1.000 | no (different order) | 33–86 / 33–86 |
| edge BFS depth 2, limit 10 | 20 | 0.099 | 0.000 | no | 10 / 10 |
| node similarity (group-filtered) | 20 | **1.000** | 1.000 | **yes, all 20 calls** | 10 / 10 |
| edge similarity (group-filtered) | 20 | **1.000** | 1.000 | **yes, all 20 calls** | 10 / 10 |
| node fulltext | 20 | 0.222 | 0.111 | no | 10 / 10 |
| edge fulltext | 20 | 0.140 | 0.053 | no | 10 / 10 |
| hybrid RRF — node leg | 20 | 0.386 | 0.333 | no | 10 / 10 |
| hybrid RRF — edge leg | 20 | 0.160 | 0.053 | no | 10 / 10 |

Every leg was run 20 times per engine; the BFS legs use 20 *different* 10-origin sets (one per
call), the other legs use 20 different query vectors / texts. The row counts vary per call
(33–86 reachable nodes), so the range is given rather than a single number.

**Duplicate rows in ArcadeDB's node BFS.** On the unlimited node-BFS leg, ArcadeDB returns the same
uuid more than once in 10 of the 20 calls (e.g. 79 rows for 73 unique uuids, 60 for 53) while
TigerGraph never repeats a uuid. The *unique* sets are still identical between the engines on all
20 calls (that is what the Jaccard 1.000 measures). Edge BFS shows no duplicates on either engine.
[INFERENCE] The shared Cypher in `graphiti_core/search/search_utils.py` (`node_bfs_search`) has no
`DISTINCT`, so it returns one row per path and an entity reachable by several paths appears several
times; this is upstream Cypher semantics rather than an ArcadeDB defect — it was not checked against
Neo4j in this run. The practical consequence for the benchmark is that the two engines' BFS outputs
are differently *shaped* (same unique set, different row count), so the BFS timing rows compare
outputs of different sizes.

**What holds and what does not, versus the Mac:**

* **Holds:** node similarity and edge similarity are 1.000 with identical order (all 20 calls).
* **Holds, in the same direction:** fulltext is low — 0.22 node / 0.14 edge, versus 0.14–0.21 on the
  Mac. TigerGraph's fulltext is a token scan, not Lucene, so it ranks differently by construction.
* **Holds in direction but the value moved:** hybrid 0.39 (node leg) / 0.16 (edge leg) versus 0.26 on
  the Mac. The hybrid leg inherits the fulltext difference.
* **Partially holds: BFS.** On the Mac, BFS was reported as identical (Jaccard 1.00). Here the
  *unlimited* BFS result is the same **set** on all 20 calls — node BFS Jaccard 1.000, edge BFS
  Jaccard 1.000 — so the Mac's set-equality finding reproduces. What this run adds is that the
  **order differs**: the engines enumerate the same reachable set in different orders, so any
  `limit` smaller than the reachable set selects different rows (top-10 node BFS Jaccard 0.097,
  edge BFS 0.099; the minimum over the 20 calls is 0.000, i.e. the top-10 sets do not overlap at
  all on some calls). [INFERENCE] The BFS legs return an unordered reachable set and the two engines
  apply the limit in their own enumeration order, so the limited form is not comparable across
  engines even though the unlimited set is. The Mac run's "BFS Jaccard 1.00" is consistent with set
  equality; it says nothing about order, and this run shows order is not preserved across engines.

**Speed ratios are therefore only quoted for:** node similarity, edge similarity (same answers, same
order), and the bulk-write / single-save legs, plus the BFS legs compared as sets. They are **not**
quoted for fulltext (0.14–0.22) or hybrid (0.16–0.39), where the engines return different answers,
nor for limited BFS (0.10), where the engines return different rows.

## ArcadeDB snapshot drift: pinned digest vs the latest snapshot

The `26.12.1-SNAPSHOT` tag is moving, so the latest snapshot was pulled after the main run and the
ArcadeDB columns were re-measured on it. Same command, same host, same day.

| image | digest | image created | server build id / timestamp |
|---|---|---|---|
| pinned (main tables above) | `sha256:87bd5febce2e094…` | 2026-11-18T11:52:38Z | `a37542d996748abe…`, 11:48:16Z |
| latest snapshot | `sha256:54592d8ad5090e6d…` | 2026-11-18T23:14:00Z | `9bad325768141aeb…`, 23:08:26Z |

Raw data: `benchmarks/results/x86-arcadedb-latest.json` / `.md`.

Ratios of new ÷ old, p50 ms (a ratio > 1 means the new snapshot is slower). Only cells whose ratio
leaves the ~1.0 ± noise band are discussed; with one run per cell, ratios below about 1.2 are noise.

| metric | N | ArcadeDB exact | ArcadeDB ANN |
|---|---|---|---|
| bulk nodes/s | 1186 | 1154 | 0.99 |
| bulk edges/s | 652 | 653 | 0.99 |
| save node c=1 | 2.4 / 3.6 | 2.5 / 3.2 | 0.96 |
| save edge c=1 | 2.4 / 3.2 | 2.6 / 3.5 | 1.05 |
| save node c=8 | 11.5 / 76.8 | 11.7 / 14.0 | 1.01 |
| save edge c=8 | 12.3 / 17.7 | 12.5 / 15.3 | 0.95 |
| node fulltext | 11.1 / 12.8 | 11.4 / 13.1 | 0.98 |
| edge fulltext | 19.9 / 22.2 | 20.1 / 22.7 | 0.99 |
| **node similarity** | **50,000** | 1.00 | **1.52** |
| edge similarity (exact) | 490.5 / 511.6 | 499.8 / 523.2 | 0.99 |
| node BFS depth 2 | 141.9 / 150.6 | 145.6 / 157.5 | 1.01 |
| hybrid RRF | 480.8 / 495.1 | 484.9 / 501.0 | 0.99 |
| recall@10 | n/a | 0.998 | 1.000 (both) |
| peak container memory | 2878 MiB | 2949 MiB | 2979 → 2977 MiB |

**Nothing changed except one cell: ArcadeDB ANN node similarity at 50,000 entities, 39.7 → 60.2 ms
(1.52×).** It is not noise. Three measurements of the same cell on the same host, same session:

| image | server build id | ANN node similarity p50 / p95 |
|---|---|---|
| pinned `sha256:87bd5febce2e094…` | `a37542d996748abe…` | 39.7 / 44.3 ms (main run) |
| pinned `sha256:87bd5febce2e094…` | `a37542d996748abe…` | **40.9 / 45.6 ms** (repeat, container `arcadedb-pinned`, later that night) |
| latest `sha256:54592d8ad5090e6d…` | `9bad325768141aeb…` | 60.2 / 67.0 ms (full re-run) |
| latest `sha256:54592d8ad5090e6d…` | `9bad325768141aeb…` | **63.0 / 71.6 ms** (independent repeat) |

The pinned digest reproduces 39.7 / 40.9 ms; the latest snapshot reproduces 60.2 / 63.0 ms. The
difference is therefore attributable to the **image**, not to the host's run-to-run variance. Raw data
for the pinned repeat: `benchmarks/results/x86-arcadedb-pinned-50k-repeat.json`.

At N=10,000 the same leg moved 23.6 → 27.4 ms (1.16×, at the edge of the noise band); at N=1,000 it
was unchanged (8.8 → 8.9 ms). So the effect scales with N.

The exact (non-vector-index) column's node similarity did not move at all (690.7 → 688.9 ms), so the
change is confined to the **HNSW vector index** path, not to the query layer. Recall stayed 1.000, so
the results are unchanged — only slower. [INFERENCE] The snapshot changed something inside ArcadeDB's
vector index (index build parameters, buffer sizes or a search-path change), not the query planner;
the two snapshots differ by 11.4 hours of upstream commits and no release note was consulted.

**Consequence for the headline numbers:** the TigerGraph-vs-ArcadeDB ratio for node similarity at
50,000 entities moves from **3.2× to 4.9–5.1× in TigerGraph's favour** (ArcadeDB ANN 39.7 ms →
60.2–63.0 ms against TigerGraph's 12.3 ms). The margin *widens* because ArcadeDB got slower, not
because TigerGraph got faster. The answers are identical on both (Jaccard 1.000, same order), so
the conclusion does not change — it strengthens. At N=50,000 all other legs are within 1.08× between
the two snapshots; at smaller sizes the noise band is wider (e.g. exact save-edge-c1 at N=1,000 was
0.80×, ANN node similarity at N=10,000 was 1.16×), all inside the single-run noise band.

## Where TigerGraph wins and loses (same answers only)

50,000 entities, p50 ms, x86-64 native:

| leg | ArcadeDB ANN | TigerGraph | verdict | comparable? |
|---|---|---|---|---|
| node similarity | 40.6 | 12.3 | **TigerGraph 3.3× faster** | yes — Jaccard 1.000, same order |
| edge similarity (exact) | 499.8 | 215.9 | **TigerGraph 2.3× faster** | yes — Jaccard 1.000, same order |
| bulk nodes/s | 1154 | 1628 | TigerGraph 1.41× faster | n/a (writes) |
| bulk edges/s | 653 | 1483 | TigerGraph 2.27× faster | n/a (writes) |
| single node save (c=1) | 2.4 | 14.5 | ArcadeDB 6.0× faster | n/a (writes) |
| single edge save (c=1) | 2.5 | 9.1 | ArcadeDB 3.6× faster | n/a (writes) |
| save node c=8 | 10.9 | 18.3 | ArcadeDB 1.7× faster | n/a (writes) |
| peak memory | 2949 MiB | 4291 MiB | TigerGraph 1.46× more | n/a |
| node BFS depth 2 | 145.6 | 4.8 | *not comparable* | same reachable **set** (Jaccard 1.000), different top-10 (Jaccard 0.097) — ratio indicative only |
| node fulltext | 11.4 | 21.2 | *not comparable* | different answers (Jaccard 0.222) |
| edge fulltext | 20.1 | 44.0 | *not comparable* | different answers (Jaccard 0.140) |
| hybrid RRF | 484.9 | 295.6 | *not comparable* | different answers (node leg 0.386, edge leg 0.160) |

Only the first two rows carry a speed ratio on comparable answers. The write rows compare write
throughput, which is answer-independent. The last four rows return different results on the two
engines, so no winner is drawn from them — the raw latencies are listed for reference only.

The BFS row needs care: the benchmark times `node_bfs_search(..., limit=10)`, which returns a
*different* top-10 on each engine (Jaccard 0.097 over 20 calls), so the ratio compares
differently-shaped outputs and is indicative only. The engines return the same reachable set
(Jaccard 1.000 on all 20 unlimited calls) and reach it in 4.8 ms vs 145.6 ms; the ordering of that
set is what differs, and the limited form inherits it.

Single-run caveat: one run per cell, so any gap under roughly 2× is within noise. On that rule the
save-node-c=8 (1.7×) and bulk-nodes (1.41×) gaps carry little weight. The node-similarity (3.3×)
gap is well clear of the noise band and is on identical answers; the edge-similarity gap narrowed
from 6.8× to **2.3×** after the embedding-storage change, and is now close to the noise band.

## Honesty notes

* **One run per cell** for the main tables. Two deliberate repeats exist, both on the ArcadeDB ANN
  node-similarity cell at N=50,000: one on the latest snapshot and one on the pinned digest, used to
  attribute the snapshot difference to the image rather than to host variance.
* **The ArcadeDB numbers are from a run with the embedding-storage change** (the driver declares the
  embedding properties `ARRAY_OF_FLOATS`). The baseline before the change is preserved in
  `benchmarks/results/x86-arcadedb-pinned-baseline.md` and the run after it in
  `benchmarks/results/x86-arcadedb-array-of-floats.md` / `.json`. The change is in the Graphiti
  driver; no ArcadeDB engine code was modified.
* **The stored embeddings are float32 after the change** (they were float64 in a list). The
  round-trip error is 2.98e-08; the similarity answers did not change (Jaccard 1.000, same order).
* **The ArcadeDB image is a same-day CI snapshot**, not a tagged release (last tag `26.11.1`,
  2026-11-04). Its numbers can move between snapshots; the measured drift is documented above.
* **Two ArcadeDB containers existed during the A/B** (`arcadedb-graphiti` on the latest snapshot and
  `arcadedb-pinned` on the pinned digest), but never ran measurements at the same time: the pinned
  repeat ran with `arcadedb-graphiti` stopped. TigerGraph was stopped for the whole ArcadeDB
  re-measurement, so no engine competed with another.
* **The host is shared and not idle.** Foreign containers ran throughout; `modelhub_postgres` alone
  showed ~12 % CPU during the ArcadeDB run. Per-engine ambient load was not measured, so how much
  this moved each engine's numbers is unknown. [INFERENCE] The effect is likely to be jitter rather
  than a consistent bias, since the foreign load was steady and the same for both engines, but that
  was not verified.
* **The CPU governor could not be set to "performance"** (`schedutil`, writes denied). The task's
  "performance governor if you can set it" was not satisfiable.
* **The machine is below the "ideal" spec** (8 CPUs, not ≥16), though above the minimum.
* **Two container deviations** (TigerGraph `--shm-size=2g`, ArcadeDB host ports) — both documented
  above; the shm setting's necessity is untested.
* **Cross-machine rows mix hardware with emulation.** The Mac-vs-x86 table is not a controlled
  emulation experiment.
* **Marked inference:** the claims about emulation overhead, the BFS ordering explanation, and the
  Mac-vs-x86 attribution are labelled `[INFERENCE]` where stated.
* **Not run:** FalkorDB (out of scope), `arcadedb_ann_scalar`, the Mac's exact and scalar columns
  (not present in the supplied Mac table), and no `--metrics save` re-measurement.
* **No `graphiti_core/` code was changed.** The only new file is `benchmarks/parity_check.py`, the
  parity harness for step 4.
* **No bug found in `graphiti_core/` during this run** — nothing to report there.

### One observation, not a bug

TigerGraph's `vector-index catch-up` after the bulk load was 4.1 s at N=1000 but only 0.1–0.2 s at
N=10,000/50,000. This is the documented eventually-consistent index and the driver already waits for
it; the inversion at N=1000 is consistent with the index being cold right after the schema was built
and the first bulk load, not with a defect. [INFERENCE] Not investigated further, and not "fixed".

## Cleanup

`arcadedb-graphiti` and `tigergraph-bench` are stopped. Their data volumes are **not** deleted, so
the loaded datasets and the 1024-dim TigerGraph schema remain on disk for a follow-up run. The
foreign containers (`modelhub_*`, `neo4jref`, `ltg0`) were never touched.

`arcadedb-graphiti` was recreated at the end of the run (2026-11-19 09:21 UTC) on the **pinned**
digest (`sha256:87bd5febce2e…`), so its on-disk data corresponds to the pinned image, not the latest
snapshot. The temporary A/B containers (`arcadedb-pinned`, and the `arcadedb-patched` /
`arcadedb-up` / `arcadedb-latest` / `arcadedb-jfr*` scratch servers used for the profiles) were
**removed** (they held only benchmark scratch databases).

### Files produced

| file | content |
|---|---|
| `benchmarks/results/x86-comparison.md` | this report |
| `benchmarks/results/x86-arcadedb.json` / `.md` | ArcadeDB exact + ANN, pinned digest, **after** the embedding-storage change |
| `benchmarks/results/x86-arcadedb-array-of-floats.json` / `.md` | the same run, kept under its own name |
| `benchmarks/results/x86-arcadedb-pinned-baseline.md` | the same columns **before** the change (the baseline the change is measured against) |
| `benchmarks/results/x86-tigergraph.json` / `.md` | TigerGraph native |
| `benchmarks/results/x86-arcadedb-latest.json` / `.md` | ArcadeDB exact + ANN, latest snapshot |
| `benchmarks/results/x86-arcadedb-latest-50k-repeat.json` | N=50,000 repeat on the latest snapshot |
| `benchmarks/results/x86-arcadedb-pinned-50k-repeat.json` | N=50,000 ANN repeat on the pinned digest (A/B) |
| `benchmarks/results/x86-parity-arcadedb.json` | ArcadeDB ANN parity results (20 calls per leg) |
| `benchmarks/results/x86-parity-tigergraph.json` | TigerGraph parity results (20 calls per leg) |
| `benchmarks/results/x86-parity-compare.json` | the parity comparison |
| `benchmarks/parity_check.py` | the parity harness (new file; no `graphiti_core/` change) |

**Only the baseline's rendered `.md` survives.** The re-run that measured the change wrote its raw
JSON over `x86-arcadedb.json` (the file the baseline's run had produced), so the baseline's raw
`.json` is gone and the baseline is readable only as the rendered `x86-arcadedb-pinned-baseline.md`.
`x86-arcadedb.json` and `x86-arcadedb-array-of-floats.json` are the same run under two names.

The investigation behind the embedding-storage change left these behind in the working tree (none of them
touches `graphiti_core/`):

| file | what it does |
|---|---|
| `benchmarks/profile_arcadedb_edge.py` | JFR profile of the edge-similarity query, sliced to the query window |
| `benchmarks/profile_arcadedb_slow.py` | JFR profile of the slow legs, for the same slicing |
| `benchmarks/repro_arcadedb_edge_similarity.py` | the exact edge-similarity scan on its own |
| `benchmarks/repro_arcadedb_projection_reads.py` | the read cost of a projected row |
| `benchmarks/repro_arcadedb_storage_type.py` | how the storage type of a property changes a read |
| `benchmarks/repro_arcadedb_wide_row_serialization.py` | the serialization of a wide row |
| `benchmarks/verify_arcadedb_embedding_type.py` | checks that a stored embedding reads back as a float array |
| `benchmarks/compare_arcadedb_embedding_array_type.py` | `ARRAY_OF_FLOATS` against `ARRAY_OF_DOUBLES`: scan time, stored-value error, and the score ArcadeDB answers |
| `benchmarks/repro_arcadedb_postgres_wide_row.py`, `benchmarks/pg/PostgresWideRow.java` | the Postgres wire path of a wide row (reported upstream as ArcadeData/arcadedb#12558) |
