#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Telegram-бот к сканеру сертификатов.

Команды:
  /scan [top25|banks|all]   запустить проверку сейчас, сохранить снимок
  /report [дата1] [дата2]   сравнить снимки и прислать отчёт (по умолчанию два последних)
  /post                     только текст поста, без технической сводки
  /last                     сводка по последнему снимку
  /snapshots                список снимков
  /targets [scope]          что сканируем
  /help

Запуск:
  export TG_BOT_TOKEN=...        (или создайте config.json, см. config.example.json)
  python3 bot.py

Зависимости: только стандартная библиотека.
"""

import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

import bankcerts as bc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3900  # запас к лимиту телеграма в 4096

KEYBOARD = {
    "keyboard": [["/scan", "/report"], ["/last", "/snapshots"], ["/post", "/git"]],
    "resize_keyboard": True,
}

HELP = """Сканер TLS-сертификатов сайтов российских банков.

/scan — проверить сейчас (по умолчанию топ-25)
/scan all — все банки + инфраструктура (60+ сайтов, ~1–2 мин)
/scan banks — только банки

/report — сравнить два последних снимка
/report 2026-07-31 2026-08-12 — сравнить конкретные даты
/post — то же, но только текст поста

/last — сводка по последнему снимку
/snapshots — какие снимки есть
/targets all — список целей
/git — закоммитить снимки и отчёты и отправить на GitHub"""


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    token = os.environ.get("TG_BOT_TOKEN") or cfg.get("token")
    if not token:
        sys.exit("Нет токена бота. Задайте TG_BOT_TOKEN или token в config.json "
                 "(шаблон — config.example.json).")
    allowed = cfg.get("allowed_user_ids") or []
    env_allowed = os.environ.get("TG_ALLOWED")
    if env_allowed:
        allowed = [int(x) for x in re.findall(r"\d+", env_allowed)]
    return {
        "token": token,
        "allowed_user_ids": [int(x) for x in allowed],
        "default_scope": cfg.get("default_scope", "top25"),
        "workers": int(cfg.get("workers", 16)),
        "timeout": float(cfg.get("timeout", 8.0)),
        "daily_scan_at": cfg.get("daily_scan_at"),      # "09:00" или null
        "daily_scan_chat_id": cfg.get("daily_scan_chat_id"),
        "git_autosave": bool(cfg.get("git_autosave", True)),
    }


CFG = {}


# --------------------------------------------------------------------------
# Telegram API
# --------------------------------------------------------------------------
_SSL_CTX = []


def tls_context():
    """Контекст с корнями из Keychain — иначе Python не видит сертификат
    локального прокси/VPN и падает на CERTIFICATE_VERIFY_FAILED."""
    if not _SSL_CTX:
        _SSL_CTX.append(bc.trusting_context())
    return _SSL_CTX[0]


def api(method: str, payload: dict, timeout=70):
    url = API.format(token=CFG["token"], method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=tls_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def split_text(text: str, limit=MAX_LEN):
    """Режет длинный текст по абзацам, затем по строкам."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for block in text.split("\n\n"):
        piece = (buf + "\n\n" + block) if buf else block
        if len(piece) <= limit:
            buf = piece
            continue
        if buf:
            chunks.append(buf)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(block[:cut])
            block = block[cut:].lstrip("\n")
        buf = block
    if buf:
        chunks.append(buf)
    return chunks


def send_message(chat_id, text, keyboard=True, silent=False):
    ids = []
    for part in split_text(text):
        payload = {"chat_id": chat_id, "text": part,
                   "disable_web_page_preview": True}
        if silent:
            payload["disable_notification"] = True
        if keyboard:
            payload["reply_markup"] = KEYBOARD
        try:
            r = api("sendMessage", payload)
            ids.append(r.get("result", {}).get("message_id"))
        except Exception as e:
            print(f"[send_message] {e}", file=sys.stderr)
    return ids[0] if ids else None


