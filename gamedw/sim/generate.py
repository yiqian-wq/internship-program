"""源系统模拟：业务库(账号/角色) + 客户端埋点(登录/登出/支付/道具/广告)
逐日生成并写入 ODS 层。用法：
    python -m gamedw.sim.generate [--fresh] [--scale 1.0] [--days N] [--out PATH]
"""
import argparse
import math
import time
from datetime import timedelta

import numpy as np
import pandas as pd
from elasticsearch import Elasticsearch, helpers

from gamedw.config import load_config
from gamedw.db import (es_client, init_es, init_mysql, insert_many,
                       mysql_connect, wait_es)

IOS_VERS = np.array(["iOS 16.5", "iOS 17.1", "iOS 17.5", "iOS 18.0", "iOS 18.1"], dtype=object)
IOS_P = np.array([0.08, 0.15, 0.25, 0.30, 0.22])
AND_VERS = np.array(["Android 11", "Android 12", "Android 13", "Android 14", "Android 15"], dtype=object)
AND_P = np.array([0.08, 0.18, 0.30, 0.28, 0.16])
NAME_PRE = ["青云", "星河", "苍月", "流云", "踏雪", "听风", "醉月", "凌霄", "落霞", "寒鸦",
            "孤鸿", "青衫", "墨染", "白露", "望舒", "疏影", "扶摇", "惊鸿", "栖迟", "南浦"]
NAME_SUF = ["剑仙", "行者", "真人", "少侠", "剑客", "道人", "游侠", "居士", "浪人", "散人"]


class UserPool:
    """按注册顺序增长的账号池，每日注册 cohort 占据连续区间。

    池子头部 [0, cohort_start[0]) 是测试账号，不参与 cohort 活跃模拟。
    """

    F32 = ("quality", "payer_prop", "activity_pref")
    I32 = ("reg_day", "ch_pos", "server", "last_active", "level", "diamond_balance")

    def __init__(self, capacity: int = 600_000):
        self.cap = capacity
        self.size = 0
        for a in self.F32:
            setattr(self, a, np.zeros(self.cap, dtype=np.float32))
        for a in self.I32:
            setattr(self, a, np.zeros(self.cap, dtype=np.int32))
        self.has_paid = np.zeros(self.cap, dtype=bool)
        self.os = np.empty(self.cap, dtype=object)
        self.device_id = np.empty(self.cap, dtype=object)
        self.cohort_start: list[int] = []

    def ensure(self, extra: int):
        if self.size + extra <= self.cap:
            return
        while self.cap < self.size + extra:
            self.cap *= 2
        for a in self.F32:
            grown = np.zeros(self.cap, dtype=np.float32)
            grown[: self.size] = getattr(self, a)[: self.size]
            setattr(self, a, grown)
        for a in self.I32:
            grown = np.zeros(self.cap, dtype=np.int32)
            grown[: self.size] = getattr(self, a)[: self.size]
            setattr(self, a, grown)
        grown = np.zeros(self.cap, dtype=bool)
        grown[: self.size] = self.has_paid[: self.size]
        self.has_paid = grown
        for a in ("os", "device_id"):
            grown = np.empty(self.cap, dtype=object)
            grown[: self.size] = getattr(self, a)[: self.size]
            setattr(self, a, grown)


