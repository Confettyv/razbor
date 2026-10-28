"""Разбор обращения одной моделью в OpenAI-формате.

Адрес, ключ и модель берутся из окружения, поэтому российская или локальная модель
(YandexGPT, GigaChat через совместимый шлюз, vLLM, Ollama) подключается без правки кода.
"""

import json
import os
import re
import time
import urllib.request

from razbor import kb

SYSTEM = """Ты помогаешь сотруднику вуза разбирать письменные обращения студентов и сотрудников.
Верни только JSON без пояснений:
{"topic": тема, "dept": код подразделения, "urgency": "high"|"normal"|"low",
 "risk": true|false, "reason": причина обращения в 3-8 словах,
 "kb": [номера фактов базы знаний вида "k5", на которые опирается ответ], "draft": черновик ответа}

Темы: %(topics)s.

Подразделения (код: зона ответственности):
%(depts)s

База знаний вуза:
%(kb)s

Правила:
- urgency=high, если срок истекает в ближайшие дни, человек без доступа к учёбе или жилью,
  есть угроза здоровью или безопасности.
- risk=true, если человек грозит жалобой ректору, в министерство, прокуратуру, СМИ
  или пишет повторно без ответа.
- Черновик на «вы», 3-5 предложений, не длиннее 700 знаков: что произошло, почему,
  что сделать дальше.
  Опирайся только на базу знаний и текст обращения. Не придумывай даты, суммы, сроки и имена,
  не обещай результат, не используй заглушки в скобках. Если фактов не хватает, напиши, какое
  подразделение ответит и что ему передано. Подпись: «Учебный офис» или название подразделения."""


def system_prompt() -> str:
    return SYSTEM % {
        "topics": ", ".join(kb.TOPICS),
        "depts": "\n".join(f"{k}: {kb.DEPTS[k]} - {kb.DUTIES[k]}" for k in kb.DEPTS),
        "kb": "\n".join(f"{k}: {v}" for k, v in kb.KB.items()),
    }


def chat(messages: list[dict[str, str]], max_tokens: int = 1500) -> str:
    body = {
        "model": os.environ.get("AI_MODEL", "claude-haiku-5-5"),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    url = os.environ.get("AI_URL", "https://ai.paira.ru/v1").rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ.get('AI_KEY', '')}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as r:
        return str(json.load(r)["choices"][0]["message"]["content"])


def parse(raw: str) -> dict[str, object]:
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("в ответе модели нет JSON")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("ответ модели не объект")
    return data


def analyze(text: str) -> tuple[dict[str, object], int]:
    """Возвращает разметку обращения и время ответа модели в мс."""
    started = time.monotonic()
    messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": text}]
    try:
        data = parse(chat(messages))
    except ValueError:
        data = parse(chat(messages))
    return data, int((time.monotonic() - started) * 1000)
