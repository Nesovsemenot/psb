#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bankcerts.py — сканер TLS-сертификатов сайтов российских банков.

Что делает:
  scan    — подключается к каждому сайту, снимает сертификат, определяет УЦ,
            сохраняет снимок в snapshots/YYYY-MM-DD.json
  report  — сравнивает два снимка и генерирует отчёт в стиле Telegram-поста
  show    — печатает содержимое снимка таблицей
  list    — печатает список целей

Зависимости: только стандартная библиотека.
Если установлен пакет `cryptography` — разбор сертификата точнее (SAN, алгоритм ключа).
Иначе используется системный openssl (есть в macOS/Linux из коробки).

Примеры:
  python3 bankcerts.py scan
  python3 bankcerts.py scan --scope all --workers 24
  python3 bankcerts.py report snapshots/2026-08-07.json snapshots/2026-08-12.json
  python3 bankcerts.py report          # автоматически два последних снимка
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(BASE_DIR, "snapshots")
BANKS_FILE = os.path.join(BASE_DIR, "banks.json")

# --------------------------------------------------------------------------
# Классификация удостоверяющих центров
# --------------------------------------------------------------------------
# Порядок важен: первое совпадение выигрывает. Проверяется строка "O + CN" issuer'а.
CA_RULES = [
    ("НУЦ Минцифры", [
        r"russian trusted",
        r"ministry of digital development",
        r"минцифры",
        r"нуц",
        r"russian certification",
    ]),
    ("TrustAsia", [r"trustasia", r"trust asia"]),
    ("Let's Encrypt", [r"let's encrypt", r"lets encrypt", r"\bisrg\b"]),
    ("GlobalSign", [r"globalsign"]),
    ("HARICA", [r"harica", r"hellenic academic"]),
    ("SSL.com", [r"ssl\.com", r"ssl corp"]),
    ("Sectigo", [r"sectigo", r"comodo", r"usertrust"]),
    ("DigiCert", [r"digicert", r"rapidssl", r"thawte", r"geotrust", r"cybertrust"]),
    ("Entrust", [r"entrust", r"affirmtrust"]),
    ("Certum", [r"certum", r"asseco", r"unizeto"]),
    ("GoDaddy", [r"go daddy", r"godaddy", r"starfield"]),
    ("Google Trust Services", [r"google trust", r"\bgts\b"]),
    ("Amazon", [r"amazon"]),
    ("ZeroSSL", [r"zerossl"]),
    ("Actalis", [r"actalis"]),
    ("Buypass", [r"buypass"]),
    ("Microsoft", [r"microsoft"]),
    ("Cloudflare", [r"cloudflare"]),
    ("Yandex", [r"yandex"]),
    ("QuoVadis", [r"quovadis"]),
    ("SwissSign", [r"swisssign"]),
    ("TWCA", [r"\btwca\b", r"taiwan"]),
    ("WoTrus", [r"wotrus", r"wosign"]),
    ("CFCA", [r"\bcfca\b", r"china financial"]),
]

DOMESTIC_CAS = {"НУЦ Минцифры", "Yandex"}


def classify_ca(issuer_o: str, issuer_cn: str) -> str:
    blob = f"{issuer_o or ''} {issuer_cn or ''}".lower()
    if not blob.strip():
        return "неизвестно"
    for name, patterns in CA_RULES:
        for p in patterns:
            if re.search(p, blob):
                return name
    # самоподписанный / внутренний УЦ
    return (issuer_o or issuer_cn or "неизвестно").strip()


# --------------------------------------------------------------------------
# Получение сертификата
# --------------------------------------------------------------------------
def to_ascii_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except Exception:
        return host


