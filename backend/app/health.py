"""流水线健康评分:执行记录持久化 + 评分计算。

设计约束:
- 评分只在显式刷新(POST /api/health/refresh)时计算并落库,
  历史评分快照永不修改;调整权重/等级分界只影响之后的刷新。
- 统计窗口内没有执行记录的流水线记为"暂无数据",不给 0 分。
- 中途被打断的执行按已完成部分(完成度)折算计入,并在备注中说明。
- 某个环节(出错/重试/时长)数据缺失时,该环节不参与计算,
  权重按可用环节归一化,并在备注中点明是哪一项。
"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "health.db")
_LOCK = threading.RLock()  # 可重入:refresh_all 持锁时会调用 compute_for_workflow

DEFAULT_CONFIG = {
    "w_error": 0.5,      # 出错环节权重
    "w_retry": 0.2,      # 重试环节权重
    "w_duration": 0.3,   # 时长环节权重
    "grade_a": 90.0,     # >= grade_a 为 A
    "grade_b": 75.0,     # >= grade_b 为 B
    "grade_c": 60.0,     # >= grade_c 为 C,否则 D
    "window_size": 10,   # 统计最近 N 次执行
}

NO_DATA_GRADE = "暂无数据"


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _LOCK, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS workflows (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            workflow_name TEXT NOT NULL,
            started_at REAL,
            ended_at REAL,
            status TEXT NOT NULL,            -- SUCCESS / INTERRUPTED
            total_tasks INTEGER NOT NULL,
            completed_tasks INTEGER NOT NULL,
            attempts INTEGER NOT NULL,       -- 任务尝试总次数(含重试)
            failed_attempts INTEGER NOT NULL,-- 出错的尝试次数
            retries INTEGER NOT NULL,        -- 重新入队的重试次数
            duration REAL,                   -- 实际耗时(秒),未知为 NULL
            expected_duration REAL           -- 基线耗时(关键路径/Worker 取大)
        );
        CREATE TABLE IF NOT EXISTS health_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS health_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL,
            workflow_name TEXT NOT NULL,
            computed_at REAL NOT NULL,
            window_size INTEGER NOT NULL,
            runs_in_window INTEGER NOT NULL,
            score REAL,                      -- 暂无数据时为 NULL
            grade TEXT NOT NULL,             -- A/B/C/D 或 暂无数据
            components TEXT NOT NULL,        -- JSON: 各环节明细(含是否参与计算)
            notes TEXT NOT NULL,             -- JSON: 说明列表(中断折算/缺失环节等)
            config_snapshot TEXT NOT NULL    -- 本次计算使用的配置快照
        );
        """)
        row = c.execute("SELECT id FROM health_config WHERE id = 1").fetchone()
        if not row:
            c.execute("INSERT INTO health_config (id, data, updated_at) VALUES (1, ?, ?)",
                      (json.dumps(DEFAULT_CONFIG), time.time()))


# ---------------- 配置 ----------------

def get_config():
    with _LOCK, _conn() as c:
        row = c.execute("SELECT data FROM health_config WHERE id = 1").fetchone()
    cfg = dict(DEFAULT_CONFIG)
    if row:
        cfg.update(json.loads(row["data"]))
    return cfg


def update_config(patch):
    """校验并保存配置;只影响之后的刷新,不回改历史评分。"""
    cfg = get_config()
    for key in DEFAULT_CONFIG:
        if key in patch and patch[key] is not None:
            cfg[key] = float(patch[key])
    if cfg["w_error"] < 0 or cfg["w_retry"] < 0 or cfg["w_duration"] < 0:
        raise ValueError("权重不能为负数")
    if cfg["w_error"] + cfg["w_retry"] + cfg["w_duration"] <= 0:
        raise ValueError("权重之和必须大于 0")
    if not (0 <= cfg["grade_c"] <= cfg["grade_b"] <= cfg["grade_a"] <= 100):
        raise ValueError("等级分界需满足 0 <= C <= B <= A <= 100")
    cfg["window_size"] = int(cfg["window_size"])
    if not (1 <= cfg["window_size"] <= 100):
        raise ValueError("统计窗口需在 1~100 之间")
    with _LOCK, _conn() as c:
        c.execute("UPDATE health_config SET data = ?, updated_at = ? WHERE id = 1",
                  (json.dumps(cfg), time.time()))
    return cfg


