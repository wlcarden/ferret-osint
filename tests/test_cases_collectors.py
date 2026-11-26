"""Collectors for Bluesky and Mastodon, against stand-in servers.

No test here touches the network. Each collector is given a client whose
transport is a function, and a resolver that returns fixed addresses.
"""

import httpx
import pytest

from osint_agent.cases import SourceStatus
from osint_agent.cases.collectors import BlueskyCollector, MastodonCollector, guard

PUBLIC = ["9.9.9.9"]
OLD = "2019-01-01T10:00:00Z"
SINCE = "2021-11-25"

# The guard on which servers may be asked


@pytest.mark.parametrize(
    "host", ["mastodon.social", "social.example.org", "a-b.example.co"]
)
def test_should_accept_a_dotted_public_server_name(host):
    assert guard.refusal(host, lambda name: PUBLIC) is None


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "10.0.0.5",
        "example.com:8080",
        "exa mple.com",
        "[::1]",
        "",
    ],
)
def test_should_refuse_anything_that_is_not_a_server_name_without_looking_it_up(host):
    looked_up = []

    why = guard.refusal(host, lambda name: looked_up.append(name) or PUBLIC)

    assert why == "that is not a public server name"
    assert looked_up == []


def test_should_not_take_a_name_with_a_line_break_after_it_for_a_server_name():
    assert guard.is_public_name("mastodon.social") is True
    assert guard.is_public_name("mastodon.social\n") is False


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.10",
        "172.16.0.9",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fe80::1%eth0",
        "::ffff:127.0.0.1",
    ],
)
def test_should_refuse_a_name_that_resolves_to_a_private_or_local_address(address):
    why = guard.refusal("innocent.example.org", lambda name: [address])

    assert why == "the server is not on the public internet"


def test_should_refuse_a_name_when_any_of_its_addresses_is_private():
    why = guard.refusal("mixed.example.org", lambda name: ["9.9.9.9", "192.168.1.10"])

    assert why == "the server is not on the public internet"


def test_should_refuse_a_name_that_does_not_resolve():
    def fails(name: str) -> list[str]:
        raise OSError("no such host")

    assert (
        guard.refusal("gone.example.org", fails) == "the server name does not resolve"
    )
    assert (
        guard.refusal("gone.example.org", lambda name: [])
        == "the server name does not resolve"
    )


# Bluesky


def feed_item(
    text: str, key: str = "3kabc", created: str = "2025-06-02T10:00:00Z", **extra
) -> dict:
    record = {"text": text, "createdAt": created, **extra.pop("record", {})}
    post = {
        "uri": f"at://did:plc:abcdefghijklmnopqrstuvwx/app.bsky.feed.post/{key}",
        "author": {"handle": extra.pop("author", "jw.bsky.social")},
        "record": record,
        **extra.pop("post", {}),
    }
    return {"post": post, **extra}


def bluesky(pages: list[dict] | None = None, handler=None, **settings):
    """A collector served from fixed pages, with the requests and waits it made."""
    requests: list[httpx.Request] = []
    waits: list[float] = []
    queue = list(pages or [])

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if handler:
            return handler(request)
        return httpx.Response(200, json=queue.pop(0))

    client = httpx.Client(transport=httpx.MockTransport(serve))
    return (
        BlueskyCollector(client=client, wait=waits.append, **settings),
        requests,
        waits,
    )


def test_should_map_a_bluesky_post_with_its_date_words_and_link():
    collector, requests, _ = bluesky([{"feed": [feed_item("Range day Saturday.")]}])

    result = collector("@JW.bsky.social")

    post = result.posts[0]
    assert result.status is SourceStatus.ANSWERED
    assert requests[0].url.params["actor"] == "jw.bsky.social"
    assert (post.kind, post.text) == ("post", "Range day Saturday.")
    assert post.posted_at == "2025-06-02T10:00:00Z"
    assert post.url == "https://bsky.app/profile/jw.bsky.social/post/3kabc"
    assert post.native_id.endswith("/app.bsky.feed.post/3kabc")


def test_should_date_a_bluesky_post_by_when_the_server_received_it():
    backdated = feed_item(
        "Hidden.",
        created="2001-01-01T00:00:00Z",
        post={"indexedAt": "2025-06-02T10:00:05Z"},
    )
    collector, _, _ = bluesky([{"feed": [backdated]}])

    result = collector("jw.bsky.social", SINCE)

    assert [post.posted_at for post in result.posts] == ["2025-06-02T10:00:05Z"]


