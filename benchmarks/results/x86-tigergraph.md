# FalkorDB vs ArcadeDB benchmark (2026-11-18)

## Setup

- ArcadeDB: 26.12.1-SNAPSHOT (build a37542d996748abe76c3acfabcb68d5e2a117246/1795002496224/main), image arcadedata/arcadedb@sha256:87bd5febce2e09469269accbdb876b9386a2ef0870c2b78cff27a2ec37bba1e9
- FalkorDB image: unknown
- ArcadeDB ARCADEDB_OPTS_MEMORY: -Xms512m -Xmx2560m
- container limits: ArcadeDB 5 GB / 4 CPU, FalkorDB unknown, TigerGraph 8 GB / 4 CPU (docker), one engine running at a time
- ArcadeDB JAVA_OPTS: -Darcadedb.server.rootPassword=playwithdata -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin
- shared Docker VM: 63 GiB RAM / 8 CPUs, also running unrelated containers throughout
- host: Linux-7.0.0-1017-azure-x86_64-with-glibc2.39, unknown
- graphiti PR #1310 head (fetched): 15963f0616f29ea0bd5865033b47f1c22f1ff455
- this branch: 35cddd0ba2640f148c487a673ce86f1af41f2106
- python: 3.14.5
- embeddings: 64 clusters (centroid + noise), queries are a stored vector + noise
- FalkorDB TIMEOUT: raised from the 1000 ms default to 0 so slow exact scans are measured, not aborted
- ArcadeDB driver write_concurrency: None (no gate): the columns measure the engine under 8 concurrent writers; the driver default of 1 writer per database is measured in arcadedb_concurrent_writes.py
- TigerGraph: tigergraph/community:4.2.6 (amd64), native on this x86_64 host
- TigerGraph driver: one graph for every group, REST++ upserts (no transactions), installed GSQL queries; fulltext is a token scan, edge similarity an exact scan, node similarity HNSW (eventually consistent)
- dim=1024, reads: 200 queries after 20 warmup (50 for exact full scans), write concurrency 8, batch 500, limit 10, min_score 0.6 (the library default) for similarity
- latencies are `p50 / p95` in ms; ANN recall is recall@10 against the exact scan of the same data
- container memory is the peak `docker stats` reading: for ArcadeDB (a JVM) it mostly reflects heap and page-cache sizing, not data size. Disk is the database directory (ArcadeDB pre-sizes 16 bucket files per type) or, for FalkorDB, the growth of the RDB after SAVE.