# ---------------- 数据录入 ----------------

def register_workflow(workflow_id, name):
    with _LOCK, _conn() as c:
        c.execute("INSERT OR IGNORE INTO workflows (id, name, created_at) VALUES (?, ?, ?)",
                  (workflow_id, name, time.time()))


def workflow_name(workflow_id):
    with _LOCK, _conn() as c:
        row = c.execute("SELECT name FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return row["name"] if row else f"workflow-{workflow_id}"


def record_execution(rec):
    """执行引擎线程回调:记录一次执行(完整或中断)。任何异常都不应影响引擎。"""
    try:
        name = workflow_name(rec["workflow_id"])
        with _LOCK, _conn() as c:
            c.execute("""
                INSERT INTO executions
                (workflow_id, workflow_name, started_at, ended_at, status,
                 total_tasks, completed_tasks, attempts, failed_attempts, retries,
                 duration, expected_duration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rec["workflow_id"], name, rec.get("started_at"),
                  rec.get("ended_at"), rec["status"], rec["total_tasks"],
                  rec["completed_tasks"], rec["attempts"], rec["failed_attempts"],
                  rec["retries"], rec.get("duration"), rec.get("expected_duration")))
    except Exception:
        pass


# ---------------- 评分计算 ----------------

def _grade_of(score, cfg):
    if score >= cfg["grade_a"]:
        return "A"
    if score >= cfg["grade_b"]:
        return "B"
    if score >= cfg["grade_c"]:
        return "C"
    return "D"


def compute_for_workflow(workflow_id, workflow_name, cfg):
    """对单条流水线在最近窗口内的执行记录计算健康评分。"""
    window = int(cfg["window_size"])
    with _LOCK, _conn() as c:
        rows = c.execute("""
            SELECT * FROM executions WHERE workflow_id = ?
            ORDER BY id DESC LIMIT ?
        """, (workflow_id, window)).fetchall()

    notes = []
    components = {
        "error":    {"score": None, "weight": cfg["w_error"],    "available": False, "detail": ""},
        "retry":    {"score": None, "weight": cfg["w_retry"],    "available": False, "detail": ""},
        "duration": {"score": None, "weight": cfg["w_duration"], "available": False, "detail": ""},
    }
    if not rows:
        notes.append("统计窗口内没有执行记录")
        return {"score": None, "grade": NO_DATA_GRADE, "runs_in_window": 0,
                "components": components, "notes": notes}

    # 按完成度折算的权重:完整执行权重 1,中断执行权重 = 完成度
    w_attempts = 0.0                     # 加权后的总尝试次数
    err_num = ret_num = 0.0              # 加权后的出错/重试次数
    dur_score_sum = dur_w = 0.0          # 时长环节
    for r in rows:
        total = r["total_tasks"] or 0
        done = r["completed_tasks"] or 0
        if r["status"] == "INTERRUPTED":
            ratio = (done / total) if total > 0 else 0.0
            notes.append(f"执行 #{r['id']} 中途被打断,已按完成度 {ratio * 100:.1f}% 折算计入")
            if ratio <= 0:
                notes.append(f"执行 #{r['id']} 中断时完成度为 0,本次未纳入统计")
                continue
            w = ratio
        else:
            w = 1.0

        attempts = r["attempts"] or 0
        if attempts > 0:
            w_attempts += w * attempts
            err_num += w * r["failed_attempts"]
            ret_num += w * r["retries"]

        duration = r["duration"]
        expected = r["expected_duration"]
        if duration is not None and duration > 0 and expected and expected > 0:
            dur_score_sum += w * min(1.0, expected / duration)
            dur_w += w

    # 出错环节:出错率 = 出错尝试 / 总尝试(按完成度加权)
    if w_attempts > 0:
        err_rate = err_num / w_attempts
        components["error"]["available"] = True
        components["error"]["score"] = round(100 * (1 - min(1.0, err_rate)), 1)
        components["error"]["detail"] = f"出错率 {err_rate * 100:.1f}%"
    else:
        notes.append("出错环节数据缺失(窗口内无有效任务尝试),未参与计算")

    # 重试环节:重试比例 = 重试次数 / 总尝试(按完成度加权)
    if w_attempts > 0:
        retry_ratio = ret_num / w_attempts
        components["retry"]["available"] = True
        components["retry"]["score"] = round(100 * (1 - min(1.0, retry_ratio)), 1)
        components["retry"]["detail"] = f"重试比例 {retry_ratio * 100:.1f}%"
    else:
        notes.append("重试环节数据缺失(窗口内无有效任务尝试),未参与计算")

    # 时长环节:实际耗时相对基线的达成率
    if dur_w > 0:
        dur_score = 100 * dur_score_sum / dur_w
        components["duration"]["available"] = True
        components["duration"]["score"] = round(dur_score, 1)
        components["duration"]["detail"] = f"时长达成率 {dur_score:.1f}%"
    else:
        notes.append("时长环节数据缺失,未参与计算")

    # 汇总:只用有数据的环节,权重归一化
    avail = [k for k, v in components.items() if v["available"]]
    if not avail:
        notes.append("所有环节数据均缺失,无法计算评分")
        return {"score": None, "grade": NO_DATA_GRADE, "runs_in_window": len(rows),
                "components": components, "notes": notes}
    if len(avail) < 3:
        names = {"error": "出错", "retry": "重试", "duration": "时长"}
        notes.append("仅 " + "/".join(names[k] for k in avail) + " 环节参与计算,权重已归一化")
    w_sum = sum(components[k]["weight"] for k in avail)
    score = sum(components[k]["score"] * components[k]["weight"] for k in avail) / w_sum
    score = round(score, 1)
    return {"score": score, "grade": _grade_of(score, cfg), "runs_in_window": len(rows),
            "components": components, "notes": notes}


# ---------------- 刷新与查询 ----------------

def refresh_all():
    """用当前配置为所有流水线计算评分并写入新快照;历史快照保持不变。"""
    cfg = get_config()
    now = time.time()
    with _LOCK, _conn() as c:
        workflows = c.execute("SELECT id, name FROM workflows ORDER BY id").fetchall()
        row = c.execute("SELECT COALESCE(MAX(batch_id), 0) AS b FROM health_scores").fetchone()
        batch_id = row["b"] + 1
        results = []
        for wf in workflows:
            r = compute_for_workflow(wf["id"], wf["name"], cfg)
            c.execute("""
                INSERT INTO health_scores
                (batch_id, workflow_id, workflow_name, computed_at, window_size,
                 runs_in_window, score, grade, components, notes, config_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (batch_id, wf["id"], wf["name"], now, int(cfg["window_size"]),
                  r["runs_in_window"], r["score"], r["grade"],
                  json.dumps(r["components"]), json.dumps(r["notes"], ensure_ascii=False),
                  json.dumps(cfg)))
            results.append({"workflowId": wf["id"], "workflowName": wf["name"], **r})
    return {"batchId": batch_id, "computedAt": now, "scores": results}


def latest_scores(history_limit=5):
    """每条流水线的最新评分 + 最近若干次历史评分(历史值原样返回,证明不被回改)。"""
    with _LOCK, _conn() as c:
        workflows = c.execute("SELECT id, name FROM workflows ORDER BY id").fetchall()
        out = []
        for wf in workflows:
            rows = c.execute("""
                SELECT * FROM health_scores WHERE workflow_id = ?
                ORDER BY id DESC LIMIT ?
            """, (wf["id"], history_limit)).fetchall()
            if not rows:
                continue
            latest = rows[0]
            out.append({
                "workflowId": wf["id"],
                "workflowName": latest["workflow_name"],
                "score": latest["score"],
                "grade": latest["grade"],
                "runsInWindow": latest["runs_in_window"],
                "windowSize": latest["window_size"],
                "computedAt": latest["computed_at"],
                "components": json.loads(latest["components"]),
                "notes": json.loads(latest["notes"]),
                "history": [{"score": r["score"], "grade": r["grade"],
                             "computedAt": r["computed_at"]} for r in rows],
            })
    return out