def fetch_der(host: str, port: int = 443, timeout: float = 8.0):
    """Возвращает (der_bytes, tls_version, cipher, trusted_by_system)."""
    ahost = to_ascii_host(host)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((ahost, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=ahost) as ssock:
            der = ssock.getpeercert(binary_form=True)
            version = ssock.version()
            cipher = ssock.cipher()[0] if ssock.cipher() else None

    # отдельная проверка: доверяет ли цепочке системное хранилище
    trusted = None
    try:
        vctx = ssl.create_default_context()
        with socket.create_connection((ahost, port), timeout=timeout) as sock:
            with vctx.wrap_socket(sock, server_hostname=ahost):
                trusted = True
    except ssl.SSLError:
        trusted = False
    except Exception:
        trusted = None

    return der, version, cipher, trusted


# --------------------------------------------------------------------------
# Разбор сертификата: cryptography -> openssl
# --------------------------------------------------------------------------
try:
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.x509.oid import NameOID as _NameOID

    HAVE_CRYPTOGRAPHY = True
except Exception:
    HAVE_CRYPTOGRAPHY = False


def _name_get(name, oid):
    try:
        vals = name.get_attributes_for_oid(oid)
        return vals[0].value if vals else ""
    except Exception:
        return ""


def parse_cert_cryptography(der: bytes) -> dict:
    c = _x509.load_der_x509_certificate(der)
    try:
        nb = c.not_valid_before_utc
        na = c.not_valid_after_utc
    except AttributeError:  # старые версии
        nb = c.not_valid_before.replace(tzinfo=dt.timezone.utc)
        na = c.not_valid_after.replace(tzinfo=dt.timezone.utc)

    sans = []
    try:
        ext = c.extensions.get_extension_for_class(_x509.SubjectAlternativeName)
        sans = ext.value.get_values_for_type(_x509.DNSName)
    except Exception:
        pass

    pk = c.public_key()
    key_type = type(pk).__name__.replace("_", "")
    key_bits = getattr(pk, "key_size", None)
    if key_bits is None and hasattr(pk, "curve"):
        key_bits = pk.curve.key_size
        key_type = f"EC/{pk.curve.name}"
    elif "RSA" in key_type:
        key_type = "RSA"

    return {
        "issuer_o": _name_get(c.issuer, _NameOID.ORGANIZATION_NAME),
        "issuer_cn": _name_get(c.issuer, _NameOID.COMMON_NAME),
        "issuer_c": _name_get(c.issuer, _NameOID.COUNTRY_NAME),
        "subject_o": _name_get(c.subject, _NameOID.ORGANIZATION_NAME),
        "subject_cn": _name_get(c.subject, _NameOID.COMMON_NAME),
        "not_before": nb.strftime("%Y-%m-%d"),
        "not_after": na.strftime("%Y-%m-%d"),
        "serial": format(c.serial_number, "x"),
        "sig_alg": getattr(c.signature_algorithm_oid, "_name", "") or str(c.signature_algorithm_oid),
        "key": f"{key_type}-{key_bits}" if key_bits else key_type,
        "san_count": len(sans),
        "sans": sans[:8],
        "fingerprint_sha256": c.fingerprint(_hashes.SHA256()).hex(),
    }


_OPENSSL_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _openssl_date(s: str) -> str:
    # 'Aug 12 09:41:05 2026 GMT'
    m = re.match(r"([A-Za-z]{3})\s+(\d+)\s+\d+:\d+:\d+\s+(\d{4})", s.strip())
    if not m:
        return s.strip()
    mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{year:04d}-{_OPENSSL_MONTHS.get(mon, 1):02d}-{day:02d}"


def parse_cert_openssl(der: bytes) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".der", delete=False) as f:
        f.write(der)
        path = f.name
    try:
        def run(args):
            return subprocess.run(
                ["openssl", "x509", "-inform", "DER", "-in", path, "-noout"] + args,
                capture_output=True, text=True, timeout=15,
            ).stdout

        out = run(["-issuer", "-subject", "-dates", "-serial", "-fingerprint", "-sha256",
                   "-nameopt", "sep_multiline,utf8"])
        text = run(["-text"])

        fields = {"issuer": {}, "subject": {}}
        section = None
        res = {}
        for line in out.splitlines():
            low = line.strip()
            if low.startswith("issuer="):
                section = "issuer"
                continue
            if low.startswith("subject="):
                section = "subject"
                continue
            if low.startswith("notBefore="):
                section = None
                res["not_before"] = _openssl_date(low.split("=", 1)[1])
                continue
            if low.startswith("notAfter="):
                res["not_after"] = _openssl_date(low.split("=", 1)[1])
                continue
            if low.startswith("serial="):
                res["serial"] = low.split("=", 1)[1].lower()
                continue
            if "Fingerprint=" in low:
                res["fingerprint_sha256"] = low.split("=", 1)[1].replace(":", "").lower()
                continue
            if section and "=" in line:
                k, v = line.split("=", 1)
                fields[section][k.strip()] = v.strip()

        sig = re.search(r"Signature Algorithm:\s*(\S+)", text)
        key_bits = re.search(r"Public-Key:\s*\((\d+) bit\)", text)
        key_alg = re.search(r"Public Key Algorithm:\s*(\S+)", text)
        sans = re.findall(r"DNS:([^,\s]+)", text)

        return {
            "issuer_o": fields["issuer"].get("O", ""),
            "issuer_cn": fields["issuer"].get("CN", ""),
            "issuer_c": fields["issuer"].get("C", ""),
            "subject_o": fields["subject"].get("O", ""),
            "subject_cn": fields["subject"].get("CN", ""),
            "not_before": res.get("not_before", ""),
            "not_after": res.get("not_after", ""),
            "serial": res.get("serial", ""),
            "sig_alg": sig.group(1) if sig else "",
            "key": (f"{key_alg.group(1)}-{key_bits.group(1)}" if key_alg and key_bits
                    else (key_alg.group(1) if key_alg else "")),
            "san_count": len(sans),
            "sans": sans[:8],
            "fingerprint_sha256": res.get("fingerprint_sha256", ""),
        }
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def parse_cert(der: bytes) -> dict:
    if HAVE_CRYPTOGRAPHY:
        return parse_cert_cryptography(der)
    return parse_cert_openssl(der)


