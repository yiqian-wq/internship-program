"""配置加载与逐日因子预计算。"""
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import yaml

CONFIG_PATH = Path(__file__).parent / "configs" / "warehouse.yaml"


def _d(s) -> date:
    return s if isinstance(s, date) else date.fromisoformat(str(s))


class Config:
    def __init__(self, raw: dict):
        self.raw = raw
        m = raw["meta"]
        self.seed = raw["seed"]
        self.game_name = m["game_name"]
        self.start = _d(m["data_start"])
        self.end = _d(m["data_end"])
        self.n_days = (self.end - self.start).days + 1
        self.dates = [self.start + timedelta(days=i) for i in range(self.n_days)]
        self.epoch = np.datetime64(self.start, "s").astype(np.int64)

        # 留存曲线插值（age -> 基础活跃概率）
        curve_raw = {int(k): float(v) for k, v in raw["retention_curve"].items()}
        max_age = max(curve_raw)
        self.curve = np.interp(
            np.arange(max_age + 1),
            list(curve_raw.keys()), list(curve_raw.values()),
        )

        # 渠道
        chs = raw["channels"]
        self.channels = chs
        self.ch_ids = np.array([c["id"] for c in chs], dtype=np.int16)
        self.ch_idx = {c["id"]: i for i, c in enumerate(chs)}
        self.ch_quality = np.array([c["quality"] for c in chs], dtype=np.float32)
        self.ch_pay_quality = np.array([c["pay_quality"] for c in chs], dtype=np.float32)
        self.ch_share = np.array([c["share"] for c in chs], dtype=np.float64)
        self.test_ch_pos = self.ch_idx[9999]
        self.douyin_pos = self.ch_idx[4001]
        self.acc_base = 1_000_000       # account_id = idx + acc_base
        self.role_offset = 50_000_000   # role_id = idx + role_offset（1账号1角色）

        # 付费商品：普通/大R 两套下单结构（键集合须一致，对齐成同序数组）
        pn, pw = raw["product_weights_normal"], raw["product_weights_whale"]
        assert set(pn) == set(pw), "product_weights_normal/whale 键不一致"
        self.pay_prod_ids = list(pn.keys())
        self.pay_p_normal = np.array([float(pn[i]) for i in self.pay_prod_ids]); self.pay_p_normal /= self.pay_p_normal.sum()
        self.pay_p_whale = np.array([float(pw[i]) for i in self.pay_prod_ids]); self.pay_p_whale /= self.pay_p_whale.sum()

        # 商品
        self.products = raw["products"]
        self.prod_ids = [p["id"] for p in self.products]
        self.prod_price = {p["id"]: float(p["price"]) for p in self.products}
        self.prod_weights_normal = self._norm_weights(raw["product_weights_normal"])
        self.prod_weights_whale = self._norm_weights(raw["product_weights_whale"])
        self.first_pay_ids, self.first_pay_p = self._norm_weights(raw["first_pay_weights"])
        self.pay_cfg = raw["payment"]

        # 服务器开服时间 -> 开服日索引
        self.servers = raw["servers"]
        self.server_open_day = {_s["id"]: (_d(_s["open"]) - self.start).days for _s in self.servers}
        self.merges = raw.get("server_merge", [])

        # 版本轨迹：逐日 app_version 及最近版本
        vs = sorted(((_d(v["date"]), v["version"]) for v in raw["versions"]))
        self.versions = vs

        # 逐日倍率：dau / new / pay / ad，默认 1.0
        self.boost_dau = np.ones(self.n_days, dtype=np.float32)
        self.boost_new = np.ones(self.n_days, dtype=np.float32)
        self.boost_pay = np.ones(self.n_days, dtype=np.float32)
        self.boost_ad = np.ones(self.n_days, dtype=np.float32)
        for b in raw["boosts"]:
            self._apply_window(b["window"], lambda i: self.boost_dau.__setitem__(i, self.boost_dau[i] * b["dau"]))
            self._apply_window(b["window"], lambda i: self.boost_new.__setitem__(i, self.boost_new[i] * b["new"]))
            self._apply_window(b["window"], lambda i: self.boost_pay.__setitem__(i, self.boost_pay[i] * b["pay"]))
            self._apply_window(b["window"], lambda i: self.boost_ad.__setitem__(i, self.boost_ad[i] * b["ad"]))

        # 买量窗口：新增倍率 + 抖音渠道占比
        self.campaign = {}  # day -> (new_mult, douyin_share or None)
        for c in raw["buy_campaigns"]:
            for i in self._window_days(c["window"]):
                self.campaign[i] = (c["new_mult"], c.get("douyin_share"))

        # 事故窗口
        inc = raw["incident"]
        self.incident_days = set(self._window_days(inc["window"]))
        self.incident_new_mult = inc["new_retention_mult"]
        self.incident_dau_mult = inc["dau_mult"]

        # 获取参数
        acq = raw["acquisition"]
        self.launch_spike = acq["launch_spike"]
        self.organic_initial = acq["organic_initial"]
        self.organic_base = acq["organic_base"]
        self.organic_decay_days = acq["organic_decay_days"]
        wd = acq["new_weekend_boost"]
        dw = acq["dau_weekend_boost"]
        self.new_weekend = np.array(
            [1.0, 1.0, 1.0, 1.0, 1.0, wd["sat"], wd["sun"]], dtype=np.float32)
        self.dau_weekend = np.array(
            [1.0, 1.0, 1.0, 1.0, 1.0, dw["sat"], dw["sun"]], dtype=np.float32)

        self.user_profile = raw["user_profile"]
        self.session = raw["session"]
        self.property_flow = raw["property_flow"]
        self.ad_watch = raw["ad_watch"]
        self.dirty = raw["dirty"]
        self.activities = raw["activities"]

        # 每日活动集合（day -> bool），供活动奖励发放
        self.activity_days = np.zeros(self.n_days, dtype=bool)
        for a in self.activities:
            for i in self._window_days([a["start"], a["end"]]):
                self.activity_days[i] = True

    # ---------- helpers ----------
    @staticmethod
    def _norm_weights(w: dict):
        ids = list(w.keys())
        p = np.array([float(w[i]) for i in ids], dtype=np.float64)
        p = p / p.sum()
        return ids, p

    def _window_days(self, window):
        a, b = _d(window[0]), _d(window[1])
        return range((a - self.start).days, (b - self.start).days + 1)

    def _apply_window(self, window, fn):
        for i in self._window_days(window):
            fn(i)

    def organic_new(self, t: int) -> float:
        """第 t 天的自然新增量（未乘倍率）。"""
        if t < len(self.launch_spike):
            return float(self.launch_spike[t])
        decay = math.exp(-(t - len(self.launch_spike)) / self.organic_decay_days)
        return self.organic_base + (self.organic_initial - self.organic_base) * decay

    def version_at(self, t: int):
        """第 t 天的(最新版本号, 发布日索引)。"""
        latest = None
        for vdate, ver in self.versions:
            if (vdate - self.start).days <= t:
                latest = (ver, (vdate - self.start).days)
        return latest


def load_config() -> Config:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return Config(yaml.safe_load(f))
