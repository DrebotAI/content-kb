import httpx

from content_kb import notion_store
from content_kb.notion_store import _build_blocks, _build_properties, _retry
from content_kb.tenants import Tenant

notion_store.RETRY_BACKOFF_SECONDS = 0  # без реальних пауз у тестах

KENT = Tenant("kent", 42, "ntn_kent", "11111111-1111-1111-1111-111111111111")
OWNER = Tenant("owner", 7, "ntn_owner", "22222222-2222-2222-2222-222222222222")


class _Api(Exception):
    """Схожа на notion_client.errors.HTTPResponseError — має .status."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def _http_error(status):
    from notion_client.errors import HTTPResponseError

    err = HTTPResponseError.__new__(HTTPResponseError)
    Exception.__init__(err, f"HTTP {status}")
    err.status = status
    return err


class _FakeClient:
    """Мінімальний двійник notion_client.Client — ловить, куди пішов запит."""

    def __init__(self, calls, retrieve=None):
        self.calls = calls
        self._retrieve = retrieve
        self.pages = self
        self.databases = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "page-1", "url": "https://notion.so/page-1"}

    def update(self, **kwargs):
        self.calls.append(kwargs)
        return {}

    def retrieve(self, **kwargs):
        if isinstance(self._retrieve, Exception):
            raise self._retrieve
        return self._retrieve

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": []}


def _fake(tenant, calls, retrieve=None):
    notion_store._clients[tenant.notion_token] = _FakeClient(calls, retrieve)


def test_retry_survives_dropped_connection():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("Connection reset by peer")
        return "ok"

    assert _retry(flaky) == "ok"
    assert len(calls) == 3


def test_retry_gives_up_after_limit():
    def always_down():
        raise httpx.ConnectError("down")

    try:
        _retry(always_down)
    except httpx.ConnectError:
        return
    raise AssertionError("мало прокинути помилку після трьох спроб")


def test_bad_request_is_not_retried():
    from notion_client.errors import HTTPResponseError

    calls = []

    def bad_schema():
        calls.append(1)
        raise _http_error(400)

    try:
        _retry(bad_schema)
    except HTTPResponseError:
        assert len(calls) == 1  # 400 повторювати безглуздо
        return
    raise AssertionError("мало прокинути 400 одразу")


ANALYSIS = {
    "title": "Тест", "tldr": "Суть одним реченням.", "summary": "Три речення.",
    "key_ideas": ["ідея 1", "ідея 2"], "practical": ["інструмент X"],
    "tags": ["AI", "no-code"], "value": "🔥 Must-know", "why_useful": "бо треба",
    "angle": "",
}


def test_each_tenant_writes_into_own_base():
    kent, owner = [], []
    _fake(KENT, kent)
    _fake(OWNER, owner)
    notion_store.save_entry(KENT, ANALYSIS, None, "", "Voice", "")
    notion_store.save_entry(OWNER, ANALYSIS, None, "", "Voice", "")
    assert kent[0]["parent"]["database_id"] == KENT.notion_database_id
    assert owner[0]["parent"]["database_id"] == OWNER.notion_database_id


def test_dedup_query_is_scoped_to_tenant_base():
    """Дубль шукається в базі власника: те, що кент уже зберіг, мене не стосується."""
    calls = []
    _fake(KENT, calls)
    assert notion_store.find_by_link(KENT, "https://instagram.com/reel/1") is None
    assert calls[0]["path"] == f"databases/{KENT.notion_database_id}/query"


def test_client_is_cached_per_token():
    notion_store._clients.clear()
    a = notion_store._client(KENT)
    assert notion_store._client(KENT) is a
    assert notion_store._client(OWNER) is not a
    notion_store._clients.clear()


def test_missing_connection_reads_like_an_instruction():
    _fake(KENT, [], retrieve=_http_error(404))
    problems = notion_store.check_access(KENT)
    assert len(problems) == 1 and "Connections" in problems[0]


def test_bad_token_is_told_apart_from_missing_connection():
    _fake(KENT, [], retrieve=_http_error(401))
    assert "токен" in notion_store.check_access(KENT)[0]


def test_missing_columns_are_listed_by_name():
    _fake(KENT, [], retrieve={"properties": {
        "Name": {"type": "title"}, "Source": {"type": "select"},
        "Value": {"type": "select"}, "Tags": {"type": "select"},
        "Why useful": {"type": "rich_text"}, "Link": {"type": "url"},
        "Creator": {"type": "select"},
    }})
    problems = notion_store.check_access(KENT)
    assert any("Transcript" in p for p in problems)          # колонки нема
    assert any("Tags" in p and "multi_select" in p for p in problems)  # тип не той


def test_healthy_base_has_no_complaints():
    _fake(KENT, [], retrieve={"properties": {
        name: {"type": kind} for name, kind in notion_store.REQUIRED_PROPERTIES.items()}})
    assert notion_store.check_access(KENT) == []


def test_probe_archives_what_it_created():
    calls = []
    _fake(KENT, calls)
    notion_store.probe(KENT)
    assert calls[0]["parent"]["database_id"] == KENT.notion_database_id
    assert calls[1] == {"page_id": "page-1", "archived": True}


def test_angle_adds_its_own_section():
    with_angle = dict(ANALYSIS, angle="Чому клініки втрачають записи вночі")
    blocks = _build_blocks(with_angle, "")
    assert [b["type"] for b in blocks].count("heading_2") == 4
    head = next(b for b in blocks if b["type"] == "heading_2"
                and "Кут" in b["heading_2"]["rich_text"][0]["text"]["content"])
    assert head


def test_properties_full():
    props = _build_properties(ANALYSIS, "https://instagram.com/reel/1", "@author", "IG Reel", "текст")
    assert props["Name"]["title"][0]["text"]["content"] == "Тест"
    assert props["Link"]["url"] == "https://instagram.com/reel/1"
    assert props["Creator"]["select"]["name"] == "@author"
    assert props["Source"]["select"]["name"] == "IG Reel"
    assert props["Value"]["select"]["name"] == "🔥 Must-know"
    assert [t["name"] for t in props["Tags"]["multi_select"]] == ["AI", "no-code"]


def test_content_fields_become_properties_not_just_blocks():
    """37 готових кутів лежали лише в тілі сторінки — їх не відфільтрувати й не вивести списком."""
    rich = dict(ANALYSIS, angle="Чому клініка втрачає записи вночі", hook="Красиві відповіді ще не все",
                content_potential="🔥 Strong angle", recommended_format="carousel")
    props = _build_properties(rich, None, "", "IG Reel")
    assert props["Content Angle"]["rich_text"][0]["text"]["content"].startswith("Чому клініка")
    assert props["Hook"]["rich_text"][0]["text"]["content"] == "Красиві відповіді ще не все"
    assert props["Content Potential"]["select"]["name"] == "🔥 Strong angle"
    assert props["Recommended Format"]["select"]["name"] == "carousel"


def test_content_columns_skipped_when_model_gave_nothing():
    props = _build_properties(dict(ANALYSIS, angle="", hook=""), None, "", "Voice")
    assert "Content Angle" not in props and "Hook" not in props


def test_properties_omit_empty_link_and_creator():
    props = _build_properties(ANALYSIS, None, "", "Voice")
    assert "Link" not in props and "Creator" not in props and "Transcript" not in props


def test_transcript_property_chunked_and_capped():
    props = _build_properties(ANALYSIS, None, "", "Voice", "т" * 4000)
    chunks = props["Transcript"]["rich_text"]
    assert len(chunks) == 3
    assert all(len(c["text"]["content"]) <= 2000 for c in chunks)

    huge = _build_properties(ANALYSIS, None, "", "Voice", "т" * 200_000)
    assert len(huge["Transcript"]["rich_text"]) == 45  # обрізається, а не падає


def test_blocks_order_and_toggle():
    blocks = _build_blocks(ANALYSIS, "т" * 4000)
    assert blocks[0]["type"] == "callout"
    assert blocks[0]["callout"]["rich_text"][0]["text"]["content"] == "Суть одним реченням."
    types = [b["type"] for b in blocks]
    assert types.count("heading_2") == 3
    toggle = blocks[-1]
    assert toggle["type"] == "toggle"
    children = toggle["toggle"]["children"]
    assert len(children) == 3  # 4000 символів / 1900 → 3 чанки
    assert all(c["type"] == "paragraph" for c in children)


def test_blocks_skip_empty_sections_and_transcript():
    empty = dict(ANALYSIS, summary="", key_ideas=[], practical=[])
    blocks = _build_blocks(empty, "")
    assert [b["type"] for b in blocks] == ["callout"]


if __name__ == "__main__":
    for _name, _fn in sorted(dict(globals()).items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")
