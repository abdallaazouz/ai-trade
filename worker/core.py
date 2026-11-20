# -*- coding: utf-8 -*-
"""
core.py — منطق التحليل مستخرج من iq_ai_analyzer_practice_auto.py بدون tkinter/صوت/LSTM.
تحليل فقط — لا يوجد تداول آلي.
"""
from __future__ import annotations
import os, time, sqlite3, json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier, ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import balanced_accuracy_score, brier_score_loss, log_loss

try:
    from lightgbm import LGBMClassifier
    LIGHTGBM_AVAILABLE = True
except ImportError:
    LIGHTGBM_AVAILABLE = False
try:
    from xgboost import XGBClassifier
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False

class _Events:
    """بديل بسيط لطابور الأحداث: يطبع السجل في Actions."""
    def put(self, item):
        if item and item[0] == "log":
            print(item[1], flush=True)
events = _Events()

BASE = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "state"))
BASE.mkdir(parents=True, exist_ok=True)
DB = BASE / "iq_ai_data.sqlite3"
MODEL_DIR = BASE / "models"
MODEL_DIR.mkdir(exist_ok=True)

INTERVAL = 60
TRAIN_HORIZONS = (5, 10, 15, 30)
DISPLAY_HORIZONS = (5, 10, 15, 30)

FEATURES = [
    "ret1", "ret2", "ret3", "ret5", "ret10", "ret20",
    "sma5", "sma10", "sma20", "sma50", "sma100",
    "ema5", "ema10", "ema20", "ema50",
    "rsi7", "rsi14", "macd", "macd_signal", "macd_hist",
    "bb_width", "bb_position", "atr14", "stoch_k", "stoch_d",
    "cci20", "williams_r", "body", "upper_shadow",
    "lower_shadow", "range_pct", "doji", "hammer",
    "shooting_star", "bullish_engulfing", "bearish_engulfing",
    "vol_z", "ret_vol", "mom10", "mom20", "roc10",
    "trend_strength", "hour_sin", "hour_cos", "weekday_sin", "weekday_cos",
]

MODEL_VERSION = "v3-duration-2026"

models = {}
last_train_count = {}
TORCH_AVAILABLE = False

def _table_columns(c, table):
    try:
        return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _migrate_predictions(c):
    """
    يرقّي جدول predictions القديم (بدون عمود id) إلى المخطط الجديد
    دون المساس بجدول candles.
    """
    cols = _table_columns(c, "predictions")
    if not cols:
        return  # سيُنشأ لاحقًا
    # المخطط الجديد يحتوي على id و created_ts و status
    if "id" in cols and "created_ts" in cols and "status" in cols:
        return  # محدّث بالفعل

    # نسخ احتياطي للجدول القديم ثم إنشاء الجديد
    try:
        c.execute("ALTER TABLE predictions RENAME TO predictions_legacy")
    except Exception:
        # إن فشل، حاول حذف القديم فقط إذا كان فارغًا من الأعمدة الجديدة
        try:
            c.execute("DROP TABLE IF EXISTS predictions_old_bak")
            c.execute("ALTER TABLE predictions RENAME TO predictions_old_bak")
        except Exception as exc:
            events.put(("log", f"تعذر ترقية predictions: {exc}"))
            return
    events.put(("log", "ترقية جدول predictions إلى المخطط الجديد (الشموع لم تُمس)."))


