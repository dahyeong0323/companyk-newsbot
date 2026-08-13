from dataclasses import replace
from datetime import UTC, datetime, timedelta

from companyk_newsbot.dedup import RouteBEventClusterer
from companyk_newsbot.judges import JudgeOutput, JudgedRouteBCandidate
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteBCandidate
from companyk_newsbot.ranking import NewsRanker, RankedNewsItem


def judged(company: str, title: str, url: str, *, hour: int = 8, family: str = "competition") -> JudgedRouteBCandidate:
    article = Article(source="publisher", source_type="fixture", title=title, url=url, canonical_url=url, description=title, retrieved_at=datetime(2026, 8, 12, tzinfo=UTC), published_at=datetime(2026, 8, 11, hour, tzinfo=UTC))
    candidate = RouteBCandidate(article, company, f"{company}-exposure", "External subject", (family,))
    decision = JudgeOutput(qualifies=True, company=company, exposure_id=candidate.exposure_id, event_family=family, materiality="high", impact_direction="negative", causal_mechanism="Approved mechanism.", rejection_reason="none")
    return JudgedRouteBCandidate(candidate, decision, "test", "test")


def test_route_b_one_event_retains_two_company_impact_links() -> None:
    class Same:
        def resolve(self, left, right):
            from companyk_newsbot.dedup import ResolverResult
            return ResolverResult("SAME_EVENT", "same penalty fixture", True)

    cluster = RouteBEventClusterer(resolver=Same()).cluster([
        judged("A", "Regulator announces $10m platform penalty", "https://a.example/penalty"),
        judged("B", "Platform receives $10m regulator penalty", "https://b.example/penalty", hour=9),
    ])
    assert len(cluster) == 1
    assert cluster[0].companies == ("A", "B")
    assert len(cluster[0].impact_links) == 2
    ranked = NewsRanker(total_max_items=3, max_items_per_company=1).rank([RankedNewsItem.from_external_event(cluster[0])])
    assert len(ranked) == 1 and ranked[0].impacted_companies == ("A", "B")


def test_route_b_conflicting_amounts_do_not_merge() -> None:
    cluster = RouteBEventClusterer().cluster([
        judged("A", "Regulator announces $10m platform penalty", "https://a.example/ten"),
        judged("A", "Regulator announces $20m platform penalty", "https://a.example/twenty", hour=9),
    ])
    assert len(cluster) == 2


def test_route_b_incompatible_family_or_window_do_not_merge() -> None:
    value = judged("A", "Platform decision update", "https://a.example/one")
    other_family = judged("A", "Platform policy decision", "https://a.example/two", family="policy_regulatory")
    far = judged("A", "Platform decision outlook", "https://a.example/three", hour=8)
    far_article = far.candidate.article.model_copy(update={"published_at": far.candidate.article.published_at + timedelta(hours=73)})
    far = replace(far, candidate=replace(far.candidate, article=far_article))
    assert len(RouteBEventClusterer().cluster([value, other_family, far])) == 3