def _add_users(pool: UserPool, cfg, rng, t: int, n_new: int, douyin_share=None, force_test=False):
    if n_new <= 0:
        return
    pool.ensure(n_new)
    s = pool.size

    w = cfg.ch_share.copy()
    if force_test:
        ch_pos = np.full(n_new, cfg.test_ch_pos, dtype=np.int32)
    else:
        if douyin_share:  # 买量日：抖音占比拉高，其余渠道等比缩水
            base_dy = w[cfg.douyin_pos]
            scale = (1.0 - douyin_share) / max(1e-9, 1.0 - base_dy)
            w[cfg.douyin_pos] = douyin_share
            for i in range(len(w)):
                if i != cfg.douyin_pos:
                    w[i] *= scale
        ch_pos = rng.choice(len(w), size=n_new, p=w / w.sum()).astype(np.int32)

    up = cfg.user_profile
    quality = rng.gamma(up["quality_gamma"]["shape"], up["quality_gamma"]["scale"], n_new)
    quality = (quality / quality.mean() * cfg.ch_quality[ch_pos]).astype(np.float32)
    quality[rng.random(n_new) < up["core_user_share"]] *= 2.0

    u = rng.random(n_new)
    prop = np.where(u < 0.72, rng.uniform(0.006, 0.032, n_new),
            np.where(u < 0.95, rng.uniform(0.03, 0.12, n_new),
                                       rng.uniform(0.15, 0.45, n_new))).astype(np.float32)

    # 分服：七成进最新开的服，三成进此前开过的服
    open_servers = [sid for sid, od in cfg.server_open_day.items() if od <= t]
    latest = max(cfg.server_open_day[sid] for sid in open_servers)
    newest = np.array([sid for sid in open_servers if cfg.server_open_day[sid] == latest], dtype=np.int32)
    server = rng.choice(newest, size=n_new).astype(np.int32)
    older = [sid for sid in open_servers if cfg.server_open_day[sid] < latest][-5:]
    if older:
        alt = np.array(older, dtype=np.int32)
        alt_mask = rng.random(n_new) < 0.30
        server[alt_mask] = rng.choice(alt, size=int(alt_mask.sum()))

    is_ios = ch_pos == cfg.ch_idx[1001]
    os_col = np.where(is_ios, rng.choice(IOS_VERS, size=n_new, p=IOS_P),
                              rng.choice(AND_VERS, size=n_new, p=AND_P))
    device = np.array([f"{x:016X}" for x in rng.integers(0, 2**63, n_new, dtype=np.int64)],
                      dtype=object)

    pool.reg_day[s:s + n_new] = t
    pool.ch_pos[s:s + n_new] = ch_pos
    pool.server[s:s + n_new] = server
    pool.quality[s:s + n_new] = quality
    pool.payer_prop[s:s + n_new] = prop
    pool.activity_pref[s:s + n_new] = rng.uniform(*up["activity_pref_range"], n_new)
    pool.last_active[s:s + n_new] = -1
    pool.level[s:s + n_new] = 1
    pool.diamond_balance[s:s + n_new] = 300
    pool.has_paid[s:s + n_new] = False
    pool.os[s:s + n_new] = os_col
    pool.device_id[s:s + n_new] = device
    pool.size = s + n_new


def _pick_versions(cfg, rng, t, n):
    """按发布时间衰减的升级概率，从最近版本中抽样。"""
    avail = [((d - cfg.start).days, v) for d, v in cfg.versions if (d - cfg.start).days <= t][-3:]
    latest_day, latest = avail[-1]
    out = np.empty(n, dtype=object)
    if len(avail) == 1:
        out[:] = latest
        return out
    p_new = float(np.clip(1 - np.exp(-(t - latest_day) / 5), 0.03, 0.93))
    take_new = rng.random(n) < p_new
    out[take_new] = latest
    rest = np.nonzero(~take_new)[0]
    if rest.size:
        prev = avail[-2][1]
        third = avail[-3][1] if len(avail) >= 3 else prev
        out[rest] = np.where(rng.random(rest.size) < 0.7, prev, third)
    return out


def _hour_secs(cfg, rng, t, size, hlo=0, hhi=24):
    return cfg.epoch + t * 86400 + rng.integers(hlo * 3600, hhi * 3600 - 60, size)


GID = 1  # 单游戏模拟：《苍穹物语》gid=1


def _sd_col(d, n):
    """store_day 分区列（int YYYYMMDD，对齐公司真实规范）。"""
    return np.full(n, int(d.strftime("%Y%m%d")), dtype=np.int32)


def _clean_value(v):
    """把 pandas/numpy 标量转成可 JSON 序列化或可入库的 Python 原生值。"""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, np.generic):
        return v.item()
    return v


