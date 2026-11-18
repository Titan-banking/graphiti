# TigerGraph driver

`TigerGraphDriver` runs Graphiti on [TigerGraph](https://www.tigergraph.com/). TigerGraph's openCypher has no `MERGE`
and only a single-block `CREATE`/`SET`, so Graphiti's own Cypher cannot run on it. The driver talks GSQL (port 14240) and
REST++ (port 9000) over HTTP instead and implements all eleven operation classes (`graphiti_core/driver/operations`).

```python
from graphiti_core import Graphiti
from graphiti_core.driver.tigergraph_driver import TigerGraphDriver

driver = TigerGraphDriver(host='localhost', graph='graphiti', embedding_dim=1024)
await driver.build_indices_and_constraints()   # creates the graph, the vector attributes and the installed queries
graphiti = Graphiti(graph_driver=driver)
```

## Server

The free Community image runs without a licence (the `tigergraph/tigergraph` image needs one). Both images are
`linux/amd64` only; on Apple silicon Docker runs them under emulation, which is slow.

```bash
docker run -d --name tigergraph --platform linux/amd64 --memory 8g --ulimit nofile=1000000:1000000 \
    -p 14240:14240 -p 9000:9000 tigergraph/community:4.2.6
```

Credentials are `tigergraph` / `tigergraph`. Services take about two minutes to come up. Creating the schema takes about
a minute under emulation (`CREATE GRAPH` alone is ~20 s) and installing the queries another 1-2 minutes, once.

## How it maps

| Graphiti | TigerGraph |
|---|---|
| all groups | one graph; `group_id` is an attribute (`CREATE GRAPH` is too slow for a graph per group); `clone()` returns the driver |
| `Entity`, `Episodic`, `Community`, `Saga` | vertex types keyed by `uuid` |
| `RELATES_TO`, `MENTIONS`, `HAS_MEMBER`, `HAS_EPISODE`, `NEXT_EPISODE` | directed edge types with `DISCRIMINATOR(uuid)`, so several facts may join one pair of entities |
| `name_embedding` | a TigerGraph vector attribute (HNSW, cosine) on `Entity` and `Community`; its dimension is fixed at schema creation |
| `fact_embedding` | a `LIST<DOUBLE>` on the edge (an edge cannot carry a vector attribute) |
| `attributes`, `episode_metadata` | one JSON string each |
| datetimes | fixed-width UTC strings (`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`): TigerGraph's `DATETIME` keeps seconds only |
| NULL | TigerGraph has none: an absent value is `''` or `[]` |

Graphiti core saves and searches through Cypher unless `driver.graph_operations_interface` / `driver.search_interface`
answer first; the driver sets both (`interface.py`, `search_interface.py`) so `Graphiti.add_episode`, `search` and the
bulk paths run unchanged. Four core call sites that had no hook branch on `GraphProvider.TIGERGRAPH`
(`graphiti.py` saga lookup and episode removal, `edge_operations.filter_existing_duplicate_of_edges`).

## Behaviour that differs from the Cypher backends

* **No transactions.** Each write is its own REST++ request; a failed bulk save is not rolled back, and durability
  after the call returns was not tested against a crash. `transaction()` is a no-op wrapper.
* **Writes are upserts keyed by uuid.** There is no unique-constraint error to catch, and no duplicate-edge race.
* **The vector index is eventually consistent.** A vertex is readable at once, but a vector search may miss it for a
  while after the write: 0.7 s once after a single save, 1.6-2.9 s after a 1,000-node bulk load, and the index does not
  apply writes in order. Before a similarity search or an embedding read the driver therefore probes a sample of the
  vectors it wrote recently and blocks, briefly, until they are visible (`wait_for_vector`). After 10 s it logs a
  warning and runs the search anyway, which may then miss those vertices. A write by another process is not covered.
* **Facts are found by scanning.** TigerGraph cannot index an edge attribute, so reading a fact by `uuid` or by group scans
  every `RELATES_TO` edge. Searches avoid it: similarity and fulltext return `(uuid, source, target)` and the fact is read by
  its end pair.
* **Fulltext is a token scan, not Lucene.** The installed `*_fulltext` queries count how many of the query's
  lower-cased words occur in the text fields. They read every vertex (or fact) of the type, rank differently from BM25,
  and do not stem.
* **Edge similarity is an exact scan** of the facts (as on ArcadeDB). Node and community similarity use the vector index
  with the group as a `candidate_set`, so a group restriction is applied inside the search; filtering the top `k`
  afterwards gave recall 0.41 with three groups. With a `candidate_set` the benchmark's recall@10 was 1.000, which is
  also what an exact search over the candidates would give: whether TigerGraph walks its HNSW graph or scans the
  candidates there is not established here.
* **Search filters are applied in Python** to an oversampled candidate list (`filters.py`).
* **BFS is directed** (`RELATES_TO` / `MENTIONS`), as in the Cypher backends.
* **`delete_all_indexes` does nothing**: the vector attributes and queries are part of the schema and dropping them drops
  the data (`schema.drop_schema`, which also restarts TigerGraph's services, ~1 minute).
* **One embedding dimension per schema.** The shared test suites use 384-dimensional helper embeddings and the benchmark
  1024: rebuild the schema (`drop_schema` then `build_indices_and_constraints`) to change it.

## Tests

`tests/driver/test_tigergraph_driver.py` needs no server. The shared suites run against a server with
`ENABLE_TIGERGRAPH=1` and `TIGERGRAPH_EMBEDDING_DIM=384` (and a 384-dimensional schema), for example
`ENABLE_TIGERGRAPH=1 DISABLE_NEO4J=1 DISABLE_FALKORDB=1 DISABLE_ARCADEDB=1 uv run pytest tests/test_node_int.py tests/test_edge_int.py tests/test_graphiti_mock.py`.
