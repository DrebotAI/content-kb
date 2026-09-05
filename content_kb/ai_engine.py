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
# a digest of a story batch runs longer than a single analysis — 180 s is not enough
CODEX_TIMEOUT_SECONDS = int(os.getenv("CODEX_TIMEOUT_SECONDS", "300"))


def _codex_argv(out_path: str, model: str | None, images: list | None = None) -> list:
    # the prompt goes in on stdin (the "-" argument): on Linux a single argv is capped
    # at 128 KB, and non-ASCII text costs 2 bytes a character — an hour of podcast would
    # have died with E2BIG
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
        # second attempt without -m: if the CLI updates and stops recognising our model,
        # the bot survives on the default one. It also covers transient failures.
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
                errors.append(f"model {model or 'default'}: timed out after {CODEX_TIMEOUT_SECONDS} s")
                continue
            if result.returncode == 0:
                return Path(out_path).read_text().strip()
            errors.append(f"model {model or 'default'}: {result.stderr.strip()[:200]}")
        raise RuntimeError("codex exec did not succeed — " + "; ".join(errors))
    finally:
        os.unlink(out_path)


LANGUAGE = (os.getenv("KB_LANGUAGE") or "en").strip().lower()
if LANGUAGE not in ("uk", "en", "auto"):
    logger.warning("unknown KB_LANGUAGE=%r, falling back to en", LANGUAGE)
    LANGUAGE = "en"

# labels and tags are enum values living in Notion select columns: under auto the
# content can be in any language, but the database schema is fixed for the lifetime of
# a tenant, so under auto the labels are pinned to English (as they are under en).
LABEL_LANG = "en" if LANGUAGE == "auto" else LANGUAGE
# same for the instruction prompts: auto talks to the model in English (the model
# understands multilingual instructions either way); only an explicit uk switches the
# prompt text itself.
_PROMPT_LANG = "uk" if LANGUAGE == "uk" else "en"

_VALUES_BY_LANG = {
    "uk": ("🔥 Must-know", "👍 Корисно", "📎 Довідково"),
    "en": ("🔥 Must-know", "👍 Useful", "📎 Reference"),
}
# the second, independent scale: material that is mundane to the owner can still carry a
# strong angle, while a deep technical breakdown may not fit into content at all
_POTENTIALS_BY_LANG = {
    "uk": ("🔥 Strong angle", "👍 Adaptable", "📎 Weak"),
    "en": ("🔥 Strong angle", "👍 Adaptable", "📎 Weak"),
}
_FORMATS_BY_LANG = {
    "uk": ("Reel", "talking-head Reel", "screen recording", "carousel",
           "Telegram post", "Threads post", "story sequence", "technical breakdown",
           "case study", "не для контенту"),
    "en": ("Reel", "talking-head Reel", "screen recording", "carousel",
           "Telegram post", "Threads post", "story sequence", "technical breakdown",
           "case study", "not for content"),
}
_TAGS_BY_LANG = {
    "uk": ("контент-ідея", "продукт/курс", "делівері", "продажі", "лідген"),
    "en": ("content idea", "product/course", "delivery", "sales", "lead gen"),
}

VALUES: tuple = _VALUES_BY_LANG[LABEL_LANG]
POTENTIALS: tuple = _POTENTIALS_BY_LANG[LABEL_LANG]


def _from_env(var: str, default: tuple) -> tuple:
    # the override wins in any language; empty/whitespace entries are dropped, and if
    # nothing survives that, fall back quietly to the language default
    custom = tuple(t.strip() for t in (os.getenv(var) or "").split(",") if t.strip())
    return custom or default


TAGS: tuple = _from_env("KB_TAGS", _TAGS_BY_LANG[LABEL_LANG])
FORMATS: tuple = _from_env("KB_FORMATS", _FORMATS_BY_LANG[LABEL_LANG])

