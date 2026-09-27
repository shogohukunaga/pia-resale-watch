#!/usr/bin/env python3
"""チケットぴあ リセール出品監視 → Gmail 通知。

出品一覧ページを定期的に取得し、「出品されたリセールチケットはありません」が消えたら
（または新しい出品リンクが増えたら）メールで知らせる。標準ライブラリのみで動作する。

  python resale_watch.py                 常駐ループ（60秒間隔）
  python resale_watch.py --once          1回だけチェック（GitHub Actions 用）
  python resale_watch.py --test-mail     テストメールを送信
  python resale_watch.py --check-file x  保存済み HTML を判定するだけ（メール送信なし）
"""
import argparse
import hashlib
import html as htmllib
import json
import logging
import os
import random
import re
import smtplib
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "resale_watch.log"
DEFAULT_STATE = BASE_DIR / "state.json"

JST = timezone(timedelta(hours=9))
LIST_URL = "https://cloak.pia.jp/resale/item/list?eventCd={}"
DEFAULT_EVENT_CD = "2606356"
TITLE_MARK = "リセールチケット一覧"
EMPTY_MARK = "出品されたリセールチケットはありません"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
CONFIG_KEYS = ("GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO", "EVENT_CD")
MIN_INTERVAL = 30
JITTER = 10
ERROR_ALERT_AFTER = timedelta(minutes=30)
LOCK_PORT = 47613  # 常駐の二重起動防止用

log = logging.getLogger("resale_watch")


# ---------- 設定 ----------