| metric | N nodes | FalkorDB | ArcadeDB exact | ArcadeDB ANN | ArcadeDB ANN scalar | TigerGraph |
|---|---|---|---|---|---|---|
| bulk write nodes/s | 1000 | n/a | n/a | n/a | n/a | 1475 |
| bulk write nodes/s | 10000 | n/a | n/a | n/a | n/a | 1639 |
| bulk write nodes/s | 50000 | n/a | n/a | n/a | n/a | 1628 |
| bulk write edges/s | 1000 | n/a | n/a | n/a | n/a | 1600 |
| bulk write edges/s | 10000 | n/a | n/a | n/a | n/a | 1620 |
| bulk write edges/s | 50000 | n/a | n/a | n/a | n/a | 1483 |
| rows after load == N nodes / 2N edges | 1000 | n/a | n/a | n/a | n/a | ok |
| rows after load == N nodes / 2N edges | 10000 | n/a | n/a | n/a | n/a | ok |
| rows after load == N nodes / 2N edges | 50000 | n/a | n/a | n/a | n/a | ok |
| save node c=1 (ms) | 1000 | n/a | n/a | n/a | n/a | 14.4 / 26.3 |
| save node c=1 (ms) | 10000 | n/a | n/a | n/a | n/a | 14.6 / 23.9 |
| save node c=1 (ms) | 50000 | n/a | n/a | n/a | n/a | 14.5 / 19.8 |
| save edge c=1 (ms) | 1000 | n/a | n/a | n/a | n/a | 8.8 / 9.3 |
| save edge c=1 (ms) | 10000 | n/a | n/a | n/a | n/a | 9.0 / 13.9 |
| save edge c=1 (ms) | 50000 | n/a | n/a | n/a | n/a | 9.1 / 11.6 |
| save node c=8 (ms) | 1000 | n/a | n/a | n/a | n/a | 19.3 / 36.3 |
| save node c=8 (ms) | 10000 | n/a | n/a | n/a | n/a | 17.5 / 26.2 |
| save node c=8 (ms) | 50000 | n/a | n/a | n/a | n/a | 18.3 / 34.0 |
| save edge c=8 (ms) | 1000 | n/a | n/a | n/a | n/a | 14.1 / 20.9 |
| save edge c=8 (ms) | 10000 | n/a | n/a | n/a | n/a | 15.6 / 33.5 |
| save edge c=8 (ms) | 50000 | n/a | n/a | n/a | n/a | 12.9 / 17.2 |
| node fulltext (ms) | 1000 | n/a | n/a | n/a | n/a | 9.0 / 13.3 |
| node fulltext (ms) | 10000 | n/a | n/a | n/a | n/a | 15.0 / 20.1 |
| node fulltext (ms) | 50000 | n/a | n/a | n/a | n/a | 21.2 / 24.3 |
| edge fulltext (ms) | 1000 | n/a | n/a | n/a | n/a | 13.1 / 17.5 |
| edge fulltext (ms) | 10000 | n/a | n/a | n/a | n/a | 18.8 / 24.6 |
| edge fulltext (ms) | 50000 | n/a | n/a | n/a | n/a | 44.0 / 54.5 |
| node similarity (ms) | 1000 | n/a | n/a | n/a | n/a | 6.6 / 7.0 |
| node similarity (ms) | 10000 | n/a | n/a | n/a | n/a | 7.7 / 8.8 |
| node similarity (ms) | 50000 | n/a | n/a | n/a | n/a | 12.3 / 15.1 |
| edge similarity, exact (ms) | 1000 | n/a | n/a | n/a | n/a | 24.8 / 26.9 |
| edge similarity, exact (ms) | 10000 | n/a | n/a | n/a | n/a | 58.7 / 68.1 |
| edge similarity, exact (ms) | 50000 | n/a | n/a | n/a | n/a | 215.9 / 235.5 |
| node BFS depth 2 (ms) | 1000 | n/a | n/a | n/a | n/a | 4.9 / 5.1 |
| node BFS depth 2 (ms) | 10000 | n/a | n/a | n/a | n/a | 4.7 / 5.3 |
| node BFS depth 2 (ms) | 50000 | n/a | n/a | n/a | n/a | 4.8 / 5.4 |
| hybrid search RRF (ms) | 1000 | n/a | n/a | n/a | n/a | 20.5 / 29.3 |
| hybrid search RRF (ms) | 10000 | n/a | n/a | n/a | n/a | 114.5 / 128.2 |
| hybrid search RRF (ms) | 50000 | n/a | n/a | n/a | n/a | 295.6 / 320.7 |
| ANN recall@10 | 1000 | n/a | n/a | n/a | n/a | 1.000 |
| ANN recall@10 | 10000 | n/a | n/a | n/a | n/a | 1.000 |
| ANN recall@10 | 50000 | n/a | n/a | n/a | n/a | 1.000 |
| vector index catch-up after the bulk load | 1000 | n/a | n/a | n/a | n/a | 4.1 s |
| vector index catch-up after the bulk load | 10000 | n/a | n/a | n/a | n/a | 0.1 s |
| vector index catch-up after the bulk load | 50000 | n/a | n/a | n/a | n/a | 0.2 s |
| container memory / disk | 1000 | n/a | n/a | n/a | n/a | 2916 MiB peak / ? MB disk |
| container memory / disk | 10000 | n/a | n/a | n/a | n/a | 3270 MiB peak / ? MB disk |
| container memory / disk | 50000 | n/a | n/a | n/a | n/a | 4291 MiB peak / ? MB disk |

## ArcadeDB engine-fused search vs Graphiti Python RRF (informational, ungrouped)

| N nodes | Graphiti Python RRF p50 / p95 | engine `/vector/{db}/hybrid` p50 / p95 |
|---|---|---|
| 1000 | n/a | n/a |
| 10000 | n/a | n/a |
| 50000 | n/a | n/a |
