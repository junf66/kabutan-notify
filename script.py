"""
株探「市場ニュース＞注目」(category=9) から、タイトルに「好悪材料」を含む
当日付の記事を抽出し、Gmail で自分宛にメール送信するスクリプト。

GitHub Actions 上から定期実行される想定。
"""

from __future__ import annotations

import html as html_module
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import jpholiday  # 日本の祝日判定
except ImportError:
    jpholiday = None  # type: ignore

BASE_URL = "https://kabutan.jp"
LIST_URL = "https://kabutan.jp/news/marketnews/?category=9"
STOCK_URL_TEMPLATE = "https://kabutan.jp/stock/?code={code}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

JST = timezone(timedelta(hours=9))

MAX_PAGES = 3
REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_BACKOFF = 2.0

TITLE_KEYWORD = "好悪材料"
SECTION_HEADING = "【好悪材料が混在】"
SECTION_END_PATTERN = re.compile(r"(※|⇒⇒)")
STOCK_HEADER_RE = re.compile(
    r"^[\s■]*([^\n<>\[\]【】＜＞［］■]+?)\s*"
    r"[<＜]\s*([0-9A-Z]{4,5})\s*[>＞]\s*"
    r"[\[［]([^\]］]+)[\]］]",
    re.MULTILINE,
)
# 株探の記事リンクは ?b=<news_id> 形式（例: ?b=n202604271091）。
# news_id 内に YYYYMMDD が含まれる。
ARTICLE_HREF_RE = re.compile(r"[?&]b=([A-Za-z0-9]+)")
NEWS_ID_DATE_RE = re.compile(r"(\d{8})")


# --------------------------------------------------------------------------- #
# データクラス
# --------------------------------------------------------------------------- #
@dataclass
class StockEntry:
    name: str
    code: str
    market: str
    body: str


@dataclass
class Article:
    title: str
    url: str
    stocks: list[StockEntry] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8"
        ),
    }
    last_err: Exception | None = None
    for attempt in range(RETRY_COUNT):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            # 文字化け対策: meta charset を見て decode させる
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            last_err = e
            if attempt < RETRY_COUNT - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise RuntimeError(f"GET failed: {url}: {last_err}")


# --------------------------------------------------------------------------- #
# 一覧ページ
# --------------------------------------------------------------------------- #
def list_target_articles(today: datetime) -> list[tuple[str, str]]:
    """
    最大 MAX_PAGES ページ遡って、タイトルに TITLE_KEYWORD を含む
    当日付の (title, url) を返す。重複は除外。

    記事 URL は `?b=<news_id>` 形式。news_id 内に YYYYMMDD が含まれるため、
    それで当日判定する。
    """
    yyyymmdd = today.strftime("%Y%m%d")
    seen_ids: set[str] = set()
    results: list[tuple[str, str]] = []

    for page in range(1, MAX_PAGES + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}&page={page}"
        html = http_get(url)
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.find_all("a", href=True):
            href_raw = a["href"]
            m = ARTICLE_HREF_RE.search(href_raw)
            if not m:
                continue
            news_id = m.group(1)

            date_match = NEWS_ID_DATE_RE.search(news_id)
            if not date_match or date_match.group(1) != yyyymmdd:
                continue

            title = a.get_text(strip=True)
            if not title or TITLE_KEYWORD not in title:
                continue

            if news_id in seen_ids:
                continue
            seen_ids.add(news_id)
            # URL は ?b=<news_id> に正規化
            normalized = f"{BASE_URL}/news/marketnews/?b={news_id}"
            results.append((title, normalized))

    return results


# --------------------------------------------------------------------------- #
# 記事ページ
# --------------------------------------------------------------------------- #
def parse_article(title: str, url: str) -> Article:
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    body_text = _extract_body_text(soup)

    # 1) 「【好悪材料が混在】」セクションを優先抽出
    section_text = _extract_section(body_text, SECTION_HEADING)
    if section_text:
        print(
            f"[INFO] 「{SECTION_HEADING}」セクションを抽出"
            f" (length={len(section_text)})"
        )
        stocks = _parse_stock_entries(section_text)
    else:
        # 2) セクションが無い場合は本文全体から銘柄ごとの開示情報を抽出
        print("[INFO] 「混在」セクション無し → 本文全体から銘柄抽出")
        stocks = _parse_stock_entries(body_text)

    if not stocks:
        # 構造が予想と異なる場合の診断ログ
        head = body_text[:1200].replace("\n", "⏎")
        print(
            f"[DEBUG] body_text len={len(body_text)} head=<<{head}>>",
            file=sys.stderr,
        )

    return Article(title=title, url=url, stocks=stocks)


