import asyncio, time, random, json, threading
from typing import Optional, List
from collections import defaultdict, deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app.health import health_store, METRIC_ERRORS, METRIC_RETRIES, METRIC_DURATION

app = FastAPI(title="DAG Workflow Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ACTIVE_CLIENTS = []
WORKFLOW_ID = 0

class WorkflowCreate(BaseModel):
    name: str = "data-pipeline"

class RunRequest(BaseModel):
    workflowId: int
    workers: int = 3
    strategy: str = "fifo"


def generate_dag_workflow(name: str):
    """Create a realistic DAG pipeline"""
    nodes = [
        {"id": "extract", "name": "数据提取", "deps": [], "duration": 2.0},
        {"id": "validate", "name": "数据校验", "deps": ["extract"], "duration": 1.5},
        {"id": "clean_a", "name": "清洗分支A", "deps": ["validate"], "duration": 1.8},
        {"id": "clean_b", "name": "清洗分支B", "deps": ["validate"], "duration": 1.2},
        {"id": "transform", "name": "数据转换", "deps": ["clean_a"], "duration": 3.0},
        {"id": "enrich", "name": "数据增强", "deps": ["clean_a", "clean_b"], "duration": 2.0},
        {"id": "aggregate", "name": "聚合计算", "deps": ["transform", "enrich"], "duration": 2.5},
        {"id": "quality", "name": "质量检查", "deps": ["aggregate"], "duration": 1.0},
        {"id": "export_db", "name": "入库", "deps": ["quality"], "duration": 1.8},
        {"id": "export_report", "name": "报表生成", "deps": ["quality"], "duration": 2.2},
        {"id": "notify", "name": "通知", "deps": ["export_db", "export_report"], "duration": 0.5},
    ]
    positions = [
        (0, 0), (0, 1), (-1, 2), (1, 2), (-1, 3),
        (0.5, 3), (-0.3, 4), (-0.3, 5), (-1, 6), (0.5, 6), (-0.3, 7)
    ]
    for i, n in enumerate(nodes):
        n["x"] = positions[i][0] * 2.5 + 2.5
        n["y"] = positions[i][1] * 0.9
        n["status"] = "PENDING"
        n["retries"] = 0
        n["startTime"] = None
        n["endTime"] = None

    edges = []
    for n in nodes:
        for d in n["deps"]:
            edges.append([d, n["id"]])

    return {"nodes": [{
        "id": n["id"], "name": n["name"], "deps": n["deps"],
        "x": n["x"], "y": n["y"], "status": n["status"],
        "startTime": None, "endTime": None, "retries": n["retries"]
    } for n in nodes], "edges": edges, "durations": {n["id"]: n["duration"] for n in nodes}}


def planned_duration(nodes, edges, durations):
    """关键路径长度 (无限 Worker 时的计划总时长), 作为健康评分的时长基准。"""
    adj = defaultdict(list)
    indeg = defaultdict(int)
    ids = [n["id"] for n in nodes]
    for u, v in edges:
        adj[u].append(v)
        indeg[v] += 1
    dist = {tid: float(durations.get(tid, 0)) for tid in ids}
    queue = deque([tid for tid in ids if indeg[tid] == 0])
    indeg_work = dict(indeg)
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            dist[v] = max(dist[v], dist[u] + float(durations.get(v, 0)))
            indeg_work[v] -= 1
            if indeg_work[v] == 0:
                queue.append(v)
    return round(max(dist.values()) if dist else 0, 3)


@app.post("/api/workflow")
def create_workflow(req: WorkflowCreate):
    global WORKFLOW_ID
    WORKFLOW_ID += 1
    dag = generate_dag_workflow(req.name)
    # 在健康评分中心登记流水线 (名称 + 计划时长基准), 不影响任何调度逻辑
    health_store.register_pipeline(
        str(WORKFLOW_ID), req.name,
        planned_duration(dag["nodes"], dag["edges"], dag["durations"]))
    return {"id": WORKFLOW_ID, "name": req.name, "nodes": dag["nodes"], "edges": dag["edges"],
            "_durations": dag["durations"]}


# 正在执行的 run: run_id -> cancel_event
ACTIVE_RUNS = {}


@app.post("/api/run")
def run_workflow(req: RunRequest):
    dag = generate_dag_workflow("workflow")
    run_id = f"run-{int(time.time()*1000)}-{random.randint(1000,9999)}"
    cancel_event = threading.Event()
    ACTIVE_RUNS[run_id] = cancel_event
    t = threading.Thread(target=execute_workflow,
                         args=(dag, req.workers, req.strategy, str(req.workflowId), run_id, cancel_event),
                         daemon=True)
    t.start()
    return {
        "runId": run_id,
        "workflow": {"id": req.workflowId, "name": "workflow", "nodes": dag["nodes"], "edges": dag["edges"]},
        "logs": [], "circuitBreakers": [], "completed": False
    }


@app.post("/api/run/{run_id}/interrupt")
def interrupt_run(run_id: str):
    """请求中断一次执行。调度循环在当前周期收尾后停止, 已完成部分计入历史。"""
    event = ACTIVE_RUNS.get(run_id)
    if event is None:
        raise HTTPException(status_code=404, detail="执行不存在或已结束")
    event.set()
    return {"runId": run_id, "interrupting": True}


def execute_workflow(dag, workers, strategy, pipeline_id, run_id, cancel_event):
    nodes = dag["nodes"]
    durations = dag["durations"]
    edges = dag["edges"]
    in_degree = defaultdict(int)
    adj = defaultdict(list)
    for u, v in edges:
        in_degree[v] += 1
        adj[u].append(v)

    # BFS topological sort
    ready = deque([n["id"] for n in nodes if in_degree[n["id"]] == 0])
    node_map = {n["id"]: n for n in nodes}
    logs = []
    cb_state = defaultdict(lambda: {"failureCount": 0, "state": "CLOSED", "cooldownUntil": 0})
    failure_threshold = 3
    running_tasks = {}
    completed = set()

    # 健康评分所需的只读统计 (不改变既有调度/重试行为)
    started_at = time.time()
    stat_attempts = 0
    stat_errors = 0
    stat_retries = 0
    interrupted = False

    def send_update(completed_flag=False):
        payload = {
            "runId": run_id,
            "workflow": {"id": int(pipeline_id), "name": "workflow", "nodes": nodes, "edges": edges},
            "logs": logs[-30:],
            "circuitBreakers": [{"taskId": k, **v} for k, v in cb_state.items()],
            "completed": completed_flag,
            "interrupted": interrupted,
        }
        for ws in ACTIVE_CLIENTS:
            try: asyncio.run_coroutine_threadsafe(ws.send_text(json.dumps(payload)), asyncio.get_event_loop())
            except: pass
        time.sleep(0.3)

    while ready or running_tasks:
        # Start tasks
        while ready and len(running_tasks) < workers:
            tid = ready.popleft()
            node = node_map[tid]
            cb = cb_state[tid]
            if cb["state"] == "OPEN" and time.time() < cb["cooldownUntil"]:
                ready.appendleft(tid)
                continue
            if cb["state"] == "OPEN":
                cb["state"] = "HALF_OPEN"

            node["status"] = "RUNNING"
            node["startTime"] = time.time()
            stat_attempts += 1

            # Simulate task execution (random success/failure)
            will_fail = random.random() < 0.12  # 12% failure rate
            runtime = durations.get(tid, 1.5) * random.uniform(0.7, 1.3)
            running_tasks[tid] = {
                "end_time": time.time() + runtime,
                "will_fail": will_fail,
                "retries": node["retries"]
            }
            logs.append({"taskId": tid, "status": "RUNNING", "timestamp": time.time(), "message": f"开始执行 {node['name']}"})

        # Check completed tasks
        now = time.time()
        finished = []
        for tid, info in running_tasks.items():
            if now >= info["end_time"]:
                node = node_map[tid]
                if info["will_fail"] and node["retries"] < 3:
                    node["retries"] += 1
                    node["status"] = "PENDING"
                    ready.appendleft(tid)
                    stat_errors += 1
                    stat_retries += 1
                    cb = cb_state[tid]
                    cb["failureCount"] += 1
                    logs.append({"taskId": tid, "status": "FAILED", "timestamp": now, "message": f"重试 {node['retries']}/3"})
                    if cb["failureCount"] >= failure_threshold:
                        cb["state"] = "OPEN"
                        cb["cooldownUntil"] = now + 5
                        logs.append({"taskId": tid, "status": "CIRCUIT_OPEN", "timestamp": now, "message": f"熔断! {failure_threshold}次连续失败"})
                else:
                    if info["will_fail"]:
                        # 重试用尽后的失败仍计为一次出错 (调度行为保持原样)
                        stat_errors += 1
                    node["status"] = "SUCCESS"
                    node["endTime"] = now
                    completed.add(tid)
                    cb_state[tid]["failureCount"] = 0
                    cb_state[tid]["state"] = "CLOSED"
                    logs.append({"taskId": tid, "status": "SUCCESS", "timestamp": now, "message": f"完成 {node['name']}"})
                    for next_tid in adj[tid]:
                        in_degree[next_tid] -= 1
                        if in_degree[next_tid] == 0:
                            ready.append(next_tid)
                finished.append(tid)

        for tid in finished:
            del running_tasks[tid]

        # 中断请求: 未完成任务标记为 INTERRUPTED, 保留已完成部分后退出
        if cancel_event.is_set():
            interrupted = True
            for tid, info in running_tasks.items():
                node_map[tid]["status"] = "INTERRUPTED"
                node_map[tid]["endTime"] = now
            for tid, n in node_map.items():
                if n["status"] in ("PENDING", "RUNNING"):
                    n["status"] = "INTERRUPTED"
            logs.append({"taskId": "-", "status": "INTERRUPTED", "timestamp": now,
                         "message": f"执行被手动中断, 已完成 {len(completed)}/{len(nodes)}"})
            send_update(True)
            break

        send_update()
        if len(completed) == len(nodes):
            break

    # 汇总本次执行并写入历史, 然后按"当前配置版本"生成一张新评分快照;
    # 旧快照不会被覆盖或重算。
    ended_at = time.time()
    run_summary = {
        "runId": run_id,
        "startedAt": started_at,
        "completedAt": ended_at,
        "totalTasks": len(nodes),
        "completedTasks": len(completed),
        "attempts": stat_attempts,
        "errors": stat_errors,
        "retries": stat_retries,
        "duration": round(ended_at - started_at, 3),
        "interrupted": interrupted,
        "missingMetrics": [],
    }
    health_store.record_run(pipeline_id, "workflow",
                            planned_duration(nodes, edges, durations), run_summary)
    health_store.refresh_pipeline(pipeline_id)
    ACTIVE_RUNS.pop(run_id, None)
    if not interrupted:
        send_update(True)


# ===================== 流水线健康评分 API =====================

class HealthConfigUpdate(BaseModel):
    params: dict
    note: str = ""


@app.get("/api/health/scores")
def get_health_scores():
    """评分列表: 每条流水线的最新评分快照 + 数据完整性说明。"""
    return {"activeConfig": health_store.get_active_config().to_dict(),
            "pipelines": health_store.list_scores()}


@app.post("/api/health/refresh")
def refresh_health_scores(pipelineId: Optional[str] = None):
    """用当前配置版本重新评分。旧快照保留在历史中、数值不变。"""
    if pipelineId:
        return health_store.refresh_pipeline(pipelineId)
    return {"pipelines": health_store.refresh_all()}


@app.get("/api/health/config")
def get_health_config():
    return {"active": health_store.get_active_config().to_dict(),
            "versions": health_store.list_config_versions()}


@app.put("/api/health/config")
def update_health_config(req: HealthConfigUpdate):
    """调整权重 / 等级分界 / 统计窗口。生成不可变新版本, 历史评分不受影响。"""
    try:
        cfg = health_store.update_config(req.params, note=req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return cfg.to_dict()


@app.get("/api/health/scores/{pipeline_id}/history")
def get_score_history(pipeline_id: str, limit: int = 20):
    """某条流水线的评分快照历史, 用于验证配置调整前后旧评分保持原值。"""
    return {"pipelineId": pipeline_id,
            "snapshots": health_store.get_score_history(pipeline_id, limit)}


@app.get("/api/pipelines/{pipeline_id}/runs")
def get_pipeline_runs(pipeline_id: str):
    return {"pipelineId": pipeline_id, "runs": health_store.get_runs(pipeline_id)}


class SeedRun(BaseModel):
    """演示/测试用: 手工写入一次执行记录, 可指定缺失的采集指标。"""
    pipelineId: str
    name: str = "seed-pipeline"
    plannedDuration: Optional[float] = 15.0
    totalTasks: int = 11
    completedTasks: int = 11
    attempts: int = 11
    errors: int = 0
    retries: int = 0
    duration: Optional[float] = 16.0
    interrupted: bool = False
    missingMetrics: List[str] = []


@app.post("/api/health/seed-run")
def seed_run(req: SeedRun):
    missing = [m for m in req.missingMetrics if m in (METRIC_ERRORS, METRIC_RETRIES, METRIC_DURATION)]
    run = {
        "runId": f"seed-{int(time.time()*1000)}-{random.randint(1000,9999)}",
        "startedAt": time.time(), "completedAt": time.time(),
        "totalTasks": req.totalTasks, "completedTasks": req.completedTasks,
        "attempts": req.attempts, "errors": req.errors, "retries": req.retries,
        "duration": req.duration, "interrupted": req.interrupted,
        "missingMetrics": missing,
    }
    health_store.record_run(req.pipelineId, req.name, req.plannedDuration, run)
    return health_store.refresh_pipeline(req.pipelineId)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ACTIVE_CLIENTS.append(ws)
    try:
        while True: await ws.receive_text()
    except:
        if ws in ACTIVE_CLIENTS: ACTIVE_CLIENTS.remove(ws)
