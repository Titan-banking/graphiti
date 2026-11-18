"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

The TigerGraph schema Graphiti runs on, and the GSQL queries that are installed (compiled) once because they are on a hot
path or need vector search, which TigerGraph does not offer in interpreted mode.

TigerGraph is schema-first: every type and every attribute is declared. Free-form Graphiti attributes (``attributes`` of
an entity or an edge, ``episode_metadata``) are stored as one JSON string. Every datetime is a fixed-width string (see
``convert``). One graph holds every group: ``group_id`` is an attribute, as a graph per group would cost minutes to create.
"""

import hashlib
import logging
import re

from graphiti_core.driver.tigergraph.client import TigerGraphClient, TigerGraphError

logger = logging.getLogger(__name__)

# Vertex types that carry a vector attribute ``name_embedding`` (added by a global schema-change job)
VECTOR_VERTEX_TYPES = ('Entity', 'Community')

VERTEX_DDL = {
    'Entity': (
        'CREATE VERTEX Entity (PRIMARY_ID uuid STRING, name STRING, group_id STRING, summary STRING, '
        'created_at STRING, labels LIST<STRING>, attributes STRING) WITH primary_id_as_attribute="true"'
    ),
    'Episodic': (
        'CREATE VERTEX Episodic (PRIMARY_ID uuid STRING, name STRING, group_id STRING, source STRING, '
        'source_description STRING, content STRING, valid_at STRING, created_at STRING, '
        'entity_edges LIST<STRING>, labels LIST<STRING>, episode_metadata STRING) '
        'WITH primary_id_as_attribute="true"'
    ),
    'Community': (
        'CREATE VERTEX Community (PRIMARY_ID uuid STRING, name STRING, group_id STRING, summary STRING, '
        'created_at STRING, labels LIST<STRING>) WITH primary_id_as_attribute="true"'
    ),
    'Saga': (
        'CREATE VERTEX Saga (PRIMARY_ID uuid STRING, name STRING, group_id STRING, summary STRING, '
        'created_at STRING, first_episode_uuid STRING, last_episode_uuid STRING, last_summarized_at STRING, '
        'last_summarized_episode_valid_at STRING, labels LIST<STRING>) WITH primary_id_as_attribute="true"'
    ),
}

# Edges carry a DISCRIMINATOR on ``uuid``: Graphiti keeps several facts between the same pair of entities, and a TigerGraph
# edge is otherwise unique per (source, type, target)
EDGE_DDL = {
    'RELATES_TO': (
        'CREATE DIRECTED EDGE RELATES_TO (FROM Entity, TO Entity, DISCRIMINATOR(uuid STRING), name STRING, '
        'fact STRING, group_id STRING, episodes LIST<STRING>, created_at STRING, expired_at STRING, '
        'valid_at STRING, invalid_at STRING, reference_time STRING, attributes STRING, '
        'fact_embedding LIST<DOUBLE>)'
    ),
    'MENTIONS': (
        'CREATE DIRECTED EDGE MENTIONS (FROM Episodic, TO Entity, DISCRIMINATOR(uuid STRING), group_id STRING, '
        'created_at STRING)'
    ),
    'HAS_MEMBER': (
        'CREATE DIRECTED EDGE HAS_MEMBER (FROM Community, TO Entity | FROM Community, TO Community, '
        'DISCRIMINATOR(uuid STRING), group_id STRING, created_at STRING)'
    ),
    'HAS_EPISODE': (
        'CREATE DIRECTED EDGE HAS_EPISODE (FROM Saga, TO Episodic, DISCRIMINATOR(uuid STRING), group_id STRING, '
        'created_at STRING)'
    ),
    'NEXT_EPISODE': (
        'CREATE DIRECTED EDGE NEXT_EPISODE (FROM Episodic, TO Episodic, DISCRIMINATOR(uuid STRING), '
        'group_id STRING, created_at STRING)'
    ),
}


def vector_job_ddl(dimension: int, metric: str = 'COSINE') -> str:
    """The global schema-change job that adds the vector attributes (they cannot be added by a graph-local job)."""
    alters = ' '.join(
        f'ALTER VERTEX {t} ADD VECTOR ATTRIBUTE name_embedding(DIMENSION={dimension}, METRIC="{metric}");'
        for t in VECTOR_VERTEX_TYPES
    )
    return f'CREATE GLOBAL SCHEMA_CHANGE JOB graphiti_add_vectors {{ {alters} }}'


# --- installed queries ---------------------------------------------------------------------------------------------
# ``{graph}`` is replaced by the graph name. Parameters that would be empty collections are guarded by a BOOL, because an
# installed query has no optional parameters.

INSTALLED_QUERIES: dict[str, str] = {
    # ANN over one vertex type. The group restriction is a ``candidate_set`` the index searches inside, not a filter on the
    # top ``k``: filtering afterwards drops most of the candidates when the graph holds several groups (measured recall
    # 0.41 against 1.00 with three groups). The score is (1 + cosine) / 2, as the other Graphiti backends; TigerGraph
    # reports 1 - cosine.
    'entity_vsearch': """