def _extract_body_text(soup: BeautifulSoup) -> str:
    """記事本文の text を取得。

    複数の候補セレクタを試し、`【悪材料】` まで含むなど **最も内容量の大きい**
    要素を採用する（株探は記事種別ごとに body コンテナが異なる）。
    """
    candidates = [
        "#shijo",
        "div#shijo",
        "div.body",
        "div#news_contents",
        "div.news_contents",
        "article",
        "div.news_box",
        "div#main",
        "main",
    ]

    def _node_text(node) -> str:
        for tag in node.select("script, style, .ad, .adsbygoogle"):
            tag.decompose()
        t = node.get_text("\n", strip=False)
        t = re.sub(r"\r\n?", "\n", t)
        t = "\n".join(line.strip() for line in t.split("\n"))
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    best_sel = None
    best_text = ""
    for sel in candidates:
        node = soup.select_one(sel)
        if not node:
            continue
        t = _node_text(node)
        if len(t) > len(best_text):
            best_text = t
            best_sel = sel

    if not best_text and soup.body is not None:
        best_sel = "body"
        best_text = _node_text(soup.body)

    print(
        f"[DEBUG] body container = {best_sel}, length = {len(best_text)}",
        file=sys.stderr,
    )
    for kw in ("【好材料】", "【悪材料】", SECTION_HEADING, "※", "⇒⇒"):
        idx = best_text.find(kw)
        print(f"[DEBUG]   '{kw}' position = {idx}", file=sys.stderr)

    return best_text


def _extract_section(text: str, heading: str) -> str | None:
    """heading 直後から ※ または ⇒⇒ または末尾の手前までを返す。"""
    idx = text.find(heading)
    if idx < 0:
        return None
    start = idx + len(heading)
    rest = text[start:]
    end_match = SECTION_END_PATTERN.search(rest)
    if end_match:
        rest = rest[: end_match.start()]
    return rest.strip() or None


def _parse_stock_entries(text: str) -> list[StockEntry]:
    """テキストから「銘柄名 <コード> [市場]」のブロックを抽出。"""
    matches = list(STOCK_HEADER_RE.finditer(text))
    entries: list[StockEntry] = []
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        code = m.group(2).strip()
        market = m.group(3).strip()
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[block_start:block_end]

        # ※ や ⇒⇒ で打ち切り
        cut = SECTION_END_PATTERN.search(body)
        if cut:
            body = body[: cut.start()]
        body = body.strip()

        if not body:
            continue
        entries.append(
            StockEntry(name=name, code=code, market=market, body=body)
        )
    return entries


# --------------------------------------------------------------------------- #
# メール組み立て
# --------------------------------------------------------------------------- #
def build_subject(today: datetime, article_count: int) -> str:
    return (
        f"【好悪材料】{today.year}年{today.month}月{today.day}日分"
        f"（{article_count}件）"
    )


def build_plain_body(articles: list[Article]) -> str:
    out: list[str] = []
    for art in articles:
        out.append(f"■ {art.title}")
        out.append(art.url)
        out.append("")
        for s in art.stocks:
            out.append(f"{s.name} <{s.code}> [{s.market}]")
            out.append(f"  {STOCK_URL_TEMPLATE.format(code=s.code)}")
            out.append(s.body)
            out.append("")
        out.append("")
    return "\n".join(out).strip() + "\n"


