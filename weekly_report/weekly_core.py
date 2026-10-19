#!/usr/bin/env python3
"""
tentus.jp Weekly Report — Cowork 版コア

旧 weekly_report.py から「データ取得」と「HTML生成」だけを切り出したもの。
Claude API 呼び出し・Resend 送信・Google Tasks/Calendar 同期は Cowork 側
（Claude 本体＋Gmail/Calendar コネクタ）が担うため、このファイルには含まない。

使い方:
  python3 weekly_core.py fetch  --out data.json [--week-start YYYY-MM-DD]
      GA4(2系統・ボット除外・8週推移) / Search Console / PageSpeed / TED を取得し
      JSON に保存。併せて Claude 向けインサイト生成プロンプトを data.json 内 "insight_prompt" に格納。
  python3 weekly_core.py build  --data data.json --insights insights.json --out report.html
                                [--touchpoint touchpoint.json] [--translations translations.json]
      Cowork 側で作った insights / 接点データ / 公募タイトル訳を合流させて HTML を生成。

環境変数:
  GA4_KEY_FILE / GA4_KEY_B64
      GA4 サービスアカウント JSON のパス、または base64 化した中身（既定 ./ga4_key.json）
  GOOGLE_TOKEN_FILE / GOOGLE_TOKEN_B64
      Search Console 用 OAuth トークン JSON のパス、または base64 化した中身（既定 ./google_token.json）
  TENDER_SEEN_FILE    公募ウォッチ既見IDファイル（既定 ./tender_seen.json、tender_watch.py 参照）
  PAGESPEED_API_KEY   任意
"""

import argparse
import html
from html import escape as esc  # build_html 内はローカル変数 html と衝突するため別名で使う
import json
import os
import sys
from datetime import date, timedelta

import requests
import tender_watch
from google.oauth2 import service_account
import google.auth.transport.requests

# ── 設定 ──────────────────────────────────────────────
_HERE             = os.path.dirname(os.path.abspath(__file__))
GA4_PROPERTY      = "528060272"
SC_SITE_URL       = "https://tentus.jp/"


def _credential_file(name: str, default_filename: str) -> str:
    """認証 JSON のパスを決める。優先順: <NAME>_FILE → <NAME>_B64（base64 の中身）→ 既定ファイル。

    Cowork のクラウド環境では実行のたびに VM がまっさらになるため、環境設定の
    「環境変数」に base64 化した JSON を置き（1行に収まる）、起動時にファイルへ展開する。
    """
    path = os.environ.get(f"{name}_FILE")
    if path:
        return path
    b64 = os.environ.get(f"{name}_B64")
    if b64:
        import base64, tempfile
        fd, path = tempfile.mkstemp(prefix=f"{name.lower()}_", suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(b64))
        return path
    return os.path.join(_HERE, default_filename)


GA4_KEY_FILE      = _credential_file("GA4_KEY", "ga4_key.json")
GOOGLE_TOKEN_FILE = _credential_file("GOOGLE_TOKEN", "google_token.json")
# ──────────────────────────────────────────────────────


def get_week_range(week_start: str = None):
    """先週月曜〜日曜を返す。week_start(YYYY-MM-DD, 月曜) を渡すとその週。"""
    if week_start:
        last_monday = date.fromisoformat(week_start)
        if last_monday.weekday() != 0:
            raise SystemExit(f"--week-start は月曜日を指定してください: {week_start}")
    else:
        today = date.today()
        last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday  = last_monday + timedelta(days=6)
    return last_monday.isoformat(), last_sunday.isoformat()


# ボット判定のしきい値。2026-07-13 の英語版公開以降、滞在0.0秒・直帰100% の
# セッションが流入し、直帰率とセッション数を押し上げていた（2026-08-24 調査）。
# 国では切らない。日本にも滞在1.9秒・直帰94% のコホートがあり、逆にシンガポール
# には滞在15.6秒の実訪問がある。EU代表部のような海外の実顧客を巻き込まないため、
# 判定は挙動（直帰率×滞在時間）のみで行う。
BOT_MIN_BOUNCE_RATE      = 0.9
BOT_MAX_AVG_DURATION_SEC = 2.0
BOT_MIN_SESSIONS         = 2   # 1セッションだけのコホートは断定しない
BOT_COHORT_DIMENSIONS    = ["country", "browser", "operatingSystem"]


def identify_bot_cohorts(report: dict) -> list:
    """GA4 の country×browser×OS 内訳から、ボットとみなすコホートを返す。

    report は dimensions=BOT_COHORT_DIMENSIONS、
    metrics=[sessions, bounceRate, averageSessionDuration] の順を前提とする。
    """
    cohorts = []
    for row in report.get("rows", []):
        dims = [d.get("value", "") for d in row.get("dimensionValues", [])]
        mets = [m.get("value", "0") for m in row.get("metricValues", [])]
        if len(dims) < 3 or len(mets) < 3:
            continue
        try:
            sessions = int(float(mets[0]))
            bounce   = float(mets[1])
            duration = float(mets[2])
        except (TypeError, ValueError):
            continue
        if sessions < BOT_MIN_SESSIONS:
            continue
        if bounce >= BOT_MIN_BOUNCE_RATE and duration < BOT_MAX_AVG_DURATION_SEC:
            cohorts.append({
                "country": dims[0], "browser": dims[1], "operatingSystem": dims[2],
                "sessions": sessions,
            })
    return cohorts


def build_bot_exclusion_filter(cohorts: list):
    """ボットコホートを除外する GA4 FilterExpression を組む。空なら None。"""
    if not cohorts:
        return None
    expressions = [
        {"andGroup": {"expressions": [
            {"filter": {"fieldName": field,
                        "stringFilter": {"matchType": "EXACT", "value": cohort[field]}}}
            for field in BOT_COHORT_DIMENSIONS
        ]}}
        for cohort in cohorts
    ]
    return {"notExpression": {"orGroup": {"expressions": expressions}}}


def combine_filters(*expressions):
    """None を除いた FilterExpression を AND で結合する。全て None なら None。"""
    real = [e for e in expressions if e]
    if not real:
        return None
    if len(real) == 1:
        return real[0]
    return {"andGroup": {"expressions": real}}


def fetch_bot_cohorts(start_date: str, end_date: str, hostname_filter: str = None) -> list:
    """対象期間の country×browser×OS 内訳を取得し、ボットコホートを判定する。"""
    try:
        report = _run_ga4_report({
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": d} for d in BOT_COHORT_DIMENSIONS],
            "metrics": [
                {"name": "sessions"},
                {"name": "bounceRate"},
                {"name": "averageSessionDuration"},
            ],
        }, hostname_filter=hostname_filter)
        return identify_bot_cohorts(report)
    except Exception as e:
        print(f"  ボット判定に失敗（除外なしで続行）: {e}")
        return []


def _hostname_filter_expression(hostname_filter: str):
    if not hostname_filter:
        return None
    return {"filter": {"fieldName": "hostname",
                       "stringFilter": {"matchType": "EXACT", "value": hostname_filter}}}


def _run_ga4_report(body: dict, hostname_filter: str = None, bot_filter: dict = None) -> dict:
    """GA4 runReport を1本実行する。hostname とボット除外を AND で適用する。"""
    credentials = service_account.Credentials.from_service_account_file(
        GA4_KEY_FILE,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY}:runReport"
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }
    combined = combine_filters(_hostname_filter_expression(hostname_filter), bot_filter)
    if combined:
        body = dict(body)
        body["dimensionFilter"] = combined
    return requests.post(url, headers=headers, json=body).json()


