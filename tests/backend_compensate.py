"""后端补偿操作验证（无需 Flask）：用最小 stub 导入 app.py，驱动实际路由。

覆盖补偿时间线末条普通提交的完整语义（真实经过 admit_experiment 准入门控、
script_lock / timeline_lock 与真实 SQLite，不另写模拟准入逻辑）：

- 基本流程：甲 100 +5 +7 提交后补偿 seq=1，已提交值恢复 100，
  追加系统记录 112→100、增量 [-7,-5]、compensates=1，原记录字段不变；
- 两栏事务 / 只读模式 / 最近一次显式读值 / 读快照不受补偿影响，
  已有读快照仍见旧值；
- 过期目标（空时间线、seq 不是末条）、补偿记录不可再补偿、脚本有效
  一律 409；非法 seq（非正整数 / 非整数类型）一律 400；
- 写锁冲突立即返回 SQLITE_BUSY(5)，失败不改库也不改时间线；
- 同一 seq 并发补偿最多成功一次；装载等待在途补偿收尾；
- 大整数无损（十进制字符串）；删除准入（410）与未知 ID（404）。
"""
import sys, types, json, os, re, tempfile, threading

# ---- 最小 Flask stub：路由注册 / 请求体注入 / 响应包装（仅 Web 框架管道） ----
flask = types.ModuleType("flask")


class _Resp:
    """仿 Flask 响应：视图返回 jsonify(...) 或 (jsonify(...), status)。"""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def get_json(self):
        return self._payload


def _jsonify(*args, **kwargs):
    if args and kwargs:
        raise TypeError("jsonify: cannot mix positional and keyword")
    if len(args) == 1:
        payload = args[0]
    elif args:
        payload = list(args)
    else:
        payload = kwargs
    return _Resp(payload)


def _compile_path(path):
    # /api/experiments/<exp_id>/conn/<side>/op → 命名分组正则
    return re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", path) + "$")


class _App:
    def __init__(self, name):
        self.routes = []  # [(method, regex, view_fn)]，按注册顺序匹配

    def _reg(self, method, path):
        def deco(fn):
            self.routes.append((method, _compile_path(path), fn))
            return fn
        return deco

    def get(self, path, **k):
        return self._reg("GET", path)

    def post(self, path, **k):
        return self._reg("POST", path)

    def delete(self, path, **k):
        return self._reg("DELETE", path)

    def run(self, **k):
        pass


class _Req(threading.local):
    # 请求体按线程隔离：并发交错测试里各线程看到各自的 JSON body
    def __init__(self):
        self._json = None

    def get_json(self, silent=False):
        return self._json


flask.Flask = _App
flask.jsonify = _jsonify
flask.render_template = lambda *a, **k: "html"
flask.request = _Req()
sys.modules["flask"] = flask


class TestClient:
    """最小路由分发：直接调用 app.py 注册的实际视图函数。

    请求真实经过 admit_experiment 准入门控与全部实验级锁；
    这里只替代 Flask 的 URL 匹配与请求 / 响应包装，不另写模拟准入逻辑。
    """

    def __init__(self, app):
        self.app = app

    def open(self, method, path, json_body=None):
        flask.request._json = json_body
        try:
            for registered_method, rx, fn in self.app.routes:
                if registered_method != method:
                    continue
                match = rx.match(path)
                if match:
                    return self._normalize(fn(**match.groupdict()))
            raise AssertionError("no route: %s %s" % (method, path))
        finally:
            flask.request._json = None

    @staticmethod
    def _normalize(rv):
        if isinstance(rv, tuple):
            resp, status = rv[0], rv[1]
            resp.status_code = status
            return resp
        if isinstance(rv, _Resp):
            return rv
        return _Resp(rv)

    def get(self, path):
        return self.open("GET", path)

    def post(self, path, json=None):
        return self.open("POST", path, json)

    def delete(self, path):
        return self.open("DELETE", path)


_tmp = tempfile.mkdtemp()
os.environ["LAB_DATA_DIR"] = _tmp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import app as A

