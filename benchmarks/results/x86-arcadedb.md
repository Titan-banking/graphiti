# FalkorDB vs ArcadeDB benchmark (2026-11-19)

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
| bulk write nodes/s | 1000 | n/a | 1050 | 1059 | n/a | n/a |
| bulk write nodes/s | 10000 | n/a | 1163 | 1138 | n/a | n/a |
| bulk write nodes/s | 50000 | n/a | 1186 | 1154 | n/a | n/a |
| bulk write edges/s | 1000 | n/a | 556 | 631 | n/a | n/a |
| bulk write edges/s | 10000 | n/a | 469 | 409 | n/a | n/a |
| bulk write edges/s | 50000 | n/a | 652 | 653 | n/a | n/a |
| rows after load == N nodes / 2N edges | 1000 | n/a | ok | ok | n/a | n/a |
| rows after load == N nodes / 2N edges | 10000 | n/a | ok | ok | n/a | n/a |
| rows after load == N nodes / 2N edges | 50000 | n/a | ok | ok | n/a | n/a |
| save node c=1 (ms) | 1000 | n/a | 2.8 / 3.7 | 2.4 / 3.0 | n/a | n/a |
| save node c=1 (ms) | 10000 | n/a | 2.4 / 2.8 | 2.3 / 2.7 | n/a | n/a |
| save node c=1 (ms) | 50000 | n/a | 2.4 / 3.6 | 2.5 / 3.2 | n/a | n/a |
| save edge c=1 (ms) | 1000 | n/a | 2.9 / 3.6 | 2.4 / 2.8 | n/a | n/a |
| save edge c=1 (ms) | 10000 | n/a | 2.5 / 3.0 | 2.4 / 2.8 | n/a | n/a |
| save edge c=1 (ms) | 50000 | n/a | 2.4 / 3.2 | 2.6 / 3.5 | n/a | n/a |
| save node c=8 (ms) | 1000 | n/a | 12.3 / 32.2 | 12.0 / 16.1 | n/a | n/a |
| save node c=8 (ms) | 10000 | n/a | 11.2 / 14.1 | 11.1 / 13.6 | n/a | n/a |
| save node c=8 (ms) | 50000 | n/a | 11.5 / 76.8 | 11.7 / 14.0 | n/a | n/a |
| save edge c=8 (ms) | 1000 | n/a | 12.0 / 17.7 | 12.4 / 15.6 | n/a | n/a |
| save edge c=8 (ms) | 10000 | n/a | 12.2 / 16.2 | 11.6 / 17.4 | n/a | n/a |
| save edge c=8 (ms) | 50000 | n/a | 12.3 / 17.7 | 12.5 / 15.3 | n/a | n/a |
| node fulltext (ms) | 1000 | n/a | 4.5 / 7.2 | 3.8 / 5.6 | n/a | n/a |
| node fulltext (ms) | 10000 | n/a | 7.0 / 10.8 | 6.7 / 8.7 | n/a | n/a |
| node fulltext (ms) | 50000 | n/a | 11.1 / 12.8 | 11.4 / 13.1 | n/a | n/a |
| edge fulltext (ms) | 1000 | n/a | 5.4 / 8.4 | 5.3 / 6.3 | n/a | n/a |
| edge fulltext (ms) | 10000 | n/a | 8.5 / 10.0 | 8.4 / 8.8 | n/a | n/a |
| edge fulltext (ms) | 50000 | n/a | 19.9 / 22.2 | 20.1 / 22.7 | n/a | n/a |
| node similarity (ms) | 1000 | n/a | 9.2 / 11.7 | 8.9 / 12.9 | n/a | n/a |
| node similarity (ms) | 10000 | n/a | 32.4 / 33.0 | 23.7 / 29.0 | n/a | n/a |
| node similarity (ms) | 50000 | n/a | 141.3 / 147.3 | 40.6 / 45.9 | n/a | n/a |
| edge similarity, exact (ms) | 1000 | n/a | 13.7 / 18.2 | 13.3 / 15.8 | n/a | n/a |
| edge similarity, exact (ms) | 10000 | n/a | 68.8 / 72.6 | 68.1 / 72.9 | n/a | n/a |
| edge similarity, exact (ms) | 50000 | n/a | 490.5 / 511.6 | 499.8 / 523.2 | n/a | n/a |
| node BFS depth 2 (ms) | 1000 | n/a | 8.4 / 9.2 | 7.9 / 9.2 | n/a | n/a |
| node BFS depth 2 (ms) | 10000 | n/a | 31.2 / 41.1 | 31.2 / 38.8 | n/a | n/a |
| node BFS depth 2 (ms) | 50000 | n/a | 141.9 / 150.6 | 145.6 / 157.5 | n/a | n/a |
| hybrid search RRF (ms) | 1000 | n/a | 15.9 / 20.0 | 14.4 / 17.0 | n/a | n/a |
| hybrid search RRF (ms) | 10000 | n/a | 65.8 / 73.0 | 65.0 / 71.7 | n/a | n/a |
| hybrid search RRF (ms) | 50000 | n/a | 480.8 / 495.1 | 484.9 / 501.0 | n/a | n/a |
| ANN recall@10 | 1000 | n/a | n/a | 1.000 | n/a | n/a |
| ANN recall@10 | 10000 | n/a | n/a | 1.000 | n/a | n/a |
| ANN recall@10 | 50000 | n/a | n/a | 0.998 | n/a | n/a |
| vector index catch-up after the bulk load | 1000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 10000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 50000 | n/a | n/a | n/a | n/a | n/a |
| container memory / disk | 1000 | n/a | 2568 MiB peak / 168 MB disk | 2802 MiB peak / 176 MB disk | n/a | n/a |
| container memory / disk | 10000 | n/a | 2715 MiB peak / 434 MB disk | 2854 MiB peak / 460 MB disk | n/a | n/a |
| container memory / disk | 50000 | n/a | 2878 MiB peak / 1114 MB disk | 2949 MiB peak / 1071 MB disk | n/a | n/a |

## ArcadeDB engine-fused search vs Graphiti Python RRF (informational, ungrouped)

| N nodes | Graphiti Python RRF p50 / p95 | engine `/vector/{db}/hybrid` p50 / p95 |
|---|---|---|
| 1000 | 10.7 / 11.2 | 6.3 / 7.4 |
| 10000 | 11.2 / 11.7 | 8.2 / 9.7 |
| 50000 | 16.9 / 20.8 | 16.1 / 18.7 |
