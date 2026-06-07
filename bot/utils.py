from aiogram import types

MAX_MSG_LEN = 4096


def not_a_command(message: types.Message) -> bool:
    """True, если сообщение — обычный текст, а не команда (не начинается с '/').

    Используется в FSM-хендлерах ожидания ввода, чтобы команды (/digest, /trends…)
    не перехватывались как пользовательский текст, а доходили до своих обработчиков.
    """
    return bool(message.text) and not message.text.startswith("/")


async def send_long_message(message: types.Message, text: str, **kwargs) -> None:
    """Send text, splitting by lines if it exceeds Telegram's 4096-char limit."""
    if len(text) <= MAX_MSG_LEN:
        await message.answer(text, **kwargs)
        return

    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_MSG_LEN:
            parts.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        parts.append(current)

    for part in parts:
        await message.answer(part, **kwargs)
