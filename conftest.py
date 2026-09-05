"""Pin the settings ai_engine reads at import, before pytest collects anything.

tests/test_bot.py imports content_kb.bot, which calls load_dotenv() at module level, and
pytest does that while collecting — so without this a deployment's .env would decide what
the suite sees. The server's .env sets KB_LANGUAGE=uk and its own KB_TAGS, and the suite
went red there while passing on a laptop that had neither.

load_dotenv() does not overwrite a variable that already exists, so assigning here is what
neutralises it. An empty KB_TAGS/KB_FORMATS is not "no tags" — _from_env() falls back to
the shipped default for the language, which is exactly the baseline the tests assert on.
"""
import os

os.environ["KB_LANGUAGE"] = "en"
os.environ["KB_TAGS"] = ""
os.environ["KB_FORMATS"] = ""