# --------------------------------------------------------------------------
# Скан
# --------------------------------------------------------------------------
def scan_target(target: dict, timeout: float) -> dict:
    domains = [target["domain"]] + list(target.get("alt") or [])
    errors = []
    for dom in domains:
        try:
            der, version, cipher, trusted = fetch_der(dom, timeout=timeout)
            if not der:
                raise RuntimeError("сертификат не получен")
            info = parse_cert(der)
            ca = classify_ca(info["issuer_o"], info["issuer_cn"])
            days_left = None
            if info["not_after"]:
                try:
                    na = dt.datetime.strptime(info["not_after"], "%Y-%m-%d").date()
                    days_left = (na - dt.date.today()).days
                except ValueError:
                    pass
            return {
                "name": target["name"],
                "rank": target.get("rank"),
                "group": target.get("group", "bank"),
                "domain": dom,
                "ca": ca,
                "domestic": ca in DOMESTIC_CAS,
                "trusted_by_system_store": trusted,
                "tls": version,
                "cipher": cipher,
                "days_left": days_left,
                "error": None,
                **info,
            }
        except Exception as e:
            errors.append(f"{dom}: {type(e).__name__}: {e}")
    return {
        "name": target["name"],
        "rank": target.get("rank"),
        "group": target.get("group", "bank"),
        "domain": target["domain"],
        "ca": None,
        "error": " | ".join(errors),
    }