def _sink_mysql(con, table: str, df: pd.DataFrame, chunk: int = 5000) -> int:
    """DataFrame 按列序整行写入 MySQL 表（列名须与建表一致）。"""
    if df is None or len(df) == 0:
        return 0
    cols = list(df.columns)
    rows = [tuple(_clean_value(v) for v in row)
            for row in zip(*[df[c].tolist() for c in cols])]
    placeholders = ",".join(["%s"] * len(cols))
    return insert_many(con, f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                       rows, chunk)


def _sink_es(es: Elasticsearch, event: str, d, df: pd.DataFrame, chunk: int = 5000) -> int:
    """埋点明细写入 ES 按月索引 mlog-{event}-{YYYYMM}（按天过滤靠 store_day 字段）。"""
    if df is None or len(df) == 0:
        return 0
    index = f"mlog-{event}-{d.strftime('%Y%m')}"

    def _actions():
        for row in df.to_dict("records"):
            doc = {k: v for k, v in ((k, _clean_value(v)) for k, v in row.items())
                   if v is not None}
            yield {"_index": index, "_source": doc}

    # es.options() 返回视图客户端（非 with 用法不会关闭 transport），避免 deprecated 传参告警
    ok, errors = helpers.bulk(es.options(request_timeout=120), _actions(),
                              chunk_size=chunk, raise_on_error=False)
    if errors:
        raise RuntimeError(f"[ES bulk] {event} 写入失败 {len(errors)} 条，"
                           f"首条错误: {errors[0].get('index', {}).get('error')}")
    return len(df)


def _int64_nullable(vals_int, na_mask):
    """按掩码构造含 pd.NA 的 Int64 列。"""
    col = pd.array(np.where(na_mask, 0, vals_int), dtype="Int64")
    col[na_mask] = pd.NA
    return col


