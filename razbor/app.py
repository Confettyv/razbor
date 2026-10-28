"""HTTP-сервис «Разбор»: очередь обращений, экран оператора, панель руководителя.

Только стандартная библиотека и SQLite. Вход закрывает nginx (basic auth).
Запуск: python -m razbor.app [--port 8097] [--seed data/seed.json] [--analyze-all]
"""

import argparse
import difflib
import json
import os
import re
import sqlite3
import statistics
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from razbor import checks, kb, llm

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("RAZBOR_DB", ROOT / "data" / "razbor.sqlite3"))
DAY_LIMIT = int(os.environ.get("RAZBOR_DAY_LIMIT", "300"))
MSK = timezone(timedelta(hours=3))

SCHEMA = """
create table if not exists requests (
  id integer primary key,
  created text not null,
  channel text not null,
  author text not null,
  text text not null,
  status text not null default 'new',
  topic text, dept text, model_dept text, urgency text, risk integer,
  reason text, kb text, draft text, checks text, notes text,
  model_ms integer, analyzed text,
  final text, sent text, edit_ratio real, work_s integer
);
create table if not exists calls (day text primary key, n integer not null);
"""

model_lock = threading.Lock()


def now() -> str:
    return datetime.now(MSK).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def seed(db: sqlite3.Connection, path: Path) -> int:
    """Загружает синтетические обращения; созданы равномерно за последние 10 дней."""
    if db.execute("select count(*) from requests").fetchone()[0]:
        return 0
    items = json.loads(path.read_text())
    start = datetime.now(MSK) - timedelta(days=10)
    step = timedelta(days=10) / max(len(items), 1)
    for i, item in enumerate(items):
        created = (start + step * i).replace(microsecond=0).isoformat()
        db.execute(
            "insert into requests(created, channel, author, text) values (?,?,?,?)",
            (created, item["channel"], item["author"], item["text"]),
        )
    db.commit()
    return len(items)


def spend_call(db: sqlite3.Connection) -> bool:
    day = datetime.now(MSK).date().isoformat()
    row = db.execute("select n from calls where day=?", (day,)).fetchone()
    if row and row[0] >= DAY_LIMIT:
        return False
    db.execute(
        "insert into calls(day, n) values (?,1) on conflict(day) do update set n=n+1", (day,)
    )
    db.commit()
    return True


def analyze(db: sqlite3.Connection, rid: int) -> None:
    row = db.execute("select text from requests where id=?", (rid,)).fetchone()
    if row is None:
        raise KeyError(rid)
    if not spend_call(db):
        raise RuntimeError("дневной лимит вызовов модели исчерпан")
    with model_lock:
        raw, ms = llm.analyze(row["text"])
    data, notes = checks.route(raw)
    draft = str(data.get("draft") or "")
    result = checks.draft(row["text"], draft, data["kb"])  # type: ignore[arg-type]
    db.execute(
        "update requests set status='ready', topic=?, dept=?, model_dept=?, urgency=?, risk=?,"
        " reason=?, kb=?, draft=?, checks=?, notes=?, model_ms=?, analyzed=? where id=?",
        (
            data["topic"], data["dept"], data["dept"], data["urgency"], int(bool(data["risk"])),
            str(data.get("reason") or ""), json.dumps(data["kb"]), draft,
            json.dumps(result, ensure_ascii=False), json.dumps(notes, ensure_ascii=False),
            ms, now(), rid,
        ),
    )
    db.commit()


def send(db: sqlite3.Connection, rid: int, final: str, dept: str, work_s: int) -> None:
    row = db.execute("select draft from requests where id=?", (rid,)).fetchone()
    if row is None:
        raise KeyError(rid)
    if dept not in kb.DEPTS:
        raise ValueError("неизвестное подразделение")
    if not final.strip():
        raise ValueError("пустой ответ")
    ratio = difflib.SequenceMatcher(None, row["draft"] or "", final).ratio()
    db.execute(
        "update requests set status='sent', final=?, dept=?, sent=?, edit_ratio=?, work_s=?"
        " where id=?",
        (final, dept, now(), round(ratio, 3), max(0, min(work_s, 3600)), rid),
    )
    db.commit()


def item(row: sqlite3.Row, full: bool = False) -> dict[str, Any]:
    out = {k: row[k] for k in row.keys() if k not in ("checks", "notes", "kb")}
    out["checks"] = json.loads(row["checks"] or "[]")
    out["checks_ok"] = checks.ok(out["checks"]) if row["checks"] else None
    if full:
        ids = json.loads(row["kb"] or "[]")
        out["kb"] = [{"id": i, "text": kb.KB[i]} for i in ids if i in kb.KB]
        out["notes"] = json.loads(row["notes"] or "[]")
    else:
        out.pop("text")
        out["snippet"] = row["text"][:140]
        out.pop("draft")
        out.pop("final")
    return out