CREATE OR REPLACE QUERY entity_vsearch(LIST<FLOAT> qv, INT k, BOOL use_groups, SET<STRING> groups, FLOAT min_score) FOR GRAPH {graph} {{
  TYPEDEF TUPLE<STRING uuid, DOUBLE score> Hit;
  HeapAccum<Hit>(k, score DESC) @@top;
  MapAccum<VERTEX, FLOAT> @@d;
  Cand = SELECT v FROM Entity:v WHERE NOT use_groups OR v.group_id IN groups;
  R = vectorSearch({{Entity.name_embedding}}, qv, k, {{distance_map: @@d, candidate_set: Cand}});
  R2 = SELECT v FROM R:v
       WHERE 1.0 - @@d.get(v) / 2 >= min_score
       ACCUM @@top += Hit(v.uuid, 1.0 - @@d.get(v) / 2);
  PRINT R2, @@top;
}}""",
    'community_vsearch': """
CREATE OR REPLACE QUERY community_vsearch(LIST<FLOAT> qv, INT k, BOOL use_groups, SET<STRING> groups, FLOAT min_score) FOR GRAPH {graph} {{
  TYPEDEF TUPLE<STRING uuid, DOUBLE score> Hit;
  HeapAccum<Hit>(k, score DESC) @@top;
  MapAccum<VERTEX, FLOAT> @@d;
  Cand = SELECT v FROM Community:v WHERE NOT use_groups OR v.group_id IN groups;
  R = vectorSearch({{Community.name_embedding}}, qv, k, {{distance_map: @@d, candidate_set: Cand}});
  R2 = SELECT v FROM R:v
       WHERE 1.0 - @@d.get(v) / 2 >= min_score
       ACCUM @@top += Hit(v.uuid, 1.0 - @@d.get(v) / 2);
  PRINT R2, @@top;
}}""",
    # Entities reached from the origins (entities or episodes) over 1..depth directed RELATES_TO / MENTIONS hops, in the
    # origin's group: the direction of the Cypher backends' ``-[:RELATES_TO|MENTIONS*1..d]->``
    'node_bfs': """
CREATE OR REPLACE QUERY node_bfs(SET<STRING> origins, INT depth, BOOL use_groups, SET<STRING> groups, INT lim) FOR GRAPH {graph} {{
  SetAccum<STRING> @grps;
  EntityStart = to_vertex_set(origins, "Entity");
  EpisodeStart = to_vertex_set(origins, "Episodic");
  Start = EntityStart UNION EpisodeStart;
  Start = SELECT v FROM Start:v ACCUM v.@grps += v.group_id;
  Frontier = Start;
  Found = {{}};
  FOREACH i IN RANGE[1, depth] DO
    Frontier = SELECT t FROM Frontier:s -((RELATES_TO|MENTIONS):e)-> Entity:t
               WHERE NOT use_groups OR t.group_id IN groups
               ACCUM t.@grps += s.@grps;
    Found = Found UNION Frontier;
  END;
  Out = SELECT t FROM Found:t WHERE t.@grps.contains(t.group_id) LIMIT lim;
  PRINT Out;
}}""",
    # RELATES_TO edges met on those walks
    'edge_bfs': """
CREATE OR REPLACE QUERY edge_bfs(SET<STRING> origins, INT depth, BOOL use_groups, SET<STRING> groups, INT lim) FOR GRAPH {graph} {{
  ListAccum<EDGE> @@edges;
  EntityStart = to_vertex_set(origins, "Entity");
  EpisodeStart = to_vertex_set(origins, "Episodic");
  Start = EntityStart UNION EpisodeStart;
  Frontier = Start;
  FOREACH i IN RANGE[1, depth] DO
    Frontier = SELECT t FROM Frontier:s -((RELATES_TO|MENTIONS):e)-> Entity:t
               WHERE NOT use_groups OR t.group_id IN groups
               ACCUM IF e.type == "RELATES_TO" AND @@edges.size() < lim THEN @@edges += e END;
  END;
  PRINT @@edges;
}}""",
    # Exact cosine scan of the facts: TigerGraph has no vector attribute on an edge
    'edge_similarity': """
