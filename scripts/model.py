"""Predict, at the moment a pothole request comes in, whether it will wait more than 30 days.

Run after scripts/build.py:
    python scripts/model.py

Reads the cleaned `kept` table from data/311.duckdb and writes:
    exports/model_metrics.json        every number quoted in the README
    charts/6_model_risk_deciles.png   share of slow requests by predicted-risk decile (test years)
    charts/7_model_importance.png     which inputs the model relies on (permutation importance)

Design
- Label: "slow" = not closed within 30 days of being opened (still-open requests count as slow).
  Only requests opened at least 30 days before the data snapshot get a label.
- Inputs are limited to what the city knows when the request arrives: where it is, how it came in,
  when it came in, and how busy and how fast pothole crews have been recently (the "workload" inputs,
  computed only from requests opened or closed *before* that moment).
- Time-based split, so the model is always tested on later years than it learned from:
  train 2019-2023, choose settings on 2024, then refit on 2019-2024 and test once on
  Jan 2025 - Aug 2026.
"""

import json
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.preprocessing import OneHotEncoder

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "311.duckdb"
CHARTS = ROOT / "charts"
EXPORTS = ROOT / "exports"

SR_TYPE = "Pothole in Street Complaint"
SLOW_DAYS = 30
# Requests opened on or after this date haven't had 30 days to close in the snapshot
# (the download stopped at midnight on Sep 29, 2026).
LABEL_CUTOFF = pd.Timestamp("2026-08-29")
SEED = 0

DAY = np.int64(86_400 * 10**9)  # one day in nanoseconds
MAIN_ORIGINS = ["Phone Call", "Internet", "Mobile Device", "Alderman's Office"]


def load():
    con = duckdb.connect(str(DB), read_only=True)
    df = con.sql(
        """
        SELECT created_date, closed_date, days_to_close, community_area, origin, latitude, longitude
        FROM kept
        WHERE sr_type = ? AND community_area IS NOT NULL
        ORDER BY created_date
        """,
        params=[SR_TYPE],
    ).df()
    con.close()
    return df


def window_features(df, group_col, prefix):
    """Workload and recent-speed inputs for each request, using only requests in the same group
    (one community area, or the whole city) that were opened or closed before it arrived."""
    out = {f"{prefix}_open_now": np.zeros(len(df)), f"{prefix}_new_last_7d": np.zeros(len(df)),
           f"{prefix}_closed_last_30d": np.zeros(len(df)),
           f"{prefix}_recent_log_days": np.full(len(df), np.nan),
           f"{prefix}_recent_slow_share": np.full(len(df), np.nan)}
    groups = df.groupby(group_col).indices if group_col else {"all": np.arange(len(df))}
    for idx in groups.values():
        g = df.iloc[idx]
        t = g["created_date"].values.astype("int64")             # arrival times of this group's requests
        created = np.sort(t)
        closed_mask = g["closed_date"].notna().values
        order = np.argsort(g["closed_date"].values[closed_mask].astype("int64"))
        closed = g["closed_date"].values[closed_mask].astype("int64")[order]
        log_days = np.log1p(g["days_to_close"].values[closed_mask][order])
        slow = (g["days_to_close"].values[closed_mask][order] > SLOW_DAYS).astype(float)
        cum_log = np.concatenate([[0.0], np.cumsum(log_days)])
        cum_slow = np.concatenate([[0.0], np.cumsum(slow)])

        n_created_before = np.searchsorted(created, t, side="left")
        n_closed_before = np.searchsorted(closed, t, side="left")
        out[f"{prefix}_open_now"][idx] = n_created_before - n_closed_before
        out[f"{prefix}_new_last_7d"][idx] = n_created_before - np.searchsorted(created, t - 7 * DAY, side="left")

        lo = np.searchsorted(closed, t - 30 * DAY, side="left")
        n = n_closed_before - lo
        out[f"{prefix}_closed_last_30d"][idx] = n
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"{prefix}_recent_log_days"][idx] = np.where(n > 0, (cum_log[n_closed_before] - cum_log[lo]) / n, np.nan)
            out[f"{prefix}_recent_slow_share"][idx] = np.where(n > 0, (cum_slow[n_closed_before] - cum_slow[lo]) / n, np.nan)
    return pd.DataFrame(out, index=df.index)


