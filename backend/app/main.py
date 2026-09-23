import asyncio, time, random, json, threading
from collections import defaultdict, deque
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from . import health
except ImportError:
    import health

app = FastAPI(title="DAG Workflow Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ACTIVE_CLIENTS = []
WORKFLOW_ID = 0
MAIN_LOOP = None
STOP_EVENT = threading.Event()
RUN_STATE = {"active": False}

class WorkflowCreate(BaseModel):
    name: str = "data-pipeline"

class RunRequest(BaseModel):
    workflowId: int
    workers: int = 3
    strategy: str = "fifo"

class HealthConfigUpdate(BaseModel):
    w_error: Optional[float] = None
    w_retry: Optional[float] = None
    w_duration: Optional[float] = None
    grade_a: Optional[float] = None
    grade_b: Optional[float] = None
    grade_c: Optional[float] = None
    window_size: Optional[int] = None


@app.on_event("startup")
async def startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    health.init_db()


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


def expected_makespan(dag, workers):
    """基线耗时:关键路径与总工作量/Worker 数取大,作为时长环节的参照。"""
    nodes, edges, durations = dag["nodes"], dag["edges"], dag["durations"]
    adj = defaultdict(list)
    in_deg = defaultdict(int)
    for u, v in edges:
        adj[u].append(v)
        in_deg[v] += 1
    dist = {n["id"]: durations.get(n["id"], 1.5) for n in nodes}
    dq = deque([n["id"] for n in nodes if in_deg[n["id"]] == 0])
    while dq:
        u = dq.popleft()
        for v in adj[u]:
            dist[v] = max(dist[v], dist[u] + durations.get(v, 1.5))
            in_deg[v] -= 1
            if in_deg[v] == 0:
                dq.append(v)
    critical_path = max(dist.values()) if dist else 0.0
    work_bound = sum(durations.get(n["id"], 1.5) for n in nodes) / max(1, workers)
    return max(critical_path, work_bound)


@app.post("/api/workflow")
def create_workflow(req: WorkflowCreate):
    global WORKFLOW_ID
    WORKFLOW_ID += 1
    dag = generate_dag_workflow(req.name)
    health.register_workflow(WORKFLOW_ID, req.name)
    return {"id": WORKFLOW_ID, "name": req.name, "nodes": dag["nodes"], "edges": dag["edges"],
            "_durations": dag["durations"]}


@app.post("/api/run")
def run_workflow(req: RunRequest):
    dag = generate_dag_workflow("workflow")
    health.register_workflow(req.workflowId, f"workflow-{req.workflowId}")
    STOP_EVENT.clear()
    RUN_STATE["active"] = True
    t = threading.Thread(target=execute_workflow,
                         args=(dag, req.workers, req.strategy, req.workflowId), daemon=True)
    t.start()
    return {
        "workflow": {"id": req.workflowId, "name": "workflow", "nodes": dag["nodes"], "edges": dag["edges"]},
        "logs": [], "circuitBreakers": [], "completed": False
    }


@app.post("/api/stop")
def stop_workflow():
    """中断当前执行:已完成部分保留,剩余任务标记为 INTERRUPTED。"""
    if not RUN_STATE["active"]:
        return {"stopping": False, "message": "当前没有正在执行的流水线"}
    STOP_EVENT.set()
    return {"stopping": True, "message": "已发送中断信号"}


def execute_workflow(dag, workers, strategy, workflow_id):
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

    # 健康评分统计(仅记录,不影响调度与重试逻辑)
    started_at = time.time()
    expected = expected_makespan(dag, workers)
    stats = {"attempts": 0, "failed_attempts": 0, "retries": 0}
    interrupted = False

    def send_update(completed_flag=False):
        payload = {
            "workflow": {"id": workflow_id, "name": "workflow", "nodes": nodes, "edges": edges},
            "logs": logs[-30:],
            "circuitBreakers": [{"taskId": k, **v} for k, v in cb_state.items()],
            "completed": completed_flag
        }
        if MAIN_LOOP is not None:
            for ws in ACTIVE_CLIENTS:
                try: asyncio.run_coroutine_threadsafe(ws.send_text(json.dumps(payload)), MAIN_LOOP)
                except: pass
        time.sleep(0.3)

    try:
        while ready or running_tasks:
            if STOP_EVENT.is_set():
                interrupted = True
                break
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
                stats["attempts"] += 1

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
                    if info["will_fail"]:
                        stats["failed_attempts"] += 1
                    if info["will_fail"] and node["retries"] < 3:
                        node["retries"] += 1
                        stats["retries"] += 1
                        node["status"] = "PENDING"
                        ready.appendleft(tid)
                        cb = cb_state[tid]
                        cb["failureCount"] += 1
                        logs.append({"taskId": tid, "status": "FAILED", "timestamp": now, "message": f"重试 {node['retries']}/3"})
                        if cb["failureCount"] >= failure_threshold:
                            cb["state"] = "OPEN"
                            cb["cooldownUntil"] = now + 5
                            logs.append({"taskId": tid, "status": "CIRCUIT_OPEN", "timestamp": now, "message": f"熔断! {failure_threshold}次连续失败"})
                    else:
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

            send_update()
            if len(completed) == len(nodes):
                break

        if interrupted:
            for n in nodes:
                if n["status"] in ("PENDING", "RUNNING"):
                    n["status"] = "INTERRUPTED"
            logs.append({"taskId": "-", "status": "INTERRUPTED", "timestamp": time.time(),
                         "message": f"执行被中断,已完成 {len(completed)}/{len(nodes)} 个任务,将按完成度折算计入健康评分"})
            send_update(False)
        else:
            send_update(True)
    finally:
        RUN_STATE["active"] = False
        STOP_EVENT.clear()
        ended_at = time.time()
        health.record_execution({
            "workflow_id": workflow_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": "SUCCESS" if len(completed) == len(nodes) else "INTERRUPTED",
            "total_tasks": len(nodes),
            "completed_tasks": len(completed),
            "attempts": stats["attempts"],
            "failed_attempts": stats["failed_attempts"],
            "retries": stats["retries"],
            "duration": ended_at - started_at,
            "expected_duration": expected,
        })


# ---------------- 健康评分 ----------------

@app.get("/api/health")
def get_health():
    """每条流水线最近一次刷新的评分(含历史快照,历史值不回改)。"""
    return {"scores": health.latest_scores(), "config": health.get_config()}


@app.post("/api/health/refresh")
def refresh_health():
    """用当前配置重新计算所有流水线的评分,写入新快照;旧快照保持不变。"""
    return health.refresh_all()


@app.get("/api/health/config")
def get_health_config():
    return health.get_config()


@app.put("/api/health/config")
def put_health_config(req: HealthConfigUpdate):
    """调整权重/等级分界/统计窗口;只影响之后的刷新,历史评分保留原值。"""
    patch = {k: v for k, v in req.dict().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=400, detail="没有需要更新的配置项")
    try:
        return health.update_config(patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ACTIVE_CLIENTS.append(ws)
    try:
        while True: await ws.receive_text()
    except:
        if ws in ACTIVE_CLIENTS: ACTIVE_CLIENTS.remove(ws)
