#!/usr/bin/env python3
"""미국·한국 종목의 시세(2년 일봉)·지수·재무지표·뉴스를 모아 data/stocks.json 으로 저장한다.

- 시세·지수: 실행할 때마다 갱신 (Yahoo Finance, 거래소 지연 시세)
- 재무지표·실적일·뉴스: 오래된 종목부터 한 번에 MAX_FUND 개씩 나눠 갱신 (차단 방지)
- 조회에 실패한 종목은 직전 데이터를 그대로 유지하고 stale 표시
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent
FUND_TTL_HOURS = 12
MAX_FUND = int(os.environ.get("MAX_FUND", "60"))
BARS = 500
INDEX_BARS = 260
EPOCH = dt.date(1970, 1, 1)


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_hours(iso):
    try:
        t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 1e9


def load_prev(path):
    if not path:
        return {}, {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return ({s["id"]: s for s in data.get("stocks", [])},
                {s["id"]: s for s in data.get("indices", [])})
    except Exception as e:  # 첫 실행이거나 파일이 깨진 경우
        print(f"[prev] 사용 안 함: {e}")
        return {}, {}


def frame_to_bars(df, bars, digits):
    """DataFrame(Close, Volume) -> (dates[1970-01-01 기준 일수], closes, 최근 60일 거래량)"""
    if df is None or len(df) == 0 or "Close" not in df:
        return None
    df = df.dropna(subset=["Close"]).tail(bars)
    if len(df) < 30:
        return None
    dates = [(d.date() - EPOCH).days for d in df.index]
    closes = [round(float(c), digits) for c in df["Close"]]
    if digits == 0:
        closes = [int(c) for c in closes]
    vols = [int(num(v) or 0) for v in df["Volume"].tail(60)] if "Volume" in df else []
    return dates, closes, vols


def fetch_history(symbols, bars, digits):
    out = {}
    if not symbols:
        return out
    for attempt in range(3):
        try:
            df = yf.download(
                symbols, period="2y", interval="1d", group_by="ticker",
                auto_adjust=True, threads=False, progress=False,
            )
            for sym in symbols:
                if sym in out:
                    continue
                try:
                    b = frame_to_bars(df[sym], bars, digits)
                except Exception:
                    b = None
                if b:
                    out[sym] = b
            if len(out) >= len(symbols) * 0.8:
                break
        except Exception as e:
            print(f"[history] 일괄 조회 실패({attempt + 1}/3): {e}")
        time.sleep(5 * (attempt + 1))
    return out


def fetch_one(sym, bars, digits):
    try:
        return frame_to_bars(yf.Ticker(sym).history(period="2y", interval="1d", auto_adjust=True), bars, digits)
    except Exception as e:
        print(f"[history] {sym} 실패: {e}")
        return None


def parse_news(items):
    out = []
    for it in items or []:
        try:
            c = it.get("content") if isinstance(it.get("content"), dict) else None
            if c:
                title = c.get("title")
                url = ((c.get("canonicalUrl") or {}).get("url")) or ((c.get("clickThroughUrl") or {}).get("url"))
                date = (c.get("pubDate") or "")[:10]
                pub = (c.get("provider") or {}).get("displayName")
            else:
                title, url, pub = it.get("title"), it.get("link"), it.get("publisher")
                ts = it.get("providerPublishTime")
                date = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") if ts else ""
            if title and url and str(url).startswith("https://"):
                out.append({"t": str(title)[:160], "u": str(url), "d": date, "p": str(pub or "")[:40]})
        except Exception:
            continue
        if len(out) >= 4:
            break
    return out


NAVER = {"ok": True, "fail": 0, "shown": False}


def naver_fund(code):
    """한국 종목의 PER·PBR·배당수익률·추정PER 을 네이버 증권에서 보강한다(Yahoo 값이 부정확한 경우가 많음)."""
    if not NAVER["ok"]:
        return {}
    try:
        req = urllib.request.Request(
            f"https://m.stock.naver.com/api/stock/{code}/integration",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.load(r)
        infos = j.get("totalInfos") or []
        if not NAVER["shown"]:
            NAVER["shown"] = True
            print("[네이버] 응답 예시:", json.dumps([[i.get("code"), i.get("value")] for i in infos], ensure_ascii=False)[:600])
        out = {}
        key = {"per": "pe", "pbr": "pbr", "cnsPer": "fpe", "dividendYieldRatio": "div"}
        for it in infos:
            k = key.get(it.get("code"))
            m = re.search(r"-?[\d,]+(?:\.\d+)?", str(it.get("value") or ""))
            if not k or not m:
                continue
            v = num(m.group(0).replace(",", ""))
            if v is None:
                continue
            out[k] = v / 100.0 if k == "div" else v
        NAVER["fail"] = 0
        return out
    except Exception as e:
        NAVER["fail"] += 1
        if NAVER["fail"] >= 5:
            NAVER["ok"] = False
            print(f"[네이버] 연속 실패로 이번 실행에서는 사용 안 함: {e}")
        return {}


def fetch_fundamentals(sym, price):
    tk = yf.Ticker(sym)
    info = tk.info or {}
    if not info.get("marketCap") and not info.get("trailingPE") and not info.get("priceToBook"):
        raise RuntimeError("빈 응답")
    div_rate = num(info.get("trailingAnnualDividendRate")) or num(info.get("dividendRate"))
    div = (div_rate / price) if (div_rate and price) else None
    if div is not None and div > 0.2:  # 명백한 데이터 오류
        div = None
    pe, pbr = num(info.get("trailingPE")), num(info.get("priceToBook"))
    eps, bv = num(info.get("trailingEps")), num(info.get("bookValue"))
    if pe is None and eps and price:
        pe = price / eps
    if pbr is None and bv and bv > 0 and price:
        pbr = price / bv
    revg, epsg = num(info.get("revenueGrowth")), num(info.get("earningsGrowth"))
    if revg is not None and revg > 3:
        revg = None
    if epsg is not None and epsg > 20:
        epsg = None
    de = num(info.get("debtToEquity"))
    earn = None
    for key in ("earningsTimestampStart", "earningsTimestamp"):
        ts = num(info.get(key))
        if ts and ts > time.time() - 86400:
            earn = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
            break
    f = {
        "pe": pe,
        "fpe": num(info.get("forwardPE")),
        "pbr": pbr,
        "roe": num(info.get("returnOnEquity")),
        "opm": num(info.get("operatingMargins")),
        "revg": revg,
        "epsg": epsg,
        "de": (de / 100.0) if de is not None else None,
        "div": div,
        "mcap": num(info.get("marketCap")),
        "beta": num(info.get("beta")),
        "target": num(info.get("targetMeanPrice")),
        "analysts": num(info.get("numberOfAnalystOpinions")),
        "earn": earn,
    }
    if sym.endswith((".KS", ".KQ")):
        f.update(naver_fund(sym.split(".")[0]))
    news = []
    try:
        news = parse_news(tk.news)
    except Exception:
        pass
    return f, news


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", default="")
    ap.add_argument("--out", default="data/stocks.json")
    ap.add_argument("--force-fund", action="store_true")
    args = ap.parse_args()

    universe = json.loads((ROOT / "universe.json").read_text(encoding="utf-8"))
    prev, prev_idx = load_prev(args.prev)
    stocks, indices, errors = [], [], []
    fresh = 0

    # ---- 지수 ----
    idx_rows = universe.get("indices", [])
    ih = fetch_history([r[0] for r in idx_rows], INDEX_BARS, 2)
    for sym, name, mkt in idx_rows:
        b = ih.get(sym) or fetch_one(sym, INDEX_BARS, 2)
        if b:
            indices.append({"id": sym, "name": name, "mkt": mkt, "dates": b[0], "closes": b[1], "vols": b[2]})
        elif sym in prev_idx:
            indices.append(prev_idx[sym])
            errors.append(f"지수 {sym}")
        else:
            errors.append(f"지수 {sym}(데이터 없음)")
    print(f"[지수] {len(indices)}/{len(idx_rows)}")

    # ---- 종목 시세 ----
    ysym_of = {}
    for mkt in ("US", "KR"):
        rows = universe.get(mkt, [])
        digits = 0 if mkt == "KR" else 2

        def ys(r, alt=False):
            if mkt != "KR":
                return r[0]
            suf = r[3] if len(r) > 3 else "KS"
            if alt:
                suf = "KQ" if suf == "KS" else "KS"
            return f"{r[0]}.{suf}"

        hist = fetch_history([ys(r) for r in rows], BARS, digits)
        print(f"[{mkt}] 시세 {len(hist)}/{len(rows)}")
        for r in rows:
            code, name, sector = r[0], r[1], r[2]
            sid = f"{mkt}:{code}"
            old = prev.get(sid, {})
            entry = {"id": sid, "sym": code, "mkt": mkt, "name": name, "sector": sector,
                     "cur": "KRW" if mkt == "KR" else "USD"}
            y = ys(r)
            bars = hist.get(y)
            if not bars:
                bars = fetch_one(y, BARS, digits)
                time.sleep(1)
            if not bars and mkt == "KR":  # 코스피/코스닥 이전 상장 대비
                y = ys(r, alt=True)
                bars = fetch_one(y, BARS, digits)
                time.sleep(1)
            if bars:
                entry["dates"], entry["closes"], entry["vols"] = bars
                entry["p_at"] = now_iso()
                fresh += 1
            elif old.get("closes"):
                for k in ("dates", "closes", "vols", "p_at"):
                    entry[k] = old.get(k)
                entry["stale"] = True
                y = old.get("y") or ys(r)
                errors.append(f"{sid} 시세")
            else:
                errors.append(f"{sid} 시세(데이터 없음)")
                continue
            entry["y"] = y
            ysym_of[sid] = y
            entry["f"], entry["f_at"], entry["news"] = old.get("f") or {}, old.get("f_at"), old.get("news") or []
            stocks.append(entry)

    # ---- 재무·뉴스: 오래된 것부터 MAX_FUND 개 ----
    need = [e for e in stocks if args.force_fund or not e["f"] or age_hours(e["f_at"]) >= FUND_TTL_HOURS]
    need.sort(key=lambda e: -age_hours(e["f_at"]))
    done = fail = 0
    for e in (need if args.force_fund else need[:MAX_FUND]):
        try:
            e["f"], e["news"] = fetch_fundamentals(ysym_of[e["id"]], e["closes"][-1])
            e["f_at"] = now_iso()
            done += 1
            fail = 0
        except Exception as ex:
            errors.append(f"{e['id']} 재무: {ex}")
            fail += 1
            if fail >= 8:  # 연속 실패는 차단으로 보고 다음 실행으로 미룸
                print("[재무] 연속 실패로 중단")
                break
        time.sleep(0.6)
    print(f"[재무] 갱신 {done} / 대상 {len(need)}")

    total = len(universe.get("US", [])) + len(universe.get("KR", []))
    print(f"시세 갱신 {fresh}/{total}, 오류 {len(errors)}건")
    for e in errors[:60]:
        print("  -", e)
    if fresh == 0:
        print("새로 받은 시세가 없어 배포를 건너뜁니다.")
        sys.exit(2)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"updated": now_iso(), "source": "Yahoo Finance", "errors": errors,
                    "indices": indices, "stocks": stocks}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"저장: {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
