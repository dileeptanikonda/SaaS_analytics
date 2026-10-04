#!/usr/bin/env python3
"""
SaaS PRODUCT ANALYTICS - ONE-FILE PIPELINE
Dataset : RavenStack SaaS Subscription & Churn Analytics (Kaggle, synthetic)
Tools   : Python + MySQL + Excel + Power BI

WHAT IT DOES (in order)
  1. Reads the 5 Kaggle CSVs and cleans them (logs every data issue found)
  2. Generates a 1,000,000-row SYNTHETIC product-event table (clearly labelled)
  3. Creates the MySQL schema (from saas_analytics.sql), loads all tables,
     builds the views and prints the data-quality checks
  4. Runs the analysis (KPIs, churn, MRR, features, support) + saves charts
  5. Trains a churn-risk model and scores every account
  6. Exports an Excel workbook and Power BI-ready CSVs

SETUP
  pip install pandas numpy matplotlib scikit-learn openpyxl sqlalchemy pymysql
  Put the 5 CSVs in ./data  (accounts, subscriptions, feature_usage,
  support_tickets, churn_events) and saas_analytics.sql next to this file.

RUN
  python saas_analytics.py                       # everything
  python saas_analytics.py --no-mysql            # skip database steps
  python saas_analytics.py --events 200000       # smaller event table
  python saas_analytics.py --mysql-password abc  # or set env MYSQL_PASSWORD
"""
import argparse
import os
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
SEED = 42
rng = np.random.default_rng(SEED)
ISSUES = []  # (table, issue, count)


def log_issue(table, issue, n):
    if n:
        ISSUES.append((table, issue, int(n)))
        print(f"   [issue] {table}: {issue} -> {int(n):,}")


# ---------------------------------------------------------------- 1. LOAD + CLEAN
def find_csv(data_dir, keyword):
    for f in sorted(Path(data_dir).glob("*.csv")):
        if keyword in f.stem.lower():
            return f
    raise FileNotFoundError(f"No CSV containing '{keyword}' in {data_dir}")


