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
| bulk write nodes/s | 1000 | n/a | 1003 | 995 | n/a | n/a |
| bulk write nodes/s | 10000 | n/a | 1167 | 1195 | n/a | n/a |
| bulk write nodes/s | 50000 | n/a | 1195 | 1205 | n/a | n/a |
| bulk write edges/s | 1000 | n/a | 332 | 349 | n/a | n/a |
| bulk write edges/s | 10000 | n/a | 418 | 424 | n/a | n/a |
| bulk write edges/s | 50000 | n/a | 533 | 547 | n/a | n/a |
| rows after load == N nodes / 2N edges | 1000 | n/a | ok | ok | n/a | n/a |
| rows after load == N nodes / 2N edges | 10000 | n/a | ok | ok | n/a | n/a |
| rows after load == N nodes / 2N edges | 50000 | n/a | ok | ok | n/a | n/a |
| save node c=1 (ms) | 1000 | n/a | 2.6 / 3.0 | 2.5 / 3.3 | n/a | n/a |
| save node c=1 (ms) | 10000 | n/a | 2.5 / 3.2 | 2.3 / 3.7 | n/a | n/a |
| save node c=1 (ms) | 50000 | n/a | 2.2 / 8.2 | 2.4 / 3.1 | n/a | n/a |
| save edge c=1 (ms) | 1000 | n/a | 3.4 / 5.0 | 2.4 / 3.4 | n/a | n/a |
| save edge c=1 (ms) | 10000 | n/a | 2.5 / 3.0 | 2.5 / 3.5 | n/a | n/a |
| save edge c=1 (ms) | 50000 | n/a | 2.6 / 3.7 | 2.5 / 2.9 | n/a | n/a |
| save node c=8 (ms) | 1000 | n/a | 11.9 / 31.4 | 11.7 / 14.7 | n/a | n/a |
| save node c=8 (ms) | 10000 | n/a | 11.5 / 13.1 | 11.4 / 13.7 | n/a | n/a |
| save node c=8 (ms) | 50000 | n/a | 11.4 / 14.6 | 10.9 / 14.4 | n/a | n/a |
| save edge c=8 (ms) | 1000 | n/a | 12.1 / 14.6 | 11.5 / 14.7 | n/a | n/a |
| save edge c=8 (ms) | 10000 | n/a | 12.0 / 13.7 | 10.8 / 14.0 | n/a | n/a |
| save edge c=8 (ms) | 50000 | n/a | 11.8 / 14.6 | 12.4 / 19.0 | n/a | n/a |
| node fulltext (ms) | 1000 | n/a | 4.7 / 7.8 | 3.8 / 5.5 | n/a | n/a |
| node fulltext (ms) | 10000 | n/a | 6.9 / 9.6 | 6.5 / 7.3 | n/a | n/a |
| node fulltext (ms) | 50000 | n/a | 11.5 / 13.5 | 11.3 / 12.9 | n/a | n/a |
| edge fulltext (ms) | 1000 | n/a | 5.7 / 7.8 | 5.2 / 6.8 | n/a | n/a |
| edge fulltext (ms) | 10000 | n/a | 8.5 / 10.0 | 8.2 / 9.1 | n/a | n/a |
| edge fulltext (ms) | 50000 | n/a | 19.0 / 21.8 | 18.7 / 20.8 | n/a | n/a |
| node similarity (ms) | 1000 | n/a | 18.8 / 22.8 | 8.8 / 11.2 | n/a | n/a |
| node similarity (ms) | 10000 | n/a | 108.8 / 113.9 | 23.6 / 28.3 | n/a | n/a |
| node similarity (ms) | 50000 | n/a | 690.7 / 709.5 | 39.7 / 44.3 | n/a | n/a |
| edge similarity, exact (ms) | 1000 | n/a | 31.6 / 37.4 | 30.9 / 32.8 | n/a | n/a |
| edge similarity, exact (ms) | 10000 | n/a | 223.1 / 230.5 | 223.7 / 236.0 | n/a | n/a |
| edge similarity, exact (ms) | 50000 | n/a | 1460.3 / 1482.2 | 1464.2 / 1490.8 | n/a | n/a |
| node BFS depth 2 (ms) | 1000 | n/a | 9.9 / 12.7 | 7.7 / 8.3 | n/a | n/a |
| node BFS depth 2 (ms) | 10000 | n/a | 51.6 / 56.7 | 31.3 / 32.8 | n/a | n/a |
| node BFS depth 2 (ms) | 50000 | n/a | 463.7 / 561.0 | 143.0 / 160.8 | n/a | n/a |
| hybrid search RRF (ms) | 1000 | n/a | 27.5 / 35.4 | 27.4 / 30.9 | n/a | n/a |
| hybrid search RRF (ms) | 10000 | n/a | 218.2 / 228.7 | 218.7 / 227.0 | n/a | n/a |
| hybrid search RRF (ms) | 50000 | n/a | 1460.2 / 1496.4 | 1429.1 / 1454.3 | n/a | n/a |
| ANN recall@10 | 1000 | n/a | n/a | 1.000 | n/a | n/a |
| ANN recall@10 | 10000 | n/a | n/a | 1.000 | n/a | n/a |
| ANN recall@10 | 50000 | n/a | n/a | 1.000 | n/a | n/a |
| vector index catch-up after the bulk load | 1000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 10000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 50000 | n/a | n/a | n/a | n/a | n/a |
| container memory / disk | 1000 | n/a | 2693 MiB peak / 224 MB disk | 2793 MiB peak / 207 MB disk | n/a | n/a |
| container memory / disk | 10000 | n/a | 2866 MiB peak / 664 MB disk | 2895 MiB peak / 479 MB disk | n/a | n/a |
| container memory / disk | 50000 | n/a | 3034 MiB peak / 2139 MB disk | 2979 MiB peak / 1804 MB disk | n/a | n/a |

## ArcadeDB engine-fused search vs Graphiti Python RRF (informational, ungrouped)

| N nodes | Graphiti Python RRF p50 / p95 | engine `/vector/{db}/hybrid` p50 / p95 |
|---|---|---|
| 1000 | 10.9 / 12.2 | 6.5 / 7.4 |
| 10000 | 10.7 / 13.3 | 8.0 / 9.1 |
| 50000 | 17.1 / 19.7 | 15.5 / 16.5 |
