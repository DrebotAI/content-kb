import tempfile
from pathlib import Path

from ai_engine import CODEX_MODEL, _codex_argv, _extract_json, _normalize, profile


def test_argv_passes_prompt_via_stdin_not_argument():
    argv = _codex_argv("/tmp/out.txt", CODEX_MODEL)
    assert argv[-1] == "-"  # "-" = читай промпт зі stdin, інакше E2BIG на довгому тексті
    assert "-m" in argv and CODEX_MODEL in argv


def test_argv_without_model_drops_model_flags():
    argv = _codex_argv("/tmp/out.txt", None)
    assert "-m" not in argv and not any("reasoning" in a for a in argv)
    assert argv[-1] == "-"


def test_argv_attaches_images_before_stdin_marker():
    argv = _codex_argv("/tmp/out.txt", None, ["/tmp/a.jpg", "/tmp/b.jpg"])
    assert argv.count("-i") == 2 and "/tmp/b.jpg" in argv
    assert argv[-1] == "-"  # картинки не мають витіснити stdin-промпт


def test_profile_reads_context_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "context.md"
        path.write_text("Ніша — booking-агенти.")
        assert "booking-агенти" in profile(path)


def test_criteria_are_global_not_locked_to_this_weeks_deal():
    """Через це формулювання вся бібліотека й мірялась одним поточним проєктом."""
    from ai_engine import _CRITERIA
    assert "цього тижня" not in _CRITERIA
    assert "content_potential" in _CRITERIA and "value" in _CRITERIA


def test_profile_of_another_tenant_is_his_own():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "context.second-owner.md"
        path.write_text("Возить каву, ніша — обсмажування.")
        assert "каву" in profile(path)


def test_missing_profile_does_not_leak_anyone_elses():
    """Найгірший тихий баг мультітенанта: чужий контент оцінюється під мої деали.

    Профілю нема — має прийти саме заглушка «контексту немає», а не текст,
    який хтось лишив у своєму context-файлі.
    """
    with tempfile.TemporaryDirectory() as tmp:
        mine = Path(tmp) / "context.md"
        mine.write_text("Ніша — booking-агенти для стоматологій, деал у роботі.")
        absent = Path(tmp) / "context.no-such-tenant.md"
        text = profile(absent)
        assert "booking-агенти" not in text and "стоматолог" not in text
        assert "контексту" in text or "не лишив" in text


def test_empty_profile_file_is_treated_as_no_profile():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "context.md"
        path.write_text("   \n\n")
        assert profile(path) == profile(Path(tmp) / "context.absent.md")


def test_extract_json_strips_surrounding_text():
    raw = 'Ось відповідь:\n{"topic": "AI", "summary": "x"}\nДякую'
    assert _extract_json(raw) == {"topic": "AI", "summary": "x"}


def test_extract_json_raises_without_braces():
    try:
        _extract_json("не json взагалі")
    except ValueError:
        return
    raise AssertionError("очікувався ValueError")


def test_normalize_fills_defaults_and_fixes_bad_value():
    result = _normalize({"title": "X", "value": "щось ліве", "content_potential": "нема такого"})
    assert result["title"] == "X"
    assert result["value"] == "📎 Довідково"  # сумнів трактуємо на користь «довідково»
    assert result["content_potential"] == "📎 Weak"
    assert result["key_ideas"] == []
    assert result["practical"] == []
    assert result["tags"] == []
    assert result["tldr"] == ""
    assert result["angle"] == ""
    assert result["hook"] == "" and result["adaptation"] == []
    assert result["recommended_format"] == ""


def test_two_scales_are_independent():
    """Сенс усієї переробки: банальне для навчання може мати сильний кут для контенту."""
    result = _normalize({"value": "📎 Довідково", "content_potential": "🔥 Strong angle"})
    assert result["value"] == "📎 Довідково"
    assert result["content_potential"] == "🔥 Strong angle"


def test_normalize_reads_angle_from_content_angle_key():
    # промпт тепер називає поле content_angle, а решта коду знає його як angle
    assert _normalize({"content_angle": "кут"})["angle"] == "кут"


def test_normalize_drops_format_outside_fixed_list():
    assert _normalize({"recommended_format": "TikTok-танець"})["recommended_format"] == ""
    assert _normalize({"recommended_format": "carousel"})["recommended_format"] == "carousel"


def test_normalize_keeps_valid_value_and_coerces_lists():
    result = _normalize({
        "title": "T", "value": "🔥 Must-know", "angle": "кут для рілса",
        "key_ideas": ["a", 5], "tags": ["продажі"], "practical": ["x"],
    })
    assert result["value"] == "🔥 Must-know"
    assert result["key_ideas"] == ["a", "5"]
    assert result["tags"] == ["продажі"]
    assert result["angle"] == "кут для рілса"


def test_normalize_drops_tags_outside_fixed_list():
    result = _normalize({"tags": ["продажі", "AI-кодинг", "лідген", "промптинг"]})
    assert result["tags"] == ["продажі", "лідген"]


def test_normalize_empty_title_becomes_placeholder():
    assert _normalize({})["title"] == "Без назви"


if __name__ == "__main__":
    for _name, _fn in sorted(dict(globals()).items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")