def simulate_day(pool: UserPool, cfg, con, es: Elasticsearch, t: int, d,
                 n_new: int, douyin_share=None):
    rng = np.random.default_rng([cfg.seed, t])
    dow = d.weekday()
    dau_boost = float(cfg.boost_dau[t]) * float(cfg.dau_weekend[dow])
    pay_boost = float(cfg.boost_pay[t])
    ad_boost = float(cfg.boost_ad[t])
    in_incident = t in cfg.incident_days

    pool.cohort_start.append(pool.size)
    _add_users(pool, cfg, rng, t, n_new, douyin_share)

    # ---------- 活跃模拟（按 cohort 扫描） ----------
    parts = []
    ends = pool.cohort_start[1:] + [pool.size]
    up = cfg.user_profile
    for c in range(t + 1):
        s0 = pool.cohort_start[c]
        e0 = ends[c]
        if s0 == e0:
            continue
        age = t - c
        if age == 0:
            idx = np.arange(s0, e0)
        else:
            p = cfg.curve[age] * pool.quality[s0:e0] * dau_boost
            if in_incident:
                if age <= 3:
                    p = p * cfg.incident_new_mult
                p = p * cfg.incident_dau_mult
            inactive = pool.last_active[s0:e0] < t - up["churn_window_days"]
            p = np.where(inactive, up["winback_prob"] * dau_boost, p)
            idx = np.nonzero(rng.random(e0 - s0) < p)[0] + s0
        if idx.size:
            pool.last_active[idx] = t
            parts.append(idx)

    # 测试账号：每日随机部分活跃（不参与 cohort 衰减）
    test_end = pool.cohort_start[0]
    if test_end > 0:
        tsel = np.arange(0, test_end)[rng.random(test_end) < cfg.dirty["test_daily_active_prob"]]
        if tsel.size:
            pool.last_active[tsel] = t
            parts.append(tsel)

    active = np.concatenate(parts) if parts else np.empty(0, dtype=np.int64)
    n = active.size
    if n == 0:
        return
    pool.level[active] = np.minimum(130, pool.level[active] + rng.poisson(0.9, n))

    # ---------- 登录 / 登出 ----------
    ss = cfg.session
    role_id = active + cfg.role_offset
    account_id = active + cfg.acc_base
    ch_u = pool.ch_pos[active]
    is_new = pool.reg_day[active] == t

    n_logins = np.searchsorted(np.array(ss["n_logins_cum"]), rng.random(n), side="right") + 1
    n_logins += ((pool.activity_pref[active] > 1.25) & (rng.random(n) < 0.18)).astype(np.int32)
    total = int(n_logins.sum())
    sess_local = np.repeat(np.arange(n), n_logins)

    tm = ss["time_mix"]
    tw = np.array([x[2] for x in tm]); tw = tw / tw.sum()
    tb = rng.choice(len(tm), size=total, p=tw)
    lo = np.array([x[0] for x in tm], dtype=float)[tb]
    hi = np.array([x[1] for x in tm], dtype=float)[tb]
    hour = np.minimum(lo + rng.random(total) * (hi - lo), 23.983)
    login_secs = cfg.epoch + t * 86400 + (hour * 3600).astype(np.int64) + rng.integers(0, 60, total)
    mean_min = np.where(is_new[sess_local], ss["new_user_duration_min"], ss["duration_mean_min"])
    dur_min = np.minimum(np.exp(rng.normal(np.log(mean_min), ss["duration_sigma"])), ss["duration_cap_min"])
    dur_sec = (dur_min * 60).astype(np.int64)
    sess_id = t * 1_000_000 + np.arange(1, total + 1, dtype=np.int64)
    ver = _pick_versions(cfg, rng, t, n)[sess_local]

    keep_logout = rng.random(total) >= ss["logout_missing_rate"]
    logout_secs = login_secs + dur_sec

    fail_local = np.nonzero(rng.random(n) < ss["login_fail_rate"])[0]
    nf = fail_local.size

    miss_ch = rng.random(total) < cfg.dirty["login_no_channel_rate"]
    ch_sess = cfg.ch_ids[ch_u[sess_local]].copy()
    ch_sess[miss_ch] = -1

    pid_u = np.where(ch_u == cfg.ch_idx[1001], 1, 2).astype(np.int16)
    sid_all = np.concatenate([sess_id, np.zeros(nf, dtype=np.int64)])
    df_login = pd.DataFrame({
        "store_day": _sd_col(d, total + nf),
        "log_time": pd.to_datetime(
            np.concatenate([login_secs, _hour_secs(cfg, rng, t, nf)]), unit="s"),
        "session_id": _int64_nullable(sid_all, np.arange(total + nf) >= total),
        "account_id": np.concatenate([account_id[sess_local], account_id[fail_local]]),
        "role_id": np.concatenate([role_id[sess_local], role_id[fail_local]]),
        "server_id": pd.array(np.concatenate(
            [pool.server[active][sess_local], pool.server[active][fail_local]]), dtype="Int16"),
        "pid": pd.array(np.concatenate(
            [pid_u[sess_local], pid_u[fail_local]]), dtype="Int16"),
        "gid": pd.array(np.full(total + nf, GID), dtype="Int16"),
        "cid": pd.array(np.concatenate(
            [ch_sess, cfg.ch_ids[ch_u[fail_local]]]), dtype="Int16"),
        "dev": np.concatenate([pool.device_id[active][sess_local], pool.device_id[active][fail_local]]),
        "os": np.concatenate([pool.os[active][sess_local], pool.os[active][fail_local]]),
        "app_version": np.concatenate([ver, _pick_versions(cfg, rng, t, nf)]),
        "status": np.concatenate([np.ones(total, dtype=np.int8), np.zeros(nf, dtype=np.int8)]),
    })
    _sink_es(es, "login", d, df_login)

    df_logout = pd.DataFrame({
        "store_day": _sd_col(d, int(keep_logout.sum())),
        "session_id": sess_id[keep_logout],
        "logout_time": pd.to_datetime(logout_secs[keep_logout], unit="s"),
        "duration_seconds": dur_sec[keep_logout],
        "role_id": role_id[sess_local][keep_logout],
        "server_id": pd.array(pool.server[active][sess_local][keep_logout], dtype="Int16"),
        "cid": pd.array(ch_sess[keep_logout], dtype="Int16"),
    })
    _sink_es(es, "logout", d, df_logout)

    # ---------- 支付 ----------
    pc = cfg.pay_cfg
    p_pay = pool.payer_prop[active] * cfg.ch_pay_quality[ch_u] * pay_boost * pc["active_scale"]
    payers_local = np.nonzero(rng.random(n) < p_pay)[0]
    whales = payers_local[pool.payer_prop[active][payers_local] > pc["whale_threshold"]]
    extra = whales[rng.random(whales.size) < pc["whale_extra_order_prob"]]
    order_local = np.concatenate([payers_local, extra]).astype(np.int64)
    if order_local.size:
        first = ~pool.has_paid[active[order_local]]
        prod = np.empty(order_local.size, dtype=object)
        fi = np.nonzero(first)[0]
        oi = np.nonzero(~first)[0]
        if fi.size:
            prod[fi] = rng.choice(np.array(cfg.first_pay_ids, dtype=object),
                                  size=fi.size, p=cfg.first_pay_p)
        if oi.size:
            is_whale_row = pool.payer_prop[active[order_local[oi]]] > pc["whale_threshold"]
            P = np.where(is_whale_row[:, None], cfg.pay_p_whale[None, :], cfg.pay_p_normal[None, :])
            pick = (rng.random((oi.size, 1)) > np.cumsum(P, axis=1)).sum(axis=1)
            prod[oi] = np.array(cfg.pay_prod_ids, dtype=object)[pick]
        prod[pool.ch_pos[active[order_local]] == cfg.test_ch_pos] = "P999"

        amounts = np.array([cfg.prod_price[p] for p in prod], dtype=np.float64)
        pay_secs = _hour_secs(cfg, rng, t, order_local.size, *pc["pay_hour_range"])
        sync_secs = pay_secs + rng.poisson(120, order_local.size)

        pay_pid = np.where(pool.ch_pos[active][order_local] == cfg.ch_idx[1001], 1, 2).astype(np.int16)
        df_pay = pd.DataFrame({
            "store_day": _sd_col(d, order_local.size),
            "order_id": np.array([f"PAY{d:%Y%m%d}{i:06d}" for i in range(1, order_local.size + 1)],
                                 dtype=object),
            "pay_time": pd.to_datetime(pay_secs, unit="s"),
            "account_id": active[order_local] + cfg.acc_base,
            "role_id": active[order_local] + cfg.role_offset,
            "server_id": pd.array(pool.server[active][order_local], dtype="Int16"),
            "pid": pd.array(pay_pid, dtype="Int16"),
            "gid": pd.array(np.full(order_local.size, GID), dtype="Int16"),
            "cid": pd.array(cfg.ch_ids[pool.ch_pos[active][order_local]], dtype="Int16"),
            "product_id": prod,
            "amount": np.round(amounts, 2),
            "status": np.full(order_local.size, "paid", dtype=object),
            "_sync_time": pd.to_datetime(sync_secs, unit="s"),
        })

        # 异常大额订单（运营/语义层需要识别的脏数据）
        dirty = cfg.dirty.get("pay_outlier", {})
        if dirty and d.isoformat() == dirty["date"]:
            k = int(dirty["count"])
            more_secs = _hour_secs(cfg, rng, t, k, 10, 23)
            extra = pd.DataFrame({
                "store_day": _sd_col(d, k),
                "order_id": np.array([f"PAY{d:%Y%m%d}9{i:05d}" for i in range(1, k + 1)],
                                     dtype=object),
                "pay_time": pd.to_datetime(more_secs, unit="s"),
                "account_id": np.full(k, int(df_pay["account_id"].iloc[0]), dtype=np.int64),
                "role_id": np.full(k, int(df_pay["role_id"].iloc[0]), dtype=np.int64),
                "server_id": pd.array(np.full(k, int(df_pay["server_id"].iloc[0])), dtype="Int16"),
                "pid": pd.array(np.full(k, int(df_pay["pid"].iloc[0])), dtype="Int16"),
                "gid": pd.array(np.full(k, GID), dtype="Int16"),
                "cid": pd.array(np.full(k, int(df_pay["cid"].iloc[0])), dtype="Int16"),
                "product_id": np.full(k, "P206", dtype=object),
                "amount": np.full(k, float(dirty["amount"])),
                "status": np.full(k, "paid", dtype=object),
                "_sync_time": pd.to_datetime(more_secs + 300, unit="s"),
            })
            df_pay = pd.concat([df_pay, extra], ignore_index=True)
        dup_cfg = cfg.dirty.get("pay_dup", {})
        if dup_cfg and dup_cfg["window"][0] <= d.isoformat() <= dup_cfg["window"][1]:
            k = max(1, int(len(df_pay) * dup_cfg["rate"]))
            dup_rows = df_pay.sample(n=k, random_state=t).copy()
            dup_rows["_sync_time"] = dup_rows["_sync_time"] + pd.Timedelta(hours=1)
            df_pay = pd.concat([df_pay, dup_rows], ignore_index=True)
        _sink_mysql(con, "db_mlog.t_sdk_pay_order", df_pay)
        pool.has_paid[active[order_local]] = True

    # ---------- 道具 / 货币流水 ----------
    pf = cfg.property_flow
    p_local, p_item, p_type, p_num, p_bal = [], [], [], [], []

    def emit(users, item, ctype, nums, bals=None):
        p_local.append(users)
        p_item.append(np.full(users.size, item, dtype=np.int32))
        p_type.append(np.full(users.size, ctype, dtype=object))
        p_num.append(np.asarray(nums, dtype=np.int32))
        p_bal.append(bals if bals is not None else np.full(users.size, np.nan))

    idle_u = np.nonzero(rng.random(n) < pf["idle_claim_prob"])[0]
    if idle_u.size:
        gold_u = idle_u[rng.random(idle_u.size) > 0.3]
        dia_u = idle_u[rng.random(idle_u.size) <= 0.3]
        if gold_u.size:
            emit(gold_u, 30002, "idle_claim", 4000 + rng.poisson(9000, gold_u.size))
        if dia_u.size:
            gain = 40 + rng.poisson(60, dia_u.size)
            emit(dia_u, 30001, "idle_claim", gain,
                 pool.diamond_balance[active[dia_u]] + gain)
            pool.diamond_balance[active[dia_u]] += gain
    gacha_u = np.nonzero(rng.random(n) < pf["gacha_prob"])[0]
    if gacha_u.size:
        lam = pf["gacha_lambda"] + np.where(
            pool.payer_prop[active[gacha_u]] > pc["whale_threshold"],
            pf["gacha_lambda_whale_mult"], 0)
        draws = 1 + rng.poisson(lam)
        emit(gacha_u, 30003, "gacha_consume", draws)
        rmask = rng.random(gacha_u.size) < 0.75
        rew_u, rew_n = gacha_u[rmask], draws[rmask]
        if rew_u.size:
            emit(rew_u, 40001, "gacha_reward", 1 + rng.poisson(rew_n * 0.3))
    shop_u = np.nonzero(rng.random(n) < pf["shop_prob"])[0]
    if shop_u.size:
        spend = -np.array([60, 300, 680, 1280], dtype=np.int32)[
            rng.choice(4, size=shop_u.size, p=[0.5, 0.28, 0.15, 0.07])]
        bal = pool.diamond_balance[active[shop_u]]
        emit(shop_u, 30001, "shop_buy", spend, np.maximum(bal + spend, 0))
        pool.diamond_balance[active[shop_u]] = np.maximum(bal + spend, 0)
    lv_u = np.nonzero(rng.random(n) < pf["level_reward_prob"])[0]
    if lv_u.size:
        gold_mask = rng.random(lv_u.size) < 0.7
        if gold_mask.any():
            emit(lv_u[gold_mask], 30002, "level_reward", 800 + rng.poisson(2500, int(gold_mask.sum())))
        if (~gold_mask).any():
            emit(lv_u[~gold_mask], 50001, "level_reward", 20 + rng.poisson(40, int((~gold_mask).sum())))
    if cfg.activity_days[t]:
        act_u = np.nonzero(rng.random(n) < pf["activity_reward_prob"])[0]
        if act_u.size:
            emit(act_u, 40002, "activity_reward", np.ones(act_u.size, dtype=np.int32))

    if p_local:
        local_all = np.concatenate(p_local)
        order = np.argsort(rng.random(local_all.size))
        local_all = local_all[order]
        p_item_c = np.concatenate(p_item)[order]
        p_type_c = np.concatenate(p_type)[order]
        p_num_c = np.concatenate(p_num)[order]
        p_bal_c = np.concatenate(p_bal)[order]
        df_prop = pd.DataFrame({
            "store_day": _sd_col(d, local_all.size),
            "flow_time": pd.to_datetime(_hour_secs(cfg, rng, t, local_all.size, 8, 23), unit="s"),
            "role_id": active[local_all] + cfg.role_offset,
            "server_id": pd.array(pool.server[active][local_all], dtype="Int16"),
            "cid": pd.array(cfg.ch_ids[ch_u[local_all]], dtype="Int16"),
            "item_id": p_item_c,
            "change_type": p_type_c,
            "change_num": p_num_c,
            "balance_after": _int64_nullable(
                np.where(np.isnan(p_bal_c), 0, np.round(p_bal_c)).astype(np.int64),
                np.isnan(p_bal_c)),
        })
        _sink_es(es, "property", d, df_prop)

    # ---------- 激励视频 ----------
    aw = cfg.ad_watch
    watch_u = np.nonzero(rng.random(n) < min(0.75, aw["base_prob"] * ad_boost))[0]
    if watch_u.size:
        w_cnt = 1 + rng.poisson(aw["lambda"], watch_u.size)
        w_local = np.repeat(watch_u, w_cnt)
        slots = list(aw["slots"].keys())
        slot_arr = rng.choice(np.array(slots, dtype=object), size=w_local.size,
                              p=np.array(list(aw["slots"].values())) / sum(aw["slots"].values()))
        reward_map = {"ad_energy": 50001, "ad_signin": 30002, "ad_chest": 40002, "ad_gacha": 30003}
        reward = np.array([reward_map[s] for s in slot_arr], dtype=np.int32)
        df_ad = pd.DataFrame({
            "store_day": _sd_col(d, w_local.size),
            "watch_time": pd.to_datetime(_hour_secs(cfg, rng, t, w_local.size, 8, 23), unit="s"),
            "role_id": active[w_local] + cfg.role_offset,
            "server_id": pd.array(pool.server[active][w_local], dtype="Int16"),
            "cid": pd.array(cfg.ch_ids[ch_u[w_local]], dtype="Int16"),
            "ad_slot": slot_arr,
            "reward_item_id": reward,
        })
        _sink_es(es, "ad", d, df_ad)