def edit_message(chat_id, message_id, text):
    if not message_id:
        return
    try:
        api("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                "text": text[:MAX_LEN]})
    except Exception:
        pass


def send_document(chat_id, path, caption=""):
    """multipart/form-data вручную, чтобы не тянуть requests."""
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1000]

    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
             f"filename=\"{os.path.basename(path)}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
    body += content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = API.format(token=CFG["token"], method="sendDocument")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=tls_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[send_document] {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------
SCAN_LOCK = threading.Lock()
REPO_DIR = os.path.dirname(BASE_DIR)


def git(*args, timeout=120):
    r = subprocess.run(["git", "-C", REPO_DIR] + list(args),
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def do_git_save(chat_id, message=None, quiet=False):
    """Коммит и push снимков и отчётов."""
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        if not quiet:
            send_message(chat_id, "Это не git-репозиторий, сохранять некуда.")
        return
    msg = message or f"снимок и отчёт за {dt.date.today().isoformat()}"
    git("add", "cert-scanner/snapshots", "cert-scanner/reports")
    code, out = git("commit", "-m", msg)
    if code != 0 and "nothing to commit" in out:
        if not quiet:
            send_message(chat_id, "Нечего сохранять — новых файлов нет.")
        return
    if code != 0:
        send_message(chat_id, "Не удалось закоммитить:\n" + out[-1000:])
        return
    code, out = git("push")
    if code == 0:
        send_message(chat_id, f"Сохранено в git и отправлено на GitHub: {msg}")
    else:
        send_message(chat_id, "Коммит сделан, но push не прошёл:\n" + out[-1000:])


def resolve_snapshot(arg: str):
    """'2026-08-12' | 'latest' | путь -> путь к файлу снимка."""
    if os.path.exists(arg):
        return arg
    cand = os.path.join(bc.SNAP_DIR, f"{arg}.json")
    if os.path.exists(cand):
        return cand
    return None


def do_scan(chat_id, scope):
    if not SCAN_LOCK.acquire(blocking=False):
        send_message(chat_id, "Проверка уже идёт, подождите её окончания.")
        return
    try:
        targets = bc.load_targets(scope)
        total = len(targets)
        msg_id = send_message(chat_id, f"Проверяю {total} сайтов (scope={scope})…",
                              keyboard=False)
        state = {"last": 0.0}

        def progress(done, tot, r):
            now = time.time()
            if now - state["last"] > 3 or done == tot:
                state["last"] = now
                edit_message(chat_id, msg_id, f"Проверяю {tot} сайтов (scope={scope})…\n"
                                              f"Готово: {done}/{tot}")

        started = time.time()
        snap, path = bc.run_scan(scope, CFG["workers"], CFG["timeout"], progress=progress)
        took = int(time.time() - started)
        edit_message(chat_id, msg_id, f"Проверка завершена за {took} с.")
        send_message(chat_id, bc.summary_text(snap, verbose=True))

        snaps = bc.latest_snapshots(2)
        if len(snaps) >= 2:
            send_message(chat_id, "Есть предыдущий снимок — жму /report за вас.",
                         silent=True)
            do_report(chat_id, snaps[0], snaps[1], post_only=False)
        else:
            send_message(chat_id, "Это первый снимок — сравнивать пока не с чем. "
                                  "Следующий скан даст отчёт о переходах.")
        if CFG.get("git_autosave"):
            do_git_save(chat_id, f"скан {snap['date']} (scope={scope})", quiet=True)
    except Exception:
        send_message(chat_id, "Ошибка при сканировании:\n" + traceback.format_exc()[-1500:])
    finally:
        SCAN_LOCK.release()


def do_report(chat_id, path_a=None, path_b=None, post_only=False):
    if not path_a or not path_b:
        snaps = bc.latest_snapshots(2)
        if len(snaps) < 2:
            send_message(chat_id, "Нужно минимум два снимка. Запустите /scan сегодня "
                                  "и ещё раз в другой день.")
            return
        path_a, path_b = snaps
    a, b = bc.load_snapshot(path_a), bc.load_snapshot(path_b)
    if a["date"] > b["date"]:
        a, b = b, a
    text = bc.build_report(a, b)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    out = os.path.join(REPORTS_DIR, f"report_{a['date']}_{b['date']}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)

    post = text.split("\n---\n")[0].strip()
    send_message(chat_id, post if post_only else text)
    if not post_only:
        send_document(chat_id, out, f"Отчёт {a['date']} → {b['date']}")


def do_last(chat_id):
    snaps = bc.latest_snapshots(1)
    if not snaps:
        send_message(chat_id, "Снимков ещё нет. Запустите /scan.")
        return
    snap = bc.load_snapshot(snaps[0])
    send_message(chat_id, bc.summary_text(snap, verbose=True))


def do_snapshots(chat_id):
    if not os.path.isdir(bc.SNAP_DIR):
        send_message(chat_id, "Снимков ещё нет.")
        return
    files = sorted(f for f in os.listdir(bc.SNAP_DIR) if f.endswith(".json"))
    if not files:
        send_message(chat_id, "Снимков ещё нет.")
        return
    lines = []
    for f in files:
        try:
            s = bc.load_snapshot(os.path.join(bc.SNAP_DIR, f))
            nuc = sum(1 for r in s["results"] if r.get("ca") == "НУЦ Минцифры")
            lines.append(f"{s['date']} — {s['ok']}/{s['total']} ок, "
                         f"scope={s.get('scope')}, НУЦ: {nuc}")
        except Exception:
            lines.append(f"{f} — не читается")
    send_message(chat_id, "Снимки:\n\n" + "\n".join(lines))


def do_targets(chat_id, scope):
    targets = bc.load_targets(scope)
    lines = [f"{('#' + str(t['rank'])) if t.get('rank') else '  '} {t['name']} — {t['domain']}"
             for t in targets]
    send_message(chat_id, f"Цели ({scope}, {len(targets)}):\n\n" + "\n".join(lines))


def handle_command(chat_id, text):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    if cmd in ("/start", "/help"):
        send_message(chat_id, HELP)
    elif cmd == "/scan":
        scope = args[0] if args and args[0] in ("top25", "banks", "all") else CFG["default_scope"]
        threading.Thread(target=do_scan, args=(chat_id, scope), daemon=True).start()
    elif cmd in ("/report", "/post"):
        pa = resolve_snapshot(args[0]) if len(args) > 0 else None
        pb = resolve_snapshot(args[1]) if len(args) > 1 else None
        if len(args) >= 1 and not pa:
            send_message(chat_id, f"Снимок {args[0]} не найден. /snapshots — что есть.")
            return
        if len(args) >= 2 and not pb:
            send_message(chat_id, f"Снимок {args[1]} не найден. /snapshots — что есть.")
            return
        do_report(chat_id, pa, pb, post_only=(cmd == "/post"))
    elif cmd == "/last":
        do_last(chat_id)
    elif cmd in ("/snapshots", "/list"):
        do_snapshots(chat_id)
    elif cmd in ("/git", "/save"):
        do_git_save(chat_id, " ".join(args) if args else None)
    elif cmd == "/targets":
        scope = args[0] if args and args[0] in ("top25", "banks", "all") else "top25"
        do_targets(chat_id, scope)
    else:
        send_message(chat_id, "Не знаю такой команды. /help")


# --------------------------------------------------------------------------
# Расписание (опционально)
# --------------------------------------------------------------------------
def scheduler():
    at = CFG.get("daily_scan_at")
    chat_id = CFG.get("daily_scan_chat_id")
    if not at or not chat_id:
        return
    hh, mm = (int(x) for x in at.split(":"))
    done_for = None
    while True:
        now = dt.datetime.now()
        if now.hour == hh and now.minute == mm and done_for != now.date():
            done_for = now.date()
            do_scan(chat_id, CFG["default_scope"])
        time.sleep(20)


# --------------------------------------------------------------------------
# Основной цикл
# --------------------------------------------------------------------------
def main():
    global CFG
    CFG = load_config()

    try:
        me = api("getMe", {}, timeout=20)["result"]
        print(f"Бот @{me['username']} запущен. Ctrl+C для остановки.")
    except Exception as e:
        sys.exit(f"Не удалось подключиться к Telegram: {e}")

    if CFG.get("daily_scan_at"):
        threading.Thread(target=scheduler, daemon=True).start()
        print(f"Ежедневный скан в {CFG['daily_scan_at']}.")

    offset = None
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message"]}
            if offset:
                payload["offset"] = offset
            resp = api("getUpdates", payload)
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = msg.get("text")
                chat_id = (msg.get("chat") or {}).get("id")
                user = msg.get("from") or {}
                if not text or not chat_id:
                    continue

                allowed = CFG["allowed_user_ids"]
                if allowed and user.get("id") not in allowed:
                    send_message(chat_id, "Доступ закрыт.", keyboard=False)
                    print(f"Отказ: {user.get('id')} ({user.get('username')})")
                    continue
                if not allowed:
                    send_message(chat_id,
                                 f"Бот пока никому не привязан. Ваш user_id: {user.get('id')}\n"
                                 f"Добавьте его в allowed_user_ids в config.json "
                                 f"и перезапустите бота.", keyboard=False)
                    continue

                print(f"[{dt.datetime.now():%H:%M:%S}] {user.get('username')}: {text}")
                try:
                    handle_command(chat_id, text)
                except Exception:
                    traceback.print_exc()
                    send_message(chat_id, "Ошибка:\n" + traceback.format_exc()[-1500:])
        except KeyboardInterrupt:
            print("\nОстановлен.")
            return
        except urllib.error.URLError as e:
            print(f"Сеть недоступна ({e}), повтор через 10 с…", file=sys.stderr)
            time.sleep(10)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
