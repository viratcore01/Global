"""Discovery adapters: automatic board links, selector fallbacks."""

from __future__ import annotations

from radar.config import SourceConfig
from radar.discovery.base import discover_source
from radar.fetch.http import FetchResult
from radar import repo


BOARD_HTML = """
<html><body>
  <header><a href="/about">About</a></header>
  <div class="col-xl-4">
    <div class="row casting_card">
      <div class="casting_card_title"><a href="/auditions/c/1">Lead actor for Mumbai short</a></div>
      <div class="casting_card_cat">Casting for a short film. Role: lead, age 22-30.</div>
    </div>
    <div class="user_card_footer_box"><div class="col-4">Mumbai</div></div>
  </div>
  <div class="col-xl-4">
    <div class="row casting_card">
      <div class="casting_card_title"><a href="/auditions/c/2">Supporting actress for Delhi series</a></div>
      <div class="casting_card_cat">We are casting a supporting role, female, age 25-32.</div>
    </div>
    <div class="user_card_footer_box"><div class="col-4">Delhi</div></div>
  </div>
  <div class="col-xl-4">
    <div class="row casting_card">
      <div class="casting_card_title"><a href="/auditions/c/3">DOP required for Pune ad film</a></div>
      <div class="casting_card_cat">Crew call: DOP required for a two-day ad shoot. Paid.</div>
    </div>
    <div class="user_card_footer_box"><div class="col-4">Pune</div></div>
  </div>
</body></html>
"""


class FakeFetcher:
    def __init__(self, html: str) -> None:
        self.html = html

    def fetch(self, url: str, **kwargs):  # noqa: D401 - matches HttpFetcher.fetch
        return FetchResult(
            url=url,
            final_url=url,
            status=200,
            headers={"content-type": "text/html"},
            body=self.html.encode("utf-8"),
            text=self.html,
            content_type="text/html",
        )


def test_web_source_auto_discovers_board_links(config, db) -> None:
    source_id = repo.add_source(
        db,
        SourceConfig(
            name="Auto board",
            type="web",
            url="https://board.example/auditions",
            enabled=True,
            tier=2,
        ),
    )
    source = repo.get_source(db, source_id)
    result = discover_source(config, db, source, fetcher=FakeFetcher(BOARD_HTML))
    assert result.error is None
    assert result.queued == 3
    queued = {row["url"] for row in db.query("SELECT url FROM discovery_queue ORDER BY id")}
    assert queued == {
        "https://board.example/auditions/c/1",
        "https://board.example/auditions/c/2",
        "https://board.example/auditions/c/3",
    }


def test_web_source_without_board_queues_the_page(config, db) -> None:
    source_id = repo.add_source(
        db,
        SourceConfig(
            name="Single page",
            type="web",
            url="https://board.example/auditions/c/9",
            enabled=True,
            tier=2,
            assume_opportunity=True,
        ),
    )
    source = repo.get_source(db, source_id)
    html = """
    <html><body>
      <h1>Casting call: lead actor</h1>
      <p>Casting for a short film in Mumbai. Deadline: 20 December 2026.</p>
      <p>Send a self tape to casting@example.com.</p>
    </body></html>
    """
    result = discover_source(config, db, source, fetcher=FakeFetcher(html))
    assert result.error is None
    assert result.queued == 1
    queued = [row["url"] for row in db.query("SELECT url FROM discovery_queue ORDER BY id")]
    assert queued == ["https://board.example/auditions/c/9"]


def test_nav_page_does_not_become_a_board(config, db) -> None:
    """A 404 or nav page that merely says "Auditions" is not a listing."""
    nav_html = """
    <html><body>
      <p>Page not found</p>
      <nav><ul>
        <li><a href="/">Homepage</a></li>
        <li><a href="/profiles">Talents</a></li>
        <li><a href="/auditions">Auditions</a></li>
        <li><a href="/index.php?action=login">Login</a></li>
        <li><a href="/index.php?action=register">Register</a></li>
      </ul></nav>
    </body></html>
    """
    source_id = repo.add_source(
        db,
        SourceConfig(
            name="Nav only",
            type="web",
            url="https://board.example/casting-calls",
            enabled=True,
            tier=2,
        ),
    )
    source = repo.get_source(db, source_id)
    result = discover_source(config, db, source, fetcher=FakeFetcher(nav_html))
    assert result.error is None
    assert result.queued == 1
    queued = [row["url"] for row in db.query("SELECT url FROM discovery_queue ORDER BY id")]
    assert queued == ["https://board.example/casting-calls"]


def test_board_cards_with_long_titles_stay_one_group() -> None:
    """Grouping must not split a board by how long an individual title is."""
    from radar.extract.board import detect_board_items

    long_title = "Role: Lead Hero in a Hindi action feature film about courage and justice"
    html = ["<html><body>"]
    for index in range(4):
        title = long_title if index % 2 else f"Short role {index}"
        html.append(
            f'<div class="col-xl-4"><div class="row casting_card">'
            f'<div class="casting_card_title"><a href="/auditions/c/{index}">{title}</a></div>'
            f'<div class="casting_card_cat">Casting for a movie, 15+ years old.</div>'
            f"</div><div class=\"user_card_footer_box\"><div class=\"col-4\">Mumbai</div></div></div>"
        )
    html.append("</body></html>")
    items = detect_board_items("".join(html), "https://board.example/auditions")
    assert len(items) == 4
    assert {item.city for item in items} == {"Mumbai"}


def test_web_source_can_disable_auto_links(config, db) -> None:
    source_id = repo.add_source(
        db,
        SourceConfig(
            name="Manual page",
            type="web",
            url="https://board.example/auditions",
            enabled=True,
            tier=2,
            config={"auto_links": False},
        ),
    )
    source = repo.get_source(db, source_id)
    result = discover_source(config, db, source, fetcher=FakeFetcher(BOARD_HTML))
    assert result.error is None
    assert result.queued == 1
    queued = [row["url"] for row in db.query("SELECT url FROM discovery_queue ORDER BY id")]
    assert queued == ["https://board.example/auditions"]