fails = []
def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: fails.append(name)


client = TestClient(A.app)
ROUTE_TIMEOUT = 5.0  # 正常路由调用为毫秒级；超时即视为「卡住」回归


def create_exp():
    resp = client.post("/api/experiments")
    assert resp.status_code == 200
    return resp.get_json()["id"]


def op_url(exp_id, side):
    return "/api/experiments/%s/conn/%s/op" % (exp_id, side)


def comp_url(exp_id):
    return "/api/experiments/%s/compensate" % exp_id


def committed(exp_id):
    resp = client.get("/api/experiments/%s/committed" % exp_id)
    assert resp.status_code == 200
    return resp.get_json()["committed"]


def timeline(exp_id):
    resp = client.get("/api/experiments/%s/timeline" % exp_id)
    assert resp.status_code == 200
    return resp.get_json()["timeline"]


def op(exp_id, side, operation, value=None):
    body = {"op": operation}
    if value is not None:
        body["value"] = value
    resp = client.post(op_url(exp_id, side), body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


# ================= 基本流程：100 +5 +7 提交 → 补偿 → 值回 100 =================
exp1 = create_exp()
op(exp1, "a", "begin")
op(exp1, "a", "add", 5)
op(exp1, "a", "add", 7)
op(exp1, "a", "commit")
check("基本·已提交值112", committed(exp1) == 112, str(committed(exp1)))
tl = timeline(exp1)
check("基本·时间线1条普通记录", len(tl) == 1 and tl[0]["side"] == "a"
      and tl[0]["before"] == 100 and tl[0]["after"] == 112
      and tl[0]["deltas"] == [5, 7], str(tl))
check("基本·普通记录字段不变",
      set(tl[0]) == {"seq", "side", "before", "after", "deltas"}, str(sorted(tl[0])))

resp = client.post(comp_url(exp1), {"seq": 1})
body = resp.get_json()
check("基本·补偿200", resp.status_code == 200 and body["ok"] is True, str(body))
check("基本·响应字段约定", set(body) == {"ok", "error", "record", "timeline"},
      str(sorted(body)))
rec = body["record"]
check("基本·补偿记录seq顺延", rec["seq"] == 2, str(rec))
check("基本·补偿记录side=system", rec["side"] == "system")
check("基本·补偿记录112→100", rec["before"] == 112 and rec["after"] == 100, str(rec))
check("基本·补偿记录逆序取负[-7,-5]", rec["deltas"] == [-7, -5], str(rec))
check("基本·补偿记录compensates=1", rec["compensates"] == 1, str(rec))
check("基本·补偿后已提交值100", committed(exp1) == 100, str(committed(exp1)))
tl = timeline(exp1)
check("基本·原记录保留且字段不变", len(tl) == 2
      and set(tl[0]) == {"seq", "side", "before", "after", "deltas"}
      and tl[0]["before"] == 100 and tl[0]["after"] == 112
      and tl[0]["deltas"] == [5, 7], str(tl))
check("基本·时间线第2条为补偿记录", tl[1]["side"] == "system"
      and tl[1]["compensates"] == 1 and tl[1]["deltas"] == [-7, -5], str(tl[1]))

# ---- 过期目标：同一 seq 不可再补偿；补偿记录不可再补偿；不存在的 seq ----
resp = client.post(comp_url(exp1), {"seq": 1})
check("过期·同一seq再补偿409", resp.status_code == 409 and "error" in resp.get_json())
resp = client.post(comp_url(exp1), {"seq": 2})
check("过期·补偿记录不可再补偿409", resp.status_code == 409)
resp = client.post(comp_url(exp1), {"seq": 99})
check("过期·不存在的seq 409", resp.status_code == 409)
check("过期·拒绝后时间线不变", len(timeline(exp1)) == 2)
check("过期·拒绝后已提交值不变", committed(exp1) == 100)

# ---- 非法 seq：一律 400，不执行 ----
exp_bad = create_exp()
op(exp_bad, "a", "begin")
op(exp_bad, "a", "add", 1)
op(exp_bad, "a", "commit")
for bad in (0, -1, 1.5, "1", True, None):
    resp = client.post(comp_url(exp_bad), {"seq": bad})
    check("非法·seq=%r 400" % bad, resp.status_code == 400)
resp = client.post(comp_url(exp_bad), {})
check("非法·缺seq字段400", resp.status_code == 400)
resp = client.post(comp_url(exp_bad), None)
check("非法·非JSON body 400", resp.status_code == 400)
check("非法·时间线未变", len(timeline(exp_bad)) == 1)

# ---- 空时间线：409 ----
exp_empty = create_exp()
resp = client.post(comp_url(exp_empty), {"seq": 1})
check("空时间线·补偿409", resp.status_code == 409)

# ================= 两栏事务 / 只读 / 显式读值 / 读快照不受补偿影响 =================
exp2 = create_exp()
op(exp2, "a", "begin")
op(exp2, "a", "add", 5)
op(exp2, "a", "add", 7)
op(exp2, "a", "commit")  # #1: 100→112 [5,7]
# 乙：开启事务并显式读值（建立读快照），开启只读模式
op(exp2, "b", "begin")
r = op(exp2, "b", "read")
check("快照·乙读到112", r["state"]["last_read"] == 112, str(r["state"]))
resp = client.post("/api/experiments/%s/conn/b/readonly" % exp2, {"enabled": True})
check("快照·乙只读开启", resp.get_json()["ok"] is True)
# 甲：开启一个空事务（无快照、无写入）
op(exp2, "a", "begin")

resp = client.post(comp_url(exp2), {"seq": 1})
check("快照·补偿成功", resp.status_code == 200 and resp.get_json()["ok"] is True)
check("快照·已提交值回100", committed(exp2) == 100)
st = client.get("/api/experiments/%s/status" % exp2).get_json()
check("快照·乙事务仍开启", st["b"]["in_transaction"] is True)
check("快照·乙只读模式不变", st["b"]["readonly"] is True)
check("快照·乙最近显式读值不变", st["b"]["last_read"] == 112)
check("快照·甲事务仍开启", st["a"]["in_transaction"] is True)
# 乙的读快照仍见旧值（补偿在独立连接提交，不触碰乙的快照）
r = op(exp2, "b", "read")
check("快照·已有读快照仍见旧值112", r["state"]["last_read"] == 112,
      str(r["state"]))
op(exp2, "b", "rollback")
op(exp2, "a", "rollback")
# 回滚后乙读到补偿后的新值
op(exp2, "b", "begin")
r = op(exp2, "b", "read")
check("快照·新事务读到100", r["state"]["last_read"] == 100, str(r["state"]))
op(exp2, "b", "rollback")

# ================= 写锁冲突：立即返回 SQLite 错误名 / 码，不改库或时间线 =================
exp3 = create_exp()
op(exp3, "a", "begin")
op(exp3, "a", "add", 10)
op(exp3, "a", "commit")  # #1: 100→110 [10]
op(exp3, "a", "begin")
op(exp3, "a", "add", 3)  # 甲持有写锁，未提交
resp = client.post(comp_url(exp3), {"seq": 1})
body = resp.get_json()
check("写锁·冲突返回200+ok:false", resp.status_code == 200 and body["ok"] is False,
      str(body))
check("写锁·错误名SQLITE_BUSY", body["error"]["name"] == "SQLITE_BUSY", str(body["error"]))
check("写锁·错误码5", body["error"]["code"] == 5)
check("写锁·失败不改库", committed(exp3) == 110)
check("写锁·失败不改时间线", len(timeline(exp3)) == 1)
# 甲提交后（#2: 110→113 [3]），旧目标过期、新末条可补偿
op(exp3, "a", "commit")
resp = client.post(comp_url(exp3), {"seq": 1})
check("写锁·交错后旧seq过期409", resp.status_code == 409)
resp = client.post(comp_url(exp3), {"seq": 2})
body = resp.get_json()
check("写锁·新末条补偿成功", resp.status_code == 200 and body["ok"] is True)
check("写锁·补偿113→110 [-3]", body["record"]["before"] == 113
      and body["record"]["after"] == 110
      and body["record"]["deltas"] == [-3], str(body["record"]))
check("写锁·已提交值回110", committed(exp3) == 110)

# ================= 连续补偿：补偿后再提交，可再补偿新末条 =================
exp4 = create_exp()
op(exp4, "a", "begin")
op(exp4, "a", "add", 20)
op(exp4, "a", "commit")  # #1: 100→120 [20]
client.post(comp_url(exp4), {"seq": 1})  # #2: system 120→100 [-20]
op(exp4, "b", "begin")
op(exp4, "b", "add", 1)
op(exp4, "b", "commit")  # #3: 100→101 [1]
resp = client.post(comp_url(exp4), {"seq": 3})
body = resp.get_json()
check("连续·补偿新末条成功", resp.status_code == 200 and body["ok"] is True)
check("连续·记录101→100 [-1] compensates=3",
      body["record"]["seq"] == 4 and body["record"]["before"] == 101
      and body["record"]["after"] == 100 and body["record"]["deltas"] == [-1]
      and body["record"]["compensates"] == 3, str(body["record"]))
check("连续·已提交值回100", committed(exp4) == 100)
check("连续·时间线4条", len(timeline(exp4)) == 4)

# ================= 大整数无损 =================
exp5 = create_exp()
op(exp5, "a", "begin")
op(exp5, "a", "add", "9007199254740993")
op(exp5, "a", "commit")  # #1: 100→9007199254741093
tl = timeline(exp5)
check("大整数·普通记录字符串编码", tl[0]["after"] == "9007199254741093"
      and tl[0]["deltas"] == ["9007199254740993"], str(tl))
resp = client.post(comp_url(exp5), {"seq": 1})
body = resp.get_json()
check("大整数·补偿成功", resp.status_code == 200 and body["ok"] is True)
rec = body["record"]
check("大整数·补偿记录无损", rec["before"] == "9007199254741093"
      and rec["after"] == 100 and rec["deltas"] == ["-9007199254740993"],
      str(rec))
check("大整数·已提交值精确回100", committed(exp5) == 100, str(committed(exp5)))

# ================= 脚本有效期间：补偿 409；取消后恢复 =================
exp6 = create_exp()
op(exp6, "a", "begin")
op(exp6, "a", "add", 5)
op(exp6, "a", "commit")  # #1
resp = client.post("/api/experiments/%s/script" % exp6,
                   [{"side": "a", "op": "begin"}])
sid6 = resp.get_json()["script"]["id"]
resp = client.post(comp_url(exp6), {"seq": 1})
check("脚本·有效期间补偿409", resp.status_code == 409 and "error" in resp.get_json())
check("脚本·补偿未执行", committed(exp6) == 105 and len(timeline(exp6)) == 1)
client.post("/api/experiments/%s/script/cancel" % exp6,
            {"script_id": sid6, "step": 1})
resp = client.post(comp_url(exp6), {"seq": 1})
check("脚本·取消后补偿恢复", resp.status_code == 200 and resp.get_json()["ok"] is True)

# ================= 装载等待在途补偿收尾 =================
# 把该实例的 compensate 按在「在途」状态（已准入、尚未执行），由测试放行，
# 确定性验证：装载在 script_lock 内等待 compensate_active 归零。
exp7 = create_exp()
op(exp7, "a", "begin")
op(exp7, "a", "add", 5)
op(exp7, "a", "commit")  # #1
real_compensate = A.Experiment.compensate
exp_obj7 = A.experiments[exp7]
gate_entered = threading.Event()
gate_release = threading.Event()


def slow_compensate(self, seq):
    if self.id == exp7:
        gate_entered.set()
        gate_release.wait(ROUTE_TIMEOUT)
    return real_compensate(self, seq)


A.Experiment.compensate = slow_compensate
try:
    box = {}

    def do_comp():
        box["comp"] = client.post(comp_url(exp7), {"seq": 1})

    t_comp = threading.Thread(target=do_comp, daemon=True)
    t_comp.start()
    check("装载交错·补偿在途", gate_entered.wait(ROUTE_TIMEOUT))
    box["load"] = None

    def do_load():
        box["load"] = client.post("/api/experiments/%s/script" % exp7,
                                  [{"side": "a", "op": "read"}])

    t_load = threading.Thread(target=do_load, daemon=True)
    t_load.start()
    t_load.join(0.5)
    check("装载交错·装载等待在途补偿（未抢先成功）", t_load.is_alive())
    gate_release.set()
    t_comp.join(ROUTE_TIMEOUT)
    t_load.join(ROUTE_TIMEOUT)
    check("装载交错·补偿完成200", box["comp"].status_code == 200
          and box["comp"].get_json()["ok"] is True)
    check("装载交错·补偿收尾后装载成功200", box["load"].status_code == 200)
    sid7 = box["load"].get_json()["script"]["id"]
    # 装载成功后脚本有效：补偿 409
    resp = client.post(comp_url(exp7), {"seq": 2})
    check("装载交错·装载成功后补偿409", resp.status_code == 409)
    client.post("/api/experiments/%s/script/cancel" % exp7,
                {"script_id": sid7, "step": 1})
finally:
    A.Experiment.compensate = real_compensate

# ================= 同一 seq 并发补偿：最多成功一次 =================
race_ok = True
for _ in range(20):
    exp_r = create_exp()
    op(exp_r, "a", "begin")
    op(exp_r, "a", "add", 1)
    op(exp_r, "a", "commit")  # #1
    barrier = threading.Barrier(2)
    results = {}

    def do_comp1():
        barrier.wait()
        results["c1"] = client.post(comp_url(exp_r), {"seq": 1})

    def do_comp2():
        barrier.wait()
        results["c2"] = client.post(comp_url(exp_r), {"seq": 1})

    threads = [threading.Thread(target=do_comp1, daemon=True),
               threading.Thread(target=do_comp2, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        race_ok = False
        break
    r1, r2 = results["c1"], results["c2"]
    ok_count = sum(
        1 for r in (r1, r2)
        if r.status_code == 200 and r.get_json()["ok"] is True
    )
    stale_count = sum(1 for r in (r1, r2) if r.status_code == 409)
    if ok_count != 1 or stale_count != 1:
        race_ok = False
        break
    if committed(exp_r) != 100 or len(timeline(exp_r)) != 2:
        race_ok = False
        break
    if client.delete("/api/experiments/%s" % exp_r).status_code != 200:
        race_ok = False
        break
check("并发·同一seq补偿最多成功一次(20轮)", race_ok)

# ================= 删除准入与未知 ID =================
exp8 = create_exp()
op(exp8, "a", "begin")
op(exp8, "a", "add", 5)
op(exp8, "a", "commit")
resp = client.delete("/api/experiments/%s" % exp8)
check("删除·删除成功", resp.status_code == 200 and resp.get_json()["deleted"] is True)
resp = client.post(comp_url(exp8), {"seq": 1})
check("删除·已删除实验补偿410", resp.status_code == 410)
resp = client.post(comp_url("does-not-exist"), {"seq": 1})
check("删除·未知实验补偿404", resp.status_code == 404)

# 收尾：删除其余实验，确认没有请求占住准入名额
clean_ok = True
for e in (exp1, exp_bad, exp_empty, exp2, exp3, exp4, exp5, exp6, exp7):
    resp = client.delete("/api/experiments/%s" % e)
    clean_ok = clean_ok and resp.status_code == 200
check("收尾·全部实验删除完成（名额均已归还）", clean_ok)

print()
print(f"{len(fails)} 失败" if fails else "后端补偿操作测试全部通过")
sys.exit(1 if fails else 0)
