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

``SearchFilters`` applied in Python to the candidates a TigerGraph query returned. The installed queries cannot take a
free-form filter (a compiled query has a fixed shape), so a filtered search asks the query for more candidates than
``limit`` (``oversample``) and keeps the first ``limit`` that pass.
"""

from datetime import datetime, timezone
from typing import Any

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search_filters import (
    ComparisonOperator,
    DateFilter,
    PropertyFilter,
    SearchFilters,
)

# Candidates asked of the database per result wanted when a filter can discard some
OVERSAMPLE = 10


def has_node_filters(filters: SearchFilters) -> bool:
    return filters.node_labels is not None or bool(filters.property_filters)


def has_edge_filters(filters: SearchFilters) -> bool:
    return any(
        value is not None
        for value in (
            filters.node_labels,
            filters.edge_types,
            filters.valid_at,
            filters.invalid_at,
            filters.created_at,
            filters.expired_at,
            filters.edge_uuids,
            filters.edge_source_node_uuids,
            filters.edge_target_node_uuids,
            filters.property_filters,
        )
    )


def candidates_wanted(limit: int, filtered: bool) -> int:
    return limit * OVERSAMPLE if filtered else limit


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _compare(value: Any, operator: ComparisonOperator, other: Any) -> bool:
    if operator == ComparisonOperator.is_null:
        return value is None
    if operator == ComparisonOperator.is_not_null:
        return value is not None
    if value is None or other is None:
        return False
    if isinstance(value, datetime) and isinstance(other, datetime):
        # A filter date without a zone is UTC, as everywhere else in Graphiti
        value, other = _utc(value), _utc(other)
    if operator == ComparisonOperator.equals:
        return value == other
    if operator == ComparisonOperator.not_equals:
        return value != other
    if operator == ComparisonOperator.greater_than:
        return value > other
    if operator == ComparisonOperator.less_than:
        return value < other
    if operator == ComparisonOperator.greater_than_equal:
        return value >= other
    return value <= other


def _date_matches(value: datetime | None, groups: list[list[DateFilter]] | None) -> bool:
    """``groups`` is an OR of ANDs, as in the Cypher filters."""
    if groups is None:
        return True
    return any(all(_compare(value, f.comparison_operator, f.date) for f in ands) for ands in groups)


def _properties_match(attributes: dict[str, Any], filters: list[PropertyFilter] | None) -> bool:
    return all(
        _compare(attributes.get(f.property_name), f.comparison_operator, f.property_value)
        for f in filters or []
    )


def node_matches(node: EntityNode, filters: SearchFilters) -> bool:
    if filters.node_labels is not None and not all(
        label in node.labels for label in filters.node_labels
    ):
        return False
    return _properties_match(node.attributes, filters.property_filters)


def edge_matches(
    edge: EntityEdge, filters: SearchFilters, endpoints: dict[str, EntityNode] | None = None
) -> bool:
    """``endpoints`` (uuid to node) is needed only for a ``node_labels`` filter, which applies to both ends."""
    if filters.edge_types is not None and edge.name not in filters.edge_types:
        return False
    if filters.edge_uuids is not None and edge.uuid not in filters.edge_uuids:
        return False
    if (
        filters.edge_source_node_uuids is not None
        and edge.source_node_uuid not in filters.edge_source_node_uuids
    ):
        return False
    if (
        filters.edge_target_node_uuids is not None
        and edge.target_node_uuid not in filters.edge_target_node_uuids
    ):
        return False
    if filters.node_labels is not None:
        for uuid in (edge.source_node_uuid, edge.target_node_uuid):
            node = (endpoints or {}).get(uuid)
            if node is None or not all(label in node.labels for label in filters.node_labels):
                return False
    return (
        _date_matches(edge.valid_at, filters.valid_at)
        and _date_matches(edge.invalid_at, filters.invalid_at)
        and _date_matches(edge.created_at, filters.created_at)
        and _date_matches(edge.expired_at, filters.expired_at)
        and _properties_match(edge.attributes, filters.property_filters)
    )
