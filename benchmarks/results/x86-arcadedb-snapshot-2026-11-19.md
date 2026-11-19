# FalkorDB vs ArcadeDB benchmark (2026-11-19)

## Setup

- ArcadeDB: 26.12.1-SNAPSHOT (build e710f472f2c9b7700be43002c7b51104f0946fc0/1795125058974/main), image arcadedata/arcadedb@sha256:71ca858da7d7598c76e9efc714285a65be2c1ef5bb26573085f71200ab213e9c
- FalkorDB image: unknown
- ArcadeDB ARCADEDB_OPTS_MEMORY: -Xms512m -Xmx2560m
- container limits: ArcadeDB 5 GB / 4 CPU, FalkorDB unknown, TigerGraph 8 GB / 4 CPU (docker), one engine running at a time
- ArcadeDB JAVA_OPTS: -Darcadedb.server.rootPassword=playwithdata -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin -Darcadedb.server.defaultDatabases=graphiti[root] -Darcadedb.maxPageRAM=512 -XX:MaxDirectMemorySize=1g
- shared Docker VM: 63 GiB RAM / 8 CPUs, also running unrelated containers throughout
- host: Linux-7.0.0-1017-azure-x86_64-with-glibc2.39, unknown
- graphiti PR #1310 head (fetched): 15963f0616f29ea0bd5865033b47f1c22f1ff455
- this branch: 4b0e1e5aea8b04a7a8cd6d6bb5b921b87a2b1770
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
| bulk write nodes/s | 1000 | n/a | 1061 | 955 | 1089 | n/a |
| bulk write nodes/s | 10000 | n/a | 1166 | 1113 | 1211 | n/a |
| bulk write nodes/s | 50000 | n/a | 1195 | 1208 | 1207 | n/a |
| bulk write edges/s | 1000 | n/a | 608 | 366 | 294 | n/a |
| bulk write edges/s | 10000 | n/a | 419 | 575 | 457 | n/a |
| bulk write edges/s | 50000 | n/a | 622 | 580 | 622 | n/a |
| rows after load == N nodes / 2N edges | 1000 | n/a | ok | ok | ok | n/a |
| rows after load == N nodes / 2N edges | 10000 | n/a | ok | ok | ok | n/a |
| rows after load == N nodes / 2N edges | 50000 | n/a | ok | ok | ok | n/a |
| save node c=1 (ms) | 1000 | n/a | 2.8 / 3.9 | 2.5 / 3.7 | 2.4 / 3.3 | n/a |
| save node c=1 (ms) | 10000 | n/a | 2.4 / 2.7 | 2.4 / 3.0 | 2.2 / 2.9 | n/a |
| save node c=1 (ms) | 50000 | n/a | 2.3 / 2.8 | 2.5 / 3.2 | 2.3 / 2.9 | n/a |
| save edge c=1 (ms) | 1000 | n/a | 2.8 / 4.2 | 2.5 / 3.8 | 2.5 / 3.1 | n/a |
| save edge c=1 (ms) | 10000 | n/a | 2.5 / 3.6 | 2.5 / 3.5 | 2.4 / 2.7 | n/a |
| save edge c=1 (ms) | 50000 | n/a | 2.4 / 3.3 | 2.5 / 3.2 | 2.5 / 2.9 | n/a |
| save node c=8 (ms) | 1000 | n/a | 12.1 / 22.2 | 11.7 / 14.0 | 11.4 / 13.5 | n/a |
| save node c=8 (ms) | 10000 | n/a | 11.2 / 14.0 | 10.9 / 13.1 | 11.4 / 13.1 | n/a |
| save node c=8 (ms) | 50000 | n/a | 11.7 / 14.5 | 11.3 / 13.9 | 11.3 / 13.4 | n/a |
| save edge c=8 (ms) | 1000 | n/a | 11.9 / 15.4 | 11.5 / 14.1 | 11.5 / 13.3 | n/a |
| save edge c=8 (ms) | 10000 | n/a | 11.6 / 13.3 | 11.3 / 13.2 | 11.4 / 14.8 | n/a |
| save edge c=8 (ms) | 50000 | n/a | 11.4 / 13.6 | 11.4 / 13.4 | 11.9 / 14.9 | n/a |
| node fulltext (ms) | 1000 | n/a | 4.8 / 8.1 | 3.9 / 5.6 | 3.8 / 5.5 | n/a |
| node fulltext (ms) | 10000 | n/a | 7.0 / 8.1 | 6.6 / 8.1 | 6.6 / 8.7 | n/a |
| node fulltext (ms) | 50000 | n/a | 10.9 / 13.4 | 11.3 / 13.1 | 11.3 / 13.2 | n/a |
| edge fulltext (ms) | 1000 | n/a | 5.6 / 8.1 | 5.2 / 7.6 | 5.0 / 6.3 | n/a |
| edge fulltext (ms) | 10000 | n/a | 8.3 / 11.5 | 8.3 / 9.4 | 8.2 / 9.5 | n/a |
| edge fulltext (ms) | 50000 | n/a | 19.9 / 22.3 | 19.7 / 21.5 | 19.9 / 22.7 | n/a |
| node similarity (ms) | 1000 | n/a | 8.5 / 10.8 | 9.0 / 10.8 | 8.5 / 10.5 | n/a |
| node similarity (ms) | 10000 | n/a | 33.0 / 40.0 | 27.2 / 31.1 | 24.3 / 27.4 | n/a |
| node similarity (ms) | 50000 | n/a | 138.2 / 145.3 | 65.2 / 80.1 | 42.2 / 51.3 | n/a |
| edge similarity, exact (ms) | 1000 | n/a | 13.4 / 14.7 | 22.4 / 24.4 | 15.6 / 17.4 | n/a |
| edge similarity, exact (ms) | 10000 | n/a | 68.2 / 71.7 | 88.9 / 97.9 | 43.3 / 48.4 | n/a |
| edge similarity, exact (ms) | 50000 | n/a | 490.9 / 506.0 | 175.0 / 252.5 | 109.0 / 131.7 | n/a |
| node BFS depth 2 (ms) | 1000 | n/a | 6.3 / 9.7 | 5.7 / 9.1 | 5.7 / 6.1 | n/a |
| node BFS depth 2 (ms) | 10000 | n/a | 5.7 / 6.1 | 5.7 / 6.1 | 5.6 / 6.4 | n/a |
| node BFS depth 2 (ms) | 50000 | n/a | 5.9 / 6.3 | 5.6 / 5.9 | 5.8 / 6.3 | n/a |
| hybrid search RRF (ms) | 1000 | n/a | 15.0 / 18.3 | 18.5 / 20.5 | 15.1 / 20.7 | n/a |
| hybrid search RRF (ms) | 10000 | n/a | 64.5 / 70.9 | 90.6 / 101.4 | 43.4 / 48.4 | n/a |
| hybrid search RRF (ms) | 50000 | n/a | 479.9 / 503.4 | 365.8 / 386.8 | 196.5 / 221.9 | n/a |
| ANN recall@10 | 1000 | n/a | n/a | 1.000 | 1.000 | n/a |
| ANN recall@10 | 10000 | n/a | n/a | 1.000 | 0.992 | n/a |
| ANN recall@10 | 50000 | n/a | n/a | 1.000 | 0.986 | n/a |
| vector index catch-up after the bulk load | 1000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 10000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 50000 | n/a | n/a | n/a | n/a | n/a |
| container memory / disk | 1000 | n/a | 2674 MiB peak / 168 MB disk | 2775 MiB peak / 184 MB disk | 2818 MiB peak / 186 MB disk | n/a |
| container memory / disk | 10000 | n/a | 2722 MiB peak / 377 MB disk | 2856 MiB peak / 456 MB disk | 2884 MiB peak / 474 MB disk | n/a |
| container memory / disk | 50000 | n/a | 2847 MiB peak / 1067 MB disk | 2937 MiB peak / 1096 MB disk | 2941 MiB peak / 1152 MB disk | n/a |

## ArcadeDB engine-fused search vs Graphiti Python RRF (informational, ungrouped)

| N nodes | Graphiti Python RRF p50 / p95 | engine `/vector/{db}/hybrid` p50 / p95 |
|---|---|---|
| 1000 | 10.8 / 11.6 | 6.2 / 7.0 |
| 10000 | 11.6 / 14.3 | 9.6 / 10.6 |
| 50000 | 34.9 / 38.6 | 29.6 / 35.4 |
