"""Проверка ответа модели кодом.

Модель предлагает тему, подразделение и черновик; код решает, можно ли отдавать черновик
оператору как есть или в нём нужно что-то поправить. Каждая проверка возвращает
(название, прошла ли, пояснение), оператор видит весь список.
"""

import re

from razbor import kb

Check = tuple[str, bool, str]

PROMISES = re.compile(
    r"гарантиру|обязательно реш|непременно|100\s*%|точно (?:решим|получите|вернём|успеете)"
    r"|в течение (?:часа|получаса)|сегодня же|вернём (?:деньги|средства)|компенсиру"
    r"|исключение для вас|в порядке исключения",
    re.I,
)
PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|_{3,}|X{3,}|Х{3,}|TODO|<[^>]+>|\bФИО\b")
DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|\b\d{1,2} (?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", re.I)
DAY_RULE = re.compile(r"\b(\d{1,2}) числа\b", re.I)
MONEY = re.compile(r"\b\d[\d\s]*(?:₽|руб)", re.I)
NEXT_STEP = re.compile(
    r"нужно|необходимо|можно|можете|сможете|рекоменд|подай|подать|закаж|заказыва|напиш|обрат"
    r"|провер|приход|получ|передал|передан|направ|сообщим|оформ|прикреп|зайд|свяж|ожидайте",
    re.I,
)


def _numbers(pattern: re.Pattern[str], text: str) -> set[str]:
    return {re.sub(r"\s+", " ", m.group(0).strip().lower()) for m in pattern.finditer(text)}


def kb_text(ids: list[str]) -> str:
    return " ".join(kb.KB.get(i, "") for i in ids)


def draft(text: str, answer: str, kb_ids: list[str]) -> list[Check]:
    """Проверки черновика ответа на обращение `text`."""
    out: list[Check] = []
    size = len(answer.strip())
    out.append(("Длина", 120 <= size <= 1500, f"{size} знаков"))

    hits = sorted({m.group(0) for m in PLACEHOLDER.finditer(answer)})
    out.append(("Нет заглушек", not hits, ", ".join(hits) or "чисто"))

    hits = sorted({m.group(0) for m in PROMISES.finditer(answer)})
    out.append(("Нет лишних обещаний", not hits, ", ".join(hits) or "чисто"))

    known = [i for i in kb_ids if i in kb.KB]
    out.append(("Опирается на регламент", bool(known), ", ".join(known) or "нет ссылки на базу знаний"))

    source = text + " " + kb_text(known)
    # «до 25 числа» в регламенте разрешает «до 25 октября» в ответе
    days = {m.group(1) for m in DAY_RULE.finditer(source)}
    dates = {d for d in _numbers(DATE, answer) - _numbers(DATE, source) if d.split()[0] not in days}
    new = dates | (_numbers(MONEY, answer) - _numbers(MONEY, source))
    out.append(("Даты и суммы из источника", not new, ", ".join(sorted(new)) or "чисто"))

    out.append(("Есть следующий шаг", bool(NEXT_STEP.search(answer)), "что делать студенту"))
    return out


def ok(checks: list[Check]) -> bool:
    return all(passed for _, passed, _ in checks)


def route(raw: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    """Приводит разметку модели к допустимым значениям; возвращает исправленное и замечания."""
    notes: list[str] = []
    fixed = dict(raw)
    if fixed.get("dept") not in kb.DEPTS:
        notes.append(f"неизвестное подразделение {fixed.get('dept')!r}, отправлено в учебный офис")
        fixed["dept"] = "study"
    if fixed.get("topic") not in kb.TOPICS:
        notes.append(f"неизвестная тема {fixed.get('topic')!r}")
        fixed["topic"] = "Другое"
    if fixed.get("urgency") not in kb.URGENCY:
        fixed["urgency"] = "normal"
    fixed["risk"] = bool(fixed.get("risk"))
    kb_ids = fixed.get("kb")
    ids = [str(i).strip().lower() for i in kb_ids] if isinstance(kb_ids, list) else []
    fixed["kb"] = [i if i.startswith("k") else f"k{i}" for i in ids]
    return fixed, notes