def test_should_mark_a_bluesky_repost_with_whose_words_they_are_and_when_it_was_shared():
    item = feed_item(
        "Stop voting.",
        author="other.bsky.social",
        reason={
            "$type": "app.bsky.feed.defs#reasonRepost",
            "indexedAt": "2025-07-01T08:00:00Z",
        },
    )
    collector, _, _ = bluesky([{"feed": [item]}])

    post = collector("jw.bsky.social").posts[0]

    assert (post.kind, post.posted_at) == ("repost", "2025-07-01T08:00:00Z")
    assert post.text == "Repost of @other.bsky.social: Stop voting."
    assert post.native_id.startswith("repost:at://")


def test_should_give_a_bluesky_reply_the_words_it_answers_as_context():
    parent = {
        "author": {"handle": "kev.bsky.social"},
        "record": {"text": "Where are you?"},
    }
    item = feed_item(
        "Running late.",
        record={"reply": {"parent": {}, "root": {}}},
        reply={"parent": parent},
    )
    collector, _, _ = bluesky([{"feed": [item]}])

    post = collector("jw.bsky.social").posts[0]

    assert post.kind == "reply"
    assert post.context == "replying to @kev.bsky.social: Where are you?"


def test_should_give_a_bluesky_quote_post_the_quoted_words_as_context():
    quoted = {
        "record": {
            "author": {"handle": "hoa.example.org"},
            "value": {"text": "Dues are up."},
        }
    }
    item = feed_item("Exactly this.", post={"embed": quoted})
    collector, _, _ = bluesky([{"feed": [item]}])

    post = collector("jw.bsky.social").posts[0]

    assert (post.text, post.context) == (
        "Exactly this.",
        "quoting @hoa.example.org: Dues are up.",
    )


def test_should_count_the_pictures_on_a_bluesky_post():
    embed = {"$type": "app.bsky.embed.images", "images": [{}, {}, {}]}
    collector, _, _ = bluesky([{"feed": [feed_item("", record={"embed": embed})]}])

    assert collector("jw.bsky.social").posts[0].media == 3


def test_should_follow_bluesky_pages_until_they_run_out_and_pause_between_them():
    pages = [
        {"feed": [feed_item("One.", key="k1")], "cursor": "next"},
        {"feed": [feed_item("Two.", key="k2")]},
    ]
    collector, requests, waits = bluesky(pages, pause=0.5)

    result = collector("jw.bsky.social")

    assert [post.text for post in result.posts] == ["One.", "Two."]
    assert "cursor" not in requests[0].url.params
    assert requests[1].url.params["cursor"] == "next"
    assert waits == [0.5]


def test_should_keep_collecting_bluesky_posts_past_one_old_item_among_new_ones():
    pages = [
        {
            "feed": [
                feed_item("Pinned long ago.", key="k1", created=OLD),
                feed_item("New.", key="k2"),
            ],
            "cursor": "next",
        },
        {"feed": [feed_item("Also new.", key="k3")]},
    ]
    collector, requests, _ = bluesky(pages)

    result = collector("jw.bsky.social", SINCE)

    assert [post.text for post in result.posts] == ["New.", "Also new."]
    assert len(requests) == 2


def test_should_stop_collecting_bluesky_posts_at_a_page_wholly_before_the_review_period():
    pages = [
        {"feed": [feed_item("New.", key="k1")], "cursor": "next"},
        {
            "feed": [
                feed_item("Old.", key="k2", created=OLD),
                feed_item("Older.", key="k3", created=OLD),
            ],
            "cursor": "more",
        },
        {"feed": [feed_item("Never asked for.", key="k4", created=OLD)]},
    ]
    collector, requests, _ = bluesky(pages)

    result = collector("jw.bsky.social", SINCE)

    assert [post.text for post in result.posts] == ["New."]
    assert len(requests) == 2


def test_should_stop_when_bluesky_hands_back_a_page_marker_it_cannot_trust():
    pages = [{"feed": [feed_item("One.", key="k1")], "cursor": "x" * 600}]
    collector, requests, _ = bluesky(pages)

    result = collector("jw.bsky.social")

    assert (len(result.posts), len(requests)) == (1, 1)


@pytest.mark.parametrize(
    "handle", ["../../etc/passwd", "two words.example", "localhost", "", "a.b/c"]
)
def test_should_refuse_a_malformed_bluesky_handle_without_asking_bluesky(handle):
    collector, requests, _ = bluesky([])

    result = collector(handle)

    assert result.status is SourceStatus.FAILED
    assert "not a Bluesky handle" in result.note
    assert requests == []


