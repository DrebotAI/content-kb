import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.6-sol")
CODEX_REASONING = os.getenv("CODEX_REASONING", "medium")
# дайджест із пачки сторіз довший за одиничний аналіз — 180 с на нього не вистачає
CODEX_TIMEOUT_SECONDS = int(os.getenv("CODEX_TIMEOUT_SECONDS", "300"))


def _codex_argv(out_path: str, model: str | None, images: list | None = None) -> list:
    # промпт іде в stdin (аргумент "-"): у Linux один argv ріжеться на 128 КБ,
    # а кирилиця по 2 байти — година подкасту впала б з E2BIG
    argv = [CODEX_BIN, "exec", "--skip-git-repo-check", "-s", "read-only", "-o", out_path]
    if model:
        argv += ["-m", model, "-c", f'model_reasoning_effort="{CODEX_REASONING}"']
    for path in images or []:
        argv += ["-i", path]
    return argv + ["-"]


def _run_codex(prompt: str, images: list | None = None) -> str:
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        out_path = tmp.name
    try:
        # другий захід — без -m: якщо CLI оновиться і перестане знати нашу модель,
        # бот виживе на дефолтній. Заразом покриває транзієнтні падіння.
        errors = []
        for model in (CODEX_MODEL, None):
            try:
                result = subprocess.run(
                    _codex_argv(out_path, model, images),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=CODEX_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"модель {model or 'дефолтна'}: таймаут {CODEX_TIMEOUT_SECONDS} с")
                continue
            if result.returncode == 0:
                return Path(out_path).read_text().strip()
            errors.append(f"модель {model or 'дефолтна'}: {result.stderr.strip()[:200]}")
        raise RuntimeError("codex exec не спрацював — " + "; ".join(errors))
    finally:
        os.unlink(out_path)


_VALUES = ("🔥 Must-know", "👍 Корисно", "📎 Довідково")
# друга, незалежна шкала: банальний для власника матеріал може мати сильний кут,
# а глибокий технічний розбір — не лягати в контент узагалі
_POTENTIALS = ("🔥 Strong angle", "👍 Adaptable", "📎 Weak")
_FORMATS = ("Reel", "talking-head Reel", "screen recording", "carousel",
            "Telegram post", "story sequence", "technical breakdown", "case study",
            "не для контенту")
_TAGS = ("контент-ідея", "продукт/курс", "делівері", "продажі", "лідген")

# Дві шкали навмисно розведені. Раніше була одна, зі словами «цього тижня» й «активного
# деала» — і вся бібліотека міряла себе по одному поточному проєкту: банальний матеріал
# із сильним хуком тонув у 📎, а глибокий технічний розбір без кута ліз у 🔥.
_CRITERIA = """Дві НЕЗАЛЕЖНІ оцінки. Не змішуй їх і не підганяй одну під одну.

value — цінність для навчання й роботи:
🔥 Must-know — конкретне знання: implementation detail, фреймворк, архітектура, промпт,
  тул із чітким use case; або те, що покращує positioning, лідген, продажі, продуктизацію;
  або знімає активний блокер чи змінює поточне професійне рішення. Головне — перевірюване
  й застосовне на практиці.
👍 Корисно — тематично й стратегічно релевантно, дає контекст, приклад або новий погляд,
  може знадобитися пізніше; але без термінового застосування чи глибини на 🔥.
📎 Довідково — поверхнево, generic-порада, переказ новин, очевидне для власника,
  тул без use case. Зберігаємо лише як джерело.

content_potential — потенціал зробити з цього ВЛАСНИЙ контент:
🔥 Strong angle — є хук або напруга, є контрарна теза, є куди підставити власний кейс,
  тема лягає в позиціонування, проблема зрозуміла власникам бізнесу, є практичний висновок,
  і ідею можна суттєво трансформувати, а не скопіювати.
👍 Adaptable — тема релевантна аудиторії, Reel/пост проглядається, але бракує тези, кейсу
  або прикладу; годиться як частина більшого матеріалу.
📎 Weak — нема зрозумілого хука, не лягає в позиціонування, занадто generic, нема куди
  додати власний досвід, перепакування вийде копіюванням.
🔥 став калібровано, а не щедро: якщо кут тримається на тезі чи кейсі, яких у власника поки
  нема, це 👍, не 🔥. Шкала, де все 🔥, не сортує нічого.

Комбінації нормальні й очікувані: 📎 value + 🔥 content_potential (банальний список тулів,
але з нього робиться сильна контртеза) або 🔥 value + 👍 content_potential (глибокий
технічний розбір, який треба сильно адаптувати, щоб став зрозумілим Reel)."""