def load_targets(scope: str) -> list:
    with open(BANKS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    targets = data["targets"]
    if scope == "top25":
        targets = [t for t in targets if t.get("rank")]
        targets.sort(key=lambda t: t["rank"])
    elif scope == "banks":
        targets = [t for t in targets if t.get("group") == "bank"]
    return targets


def cmd_scan(args):
    targets = load_targets(args.scope)
    print(f"Сканирую {len(targets)} целей (scope={args.scope}, "
          f"парсер={'cryptography' if HAVE_CRYPTOGRAPHY else 'openssl'})…\n", file=sys.stderr)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(scan_target, t, args.timeout): t for t in targets}
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            results.append(r)
            mark = "✗" if r.get("error") else ("★" if r.get("domestic") else "·")
            print(f"  {mark} {r['name']:<28} {r.get('ca') or 'ОШИБКА'}", file=sys.stderr)

    order = {t["name"]: i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r["name"], 999))

    os.makedirs(SNAP_DIR, exist_ok=True)
    date = args.date or dt.date.today().isoformat()
    path = args.out or os.path.join(SNAP_DIR, f"{date}.json")
    snapshot = {
        "date": date,
        "scanned_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": args.scope,
        "parser": "cryptography" if HAVE_CRYPTOGRAPHY else "openssl",
        "total": len(results),
        "ok": sum(1 for r in results if not r.get("error")),
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"\nСнимок сохранён: {path}", file=sys.stderr)
    print_summary(snapshot)
    return path


def print_summary(snap: dict):
    ok = [r for r in snap["results"] if not r.get("error")]
    errs = [r for r in snap["results"] if r.get("error")]
    by_ca = {}
    for r in ok:
        by_ca.setdefault(r["ca"], []).append(r["name"])

    print(f"\n=== Снимок {snap['date']}: {len(ok)}/{snap['total']} успешно ===")
    for ca, names in sorted(by_ca.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"{ca:<24} {len(names):>3}  {', '.join(names)}")
    soon = sorted([r for r in ok if (r.get("days_left") or 999) < 30],
                  key=lambda r: r["days_left"])
    if soon:
        print("\nИстекают в ближайшие 30 дней:")
        for r in soon:
            print(f"  {r['name']} ({r['domain']}) — {r['days_left']} дн., до {r['not_after']}")
    if errs:
        print(f"\nНе удалось проверить ({len(errs)}):")
        for r in errs:
            print(f"  {r['name']}: {r['error'][:120]}")


# --------------------------------------------------------------------------
# Отчёт
# --------------------------------------------------------------------------
def load_snapshot(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def latest_snapshots(n=2):
    if not os.path.isdir(SNAP_DIR):
        return []
    files = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".json"))
    return [os.path.join(SNAP_DIR, f) for f in files[-n:]]


def ru_date(iso: str) -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d.day} {months[d.month - 1]}"
    except ValueError:
        return iso


NUM_WORDS = {1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть",
             7: "семь", 8: "восемь", 9: "девять", 10: "десять"}