def read_env_file(path=ENV_FILE):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_password():
    """アプリパスワードを画面に表示せずに入力させ、.env に書き込む。"""
    import getpass
    if sys.stdin is not None and not sys.stdin.isatty():
        password = sys.stdin.read()  # 例: Get-Clipboard | python resale_watch.py --set-password
    else:
        password = getpass.getpass("Gmail アプリパスワード（入力して Enter。表示されません）: ")
    # 空白・改行・パイプで付く BOM などを除く（アプリパスワードは英字16文字）
    password = re.sub(r"[^A-Za-z0-9]", "", password)
    if len(password) != 16:
        print(f"16文字ではありません（{len(password)}文字）。もう一度実行してください。")
        return 1
    source = ENV_FILE if ENV_FILE.exists() else BASE_DIR / ".env.example"
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    new_line = f"GMAIL_APP_PASSWORD={password}"
    if any(line.startswith("GMAIL_APP_PASSWORD=") for line in lines):
        lines = [new_line if line.startswith("GMAIL_APP_PASSWORD=") else line for line in lines]
    else:
        lines.append(new_line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{ENV_FILE} に保存しました。")
    return 0


def get_config():
    """呼ぶたびに .env を読み直す（常駐中にパスワードを書き換えても再起動不要）。"""
    config = read_env_file()
    for key in CONFIG_KEYS:
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def event_codes(config):
    raw = config.get("EVENT_CD") or DEFAULT_EVENT_CD
    return [cd.strip() for cd in raw.split(",") if cd.strip()]


# ---------- 取得と判定 ----------

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def main_section(html):
    m = re.search(r"<main\b.*?</main>", html, re.S | re.I)
    return m.group(0) if m else html


def page_text(fragment):
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>|</p>|</li>|</tr>|</div>", "\n", fragment, flags=re.I)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))
    lines = (re.sub(r"[ \t　]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def item_links(fragment, page_url):
    """出品一覧内のリンクのうち、出品（item）を指しそうなものを集める。"""
    links = set()
    for href in re.findall(r"""href\s*=\s*["']([^"']+)["']""", fragment, re.I):
        href = htmllib.unescape(href).strip()
        if href.startswith(("#", "javascript:")):
            continue
        if "item" in href and "item/list" not in href:
            links.add(urljoin(page_url, href))
    return sorted(links)


def classify(status, html, page_url):
    """戻り値の status は empty / listed / unknown。unknown では通知しない。"""
    if status != 200:
        return {"status": "unknown", "reason": f"HTTP {status}"}
    if TITLE_MARK not in html:
        return {"status": "unknown", "reason": "一覧ページではない応答（メンテナンス・アクセス制限など）"}
    if EMPTY_MARK in html:
        return {"status": "empty", "items": []}
    main = main_section(html)
    text = page_text(main)
    links = item_links(main, page_url)
    # 出品リンクが取れない構造だった場合は本文ハッシュで代用する
    items = links or ["digest:" + hashlib.sha256(text.encode()).hexdigest()[:16]]
    return {"status": "listed", "items": items, "excerpt": text[:1000]}


def new_listings(prev, cur):
    """通知すべき新しい出品を返す。空リストなら通知しない。"""
    if cur["status"] != "listed":
        return []
    if not prev or prev.get("status") != "listed":
        return cur["items"]
    seen = set(prev.get("items", []))
    # digest は本文の細かな変化でも変わるので、出品ありが続いている間は再通知しない
    return [i for i in cur["items"] if i not in seen and not i.startswith("digest:")]


# ---------- 状態 ----------

def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------- メール ----------

def send_mail(subject, body):
    config = get_config()
    user = config.get("GMAIL_USER")
    password = (config.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = [a.strip() for a in (config.get("MAIL_TO") or user or "").split(",") if a.strip()]
    if not user or not password or not to:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD / MAIL_TO が設定されていません（.env を確認）")
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    log.info("メール送信: %s -> %s", subject, ", ".join(to))


def now_jst():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def with_label(label, subject):
    return f"{label} {subject}" if label else subject


def notify_listing(cd, url, result, items, label):
    links = [i for i in items if not i.startswith("digest:")]
    lines = [
        "リセールチケットの出品を検知しました。急いで確認してください。",
        "",
        f"公演コード: {cd}",
        f"検知時刻: {now_jst()}",
        f"一覧ページ: {url}",
        "",
    ]
    if links:
        lines.append(f"新しい出品リンク（{len(links)}件）:")
        lines += [f"- {link}" for link in links]
        lines.append("")
    lines += ["--- ページ抜粋 ---", result.get("excerpt", "")]
    send_mail(with_label(label, f"【リセール出品】eventCd {cd}"), "\n".join(lines))


# ---------- チェック本体 ----------

def check_event(cd, state, label):
    """1公演をチェックする。戻り値は (取得・判定に成功したか, 状態が変わったか)。

    メール送信に失敗した場合は状態を更新しない（次回のチェックで再送を試みる）。
    """
    url = LIST_URL.format(cd)
    try:
        status, html = fetch(url)
    except (urllib.error.URLError, OSError) as e:
        log.warning("%s: 取得失敗 %s", cd, e)
        return False, False
    result = classify(status, html, url)
    if result["status"] == "unknown":
        log.warning("%s: unknown (%s)", cd, result["reason"])
        return False, False

    prev = state.get(cd)
    fresh = new_listings(prev, result)
    log.info("%s: %s%s", cd, result["status"],
             f" / 新規 {len(fresh)}件" if fresh else "")
    if fresh:
        notify_listing(cd, url, result, fresh, label)

    entry = {"status": result["status"], "items": result["items"]}
    if prev and {k: prev.get(k) for k in entry} == entry:
        return True, False
    entry["changed_at"] = now_jst()
    state[cd] = entry
    return True, True


def check_all(state_path, state, label):
    """全公演をチェックし、状態が変わっていれば保存する。

    戻り値は (全件正常に取得・判定できたか, 例外（メール送信失敗など）が起きたか)。
    """
    ok = True
    crashed = False
    changed = False
    for cd in event_codes(get_config()):
        try:
            o, c = check_event(cd, state, label)
        except Exception:
            log.exception("%s: チェック中にエラー", cd)
            o, c = False, False
            crashed = True
        ok &= o
        changed |= c
    if changed:
        save_state(state_path, state)
    return ok, crashed


def run_once(state_path, label):
    # 一時的な取得失敗では失敗扱いにしない（Actions の失敗メールが大量に来るのを防ぐ）
    _, crashed = check_all(state_path, load_state(state_path), label)
    return 1 if crashed else 0


def run_loop(state_path, label, interval):
    try:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log.error("すでに別の監視プロセスが動いているため終了します")
        return 1

    config = get_config()
    if not config.get("GMAIL_APP_PASSWORD"):
        log.warning(".env に GMAIL_APP_PASSWORD がありません。設定するまでメールは送れません")
    log.info("監視開始: eventCd=%s 間隔=%d秒", ",".join(event_codes(config)), interval)

    state = load_state(state_path)
    fail_since = None
    error_notified = False
    while True:
        try:
            ok, _ = check_all(state_path, state, label)
        except Exception:
            log.exception("チェック中にエラー")
            ok = False

        try:
            if ok:
                if error_notified:
                    send_mail(with_label(label, "【リセール監視】復旧しました"),
                              f"{now_jst()} にページの取得が復旧しました。監視を続けています。")
                fail_since, error_notified = None, False
            else:
                fail_since = fail_since or datetime.now(JST)
                if not error_notified and datetime.now(JST) - fail_since >= ERROR_ALERT_AFTER:
                    send_mail(with_label(label, "【リセール監視】ページを取得できません"),
                              f"{fail_since.strftime('%Y-%m-%d %H:%M')} から出品一覧ページを正常に取得できていません。\n"
                              f"ネット接続やぴあ側のメンテナンスを確認してください。\n"
                              f"詳細: {LOG_FILE}")
                    error_notified = True
        except Exception:
            log.exception("監視エラー通知の送信に失敗")

        time.sleep(max(MIN_INTERVAL, interval + random.uniform(-JITTER, JITTER)))


# ---------- エントリポイント ----------

def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stderr is not None:  # pythonw では stderr が無い
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        log.addHandler(stream)
    log.setLevel(logging.INFO)


def main(argv=None):
    p = argparse.ArgumentParser(description="チケットぴあ リセール出品監視")
    p.add_argument("--once", action="store_true", help="1回だけチェックして終了")
    p.add_argument("--state", default=str(DEFAULT_STATE), help="状態ファイルのパス")
    p.add_argument("--label", default="", help="メール件名の先頭に付ける文字列")
    p.add_argument("--interval", type=int, default=60, help=f"チェック間隔（秒、最小 {MIN_INTERVAL}）")
    p.add_argument("--test-mail", action="store_true", help="テストメールを送信して終了")
    p.add_argument("--check-file", metavar="HTML", help="保存済み HTML を判定して結果を表示（送信なし）")
    p.add_argument("--set-password", action="store_true", help="Gmail アプリパスワードを入力して .env に保存")
    args = p.parse_args(argv)

    if args.set_password:
        return set_password()

    if args.check_file:
        html = Path(args.check_file).read_text(encoding="utf-8", errors="replace")
        result = classify(200, html, LIST_URL.format(DEFAULT_EVENT_CD))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    setup_logging()

    if args.test_mail:
        cd = event_codes(get_config())[0]
        send_mail(with_label(args.label, "【リセール監視】テストメール"),
                  f"テスト送信です（{now_jst()}）。\n"
                  f"出品を検知するとこのアドレスに通知します。\n\n"
                  f"監視対象: {LIST_URL.format(cd)}")
        return 0

    if args.once:
        return run_once(args.state, args.label)

    return run_loop(args.state, args.label, max(MIN_INTERVAL, args.interval))


if __name__ == "__main__":
    sys.exit(main())