# The two scales are deliberately separate. There used to be one, phrased around "this
# week" and "the active deal" — and the whole library measured itself against a single
# current project: mundane material with a strong hook drowned in 📎, while a deep
# technical breakdown with no angle climbed to 🔥.
_CRITERIA_BY_LANG = {
    "uk": """Дві НЕЗАЛЕЖНІ оцінки. Не змішуй їх і не підганяй одну під одну.

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
технічний розбір, який треба сильно адаптувати, щоб став зрозумілим Reel).""",
    "en": """Two INDEPENDENT ratings. Do not mix them and do not tune one to match the other.

value — value for learning and work:
🔥 Must-know — concrete knowledge: an implementation detail, framework, architecture, prompt,
  a tool with a clear use case; or something that improves positioning, lead gen, sales,
  productization; or removes an active blocker or changes a current professional decision.
  The key is that it is verifiable and applicable in practice.
👍 Useful — thematically and strategically relevant, gives context, an example, or a new
  angle, may come in handy later; but without urgent applicability or 🔥-level depth.
📎 Reference — superficial, generic advice, a rehash of news, obvious to the owner,
  a tool with no use case. Kept only as a source.

content_potential — the potential to turn this into the owner's OWN content:
🔥 Strong angle — there is a hook or tension, a contrarian thesis, somewhere to plug in the
  owner's own case, the topic fits the positioning, the problem is clear to business owners,
  there is a practical takeaway, and the idea can be substantially transformed, not just copied.
👍 Adaptable — the topic is relevant to the audience, a Reel/post is conceivable, but it
  lacks a thesis, a case, or an example; works as part of a bigger piece.
📎 Weak — no clear hook, does not fit the positioning, too generic, nowhere to add the
  owner's own experience, repackaging would amount to copying.
Set 🔥 with calibration, not generosity: if the angle rests on a thesis or case the owner
  does not have yet, that is 👍, not 🔥. A scale where everything is 🔥 sorts nothing.

Combinations are normal and expected: 📎 value + 🔥 content_potential (a mundane list of
tools that nonetheless yields a strong counter-thesis) or 🔥 value + 👍 content_potential
(a deep technical breakdown that needs heavy adaptation to become a legible Reel).""",
}
_CRITERIA = _CRITERIA_BY_LANG[_PROMPT_LANG]

# project root, one level above the content_kb package: context.md is a
# per-owner config file that lives next to the repo, not inside the package
CONTEXT_FILE = Path(__file__).resolve().parents[1] / "context.md"

# when a tenant's context file is missing: better to rate honestly without a profile
# than to measure their content against somebody else's goals
_NO_PROFILE_BY_LANG = {
    "uk": """Власник цієї бази не лишив опису себе й своїх цілей.
Прив'язки до конкретного проєкту не вигадуй: у why_useful так і напиши,
що контексту власника немає, і став 📎 Довідково, крім випадків, коли матеріал
самоцінний сам по собі.""",
    "en": """The owner of this base has not left a description of themselves or their goals.
Do not invent ties to a specific project: say plainly in why_useful that there is
no owner context, and set 📎 Reference, except when the material is valuable on its
own regardless.""",
}
_NO_PROFILE = _NO_PROFILE_BY_LANG[_PROMPT_LANG]

# the field-language instruction inside the JSON prompt itself — three variants: en and
# auto talk to the model in English (as does the wording of the prompt), an explicit uk
# switches both the prompt and the requirement to Ukrainian.
_LANG_INSTRUCTION_BY_LANG = {
    "uk": "Мова всіх полів — українська, включно з content_angle і hook.",
    "en": "The language of every field is English, including content_angle and hook.",
    "auto": "The language of the fields matches the language of the content, "
            "including content_angle and hook.",
}
_LANG_INSTRUCTION = _LANG_INSTRUCTION_BY_LANG[LANGUAGE]


def profile(path=None) -> str:
    """Read on every analysis: an edit to the context file takes effect at once, no restart.

    No profile means we rate without one. A hardcoded copy of somebody's profile would be
    worse than none at all: the content would be measured against a stranger's deals, and
    everything meaningful would quietly become 📎 Reference.
    """
    path = Path(path) if path else CONTEXT_FILE
    try:
        text = path.read_text().strip()
    except OSError:
        text = ""
    if not text:
        logger.warning("no profile at %s — rating without the owner's context", path)
        return _NO_PROFILE
    return text