def test_should_say_when_bluesky_has_no_such_account():
    collector, _, _ = bluesky(
        handler=lambda request: httpx.Response(400, json={"error": "x"})
    )

    result = collector("nobody.bsky.social")

    assert result.status is SourceStatus.FAILED
    assert result.note == "Bluesky has no account with that handle."


def test_should_report_failure_when_bluesky_cannot_be_reached():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    collector, _, _ = bluesky(handler=refuse)

    result = collector("jw.bsky.social")

    assert result.status is SourceStatus.FAILED
    assert "could not be reached" in result.note


def test_should_keep_what_it_has_and_say_it_is_incomplete_when_bluesky_fails_part_way():
    replies = iter(
        [
            httpx.Response(
                200, json={"feed": [feed_item("One.", key="k1")], "cursor": "next"}
            ),
            httpx.Response(502),
        ]
    )
    collector, _, _ = bluesky(handler=lambda request: next(replies))

    result = collector("jw.bsky.social")

    assert result.status is SourceStatus.ANSWERED
    assert (len(result.posts), result.complete) == (1, False)
    assert "older ones are missing" in result.note


def test_should_stop_at_the_post_limit_and_say_so():
    pages = [
        {
            "feed": [feed_item("One.", key="k1"), feed_item("Two.", key="k2")],
            "cursor": "next",
        },
        {"feed": [feed_item("Three.", key="k3")]},
    ]
    collector, requests, _ = bluesky(pages, max_posts=2)

    result = collector("jw.bsky.social")

    assert (len(result.posts), result.complete, len(requests)) == (2, False, 1)
    assert "post limit" in result.note


def test_should_skip_bluesky_entries_it_cannot_use_and_give_no_link_for_an_odd_author():
    feed = [
        {"post": {"record": {"text": "No address."}}},
        "junk",
        feed_item("Kept.", author="not a name"),
    ]
    collector, _, _ = bluesky([{"feed": feed}])

    result = collector("jw.bsky.social")

    assert [(post.text, post.url) for post in result.posts] == [("Kept.", None)]


def test_should_give_no_link_when_a_bluesky_record_key_ends_in_a_line_break():
    collector, _, _ = bluesky([{"feed": [feed_item("Kept.", key="3kabc\n")]}])

    assert collector("jw.bsky.social").posts[0].url is None


# Mastodon


def status(
    number: int,
    content: str = "<p>Hello.</p>",
    created: str = "2025-06-02T10:00:00Z",
    **extra,
) -> dict:
    return {
        "id": str(number),
        "created_at": created,
        "content": content,
        "url": f"https://example.social/@jw/{number}",
        **extra,
    }


def mastodon(
    pages: list[list[dict]] | None = None, handler=None, addresses=None, **settings
):
    """A collector served from fixed pages, with the requests it made and names it resolved."""
    requests: list[httpx.Request] = []
    resolved: list[str] = []
    queue = list(pages or [])

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if handler:
            return handler(request)
        if request.url.path == "/api/v1/accounts/lookup":
            return httpx.Response(200, json={"id": "42"})
        return httpx.Response(200, json=queue.pop(0) if queue else [])

    def resolver(name: str) -> list[str]:
        resolved.append(name)
        return addresses or PUBLIC

    collector = MastodonCollector(
        client=httpx.Client(transport=httpx.MockTransport(serve)),
        wait=lambda seconds: None,
        resolver=resolver,
        **settings,
    )
    return collector, requests, resolved


def test_should_ask_the_server_named_in_the_handle_for_the_account_then_its_posts():
    collector, requests, resolved = mastodon([[status(7)]])

    result = collector("@jw@Example.Social")

    assert result.status is SourceStatus.ANSWERED
    assert resolved == ["example.social"]
    assert [(r.url.host, r.url.path) for r in requests[:2]] == [
        ("example.social", "/api/v1/accounts/lookup"),
        ("example.social", "/api/v1/accounts/42/statuses"),
    ]
    assert requests[0].url.params["acct"] == "jw"


def test_should_strip_markup_from_a_mastodon_post_and_keep_its_warning():
    content = "<p>Line one<br />line &amp; two</p><p>Next &lt;para&gt;</p>"
    collector, _, _ = mastodon(
        [[status(7, content, spoiler_text="politics", media_attachments=[{}])]]
    )

    post = collector("jw@example.social").posts[0]

    assert post.text == "politics: Line one\nline & two\nNext <para>"
    assert (post.kind, post.media, post.native_id) == ("post", 1, "example.social:7")
    assert post.url == "https://example.social/@jw/7"


