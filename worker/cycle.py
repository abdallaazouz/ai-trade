# -*- coding: utf-8 -*-
"""
cycle.py — يُشغَّل من GitHub Actions.
  python worker/cycle.py collect   -> يجلب الشموع + يسوّي التوقعات القديمة + يسجل توقعًا جديدًا + يصدّر JSON
  python worker/cycle.py train     -> يدرّب النماذج ثم يصدّر JSON
تحليل فقط — لا يوجد أي تداول آلي ولا أوامر شراء/بيع.
"""
import os, sys, json, time, sqlite3
from datetime import datetime, timezone

import core

PAIRS = [p.strip() for p in os.environ.get("PAIRS", "EURUSD").split(",") if p.strip()]
PUBLIC = core.BASE / "public"
OFFLINE = os.environ.get("OFFLINE") == "1"      # للاختبار بدون اتصال بالمنصة


# ------------------------------------------------------------------ IQ Option
def connect_api():
    from iqoptionapi.stable_api import IQ_Option
    email, pwd = os.environ.get("IQ_EMAIL"), os.environ.get("IQ_PASSWORD")
    if not email or not pwd:
        raise RuntimeError("IQ_EMAIL / IQ_PASSWORD غير مضبوطين في GitHub Secrets")
    api = IQ_Option(email, pwd)
    ok, reason = api.connect()
    if not ok:
        raise RuntimeError(f"تعذر الاتصال بـ IQ Option: {reason}")
    api.change_balance("PRACTICE")  # حساب تجريبي — نقرأ شموعًا فقط
    return api


def fetch_candles(api, c, pair):
    """يرجع عدد الشموع الجديدة. قاعدة صغيرة: 4 دفعات للخلف. غير ذلك: يسد الفجوة حتى آخر شمعة محفوظة."""
    have, last = c.execute("SELECT COUNT(*), MAX(ts) FROM candles WHERE pair=?", (pair,)).fetchone()
    target = last if have >= 1500 else None
    max_batches = 5 if target else 4
    end, total = int(time.time()), 0
    for _ in range(max_batches):
        candles = api.get_candles(pair, core.INTERVAL, 1000, end)
        if not candles:
            break
        n, _ = core.save_candles(c, pair, candles)
        total += n
        first = int(candles[0]["from"])
        if target is not None and first <= target:
            break
        end = first - 1
    return total


# ------------------------------------------------------------------ تصدير
def _jd(o):
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def export_public():
    PUBLIC.mkdir(exist_ok=True)
    c = sqlite3.connect(core.DB)
    c.row_factory = sqlite3.Row
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "disclaimer": "تحليل إحصائي تعليمي فقط — ليس نصيحة مالية ولا ضمان ربح.",
           "pairs": {}}
    for pair in PAIRS:
        raw = core.read_candles(c, pair)
        item = {"candles": [], "forecast": None, "stats": core.prediction_stats(pair),
                "last_candle_ts": None, "market_open_guess": False}
        if not raw.empty:
            tail = raw.tail(180)
            item["candles"] = [[int(r.ts), r.open, r.high, r.low, r.close] for r in tail.itertuples()]
            item["last_candle_ts"] = int(raw.iloc[-1]["ts"])
            item["market_open_guess"] = (time.time() - item["last_candle_ts"]) < 600
            if len(raw) >= 200:
                fc = core.build_forecast(raw, pair)
                fc.pop("model_lines", None); fc.pop("horizon_lines", None)
                item["forecast"] = fc
        out["pairs"][pair] = item

    out["predictions"] = [dict(r) for r in c.execute(
        "SELECT * FROM predictions ORDER BY id DESC LIMIT 300")]
    out["metrics"] = [dict(r) for r in c.execute(
        """SELECT m.* FROM model_metrics m
           JOIN (SELECT pair,horizon,MAX(trained_at) t FROM model_metrics GROUP BY pair,horizon) x
           ON m.pair=x.pair AND m.horizon=x.horizon AND m.trained_at=x.t
           ORDER BY m.pair, m.horizon""")]
    c.close()
    (PUBLIC / "latest.json").write_text(json.dumps(out, ensure_ascii=False, default=_jd), encoding="utf-8")
    return out


def push_to_sheets(out):
    url, token = os.environ.get("SHEETS_WEBHOOK_URL"), os.environ.get("SHEETS_TOKEN", "")
    if not url:
        return
    import requests
    payload = {
        "token": token,
        "predictions": out["predictions"][:200],
        "metrics": out["metrics"],
        "summary": {"updated": out["generated_at"],
                    **{f"{p}_{k}": v for p, d in out["pairs"].items() for k, v in d["stats"].items()}},
    }
    try:
        r = requests.post(url, data=json.dumps(payload, default=_jd), timeout=60,
                          headers={"Content-Type": "text/plain"})
        print("Sheets:", r.status_code, r.text[:120])
    except Exception as exc:  # المزامنة اختيارية — لا تُفشل الدورة
        print("تعذرت مزامنة Sheets:", exc)


def finalize_db():
    """يدمج ملف WAL في الملف الرئيسي ليُحفظ ملف sqlite واحد نظيف في git."""
    c = sqlite3.connect(core.DB)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()


# ------------------------------------------------------------------ الأوامر
def collect():
    c = core.db_connect()
    api = None
    try:
        if not OFFLINE:
            api = connect_api()
        for pair in PAIRS:
            n = fetch_candles(api, c, pair) if api else 0
            raw = core.read_candles(c, pair)
            print(f"[{pair}] شموع جديدة={n} | الإجمالي={len(raw)} | فجوات≈{core.detect_gaps(raw)}")
            core.settle_pending_predictions(raw, pair)
            if len(raw) >= 200 and (n > 0 or OFFLINE):
                fc = core.build_forecast(raw, pair)
                if fc.get("ok"):
                    core.save_prediction(fc)
                    print(f"[{pair}] توقع: {fc['direction']} {fc['dir_confidence']:.0%} ({fc['status']})")
                else:
                    print(f"[{pair}] لا توقع: {fc.get('note')}")
    finally:
        c.close()
    out = export_public()
    finalize_db()
    push_to_sheets(out)


def train():
    c = core.db_connect()
    try:
        for pair in PAIRS:
            raw = core.read_candles(c, pair)
            print(f"[{pair}] تدريب على {len(raw)} شمعة")
            for h in core.TRAIN_HORIZONS:
                _, info = core.train_model(raw, pair, h)
                print(f"[{pair}] أفق {h}د: {info}")
    finally:
        c.close()
    out = export_public()
    finalize_db()
    push_to_sheets(out)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    {"collect": collect, "train": train}[cmd]()