def stats(db: sqlite3.Connection) -> dict[str, Any]:
    rows = db.execute("select * from requests").fetchall()
    ready = [r for r in rows if r["status"] != "new"]
    sent = [r for r in rows if r["status"] == "sent"]
    open_ = [r for r in rows if r["status"] == "ready"]

    def count(rs: list[sqlite3.Row], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rs:
            out[r[key]] = out.get(r[key], 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    reasons: dict[str, list[str]] = {}
    for r in ready:
        reasons.setdefault(r["topic"], []).append(r["reason"])

    def hours(r: sqlite3.Row) -> float:
        a = datetime.fromisoformat(r["created"])
        b = datetime.fromisoformat(r["sent"])
        return (b - a).total_seconds() / 3600

    days: dict[str, int] = {}
    for r in rows:
        days[r["created"][:10]] = days.get(r["created"][:10], 0) + 1
    return {
        "total": len(rows),
        "analyzed": len(ready),
        "open": len(open_),
        "sent": len(sent),
        "high_open": sum(1 for r in open_ if r["urgency"] == "high"),
        "risk_open": sum(1 for r in open_ if r["risk"]),
        "checks_ok": sum(1 for r in ready if checks.ok(json.loads(r["checks"] or "[]"))),
        "by_topic": count(ready, "topic"),
        "by_dept_open": {kb.DEPTS[k]: v for k, v in count(open_, "dept").items()},
        "reasons": {t: rs[:3] for t, rs in reasons.items()},
        "days": dict(sorted(days.items())),
        "kept": sum(1 for r in sent if (r["edit_ratio"] or 0) >= 0.85),
        "rerouted": sum(1 for r in sent if r["dept"] != r["model_dept"]),
        "edit_ratio": round(statistics.mean(r["edit_ratio"] for r in sent), 2) if sent else None,
        "work_s": int(statistics.median(r["work_s"] for r in sent)) if sent else None,
        "answer_h": round(statistics.median(hours(r) for r in sent), 1) if sent else None,
        "model_ms": int(statistics.median(r["model_ms"] for r in ready)) if ready else None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "razbor"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("RAZBOR_LOG"):
            super().log_message(fmt, *args)

    def reply(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length") or 0)
        if size > 20000:
            raise ValueError("слишком длинный запрос")
        data = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("ожидается объект")
        return data

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.reply(200, (STATIC / "index.html").read_bytes(), "text/html")
        with closing(connect()) as db:
            if path == "/api/meta":
                return self.reply(200, {"depts": kb.DEPTS, "topics": kb.TOPICS, "urgency": kb.URGENCY})
            if path == "/api/requests":
                rows = db.execute(
                    "select * from requests order by status='sent', urgency!='high', risk=0,"
                    " created desc"
                ).fetchall()
                return self.reply(200, [item(r) for r in rows])
            if m := re.fullmatch(r"/api/requests/(\d+)", path):
                row = db.execute("select * from requests where id=?", (int(m[1]),)).fetchone()
                if row is None:
                    return self.reply(404, {"error": "нет такого обращения"})
                return self.reply(200, item(row, full=True))
            if path == "/api/stats":
                return self.reply(200, stats(db))
        self.reply(404, {"error": "не найдено"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            data = self.body()
            with closing(connect()) as db:
                if path == "/api/requests":
                    text = str(data.get("text") or "").strip()
                    if not 10 <= len(text) <= 4000:
                        raise ValueError("текст обращения от 10 до 4000 знаков")
                    cur = db.execute(
                        "insert into requests(created, channel, author, text) values (?,?,?,?)",
                        (now(), str(data.get("channel") or "Форма"),
                         str(data.get("author") or "Студент")[:60], text),
                    )
                    db.commit()
                    rid = int(cur.lastrowid or 0)
                    analyze(db, rid)
                    row = db.execute("select * from requests where id=?", (rid,)).fetchone()
                    return self.reply(200, item(row, full=True))
                if m := re.fullmatch(r"/api/requests/(\d+)/analyze", path):
                    analyze(db, int(m[1]))
                    row = db.execute("select * from requests where id=?", (int(m[1]),)).fetchone()
                    return self.reply(200, item(row, full=True))
                if m := re.fullmatch(r"/api/requests/(\d+)/send", path):
                    send(db, int(m[1]), str(data.get("final") or ""), str(data.get("dept")),
                         int(data.get("work_s") or 0))
                    return self.reply(200, {"ok": True})
            self.reply(404, {"error": "не найдено"})
        except KeyError:
            self.reply(404, {"error": "нет такого обращения"})
        except (ValueError, RuntimeError) as e:
            self.reply(400, {"error": str(e)})
        except OSError as e:
            self.reply(502, {"error": f"модель недоступна: {e}"})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=int(os.environ.get("RAZBOR_PORT", "8097")))
    p.add_argument("--seed", type=Path)
    p.add_argument("--analyze-all", action="store_true")
    a = p.parse_args()
    with closing(connect()) as db:
        if a.seed:
            print("seeded", seed(db, a.seed))
        if a.analyze_all:
            ids = [r[0] for r in db.execute("select id from requests where status='new'")]
            for rid in ids:
                for attempt in range(3):
                    try:
                        analyze(db, rid)
                        break
                    except (OSError, ValueError) as e:
                        print("retry", rid, e)
                        time.sleep(2 + attempt * 3)
            print("analyzed", len(ids))
    if a.seed or a.analyze_all:
        return
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"razbor on 127.0.0.1:{a.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