def test_should_mark_a_mastodon_boost_with_whose_words_they_are():
    boosted = {
        "content": "<p>Stop voting.</p>",
        "account": {"acct": "other@far.example"},
        "url": "https://far.example/@other/1",
    }
    collector, _, _ = mastodon([[status(7, "", reblog=boosted)]])

    post = collector("jw@example.social").posts[0]

    assert (post.kind, post.text) == (
        "repost",
        "Repost of @other@far.example: Stop voting.",
    )
    assert post.url == "https://far.example/@other/1"


def test_should_mark_a_mastodon_reply_and_drop_a_link_that_is_not_a_web_address():
    collector, _, _ = mastodon(
        [[status(7, in_reply_to_id="5", url="javascript:alert(1)")]]
    )

    post = collector("jw@example.social").posts[0]

    assert (post.kind, post.url) == ("reply", None)


def test_should_page_through_mastodon_posts_by_the_oldest_id_seen():
    collector, requests, _ = mastodon([[status(9), status(8)], [status(7)], []])

    result = collector("jw@example.social")

    assert [post.native_id for post in result.posts] == [
        f"example.social:{n}" for n in (9, 8, 7)
    ]
    assert [r.url.params.get("max_id") for r in requests[1:]] == [None, "8", "7"]


def test_should_keep_collecting_mastodon_posts_past_one_old_item_among_new_ones():
    pages = [[status(9, created=OLD), status(8)], [status(7)], []]
    collector, _, _ = mastodon(pages)

    result = collector("jw@example.social", SINCE)

    assert [post.native_id for post in result.posts] == [
        "example.social:8",
        "example.social:7",
    ]


def test_should_stop_collecting_mastodon_posts_at_a_page_wholly_before_the_review_period():
    pages = [[status(9)], [status(8, created=OLD), status(7, created=OLD)], [status(6)]]
    collector, requests, _ = mastodon(pages)

    result = collector("jw@example.social", SINCE)

    assert [post.native_id for post in result.posts] == ["example.social:9"]
    assert len(requests) == 3  # The lookup and two pages


@pytest.mark.parametrize(
    "handle",
    [
        "jw",
        "jw@",
        "jw@localhost",
        "jw@127.0.0.1",
        "jw@example.social:8080",
        "j w@example.social",
    ],
)
def test_should_refuse_a_mastodon_handle_that_names_no_public_server_without_any_request(
    handle,
):
    collector, requests, resolved = mastodon([[status(7)]])

    result = collector(handle)

    assert result.status is SourceStatus.FAILED
    assert requests == []
    assert resolved == []


def test_should_refuse_a_server_on_the_local_network_without_any_request():
    collector, requests, _ = mastodon([[status(7)]], addresses=["192.168.1.1"])

    result = collector("jw@router.example.org")

    assert result.status is SourceStatus.FAILED
    assert "not on the public internet" in result.note
    assert requests == []


def test_should_not_follow_a_redirect_from_a_mastodon_server():
    redirect = httpx.Response(
        302, headers={"Location": "http://127.0.0.1:11434/api/tags"}
    )
    collector, requests, _ = mastodon(handler=lambda request: redirect)

    result = collector("jw@example.social")

    assert result.status is SourceStatus.FAILED
    assert "redirect" in result.note
    assert len(requests) == 1


def test_should_say_when_a_mastodon_server_has_no_such_account():
    collector, _, _ = mastodon(
        handler=lambda request: httpx.Response(404, json={"error": "x"})
    )

    result = collector("nobody@example.social")

    assert result.note == "example.social has no account with that handle."


@pytest.mark.parametrize("account_id", ["../../admin", "42\n", "42/../../x", ""])
def test_should_refuse_an_account_id_that_could_change_the_address_asked(account_id):
    collector, requests, _ = mastodon(
        handler=lambda request: httpx.Response(200, json={"id": account_id})
    )

    result = collector("jw@example.social")

    assert result.status is SourceStatus.FAILED
    assert len(requests) == 1


def test_should_skip_a_mastodon_post_whose_id_ends_in_a_line_break():
    collector, _, _ = mastodon([[{**status(9), "id": "9\n"}, status(8)]])

    result = collector("jw@example.social")

    assert [post.native_id for post in result.posts] == ["example.social:8"]


def test_should_keep_what_it_has_when_a_mastodon_server_fails_part_way():
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lookup"):
            return httpx.Response(200, json={"id": "42"})
        if "max_id" in request.url.params:
            return httpx.Response(500)
        return httpx.Response(200, json=[status(9)])

    collector, _, _ = mastodon(handler=serve)

    result = collector("jw@example.social")

    assert (result.status, len(result.posts), result.complete) == (
        SourceStatus.ANSWERED,
        1,
        False,
    )