def analyze(content: str, link: str, profile_path=None) -> dict:
    if _PROMPT_LANG == "en":
        prompt = (
            "You are populating a content and learning library — this is NOT just a knowledge "
            "base and NOT a list of \"what's useful for the current deal\". The owner has two "
            "independent goals: (1) learn — understand a method, tool, framework, implementation "
            "detail, or business insight; (2) repackage — turn it into their own Reel/post with "
            "their own experience and positioning.\n\n"
            "Here is the owner:\n\n"
            f"{profile(profile_path)}\n\n"
            + _CRITERIA +
            "\n\nRepackaging rules (strict):\n"
            "- Extract the idea, do not copy the author's wording.\n"
            "- Do not invent experience, cases, results, or numbers on the owner's behalf. If the "
            "angle lacks the owner's own proof — say so plainly in own_proof.\n"
            "- If the material contains a specific proprietary method or a unique thesis of the "
            "author's — note in own_proof that attribution is required.\n\n"
            "Reply — ONLY JSON with no surrounding text, in this format:\n"
            '{"title": "the gist as a title, up to 80 characters", '
            '"tldr": "the whole point in one sentence", '
            '"summary": "3-5 sentences", '
            '"source_idea": "the original\'s main idea, no filler", '
            '"key_ideas": ["an insight or quote", ...], '
            '"practical": ["what to apply / a tool or service mentioned", ...], '
            '"learning_takeaway": "specific: what to check, apply, add to your own systems, or '
            'investigate further. Not \\"this is useful for AI direction growth\\", a verifiable '
            'action", '
            f'"tags": a subset of {list(TAGS)} — only from this list, do not invent, '
            f'"value": one of {list(VALUES)} — value for learning and work, '
            f'"content_potential": one of {list(POTENTIALS)} — potential for the owner\'s OWN '
            "content, rate it independently of value, "
            '"why_useful": "why this is useful for learning/application", '
            '"content_angle": "the owner\'s authorial angle: what they would say in their own '
            'words, which thesis they would challenge, which business mistake they would point '
            'out. Not a recap. Empty string if there is no angle", '
            '"hook": "the first line of a Reel/post — one sentence, in natural language, no '
            'AI-slop", '
            '"adaptation": ["steps for repackaging: take the problem, replace the original '
            'example with your own, add a counter-thesis, show the workflow, close with a '
            'takeaway"], '
            '"own_proof": "which own case/screenshot/system logic to add; or honestly — what '
            'proof the owner is missing", '
            f'"recommended_format": one of {list(FORMATS)}'
            "}\n"
            f"{_LANG_INSTRUCTION}\n"
            f"Link: {link}\n"
            f"Content:\n{content}"
        )
    else:
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
            f'"tags": підмножина {list(TAGS)} — тільки з цього списку, нічого не вигадуй, '
            f'"value": одне з {list(VALUES)} — цінність для навчання й роботи, '
            f'"content_potential": одне з {list(POTENTIALS)} — потенціал для ВЛАСНОГО контенту, '
            "оцінюй незалежно від value, "
            '"why_useful": "чому це корисно для навчання/застосування", '
            '"content_angle": "авторський кут власника: що він скаже від себе, яку тезу оскаржить, '
            'яку помилку бізнесу покаже. Не переказ. Порожній рядок, якщо кута нема", '
            '"hook": "перший рядок Reels/поста — одне речення, живою мовою, без AI-slop", '
            '"adaptation": ["кроки, як перепакувати: взяти проблему, замінити чужий приклад своїм, '
            'додати контртезу, показати workflow, завершити висновком"], '
            '"own_proof": "який власний кейс/скрін/логіку системи додати; або чесно — якого доказу '
            'у власника бракує", '
            f'"recommended_format": одне з {list(FORMATS)}'
            "}\n"
            f"{_LANG_INSTRUCTION}\n"
            f"Посилання: {link}\n"
            f"Контент:\n{content}"
        )
    return _normalize(_extract_json(_run_codex(prompt)))