CREATE OR REPLACE QUERY edge_similarity(LIST<DOUBLE> qv, BOOL use_groups, SET<STRING> groups, STRING src_uuid, STRING dst_uuid, FLOAT min_score, INT lim) FOR GRAPH {graph} {{
  TYPEDEF TUPLE<STRING uuid, STRING src, STRING dst, DOUBLE score> Hit;
  HeapAccum<Hit>(lim, score DESC) @@top;
  ListAccum<DOUBLE> @@q;
  DOUBLE qn = 0;
  FOREACH x IN qv DO @@q += x; qn = qn + x * x; END;
  qn = sqrt(qn);
  INT dim = @@q.size();
  Start = {{Entity.*}};
  R = SELECT t FROM Start:s -(RELATES_TO:e)-> Entity:t
      WHERE (NOT use_groups OR e.group_id IN groups)
        AND (src_uuid == "" OR s.uuid == src_uuid) AND (dst_uuid == "" OR t.uuid == dst_uuid)
        AND e.fact_embedding.size() == dim
      ACCUM
        DOUBLE dot = 0, DOUBLE en = 0,
        FOREACH i IN RANGE[0, dim - 1] DO
          dot = dot + @@q.get(i) * e.fact_embedding.get(i),
          en = en + e.fact_embedding.get(i) * e.fact_embedding.get(i)
        END,
        DOUBLE sc = (1 + dot / (qn * sqrt(en))) / 2,
        IF sc >= min_score THEN @@top += Hit(e.uuid, s.uuid, t.uuid, sc) END;
  PRINT @@top;
}}""",
    # The embeddings of the vertices with these ids (PRINT ... WITH VECTOR is the only way to read a vector attribute)
    'entity_embeddings': """
CREATE OR REPLACE QUERY entity_embeddings(SET<STRING> ids) FOR GRAPH {graph} {{
  S = to_vertex_set(ids, "Entity");
  T = SELECT v FROM S:v;
  PRINT T WITH VECTOR;
}}""",
    'community_embeddings': """
CREATE OR REPLACE QUERY community_embeddings(SET<STRING> ids) FOR GRAPH {graph} {{
  S = to_vertex_set(ids, "Community");
  T = SELECT v FROM S:v;
  PRINT T WITH VECTOR;
}}""",
    # Token scan: a hit's score is the number of query tokens (lower case) that occur in any of its text fields.
    # TigerGraph has no inverted index to rank with, so each call reads every vertex (or fact) of the type.
    'entity_fulltext': """
CREATE OR REPLACE QUERY entity_fulltext(SET<STRING> toks, BOOL use_groups, SET<STRING> groups, INT lim) FOR GRAPH {graph} {{
  TYPEDEF TUPLE<STRING uuid, DOUBLE score> Hit;
  HeapAccum<Hit>(lim, score DESC) @@top;
  R = SELECT v FROM Entity:v WHERE NOT use_groups OR v.group_id IN groups
      ACCUM DOUBLE sc = 0,
        FOREACH t IN toks DO IF instr(lower(v.name), t) >= 0 OR instr(lower(v.summary), t) >= 0 THEN sc = sc + 1 END END,
        IF sc > 0 THEN @@top += Hit(v.uuid, sc) END;
  PRINT @@top;
}}""",
    'episode_fulltext': """
CREATE OR REPLACE QUERY episode_fulltext(SET<STRING> toks, BOOL use_groups, SET<STRING> groups, INT lim) FOR GRAPH {graph} {{
  TYPEDEF TUPLE<STRING uuid, DOUBLE score> Hit;
  HeapAccum<Hit>(lim, score DESC) @@top;
  R = SELECT v FROM Episodic:v WHERE NOT use_groups OR v.group_id IN groups
      ACCUM DOUBLE sc = 0,
        FOREACH t IN toks DO IF instr(lower(v.content), t) >= 0 OR instr(lower(v.source_description), t) >= 0 OR instr(lower(v.name), t) >= 0 THEN sc = sc + 1 END END,
        IF sc > 0 THEN @@top += Hit(v.uuid, sc) END;
  PRINT @@top;
}}""",
    'community_fulltext': """