CONTEXT_FILE = Path(__file__).with_name("context.md")

# коли контекст-файл тенанта не знайдено: краще чесно оцінювати без профілю,
# ніж міряти його контент чужими цілями
_NO_PROFILE = """Власник цієї бази не лишив опису себе й своїх цілей.
Прив'язки до конкретного проєкту не вигадуй: у why_useful так і напиши,
що контексту власника немає, і став 📎 Довідково, крім випадків, коли матеріал
самоцінний сам по собі."""


def profile(path=None) -> str:
    """Читається на кожен аналіз: правка контекст-файла діє одразу, без рестарту.

    Профілю нема — оцінюємо без нього. Вшита копія чийогось профілю була б гірша
    за її відсутність: чужий контент мірявся б чужими деалами, і все осмислене
    тихо ставало б 📎 Довідково.
    """
    path = Path(path) if path else CONTEXT_FILE
    try:
        text = path.read_text().strip()
    except OSError:
        text = ""
    if not text:
        logger.warning("немає профілю %s — оцінюю без контексту власника", path)
        return _NO_PROFILE
    return text


def analyze(content: str, link: str, profile_path=None) -> dict:
    prompt = (
        "Ти наповнюєш content and learning library — це НЕ просто база знань і НЕ список "
        "«що корисно для поточного деала». У власника дві незалежні цілі: (1) навчитися — "
        "зрозуміти метод, тул, фреймворк, implementation detail чи бізнес-інсайт; "
        "(2) перепакувати — зняти власний Reel/пост із власним досвідом і позиціонуванням.\n\n"
        "Ось власник:\n\n"
        f"{profile(profile_path)}\n\n"
        + _CRITERIA +
        "\n\nПравила перепакування (жорсткі):\n"
        "- Витягай ідею, не копіюй формулювання автора.\n"
        "- Не вигадуй за власника досвід, кейси, результати чи цифри. Якщо для кута бракує "
        "його власного доказу — так і напиши в own_proof.\n"
        "- Якщо в матеріалі є специфічна авторська методика чи унікальна теза — зазнач "
        "в own_proof, що потрібна атрибуція.\n\n"
        "Відповідь — ЛИШЕ JSON без жодного тексту навколо, формат:\n"
        '{"title": "заголовок-суть, до 80 символів", '
        '"tldr": "вся суть одним реченням", '
        '"summary": "3-5 речень", '
        '"source_idea": "головна ідея оригіналу без води", '
        '"key_ideas": ["інсайт або цитата", ...], '
        '"practical": ["що застосувати / згаданий інструмент чи сервіс", ...], '
        '"learning_takeaway": "конкретно: що перевірити, застосувати, додати в свої системи '
        'чи дослідити далі. Не «це корисно для розвитку AI-напряму», а перевірювана дія", '
        f'"tags": підмножина {list(_TAGS)} — тільки з цього списку, нічого не вигадуй, '
        f'"value": одне з {list(_VALUES)} — цінність для навчання й роботи, '
        f'"content_potential": одне з {list(_POTENTIALS)} — потенціал для ВЛАСНОГО контенту, '
        "оцінюй незалежно від value, "
        '"why_useful": "чому це корисно для навчання/застосування", '
        '"content_angle": "авторський кут власника: що він скаже від себе, яку тезу оскаржить, '
        'яку помилку бізнесу покаже. Не переказ. Порожній рядок, якщо кута нема", '
        '"hook": "перший рядок Reels/поста — одне речення, живою українською, без AI-slop", '
        '"adaptation": ["кроки, як перепакувати: взяти проблему, замінити чужий приклад своїм, '
        'додати контртезу, показати workflow, завершити висновком"], '
        '"own_proof": "який власний кейс/скрін/логіку системи додати; або чесно — якого доказу '
        'у власника бракує", '
        f'"recommended_format": одне з {list(_FORMATS)}'
        "}\n"
        "Мова полів — така сама, як мова контенту, крім content_angle і hook: вони завжди "
        "українською.\n"
        f"Посилання: {link}\n"
        f"Контент:\n{content}"
    )
    return _normalize(_extract_json(_run_codex(prompt)))


