"""Render qualified, summarized news into a standalone HTML email body."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
        if not self.summary.insight_one_liner:
            raise ValueError("email items require a grounded executive insight")


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str


class HtmlEmailRenderer:
    """A dependency-light, email-client-safe renderer. Sending belongs to a later step."""

    def render(self, items: Iterable[EmailNewsItem], *, report_date: date) -> RenderedEmail:
        all_items = list(items)
        direct = [item for item in all_items if item.item.route == "direct"]
        external = [item for item in all_items if item.item.route == "external"]
        subject = f"[Company K] 포트폴리오 데일리 뉴스 | {report_date.isoformat()}"
        body = "".join(
            [
                self._header(report_date, len(all_items)),
                self._section("1. 기업 직접 뉴스", direct, external=False),
                self._section("2. 포트폴리오 영향 뉴스", external, external=True),
                "</td></tr></table></body></html>",
            ]
        )
        return RenderedEmail(subject=subject, html=body)

    @staticmethod
    def _header(report_date: date, count: int) -> str:
        return f"""<!doctype html><html lang="ko"><body style="margin:0;background:#f5f7fa;font-family:Arial,sans-serif;color:#172033">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:24px"><tr><td align="center">
<table role="presentation" width="680" cellspacing="0" cellpadding="0" style="max-width:680px;background:#fff;border-radius:12px;overflow:hidden">
<tr><td style="padding:28px 32px;background:#15213b;color:#fff"><div style="font-size:12px;letter-spacing:.08em">COMPANY K PARTNERS</div><h1 style="margin:8px 0 0;font-size:23px">포트폴리오 데일리 뉴스</h1><p style="margin:8px 0 0;color:#cdd8ee">{report_date.isoformat()} · 주요 뉴스 {count}건</p></td></tr>
<tr><td style="padding:12px 32px 32px">"""

    def _section(self, title: str, items: list[EmailNewsItem], *, external: bool) -> str:
        content = "".join(self._item(item, external=external) for item in items)
        if not content:
            content = "<p style=\"margin:12px 0 24px;color:#697386;font-size:14px\">해당 뉴스가 없습니다.</p>"
        return f"<h2 style=\"margin:28px 0 8px;font-size:17px;color:#15213b\">{escape(title)}</h2>{content}"

    @staticmethod
    def _item(news: EmailNewsItem, *, external: bool) -> str:
        item, summary = news.item, news.summary
        company = escape(item.company)
        title = escape(item.article_title)
        url = escape(item.article_url, quote=True)
        main_summary = escape(summary.summary)
        source = item.direct_match.article.source if item.direct_match else item.external_match.candidate.article.source
        coverage = escape(source)
        if item.coverage_count > 1:
            coverage += f" · 외 {item.coverage_count - 1}개 매체 보도"
        company_label = "영향" if item.route == "external" and len(item.impacted_companies) > 1 else "회사"
        insight = f"<p style=\"margin:10px 0 0;font-size:14px;line-height:1.55;color:#26364f\"><strong>투자자 관점:</strong> {escape(summary.insight_one_liner or '')}</p>"
        why = ""
        if external:
            why = f"<p style=\"margin:10px 0 0;font-size:14px;line-height:1.55;color:#26364f\"><strong>왜 이 회사에 중요한가:</strong> {escape(summary.why_it_matters or '')}</p>"
        return f"""<article style="margin:12px 0;padding:18px 20px;border:1px solid #e3e8ef;border-radius:9px">
<div style="font-size:13px;font-weight:bold;color:#315ea8">{company_label}: {company}</div>
<a href="{url}" style="display:block;margin-top:6px;color:#172033;font-size:16px;font-weight:bold;line-height:1.4;text-decoration:none">{title}</a>
<div style="margin-top:6px;font-size:12px;color:#697386">{coverage}</div>
<p style="margin:10px 0 0;font-size:14px;line-height:1.55;color:#34445e">{main_summary}</p>{insight}{why}
<p style="margin:12px 0 0;font-size:12px"><a href="{url}" style="color:#315ea8">기사 보기</a></p>
</article>"""