def num_word(n: int) -> str:
    return NUM_WORDS.get(n, str(n))


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def build_report(a: dict, b: dict) -> str:
    ca_a = {r["name"]: r["ca"] for r in a["results"] if not r.get("error") and r.get("ca")}
    ca_b = {r["name"]: r["ca"] for r in b["results"] if not r.get("error") and r.get("ca")}
    info_b = {r["name"]: r for r in b["results"]}

    common = [n for n in ca_b if n in ca_a]
    changes = [(n, ca_a[n], ca_b[n]) for n in common if ca_a[n] != ca_b[n]]

    to_nuc = [c for c in changes if c[2] == "НУЦ Минцифры"]
    other = [c for c in changes if c[2] != "НУЦ Минцифры"]
    from_trustasia = [c for c in changes if c[1] == "TrustAsia"]
    ta_to_nuc = [c for c in from_trustasia if c[2] == "НУЦ Минцифры"]

    nuc_a = sorted(n for n, ca in ca_a.items() if ca == "НУЦ Минцифры")
    nuc_b = sorted(n for n, ca in ca_b.items() if ca == "НУЦ Минцифры")

    da, db = ru_date(a["date"]), ru_date(b["date"])
    try:
        days = (dt.date.fromisoformat(b["date"]) - dt.date.fromisoformat(a["date"])).days
    except ValueError:
        days = 0
    period = f"{num_word(days)} {plural(days, 'день', 'дня', 'дней')}"

    def by_size(change):
        return (info_b.get(change[0], {}).get("rank") or 99, change[0])

    L = []
    L.append("📊 Какие сертификаты используют российские банки")
    L.append("")
    L.append(f"Сопоставил результаты проверок на {da} и {db} — "
             f"всего за {period} картина {'заметно изменилась' if changes else 'почти не изменилась'}.")
    L.append("")
    L.append(f"На {da} сертификаты НУЦ Минцифры использовали {len(nuc_a)} "
             f"{plural(len(nuc_a), 'организация', 'организации', 'организаций')}. "
             f"На {db} — {'уже ' if len(nuc_b) > len(nuc_a) else ''}{len(nuc_b)}.")
    L.append("")

    if to_nuc:
        L.append(f"За {period} на НУЦ перешли {len(to_nuc)} "
                 f"{plural(len(to_nuc), 'организация', 'организации', 'организаций')}:")
        L.append("")
        for n, o, w in sorted(to_nuc, key=by_size):
            L.append(f"• {n} — {o} → {w}")
        L.append("")

    if other:
        L.append("Движение идет не только в сторону НУЦ:")
        L.append("")
        for n, o, w in sorted(other, key=by_size):
            L.append(f"• {n} — {o} → {w}")
        L.append("")

    if changes:
        L.append(f"Получается, всего за {period} УЦ сменили {len(changes)} "
                 f"{plural(len(changes), 'организация', 'организации', 'организаций')}. "
                 f"Из них {len(to_nuc)} перешли на НУЦ Минцифры.")
        L.append("")

    if from_trustasia:
        L.append(f"Отдельно про TrustAsia: с него ушли {len(from_trustasia)} "
                 f"{plural(len(from_trustasia), 'организация', 'организации', 'организаций')}. "
                 f"{len(ta_to_nuc)} — на НУЦ, "
                 f"еще {len(from_trustasia) - len(ta_to_nuc)} выбрали другие УЦ.")
        L.append("")

    foreign = {}
    for n, ca in ca_b.items():
        if ca not in DOMESTIC_CAS:
            foreign.setdefault(ca, []).append(n)
    if foreign:
        parts = []
        for ca, names in sorted(foreign.items(), key=lambda kv: -len(kv[1])):
            ranked = sorted(names, key=lambda n: (info_b.get(n, {}).get("rank") or 99, n))
            parts.append(f"{', '.join(ranked[:6])}{' и др.' if len(ranked) > 6 else ''} — {ca}")
        L.append("При этом на зарубежных УЦ пока остаются: " + "; ".join(parts) + ".")
        L.append("")

    if nuc_a:
        pct = round((len(nuc_b) - len(nuc_a)) / len(nuc_a) * 100)
        if pct:
            L.append(f"📈 Если коротко: за {period} количество организаций на НУЦ "
                     f"в выборке {'выросло' if pct > 0 else 'сократилось'} "
                     f"с {len(nuc_a)} до {len(nuc_b)} — на {abs(pct)}%.")
    else:
        L.append(f"📈 Если коротко: за {period} количество организаций на НУЦ "
                 f"в выборке выросло с 0 до {len(nuc_b)}.")

    # техническая часть — не для поста, но полезна автору
    L.append("")
    L.append("---")
    L.append("")
    L.append("### Техническая сводка")
    L.append("")
    tot_b = {}
    for ca in ca_b.values():
        tot_b[ca] = tot_b.get(ca, 0) + 1
    tot_a = {}
    for ca in ca_a.values():
        tot_a[ca] = tot_a.get(ca, 0) + 1
    L.append(f"| УЦ | {a['date']} | {b['date']} | Δ |")
    L.append("|---|---:|---:|---:|")
    for ca in sorted(set(tot_a) | set(tot_b), key=lambda c: (-tot_b.get(c, 0), c)):
        x, y = tot_a.get(ca, 0), tot_b.get(ca, 0)
        L.append(f"| {ca} | {x} | {y} | {(f'{y - x:+d}' if y != x else '—')} |")
    L.append("")

    soon = sorted([r for r in b["results"]
                   if not r.get("error") and (r.get("days_left") is not None)
                   and r["days_left"] < 30], key=lambda r: r["days_left"])
    if soon:
        L.append("**Истекают в ближайшие 30 дней:** " +
                 ", ".join(f"{r['name']} ({r['days_left']} дн.)" for r in soon))
        L.append("")

    untrusted = [r for r in b["results"]
                 if not r.get("error") and r.get("trusted_by_system_store") is False]
    if untrusted:
        L.append("**Не проходят проверку системным хранилищем доверия** "
                 "(нужен корневой сертификат НУЦ / отечественный браузер): " +
                 ", ".join(r["name"] for r in untrusted))
        L.append("")

    appeared = [n for n in ca_b if n not in ca_a]
    gone = [n for n in ca_a if n not in ca_b]
    if appeared:
        L.append("**Появились в выборке:** " + ", ".join(sorted(appeared)))
    if gone:
        L.append("**Пропали / не ответили:** " + ", ".join(sorted(gone)))
    errs = [r for r in b["results"] if r.get("error")]
    if errs:
        L.append("")
        L.append(f"**Не удалось проверить ({len(errs)}):** " +
                 ", ".join(r["name"] for r in errs))

    return "\n".join(L) + "\n"