def _normalize(data: dict) -> dict:
    return {
        "title": str(data.get("title") or "")[:200] or "Без назви",
        "tldr": str(data.get("tldr") or ""),
        "summary": str(data.get("summary") or ""),
        "source_idea": str(data.get("source_idea") or ""),
        "key_ideas": [str(x) for x in data.get("key_ideas") or []],
        "practical": [str(x) for x in data.get("practical") or []],
        "learning_takeaway": str(data.get("learning_takeaway") or ""),
        # теги — тільки з фіксованого списку, інакше multi-select засмітиться за місяць
        "tags": [str(x) for x in (data.get("tags") or []) if str(x) in _TAGS],
        "value": data["value"] if data.get("value") in _VALUES else "📎 Довідково",
        "content_potential": (data["content_potential"]
                              if data.get("content_potential") in _POTENTIALS else "📎 Weak"),
        "why_useful": str(data.get("why_useful") or ""),
        # angle лишається ключем: те саме поле, тепер із власною колонкою в Notion
        "angle": str(data.get("content_angle") or data.get("angle") or ""),
        "hook": str(data.get("hook") or ""),
        "adaptation": [str(x) for x in data.get("adaptation") or []],
        "own_proof": str(data.get("own_proof") or ""),
        "recommended_format": (data["recommended_format"]
                               if data.get("recommended_format") in _FORMATS else ""),
    }


def read_image(paths: list, caption: str = "") -> str:
    """Скрін поста → текст. Далі йде тим самим шляхом, що й транскрипт голосової."""
    prompt = (
        "На зображенні — скріншот поста або слайда каруселі із соцмережі. "
        "Випиши весь видимий текст дослівно, у правильному порядку, мовою оригіналу: "
        "заголовок, тіло, підписи на картинці, автора й нік, якщо видно. "
        "Якщо крім тексту є щось змістовне (схема, графік, скрін інтерфейсу) — опиши одним "
        "абзацом. Виведи тільки сам зміст, без пояснень і коментарів від себе."
    )
    if caption:
        prompt += f"\n\nПідпис, який користувач надіслав разом із фото:\n{caption}"
    return _run_codex(prompt, images=paths)


def compile_digest(items: list) -> str:
    joined = "\n\n---\n\n".join(items)
    prompt = (
        "Нижче — пачка повідомлень (текстових і транскрибованих голосових), надісланих "
        "підряд одним користувачем. Склади один зв'язний зведений документ тією ж мовою: "
        "об'єднай суть, прибери повтори й шум, збережи всі важливі факти та деталі. "
        "Не додавай нічого від себе поза змістом повідомлень. Виведи тільки готовий документ, "
        "без пояснень і без обгортки в лапки чи markdown-код.\n\n" + joined
    )
    return _run_codex(prompt)


def _extract_json(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Codex не повернув JSON: {raw[:200]}")
    return json.loads(raw[start : end + 1])
