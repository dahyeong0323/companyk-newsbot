"""Local/shadow orchestration from normalized articles to rendered HTML."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from companyk_newsbot.dedup import ArticleDeduplicator, ArticleDeduplicationResult, RouteAEventClusterer
from companyk_newsbot.email import EmailNewsItem, HtmlEmailRenderer, RenderedEmail
from companyk_newsbot.judges import JudgedRouteBCandidate, SummaryOutput
from companyk_newsbot.models import Article
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem
from companyk_newsbot.rules import RouteADetector, RouteBCandidate, RouteBCandidateGenerator

RouteBJudge = Callable[[RouteBCandidate], JudgedRouteBCandidate]
Summarize = Callable[[RankedNewsItem], SummaryOutput]


@dataclass(frozen=True)
class PipelineResult:
    article_dedup: ArticleDeduplicationResult
    route_a_event_clusters: int
    route_b_candidates: int
    route_b_accepted: int
    route_b_rejected: int
    ranked_items: tuple[RankedNewsItem, ...]
    rendered_email: RenderedEmail


class NewsPipeline:
    """Run the completed pre-delivery pipeline against already collected articles."""

    def __init__(self, *, route_a_detector: RouteADetector, route_b_generator: RouteBCandidateGenerator, route_b_judge: RouteBJudge, summarize: Summarize, article_deduplicator: ArticleDeduplicator | None = None, route_a_clusterer: RouteAEventClusterer | None = None, ranker: NewsRanker | None = None, renderer: HtmlEmailRenderer | None = None) -> None:
        self.route_a_detector, self.route_b_generator = route_a_detector, route_b_generator
        self.route_b_judge, self.summarize = route_b_judge, summarize
        self.article_deduplicator = article_deduplicator or ArticleDeduplicator()
        self.route_a_clusterer = route_a_clusterer or RouteAEventClusterer()
        self.ranker, self.renderer = ranker or NewsRanker(), renderer or HtmlEmailRenderer()

    def run(self, articles: Iterable[Article], *, report_date: date) -> PipelineResult:
        article_dedup = self.article_deduplicator.deduplicate(articles)
        route_a_matches = [match for article in article_dedup.articles for match in self.route_a_detector.detect(article)]
        route_a_clusters = self.route_a_clusterer.cluster(route_a_matches)
        direct_items = [RankedNewsItem.from_direct(cluster.primary) for cluster in route_a_clusters]
        candidate_result = self.route_b_generator.generate(article_dedup.articles)
        judged = [self.route_b_judge(candidate) for candidate in candidate_result.candidates]
        accepted = [result for result in judged if result.decision.qualifies]
        ranked = self.ranker.rank([*direct_items, *(RankedNewsItem.from_external(result) for result in accepted)])
        rendered = self.renderer.render([EmailNewsItem(item, self.summarize(item)) for item in ranked], report_date=report_date)
        return PipelineResult(article_dedup, len(route_a_clusters), len(candidate_result.candidates), len(accepted), len(candidate_result.rejections) + len(judged) - len(accepted), tuple(ranked), rendered)
