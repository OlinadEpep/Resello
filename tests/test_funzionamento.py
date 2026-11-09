"""Test /funzionamento: 2 messaggi, sotto limite Telegram, senza nomi tech."""

import bot


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, parse_mode=None):
        self.sent.append((text, parse_mode))


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


async def test_funzionamento_sends_two_messages():
    upd = FakeUpdate()
    await bot.funzionamento(upd, None)
    assert len(upd.message.sent) == 2
    for text, mode in upd.message.sent:
        assert mode == "Markdown"
        assert len(text) < 4096
        assert len(text) > 200


async def test_funzionamento_covers_process():
    upd = FakeUpdate()
    await bot.funzionamento(upd, None)
    full = "\n".join(t for (t, _) in upd.message.sent).lower()
    for kw in ["mercato", "mediana", "soglia", "30%", "10 minuti", "spedizione",
               "protezione", "falsi positivi", "custodie", "manuali",
               "da verificare", "/lista", "/stop", "/cerca"]:
        assert kw in full, f"manca: {kw}"


async def test_funzionamento_no_tech_names():
    upd = FakeUpdate()
    await bot.funzionamento(upd, None)
    full = "\n".join(t for (t, _) in upd.message.sent).lower()
    for banned in ["jev", "typesafe", "system one", "system-one", "openai", "llm", "chatgpt"]:
        assert banned not in full, f"nome tech da non citare: {banned}"


async def test_help_mentions_funzionamento():
    upd = FakeUpdate()
    await bot.help_command(upd, None)
    assert "/funzionamento" in upd.message.sent[0][0]
