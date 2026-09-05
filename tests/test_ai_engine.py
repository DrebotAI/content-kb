import contextlib
import importlib
import os
import tempfile
from pathlib import Path

from content_kb import ai_engine
from content_kb.ai_engine import CODEX_MODEL, _codex_argv, _extract_json, _normalize, profile


@contextlib.contextmanager
def _reloaded(**env):
    """Reloads ai_engine under a temporary environment and puts everything back.

    LANGUAGE/VALUES/... are read at module import, so KB_LANGUAGE can only be switched
    through a reload. The environment is restored and reloaded once more at the end —
    otherwise the rest of the test run would be stuck in someone else's language.
    """
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(ai_engine)
    try:
        yield ai_engine
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(ai_engine)


def test_argv_passes_prompt_via_stdin_not_argument():
    argv = _codex_argv("/tmp/out.txt", CODEX_MODEL)
    assert argv[-1] == "-"  # "-" = read the prompt from stdin, otherwise E2BIG on long text
    assert "-m" in argv and CODEX_MODEL in argv


def test_argv_without_model_drops_model_flags():
    argv = _codex_argv("/tmp/out.txt", None)
    assert "-m" not in argv and not any("reasoning" in a for a in argv)
    assert argv[-1] == "-"


def test_argv_attaches_images_before_stdin_marker():
    argv = _codex_argv("/tmp/out.txt", None, ["/tmp/a.jpg", "/tmp/b.jpg"])
    assert argv.count("-i") == 2 and "/tmp/b.jpg" in argv
    assert argv[-1] == "-"  # images must not push out the stdin prompt


def test_profile_reads_context_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "context.md"
        path.write_text("Niche — booking agents.")
        assert "booking agents" in profile(path)


def test_criteria_are_global_not_locked_to_this_weeks_deal():
    """This wording is exactly what made the whole library measure itself against one project."""
    from content_kb.ai_engine import _CRITERIA
    assert "this week" not in _CRITERIA
    assert "content_potential" in _CRITERIA and "value" in _CRITERIA


def test_profile_of_another_tenant_is_his_own():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "context.second-owner.md"
        path.write_text("Ships coffee, niche — roasting.")
        assert "coffee" in profile(path)


