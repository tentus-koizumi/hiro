"""公募ウォッチ: 情報源アダプタ＋共通の正規化・関連度判定・新着判定・HTML生成。MVPはEU/TED。"""
import html
import json
import os
import time
from datetime import date, timedelta

import requests

TED_ENDPOINT = "https://api.ted.europa.eu/v3/notices/search"
TED_FIELDS = ["publication-number", "notice-title", "buyer-name",
              "publication-date", "deadline-date-lot", "links"]
# tentus中核のCPV（マーケ/広告/コンサル/PR/Web/デザイン/文化/イベント）
TENTUS_CPV = ["79340000", "79341400", "79400000", "79416000",
              "72400000", "79822500", "92000000", "79952000"]


def build_ted_query(since: str) -> str:
    """TED expert query を組み立てる。since は 'YYYYMMDD'。"""
    cpv = " ".join(TENTUS_CPV)
    return (f"place-of-performance IN (JPN) "
            f"AND classification-cpv IN ({cpv}) "
            f"AND publication-date>={since}")


def _pick_lang(value):
    """多言語dict/listから英語優先で文字列を取り出す（入れ子も再帰的に畳み込む）。"""
    if isinstance(value, dict):
        if "eng" in value:
            return _pick_lang(value["eng"])
        for v in value.values():
            return _pick_lang(v)
        return None
    if isinstance(value, list):
        parts = [_pick_lang(x) for x in value]
        parts = [p for p in parts if p]
        return ", ".join(parts) if parts else None
    return value


def normalize_ted_notice(notice: dict) -> dict:
    """TEDの1公告を共通スキーマ Tender(dict) に正規化する。"""
    pub = notice.get("publication-date")
    published = pub[:10] if isinstance(pub, str) else None

    deadline = notice.get("deadline-date-lot")
    if isinstance(deadline, list):
        deadline = deadline[0] if deadline else None
    if isinstance(deadline, str):
        deadline = deadline[:10]

    links = notice.get("links") or {}
    html_links = links.get("html") if isinstance(links, dict) else {}
    url = None
    if isinstance(html_links, dict) and html_links:
        url = html_links.get("ENG") or next(iter(html_links.values()))

    return {
        "source": "EU/TED",
        "id": notice.get("publication-number"),
        "title": _pick_lang(notice.get("notice-title")),
        "buyer": _pick_lang(notice.get("buyer-name")),
        "place": "日本",
        "published": published,
        "deadline": deadline,
        "url": url,
    }


_RELEVANT_KEYWORDS = (
    "market", "advertis", "communication", "public relations", "promotion",
    "event", "exhibition", "fair", "congress", "web", "digital", "design",
    "media", "campaign", "consultancy", "japan",
)


def is_tentus_relevant(tender: dict) -> bool:
    """tentus向き（マーケ/広報/Web/イベント/コンサル）案件かを title から判定する。"""
    title = (tender.get("title") or "").lower()
    return any(k in title for k in _RELEVANT_KEYWORDS)


# 既見公告ID。Cowork では実行環境が毎回まっさらなため、Drive から取り寄せたファイルを
# 環境変数 TENDER_SEEN_FILE で指す（省略時はスクリプトと同じディレクトリ）。
SEEN_FILE = os.environ.get(
    "TENDER_SEEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "tender_seen.json"),
)