def cmd_report(args):
    paths = [args.old, args.new]
    if not all(paths):
        paths = latest_snapshots(2)
        if len(paths) < 2:
            sys.exit("Нужно минимум два снимка в snapshots/. Запустите scan дважды в разные дни.")
    a, b = load_snapshot(paths[0]), load_snapshot(paths[1])
    if a["date"] > b["date"]:
        a, b = b, a
    text = build_report(a, b)
    out = args.out or os.path.join(BASE_DIR, f"report_{a['date']}_{b['date']}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\n[отчёт сохранён: {out}]", file=sys.stderr)


def cmd_show(args):
    snap = load_snapshot(args.path or latest_snapshots(1)[0])
    print_summary(snap)


def cmd_list(args):
    for t in load_targets(args.scope):
        rank = f"#{t['rank']:<3}" if t.get("rank") else "    "
        print(f"{rank} {t['name']:<30} {t['domain']}")


def main():
    p = argparse.ArgumentParser(description="Сканер TLS-сертификатов российских банков")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="снять сертификаты и сохранить снимок")
    s.add_argument("--scope", choices=["top25", "banks", "all"], default="top25",
                   help="top25 (по умолчанию) | banks — все банки | all — банки + инфраструктура")
    s.add_argument("--workers", type=int, default=16)
    s.add_argument("--timeout", type=float, default=8.0)
    s.add_argument("--date", help="дата снимка (по умолчанию сегодня)")
    s.add_argument("--out", help="путь к файлу снимка")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("report", help="сравнить два снимка и сгенерировать отчёт")
    r.add_argument("old", nargs="?")
    r.add_argument("new", nargs="?")
    r.add_argument("--out")
    r.set_defaults(func=cmd_report)

    sh = sub.add_parser("show", help="показать снимок")
    sh.add_argument("path", nargs="?")
    sh.set_defaults(func=cmd_show)

    ls = sub.add_parser("list", help="показать список целей")
    ls.add_argument("--scope", choices=["top25", "banks", "all"], default="top25")
    ls.set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
