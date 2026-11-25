# -*- coding: utf-8 -*-
"""K 线演化的只读 Web 接口（`ai_trader/web/kbar_api.py`）。

这一层要钉的不是"能不能取到数"，而是三条容易出事的性质：

一、**登录**。这三个端点读的是全库历史，未登录不该看到；加蓝图时最容易漏。
二、**只读**。`KBarStore.__init__` 会建表/切 WAL（它是写库的），所以 Web 层
   自己开 `mode=ro` 连接。这条必须用"真的去写一次、必须失败"来证明，
   不能只看代码里写了 `mode=ro` —— 字面量写对了但没生效的情况有的是。
三、**then / post 不许混**。表 6/6b/表 3 是看未来的。接口把它们放在
   `post` 里、把当时可见的放 `then` 里，前端才有机会分开展示。
   一旦有人把 `preds` 挪进 `then`，这个回看页面就变成偷看答案的入口 ——
   所以这里连"两侧各有哪些键"都钉死。

所有断言都对着**库里的真值**比，不写死数字：数据回填后数字会变，
写死就会逼着后来的人改测试而不是改代码。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "kbar.db")
pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="没有 data/kbar.db")

SYM = "BTCUSDT"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from flask import Flask
    from ai_trader.web.auth import auth_bp
    from ai_trader.web.kbar_api import kbar_bp

    monkeypatch.setenv("AI_SECRET_KEY", "test-secret")
    a = Flask(__name__)
    a.secret_key = "test-secret"
    a.config["KBAR_DB"] = DB
    # auth_bp 也要注册：`login_required` 会 `url_for("auth.login")`，
    # 不注册的话未登录请求变成 500 而不是 302，"需要登录"这条就测不准了。
    a.register_blueprint(auth_bp)
    a.register_blueprint(kbar_bp)
    return a


@pytest.fixture()
def c(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
    return c


def _direct(sql, args=()):
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- 一、登录
@pytest.mark.parametrize("path", [
    "/admin/api/kbar/meta?interval=15m",
    "/admin/api/kbar/evo?interval=15m",
    "/admin/api/kbar/snapshot?interval=15m&ts=1794101400000",
])
def test_all_endpoints_require_login(app, path):
    r = app.test_client().get(path, follow_redirects=False)
    assert r.status_code == 302, "%s 未登录竟然能直接取数" % path
    assert "/login" in r.headers["Location"]


# ---------------------------------------------------------------- 二、只读
def test_readonly_connection_really_refuses_writes(app):
    """真的去写一次，必须失败。

    `KBarStore` 的 `__init__` 会 `executescript(SCHEMA)` + `commit()`，
    所以 Web 层绝不能复用它 —— 那等于给每个 HTTP 请求一次写库的机会。
    这里直接证明 `_ro()` 拿到的连接写不进去。
    """
    from ai_trader.web import kbar_api as K

    with app.test_request_context("/"):
        con = K._ro()
        try:
            with pytest.raises(sqlite3.OperationalError) as ei:
                con.execute("CREATE TABLE _probe_should_fail (x INTEGER)")
            assert "readonly" in str(ei.value).lower(), str(ei.value)
            with pytest.raises(sqlite3.OperationalError):
                con.execute("DELETE FROM kbar_meta")
            # 读是通的（否则上一条会因为"表不存在"之类的原因假通过）
            assert con.execute("SELECT COUNT(*) FROM kbar_meta").fetchone()[0] >= 0
        finally:
            con.close()


def test_missing_db_gives_503(tmp_path, monkeypatch):
    from flask import Flask
    from ai_trader.web.auth import auth_bp
    from ai_trader.web.kbar_api import kbar_bp

    monkeypatch.setenv("AI_SECRET_KEY", "test-secret")
    a = Flask(__name__)
    a.secret_key = "x"
    a.config["KBAR_DB"] = str(tmp_path / "nope.db")
    a.register_blueprint(auth_bp)
    a.register_blueprint(kbar_bp)
    c = a.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
    r = c.get("/admin/api/kbar/meta?interval=15m")
    assert r.status_code == 503
    assert r.get_json()["ok"] is False


# ---------------------------------------------------------------- meta
@pytest.mark.parametrize("iv,ms", [("5m", 300000), ("15m", 900000)])
def test_meta_matches_db_exactly(c, iv, ms):
    j = c.get("/admin/api/kbar/meta?interval=" + iv).get_json()
    assert j["ok"] and not j["empty"]
    assert j["interval"] == iv and j["interval_ms"] == ms

    n, lo, hi = _direct(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM kbar_evolution "
        "WHERE symbol=? AND interval_ms=? AND o IS NOT NULL", (SYM, ms))[0]
    assert (j["bars"], j["first_ts"], j["last_ts"]) == (n, lo, hi)

    n_shape = _direct("SELECT COUNT(*) FROM kbar_shape "
                      "WHERE symbol=? AND interval_ms=?", (SYM, ms))[0][0]
    assert j["shape_rows"] == n_shape

    # 结论词分布：每一条都必须是库里真实的 (label, count)
    real = dict(_direct("SELECT label, COUNT(*) FROM kbar_shape "
                        "WHERE symbol=? AND interval_ms=? GROUP BY label",
                        (SYM, ms)))
    got = {x["label"]: x["n"] for x in j["labels"]}
    assert got == real


def test_meta_rejects_unknown_interval(c):
    r = c.get("/admin/api/kbar/meta?interval=9m")
    assert r.status_code == 400
    j = r.get_json()
    assert j["ok"] is False and sorted(j["allowed"]) == ["15m", "5m"]


def test_meta_exposes_dim_catalog_counts(c):
    j = c.get("/admin/api/kbar/meta?interval=15m").get_json()
    from ai_trader.kbar import ledger as LG

    assert j["n_dims"] == len(LG.dim_catalog())
    assert j["judge_dims"] == sorted(LG.judge_dim_names())


# ---------------------------------------------------------------- evo
@pytest.mark.parametrize("iv,ms", [("5m", 300000), ("15m", 900000)])
@pytest.mark.parametrize("limit", [60, 300])
def test_evo_arrays_are_aligned_and_match_db(c, iv, ms, limit):
    """五条平行数组必须等长 —— 前端按下标取，错一格就整图错位。"""
    j = c.get("/admin/api/kbar/evo?interval=%s&limit=%d" % (iv, limit)
              ).get_json()
    assert j["ok"] and not j["empty"]
    n = len(j["bars"])
    for key in ("label", "states", "n_touch", "status"):
        assert len(j[key]) == n, "%s 与 bars 不齐（%d vs %d）" % (
            key, len(j[key]), n)

    # 时间升序、同一周期步长、与库里的尾部逐根对得上
    ts = [b[0] for b in j["bars"]]
    assert ts == sorted(ts), "bars 不是时间升序"
    assert all(ts[i + 1] - ts[i] == ms for i in range(len(ts) - 1)), \
        "相邻两根间隔不等于该周期"

    real = _direct("SELECT ts,o,c,l,h FROM kbar_evolution "
                   "WHERE symbol=? AND interval_ms=? AND o IS NOT NULL "
                   "ORDER BY ts DESC LIMIT ?", (SYM, ms, limit))[::-1]
    assert len(real) == n
    for got, want in zip(j["bars"], real):
        assert tuple(got) == tuple(want)


def test_evo_label_comes_only_from_shape_table(c):
    """label 必须是表 5 的原值，不许在这里二次判断。

    这个接口一旦自己"翻译"标签，演化层的判据就有第二个实现，
    两处迟早分叉 —— 而分叉只会体现在图上，没有任何报错。
    """
    j = c.get("/admin/api/kbar/evo?interval=15m&limit=200").get_json()
    ts = [b[0] for b in j["bars"]]
    real = dict(_direct(
        "SELECT ts, label FROM kbar_shape WHERE symbol=? AND interval_ms=? "
        "AND ts>=?", (SYM, 900000, ts[0])))
    for i, t in enumerate(ts):
        assert j["label"][i] == real.get(t), "第 %d 根的 label 与库里不一致" % i


def test_evo_reactions_can_be_switched_off(c):
    on = c.get("/admin/api/kbar/evo?interval=5m&limit=100&with_reactions=1"
               ).get_json()
    off = c.get("/admin/api/kbar/evo?interval=5m&limit=100&with_reactions=0"
                ).get_json()
    assert off["rx"] == []
    # 关掉触点不该改变 K 线本身
    assert on["bars"] == off["bars"]
    assert on["label"] == off["label"]


def test_evo_limit_is_clamped(c):
    j = c.get("/admin/api/kbar/evo?interval=15m&limit=999999").get_json()
    from ai_trader.web.kbar_api import MAX_LIMIT
    assert len(j["bars"]) <= MAX_LIMIT
    j2 = c.get("/admin/api/kbar/evo?interval=15m&limit=1").get_json()
    from ai_trader.web.kbar_api import DEFAULT_LIMIT
    assert len(j2["bars"]) >= min(50, DEFAULT_LIMIT)


def test_evo_rejects_non_integer_limit(c):
    r = c.get("/admin/api/kbar/evo?interval=15m&limit=abc")
    assert r.status_code == 400


# ---------------------------------------------------------- snapshot 分栏
def _pick_ts(c, iv="15m"):
    j = c.get("/admin/api/kbar/evo?interval=%s&limit=100" % iv).get_json()
    return j["bars"][len(j["bars"]) // 2][0]


THEN_KEYS = {"shape", "dims", "reactions", "events"}
POST_KEYS = {"preds", "survival", "forward"}


def test_snapshot_splits_then_and_post(c):
    """`then` 只放当时能算的，`post` 只放看未来的。键集合钉死。"""
    ts = _pick_ts(c)
    j = c.get("/admin/api/kbar/snapshot?interval=15m&ts=%d" % ts).get_json()
    assert j["ok"] and j["ts"] == ts
    assert set(j["then"]) == THEN_KEYS, "then 的键变了：%s" % sorted(j["then"])
    assert set(j["post"]) == POST_KEYS, "post 的键变了：%s" % sorted(j["post"])


def test_snapshot_post_never_leaks_into_then(c):
    """把 post 的内容塞进 then 是这类页面最危险的一种改法。

    这里逐个检查 then 里**不出现**事后字段名，并且 post 里确实有内容
    （否则"没有泄露"可能只是因为到处都空）。
    """
    ts = _pick_ts(c)
    j = c.get("/admin/api/kbar/snapshot?interval=15m&ts=%d" % ts).get_json()
    then_txt = repr(j["then"])
    for bad in ("verdict", "ret1", "ret3", "ret5", "must_hold",
                "break_n", "n_seen", "max_n"):
        assert bad not in then_txt, "then 里出现了事后字段 %r" % bad

    post = j["post"]
    assert post["preds"] or post["survival"] or post["forward"], \
        "post 全空 —— 这条测试就失去意义了"


def test_snapshot_shape_matches_shape_table(c):
    ts = _pick_ts(c)
    j = c.get("/admin/api/kbar/snapshot?interval=15m&ts=%d" % ts).get_json()
    real = _direct("SELECT label, states, evidence FROM kbar_shape "
                   "WHERE symbol=? AND interval_ms=? AND ts=?",
                   (SYM, 900000, ts))
    assert real, "库里没有这一根的表 5 行"
    assert j["then"]["shape"]["label"] == real[0][0]
    assert j["then"]["shape"]["states"] == real[0][1]
    assert j["then"]["shape"]["evidence"] == real[0][2]


def test_snapshot_dims_are_39_and_stars_are_8(c):
    ts = _pick_ts(c)
    j = c.get("/admin/api/kbar/snapshot?interval=15m&ts=%d" % ts).get_json()
    cat = j["dim_catalog"]
    assert len(cat) == 39, "维度数变了：%d" % len(cat)
    star = sorted(d["name"] for d in cat if d["starred"])
    from ai_trader.kbar import ledger as LG
    assert star == sorted(LG.judge_dim_names())
    assert len(star) == 8, "判据读的维度不是 8 个：%s" % star

    # dims 块自身的 n_dims 也要是 39（表 7 落地口径）
    dims = j["then"]["dims"]
    if dims:
        assert dims["n_dims"] == 39
        assert set(dims["values"]) == set(d["name"] for d in cat), \
            "values 的键与 dim_catalog 不一致"


def test_snapshot_404_for_unknown_ts(c):
    r = c.get("/admin/api/kbar/snapshot?interval=15m&ts=1000")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_snapshot_rejects_non_integer_ts(c):
    r = c.get("/admin/api/kbar/snapshot?interval=15m&ts=abc")
    assert r.status_code == 400