CREATE OR REPLACE QUERY community_fulltext(SET<STRING> toks, BOOL use_groups, SET<STRING> groups, INT lim) FOR GRAPH {graph} {{
  TYPEDEF TUPLE<STRING uuid, DOUBLE score> Hit;
  HeapAccum<Hit>(lim, score DESC) @@top;
  R = SELECT v FROM Community:v WHERE NOT use_groups OR v.group_id IN groups
      ACCUM DOUBLE sc = 0,
        FOREACH t IN toks DO IF instr(lower(v.name), t) >= 0 OR instr(lower(v.summary), t) >= 0 THEN sc = sc + 1 END END,
        IF sc > 0 THEN @@top += Hit(v.uuid, sc) END;
  PRINT @@top;
}}""",
    'edge_fulltext': """
CREATE OR REPLACE QUERY edge_fulltext(SET<STRING> toks, BOOL use_groups, SET<STRING> groups, INT lim) FOR GRAPH {graph} {{
  TYPEDEF TUPLE<STRING uuid, STRING src, STRING dst, DOUBLE score> Hit;
  HeapAccum<Hit>(lim, score DESC) @@top;
  Start = {{Entity.*}};
  R = SELECT t FROM Start:s -(RELATES_TO:e)-> Entity:t WHERE NOT use_groups OR e.group_id IN groups
      ACCUM DOUBLE sc = 0,
        FOREACH x IN toks DO IF instr(lower(e.fact), x) >= 0 OR instr(lower(e.name), x) >= 0 THEN sc = sc + 1 END END,
        IF sc > 0 THEN @@top += Hit(e.uuid, s.uuid, t.uuid, sc) END;
  PRINT @@top;
}}""",
}


async def graph_exists(client: TigerGraphClient) -> bool:
    out = await client.gsql('ls', use_graph=False)
    return re.search(rf'Graph {re.escape(client.graph)}\b', out) is not None


def query_text(name: str, graph: str) -> str:
    """The ``CREATE QUERY`` text of an installed query, carrying a hash of its definition (``/* v:<sha1[:8]> */``)."""
    text = INSTALLED_QUERIES[name].format(graph=graph)
    version = hashlib.sha1(text.encode()).hexdigest()[:8]
    return re.sub(r'(\) FOR GRAPH \w+ \{)', rf'\1 /* v:{version} */', text, count=1)


async def queries_to_install(client: TigerGraphClient) -> list[str]:
    """The queries that are not installed, or whose installed text is not the current definition.

    A query is addressed by name, so a changed definition would otherwise never reach a graph that has the old one.
    """
    try:
        shown = await client.gsql('SHOW QUERY *')
    except TigerGraphError as e:
        if 'No query available' not in str(e):
            raise
        shown = ''
    installed = await client.installed_query_names()
    current = dict(re.findall(r'CREATE OR REPLACE QUERY (\w+)\([^{]*\{ /\* v:(\w+) \*/', shown))
    stale = []
    for name in INSTALLED_QUERIES:
        wanted = re.search(r'/\* v:(\w+) \*/', query_text(name, client.graph))
        if name not in installed or wanted is None or current.get(name) != wanted.group(1):
            stale.append(name)
    return stale


async def ensure_schema(client: TigerGraphClient, embedding_dim: int) -> None:
    """Create the graph, its types, the vector attributes and the installed queries that do not exist yet."""
    if not await graph_exists(client):
        for ddl in (*VERTEX_DDL.values(), *EDGE_DDL.values()):
            await client.gsql(ddl, use_graph=False)
        types = ', '.join((*VERTEX_DDL, *EDGE_DDL))
        await client.gsql(f'CREATE GRAPH {client.graph} ({types})', use_graph=False)
        await client.gsql(vector_job_ddl(embedding_dim), use_graph=False)
        await client.gsql('RUN GLOBAL SCHEMA_CHANGE JOB graphiti_add_vectors', use_graph=False)
        logger.info('TigerGraph graph %s created', client.graph)
    stale = await queries_to_install(client)
    if not stale:
        return
    script = '\n'.join(query_text(n, client.graph) for n in stale)
    out = await client.gsql(f'{script}\nINSTALL QUERY {", ".join(stale)}')
    if re.search(r'failed: [1-9]', out) or 'DRAFT' in out:
        raise TigerGraphError(f'TigerGraph could not install {stale}: {out[-800:]}')
    logger.info('Installed TigerGraph queries: %s', ', '.join(stale))


async def drop_schema(client: TigerGraphClient) -> None:
    """Drop the graph and every type (a TigerGraph ``DROP ALL`` also restarts its services and takes about a minute)."""
    try:
        await client.gsql('DROP ALL', use_graph=False)
    except TigerGraphError:
        logger.exception('DROP ALL failed')
        raise