def build_features(df):
    feats = pd.DataFrame(index=df.index)
    feats["community_area"] = df["community_area"].astype(int)
    feats["latitude"] = df["latitude"]
    feats["longitude"] = df["longitude"]
    origin = df["origin"].where(df["origin"].isin(MAIN_ORIGINS), "Other")
    feats["origin"] = pd.Categorical(origin, categories=MAIN_ORIGINS + ["Other"]).codes
    feats["month"] = df["created_date"].dt.month
    feats["day_of_week"] = df["created_date"].dt.dayofweek
    feats["hour"] = df["created_date"].dt.hour
    feats = feats.join(window_features(df, "community_area", "area"))
    feats = feats.join(window_features(df, None, "city"))
    return feats


LOCATION = ["community_area", "latitude", "longitude"]
CALENDAR = ["origin", "month", "day_of_week", "hour"]
WORKLOAD = [f"{p}_{s}" for p in ("area", "city") for s in
            ("open_now", "new_last_7d", "closed_last_30d", "recent_log_days", "recent_slow_share")]
CATEGORICAL = {"community_area", "origin"}


def hgb(columns, **params):
    return HistGradientBoostingClassifier(
        categorical_features=[c in CATEGORICAL for c in columns], random_state=SEED, **params)


def scores(y, p):
    top = p >= np.quantile(p, 0.8)
    return {
        "roc_auc": round(float(roc_auc_score(y, p)), 3),
        "average_precision": round(float(average_precision_score(y, p)), 3),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "top20_slow_share": round(float(y[top].mean()), 3),
        "top20_recall": round(float(y[top].sum() / y.sum()), 3),
    }


