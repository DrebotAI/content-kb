import asyncio

from delivery import MAX_MESSAGE_LEN, send_text_or_file


class FakeBot:
    def __init__(self):
        self.messages, self.documents = [], []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.messages.append((text, parse_mode))

    async def send_document(self, chat_id, document, filename):
        self.documents.append(filename)


def test_short_text_goes_as_monospace_with_escaping():
    bot = FakeBot()
    asyncio.run(send_text_or_file(bot, 1, "код: a < b & c", "t.txt"))
    text, parse_mode = bot.messages[0]
    assert text == "<pre>код: a &lt; b &amp; c</pre>"
    assert parse_mode == "HTML"
    assert not bot.documents


def test_long_text_goes_as_file():
    bot = FakeBot()
    asyncio.run(send_text_or_file(bot, 1, "т" * (MAX_MESSAGE_LEN + 1), "t.txt"))
    assert bot.documents == ["t.txt"] and not bot.messages


def test_empty_text_sends_nothing():
    bot = FakeBot()
    asyncio.run(send_text_or_file(bot, 1, "", "t.txt"))
    assert not bot.messages and not bot.documents


if __name__ == "__main__":
    test_short_text_goes_as_monospace_with_escaping()
    test_long_text_goes_as_file()
    test_empty_text_sends_nothing()
    print("ok")
