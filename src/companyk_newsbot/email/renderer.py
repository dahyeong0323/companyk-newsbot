"""Render qualified, summarized news into a standalone HTML email body."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import escape
from typing import Iterable

from companyk_newsbot.judges import SummaryOutput
from companyk_newsbot.ranking import RankedNewsItem


@dataclass(frozen=True)
class EmailNewsItem:
    item: RankedNewsItem
    summary: SummaryOutput
    summary_retry_count: int = 0
    summary_validation_failure: str | None = None

    def __post_init__(self) -> None:
        if self.item.route == "external" and not self.summary.why_it_matters:
            raise ValueError("external email items require why_it_matters")
        if self.item.route == "direct" and self.summary.why_it_matters:
            raise ValueError("direct email items must not include why_it_matters")
        if self.item.route == "external" and not self.summary.insight_one_liner:
            raise ValueError("external email items require a grounded executive insight")


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str


class HtmlEmailRenderer:
    """A dependency-light, email-client-safe renderer. Sending belongs to a later step."""

    def render(self, items: Iterable[EmailNewsItem], *, report_date: date, route_b_enabled: bool = True) -> RenderedEmail:
        all_items = list(items)
        direct = [item for item in all_items if item.item.route == "direct"]
        external = [item for item in all_items if item.item.route == "external"]
        subject = f"[Company K] 포트폴리오 데일리 뉴스 | {report_date.isoformat()} | 주요 뉴스 {len(all_items)}건"
        if route_b_enabled:
            content = self._section("1. 기업 직접 뉴스", direct, external=False) + self._section("2. 포트폴리오 영향 뉴스", external, external=True)
        elif direct:
            content = "".join(self._company_group(company, group, external=False) for company, group in self._group_by_company(direct))
        else:
            content = '<p style="margin:28px 0;color:#34445e;font-size:15px">오늘 컴퍼니케이파트너스 포트폴리오 회사의 주요 기사는 없습니다.</p>'
        body = self._header(report_date, len(all_items)) + content + "</td></tr></table></body></html>"
        return RenderedEmail(subject=subject, html=body)

    @staticmethod
    def _header(report_date: date, count: int) -> str:
        return f"""<!doctype html><html lang="ko"><body style="margin:0;background:#f5f7fa;font-family:Arial,sans-serif;color:#172033">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:24px"><tr><td align="center">
<table role="presentation" width="680" cellspacing="0" cellpadding="0" style="max-width:680px;background:#fff;border-radius:12px;overflow:hidden">
<tr><td style="padding:28px 32px;background:#15213b;color:#fff"><div style="font-size:12px;letter-spacing:.08em">COMPANY K PARTNERS</div><h1 style="margin:8px 0 0;font-size:23px">포트폴리오 데일리 뉴스</h1><p style="margin:8px 0 0;color:#cdd8ee">{report_date.isoformat()} · 주요 뉴스 {count}건</p></td></tr>
<tr><td style="padding:12px 32px 32px">"""

    def _section(self, title: str, items: list[EmailNewsItem], *, external: bool) -> str:
        content = "".join(self._company_group(company, group, external=external) for company, group in self._group_by_company(items))
        if not content:
            content = "<p style=\"margin:12px 0 24px;color:#697386;font-size:14px\">해당 뉴스가 없습니다.</p>"
        return f"<h2 style=\"margin:28px 0 8px;font-size:17px;color:#15213b\">{escape(title)}</h2>{content}"

    @staticmethod
    def _group_by_company(items: list[EmailNewsItem]) -> list[tuple[str, list[EmailNewsItem]]]:
        """Keep the ranked first company occurrence, but make each company contiguous.

        This is presentation-only: every item remains an independent event and the
        ranking pipeline remains responsible for the order between company groups.
        """
        groups: dict[str, list[tuple[int, EmailNewsItem]]] = {}
        for position, news in enumerate(items):
            groups.setdefault(news.item.company, []).append((position, news))
        return [
            (
                company,
                [news for _, news in sorted(group, key=lambda value: (
                    value[1].item.published_at is None,
                    -(value[1].item.published_at.timestamp() if value[1].item.published_at else 0),
                    value[0],
                ))],
            )
            for company, group in groups.items()
        ]

    def _company_group(self, company: str, items: list[EmailNewsItem], *, external: bool) -> str:
        label = "영향 회사" if external and any(len(news.item.impacted_companies) > 1 for news in items) else "회사"
        cards = "".join(self._item(item, external=external) for item in items)
        return f"""<section data-company-group=\"{escape(company, quote=True)}\" style=\"margin:24px 0 32px\">
<div style=\"margin:0 0 10px;font-size:18px;font-weight:bold;color:#15213b\">{label}: {escape(company)}</div>
{cards}</section>"""

    @staticmethod
    def _item(news: EmailNewsItem, *, external: bool) -> str:
        item, summary = news.item, news.summary
        title = escape(item.article_title)
        url = escape(item.article_url, quote=True)
        main_summary = escape(summary.summary)
        source = item.direct_match.article.source if item.direct_match else item.external_match.candidate.article.source
        coverage = escape(source)
        if item.coverage_count > 1:
            coverage += f" · 외 {item.coverage_count - 1}개 매체 보도"
        published_at = item.published_at.isoformat() if item.published_at else ""
        relative_time = HtmlEmailRenderer._relative_time(item.published_at)
        insight = "" if not summary.insight_one_liner else f"<p style=\"margin:10px 0 0;font-size:14px;line-height:1.55;color:#26364f\"><strong>투자자 관점:</strong> {escape(summary.insight_one_liner)}</p>"
        why = ""
        if external:
            why = f"<p style=\"margin:10px 0 0;font-size:14px;line-height:1.55;color:#26364f\"><strong>왜 이 회사에 중요한가:</strong> {escape(summary.why_it_matters or '')}</p>"
        return f"""<article style="margin:12px 0;padding:18px 20px;border:1px solid #e3e8ef;border-radius:9px">
<a href="{url}" style="display:block;color:#172033;font-size:16px;font-weight:bold;line-height:1.4;text-decoration:none">{title}</a>
<div data-published-at="{escape(published_at, quote=True)}" style="margin-top:6px;font-size:12px;color:#697386"><span>{coverage}</span><span style="float:right">{relative_time}</span></div>
<p style="margin:10px 0 0;font-size:14px;line-height:1.55;color:#34445e">{main_summary}</p>{insight}{why}
<p style="margin:12px 0 0;font-size:12px"><a href="{url}" style="color:#315ea8">기사 보기</a></p>
</article>"""

    @staticmethod
    def _relative_time(published_at: datetime | None) -> str:
        if published_at is None:
            return "발행 시각 미상"
        timestamp = published_at if published_at.tzinfo else published_at.replace(tzinfo=UTC)
        seconds = max(0, int((datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds()))
        if seconds < 60:
            return "방금 전"
        if seconds < 3600:
            return f"{seconds // 60}분 전"
        if seconds < 86400:
            return f"{seconds // 3600}시간 전"
        return f"{seconds // 86400}일 전"