def write_snapshots(con, cfg, pool: UserPool):
    """业务库最新快照 -> db_mlog.t_sync_account / t_sync_role（模拟全量同步）。"""
    n = pool.size
    if n == 0:
        return
    rng = np.random.default_rng([cfg.seed, 777_777])
    reg_secs = cfg.epoch + pool.reg_day[:n].astype(np.int64) * 86400 + rng.integers(8 * 3600, 23 * 3600, n)
    pi = rng.integers(0, len(NAME_PRE), n)
    si = rng.integers(0, len(NAME_SUF), n)
    names = np.array([f"{NAME_PRE[a]}{NAME_SUF[b]}" for a, b in zip(pi, si)], dtype=object)
    sync = pd.to_datetime(cfg.epoch + cfg.n_days * 86400, unit="s")
    frozen = rng.random(n) < cfg.dirty["frozen_account_rate"]

    df_acc = pd.DataFrame({
        "account_id": np.arange(n) + cfg.acc_base,
        "register_time": pd.to_datetime(reg_secs, unit="s"),
        "pid": pd.array(np.where(pool.ch_pos[:n] == cfg.ch_idx[1001], 1, 2), dtype="Int16"),
        "cid": pd.array(cfg.ch_ids[pool.ch_pos[:n]], dtype="Int16"),
        "dev": pool.device_id[:n],
        "status": np.where(frozen, "frozen", "normal").astype(object),
        "_sync_time": sync,
    })
    _sink_mysql(con, "db_mlog.t_sync_account", df_acc)

    last_secs = np.where(pool.last_active[:n] >= 0,
                         cfg.epoch + np.maximum(pool.last_active[:n], 0) * 86400,
                         reg_secs) + rng.integers(0, 86400, n)
    df_role = pd.DataFrame({
        "role_id": np.arange(n) + cfg.role_offset,
        "account_id": np.arange(n) + cfg.acc_base,
        "role_name": names,
        "server_id": pd.array(pool.server[:n], dtype="Int16"),
        "gid": pd.array(np.full(n, GID), dtype="Int16"),
        "create_time": pd.to_datetime(reg_secs + 180, unit="s"),
        "last_login_time": pd.to_datetime(last_secs, unit="s"),
        "level": pd.array(pool.level[:n], dtype="Int16"),
        "_sync_time": sync,
    })
    _sink_mysql(con, "db_mlog.t_sync_role", df_role)