def build_html_body(articles: list[Article]) -> str:
    def esc(s: str) -> str:
        return html_module.escape(s)

    parts: list[str] = [
        "<!DOCTYPE html><html><head><meta charset='UTF-8'></head>",
        "<body style=\"font-family: sans-serif; line-height:1.6;\">",
    ]
    for art in articles:
        parts.append(
            f"<h2 style=\"border-bottom:2px solid #333;padding-bottom:4px;margin-bottom:4px;\">"
            f"<a href=\"{esc(art.url)}\" style=\"color:#333;text-decoration:none;\">"
            f"{esc(art.title)}</a></h2>"
        )
        parts.append(
            f"<p style=\"margin:0 0 12px 0;font-size:0.9em;\">"
            f"<a href=\"{esc(art.url)}\">{esc(art.url)}</a></p>"
        )
        for s in art.stocks:
            stock_url = STOCK_URL_TEMPLATE.format(code=s.code)
            body_html = esc(s.body).replace("\n", "<br>")
            parts.append(
                "<div style=\"margin:0 0 18px 0;\">"
                f"<p style=\"margin:0 0 4px 0;font-weight:bold;\">"
                f"{esc(s.name)} &lt;<a href=\"{esc(stock_url)}\">"
                f"{esc(s.code)}</a>&gt; [{esc(s.market)}]</p>"
                f"<div style=\"margin:0;white-space:normal;\">{body_html}</div>"
                "</div>"
            )
    parts.append("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #
def send_mail(subject: str, plain: str, html: str) -> None:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ["MAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(plain, charset="utf-8")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)


# --------------------------------------------------------------------------- #
# メイン
# --------------------------------------------------------------------------- #
def _is_jp_holiday(d: datetime) -> bool:
    if jpholiday is None:
        return False
    return jpholiday.is_holiday(d.date())


def _should_run(today: datetime) -> tuple[bool, str]:
    """
    `TRIGGER_SCHEDULE` env と祝日判定で、このランで送信処理を行うかを決める。

    - `5 11 * * 1-4`（Mon-Thu 20:05）: 祝日ならスキップ（13:35 で対応済）
    - `35 4 * * 1-5`（Mon-Fri 13:35）: 祝日のみ実行
    - `35 4 * * 0`（Sun 13:35）: 常に実行
    - workflow_dispatch / 不明: 常に実行
    """
    schedule = os.environ.get("TRIGGER_SCHEDULE", "").strip()
    if not schedule:
        return True, "manual / unknown trigger → run"

    is_holiday = _is_jp_holiday(today)

    if schedule == "5 11 * * 1-4":
        if is_holiday:
            return False, (
                "20:05 cron だが本日は祝日 (13:35 cron で対応済) → skip"
            )
        return True, "20:05 cron / 平日 → run"

    if schedule == "35 4 * * 1-5":
        if not is_holiday:
            return False, "13:35 cron だが本日は祝日でない → skip"
        return True, "13:35 cron / 祝日 → run"

    if schedule == "35 4 * * 0":
        return True, "13:35 Sun cron → run"

    return True, f"unknown schedule '{schedule}' → run"


def main() -> int:
    today = datetime.now(JST)

    # workflow_dispatch で target_date が指定されている場合はその日付を使う
    target_override = os.environ.get("TARGET_DATE", "").strip()
    if target_override:
        try:
            today = datetime.strptime(target_override, "%Y-%m-%d").replace(
                tzinfo=JST
            )
            print(f"[INFO] TARGET_DATE override = {target_override}")
        except ValueError as e:
            print(
                f"[ERROR] TARGET_DATE='{target_override}' を解釈できません: {e}",
                file=sys.stderr,
            )
            return 1

    print(f"[INFO] today (JST) = {today.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[INFO] is_jp_holiday = {_is_jp_holiday(today)}")

    # target_date 指定時はゲート判定を行わない（手動リカバリ用）
    if not target_override:
        ok, reason = _should_run(today)
        print(f"[INFO] gate: {reason}")
        if not ok:
            return 0

    targets = list_target_articles(today)
    print(f"[INFO] 対象記事候補: {len(targets)} 件")
    for t, u in targets:
        print(f"  - {t}  {u}")

    articles: list[Article] = []
    for title, url in targets:
        try:
            art = parse_article(title, url)
        except Exception as e:
            print(f"[WARN] 記事解析失敗 ({url}): {e}", file=sys.stderr)
            continue
        if art.stocks:
            articles.append(art)
        else:
            print(f"[WARN] 銘柄抽出 0 件 のためスキップ: {url}")

    if not articles:
        print("[INFO] 該当なし")
        return 0

    subject = build_subject(today, len(articles))
    plain = build_plain_body(articles)
    html = build_html_body(articles)

    print(f"[INFO] 送信件名: {subject}")
    print(f"[INFO] 銘柄合計: {sum(len(a.stocks) for a in articles)} 件")

    send_mail(subject, plain, html)
    print("[INFO] 送信完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
