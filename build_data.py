#!/usr/bin/env python3
"""
build_data.py — キオクシア×サンディスク 予想PER 日次バッチ

eps_config.json（手動管理EPS履歴・レイヤー2）を読み込み、
yfinance でレイヤー1（株価）を取得して予想PERを計算し、data.json を更新する。

遡及書き換え禁止: timeseries は追記のみ。過去エントリは変更しない。
"""

import json
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

# =========================================================================
# 定数
# =========================================================================
SCRIPT_DIR      = Path(__file__).parent
EPS_CONFIG_PATH = SCRIPT_DIR / "eps_config.json"
DATA_PATH       = SCRIPT_DIR / "data.json"
INDEX_PATH      = SCRIPT_DIR / "index.html"

SUMMARY_START = "<!-- STATIC_SUMMARY_START -->"
SUMMARY_END   = "<!-- STATIC_SUMMARY_END -->"

JST          = timezone(timedelta(hours=9))
GRAPH_START  = date(2026, 4, 1)   # このより前のエントリは生成しない

MAX_RETRIES    = 5
RETRY_INTERVAL = 10  # 秒（リトライ間隔）

TICKERS = {
    "kioxia": "285A.T",
    "sndk":   "SNDK",
}

# =========================================================================
# ユーティリティ
# =========================================================================

def now_jst() -> datetime:
    return datetime.now(JST)