def main():
    df = load()
    feats = build_features(df)
    y_all = ((df["closed_date"].isna()) | (df["days_to_close"] > SLOW_DAYS)).astype(int).values
    year = df["created_date"].dt.year.values
    labeled = (df["created_date"] < LABEL_CUTOFF).values
    train = labeled & (year <= 2023)
    valid = labeled & (year == 2024)
    test = labeled & (year >= 2025)
    fit = labeled & (year <= 2024)
    X, y = feats, y_all
    print(f"Pothole requests: {len(df):,} kept; labeled {labeled.sum():,}. "
          f"Train {train.sum():,} / valid {valid.sum():,} / test {test.sum():,}")

    # 1. Choose gradient-boosting settings on 2024 (trained on 2019-2023 only).
    full_cols = LOCATION + CALENDAR + WORKLOAD
    grid = [dict(learning_rate=lr, max_leaf_nodes=leaves, min_samples_leaf=msl, max_iter=300)
            for lr in (0.05, 0.1) for leaves in (15, 31, 63) for msl in (50, 200)]
    tried = []
    for params in grid:
        m = hgb(full_cols, **params).fit(X.loc[train, full_cols], y[train])
        auc = roc_auc_score(y[valid], m.predict_proba(X.loc[valid, full_cols])[:, 1])
        tried.append((auc, params))
    best_auc, best = max(tried, key=lambda r: r[0])
    print(f"Best 2024 validation AUC {best_auc:.3f} with {best}")

    # 2. Refit every model on 2019-2024 and score each one once on the test years.
    results = {}
    test_pred = {}

    # Baseline: each area's share of slow requests in 2019-2024.
    area_rate = pd.Series(y[fit]).groupby(X.loc[fit, "community_area"].values).mean()
    p = X.loc[test, "community_area"].map(area_rate).fillna(y[fit].mean()).values
    results["Area history only (baseline)"] = scores(y[test], p)

    # Logistic regression on where and when (no workload inputs).
    enc = OneHotEncoder(handle_unknown="ignore")
    cols = ["community_area", "origin", "month", "day_of_week"]
    lr = LogisticRegression(max_iter=2000, C=1.0).fit(enc.fit_transform(X.loc[fit, cols]), y[fit])
    p = lr.predict_proba(enc.transform(X.loc[test, cols]))[:, 1]
    results["Logistic regression: location + calendar"] = scores(y[test], p)

    for name, cols in [("Gradient boosting: location + calendar", LOCATION + CALENDAR),
                       ("Gradient boosting: workload + calendar (no location)", CALENDAR + WORKLOAD),
                       ("Gradient boosting: all inputs", full_cols)]:
        m = hgb(cols, **best).fit(X.loc[fit, cols], y[fit])
        p = m.predict_proba(X.loc[test, cols])[:, 1]
        results[name] = scores(y[test], p)
        test_pred[name] = (m, cols, p)

    for name, r in results.items():
        print(f"  {name:55s} AUC {r['roc_auc']:.3f}  AP {r['average_precision']:.3f}  "
              f"top-20% slow share {r['top20_slow_share']:.1%}")

    # 3. Risk deciles and permutation importance for the full model.
    model, cols, p = test_pred["Gradient boosting: all inputs"]
    yt = y[test]
    deciles = pd.qcut(pd.Series(p).rank(method="first"), 10, labels=False)
    by_decile = pd.Series(yt).groupby(deciles.values).mean()

    rng = np.random.RandomState(SEED)
    sample = rng.choice(np.where(test)[0], size=min(30_000, test.sum()), replace=False)
    imp = permutation_importance(model, X.iloc[sample][cols], y[sample], scoring="roc_auc",
                                 n_repeats=5, random_state=SEED)
    importance = pd.Series(imp.importances_mean, index=cols).sort_values(ascending=False)

    base_rate = float(yt.mean())
    metrics = {
        "request_type": SR_TYPE,
        "label": f"not closed within {SLOW_DAYS} days of being opened (still-open counts as slow)",
        "rows": {"kept_pothole_requests": int(len(df)), "labeled": int(labeled.sum()),
                 "train_2019_2023": int(train.sum()), "valid_2024": int(valid.sum()),
                 "test_2025_to_aug_2026": int(test.sum())},
        "slow_share": {"train": round(float(y[train].mean()), 3), "valid": round(float(y[valid].mean()), 3),
                       "test": round(base_rate, 3)},
        "chosen_settings": best, "validation_auc": round(float(best_auc), 3),
        "test_results": results,
        "test_slow_share_by_risk_decile": {int(k) + 1: round(float(v), 3) for k, v in by_decile.items()},
        "permutation_importance_auc_drop": {k: round(float(v), 4) for k, v in importance.items()},
    }
    (EXPORTS / "model_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    # Charts
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(by_decile.index + 1, by_decile.values * 100, color="#3a6ea5")
    ax.axhline(base_rate * 100, color="#999", ls="--", lw=1)
    ax.text(0.6, base_rate * 100 + 1.5, f"all test requests: {base_rate:.0%}", ha="left", fontsize=9, color="#555")
    ax.set_xticks(range(1, 11))
    ax.set_xlabel("Predicted risk decile (1 = lowest, 10 = highest)")
    ax.set_ylabel("Requests not closed within 30 days (%)")
    ax.set_title("Pothole requests, Jan 2025 - Aug 2026 (years the model never saw)", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(CHARTS / "6_model_risk_deciles.png", dpi=150)
    plt.close(fig)

    labels = {"community_area": "Community area", "latitude": "Latitude", "longitude": "Longitude",
              "origin": "How it came in (phone, app, ...)", "month": "Month", "day_of_week": "Day of week",
              "hour": "Hour", "area_open_now": "Open requests in the area", "area_new_last_7d": "New requests in the area, last 7 days",
              "area_closed_last_30d": "Closed in the area, last 30 days", "area_recent_log_days": "Recent closing speed in the area",
              "area_recent_slow_share": "Recent share of slow closes in the area", "city_open_now": "Open requests citywide",
              "city_new_last_7d": "New requests citywide, last 7 days", "city_closed_last_30d": "Closed citywide, last 30 days",
              "city_recent_log_days": "Recent closing speed citywide", "city_recent_slow_share": "Recent share of slow closes citywide"}
    top = importance.head(8)[::-1]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.barh([labels[c] for c in top.index], top.values, color="#3a6ea5")
    ax.set_xlabel("Drop in test AUC when the input is shuffled")
    ax.set_title("What the model relies on (top 8 inputs)", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(CHARTS / "7_model_importance.png", dpi=150)
    plt.close(fig)
    print("Wrote exports/model_metrics.json, charts/6_model_risk_deciles.png, charts/7_model_importance.png")


if __name__ == "__main__":
    main()