def test_missing_profile_does_not_leak_anyone_elses():
    """The worst silent multi-tenant bug: one person's content rated against another's deals.

    With no profile the "no context" placeholder must come back, not whatever text
    somebody else left in their own context file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        mine = Path(tmp) / "context.md"
        mine.write_text("Niche — booking agents for dental clinics, a deal in progress.")
        absent = Path(tmp) / "context.no-such-tenant.md"
        text = profile(absent)
        assert "booking agents" not in text and "dental" not in text
        assert "no owner context" in text or "has not left" in text


def test_empty_profile_file_is_treated_as_no_profile():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "context.md"
        path.write_text("   \n\n")
        assert profile(path) == profile(Path(tmp) / "context.absent.md")


def test_extract_json_strips_surrounding_text():
    raw = 'Here is the answer:\n{"topic": "AI", "summary": "x"}\nThanks'
    assert _extract_json(raw) == {"topic": "AI", "summary": "x"}


def test_extract_json_raises_without_braces():
    try:
        _extract_json("not json at all")
    except ValueError:
        return
    raise AssertionError("expected a ValueError")


def test_normalize_fills_defaults_and_fixes_bad_value():
    result = _normalize({"title": "X", "value": "junk", "content_potential": "no such thing"})
    assert result["title"] == "X"
    assert result["value"] == "📎 Reference"  # when in doubt, fall to "reference"
    assert result["content_potential"] == "📎 Weak"
    assert result["key_ideas"] == []
    assert result["practical"] == []
    assert result["tags"] == []
    assert result["tldr"] == ""
    assert result["angle"] == ""
    assert result["hook"] == "" and result["adaptation"] == []
    assert result["recommended_format"] == ""


def test_two_scales_are_independent():
    """The point of the whole rework: the mundane can still carry a strong content angle."""
    result = _normalize({"value": "📎 Reference", "content_potential": "🔥 Strong angle"})
    assert result["value"] == "📎 Reference"
    assert result["content_potential"] == "🔥 Strong angle"


def test_normalize_reads_angle_from_content_angle_key():
    # the prompt now calls the field content_angle, while the rest of the code knows it as angle
    assert _normalize({"content_angle": "an angle"})["angle"] == "an angle"


def test_normalize_drops_format_outside_fixed_list():
    assert _normalize({"recommended_format": "TikTok dance"})["recommended_format"] == ""
    assert _normalize({"recommended_format": "carousel"})["recommended_format"] == "carousel"


def test_normalize_keeps_valid_value_and_coerces_lists():
    result = _normalize({
        "title": "T", "value": "🔥 Must-know", "angle": "an angle for a Reel",
        "key_ideas": ["a", 5], "tags": ["sales"], "practical": ["x"],
    })
    assert result["value"] == "🔥 Must-know"
    assert result["key_ideas"] == ["a", "5"]
    assert result["tags"] == ["sales"]
    assert result["angle"] == "an angle for a Reel"


def test_normalize_drops_tags_outside_fixed_list():
    result = _normalize({"tags": ["sales", "AI coding", "lead gen", "prompting"]})
    assert result["tags"] == ["sales", "lead gen"]


def test_normalize_empty_title_becomes_placeholder():
    assert _normalize({})["title"] == "Untitled"


def test_default_language_is_en():
    assert ai_engine.LANGUAGE == "en"
    assert ai_engine.VALUES == ("🔥 Must-know", "👍 Useful", "📎 Reference")


def test_kb_language_en_switches_labels_tags_and_prompt():
    with _reloaded(KB_LANGUAGE="en") as mod:
        assert mod.LANGUAGE == "en"
        assert mod.VALUES == ("🔥 Must-know", "👍 Useful", "📎 Reference")
        assert mod.POTENTIALS == ("🔥 Strong angle", "👍 Adaptable", "📎 Weak")
        assert mod.FORMATS[-1] == "not for content"
        assert mod.TAGS == ("content idea", "product/course", "delivery", "sales", "lead gen")

        captured = {}

        def fake_run_codex(prompt, images=None):
            captured["prompt"] = prompt
            return '{"title": "T"}'

        mod._run_codex = fake_run_codex
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.md"
            path.write_text("owner profile")
            mod.analyze("some content", "http://x", profile_path=path)
        assert "populating a content and learning library" in captured["prompt"]
        assert "language of every field is English" in captured["prompt"]
        assert "Ти наповнюєш" not in captured["prompt"]


def test_kb_language_uk_switches_labels_tags_and_prompt():
    with _reloaded(KB_LANGUAGE="uk") as mod:
        assert mod.LANGUAGE == "uk"
        assert mod.VALUES == ("🔥 Must-know", "👍 Корисно", "📎 Довідково")
        assert mod.TAGS == ("контент-ідея", "продукт/курс", "делівері", "продажі", "лідген")
        assert mod.FORMATS[-1] == "не для контенту"

        captured = {}

        def fake_run_codex(prompt, images=None):
            captured["prompt"] = prompt
            return '{"title": "T"}'

        mod._run_codex = fake_run_codex
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.md"
            path.write_text("owner profile")
            mod.analyze("some content", "http://x", profile_path=path)
        assert "Ти наповнюєш" in captured["prompt"]
        assert "Мова всіх полів — українська" in captured["prompt"]


def test_kb_language_auto_keeps_en_labels_and_prompt_but_frees_hook_language():
    """auto: the labels (Notion enums) stay English, while hook/content_angle follow the
    language of the content along with the rest of the fields instead of being forced."""
    with _reloaded(KB_LANGUAGE="auto") as mod:
        assert mod.LANGUAGE == "auto"
        assert mod.VALUES == ("🔥 Must-know", "👍 Useful", "📎 Reference")
        assert mod.TAGS == ("content idea", "product/course", "delivery", "sales", "lead gen")

        captured = {}

        def fake_run_codex(prompt, images=None):
            captured["prompt"] = prompt
            return '{"title": "T"}'

        mod._run_codex = fake_run_codex
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.md"
            path.write_text("owner profile")
            mod.analyze("some content", "http://x", profile_path=path)
        assert "populating a content and learning library" in captured["prompt"]
        assert "matches the language of the content" in captured["prompt"]
        assert "language of every field is English" not in captured["prompt"]


def test_kb_language_unknown_falls_back_to_en_and_warns(caplog):
    with caplog.at_level("WARNING"):
        with _reloaded(KB_LANGUAGE="fr") as mod:
            assert mod.LANGUAGE == "en"
            assert mod.VALUES == ("🔥 Must-know", "👍 Useful", "📎 Reference")
    assert any("unknown" in r.message.lower() and "fr" in r.message for r in caplog.records)


def test_kb_tags_overrides_default_list():
    with _reloaded(KB_TAGS="  a, b c , , d ") as mod:
        assert mod.TAGS == ("a", "b c", "d")


def test_kb_tags_blank_falls_back_to_language_default():
    with _reloaded(KB_TAGS="  , , ") as mod:
        assert mod.TAGS == ("content idea", "product/course", "delivery", "sales", "lead gen")


def test_kb_tags_overrides_in_en_mode_too():
    with _reloaded(KB_LANGUAGE="en", KB_TAGS="x, y") as mod:
        assert mod.TAGS == ("x", "y")


def test_normalize_still_rejects_out_of_set_value_in_en_mode():
    with _reloaded(KB_LANGUAGE="en") as mod:
        result = mod._normalize({"value": "junk", "content_potential": "no such thing"})
        assert result["value"] == "📎 Reference"
        assert result["content_potential"] == "📎 Weak"


def test_setup_notion_schema_matches_ai_engine_tuples():
    """The drift this rework was meant to remove: SCHEMA used to duplicate the labels by
    hand — changing KB_LANGUAGE/KB_TAGS can no longer desync the base from the prompt."""
    from tools import setup_notion

    def names(prop):
        return [o["name"] for o in prop["options"]]

    assert names(setup_notion.SCHEMA["Value"]["select"]) == list(ai_engine.VALUES)
    assert names(setup_notion.SCHEMA["Content Potential"]["select"]) == list(ai_engine.POTENTIALS)
    assert names(setup_notion.SCHEMA["Recommended Format"]["select"]) == list(ai_engine.FORMATS)
    assert names(setup_notion.SCHEMA["Tags"]["multi_select"]) == list(ai_engine.TAGS)


if __name__ == "__main__":
    for _name, _fn in sorted(dict(globals()).items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")