def fetch_ga4(start_date: str, end_date: str, hostname_filter: str = None,
              bot_filter: dict = None) -> dict:
    prev_start = (date.fromisoformat(start_date) - timedelta(days=7)).isoformat()
    prev_end   = (date.fromisoformat(end_date)   - timedelta(days=7)).isoformat()

    def run(body):
        return _run_ga4_report(body, hostname_filter=hostname_filter, bot_filter=bot_filter)

    summary = run({
        "dateRanges": [
            {"startDate": start_date, "endDate": end_date,   "name": "today"},
            {"startDate": prev_start, "endDate": prev_end,   "name": "prev"},
        ],
        "metrics": [
            {"name": "sessions"},
            {"name": "activeUsers"},
            {"name": "newUsers"},
            {"name": "screenPageViews"},
            {"name": "bounceRate"},
            {"name": "averageSessionDuration"},
        ],
    })

    channels = run({
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "bounceRate"},
            {"name": "averageSessionDuration"},
        ],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
    })

    pages = run({
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "sessions"}, {"name": "screenPageViews"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 5,
    })

    devices = run({
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "deviceCategory"}],
        "metrics": [{"name": "sessions"}],
    })

    countries = run({
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "country"}],
        "metrics": [{"name": "sessions"}, {"name": "activeUsers"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 6,
    })

    return {
        "summary":   summary,
        "channels":  channels,
        "pages":     pages,
        "devices":   devices,
        "countries": countries,
        "period":    {"start": start_date, "end": end_date, "prev_start": prev_start, "prev_end": prev_end},
    }