def to_iso_jst(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# =========================================================================
# index.html 静的サマリー更新
# =========================================================================

def update_html_summary(
    today_str: str,
    k_per: float | None,
    s_per: float | None,
    timeseries: list,
) -> None:
    """index.html の STATIC_SUMMARY ブロックを最新値で書き換える。"""
    if not INDEX_PATH.exists():
        print("[WARN] index.html が見つかりません。静的サマリー更新をスキップ。")
        return

    k_pers = [e["kioxia"]["per"] for e in timeseries if (e.get("kioxia") or {}).get("per") is not None]
    s_pers = [e["sndk"]["per"]   for e in timeseries if (e.get("sndk")   or {}).get("per") is not None]

    k_str = f"{k_per:.2f}倍" if k_per is not None else "取得中"
    s_str = f"{s_per:.2f}倍" if s_per is not None else "取得中"

    cheaper = ""
    if k_per is not None and s_per is not None:
        diff = abs(k_per - s_per)
        if k_per < s_per:
            cheaper = f"現時点ではキオクシアの方が割安（PER差 {diff:.2f}倍）。"
        elif s_per < k_per:
            cheaper = f"現時点ではサンディスクの方が割安（PER差 {diff:.2f}倍）。"
        else:
            cheaper = "現時点では両社のPERは同値。"

    range_line = ""
    if k_pers and s_pers:
        range_line = (
            f"\n  <p>2026年4月以降の推移："
            f"キオクシアPERは{min(k_pers):.2f}〜{max(k_pers):.2f}倍、"
            f"サンディスクPERは{min(s_pers):.2f}〜{max(s_pers):.2f}倍の範囲で推移している。</p>"
        )

    new_block = (
        f"{SUMMARY_START}\n"
        f"<div class=\"static-summary\">\n"
        f"  <p><b>{today_str} 時点の予想PER（Run-Rate）：</b>"
        f"キオクシア {k_str}、サンディスク {s_str}。{cheaper}</p>"
        f"{range_line}\n"
        f"</div>\n"
        f"{SUMMARY_END}"
    )

    html = INDEX_PATH.read_text(encoding="utf-8")
    start_idx = html.find(SUMMARY_START)
    end_idx   = html.find(SUMMARY_END)
    if start_idx == -1 or end_idx == -1:
        print("[WARN] STATIC_SUMMARY マーカーが index.html に見つかりません。スキップ。")
        return

    new_html = html[:start_idx] + new_block + html[end_idx + len(SUMMARY_END):]
    INDEX_PATH.write_text(new_html, encoding="utf-8")
    print(f"[OK] index.html 静的サマリーを更新しました（{today_str}）。")


# =========================================================================
# EPS解決（最重要）
# =========================================================================

def resolve_eps(eps_history: list, target_date: date) -> dict | None:
    """
    eps_history から valid_from <= target_date を満たすエントリのうち
    valid_from が最大（最新）のものを返す。
    該当なし（グラフ起点より前に呼ばれた等）は None。
    """
    candidates = [
        e for e in eps_history
        if date.fromisoformat(e["valid_from"]) <= target_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["valid_from"])


# =========================================================================
# 株価取得（リトライ付き）
# =========================================================================

def fetch_close(ticker_str: str) -> tuple[float | None, str | None, float | None]:
    """
    yfinance でティッカーの直近終値・タイムスタンプ・前日比を取得する。
    MAX_RETRIES 回全滅時は (None, None, None) を返す。
    """
    for attempt in range(MAX_RETRIES):
        try:
            hist = yf.Ticker(ticker_str).history(period="10d", auto_adjust=False)
            if hist.empty:
                raise ValueError(f"empty history")
            closes = hist["Close"].dropna()
            if closes.empty:
                raise ValueError(f"all NaN closes")

            last_val = round(float(closes.iloc[-1]), 4)
            last_ts  = closes.index[-1]
            ts_dt    = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts
            as_of    = to_iso_jst(ts_dt)

            change_pct = None
            if len(closes) >= 2:
                prev_val = float(closes.iloc[-2])
                if prev_val != 0:
                    change_pct = round((last_val - prev_val) / abs(prev_val) * 100, 4)

            return last_val, as_of, change_pct

        except Exception as e:
            print(f"  [{ticker_str}] 取得失敗({attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_INTERVAL)

    return None, None, None


# =========================================================================
# PER算出
# =========================================================================

def calc_per(
    price: float | None,
    eps_entry: dict | None,
    prev_valid_from: str | None,
) -> tuple[float | None, float | None, str | None, str | None]:
    """
    (per, eps_annualized, eps_valid_from, eps_event) を返す。
    price or eps_entry が None なら 4値すべて None。

    per = price / eps_annualized  ← EPS÷price は益回りなので逆にしない。
    eps_event: valid_from が前エントリと変わった最初の営業日に立てる。
               ・土日祝でvalidフロムが始まっても正しく拾える。
               ・初回エントリ（prev=None）は線の始まりなので null。
    """
    if price is None or eps_entry is None:
        return None, None, None, None

    eps_ann    = eps_entry["eps_annualized"]
    valid_from = eps_entry["valid_from"]
    per_val    = round(price / eps_ann, 2)
    eps_event  = (
        eps_entry["type"]
        if (prev_valid_from is not None and valid_from != prev_valid_from)
        else None
    )

    return per_val, eps_ann, valid_from, eps_event


# =========================================================================
# メイン
# =========================================================================

def build_data() -> None:
    generated_at = now_jst()
    today        = generated_at.date()
    today_str    = today.isoformat()
    alerts: list[str] = []

    # グラフ起点より前は処理しない
    if today < GRAPH_START:
        print(f"[SKIP] グラフ起点({GRAPH_START})より前のため処理をスキップ。")
        return

    # 設定・既存データ読み込み
    eps_cfg   = load_json(EPS_CONFIG_PATH)
    prev_data = load_json(DATA_PATH)
    prev_timeseries: list = prev_data.get("timeseries", [])

    # べき等性チェック: 当日エントリが既に存在すればスキップ
    if any(e["date"] == today_str for e in prev_timeseries):
        print(f"[SKIP] {today_str} のエントリは既に存在します（再実行を検知）。")
        return

    # -----------------------------------------------------------------------
    # レイヤー1: 株価取得（リトライ → stale フォールバック）
    # -----------------------------------------------------------------------
    print(f"▼ 株価取得  ({today_str})")
    prev_market = prev_data.get("market_data", {})

    fetch_results: dict[str, dict] = {}
    for company_id, ticker in TICKERS.items():
        market_key = f"kioxia_price_jpy" if company_id == "kioxia" else "sndk_price_usd"

        val, as_of, chg = fetch_close(ticker)

        if val is not None:
            status = "ok"
            print(f"  {ticker:10s}  {val:>12.2f}  [ok]   {as_of}")
        else:
            # 前回値フォールバック
            prev_entry = prev_market.get(market_key, {})
            val   = prev_entry.get("value")
            as_of = prev_entry.get("as_of")   # 前回の as_of をそのまま保持
            chg   = None
            if val is not None:
                status = "stale"
                alerts.append(f"[警告] {ticker}: 株価取得失敗。前回値({val})を保持して続行。")
                print(f"  {ticker:10s}  {val:>12.2f}  [stale] ← 前回値")
            else:
                status = "failed"
                alerts.append(f"[エラー] {ticker}: 株価取得失敗かつ前回値なし。PER=null。")
                print(f"  {ticker:10s}  {'None':>12s}  [failed]")

        fetch_results[company_id] = {
            "value": val, "as_of": as_of, "change_pct": chg, "status": status,
        }

    # 両社とも取得不能 → 当日スキップ（timeseriesに null エントリを残さない）
    if fetch_results["kioxia"]["value"] is None and fetch_results["sndk"]["value"] is None:
        print("[SKIP] 両社の株価が取得不能。当日エントリを追加しません。")
        return

    # -----------------------------------------------------------------------
    # レイヤー2: EPS解決
    # -----------------------------------------------------------------------
    kioxia_cfg = eps_cfg["companies"]["kioxia"]
    sndk_cfg   = eps_cfg["companies"]["sndk"]

    kioxia_eps = resolve_eps(kioxia_cfg["eps_history"], today)
    sndk_eps   = resolve_eps(sndk_cfg["eps_history"], today)

    if kioxia_eps is None:
        alerts.append(f"[警告] キオクシア: {today_str}に有効なEPSエントリなし（eps_config.json を確認）。PER=null。")
    if sndk_eps is None:
        alerts.append(f"[警告] SNDK: {today_str}に有効なEPSエントリなし（eps_config.json を確認）。PER=null。")

    # -----------------------------------------------------------------------
    # PER算出（eps_event は前エントリとの valid_from 比較で判定）
    # -----------------------------------------------------------------------
    last_ts      = prev_timeseries[-1] if prev_timeseries else {}
    prev_k_vf    = last_ts.get("kioxia", {}).get("eps_valid_from")
    prev_s_vf    = last_ts.get("sndk",   {}).get("eps_valid_from")

    k_per, k_eps_ann, k_valid_from, k_event = calc_per(
        fetch_results["kioxia"]["value"], kioxia_eps, prev_k_vf
    )
    s_per, s_eps_ann, s_valid_from, s_event = calc_per(
        fetch_results["sndk"]["value"], sndk_eps, prev_s_vf
    )

    print(f"\n▼ PER算出")
    k_price_str = f"¥{fetch_results['kioxia']['value']}" if fetch_results["kioxia"]["value"] else "None"
    s_price_str = f"${fetch_results['sndk']['value']}"   if fetch_results["sndk"]["value"]   else "None"
    print(f"  キオクシア: {k_price_str} / ¥{k_eps_ann} = {k_per}倍   event={k_event}")
    print(f"  SNDK      : {s_price_str} / ${s_eps_ann} = {s_per}倍   event={s_event}")

    # -----------------------------------------------------------------------
    # overall_status
    # -----------------------------------------------------------------------
    statuses = [fetch_results["kioxia"]["status"], fetch_results["sndk"]["status"]]
    per_nulls = (k_per is None or s_per is None)

    if all(s == "ok" for s in statuses) and not per_nulls:
        overall_status = "complete"
    else:
        overall_status = "partial"

    # -----------------------------------------------------------------------
    # timeseriesエントリ構築（追記のみ・過去エントリには一切触れない）
    # -----------------------------------------------------------------------
    ts_entry = {
        "date": today_str,
        "kioxia": {
            "price":           fetch_results["kioxia"]["value"],
            "eps_annualized":  k_eps_ann,
            "per":             k_per,
            "eps_valid_from":  k_valid_from,
            "eps_event":       k_event,
        },
        "sndk": {
            "price":           fetch_results["sndk"]["value"],
            "eps_annualized":  s_eps_ann,
            "per":             s_per,
            "eps_valid_from":  s_valid_from,
            "eps_event":       s_event,
        },
    }

    # -----------------------------------------------------------------------
    # 出力構築
    # -----------------------------------------------------------------------
    def build_company_block(
        company_id: str, cfg: dict, fetch: dict, eps_entry: dict | None,
        per: float | None, eps_ann: float | None,
    ) -> dict:
        price_status = fetch["status"]
        if price_status == "failed" or (eps_entry is None and per is None):
            company_status = "error"
        elif price_status == "stale" or eps_entry is None:
            company_status = "partial"
        else:
            company_status = "ok"

        eps_block = {
            "eps_quarterly":   eps_entry["eps_quarterly"]   if eps_entry else None,
            "eps_annualized":  eps_entry["eps_annualized"]  if eps_entry else None,
            "valid_from":      eps_entry["valid_from"]      if eps_entry else None,
            "type":            eps_entry["type"]            if eps_entry else None,
            "guidance_format": eps_entry["guidance_format"] if eps_entry else None,
            "basis":           eps_entry["basis"]           if eps_entry else None,
            "source_label":    eps_entry["source_label"]    if eps_entry else None,
            "source_url":      eps_entry["source_url"]      if eps_entry else None,
        }

        return {
            "id":            company_id,
            "display_name":  cfg["display_name"],
            "ticker":        cfg["ticker"],
            "base_currency": cfg["base_currency"],
            "status":        company_status,
            "eps_current":   eps_block,
            "calc": {
                "price": {
                    "value":    fetch["value"],
                    "currency": cfg["base_currency"],
                    "as_of":    fetch["as_of"],
                },
                "eps_annualized": {
                    "value":    eps_ann,
                    "currency": cfg["base_currency"],
                    "formula":  "eps_quarterly × 4",
                },
                "per": {
                    "value":   per,
                    "formula": "price / eps_annualized",
                },
            },
        }

    companies_out = [
        build_company_block(
            "kioxia", kioxia_cfg, fetch_results["kioxia"],
            kioxia_eps, k_per, k_eps_ann,
        ),
        build_company_block(
            "sndk", sndk_cfg, fetch_results["sndk"],
            sndk_eps, s_per, s_eps_ann,
        ),
    ]

    # _meta: 静的フィールドは定義から、動的フィールドは今回生成値で上書き
    meta = {
        "schema_version": "1.0",
        "description":    "キオクシア×サンディスク 予想PER比較。バッチが日次生成。",
        "generated_at":   to_iso_jst(generated_at),
        "overall_status": overall_status,
        "_status_vocabulary": {
            "overall_status":  "complete=両社PER算出成功 / partial=一方の株価がstaleまたはPERがnull / failed=両社株価取得不能（当日スキップ）",
            "per_item_status": "ok=正常取得 / stale=取得失敗し前回値保持中 / failed=取得失敗かつ前回値なし",
            "company_status":  "ok=算出成功 / partial=株価staleまたはEPS未解決だが算出実行 / error=PER算出不可",
        },
        "_timeseries_note": (
            "timeseriesは日次バッチが追記のみ（遡及書き換え禁止）。"
            "各エントリは書き込み後に変更しない。"
            "eps_eventが非nullの日でグラフ線を切断し、マーカーを描画する。"
        ),
        "_timeseries_entry_schema": {
            "date": "YYYY-MM-DD",
            "kioxia": {
                "price":          "float|null — 当日東京終値（JPY）",
                "eps_annualized": "float|null — 当日有効EPS年率（JPY）= eps_quarterly×4",
                "per":            "float|null — 予想PER = price / eps_annualized",
                "eps_valid_from": "YYYY-MM-DD|null — 当日有効EPS の valid_from",
                "eps_event":      "null | 'regular_earnings' | 'guidance_revision' — EPS切替日のみ非null",
            },
            "sndk": {
                "price":          "float|null — 当日米国終値（USD）",
                "eps_annualized": "float|null — 当日有効EPS年率（USD）= eps_quarterly×4",
                "per":            "float|null — 予想PER = price / eps_annualized",
                "eps_valid_from": "YYYY-MM-DD|null — 当日有効EPS の valid_from",
                "eps_event":      "null | 'regular_earnings' | 'guidance_revision' — EPS切替日のみ非null",
            },
        },
    }

    output = {
        "_meta":       meta,
        "market_data": {
            "_comment": "レイヤー1。バッチがyfinanceから日次取得。",
            "kioxia_price_jpy": {
                "value":       fetch_results["kioxia"]["value"],
                "as_of":       fetch_results["kioxia"]["as_of"],
                "change_pct":  fetch_results["kioxia"]["change_pct"],
                "status":      fetch_results["kioxia"]["status"],
                "source":      f"yfinance:{TICKERS['kioxia']}",
                "market":      "JP",
                "price_basis": "当日東京終値",
            },
            "sndk_price_usd": {
                "value":       fetch_results["sndk"]["value"],
                "as_of":       fetch_results["sndk"]["as_of"],
                "change_pct":  fetch_results["sndk"]["change_pct"],
                "status":      fetch_results["sndk"]["status"],
                "source":      f"yfinance:{TICKERS['sndk']}",
                "market":      "US",
                "price_basis": "当日米国終値",
            },
        },
        "companies":  companies_out,
        "comparison": {
            "_comment": "PERは無次元（倍率）なので自国通貨が約分され日米直接比較可能。",
            "kioxia_per": k_per,
            "sndk_per":   s_per,
        },
        "timeseries": prev_timeseries + [ts_entry],
        "alerts": {
            "_comment": "overall_status が complete 以外のとき UI に表示する。",
            "messages": alerts,
        },
    }

    save_json(DATA_PATH, output)

    # index.html の静的サマリーを更新
    update_html_summary(today_str, k_per, s_per, prev_timeseries + [ts_entry])

    label = {"complete": "OK", "partial": "WARN"}.get(overall_status, overall_status)
    print(f"\n[{label}] data.json 書き出し完了  overall_status={overall_status}  date={today_str}")
    if alerts:
        print("--- alerts ---")
        for a in alerts:
            print(" ", a)


if __name__ == "__main__":
    build_data()