def db_connect():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS candles(
            pair TEXT NOT NULL,
            ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL,
            PRIMARY KEY(pair, ts)
        )
    """)
    # ترقية predictions إن وُجد مخطط قديم
    _migrate_predictions(c)
    c.execute("""
        CREATE TABLE IF NOT EXISTS predictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            ref_price REAL NOT NULL,
            direction TEXT,
            target_horizon INTEGER,
            prob_up REAL,
            prob_down REAL,
            duration_est INTEGER,
            duration_lo INTEGER,
            duration_hi INTEGER,
            duration_confidence REAL,
            model_name TEXT,
            model_version TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            actual_price REAL,
            actual_up INTEGER,
            hit INTEGER,
            duration_error INTEGER,
            settled_at TEXT,
            data_note TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS model_metrics(
            pair TEXT, horizon INTEGER, model TEXT,
            balanced REAL, brier REAL, logloss REAL,
            baseline REAL, n_train INTEGER, n_test INTEGER,
            trained_at TEXT,
            PRIMARY KEY(pair, horizon, model, trained_at)
        )
    """)
    c.commit()
    return c


def save_candles(c, pair, candles):
    """يحفظ الشموع المغلقة فقط (ts+INTERVAL <= now). يمنع التكرار عبر PRIMARY KEY."""
    now = int(time.time())
    rows = []
    max_ts = None
    for x in candles or []:
        try:
            ts = int(x["from"])
            if ts + INTERVAL > now:
                continue  # شمعة لم تُغلق بعد
            rows.append((
                pair, ts,
                float(x["open"]), float(x["max"]),
                float(x["min"]), float(x["close"]),
                float(x.get("volume", 0) or 0),
            ))
            if max_ts is None or ts > max_ts:
                max_ts = ts
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return 0, max_ts
    before = c.total_changes
    c.executemany(
        """INSERT OR IGNORE INTO candles
           (pair, ts, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?)""",
        rows,
    )
    c.commit()
    added = c.total_changes - before
    return added, max_ts


def read_candles(c, pair):
    df = pd.read_sql_query(
        "SELECT ts, open, high, low, close, volume FROM candles WHERE pair=? ORDER BY ts",
        c, params=(pair,),
    )
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def detect_gaps(df, interval=INTERVAL):
    """عدد الفجوات (> 1.5 * interval بين شمعتين متتاليتين)."""
    if df is None or len(df) < 2:
        return 0
    diffs = df["ts"].diff().dropna()
    return int((diffs > interval * 1.5).sum())


def indicators(raw: pd.DataFrame) -> pd.DataFrame:
    d = raw.copy()
    close, high, low, op = d["close"], d["high"], d["low"], d["open"]

    for n in (1, 2, 3, 5, 10, 20):
        d[f"ret{n}"] = close.pct_change(n)
    for n in (5, 10, 20, 50, 100):
        d[f"sma{n}"] = close / close.rolling(n).mean() - 1
    for n in (5, 10, 20, 50):
        d[f"ema{n}"] = close / close.ewm(span=n, adjust=False).mean() - 1

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    for n in (7, 14):
        avg_g = gain.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
        avg_l = loss.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
        rs = avg_g / (avg_l + 1e-12)
        d[f"rsi{n}"] = 100 - 100 / (1 + rs)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    d["macd"] = (ema12 - ema26) / close
    d["macd_signal"] = (ema12 - ema26).ewm(span=9, adjust=False).mean() / close
    d["macd_hist"] = d["macd"] - d["macd_signal"]

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    upper, lower = mid + 2 * std, mid - 2 * std
    d["bb_width"] = (upper - lower) / (mid + 1e-12)
    d["bb_position"] = (close - lower) / (upper - lower + 1e-12)

    prev_c = close.shift(1)
    tr = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.rolling(14).mean() / (close + 1e-12)

    low14, high14 = low.rolling(14).min(), high.rolling(14).max()
    d["stoch_k"] = 100 * (close - low14) / (high14 - low14 + 1e-12)
    d["stoch_d"] = d["stoch_k"].rolling(3).mean()

    typical = (high + low + close) / 3
    tmean = typical.rolling(20).mean()
    mad = typical.rolling(20).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
    d["cci20"] = (typical - tmean) / (0.015 * mad + 1e-12)
    d["williams_r"] = -100 * (high14 - close) / (high14 - low14 + 1e-12)

    cr = (high - low).clip(lower=1e-12)
    body = (close - op).abs()
    d["body"] = (close - op) / (close.abs() + 1e-12)
    d["upper_shadow"] = (high - np.maximum(op, close)) / cr
    d["lower_shadow"] = (np.minimum(op, close) - low) / cr
    d["range_pct"] = cr / (close.abs() + 1e-12)
    d["doji"] = (body / cr < 0.10).astype(int)
    d["hammer"] = (
        (d["lower_shadow"] > 0.60) & (d["upper_shadow"] < 0.20) & (body / cr < 0.35)
    ).astype(int)
    d["shooting_star"] = (
        (d["upper_shadow"] > 0.60) & (d["lower_shadow"] < 0.20) & (body / cr < 0.35)
    ).astype(int)

    prev_bull = close.shift(1) > op.shift(1)
    prev_bear = close.shift(1) < op.shift(1)
    bull, bear = close > op, close < op
    d["bullish_engulfing"] = (
        prev_bear & bull & (op <= close.shift(1)) & (close >= op.shift(1))
    ).astype(int)
    d["bearish_engulfing"] = (
        prev_bull & bear & (op >= close.shift(1)) & (close <= op.shift(1))
    ).astype(int)

    hour = d["time"].dt.hour
    weekday = d["time"].dt.dayofweek
    d["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    d["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    d["weekday_sin"] = np.sin(2 * np.pi * weekday / 7)
    d["weekday_cos"] = np.cos(2 * np.pi * weekday / 7)

    ret1 = close.pct_change(1)
    vol20 = ret1.rolling(20).std()
    d["vol_z"] = (ret1 - ret1.rolling(20).mean()) / (vol20 + 1e-12)
    d["ret_vol"] = ret1 / (vol20 + 1e-12)
    d["mom10"] = close / close.shift(10) - 1
    d["mom20"] = close / close.shift(20) - 1
    d["roc10"] = close.pct_change(10)
    sma50 = close.rolling(50).mean()
    d["trend_strength"] = (close - sma50) / (close.rolling(20).std() + 1e-12)

    return d.replace([np.inf, -np.inf], np.nan)


def estimate_trend_duration(raw: pd.DataFrame, direction: str, atr_mult: float = 0.5):
    """
    يقدّر مدة استمرار الحركة بالدقائق اعتمادًا على حلقات تاريخية مشابهة.
    التعريف:
      - اتجاه صعود: استمرار طالما close لم ينخفض بمقدار atr_mult*ATR من ذروة الحركة.
      - اتجاه هبوط: العكس.
    الحالات التي لم تنتهِ داخل النافذة تُعامل كـ censored (لا تُحسب مدة مكتملة).
    """
    if raw is None or len(raw) < 200:
        return {
            "duration_est": None, "duration_lo": None, "duration_hi": None,
            "confidence": 0.0, "n_samples": 0,
            "note": "لا توجد بيانات كافية لتقدير المدة",
        }

    d = indicators(raw)
    close = d["close"].to_numpy()
    atr = (d["atr14"] * d["close"]).to_numpy()  # ATR مطلق تقريبي
    n = len(close)
    want_up = direction.upper() in ("UP", "CALL", "صعود")

    durations = []
    # امسح حلقات تاريخية (بدون الشمعة الأخيرة الحية)
    i = 50
    while i < n - 30:
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            i += 1
            continue
        threshold = atr_mult * a
        start_price = close[i]
        # هل بدأت حركة واضحة في الاتجاه المطلوب خلال 3 شموع؟
        window = close[i:i + 3]
        if want_up:
            if window.max() - start_price < threshold * 0.3:
                i += 1
                continue
        else:
            if start_price - window.min() < threshold * 0.3:
                i += 1
                continue

        peak = start_price
        ended = False
        dur = 0
        for j in range(i + 1, min(i + 180, n)):  # سقف 180 دقيقة للحلقة
            dur = j - i
            px = close[j]
            if want_up:
                peak = max(peak, px)
                if peak - px >= threshold:
                    ended = True
                    break
            else:
                peak = min(peak, px)
                if px - peak >= threshold:
                    ended = True
                    break
        if ended and dur >= 1:
            durations.append(dur)
            i = j  # تخطَّ للأمام
        else:
            i += 1  # censored — لا نضيف مدة

    if len(durations) < 8:
        return {
            "duration_est": None, "duration_lo": None, "duration_hi": None,
            "confidence": 0.0, "n_samples": len(durations),
            "note": "لا توجد بيانات كافية لتقدير المدة",
        }

    arr = np.array(durations, dtype=float)
    med = int(np.median(arr))
    lo = int(np.percentile(arr, 25))
    hi = int(np.percentile(arr, 75))
    # ثقة تعتمد على حجم العينة وتضيّق النطاق النسبي
    iqr = max(hi - lo, 1)
    conf = min(0.95, 0.25 + 0.5 * (1 - iqr / (med + iqr)) + 0.02 * min(len(arr), 40))
    return {
        "duration_est": med,
        "duration_lo": lo,
        "duration_hi": hi,
        "confidence": float(conf),
        "n_samples": int(len(arr)),
        "note": f"من {len(arr)} حلقة تاريخية مكتملة",
    }


def train_model(raw, pair, horizon):
    d = indicators(raw)
    # هدف: هل الإغلاق بعد horizon دقيقة أعلى من الإغلاق الحالي؟
    d["target"] = (d["close"].shift(-horizon) > d["close"]).astype(int)
    d = d.iloc[:-horizon].dropna(subset=FEATURES + ["target"])

    if len(d) < 500 or d["target"].nunique() < 2:
        return None, "بيانات غير كافية (500+ عينة صالحة)."

    X = d[FEATURES].astype(float)
    y = d["target"].astype(int)
    # Walk-forward بسيط: 70% تدريب زمني، 30% اختبار لاحق
    split = int(len(d) * 0.70)
    if split < 100 or len(d) - split < 50:
        return None, "بيانات غير كافية للاختبار الزمني."

    X_tr, y_tr = X.iloc[:split], y.iloc[:split]
    X_te, y_te = X.iloc[split:], y.iloc[split:]

    candidates = {
        "HistGradientBoosting": HistGradientBoostingClassifier(
            max_iter=150, learning_rate=0.05, max_leaf_nodes=31,
            min_samples_leaf=20, l2_regularization=1.0, random_state=42,
        ),
        "ExtraTrees": ExtraTreesClassifier(
            n_estimators=150, max_depth=10, min_samples_leaf=5,
            class_weight="balanced_subsample", n_jobs=-1, random_state=42,
        ),
        "RandomForest": RandomForestClassifier(
            n_estimators=150, max_depth=10, min_samples_leaf=5,
            class_weight="balanced_subsample", n_jobs=-1, random_state=42,
        ),
        "LogReg": LogisticRegression(
            C=0.5, max_iter=400, class_weight="balanced", solver="lbfgs", random_state=42,
        ),
    }
    if LIGHTGBM_AVAILABLE:
        candidates["LightGBM"] = LGBMClassifier(
            n_estimators=200, learning_rate=0.05, num_leaves=31,
            min_child_samples=20, subsample=0.85, colsample_bytree=0.85,
            class_weight="balanced", random_state=42, verbosity=-1,
        )
    if XGBOOST_AVAILABLE:
        pos = max(int(y_tr.sum()), 1)
        neg = max(int(len(y_tr) - y_tr.sum()), 1)
        candidates["XGBoost"] = XGBClassifier(
            n_estimators=200, learning_rate=0.05, max_depth=5,
            subsample=0.85, colsample_bytree=0.85,
            scale_pos_weight=neg / pos, eval_metric="logloss",
            random_state=42, n_jobs=-1, verbosity=0,
        )

    scored = []
    for name, clf in candidates.items():
        try:
            clf.fit(X_tr, y_tr)
            if not hasattr(clf, "predict_proba"):
                continue
            pred = clf.predict(X_te)
            p_all = clf.predict_proba(X_te)
            classes = list(clf.classes_)
            if 1 not in classes:
                continue
            p_up = p_all[:, classes.index(1)]
            bal = balanced_accuracy_score(y_te, pred)
            brier = brier_score_loss(y_te, p_up)
            try:
                ll = log_loss(y_te, p_up, labels=[0, 1])
            except Exception:
                ll = float("nan")
            score = bal - 0.30 * brier
            scored.append((score, name, clf, bal, brier, ll))
            events.put(("log", f"  مرشح {name} ({horizon}د): Bal={bal:.3f} Brier={brier:.3f}"))
        except Exception as exc:
            events.put(("log", f"  تعذر {name}: {exc}"))

    if not scored:
        return None, "فشل كل المرشحين."
    scored.sort(key=lambda x: x[0], reverse=True)
    _, best_name, best_model, bal, brier, ll = scored[0]
    baseline = max(float(y_te.mean()), 1.0 - float(y_te.mean()))

    try:
        base = candidates[best_name]
        base.fit(X, y)
        try:
            cal = CalibratedClassifierCV(base, method="sigmoid", cv=3)
            cal.fit(X, y)
            best_model = cal
            best_name = best_name + "+Cal"
        except Exception:
            best_model = base
    except Exception:
        best_model.fit(X, y)

    models[(pair, horizon)] = best_model
    last_train_count[(pair, horizon)] = len(raw)
    joblib.dump(best_model, MODEL_DIR / f"{pair}_{horizon}m.joblib")

    # حفظ مقاييس
    try:
        c = db_connect()
        c.execute(
            """INSERT OR REPLACE INTO model_metrics
               (pair,horizon,model,balanced,brier,logloss,baseline,n_train,n_test,trained_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (pair, horizon, best_name, bal, brier, ll, baseline,
             len(X_tr), len(X_te), datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()
        c.close()
    except Exception:
        pass

    metrics = (
        f"المختار={best_name} | Bal={bal:.3f} | Baseline={baseline:.3f} | "
        f"Brier={brier:.3f} | اختبار زمني — ليس ضمان ربح"
    )
    return best_model, metrics


def load_model_if_needed(pair, horizon):
    key = (pair, horizon)
    if key in models:
        return models[key]
    path = MODEL_DIR / f"{pair}_{horizon}m.joblib"
    if path.exists():
        try:
            models[key] = joblib.load(path)
            return models[key]
        except Exception:
            return None
    return None


def nearest_trained_horizon(h):
    return min(TRAIN_HORIZONS, key=lambda x: abs(x - h))


def multi_horizon_probs(raw, pair):
    """احتمالات على آفاق متعددة. للآفاق غير المدربة يُستخدم أقرب نموذج مع ملاحظة."""
    rows = []
    for h in DISPLAY_HORIZONS:
        train_h = h if h in TRAIN_HORIZONS else nearest_trained_horizon(h)
        probs = predict_at_horizon(raw, pair, train_h)
        if not probs:
            continue
        # متوسط النماذج المتاحة
        p_up = float(np.mean(list(probs.values())))
        rows.append({
            "horizon": h,
            "train_horizon": train_h,
            "prob_up": p_up,
            "prob_down": 1.0 - p_up,
            "models": dict(probs),
            "approx": h not in TRAIN_HORIZONS,
        })
    return rows


def build_forecast(raw, pair):
    """يبني توقعًا شاملاً: اتجاه + آفاق + مدة."""
    if raw is None or len(raw) < 100:
        return {"ok": False, "note": "بيانات غير كافية"}

    multi = multi_horizon_probs(raw, pair)
    if not multi:
        return {"ok": False, "note": "لا يوجد نموذج مدرب — شغّل workflow التدريب (train)"}

    # الاتجاه من أقرب أفق مدرب رئيسي (15 إن وُجد وإلا 10 أو 5)
    primary = None
    for prefer in (15, 10, 5, 30, 60):
        for r in multi:
            if r["horizon"] == prefer:
                primary = r
                break
        if primary:
            break
    if primary is None:
        primary = multi[0]

    p_up = primary["prob_up"]
    direction = "صعود" if p_up >= 0.5 else "هبوط"
    conf_dir = max(p_up, 1 - p_up)

    dur = estimate_trend_duration(raw, "UP" if direction == "صعود" else "DOWN")

    ref_price = float(raw.iloc[-1]["close"])
    ref_ts = int(raw.iloc[-1]["ts"])

    status = "ثقة منخفضة"
    if conf_dir >= 0.70 and dur.get("confidence", 0) >= 0.55:
        status = "ثقة متوسطة إلى جيدة"
    elif conf_dir >= 0.60:
        status = "ثقة متوسطة"
    if dur.get("duration_est") is None:
        status = "يحتاج بيانات إضافية للمدة"

    model_names = "+".join(primary["models"].keys()) if primary.get("models") else "ML"

    # صفوف جدول: كل أفق سطر واحد (ML و LSTM جنبًا إلى جنب)
    table_rows = []
    for r in multi:
        h = r["horizon"]
        ml = r.get("models") or {}
        ml_up = ml.get("ML")
        lstm_up = ml.get("LSTM")
        def _cell(p):
            if p is None:
                return "—", "—"
            if p >= 0.5:
                return "صعود", f"{p:.0%}"
            return "هبوط", f"{1-p:.0%}"
        ml_dir, ml_pct = _cell(ml_up)
        ls_dir, ls_pct = _cell(lstm_up)
        table_rows.append({
            "horizon": h,
            "ml_dir": ml_dir, "ml_pct": ml_pct,
            "lstm_dir": ls_dir, "lstm_pct": ls_pct,
            "ml_up": ml_up, "lstm_up": lstm_up,
        })
    # مدة واحدة للاتجاه العام
    model_lines = []
    horizon_lines = []

    return {
        "ok": True,
        "pair": pair,
        "ref_price": ref_price,
        "ref_ts": ref_ts,
        "direction": direction,
        "prob_up": p_up,
        "prob_down": 1 - p_up,
        "dir_confidence": conf_dir,
        "primary_horizon": primary["horizon"],
        "multi": multi,
        "duration": dur,
        "model_name": model_names,
        "model_version": MODEL_VERSION,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_lines": model_lines,
        "horizon_lines": horizon_lines,
        "table_rows": table_rows,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


def save_prediction(forecast):
    if not forecast.get("ok"):
        return
    dur = forecast.get("duration") or {}
    c = db_connect()
    try:
        cols = _table_columns(c, "predictions")
        if "created_ts" not in cols:
            return
        c.execute(
            """INSERT INTO predictions(
                pair, created_ts, created_at, ref_price, direction,
                target_horizon, prob_up, prob_down,
                duration_est, duration_lo, duration_hi, duration_confidence,
                model_name, model_version, status, data_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                forecast["pair"], forecast["ref_ts"], forecast["created_at"],
                forecast["ref_price"], forecast["direction"],
                forecast["primary_horizon"], forecast["prob_up"], forecast["prob_down"],
                dur.get("duration_est"), dur.get("duration_lo"), dur.get("duration_hi"),
                dur.get("confidence"),
                forecast["model_name"], forecast["model_version"],
                "PENDING", dur.get("note", ""),
            ),
        )
        c.commit()
    except Exception as exc:
        events.put(("log", f"تعذر حفظ التوقع: {exc}"))
    finally:
        c.close()


def settle_pending_predictions(raw, pair):
    """يحدّث التوقعات المنتهية أفقها من الشموع المتاحة."""
    if raw is None or raw.empty:
        return
    price_map = dict(zip(raw["ts"].astype(int), raw["close"].astype(float)))
    times = sorted(price_map.keys())
    c = db_connect()
    try:
        cols = _table_columns(c, "predictions")
        if "id" not in cols or "status" not in cols:
            return  # مخطط غير جاهز بعد
        rows = c.execute(
            """SELECT id, created_ts, ref_price, direction, target_horizon, prob_up
               FROM predictions WHERE pair=? AND status='PENDING'""",
            (pair,),
        ).fetchall()
        for pid, cts, ref, direction, horizon, p_up in rows:
            if cts is None or horizon is None:
                continue
            target_ts = int(cts) + int(horizon) * INTERVAL
            actual = None
            for t in times:
                if t >= target_ts:
                    actual = price_map[t]
                    break
            if actual is None:
                continue
            actual_up = 1 if actual > ref else 0
            predicted_up = 1 if (p_up or 0) >= 0.5 else 0
            hit = 1 if actual_up == predicted_up else 0
            c.execute(
                """UPDATE predictions SET status='SETTLED', actual_price=?,
                   actual_up=?, hit=?, settled_at=? WHERE id=?""",
                (actual, actual_up, hit,
                 datetime.now().isoformat(timespec="seconds"), pid),
            )
        c.commit()
    except Exception as exc:
        events.put(("log", f"تعذر تسوية التوقعات: {exc}"))
    finally:
        c.close()


def prediction_stats(pair=None):
    c = db_connect()
    try:
        cols = _table_columns(c, "predictions")
        if "status" not in cols:
            return {"settled": 0, "hits": 0, "hit_rate": None, "pending": 0}
        if pair:
            row = c.execute(
                """SELECT COUNT(*), SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END)
                   FROM predictions WHERE pair=? AND status='SETTLED'""",
                (pair,),
            ).fetchone()
            pending = c.execute(
                "SELECT COUNT(*) FROM predictions WHERE pair=? AND status='PENDING'",
                (pair,),
            ).fetchone()[0]
        else:
            row = c.execute(
                """SELECT COUNT(*), SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END)
                   FROM predictions WHERE status='SETTLED'"""
            ).fetchone()
            pending = c.execute(
                "SELECT COUNT(*) FROM predictions WHERE status='PENDING'"
            ).fetchone()[0]
        settled = int(row[0] or 0)
        hits = int(row[1] or 0)
        rate = (100.0 * hits / settled) if settled else None
        return {"settled": settled, "hits": hits, "hit_rate": rate, "pending": int(pending or 0)}
    except Exception:
        return {"settled": 0, "hits": 0, "hit_rate": None, "pending": 0}
    finally:
        c.close()


def predict_at_horizon(raw, pair, horizon):
    d = indicators(raw)
    valid = d.dropna(subset=FEATURES)
    if valid.empty:
        return {}
    row = valid.iloc[-1:]
    out = {}
    model = load_model_if_needed(pair, horizon)
    if model is not None:
        try:
            p = model.predict_proba(row[FEATURES])[0]
            out["ML"] = float(p[list(model.classes_).index(1)])
        except Exception:
            pass
    return out