def load_seen(path: str = SEEN_FILE) -> set:
    """既見の公告ID集合を読み込む。無ければ空集合。"""
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(path: str, ids: set) -> None:
    """既見の公告ID集合を保存する。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def mark_new(tenders: list, seen: set) -> list:
    """既見集合に無い公告へ is_new=True を付与した新しいリストを返す（非破壊）。"""
    return [{**t, "is_new": t.get("id") not in seen} for t in tenders]


def fetch_ted(since: str, retries: int = 4, timeout: int = 30) -> list:
    """TEDから日本履行×tentus中核CPVの公告を取得して正規化する。

    リトライ尽きても取得できなければ RuntimeError を送出する（呼び出し側で握る）。
    """
    body = {
        "query": build_ted_query(since),
        "fields": TED_FIELDS,
        "limit": 50,
        "paginationMode": "PAGE_NUMBER",
    }
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.post(TED_ENDPOINT, json=body, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if "notices" in data:
                return [normalize_ted_notice(n) for n in data["notices"]]
            last_err = data.get("message") or data.get("error") or f"no 'notices' key: {str(data)[:200]}"
        except Exception as e:  # noqa: BLE001 - ネットワーク/JSONの全失敗をリトライ対象にする
            last_err = str(e)
        if attempt < retries - 1:
            time.sleep(attempt + 1)
    raise RuntimeError(f"TED取得失敗: {last_err}")


STALE_DAYS = 180  # 締切不明の案件は公開（告知）から約半年超で実質終了とみなす


def _tender_is_open(tender: dict, today: str) -> bool:
    """応募可能とみなせるか。today/日付は 'YYYY-MM-DD'。
    - 締切が判明: today 以降なら True（未来）、過去なら False（応募終了）。
    - 締切不明: 公開から STALE_DAYS 日を超えたら False（実質終了）、以内なら True。
      公開日も不明なら True（判断不能なので残す）。"""
    deadline = tender.get("deadline")
    if deadline:
        return deadline >= today
    published = tender.get("published")
    if not published:
        return True
    try:
        cutoff = (date.fromisoformat(today) - timedelta(days=STALE_DAYS)).isoformat()
    except ValueError:
        return True
    return published >= cutoff


def collect(since: str, seen_path: str = SEEN_FILE, today: str = None):
    """全アダプタ（MVPはTEDのみ）を集約し、締切切れ除外・新着判定・並べ替えした
    Tender リストと取得成功フラグを返す。取得失敗時は ([], False)。"""
    try:
        tenders = fetch_ted(since)
        ok = True
    except RuntimeError:
        return [], False
    today = today or date.today().isoformat()
    tenders = [t for t in tenders if _tender_is_open(t, today)]
    seen = load_seen(seen_path)
    tenders = mark_new(tenders, seen)
    tenders.sort(key=lambda t: (t.get("published") or ""), reverse=True)
    save_seen(seen_path, seen | {t["id"] for t in tenders if t.get("id")})
    return tenders, ok


def apply_translations(tenders: list, translations: dict | None) -> list:
    """Cowork 側で作った日本語訳 {id: title_ja} を各 tender に付与した新リストを返す（非破壊）。

    旧 translate_titles（Claude API 直叩き）の置き換え。翻訳が無い/空なら title_ja=None とし、
    build_tender_section が原文表示にフォールバックする。
    """
    translations = translations or {}
    return [{**t, "title_ja": (translations.get(str(t.get("id"))) or None)} for t in tenders]


def build_tender_section(tenders: list, ok: bool) -> str:
    """公募ウォッチセクションのHTMLを生成する。weekly_report のインラインstyle流儀に合わせる。"""
    head = ('<div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;'
            'border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">'
            '<div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;'
            'text-transform:uppercase;letter-spacing:1px">📋 公募ウォッチ（EU/TED・日本市場向け）</div>')
    foot = "</div>"

    if not ok:
        return head + '<div style="color:#c62828;font-size:12px;padding:8px 0">公募データの取得失敗（今回はスキップ）</div>' + foot
    if not tenders:
        return head + '<div style="color:#aaa;font-size:12px;padding:8px 0">該当案件なし</div>' + foot

    card_parts = []
    for t in tenders:
        new_badge = ('<span style="background:#e86e2a;color:#fff;font-size:10px;font-weight:700;'
                     'padding:1px 6px;border-radius:3px;margin-right:4px">🆕 NEW</span>') if t.get("is_new") else ""
        rel_badge = ('<span style="background:#9e4a10;color:#fff;font-size:10px;font-weight:700;'
                     'padding:1px 6px;border-radius:3px">tentus向き</span>') if is_tentus_relevant(t) else ""
        badges = new_badge + rel_badge
        badge_line = f'<div style="margin-bottom:5px">{badges}</div>' if badges else ""
        title_orig = str(t.get("title") or t.get("id") or "")
        title_ja = t.get("title_ja")
        main = html.escape(title_ja or title_orig)
        orig_line = ""
        if title_ja and title_orig:
            orig_line = (f'<div style="font-size:11px;color:#aaa;margin-top:3px;line-height:1.4">'
                         f'{html.escape(title_orig)}</div>')
        url = html.escape(t.get("url") or "#")
        buyer = html.escape(str(t.get("buyer") or "-"))
        published = html.escape(str(t.get("published") or "-"))
        deadline = html.escape(str(t.get("deadline") or "公告で確認"))
        card_parts.append(f"""
        <div style="padding:14px 0;border-bottom:1px solid #f0f0f0">
            {badge_line}
            <a href="{url}" style="color:#1565c0;text-decoration:none;font-size:14px;font-weight:600;line-height:1.5">{main}</a>
            {orig_line}
            <div style="font-size:12px;color:#888;margin-top:6px;line-height:1.6">{buyer}<br>公開 {published} ・ 締切 {deadline}</div>
        </div>""")
    cards = "".join(card_parts)
    note = ('<div style="margin-top:12px;font-size:11px;color:#bbb;line-height:1.5">'
            'EU各国の輸出振興機関・ワイン委員会・観光局等が日本市場向けに発注する案件。'
            '案件名はClaudeによる日本語訳（原文併記）。締切は公告ページで確認。</div>')
    return head + cards + note + foot