def _normalize(data: dict) -> dict:
    return {
        "title": str(data.get("title") or "")[:200] or "Untitled",
        "tldr": str(data.get("tldr") or ""),
        "summary": str(data.get("summary") or ""),
        "source_idea": str(data.get("source_idea") or ""),
        "key_ideas": [str(x) for x in data.get("key_ideas") or []],
        "practical": [str(x) for x in data.get("practical") or []],
        "learning_takeaway": str(data.get("learning_takeaway") or ""),
        # tags only from the fixed list, otherwise the multi-select is litter within a month
        "tags": [str(x) for x in (data.get("tags") or []) if str(x) in TAGS],
        "value": data["value"] if data.get("value") in VALUES else VALUES[-1],
        "content_potential": (data["content_potential"]
                              if data.get("content_potential") in POTENTIALS else POTENTIALS[-1]),
        "why_useful": str(data.get("why_useful") or ""),
        # angle stays the key: same field, now with a column of its own in Notion
        "angle": str(data.get("content_angle") or data.get("angle") or ""),
        "hook": str(data.get("hook") or ""),
        "adaptation": [str(x) for x in data.get("adaptation") or []],
        "own_proof": str(data.get("own_proof") or ""),
        "recommended_format": (data["recommended_format"]
                               if data.get("recommended_format") in FORMATS else ""),
    }


_READ_IMAGE_PROMPT_BY_LANG = {
    "uk": (
        "На зображенні — скріншот поста або слайда каруселі із соцмережі. "
        "Випиши весь видимий текст дослівно, у правильному порядку, мовою оригіналу: "
        "заголовок, тіло, підписи на картинці, автора й нік, якщо видно. "
        "Якщо крім тексту є щось змістовне (схема, графік, скрін інтерфейсу) — опиши одним "
        "абзацом. Виведи тільки сам зміст, без пояснень і коментарів від себе."
    ),
    "en": (
        "The image is a screenshot of a social-media post or carousel slide. "
        "Transcribe all visible text verbatim, in the correct order, in the original "
        "language: the headline, body, captions on the image, author and handle if visible. "
        "If there is something substantive besides text (a diagram, a chart, a UI screenshot) "
        "— describe it in one paragraph. Output only the content itself, with no explanations "
        "or comments of your own."
    ),
}
_READ_IMAGE_CAPTION_BY_LANG = {
    "uk": "\n\nПідпис, який користувач надіслав разом із фото:\n{caption}",
    "en": "\n\nThe caption the user sent along with the photo:\n{caption}",
}


def read_image(paths: list, caption: str = "") -> str:
    """A post screenshot → text. From here it takes the same path as a voice transcript."""
    prompt = _READ_IMAGE_PROMPT_BY_LANG[_PROMPT_LANG]
    if caption:
        prompt += _READ_IMAGE_CAPTION_BY_LANG[_PROMPT_LANG].format(caption=caption)
    return _run_codex(prompt, images=paths)


_DIGEST_PROMPT_BY_LANG = {
    "uk": (
        "Нижче — пачка повідомлень (текстових і транскрибованих голосових), надісланих "
        "підряд одним користувачем. Склади один зв'язний зведений документ тією ж мовою: "
        "об'єднай суть, прибери повтори й шум, збережи всі важливі факти та деталі. "
        "Не додавай нічого від себе поза змістом повідомлень. Виведи тільки готовий документ, "
        "без пояснень і без обгортки в лапки чи markdown-код.\n\n"
    ),
    "en": (
        "Below is a batch of messages (text and transcribed voice notes) sent in a row by "
        "one user. Compile one coherent digest document in the same language: merge the "
        "substance, strip repetition and noise, keep all important facts and details. "
        "Do not add anything of your own beyond the content of the messages. Output only "
        "the finished document, with no explanations and no wrapping in quotes or a "
        "markdown code block.\n\n"
    ),
}


def compile_digest(items: list) -> str:
    joined = "\n\n---\n\n".join(items)
    prompt = _DIGEST_PROMPT_BY_LANG[_PROMPT_LANG] + joined
    return _run_codex(prompt)


def _extract_json(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Codex did not return JSON: {raw[:200]}")
    return json.loads(raw[start : end + 1])