def read_table(data_dir, keyword, id_col):
    df = pd.read_csv(find_csv(data_dir, keyword), dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    for c in df.columns:
        df[c] = df[c].str.strip().replace({"": np.nan, "nan": np.nan, "NaN": np.nan, "None": np.nan})
    # duplicated header row inside the data
    if id_col in df.columns:
        n = (df[id_col] == id_col).sum()
        df = df[df[id_col] != id_col]
        log_issue(keyword, "duplicate header rows removed", n)
        n = df.duplicated().sum()
        df = df.drop_duplicates()
        log_issue(keyword, "exact duplicate rows removed", n)
        n = df.duplicated(subset=[id_col]).sum()
        df = df.drop_duplicates(subset=[id_col], keep="first")
        log_issue(keyword, f"duplicate {id_col} removed", n)
        n = df[id_col].isna().sum()
        df = df[df[id_col].notna()]
        log_issue(keyword, f"missing {id_col} removed", n)
    return df.reset_index(drop=True)


def to_flag(s):
    m = {"true": 1, "false": 0, "1": 1, "0": 0, "yes": 1, "no": 0, "y": 1, "n": 0, "1.0": 1, "0.0": 0}
    return s.astype(str).str.lower().map(m).astype("float")


def to_num(s):
    return pd.to_numeric(s, errors="coerce")


def to_date(s):
    return pd.to_datetime(s, errors="coerce")


COLS = {
    "accounts": ["account_id", "account_name", "industry", "country", "signup_date", "referral_source",
                 "plan_tier", "seats", "is_trial", "churn_flag"],
    "subscriptions": ["subscription_id", "account_id", "start_date", "end_date", "plan_tier", "seats",
                      "mrr_amount", "arr_amount", "is_trial", "upgrade_flag", "downgrade_flag",
                      "churn_flag", "billing_frequency", "auto_renew_flag"],
    "feature_usage": ["usage_id", "subscription_id", "usage_date", "feature_name", "usage_count",
                      "usage_duration_secs", "error_count", "is_beta_feature"],
    "support_tickets": ["ticket_id", "account_id", "submitted_at", "closed_at", "resolution_time_hours",
                        "priority", "first_response_time_minutes", "satisfaction_score", "escalation_flag"],
    "churn_events": ["churn_event_id", "account_id", "churn_date", "reason_code", "refund_amount_usd",
                     "preceding_upgrade_flag", "preceding_downgrade_flag", "is_reactivation", "feedback_text"],
}


def clean_all(data_dir):
    print("\n[1/6] Loading and cleaning CSVs")
    acc = read_table(data_dir, "account", "account_id")
    sub = read_table(data_dir, "subscription", "subscription_id")
    fu = read_table(data_dir, "feature", "usage_id")
    st = read_table(data_dir, "ticket", "ticket_id")
    ce = read_table(data_dir, "churn", "churn_event_id")

    # accounts
    acc["signup_date"] = to_date(acc.get("signup_date"))
    for c in ("seats",):
        acc[c] = to_num(acc.get(c))
    for c in ("is_trial", "churn_flag"):
        acc[c] = to_flag(acc.get(c))
    for c in ("industry", "country", "referral_source", "plan_tier"):
        if c in acc:
            acc[c] = acc[c].str.title().fillna("Unknown") if c != "country" else acc[c].fillna("Unknown")

    # subscriptions
    for c in ("start_date", "end_date"):
        sub[c] = to_date(sub.get(c))
    for c in ("seats", "mrr_amount", "arr_amount"):
        sub[c] = to_num(sub.get(c))
    for c in ("is_trial", "upgrade_flag", "downgrade_flag", "churn_flag", "auto_renew_flag"):
        sub[c] = to_flag(sub.get(c))
    if "plan_tier" in sub:
        sub["plan_tier"] = sub["plan_tier"].str.title().fillna("Unknown")
    n = (sub["end_date"].notna() & (sub["end_date"] < sub["start_date"])).sum()
    sub.loc[sub["end_date"] < sub["start_date"], "end_date"] = pd.NaT
    log_issue("subscriptions", "end_date before start_date (set to NULL)", n)
    n = (sub["mrr_amount"] < 0).sum()
    sub.loc[sub["mrr_amount"] < 0, "mrr_amount"] = np.nan
    log_issue("subscriptions", "negative MRR (set to NULL)", n)

    # feature usage
    fu["usage_date"] = to_date(fu.get("usage_date"))
    for c in ("usage_count", "usage_duration_secs", "error_count"):
        fu[c] = to_num(fu.get(c))
    fu["is_beta_feature"] = to_flag(fu.get("is_beta_feature"))
    n = (fu["usage_count"] < 0).sum()
    fu.loc[fu["usage_count"] < 0, "usage_count"] = np.nan
    log_issue("feature_usage", "negative usage_count (set to NULL)", n)

    # tickets
    for c in ("submitted_at", "closed_at"):
        st[c] = to_date(st.get(c))
    for c in ("resolution_time_hours", "first_response_time_minutes", "satisfaction_score"):
        st[c] = to_num(st.get(c))
    st["escalation_flag"] = to_flag(st.get("escalation_flag"))
    st["priority"] = st["priority"].str.title().fillna("Unknown")
    n = (st["closed_at"] < st["submitted_at"]).sum()
    st.loc[st["closed_at"] < st["submitted_at"], "closed_at"] = pd.NaT
    log_issue("support_tickets", "closed before submitted (set to NULL)", n)
    n = ((st["satisfaction_score"] < 1) | (st["satisfaction_score"] > 5)).sum()
    st.loc[(st["satisfaction_score"] < 1) | (st["satisfaction_score"] > 5), "satisfaction_score"] = np.nan
    log_issue("support_tickets", "CSAT outside 1-5 (set to NULL)", n)

    # churn events
    ce["churn_date"] = to_date(ce.get("churn_date"))
    ce["refund_amount_usd"] = to_num(ce.get("refund_amount_usd"))
    for c in ("preceding_upgrade_flag", "preceding_downgrade_flag", "is_reactivation"):
        ce[c] = to_flag(ce.get(c))
    ce["reason_code"] = ce["reason_code"].fillna("unknown").str.lower()

    # referential integrity (orphans break MySQL foreign keys)
    n = (~sub["account_id"].isin(acc["account_id"])).sum()
    sub = sub[sub["account_id"].isin(acc["account_id"])]
    log_issue("subscriptions", "orphan account_id removed", n)
    n = (~fu["subscription_id"].isin(sub["subscription_id"])).sum()
    fu = fu[fu["subscription_id"].isin(sub["subscription_id"])]
    log_issue("feature_usage", "orphan subscription_id removed", n)
    n = (~st["account_id"].isin(acc["account_id"])).sum()
    st = st[st["account_id"].isin(acc["account_id"])]
    log_issue("support_tickets", "orphan account_id removed", n)
    n = (~ce["account_id"].isin(acc["account_id"])).sum()
    ce = ce[ce["account_id"].isin(acc["account_id"])]
    log_issue("churn_events", "orphan account_id removed", n)

    # churn flag vs churn log consistency (audit only, kept as-is)
    flagged = set(acc.loc[acc["churn_flag"] == 1, "account_id"])
    logged = set(ce["account_id"])
    log_issue("accounts", "flagged churned but no churn event", len(flagged - logged))
    log_issue("accounts", "churn event but flag = 0", len(logged - flagged))

    tables = {"accounts": acc, "subscriptions": sub, "feature_usage": fu,
              "support_tickets": st, "churn_events": ce}
    for k, df in tables.items():
        for c in COLS[k]:
            if c not in df.columns:
                df[c] = np.nan
        tables[k] = df[COLS[k]].reset_index(drop=True)
        print(f"   {k:16s} {len(tables[k]):>8,} rows")
    return tables


# ---------------------------------------------------------------- 2. SYNTHETIC EVENTS
EVENT_TYPES = ["login", "dashboard_viewed", "project_created", "report_exported",
               "teammate_invited", "integration_connected", "api_call", "settings_changed"]
EVENT_P = [0.30, 0.22, 0.10, 0.08, 0.05, 0.04, 0.16, 0.05]


def generate_events(t, n_events):
    """SYNTHETIC product-event log tied to each account's real signup / churn dates.
    Accounts that later churned are made less likely to be active in their first 14
    days. This is a simulation assumption, NOT a finding - say so in your README."""
    print(f"\n[2/6] Generating {n_events:,} synthetic product events")
    acc, ce = t["accounts"].copy(), t["churn_events"]
    acc = acc[acc["signup_date"].notna()].reset_index(drop=True)
    churn_dt = ce.groupby("account_id")["churn_date"].min()
    acc["end_dt"] = acc["account_id"].map(churn_dt)
    max_dt = pd.Timestamp(max(t["subscriptions"]["start_date"].max(), acc["signup_date"].max())
                          + pd.Timedelta(days=90))
    acc["end_dt"] = acc["end_dt"].fillna(max_dt)
    acc.loc[acc["end_dt"] <= acc["signup_date"], "end_dt"] = acc["signup_date"] + pd.Timedelta(days=30)

    start = acc["signup_date"].values.astype("datetime64[s]").astype("int64")
    end = acc["end_dt"].values.astype("datetime64[s]").astype("int64")
    w = (acc["seats"].fillna(5).clip(lower=1).values.astype(float)) ** 0.7
    w /= w.sum()

    n_main = int(n_events * 0.93)
    idx = rng.choice(len(acc), n_main, p=w)
    ts = start[idx] + (rng.random(n_main) * (end[idx] - start[idx])).astype("int64")
    et = rng.choice(EVENT_TYPES, n_main, p=EVENT_P)

    # early-activation events: 14-day window after signup
    p_act = np.where(acc["churn_flag"].fillna(0).values == 1, 0.55, 0.85)
    active = rng.random(len(acc)) < p_act
    a_idx = np.repeat(np.where(active)[0], 3)
    n_early = len(a_idx)
    ts_e = start[a_idx] + (rng.random(n_early) * 14 * 86400).astype("int64")
    et_e = rng.choice(["login", "project_created", "teammate_invited", "dashboard_viewed"], n_early)

    # remaining budget: signup event for every account
    ts_s, et_s, s_idx = start.copy(), np.array(["signup"] * len(acc)), np.arange(len(acc))

    all_idx = np.concatenate([idx, a_idx, s_idx])
    all_ts = np.concatenate([ts, ts_e, ts_s])
    all_et = np.concatenate([et, et_e, et_s])
    ev = pd.DataFrame({
        "event_id": np.arange(1, len(all_idx) + 1, dtype="int64"),
        "account_id": acc["account_id"].values[all_idx],
        "event_ts": pd.to_datetime(all_ts, unit="s"),
        "event_type": all_et,
    })
    ev = ev.sort_values("event_ts").reset_index(drop=True)
    ev["event_id"] = np.arange(1, len(ev) + 1, dtype="int64")
    print(f"   {len(ev):,} events for {ev['account_id'].nunique():,} accounts (synthetic)")
    return ev


# ---------------------------------------------------------------- 3. MYSQL
def split_blocks(sql_path):
    text = Path(sql_path).read_text(encoding="utf-8")
    blocks = {}
    for m in re.finditer(r"-- @@BLOCK (\d+):.*?\n(.*?)-- @@END", text, flags=re.S):
        blocks[int(m.group(1))] = m.group(2)
    return blocks


def run_sql(conn, sql, show=False):
    from sqlalchemy import text
    out = []
    # strip comment lines, split on ;
    clean = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    for stmt in [s.strip() for s in clean.split(";") if s.strip()]:
        res = conn.execute(text(stmt))
        if show and res.returns_rows:
            df = pd.DataFrame(res.fetchall(), columns=list(res.keys()))
            out.append(df)
            print(df.to_string(index=False))
            print()
    return out


def mysql_load(t, events, args, sql_path):
    from sqlalchemy import create_engine
    print("\n[3/6] MySQL: schema, load, views, data-quality checks")
    blocks = split_blocks(sql_path)
    if not all(k in blocks for k in (1, 2, 3)):
        sys.exit("saas_analytics.sql blocks not found - keep it next to this script unchanged.")
    pwd = args.mysql_password or os.environ.get("MYSQL_PASSWORD", "")
    url = f"mysql+pymysql://{args.mysql_user}:{pwd}@{args.mysql_host}:{args.mysql_port}/"
    eng = create_engine(url, pool_pre_ping=True)
    with eng.begin() as c:
        run_sql(c, blocks[1])
    print("   schema created")
    eng = create_engine(url + "saas_analytics", pool_pre_ping=True)
    order = ["accounts", "subscriptions", "feature_usage", "support_tickets", "churn_events"]
    for name in order:
        df = t[name].copy()
        df = df.astype(object).where(df.notna(), None)
        df.to_sql(name, eng, if_exists="append", index=False, chunksize=5000, method="multi")
        print(f"   loaded {name:16s} {len(df):>9,}")
    if events is not None:
        events.to_sql("product_events", eng, if_exists="append", index=False,
                      chunksize=20000, method="multi")
        print(f"   loaded {'product_events':16s} {len(events):>9,}")
    with eng.begin() as c:
        run_sql(c, blocks[3])
        print("   views created")
        print("\n   --- data-quality checks ---")
        run_sql(c, blocks[2], show=True)
    print("   Now open saas_analytics.sql in Workbench and run BLOCK 4 (19 analysis queries).")


# ---------------------------------------------------------------- 4. ANALYSIS
def build_account_frame(t):
    acc, sub, fu, st, ce = (t[k] for k in ("accounts", "subscriptions", "feature_usage",
                                           "support_tickets", "churn_events"))
    today = max(sub["start_date"].max(), acc["signup_date"].max())
    s = sub.groupby("account_id").agg(
        total_subs=("subscription_id", "count"),
        current_mrr=("mrr_amount", lambda x: x[sub.loc[x.index, "end_date"].isna()].sum()))
    f = fu.merge(sub[["subscription_id", "account_id"]], on="subscription_id").groupby("account_id").agg(
        total_usage=("usage_count", "sum"), total_errors=("error_count", "sum"),
        features_used=("feature_name", "nunique"))
    k = st.groupby("account_id").agg(tickets=("ticket_id", "count"), avg_csat=("satisfaction_score", "mean"),
                                     avg_resolution_hrs=("resolution_time_hours", "mean"),
                                     escalations=("escalation_flag", "sum"))
    c = ce.groupby("account_id").agg(churn_date=("churn_date", "min"), reason_code=("reason_code", "first"))
    df = acc.set_index("account_id").join([s, f, k, c]).reset_index()
    for col in ("total_subs", "current_mrr", "total_usage", "total_errors", "features_used", "tickets", "escalations"):
        df[col] = df[col].fillna(0)
    return df, today


def monthly_mrr(sub):
    starts = sub["start_date"].dropna()
    months = pd.date_range(starts.min().replace(day=1), starts.max().replace(day=1), freq="MS")
    rows = []
    for m in months:
        nxt = m + pd.offsets.MonthBegin(1)
        active = sub[(sub["start_date"] < nxt) & (sub["end_date"].isna() | (sub["end_date"] >= nxt))]["mrr_amount"].sum()
        new = sub[(sub["start_date"] >= m) & (sub["start_date"] < nxt)]["mrr_amount"].sum()
        churned = sub[(sub["end_date"] >= m) & (sub["end_date"] < nxt)]["mrr_amount"].sum()
        rows.append((m, active, new, churned))
    d = pd.DataFrame(rows, columns=["month_start", "active_mrr", "new_mrr", "churned_mrr"])
    d["net_new_mrr"] = d["new_mrr"] - d["churned_mrr"]
    d["mom_growth_pct"] = d["active_mrr"].pct_change() * 100
    return d


def analyse(t, events, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    print("\n[4/6] Analysis and charts")
    df, today = build_account_frame(t)
    sub, fu, st, ce = t["subscriptions"], t["feature_usage"], t["support_tickets"], t["churn_events"]

    kpi = pd.DataFrame({
        "kpi": ["Accounts", "Active accounts", "Current MRR", "Current ARR", "ARPA (active)",
                "Logo churn %", "Avg CSAT", "Avg resolution hrs", "Tickets"],
        "value": [len(df), int((df["churn_flag"] == 0).sum()), df["current_mrr"].sum(),
                  df["current_mrr"].sum() * 12,
                  df.loc[df["current_mrr"] > 0, "current_mrr"].mean(),
                  df["churn_flag"].mean() * 100, st["satisfaction_score"].mean(),
                  st["resolution_time_hours"].mean(), len(st)]})
    by_plan = df.groupby("plan_tier").agg(accounts=("account_id", "count"), churn_pct=("churn_flag", "mean"),
                                          mrr=("current_mrr", "sum")).reset_index()
    by_plan["churn_pct"] *= 100
    by_ind = df.groupby("industry").agg(accounts=("account_id", "count"), churn_pct=("churn_flag", "mean"),
                                        mrr=("current_mrr", "sum")).reset_index().sort_values("churn_pct", ascending=False)
    by_ind["churn_pct"] *= 100
    by_ref = df.groupby("referral_source").agg(accounts=("account_id", "count"),
                                               churn_pct=("churn_flag", "mean")).reset_index()
    by_ref["churn_pct"] *= 100
    mm = monthly_mrr(sub)
    reasons = ce.groupby("reason_code").agg(events=("churn_event_id", "count"),
                                            refunds=("refund_amount_usd", "sum")).reset_index() \
        .sort_values("events", ascending=False)
    reasons["pct_of_churn"] = reasons["events"] / reasons["events"].sum() * 100
    fa = fu.merge(sub[["subscription_id", "account_id"]], on="subscription_id").groupby("feature_name").agg(
        accounts_using=("account_id", "nunique"), total_usage=("usage_count", "sum"),
        total_errors=("error_count", "sum")).reset_index()
    fa["error_rate_pct"] = fa["total_errors"] / fa["total_usage"].replace(0, np.nan) * 100
    fa = fa.sort_values("accounts_using", ascending=False)
    support = st.groupby("priority").agg(tickets=("ticket_id", "count"), escalation_pct=("escalation_flag", "mean"),
                                         avg_first_response_min=("first_response_time_minutes", "mean"),
                                         avg_csat=("satisfaction_score", "mean")).reset_index()
    support["escalation_pct"] *= 100

    # cohort retention (monthly signup cohorts)
    d2 = df[df["signup_date"].notna()].copy()
    d2["cohort"] = d2["signup_date"].dt.to_period("M").dt.to_timestamp()
    d2["end"] = d2["churn_date"].fillna(today)
    d2["life_m"] = ((d2["end"] - d2["signup_date"]).dt.days / 30.4).clip(lower=0)
    rows = []
    for coh, g in d2.groupby("cohort"):
        for k in (0, 1, 2, 3, 6, 9, 12):
            if coh + pd.DateOffset(months=k) <= today:
                rows.append((coh, len(g), k, (g["life_m"] >= k).mean() * 100))
    cohort = pd.DataFrame(rows, columns=["cohort_month", "cohort_size", "months_since_signup", "retained_pct"])

    # funnel + early activation (synthetic events)
    funnel = act = None
    if events is not None:
        funnel = events.groupby("event_type")["account_id"].nunique().sort_values(ascending=False) \
            .rename("accounts").reset_index()
        funnel["pct_of_accounts"] = funnel["accounts"] / events["account_id"].nunique() * 100
        m = events.merge(df[["account_id", "signup_date", "churn_flag"]], on="account_id")
        m["early"] = (m["event_ts"] <= m["signup_date"] + pd.Timedelta(days=14)).astype(int)
        e = m.groupby("account_id").agg(active_first_14d=("early", "max"), churn_flag=("churn_flag", "first"))
        act = e.groupby("active_first_14d").agg(accounts=("churn_flag", "count"),
                                                churn_pct=("churn_flag", "mean")).reset_index()
        act["churn_pct"] *= 100

    # charts (colours = dashboard palette)
    NAVY, BLUE, TEAL, ORANGE, AMBER, GREEN, PURPLE = "#0B132B", "#2F6FED", "#17BEBB", "#E4572E", "#F4A300", "#2EAD6B", "#6C5CE7"
    out.mkdir(parents=True, exist_ok=True)
    ch = out / "charts"
    ch.mkdir(exist_ok=True)

    def save(fig, name):
        fig.tight_layout()
        fig.savefig(ch / name, dpi=140)
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(by_plan["plan_tier"], by_plan["churn_pct"], color=ORANGE)
    ax.set_title("Logo churn % by plan"); ax.set_ylabel("%")
    save(fig, "churn_by_plan.png")
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(mm["month_start"], mm["active_mrr"], color=BLUE, lw=2)
    ax.set_title("Active MRR by month")
    save(fig, "mrr_trend.png")
    fig, ax = plt.subplots(figsize=(8, 4))
    r = reasons.head(8).iloc[::-1]
    ax.barh(r["reason_code"], r["events"], color=ORANGE)
    ax.set_title("Top churn reasons")
    save(fig, "churn_reasons.png")
    fig, ax = plt.subplots(figsize=(8, 5))
    f = fa.head(10).iloc[::-1]
    ax.barh(f["feature_name"], f["accounts_using"], color=TEAL)
    ax.set_title("Top features by accounts using")
    save(fig, "feature_adoption.png")

    # churn model
    model_out = churn_model(df, out, ch)

    # export: Excel + Power BI CSVs
    print("\n[6/6] Exporting Excel + Power BI files")
    pbi = out / "powerbi"
    pbi.mkdir(exist_ok=True)
    sheets = {"KPIs": kpi, "Churn_by_Plan": by_plan, "Churn_by_Industry": by_ind,
              "Churn_by_Referral": by_ref, "Monthly_MRR": mm, "Churn_Reasons": reasons,
              "Feature_Adoption": fa, "Support_by_Priority": support, "Cohort_Retention": cohort}
    if funnel is not None:
        sheets["Event_Funnel"] = funnel
        sheets["Early_Activation"] = act
    if model_out is not None:
        sheets["Risk_Scores"] = model_out["scores"].head(1000)
        sheets["Model_Summary"] = model_out["summary"]
    sheets["Account_360"] = df.head(50000)
    with pd.ExcelWriter(out / "SaaS_Analytics_Workbook.xlsx", engine="openpyxl") as xw:
        for n, d in sheets.items():
            d.to_excel(xw, sheet_name=n[:31], index=False)
    for n, d in sheets.items():
        d.to_csv(pbi / f"{n.lower()}.csv", index=False)
    if model_out is not None:
        model_out["scores"].to_csv(pbi / "risk_scores_all.csv", index=False)
    print(f"   Excel -> {out / 'SaaS_Analytics_Workbook.xlsx'}")
    print(f"   Power BI CSVs -> {pbi}")
    print("\n   ---- HEADLINE KPIs ----")
    print(kpi.to_string(index=False))


def churn_model(df, out, ch):
    print("\n[5/6] Churn-risk model")
    try:
        import matplotlib.pyplot as plt
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.metrics import roc_auc_score, roc_curve
        from sklearn.model_selection import train_test_split
    except ImportError:
        print("   scikit-learn missing - skipped")
        return None
    num = ["seats", "is_trial", "total_subs", "current_mrr", "total_usage", "total_errors",
           "features_used", "tickets", "avg_csat", "avg_resolution_hrs", "escalations"]
    cat = ["plan_tier", "industry", "referral_source", "country"]
    X = pd.concat([df[num].fillna(df[num].median()), pd.get_dummies(df[cat], drop_first=True)], axis=1).astype(float)
    y = df["churn_flag"].fillna(0).astype(int)
    if y.nunique() < 2:
        print("   only one class - skipped")
        return None
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=SEED, stratify=y)
    mdl = GradientBoostingClassifier(random_state=SEED).fit(Xtr, ytr)
    auc = roc_auc_score(yte, mdl.predict_proba(Xte)[:, 1])
    print(f"   Test AUC = {auc:.3f}  (synthetic data -> modest AUC is normal; report it honestly)")
    prob = mdl.predict_proba(X)[:, 1]
    scores = df[["account_id", "account_name", "plan_tier", "industry", "current_mrr", "churn_flag"]].copy()
    scores["churn_probability"] = prob
    scores["risk_tier"] = pd.cut(prob, [-1, 0.33, 0.66, 2], labels=["Low", "Medium", "High"])
    scores["mrr_at_risk"] = np.where(scores["churn_flag"] == 0, scores["current_mrr"] * prob, 0)
    scores = scores.sort_values("mrr_at_risk", ascending=False)
    imp = pd.Series(mdl.feature_importances_, index=X.columns).sort_values(ascending=False).head(10)
    summary = pd.DataFrame({"feature": imp.index, "importance": imp.values})
    summary.loc[len(summary)] = ["TEST_AUC", auc]
    fpr, tpr, _ = roc_curve(yte, mdl.predict_proba(Xte)[:, 1])
    fig, ax = plt.subplots(1, 2, figsize=(11, 4))
    ax[0].plot(fpr, tpr, color="#6C5CE7", lw=2); ax[0].plot([0, 1], [0, 1], "--", color="grey")
    ax[0].set_title(f"ROC (AUC {auc:.2f})")
    imp.iloc[::-1].plot.barh(ax=ax[1], color="#6C5CE7"); ax[1].set_title("Top feature importances")
    fig.tight_layout(); fig.savefig(ch / "churn_model.png", dpi=140); plt.close(fig)
    print("   MRR at risk (active accounts): {:,.0f}".format(scores["mrr_at_risk"].sum()))
    return {"scores": scores, "summary": summary, "auc": auc}


# ---------------------------------------------------------------- MAIN
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data")
    p.add_argument("--out-dir", default="output")
    p.add_argument("--sql-file", default="saas_analytics.sql")
    p.add_argument("--events", type=int, default=1_000_000, help="0 = skip event generation")
    p.add_argument("--no-mysql", action="store_true")
    p.add_argument("--mysql-host", default="localhost")
    p.add_argument("--mysql-port", default="3306")
    p.add_argument("--mysql-user", default="root")
    p.add_argument("--mysql-password", default="")
    a = p.parse_args()

    out = Path(a.out_dir)
    tables = clean_all(a.data_dir)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ISSUES, columns=["table", "issue", "count"]).to_csv(out / "data_quality_log.csv", index=False)

    events = generate_events(tables, a.events) if a.events > 0 else None
    if not a.no_mysql:
        mysql_load(tables, events, a, a.sql_file)
    else:
        print("\n[3/6] MySQL skipped (--no-mysql)")
    analyse(tables, events, out)
    print("\nDONE. Open the output/ folder.")


if __name__ == "__main__":
    main()