def apply_refunds(con, cfg):
    """按渠道退款率，对 7 天前的订单打退款标记（模拟事后退款）。"""
    for c in cfg.channels:
        if c["refund_rate"] <= 0:
            continue
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE db_mlog.t_sdk_pay_order SET status = 'refunded'
                WHERE cid = %s AND status = 'paid'
                  AND store_day <= %s
                  AND RAND() < %s
                """,
                [c["id"],
                 int((cfg.end - timedelta(days=7)).strftime("%Y%m%d")),
                 c["refund_rate"]],
            )


def main():
    from gamedw.db import _utf8_console
    _utf8_console()

    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="清空 MySQL 分层库与 ES 埋点索引后重建")
    ap.add_argument("--scale", type=float, default=1.0, help="量级缩放系数")
    ap.add_argument("--days", type=int, default=0, help="只生成前 N 天（冒烟测试）")
    args = ap.parse_args()

    cfg = load_config()
    n_days = cfg.n_days if args.days <= 0 else min(args.days, cfg.n_days)

    t0 = time.time()
    con = mysql_connect()
    es = es_client()
    if not wait_es(es):
        raise SystemExit("[generate] Elasticsearch 连接失败，请检查 configs/infra.yaml 或 docker compose 状态")

    if args.fresh:
        print("[generate] 清空旧数据（MySQL 分层库 + ES 埋点索引）...", flush=True)
        with con.cursor() as c:
            for db in ("db_mlog", "db_adm", "db_dw_dwd", "db_dw_dws", "db_dw_ads", "db_meta"):
                c.execute(f"DROP DATABASE IF EXISTS {db}")
        # ES 8 默认 destructive_requires_name：通配符删除被禁，需先枚举再逐个删
        legacy = list(es.indices.get(index="mlog-*", ignore_unavailable=True).keys())
        if legacy:
            print(f"[generate] 删除 ES 索引 {len(legacy)} 个 ...", flush=True)
            for name in legacy:
                es.indices.delete(index=name)

    init_mysql(con)
    init_es(es)

    pool = UserPool(capacity=max(200_000, int(cfg.n_days * 1200 * args.scale) + 5_000))
    rng0 = np.random.default_rng([cfg.seed, 999_999])
    _add_users(pool, cfg, rng0, 0, int(cfg.dirty["test_accounts"] * args.scale), force_test=True)

    # 预计算每日新增量（含量级缩放）
    n_new_list, douyin_list = [], []
    for t in range(n_days):
        d = cfg.dates[t]
        campaign = cfg.campaign.get(t)
        n_new_list.append(int(cfg.organic_new(t) * args.scale
                              * float(cfg.boost_new[t]) * float(cfg.new_weekend[d.weekday()])
                              * (campaign[0] if campaign else 1.0)))
        douyin_list.append(campaign[1] if campaign else None)

    for t in range(n_days):
        d = cfg.dates[t]
        simulate_day(pool, cfg, con, es, t, d, n_new_list[t], douyin_list[t])
        if (t + 1) % 30 == 0 or t + 1 == n_days:
            print(f"[generate] day {t + 1}/{n_days} ({d}) users={pool.size:,} "
                  f"elapsed={time.time() - t0:.1f}s", flush=True)

    print("[generate] 写业务库快照 ...", flush=True)
    write_snapshots(con, cfg, pool)
    print("[generate] 标记退款 ...", flush=True)
    apply_refunds(con, cfg)
    print(f"[generate] 完成：用户 {pool.size:,}，用时 {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
