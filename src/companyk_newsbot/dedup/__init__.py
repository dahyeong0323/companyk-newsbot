"""Article and event deduplication."""

from .article import ArticleDeduplicator, ArticleDeduplicationResult, DuplicateArticleGroup
from .anchors import EventAnchors
from .event import EventCluster, EventDedupMetrics, PairDecision, RouteAEventClusterer, article_id
from .external import ExactIdentityCollapse, ExternalEventCluster, RouteBEventClusterer
from .representative import RepresentativeArticleSelector, RepresentativeScore
from .resolver import EventPairResolver, EventResolverOutput, LunaEventPairResolver, ResolverResult

__all__ = [
    "ArticleDeduplicator",
    "ArticleDeduplicationResult",
    "DuplicateArticleGroup",
    "EventCluster",
    "EventAnchors",
    "EventDedupMetrics",
    "EventPairResolver",
    "EventResolverOutput",
    "ExactIdentityCollapse",
    "ExternalEventCluster",
    "LunaEventPairResolver",
    "PairDecision",
    "RepresentativeArticleSelector",
    "RepresentativeScore",
    "ResolverResult",
    "RouteAEventClusterer",
    "RouteBEventClusterer",
    "article_id",
]