def fetch_channel_trend(weeks: int = 8, bot_filter: dict = None) -> list:
    """直近 weeks 週（月〜日）の AI Assistant / Organic Search / 全体セッション推移。
    AI Assistant は ChatGPT・Perplexity 等の生成AI経由流入を集約したGA4デフォルトチャネル。

    bot_filter はサマリー側と同じものを渡す。片方だけ除外すると推移表と
    今週の数字が食い違い、前週比の判断を誤る。"""
    from collections import defaultdict
    days = weeks * 7 + 7  # バケット取りこぼし防止に1週分余裕
    resp = _run_ga4_report({
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "dimensions": [{"name": "date"}, {"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}],
        "limit": 10000,
    }, bot_filter=bot_filter)
    wk = defaultdict(lambda: defaultdict(int))
    for row in resp.get("rows", []):
        d = row["dimensionValues"][0]["value"]
        ch = row["dimensionValues"][1]["value"]
        dt = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        monday = dt - timedelta(days=dt.weekday())
        wk[monday.isoformat()][ch] += int(row["metricValues"][0]["value"])
    out = []
    for m in sorted(wk)[-weeks:]:
        ch = wk[m]
        out.append({
            "week": m,
            "ai": ch.get("AI Assistant", 0),
            "organic": ch.get("Organic Search", 0),
            "total": sum(ch.values()),
        })
    return out


# Search Console の取得上限。以前は query×country を rowLimit 50 で取っていたが、
# 2026-07-13 の英語版114記事公開で海外インプレッションが激増し（同週は全体789の
# うち海外639）、上位50行が海外クエリで埋まって日本の中位クエリが表から消えた。
# 「順位が落ちた」ように見えたが実データは 84→175 と増加しており、表示上の
# 取りこぼしだった（2026-08-24 調査）。日本と海外を別クエリで取り、枠の食い合いを断つ。
SC_ROW_LIMIT = 5000
SC_DISPLAY_JP = 10
SC_DISPLAY_OVERSEAS = 5


def fetch_search_console(start_date: str, end_date: str) -> dict:
    """Search Console の上位クエリを「日本」「海外」で別々に取得する。

    国を区別せず合算すると、海外からの英字"tentus"等の無関係なインプレッションが
    日本の指名検索（実際は1位）の平均順位を押し下げ、実態と乖離した値になる。
    かといって query×country を一括の上位N行で取ると、海外の行が枠を食って
    日本のクエリが落ちる。そこで country フィルタを分けた2本のクエリにして、
    どちらの取得件数も互いに影響しないようにしている。
    CTR・平均順位は API 側の集計値をそのまま使う（手計算より正確）。

    戻り値: {"jp": [行...], "overseas": [行...]}（dimensions=["query"]）
    """
    def _query(token: str, country_op: str):
        resp = requests.post(
            f"https://www.googleapis.com/webmasters/v3/sites/{requests.utils.quote(SC_SITE_URL, safe='')}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["query"],
                "rowLimit": SC_ROW_LIMIT,
                "dimensionFilterGroups": [{
                    "filters": [{
                        "dimension": "country",
                        "operator": country_op,
                        "expression": "jpn",
                    }]
                }],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("rows", [])

    try:
        token = get_google_access_token()
        return {
            "jp":       _query(token, "equals"),
            "overseas": _query(token, "notEquals"),
        }
    except Exception as e:
        print(f"Search Console 取得失敗: {e}")
        return {"jp": [], "overseas": []}


def aggregate_sc_by_region(sc_data: dict) -> list:
    """日本・海外それぞれの行に地域ラベルを付け、表示回数の降順で1本にまとめる。

    メール描画と Claude へのプロンプトで同じ集約結果を使うため関数化している
    （国を落として渡すと海外ノイズを日本の指名検索と混同した分析になる）。
    戻り値: [(表示回数, クエリ, 地域, CTR%, 平均順位), ...]
    """
    items = []
    for key, region in (("jp", "日本"), ("overseas", "海外")):
        for row in sc_data.get(key, []) or []:
            keys = row.get("keys", [])
            imp  = row.get("impressions", 0)
            clk  = row.get("clicks", 0)
            ctr  = round(clk / imp * 100, 1) if imp else 0.0
            items.append((
                imp,
                keys[0] if keys else "",
                region,
                ctr,
                round(row.get("position", 0), 1),
            ))
    items.sort(key=lambda x: x[0], reverse=True)
    return items


def select_sc_display_rows(items: list,
                           jp_limit: int = SC_DISPLAY_JP,
                           overseas_limit: int = SC_DISPLAY_OVERSEAS) -> list:
    """表示用に日本・海外それぞれの上位を取る。

    単純な上位N件だと海外の高表示回数クエリが枠を独占し、日本のクエリが
    毎週入れ替わって見える（2026-08-24 調査）。地域ごとに枠を確保して防ぐ。
    """
    picked = []
    for region, limit in (("日本", jp_limit), ("海外", overseas_limit)):
        picked += [it for it in items if it[2] == region][:limit]
    picked.sort(key=lambda x: x[0], reverse=True)
    return picked


# PageSpeed Insights の Lab 計測（Lighthouse）は1回ごとのブレが大きいため、
# 複数回計測して Performance スコアの中央値の回を採用し、外れ値を除外する。
PAGESPEED_SAMPLES = 3


def _fetch_pagespeed_once(target_url: str, cache_buster: str = "") -> dict | None:
    """PageSpeed Insights を1回計測。成功時は結果 dict、失敗時は None。

    PSI は URL 単位でラボ計測結果を数分間キャッシュするため、連続呼び出しは
    同一の計測を返す。cache_buster でクエリ文字列を変えると独立した計測を強制でき、
    複数サンプルでの中央値採用が機能する（静的サイトのためクエリは無視され同一ページを計測）。
    """
    url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    measured_url = target_url
    if cache_buster:
        sep = "&" if "?" in measured_url else "?"
        measured_url = f"{measured_url}{sep}_psi={cache_buster}"
    params = {
        "url": measured_url,
        "strategy": "mobile",
    }
    pagespeed_key = os.environ.get("PAGESPEED_API_KEY", "")
    if pagespeed_key:
        params["key"] = pagespeed_key

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        lr = data.get("lighthouseResult", {})
        cats = lr.get("categories", {})
        audits = lr.get("audits", {})
        return {
            "performance": round((cats.get("performance", {}).get("score") or 0) * 100),
            "lcp": audits.get("largest-contentful-paint", {}).get("displayValue", "N/A"),
            "cls": audits.get("cumulative-layout-shift", {}).get("displayValue", "N/A"),
            "tbt": audits.get("total-blocking-time", {}).get("displayValue", "N/A"),
        }
    except Exception as e:
        print(f"PageSpeed 取得失敗: {e}")
        return None


def fetch_pagespeed(target_url: str = SC_SITE_URL, samples: int = PAGESPEED_SAMPLES) -> dict:
    """PageSpeed Insights から Core Web Vitals を取得（PAGESPEED_API_KEY 任意）。

    Lighthouse の計測ブレ対策として samples 回計測し、Performance スコアの
    中央値の回をそのまま返す（LCP/CLS/TBT もその回の値で整合）。
    全て失敗した場合は従来どおりのフォールバック値を返す。
    """
    results = [
        r for r in (_fetch_pagespeed_once(target_url, cache_buster=str(i)) for i in range(samples))
        if r is not None
    ]
    if not results:
        return {"performance": 0, "lcp": "N/A", "cls": "N/A", "tbt": "N/A"}

    results.sort(key=lambda r: r["performance"])
    median = results[len(results) // 2]
    if len(results) > 1:
        scores = [r["performance"] for r in results]
        print(f"  PageSpeed {len(results)}回計測 scores={scores} → 中央値 {median['performance']} を採用")
    return median



def parse_summary(data: dict) -> tuple[dict, dict]:
    rows = data["summary"].get("rows", [])
    if not rows:
        return {}, {}

    def parse_row(row):
        mv = row["metricValues"]
        return {
            "sessions":   int(mv[0]["value"]),
            "users":      int(mv[1]["value"]),
            "newUsers":   int(mv[2]["value"]),
            "pageviews":  int(mv[3]["value"]),
            "bounceRate": round(float(mv[4]["value"]) * 100, 1),
            "avgDuration": round(float(mv[5]["value"])),
        }

    has_dim = "dimensionValues" in rows[0]
    if has_dim:
        today_rows = [r for r in rows if r["dimensionValues"][0]["value"] == "today"]
        prev_rows  = [r for r in rows if r["dimensionValues"][0]["value"] == "prev"]
        this_week = parse_row(today_rows[0]) if today_rows else {}
        prev_week = parse_row(prev_rows[0])  if prev_rows  else {}
    else:
        this_week = parse_row(rows[0]) if len(rows) >= 1 else {}
        prev_week = parse_row(rows[1]) if len(rows) >= 2 else {}

    return this_week, prev_week


def build_insight_prompt(
    data: dict,
    this_week: dict,
    prev_week: dict,
    sc_data: dict,
    psi_data: dict,
    data_deliverus: dict = None,
    psi_data_deliverus: dict = None,
) -> str:
    """課題・ネクストアクション生成用のプロンプト文字列を返す（旧 generate_insights のプロンプト部分）。

    実際の生成は Cowork 側の Claude が行い、結果 JSON を build サブコマンドに渡す。
    プロンプト本文（数値厳守・速度施策禁止などのルール）は旧版から変えていない。
    """

    # GA4データをテキスト化
    channel_text = ""
    for row in data["channels"].get("rows", []):
        ch  = row["dimensionValues"][0]["value"]
        s   = row["metricValues"][0]["value"]
        br  = round(float(row["metricValues"][1]["value"]) * 100, 1)
        dur = round(float(row["metricValues"][2]["value"]))
        channel_text += f"  - {ch}: sessions={s}, 直帰率={br}%, 平均滞在={dur}秒\n"

    page_text = ""
    for row in data["pages"].get("rows", []):
        path = row["dimensionValues"][0]["value"]
        s    = row["metricValues"][0]["value"]
        page_text += f"  - {path}: {s} sessions\n"

    device_text = ""
    for row in data["devices"].get("rows", []):
        dev = row["dimensionValues"][0]["value"]
        s   = row["metricValues"][0]["value"]
        device_text += f"  - {dev}: {s}\n"

    def chg(now, prev):
        if not prev:
            return "前週データなし"
        d = now - prev
        pct = round(d / prev * 100)
        return f"{'+' if d >= 0 else ''}{d} ({'+' if pct >= 0 else ''}{pct}%)"

    # Search Console テキスト化（メール描画と同じ 日本/海外 集約を使う）
    sc_text = ""
    for imp, query, region, ctr, pos in select_sc_display_rows(aggregate_sc_by_region(sc_data)):
        sc_text += f"  - {query}【{region}】: 表示={int(imp)}, CTR={ctr}%, 順位={pos}\n"

    # PageSpeed テキスト化
    psi_text = (
        f"  - パフォーマンス: {psi_data.get('performance', 'N/A')}/100\n"
        f"  - LCP: {psi_data.get('lcp', 'N/A')}\n"
        f"  - CLS: {psi_data.get('cls', 'N/A')}\n"
        f"  - TBT: {psi_data.get('tbt', 'N/A')}\n"
    )

    # deliverus テキスト化
    deliverus_text = ""
    if data_deliverus:
        dlv_this, dlv_prev = parse_summary(data_deliverus)
        deliverus_text += (
            f"  - セッション数: {dlv_this.get('sessions', 0)}"
            f" ({chg(dlv_this.get('sessions',0), dlv_prev.get('sessions',0))})\n"
            f"  - アクティブユーザー: {dlv_this.get('users', 0)} ({chg(dlv_this.get('users',0), dlv_prev.get('users',0))})\n"
            f"  - 新規ユーザー: {dlv_this.get('newUsers', 0)}\n"
            f"  - ページビュー: {dlv_this.get('pageviews', 0)}\n"
            f"  - 直帰率: {dlv_this.get('bounceRate', 0)}%\n"
            f"  - 平均滞在時間: {dlv_this.get('avgDuration', 0)}秒\n"
        )
        deliverus_text += "  【流入チャネル】\n"
        for row in data_deliverus["channels"].get("rows", []):
            ch  = row["dimensionValues"][0]["value"]
            s   = row["metricValues"][0]["value"]
            br  = round(float(row["metricValues"][1]["value"]) * 100, 1)
            dur = round(float(row["metricValues"][2]["value"]))
            deliverus_text += f"    - {ch}: sessions={s}, 直帰率={br}%, 平均滞在={dur}秒\n"
        deliverus_text += "  【人気ページ】\n"
        for row in data_deliverus["pages"].get("rows", []):
            path = row["dimensionValues"][0]["value"]
            s    = row["metricValues"][0]["value"]
            deliverus_text += f"    - {path}: {s} sessions\n"
        deliverus_text += "  【デバイス】\n"
        for row in data_deliverus["devices"].get("rows", []):
            dev = row["dimensionValues"][0]["value"]
            s   = row["metricValues"][0]["value"]
            deliverus_text += f"    - {dev}: {s}\n"
        if psi_data_deliverus:
            deliverus_text += (
                f"  【PageSpeed Mobile】\n"
                f"    - パフォーマンス: {psi_data_deliverus.get('performance', 'N/A')}/100\n"
                f"    - LCP: {psi_data_deliverus.get('lcp', 'N/A')}\n"
                f"    - CLS: {psi_data_deliverus.get('cls', 'N/A')}\n"
                f"    - TBT: {psi_data_deliverus.get('tbt', 'N/A')}\n"
            )

    prompt = f"""あなたは株式会社テンタス（Webマーケティング・DX支援）のWebアナリストです。
今週のGA4データを分析し、月曜全体会議で使う課題・ネクストアクションをJSON形式で出力してください。

## 今週のGA4データ（{data['period']['start']} 〜 {data['period']['end']}）

### サマリー
- セッション数: {this_week.get('sessions', 0)} ({chg(this_week.get('sessions',0), prev_week.get('sessions',0))})
- アクティブユーザー: {this_week.get('users', 0)} ({chg(this_week.get('users',0), prev_week.get('users',0))})
- 新規ユーザー: {this_week.get('newUsers', 0)} ({chg(this_week.get('newUsers',0), prev_week.get('newUsers',0))})
- ページビュー: {this_week.get('pageviews', 0)} ({chg(this_week.get('pageviews',0), prev_week.get('pageviews',0))})
- 直帰率: {this_week.get('bounceRate', 0)}% ({chg(this_week.get('bounceRate',0), prev_week.get('bounceRate',0))})
- 平均滞在時間: {this_week.get('avgDuration', 0)}秒

### 流入チャネル
{channel_text}
### 人気ページ
{page_text}
### デバイス
{device_text}

### Search Console（上位クエリ）
{sc_text or '  データなし'}

### Core Web Vitals（PageSpeed Insights）
{psi_text}

### deliverus.tentus.jp（サービスサイト）
{deliverus_text or '  データなし'}

## テンタスについて
- コーポレートサイト（tentus.jp）リニューアル完了
- 新サービス「deliverus（デリバラス）」（準委任型マーケティングオペレーション支援）を展開中
- 新規顧客3社獲得が目標（アウトバウンド・パートナー提携の2本柱）

## サイト技術状況（対応済みの施策・現在の測定値は上記データを参照）
- 両サイトにモバイル最適化を実施済み（フォントサブセット化・画像WebP化・form-mailer の遅延読み込み）
- deliverus GA4クロスドメイン計測: 設定済み（tentus.jp・deliverus.tentus.jp 間のリンカー設定完了）
- deliverus タイトル・description: 「デリバラス（Deliverus）」カタカナ表記追加済み
- 表示速度（LCP・FCP）改善プロジェクトは 2026-08-04 に打ち切り済み。フォントpreload・unicode-range先読み・content-visibility 等の4施策をすべて実施したが LCP・FCP は1msも動かず、実ユーザーデータ（CrUX）も両サイトともトラフィック不足で取得できないため、検証手段がないと判断して停止した。流入獲得を優先する方針

## 既知の継続課題（数値は上記データの実測値のみを使うこと）
- Direct流入の直帰率・滞在時間が低位（モバイル主体のため社内IP除外困難、Organic流入の強化で補う方針）
- deliverus 1枚紹介資料（PDF）未作成
- ブランドクエリのCTRが低位（Search Consoleで表示はあるがクリックが少ない）
- 検索意図別ランディングページ未整備（「ホームページ制作 伴走」などの流入ページが不足）

## 出力形式（JSON厳守）
{{
  "focus": "今週最も注目すべき1文（全体会議のオープニングで読み上げる）",
  "issues": [
    {{"priority": "P0|P1|P2", "title": "課題タイトル", "detail": "具体的な説明（1〜2文）"}}
  ],
  "this_week": [
    {{"done": false, "task": "今週中に完了すべきタスク", "owner": "担当者イメージ"}}
  ],
  "this_month": [
    {{"task": "今月中に完了すべきタスク", "owner": "担当者イメージ", "effect": "期待効果"}}
  ],
  "roadmap": [
    {{"phase": "1〜2ヶ月後|2〜3ヶ月後", "milestone": "マイルストーン", "detail": "内容（1文）"}}
  ],
  "actions": [
    {{"priority": "P1|P2|P3", "action": "具体的なアクション", "owner": "担当者イメージ", "effect": "期待効果"}}
  ]
}}

issues 3〜5件、this_week 3〜5件、this_month 3〜5件、roadmap 3〜4件、actions 3〜5件。
データと既知課題から読み取れる事実に基づいて生成してください。

## 数値の扱い（厳守）
- 出力に書く数値は、上記データセクションに記載された値のみを使うこと。記憶や推測で数値を補ってはならない。
- 「〜と報告されているが」のように、データに無い値を引き合いに出して矛盾を指摘してはならない。
- Search Console のクエリは【日本】と【海外】が別行で与えられる。英字"tentus"の【海外】行はグローバル検索由来のノイズを含むため、日本の指名検索の評価に混ぜないこと。
- PageSpeed のパフォーマンススコア・LCP・FCP は参考値として扱い、**表示速度の改善施策を issues / this_week / this_month / actions / roadmap のいずれにも含めてはならない**（上記のとおり 2026-08-04 に打ち切り済みのため、再提案は無効な指示になる）。TBT・転送量など速度以外の技術指標は対象外ではないが、LCP・FCP の短縮を目的とするタスクは書かないこと。"""

    return prompt


def arrow(now: float, prev: float, higher_is_better: bool = True) -> str:
    if not prev:
        return ""
    delta = now - prev
    if abs(delta) < 0.01 * abs(prev):
        return "<span style='color:#888'>→</span>"
    good = (delta > 0) == higher_is_better
    color = "#2e7d32" if good else "#c62828"
    symbol = "▲" if delta > 0 else "▼"
    pct = abs(round(delta / prev * 100))
    return f"<span style='color:{color};font-size:12px'>{symbol}{pct}%</span>"


# ── 接点行動量ガード（CRITICAL）について ──────────────────
# 旧版は /tmp/touchpoint_data.json の有無と対象週の一致で送信を物理的に止めていた
# （check_touchpoint_guard）。Cowork 版では Notion を直接照会するため、同等のガードは
# 定期タスクの手順として実装する:
#   ① Notion DB②（週次活動量）に対象週 start_date の行があるか確認
#   ② 無い／取得失敗なら、送信せずチャットで報告して人の判断を待つ
#   ③ 接点セクションを省いて送るのは、その週に明示的な指示があった場合のみ
# build サブコマンドは --touchpoint の week.start が対象週と一致しない場合エラー終了する。
# ──────────────────────────────────────────────────────


AXIS_LABELS = {
    "A": "軸A 既存業界の若手",
    "B": "軸B 公的機関・国際機関",
    "C": "軸C 紹介リファラル",
}


def build_touchpoint_section(tp) -> str:
    """接点行動量セクションの HTML を生成。tp が None/空なら空文字列を返す（後方互換）。"""
    if not tp:
        return ""

    activity     = tp.get("activity", {}) or {}
    touch_counts = tp.get("touch_counts", {}) or {}
    pipeline     = tp.get("pipeline", []) or []
    actions      = tp.get("actions", {}) or {}

    # ── 軸別の行動量集計 ──
    axis_blocks = ""
    for axis in ("A", "B", "C"):
        items = activity.get(axis, {}) or {}
        count = touch_counts.get(axis)
        note  = actions.get(axis)
        if not items and count is None and not note:
            continue

        metric_html = ""
        for label, value in items.items():
            if isinstance(value, bool):
                disp = "あり" if value else "なし"
            elif value is None:
                disp = "—"
            else:
                disp = str(value)
            metric_html += (
                '<span style="display:inline-block;margin:0 14px 6px 0;font-size:12px;color:#555">'
                f'{html.escape(str(label))}: <strong style="color:#1a1a1a">{html.escape(disp)}</strong></span>'
            )
        if count is not None:
            metric_html += (
                '<span style="display:inline-block;margin:0 14px 6px 0;font-size:12px;color:#555">'
                f'今週の接点件数: <strong style="color:#9e4a10">{html.escape(str(count))}</strong></span>'
            )

        action_html = ""
        if note:
            action_html = (
                '<div style="font-size:12px;color:#555;line-height:1.7;margin-top:6px;'
                'padding:8px 12px;background:#faf6f2;border-left:3px solid #e86e2a;border-radius:0 4px 4px 0">'
                f'{html.escape(str(note))}</div>'
            )

        axis_blocks += f"""
        <div style="margin-bottom:14px">
            <div style="font-size:12px;font-weight:700;color:#555;margin-bottom:6px">{html.escape(AXIS_LABELS.get(axis, axis))}</div>
            <div>{metric_html or '<span style="color:#aaa;font-size:12px">記録なし</span>'}</div>
            {action_html}
        </div>"""

    # ── パイプライン表（最終接触日の降順）──
    rows_sorted = sorted(pipeline, key=lambda r: r.get("最終接触日") or "", reverse=True)
    pipeline_rows = ""
    for row in rows_sorted:
        pipeline_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px">{html.escape(str(row.get("相手", "")))}</td>
            <td style="padding:8px 12px">{html.escape(str(row.get("軸", "")))}</td>
            <td style="padding:8px 12px">{html.escape(str(row.get("接点ソース", "")))}</td>
            <td style="padding:8px 12px">{html.escape(str(row.get("ステータス", "")))}</td>
            <td style="padding:8px 12px">{html.escape(str(row.get("最終接触日", "")))}</td>
            <td style="padding:8px 12px">{html.escape(str(row.get("次アクション", "")))}</td>
        </tr>"""

    if pipeline_rows:
        pipeline_html = f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">相手</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">軸</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">接点ソース</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">ステータス</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">最終接触日</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">次アクション</th>
      </tr></thead>
      <tbody>{pipeline_rows}</tbody>
    </table>"""
    else:
        pipeline_html = '<div style="color:#aaa;font-size:12px;padding:8px 0">アクティブな接点なし</div>'

    return f"""
  <!-- 接点行動量 -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">接点行動量</div>
    {axis_blocks}
    <div style="font-size:11px;font-weight:700;color:#888;margin:18px 0 8px;text-transform:uppercase;letter-spacing:1px">接点パイプライン</div>
    {pipeline_html}
  </div>"""


def build_html(
    start_date: str,
    end_date: str,
    data: dict,
    insights: dict,
    sc_data: dict,
    psi_data: dict,
    data_deliverus: dict = None,
    psi_data_deliverus: dict = None,
    touchpoint: dict = None,
    tender_section: str = "",
    channel_trend: list = None,
    bot_info: dict = None,
) -> str:
    this_week, prev_week = parse_summary(data)
    touchpoint_section = build_touchpoint_section(touchpoint)

    # ── AIチャネル推移セクション ──
    trend_section = ""
    if channel_trend:
        max_ai = max((w["ai"] for w in channel_trend), default=0) or 1
        trend_rows = ""
        for w in channel_trend:
            bar = int(w["ai"] / max_ai * 60)
            trend_rows += f"""<tr style="border-bottom:1px solid #f2f2f2">
          <td style="padding:6px 12px;color:#555">{w['week'][5:]}</td>
          <td style="padding:6px 12px;text-align:right;font-weight:700;color:#e86e2a">{w['ai']}</td>
          <td style="padding:6px 12px"><div style="background:#e86e2a;height:8px;width:{bar}px;border-radius:4px;min-width:2px"></div></td>
          <td style="padding:6px 12px;text-align:right;color:#555">{w['organic']}</td>
          <td style="padding:6px 12px;text-align:right;color:#999">{w['total']}</td>
        </tr>"""
        trend_section = f"""
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">AIチャネル推移（直近{len(channel_trend)}週）</div>
    <div style="font-size:11px;color:#bbb;margin-bottom:14px">AI Assistant = ChatGPT・Perplexity 等の生成AI経由の流入（AI-SEO施策のKPI）</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">週(月曜)</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">AI Assistant</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600"></th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">Organic</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">全体</th>
      </tr></thead>
      <tbody>{trend_rows}</tbody>
    </table>
  </div>
"""

    def fmt_duration(sec: int) -> str:
        if sec >= 60:
            return f"{sec // 60}分{sec % 60}秒"
        return f"{sec}秒"

    # ── サマリーカード ──
    cards = ""
    metrics = [
        ("セッション数",      this_week.get("sessions", 0),    prev_week.get("sessions", 0),    True,  ""),
        ("アクティブユーザー", this_week.get("users", 0),       prev_week.get("users", 0),       True,  ""),
        ("新規ユーザー",       this_week.get("newUsers", 0),    prev_week.get("newUsers", 0),    True,  ""),
        ("ページビュー",       this_week.get("pageviews", 0),   prev_week.get("pageviews", 0),   True,  ""),
        ("直帰率",            this_week.get("bounceRate", 0),   prev_week.get("bounceRate", 0),  False, "%"),
        ("平均滞在時間",       this_week.get("avgDuration", 0), prev_week.get("avgDuration", 0), True,  ""),
    ]
    for label, now, prev, hib, unit in metrics:
        display = fmt_duration(int(now)) if label == "平均滞在時間" else f"{now}{unit}"
        ar = arrow(now, prev, hib)
        cards += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:16px 20px;flex:1;min-width:140px">
            <div style="font-size:11px;color:#888;margin-bottom:6px">{label}</div>
            <div style="font-size:24px;font-weight:700;color:#1a1a1a">{display}</div>
            <div style="margin-top:4px;font-size:12px">{ar}</div>
        </div>"""

    # ── チャネル表 ──
    channel_rows = ""
    for row in data["channels"].get("rows", []):
        ch  = row["dimensionValues"][0]["value"]
        s   = row["metricValues"][0]["value"]
        br  = round(float(row["metricValues"][1]["value"]) * 100, 1)
        dur = round(float(row["metricValues"][2]["value"]))
        channel_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px">{ch}</td>
            <td style="padding:8px 12px;text-align:right">{s}</td>
            <td style="padding:8px 12px;text-align:right">{br}%</td>
            <td style="padding:8px 12px;text-align:right">{fmt_duration(dur)}</td>
        </tr>"""

    # ── ページ表 ──
    page_rows = ""
    for row in data["pages"].get("rows", []):
        path = row["dimensionValues"][0]["value"]
        s    = row["metricValues"][0]["value"]
        pv   = row["metricValues"][1]["value"]
        page_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px;font-family:monospace">{path}</td>
            <td style="padding:8px 12px;text-align:right">{s}</td>
            <td style="padding:8px 12px;text-align:right">{pv}</td>
        </tr>"""

    # ── デバイス ──
    device_rows = ""
    for row in data["devices"].get("rows", []):
        dev = row["dimensionValues"][0]["value"]
        s   = row["metricValues"][0]["value"]
        device_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px">{dev}</td>
            <td style="padding:8px 12px;text-align:right">{s}</td>
        </tr>"""

    # ── 国別アクセス ──
    country_rows = ""
    for row in data.get("countries", {}).get("rows", []):
        c   = row["dimensionValues"][0]["value"] or "(不明)"
        s   = row["metricValues"][0]["value"]
        u   = row["metricValues"][1]["value"]
        is_jp = c in ("Japan", "日本")
        name_color = "#1a1a1a" if is_jp else "#9e4a10"
        country_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px"><span style="color:{name_color};font-weight:600">{esc(str(c))}</span></td>
            <td style="padding:8px 12px;text-align:right">{s}</td>
            <td style="padding:8px 12px;text-align:right">{u}</td>
        </tr>"""

    # ── deliverus セクション ──
    deliverus_html = ""
    if data_deliverus:
        dlv_this, dlv_prev = parse_summary(data_deliverus)
        dlv_metrics = [
            ("セッション数",    dlv_this.get("sessions", 0),    dlv_prev.get("sessions", 0),    True,  ""),
            ("ユーザー",        dlv_this.get("users", 0),       dlv_prev.get("users", 0),       True,  ""),
            ("新規ユーザー",    dlv_this.get("newUsers", 0),    dlv_prev.get("newUsers", 0),    True,  ""),
            ("ページビュー",    dlv_this.get("pageviews", 0),   dlv_prev.get("pageviews", 0),   True,  ""),
            ("直帰率",          dlv_this.get("bounceRate", 0),  dlv_prev.get("bounceRate", 0),  False, "%"),
            ("平均滞在時間",    dlv_this.get("avgDuration", 0), dlv_prev.get("avgDuration", 0), True,  ""),
        ]
        dlv_cards = ""
        for label, now, prev, hib, unit in dlv_metrics:
            display = fmt_duration(int(now)) if label == "平均滞在時間" else f"{now}{unit}"
            ar = arrow(now, prev, hib)
            dlv_cards += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:16px 20px;flex:1;min-width:140px">
            <div style="font-size:11px;color:#888;margin-bottom:6px">{label}</div>
            <div style="font-size:24px;font-weight:700;color:#1a1a1a">{display}</div>
            <div style="margin-top:4px;font-size:12px">{ar}</div>
        </div>"""

        dlv_channel_rows = ""
        for row in data_deliverus["channels"].get("rows", []):
            ch  = row["dimensionValues"][0]["value"]
            s   = row["metricValues"][0]["value"]
            br  = round(float(row["metricValues"][1]["value"]) * 100, 1)
            dur = round(float(row["metricValues"][2]["value"]))
            dlv_channel_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px">{ch}</td>
            <td style="padding:8px 12px;text-align:right">{s}</td>
            <td style="padding:8px 12px;text-align:right">{br}%</td>
            <td style="padding:8px 12px;text-align:right">{fmt_duration(dur)}</td>
        </tr>"""

        dlv_page_rows = ""
        for row in data_deliverus["pages"].get("rows", []):
            path = row["dimensionValues"][0]["value"]
            s    = row["metricValues"][0]["value"]
            pv   = row["metricValues"][1]["value"]
            dlv_page_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px;font-family:monospace">{path}</td>
            <td style="padding:8px 12px;text-align:right">{s}</td>
            <td style="padding:8px 12px;text-align:right">{pv}</td>
        </tr>"""

        dlv_device_rows = ""
        for row in data_deliverus["devices"].get("rows", []):
            dev = row["dimensionValues"][0]["value"]
            s   = row["metricValues"][0]["value"]
            dlv_device_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px">{dev}</td>
            <td style="padding:8px 12px;text-align:right">{s}</td>
        </tr>"""

        dlv_psi_html = ""
        if psi_data_deliverus:
            dlv_score = psi_data_deliverus.get("performance", 0)
            dlv_color = "#2e7d32" if dlv_score >= 90 else "#e65100" if dlv_score >= 50 else "#c62828"
            dlv_psi_html = f"""
    <div style="margin-top:20px">
      <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px">Core Web Vitals（PageSpeed Insights / Mobile）</div>
      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <div style="text-align:center;padding:16px 24px;background:{dlv_color};border-radius:8px;min-width:80px">
          <div style="color:#fff;font-size:32px;font-weight:700">{dlv_score}</div>
          <div style="color:rgba(255,255,255,0.8);font-size:11px">Performance</div>
        </div>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <div style="background:#f9f9f9;border:1px solid #e0e0e0;border-radius:6px;padding:10px 16px;min-width:100px">
            <div style="font-size:10px;color:#888;margin-bottom:4px">LCP</div>
            <div style="font-size:16px;font-weight:700;color:#1a1a1a">{psi_data_deliverus.get('lcp','N/A')}</div>
          </div>
          <div style="background:#f9f9f9;border:1px solid #e0e0e0;border-radius:6px;padding:10px 16px;min-width:100px">
            <div style="font-size:10px;color:#888;margin-bottom:4px">CLS</div>
            <div style="font-size:16px;font-weight:700;color:#1a1a1a">{psi_data_deliverus.get('cls','N/A')}</div>
          </div>
          <div style="background:#f9f9f9;border:1px solid #e0e0e0;border-radius:6px;padding:10px 16px;min-width:100px">
            <div style="font-size:10px;color:#888;margin-bottom:4px">TBT</div>
            <div style="font-size:16px;font-weight:700;color:#1a1a1a">{psi_data_deliverus.get('tbt','N/A')}</div>
          </div>
        </div>
      </div>
    </div>"""

        deliverus_html = f"""
  <!-- deliverus -->
  <div style="background:#1a1a1a;padding:16px 32px;margin-top:8px">
    <div style="color:#e86e2a;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase">deliverus.tentus.jp</div>
    <div style="color:#fff;font-size:16px;font-weight:700;margin-top:4px">deliverus アクセス週次レポート</div>
  </div>
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">アクセスサマリー</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px">{dlv_cards}</div>
    <div style="margin-top:10px;font-size:11px;color:#bbb">▲▼ は前週比</div>
  </div>
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">流入チャネル</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">チャネル</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">セッション</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">直帰率</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">平均滞在</th>
      </tr></thead>
      <tbody>{dlv_channel_rows}</tbody>
    </table>
  </div>
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">人気ページ Top 5</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">ページ</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">セッション</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">PV</th>
      </tr></thead>
      <tbody>{dlv_page_rows}</tbody>
    </table>
  </div>
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">デバイス</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">デバイス</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">セッション</th>
      </tr></thead>
      <tbody>{dlv_device_rows}</tbody>
    </table>
    {dlv_psi_html}
  </div>"""

    # ── Search Console（query×country を 日本/海外 に集約して併記）──
    sc_items = select_sc_display_rows(aggregate_sc_by_region(sc_data))

    sc_rows = ""
    for imp, q, region, ctr, pos in sc_items:
        region_color = "#1a1a1a" if region == "日本" else "#9e4a10"
        sc_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:8px 12px">{esc(str(q))}</td>
            <td style="padding:8px 12px"><span style="color:{region_color};font-weight:600">{region}</span></td>
            <td style="padding:8px 12px;text-align:right">{int(imp)}</td>
            <td style="padding:8px 12px;text-align:right">{ctr}%</td>
            <td style="padding:8px 12px;text-align:right">{pos}</td>
        </tr>"""

    # ── PageSpeed ──
    psi_score = psi_data.get("performance", 0)
    psi_color = "#2e7d32" if psi_score >= 90 else "#e65100" if psi_score >= 50 else "#c62828"
    psi_html = f"""
    <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <div style="text-align:center;padding:16px 24px;background:{psi_color};border-radius:8px;min-width:80px">
            <div style="color:#fff;font-size:32px;font-weight:700">{psi_score}</div>
            <div style="color:rgba(255,255,255,0.8);font-size:11px">Performance</div>
        </div>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
            <div style="background:#f9f9f9;border:1px solid #e0e0e0;border-radius:6px;padding:10px 16px;min-width:100px">
                <div style="font-size:10px;color:#888;margin-bottom:4px">LCP</div>
                <div style="font-size:16px;font-weight:700;color:#1a1a1a">{psi_data.get('lcp','N/A')}</div>
            </div>
            <div style="background:#f9f9f9;border:1px solid #e0e0e0;border-radius:6px;padding:10px 16px;min-width:100px">
                <div style="font-size:10px;color:#888;margin-bottom:4px">CLS</div>
                <div style="font-size:16px;font-weight:700;color:#1a1a1a">{psi_data.get('cls','N/A')}</div>
            </div>
            <div style="background:#f9f9f9;border:1px solid #e0e0e0;border-radius:6px;padding:10px 16px;min-width:100px">
                <div style="font-size:10px;color:#888;margin-bottom:4px">TBT</div>
                <div style="font-size:16px;font-weight:700;color:#1a1a1a">{psi_data.get('tbt','N/A')}</div>
            </div>
        </div>
    </div>"""

    # ── 課題セクション ──
    priority_color = {"P0": "#c62828", "P1": "#e65100", "P2": "#1565c0"}
    issue_items = ""
    for issue in insights.get("issues", []):
        color = priority_color.get(issue.get("priority", ""), "#555")
        issue_items += f"""
        <div style="border-left:3px solid {color};padding:10px 16px;margin-bottom:10px;background:#fafafa;border-radius:0 6px 6px 0">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                <span style="background:{color};color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px">{issue.get("priority", "")}</span>
                <span style="font-weight:700;font-size:13px;color:#1a1a1a">{issue.get("title", "")}</span>
            </div>
            <div style="font-size:12px;color:#555;line-height:1.6">{issue.get("detail", "")}</div>
        </div>"""

    # ── 今週中タスク ──
    this_week_items = ""
    for item in insights.get("this_week", []):
        check = "☐"
        this_week_items += f"""
        <div style="display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-bottom:1px solid #f0f0f0">
            <span style="font-size:16px;color:#e86e2a;flex-shrink:0">{check}</span>
            <div style="flex:1">
                <span style="font-size:13px;font-weight:600;color:#1a1a1a">{item.get("task", "")}</span>
                <span style="font-size:11px;color:#aaa;margin-left:8px">{item.get("owner", "")}</span>
            </div>
        </div>"""

    # ── 今月中タスク表 ──
    this_month_rows = ""
    for item in insights.get("this_month", []):
        this_month_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:10px 12px;font-size:13px;font-weight:600;color:#1a1a1a">{item.get("task", "")}</td>
            <td style="padding:10px 12px;font-size:12px;color:#555">{item.get("owner", "")}</td>
            <td style="padding:10px 12px;font-size:12px;color:#555">{item.get("effect", "")}</td>
        </tr>"""

    # ── ロードマップ ──
    phase_color = {"1〜2ヶ月後": "#1565c0", "2〜3ヶ月後": "#6a1b9a"}
    roadmap_items = ""
    for item in insights.get("roadmap", []):
        color = phase_color.get(item.get("phase", ""), "#555")
        roadmap_items += f"""
        <div style="display:flex;gap:16px;padding:12px 0;border-bottom:1px solid #f0f0f0">
            <div style="flex-shrink:0;width:90px">
                <span style="background:{color};color:#fff;font-size:10px;font-weight:700;padding:3px 8px;border-radius:3px;white-space:nowrap">{item.get("phase","")}</span>
            </div>
            <div>
                <div style="font-size:13px;font-weight:700;color:#1a1a1a;margin-bottom:3px">{item.get("milestone", "")}</div>
                <div style="font-size:12px;color:#777">{item.get("detail", "")}</div>
            </div>
        </div>"""

    # ── ネクストアクション表 ──
    action_rows = ""
    for act in insights.get("actions", []):
        color = priority_color.get(act.get("priority", ""), "#555")
        action_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:10px 12px">
                <span style="background:{color};color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px">{act.get("priority", "")}</span>
            </td>
            <td style="padding:10px 12px;font-size:13px;font-weight:600;color:#1a1a1a">{act.get("action", "")}</td>
            <td style="padding:10px 12px;font-size:12px;color:#555">{act.get("owner", "")}</td>
            <td style="padding:10px 12px;font-size:12px;color:#555">{act.get("effect", "")}</td>
        </tr>"""

    # 除外量を明示する。黙って数字を変えると過去週と比べたときに原因不明の段差になる。
    bot_note = ""
    if bot_info and bot_info.get("cohorts"):
        bot_note = (f"　/　ボット推定を除外済み"
                    f"（{bot_info['cohorts']}コホート・直近{bot_info.get('weeks', 9)}週で"
                    f"{bot_info['sessions']}セッション）")

    focus = insights.get("focus", "")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Helvetica Neue',Arial,sans-serif">
<div style="max-width:680px;margin:32px auto;background:#f5f5f5">

  <!-- ヘッダー -->
  <div style="background:#1a1a1a;border-radius:10px 10px 0 0;padding:24px 32px">
    <div style="color:#e86e2a;font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase">Weekly Report</div>
    <div style="color:#fff;font-size:22px;font-weight:700;margin-top:6px">tentus.jp アクセス週次レポート</div>
    <div style="color:#aaa;font-size:13px;margin-top:4px">{start_date} 〜 {end_date}</div>
  </div>

  <!-- 今週のフォーカス -->
  <div style="background:#e86e2a;padding:16px 32px">
    <div style="font-size:11px;color:rgba(255,255,255,0.7);font-weight:700;letter-spacing:1px;margin-bottom:6px">📌 今週のフォーカス</div>
    <div style="color:#fff;font-size:15px;font-weight:600;line-height:1.5">{focus}</div>
  </div>

  <!-- サマリーカード -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">アクセスサマリー</div>
    <div style="display:flex;flex-wrap:wrap;gap:10px">{cards}</div>
    <div style="margin-top:10px;font-size:11px;color:#bbb">▲▼ は前週比{bot_note}</div>
  </div>

  <!-- チャネル別 -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">流入チャネル</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">チャネル</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">セッション</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">直帰率</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">平均滞在</th>
      </tr></thead>
      <tbody>{channel_rows}</tbody>
    </table>
  </div>
{trend_section}
  <!-- ページ別 -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">人気ページ Top 5</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">ページ</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">セッション</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">PV</th>
      </tr></thead>
      <tbody>{page_rows}</tbody>
    </table>
  </div>

  <!-- デバイス -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">デバイス</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">デバイス</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">セッション</th>
      </tr></thead>
      <tbody>{device_rows}</tbody>
    </table>
  </div>

  <!-- 国別アクセス -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">国別アクセス Top 6</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">国</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">セッション</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">アクティブユーザー</th>
      </tr></thead>
      <tbody>{country_rows}</tbody>
    </table>
  </div>

  {deliverus_html}

  <!-- Search Console -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">検索クエリ（Search Console・日本/海外別）</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">クエリ</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">国</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">表示回数</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">CTR</th>
        <th style="padding:8px 12px;text-align:right;color:#888;font-weight:600">順位</th>
      </tr></thead>
      <tbody>{sc_rows}</tbody>
    </table>
    <div style="margin-top:8px;font-size:11px;color:#bbb">国は日本/海外で集約。海外は英字"tentus"等のグローバル検索を含む（集客拡大の余地）。</div>
  </div>

  <!-- PageSpeed -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">Core Web Vitals（PageSpeed Insights / Mobile）</div>
    {psi_html}
  </div>

  {touchpoint_section}

  {tender_section}

  <!-- 今週中 -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">✅ 今週中に完了すること</div>
    {this_week_items}
  </div>

  <!-- 今月中 -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">📅 今月中に完了すること</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">タスク</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">担当</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">期待効果</th>
      </tr></thead>
      <tbody>{this_month_rows}</tbody>
    </table>
  </div>

  <!-- ロードマップ -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">🗺️ ロードマップ</div>
    {roadmap_items}
  </div>

  <!-- 現状課題 -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">現状課題</div>
    {issue_items}
  </div>

  <!-- ネクストアクション -->
  <div style="background:#fff;padding:24px 32px;border-left:1px solid #e0e0e0;border-right:1px solid #e0e0e0;border-top:1px solid #f0f0f0">
    <div style="font-size:11px;font-weight:700;color:#888;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">ネクストアクション</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9f9f9">
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">優先度</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">アクション</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">担当</th>
        <th style="padding:8px 12px;text-align:left;color:#888;font-weight:600">期待効果</th>
      </tr></thead>
      <tbody>{action_rows}</tbody>
    </table>
  </div>

  <!-- フッター -->
  <div style="background:#1a1a1a;border-radius:0 0 10px 10px;padding:20px 32px;text-align:center">
    <div style="color:#666;font-size:11px">このメールは自動送信です &nbsp;|&nbsp; tentus.jp Weekly Report &nbsp;|&nbsp; Powered by Claude</div>
  </div>

</div>
</body>
</html>"""
    return html


def get_google_access_token() -> str:
    """保存済み OAuth トークン（refresh_token 付き）からアクセストークンを取得する。

    旧版は google-auth の Credentials で更新していたが、依存を減らすため
    OAuth2 トークンエンドポイントを直接叩く。refresh_token は失効しない限り使い回せる。
    """
    with open(GOOGLE_TOKEN_FILE, encoding="utf-8") as f:
        data = json.load(f)
    resp = requests.post(
        data.get("token_uri", "https://oauth2.googleapis.com/token"),
        data={
            "client_id":     data["client_id"],
            "client_secret": data["client_secret"],
            "refresh_token": data["refresh_token"],
            "grant_type":    "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ═══════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════

def cmd_fetch(args) -> int:
    start_date, end_date = get_week_range(args.week_start)
    print(f"対象期間: {start_date} 〜 {end_date}")

    # ボット判定は推移表と同じ窓（直近9週）で行い、週次サマリーにも同じ除外を
    # 適用する。窓を分けると推移表と今週の数字が食い違う。
    trend_weeks = 8
    print("ボット判定中（直近9週の country×browser×OS）...")
    bot_cohorts   = fetch_bot_cohorts(f"{trend_weeks * 7 + 7}daysAgo", "yesterday")
    bot_filter    = build_bot_exclusion_filter(bot_cohorts)
    bot_sessions  = sum(c["sessions"] for c in bot_cohorts)
    if bot_cohorts:
        print(f"  ボット推定 {len(bot_cohorts)}コホート / 直近9週 {bot_sessions}セッションを除外")
        for c in sorted(bot_cohorts, key=lambda c: -c["sessions"])[:5]:
            print(f"    - {c['country']} / {c['browser']} / {c['operatingSystem']}: {c['sessions']}")
    else:
        print("  ボット推定コホートなし")
    bot_info = {"cohorts": len(bot_cohorts), "sessions": bot_sessions, "weeks": trend_weeks + 1}

    print("GA4 データ取得中（tentus.jp）...")
    data = fetch_ga4(start_date, end_date, bot_filter=bot_filter)

    print("GA4 データ取得中（deliverus.tentus.jp）...")
    data_deliverus = fetch_ga4(start_date, end_date, hostname_filter="deliverus.tentus.jp",
                               bot_filter=bot_filter)

    print(f"AIチャネル推移（直近{trend_weeks}週）取得中...")
    channel_trend = fetch_channel_trend(weeks=trend_weeks, bot_filter=bot_filter)

    this_week, prev_week = parse_summary(data)

    print("Search Console データ取得中...")
    sc_data = fetch_search_console(start_date, end_date)

    print("PageSpeed Insights 取得中（tentus.jp）...")
    psi_data = fetch_pagespeed()

    print("PageSpeed Insights 取得中（deliverus.tentus.jp）...")
    psi_data_deliverus = fetch_pagespeed("https://deliverus.tentus.jp/")

    print("公募ウォッチ（EU/TED）取得中...")
    since = start_date.replace("-", "")[:4] + "0101"  # 当年1月1日 'YYYY0101'
    tenders, tenders_ok = tender_watch.collect(since)
    print(f"  公募ウォッチ: {len(tenders)}件（取得{'成功' if tenders_ok else '失敗'}）")

    insight_prompt = build_insight_prompt(
        data, this_week, prev_week, sc_data, psi_data,
        data_deliverus=data_deliverus,
        psi_data_deliverus=psi_data_deliverus,
    )

    out = {
        "week": {"start": start_date, "end": end_date},
        "bot_info": bot_info,
        "data": data,
        "data_deliverus": data_deliverus,
        "channel_trend": channel_trend,
        "sc_data": sc_data,
        "psi_data": psi_data,
        "psi_data_deliverus": psi_data_deliverus,
        "tenders": tenders,
        "tenders_ok": tenders_ok,
        "insight_prompt": insight_prompt,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✓ 保存: {args.out}")

    # Cowork 側が読みやすいように、翻訳対象の公募タイトルも別出力
    if args.tender_titles_out:
        titles = {str(t["id"]): t.get("title") or "" for t in tenders if t.get("id")}
        with open(args.tender_titles_out, "w", encoding="utf-8") as f:
            json.dump(titles, f, ensure_ascii=False, indent=2)
        print(f"✓ 公募タイトル（翻訳用）: {args.tender_titles_out}（{len(titles)}件）")
    if args.prompt_out:
        with open(args.prompt_out, "w", encoding="utf-8") as f:
            f.write(insight_prompt)
        print(f"✓ インサイト生成プロンプト: {args.prompt_out}")
    return 0


REQUIRED_INSIGHT_KEYS = ("focus", "issues", "this_week", "this_month", "roadmap", "actions")


def cmd_build(args) -> int:
    with open(args.data, encoding="utf-8") as f:
        d = json.load(f)
    with open(args.insights, encoding="utf-8") as f:
        insights = json.load(f)
    missing = [k for k in REQUIRED_INSIGHT_KEYS if k not in insights]
    if missing:
        print(f"✗ insights に必須キーがありません: {missing}")
        return 1

    start_date, end_date = d["week"]["start"], d["week"]["end"]

    touchpoint = None
    if args.touchpoint:
        with open(args.touchpoint, encoding="utf-8") as f:
            touchpoint = json.load(f)
        tp_week = (touchpoint.get("week") or {}).get("start")
        if tp_week != start_date:
            print(f"✗ 接点データの対象週が不一致です（touchpoint: {tp_week} / 対象週: {start_date}）")
            return 1

    translations = None
    if args.translations:
        with open(args.translations, encoding="utf-8") as f:
            translations = json.load(f)
    tenders = tender_watch.apply_translations(d["tenders"], translations)
    tender_section = tender_watch.build_tender_section(tenders, d["tenders_ok"])

    report_html = build_html(
        start_date, end_date, d["data"], insights, d["sc_data"], d["psi_data"],
        data_deliverus=d["data_deliverus"],
        psi_data_deliverus=d["psi_data_deliverus"],
        touchpoint=touchpoint,
        tender_section=tender_section,
        channel_trend=d["channel_trend"],
        bot_info=d["bot_info"],
    )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report_html)
    print(f"✓ HTML 生成: {args.out}（接点セクション: {'あり' if touchpoint else 'なし'}）")
    print(f"  件名: 【週次レポート】tentus.jp {start_date} 〜 {end_date}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="外部データを取得して JSON に保存")
    pf.add_argument("--out", default="data.json")
    pf.add_argument("--week-start", help="対象週の月曜 (YYYY-MM-DD)。省略時は先週")
    pf.add_argument("--tender-titles-out", default="tender_titles.json")
    pf.add_argument("--prompt-out", default="insight_prompt.txt")
    pf.set_defaults(func=cmd_fetch)

    pb = sub.add_parser("build", help="insights / 接点 / 翻訳を合流させて HTML を生成")
    pb.add_argument("--data", default="data.json")
    pb.add_argument("--insights", required=True)
    pb.add_argument("--touchpoint")
    pb.add_argument("--translations")
    pb.add_argument("--out", default="report.html")
    pb.set_defaults(func=cmd_build)

    # 未知引数は argparse がエラー終了する（旧 unknown_flags と同じ意図: 確認のつもりの実行を事故にしない）
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
