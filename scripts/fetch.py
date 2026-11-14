#!/usr/bin/env python3
"""미국·한국 종목의 시세(1년 일봉)와 재무지표를 모아 data/stocks.json 으로 저장한다.

- 시세: 실행할 때마다 갱신 (Yahoo Finance, 거래소 지연 시세)
- 재무지표: FUND_TTL_HOURS 가 지난 종목만 다시 조회
- 조회에 실패한 종목은 직전 데이터를 그대로 유지하고 stale 표시
"""
import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent
FUND_TTL_HOURS = 6
BARS = 260


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


def load_prev(path):
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {s["id"]: s for s in data.get("stocks", [])}
    except Exception as e:  # 첫 실행이거나 파일이 깨진 경우
        print(f"[prev] 사용 안 함: {e}")
        return {}


def frame_to_bars(df):
    """DataFrame(Close, Volume) -> (dates, closes, 최근 60일 거래량)"""
    if df is None or len(df) == 0 or "Close" not in df:
        return None
    df = df.dropna(subset=["Close"]).tail(BARS)
    if len(df) < 30:
        return None
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    closes = [round(float(c), 4) for c in df["Close"]]
    vols = [int(num(v) or 0) for v in df["Volume"].tail(60)] if "Volume" in df else []
    return dates, closes, vols


def fetch_history(symbols):
    out = {}
    for attempt in range(3):
        try:
            df = yf.download(
                symbols, period="1y", interval="1d", group_by="ticker",
                auto_adjust=True, threads=False, progress=False,
            )
            for sym in symbols:
                try:
                    bars = frame_to_bars(df[sym])
                except Exception:
                    bars = None
                if bars:
                    out[sym] = bars
            if len(out) >= len(symbols) * 0.8:
                break
        except Exception as e:
            print(f"[history] 일괄 조회 실패({attempt + 1}/3): {e}")
        time.sleep(5 * (attempt + 1))
    for sym in symbols:  # 빠진 종목은 개별 재시도
        if sym in out:
            continue
        try:
            bars = frame_to_bars(yf.Ticker(sym).history(period="1y", interval="1d", auto_adjust=True))
            if bars:
                out[sym] = bars
        except Exception as e:
            print(f"[history] {sym} 실패: {e}")
        time.sleep(1)
    return out


def fetch_fundamentals(sym, price):
    info = yf.Ticker(sym).info or {}
    if not info.get("marketCap") and not info.get("trailingPE") and not info.get("priceToBook"):
        raise RuntimeError("빈 응답")
    div_rate = num(info.get("dividendRate"))
    div = (div_rate / price) if (div_rate and price) else None
    de = num(info.get("debtToEquity"))
    return {
        "pe": num(info.get("trailingPE")),
        "fpe": num(info.get("forwardPE")),
        "pbr": num(info.get("priceToBook")),
        "roe": num(info.get("returnOnEquity")),
        "opm": num(info.get("operatingMargins")),
        "revg": num(info.get("revenueGrowth")),
        "epsg": num(info.get("earningsGrowth")),
        "de": (de / 100.0) if de is not None else None,
        "div": div,
        "mcap": num(info.get("marketCap")),
        "beta": num(info.get("beta")),
        "target": num(info.get("targetMeanPrice")),
        "analysts": num(info.get("numberOfAnalystOpinions")),
    }


def age_hours(iso):
    try:
        t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 1e9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", default="")
    ap.add_argument("--out", default="data/stocks.json")
    ap.add_argument("--force-fund", action="store_true")
    args = ap.parse_args()

    universe = json.loads((ROOT / "universe.json").read_text(encoding="utf-8"))
    prev = load_prev(args.prev)
    stocks, errors = [], []
    fresh = 0

    for mkt, rows in universe.items():
        ysym = {r[0]: (r[0] + ".KS" if mkt == "KR" else r[0]) for r in rows}
        hist = fetch_history(list(ysym.values()))
        print(f"[{mkt}] 시세 {len(hist)}/{len(rows)}")
        for code, name, sector in rows:
            sid = f"{mkt}:{code}"
            old = prev.get(sid, {})
            entry = {
                "id": sid, "sym": code, "mkt": mkt, "name": name, "sector": sector,
                "cur": "KRW" if mkt == "KR" else "USD",
            }
            bars = hist.get(ysym[code])
            if bars:
                entry["dates"], entry["closes"], entry["vols"] = bars
                entry["p_at"] = now_iso()
                fresh += 1
            elif old.get("closes"):
                for k in ("dates", "closes", "vols", "p_at"):
                    entry[k] = old.get(k)
                entry["stale"] = True
                errors.append(f"{sid} 시세")
            else:
                errors.append(f"{sid} 시세(데이터 없음)")
                continue

            f, f_at = old.get("f"), old.get("f_at")
            if args.force_fund or not f or age_hours(f_at) >= FUND_TTL_HOURS:
                try:
                    f = fetch_fundamentals(ysym[code], entry["closes"][-1])
                    f_at = now_iso()
                except Exception as e:
                    errors.append(f"{sid} 재무: {e}")
                time.sleep(0.5)
            entry["f"], entry["f_at"] = f or {}, f_at
            stocks.append(entry)

    total = sum(len(v) for v in universe.values())
    print(f"시세 갱신 {fresh}/{total}, 오류 {len(errors)}건")
    for e in errors:
        print("  -", e)
    if fresh == 0:
        print("새로 받은 시세가 없어 배포를 건너뜁니다.")
        sys.exit(2)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"updated": now_iso(), "source": "Yahoo Finance", "errors": errors, "stocks": stocks},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"저장: {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
