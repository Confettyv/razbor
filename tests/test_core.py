import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from razbor import app, checks, llm

GOOD = (
    "Здравствуйте! Справку об обучении можно заказать в личном кабинете в разделе «Справки», "
    "она будет готова за 3 рабочих дня. Если нужна бумажная, заберите её в учебном офисе. "
    "Учебный офис"
)


def test_clean_draft_passes() -> None:
    result = checks.draft("Нужна справка об обучении", GOOD, ["k1"])
    assert checks.ok(result), result


@pytest.mark.parametrize(
    ("answer", "failed"),
    [
        (GOOD + " Гарантируем, что всё решим.", "Нет лишних обещаний"),
        (GOOD + " Ваш номер [номер заявки].", "Нет заглушек"),
        (GOOD + " Деньги придут 15 ноября.", "Даты и суммы из источника"),
        (GOOD + " Штраф 500 ₽.", "Даты и суммы из источника"),
        ("Хорошо.", "Длина"),
    ],
)
def test_bad_draft_flagged(answer: str, failed: str) -> None:
    result = {name: passed for name, passed, _ in checks.draft("Нужна справка", answer, ["k1"])}
    assert result[failed] is False


def test_date_from_kb_is_allowed() -> None:
    answer = GOOD + " Заявления на перевод принимаются до 1 февраля."
    names = {n: p for n, p, _ in checks.draft("Хочу перевестись", answer, ["k1", "k10"])}
    assert names["Даты и суммы из источника"]


def test_day_rule_allows_month_date() -> None:
    answer = GOOD + " Стипендия должна была прийти до 25 октября."
    names = {n: p for n, p, _ in checks.draft("Нет стипендии", answer, ["k1", "k5"])}
    assert names["Даты и суммы из источника"]
    names = {n: p for n, p, _ in checks.draft("Нет стипендии", answer + " Ждите 3 ноября.", ["k5"])}
    assert not names["Даты и суммы из источника"]


def test_no_kb_reference_flagged() -> None:
    names = {n: p for n, p, _ in checks.draft("Нужна справка", GOOD, ["k999"])}
    assert names["Опирается на регламент"] is False


def test_route_fixes_unknown_values() -> None:
    fixed, notes = checks.route({"dept": "mars", "topic": "Космос", "urgency": "x", "kb": "k1"})
    assert fixed["dept"] == "study" and fixed["topic"] == "Другое"
    assert fixed["urgency"] == "normal" and fixed["kb"] == [] and len(notes) == 2
    assert checks.route({"kb": [5, "k6"]})[0]["kb"] == ["k5", "k6"]


def test_parse_json_in_text() -> None:
    assert llm.parse('Вот: {"dept": "it"} готово') == {"dept": "it"}
    with pytest.raises(ValueError):
        llm.parse("нет json")


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "t.sqlite3")
    fake = {
        "topic": "Справка", "dept": "study", "urgency": "normal", "risk": False,
        "reason": "нужна справка", "kb": ["k1"], "draft": GOOD,
    }
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(fake, ensure_ascii=False))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(url: str, body: dict[str, object] | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def test_flow_new_send_stats(server: str) -> None:
    created = call(server + "/api/requests", {"text": "Нужна справка об обучении для работы"})
    assert isinstance(created, dict) and created["checks_ok"] and created["kb"][0]["id"] == "k1"
    rid = created["id"]
    call(f"{server}/api/requests/{rid}/send", {"final": GOOD, "dept": "dean", "work_s": 40})
    st = call(server + "/api/stats")
    assert isinstance(st, dict)
    assert st["sent"] == 1 and st["kept"] == 1 and st["rerouted"] == 1 and st["work_s"] == 40


def test_bad_input_is_400(server: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server + "/api/requests", {"text": "коротко"})
    assert e.value.code == 400


def test_day_limit(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "DAY_LIMIT", 1)
    call(server + "/api/requests", {"text": "Нужна справка об обучении"})
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server + "/api/requests", {"text": "Нужна ещё одна справка"})
    assert e.value.code == 400
