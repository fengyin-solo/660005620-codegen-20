"""流水线健康评分引擎。

设计原则:
- 只读执行历史, 不参与调度与重试逻辑;
- 权重与等级分界可在运行时调整, 每次调整生成不可变的配置版本;
- 评分结果以快照形式保存, 记录生成时使用的配置版本, 之后配置再怎么改,
  旧快照的分数与等级都保持原值;
- 数据不完整时不拍脑袋给分: 没有执行记录 -> NO_DATA; 执行中途被打断 ->
  按已完成部分折算并在 notes 中说明; 个别指标缺数据 -> 该指标不参与加权,
  在 breakdown 中标记 excluded=true 并给出原因。
"""
import time
import uuid
import threading
from collections import deque, defaultdict

# ---- 指标键名, 同时也是前端展示的标识 ----
METRIC_ERRORS = "errors"      # 出错次数
METRIC_RETRIES = "retries"    # 重试比例
METRIC_DURATION = "duration"  # 整体处理时长

METRIC_LABELS = {
    METRIC_ERRORS: "出错次数",
    METRIC_RETRIES: "重试比例",
    METRIC_DURATION: "整体处理时长",
}

# ---- 默认评分配置 (可通过 API 调整) ----
DEFAULT_CONFIG = {
    "window": 10,              # 统计窗口: 最近 N 次执行
    "weights": {               # 三项指标权重, 无需等于 1, 计算时归一化
        METRIC_ERRORS: 0.4,
        METRIC_RETRIES: 0.3,
        METRIC_DURATION: 0.3,
    },
    # duration 评分基准: 实际平均耗时达到基准的多少倍时得 0 分 (线性)
    "durationBaseline": "planned",  # planned=以该流水线的计划时长为基准
    "durationMaxRatio": 2.0,
    # 等级分界: 按分数从高到低匹配, min 为该等级的下限 (含)
    "grades": [
        {"grade": "A", "label": "健康", "min": 85},
        {"grade": "B", "label": "良好", "min": 70},
        {"grade": "C", "label": "一般", "min": 55},
        {"grade": "D", "label": "较差", "min": 40},
        {"grade": "E", "label": "危险", "min": 0},
    ],
}

# 运行状态
STATUS_SCORED = "SCORED"
STATUS_NO_DATA = "NO_DATA"
STATUS_INTERRUPTED = "INTERRUPTED_PARTIAL"  # 窗口内仅含被打断的执行, 仍可折算


class HealthConfig:
    """不可变的评分配置版本。"""

    def __init__(self, version_id, created_at, params, note=""):
        self.version_id = version_id
        self.created_at = created_at
        self.note = note
        # 深拷贝一份, 后续外部修改不影响历史版本
        self.params = json_safe(params)

    def to_dict(self):
        return {
            "versionId": self.version_id,
            "createdAt": self.created_at,
            "note": self.note,
            "params": self.params,
        }


def json_safe(obj):
    """简单的 JSON 风格深拷贝 (配置/快照里都是基础类型)。"""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def validate_params(params):
    """校验并归一化配置参数, 不合法时抛 ValueError。"""
    if not isinstance(params, dict):
        raise ValueError("配置必须是对象")

    window = params.get("window", DEFAULT_CONFIG["window"])
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ValueError("window 必须是正整数")

    weights_in = params.get("weights", DEFAULT_CONFIG["weights"])
    if not isinstance(weights_in, dict):
        raise ValueError("weights 必须是对象")
    weights = {}
    for key in (METRIC_ERRORS, METRIC_RETRIES, METRIC_DURATION):
        w = weights_in.get(key, 0)
        if not isinstance(w, (int, float)) or isinstance(w, bool) or w < 0:
            raise ValueError(f"权重 {METRIC_LABELS[key]} 必须是非负数")
        weights[key] = float(w)
    if sum(weights.values()) <= 0:
        raise ValueError("至少需要一个正权重")

    duration_max_ratio = params.get("durationMaxRatio", DEFAULT_CONFIG["durationMaxRatio"])
    if not isinstance(duration_max_ratio, (int, float)) or isinstance(duration_max_ratio, bool) or duration_max_ratio <= 0:
        raise ValueError("durationMaxRatio 必须是正数")

    grades_in = params.get("grades", DEFAULT_CONFIG["grades"])
    if not isinstance(grades_in, list) or not grades_in:
        raise ValueError("grades 至少要有一个等级")
    grades = []
    seen_mins = set()
    for g in grades_in:
        if not isinstance(g, dict) or "grade" not in g or "min" not in g:
            raise ValueError("每个等级需要 grade 与 min 字段")
        mn = g["min"]
        if not isinstance(mn, (int, float)) or isinstance(mn, bool) or not (0 <= mn <= 100):
            raise ValueError("等级分界 min 必须在 0~100 之间")
        if mn in seen_mins:
            raise ValueError(f"等级分界 {mn} 重复")
        seen_mins.add(mn)
        grades.append({"grade": str(g["grade"]),
                       "label": str(g.get("label", "")),
                       "min": float(mn)})
    grades.sort(key=lambda x: -x["min"])
    if grades[-1]["min"] != 0:
        raise ValueError("最低等级的 min 必须为 0, 保证任何分数都有等级")

    return {
        "window": window,
        "weights": weights,
        "durationBaseline": params.get("durationBaseline", DEFAULT_CONFIG["durationBaseline"]),
        "durationMaxRatio": float(duration_max_ratio),
        "grades": grades,
    }


def grade_for_score(score, grades):
    """分数 -> 等级, 从高分到低分匹配。"""
    for g in sorted(grades, key=lambda x: -x["min"]):
        if score >= g["min"]:
            return {"grade": g["grade"], "label": g["label"]}
    last = sorted(grades, key=lambda x: -x["min"])[-1]
    return {"grade": last["grade"], "label": last["label"]}


def _clamp01(x):
    return max(0.0, min(1.0, x))


def compute_score(executions, planned_duration, params):
    """根据窗口内执行记录计算评分。

    executions: list[dict], 每个元素为一次执行的汇总:
      - totalTasks: 计划任务数
      - completedTasks: 已完成任务数
      - attempts: 任务尝试次数 (首次执行 + 重试)
      - errors: 执行中出错次数 (触发重试的失败 + 未完成终态失败)
      - retries: 重试次数
      - duration: 实际耗时 (秒); 被打断时为已消耗时间
      - interrupted: bool
      - completedAt / startedAt: 时间戳
      - missingMetrics: 该次执行中采集中断的指标列表
    planned_duration: 该流水线计划总时长 (秒), 为 None 表示时长基准缺失
    返回: (result_dict or None)。None 表示没有任何执行记录 (NO_DATA)。
    """
    if not executions:
        return None

    window = params["window"]
    windowed = executions[-window:]

    # ---- 出错次数指标 ----
    # 窗口内总出错次数, 按"每百次任务尝试的出错数"折算到 0~100:
    # 0 次错误满分; 每 10 次尝试 1 次错误(10%)扣到 0。
    err_attempts = sum(e.get("attempts") or 0 for e in windowed
                       if METRIC_ERRORS not in (e.get("missingMetrics") or []))
    err_errors = sum(e.get("errors") or 0 for e in windowed
                     if METRIC_ERRORS not in (e.get("missingMetrics") or []))
    err_present = err_attempts > 0
    if err_present:
        error_rate = err_errors / err_attempts
        error_score = round((1.0 - _clamp01(error_rate / 0.10)) * 100, 1)
    else:
        error_rate = None
        error_score = None

    # ---- 重试比例指标 ----
    # 重试次数 / 总尝试次数; 0 重试满分, 重试占比 30% 扣到 0。
    retry_present = err_attempts > 0 and all(
        METRIC_RETRIES not in (e.get("missingMetrics") or []) for e in windowed
    )
    if retry_present:
        retries = sum(e.get("retries") or 0 for e in windowed
                      if METRIC_RETRIES not in (e.get("missingMetrics") or []))
        retry_rate = retries / err_attempts
        retry_score = round((1.0 - _clamp01(retry_rate / 0.30)) * 100, 1)
    else:
        retries = None
        retry_rate = None
        retry_score = None

    # ---- 整体处理时长指标 ----
    # 被打断的执行没有真实总时长, 按已完成任务比例外推估算:
    # estimated = 已消耗时长 / 完成比例, 并在 notes 中标注折算。
    max_ratio = params["durationMaxRatio"]
    durations = []  # (估算时长, 是否折算)
    duration_notes = []
    for e in windowed:
        if METRIC_DURATION in (e.get("missingMetrics") or []):
            continue
        d = e.get("duration")
        if d is None:
            continue
        frac = ((e.get("completedTasks") or 0) / e["totalTasks"]) if e.get("totalTasks") else 0
        if e.get("interrupted"):
            if frac > 0:
                est = d / frac
                durations.append((est, True))
                duration_notes.append(
                    f"执行 {e.get('runId','?')} 中途被打断, 已完成 {e.get('completedTasks',0)}/"
                    f"{e.get('totalTasks')} ({frac*100:.0f}%), 耗时按已完成部分折算 "
                    f"{d:.1f}s→{est:.1f}s")
            else:
                duration_notes.append(
                    f"执行 {e.get('runId','?')} 中途被打断且无已完成任务, 时长样本不采用")
        else:
            durations.append((d, False))

    duration_present = bool(durations) and planned_duration and planned_duration > 0
    if duration_present:
        avg_duration = sum(d for d, _ in durations) / len(durations)
        ratio = avg_duration / planned_duration
        duration_score = round((1.0 - _clamp01((ratio - 1.0) / (max_ratio - 1.0))) * 100, 1)
    else:
        avg_duration = None
        ratio = None
        duration_score = None

    # ---- 加权 (缺失指标不参与, 权重在可用指标间重新归一化) ----
    weights = params["weights"]
    factor_map = {
        METRIC_ERRORS: (error_score, err_present,
                        None if err_present else "统计窗口内各次执行均缺少尝试/出错记录"),
        METRIC_RETRIES: (retry_score, retry_present,
                         None if retry_present else "窗口内缺少尝试次数或重试次数记录, 无法计算重试比例"),
        METRIC_DURATION: (duration_score, duration_present,
                          None if duration_present else
                          ("缺少执行时长记录" if not durations else "缺少流水线计划时长基准")),
    }

    total_weight = 0.0
    weighted_sum = 0.0
    breakdown = []
    for key, (score, present, absent_reason) in factor_map.items():
        item = {
            "metric": key,
            "label": METRIC_LABELS[key],
            "weight": weights.get(key, 0),
            "included": bool(present),
        }
        if present:
            total_weight += weights.get(key, 0)
            weighted_sum += score * weights.get(key, 0)
            item["score"] = score
            if key == METRIC_ERRORS:
                item["detail"] = f"{err_errors} 次出错 / {err_attempts} 次尝试 (出错率 {error_rate*100:.1f}%)"
            elif key == METRIC_RETRIES:
                item["detail"] = f"{retries} 次重试 / {err_attempts} 次尝试 (重试占比 {retry_rate*100:.1f}%)"
            else:
                est_tag = " (含打断折算)" if any(p for _, p in durations) else ""
                item["detail"] = f"平均 {avg_duration:.1f}s / 计划 {planned_duration:.1f}s ({ratio:.2f}x){est_tag}"
        else:
            item["excludedReason"] = absent_reason
        breakdown.append(item)

    notes = list(duration_notes)
    if total_weight <= 0:
        return {
            "status": "NO_DATA",
            "score": None,
            "grade": None,
            "windowUsed": len(windowed),
            "windowSize": window,
            "breakdown": breakdown,
            "notes": notes + ["三项指标均缺少可用数据, 不予评分"],
            "rawStats": {
                "runs": len(windowed),
                "interruptedRuns": sum(1 for e in windowed if e.get("interrupted")),
                "errors": err_errors if err_present else None,
                "retries": retries if retry_present else None,
                "attempts": err_attempts if err_present else None,
                "avgDuration": round(avg_duration, 2) if avg_duration is not None else None,
            },
        }

    score = round(weighted_sum / total_weight, 1)
    g = grade_for_score(score, params["grades"])
    interrupted_runs = sum(1 for e in windowed if e.get("interrupted"))
    return {
        "status": STATUS_INTERRUPTED if interrupted_runs else STATUS_SCORED,
        "score": score,
        "grade": g["grade"],
        "gradeLabel": g["label"],
        "windowUsed": len(windowed),
        "windowSize": window,
        "breakdown": breakdown,
        "notes": notes,
        "rawStats": {
            "runs": len(windowed),
            "interruptedRuns": interrupted_runs,
            "errors": err_errors if err_present else None,
            "retries": retries if retry_present else None,
            "attempts": err_attempts if err_present else None,
            "avgDuration": round(avg_duration, 2) if avg_duration is not None else None,
        },
    }


class HealthStore:
    """执行历史 + 评分快照 + 配置版本的内存存储 (线程安全)。

    快照一旦生成就冻结: 记录了 configVersion 与当时的 score/grade,
    配置更新或手动刷新都不会改写旧快照。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._pipelines = {}          # pipeline_id -> {name, plannedDuration, runs: deque}
        self._config_versions = {}    # version_id -> HealthConfig (按时间有序)
        self._config_order = []
        self._snapshots = {}          # pipeline_id -> {version_id -> snapshot}
        self._snapshot_order = defaultdict(list)
        # 最新配置版本
        active = HealthConfig(self._new_version_id(), time.time(),
                              validate_params(DEFAULT_CONFIG), note="初始默认配置")
        self._config_versions[active.version_id] = active
        self._config_order.append(active.version_id)

    @staticmethod
    def _new_version_id():
        return uuid.uuid4().hex[:12]

    # ---------- 流水线与执行历史 ----------
    def register_pipeline(self, pipeline_id, name, planned_duration):
        with self._lock:
            if pipeline_id not in self._pipelines:
                self._pipelines[pipeline_id] = {
                    "pipelineId": pipeline_id,
                    "name": name,
                    "plannedDuration": planned_duration,
                    "runs": deque(maxlen=200),
                }
            else:
                self._pipelines[pipeline_id]["name"] = name
                if planned_duration is not None:
                    self._pipelines[pipeline_id]["plannedDuration"] = planned_duration

    def record_run(self, pipeline_id, name, planned_duration, run_summary):
        """登记一次执行的汇总数据。只写历史, 不触发评分 (评分显式刷新)。"""
        with self._lock:
            self.register_pipeline(pipeline_id, name, planned_duration)
            run_summary = json_safe(run_summary)
            run_summary.setdefault("recordedAt", time.time())
            self._pipelines[pipeline_id]["runs"].append(run_summary)
            return run_summary

    def get_runs(self, pipeline_id):
        with self._lock:
            p = self._pipelines.get(pipeline_id)
            return list(p["runs"]) if p else []

    def list_pipelines(self):
        with self._lock:
            return [{"pipelineId": pid,
                     "name": p["name"],
                     "plannedDuration": p["plannedDuration"],
                     "runCount": len(p["runs"])}
                    for pid, p in self._pipelines.items()]

    # ---------- 配置版本 ----------
    def get_active_config(self):
        with self._lock:
            return self._config_versions[self._config_order[-1]]

    def list_config_versions(self):
        with self._lock:
            active_id = self._config_order[-1]
            return [
                dict(self._config_versions[vid].to_dict(),
                     active=(vid == active_id))
                for vid in reversed(self._config_order)
            ]

    def update_config(self, params, note=""):
        """调整权重/等级分界/窗口: 生成新版本, 旧版本与旧评分原样保留。"""
        normalized = validate_params(params)
        with self._lock:
            cfg = HealthConfig(self._new_version_id(), time.time(), normalized, note=note)
            self._config_versions[cfg.version_id] = cfg
            self._config_order.append(cfg.version_id)
            return cfg

    def get_config(self, version_id):
        with self._lock:
            return self._config_versions.get(version_id)

    # ---------- 评分快照 ----------
    def _build_snapshot(self, pipeline_id, cfg):
        p = self._pipelines.get(pipeline_id)
        if p is None or not p["runs"]:
            result = {
                "status": "NO_DATA",
                "score": None,
                "grade": None,
                "windowUsed": 0,
                "windowSize": cfg.params["window"],
                "breakdown": [
                    {"metric": k, "label": METRIC_LABELS[k],
                     "weight": cfg.params["weights"].get(k, 0),
                     "included": False,
                     "excludedReason": "统计窗口内没有执行记录, 该指标未参与计算"}
                    for k in (METRIC_ERRORS, METRIC_RETRIES, METRIC_DURATION)
                ],
                "notes": ["统计窗口内没有执行记录, 暂无数据, 不进行评分"],
                "rawStats": {"runs": 0, "interruptedRuns": 0},
            }
        else:
            result = compute_score(list(p["runs"]), p["plannedDuration"], cfg.params)

        return {
            "pipelineId": pipeline_id,
            "pipelineName": p["name"] if p else pipeline_id,
            "configVersion": cfg.version_id,
            "generatedAt": time.time(),
            **result,
        }

    def refresh_pipeline(self, pipeline_id, config_version=None):
        """用指定版本(默认当前版本)重新计算并生成一张新快照, 旧快照不动。"""
        with self._lock:
            if pipeline_id not in self._pipelines:
                self.register_pipeline(pipeline_id, pipeline_id, None)
            cfg = self._config_versions[config_version] if config_version else self.get_active_config()
            snapshot = self._build_snapshot(pipeline_id, cfg)
            bucket = self._snapshots.setdefault(pipeline_id, {})
            bucket[cfg.version_id] = snapshot
            if cfg.version_id not in self._snapshot_order[pipeline_id]:
                self._snapshot_order[pipeline_id].append(cfg.version_id)
            return snapshot

    def refresh_all(self):
        with self._lock:
            cfg = self.get_active_config()
            return [self.refresh_pipeline(pid, cfg.version_id)
                    for pid in list(self._pipelines.keys())]

    def list_scores(self):
        """评分列表: 每条流水线展示其最新一张快照 (可能是旧配置版本生成的)。

        配置调整后不自动重算, 因此这里返回的旧评分保持原值,
        staleCurrentConfig 标明它不是用当前配置版本算出来的。
        """
        with self._lock:
            active = self.get_active_config()
            out = []
            # 有历史但从未评过分的流水线也要出现在列表里: 暂无数据
            pipeline_ids = set(self._pipelines.keys()) | set(self._snapshots.keys())
            for pid in sorted(pipeline_ids):
                p = self._pipelines.get(pid)
                bucket = self._snapshots.get(pid, {})
                if bucket:
                    latest_vid = self._snapshot_order[pid][-1]
                    snap = json_safe(bucket[latest_vid])
                else:
                    snap = {
                        "pipelineId": pid,
                        "pipelineName": p["name"] if p else pid,
                        "configVersion": None,
                        "generatedAt": None,
                        "status": "NO_DATA",
                        "score": None,
                        "grade": None,
                        "windowUsed": 0,
                        "windowSize": active.params["window"],
                        "breakdown": [
                            {"metric": k, "label": METRIC_LABELS[k],
                             "weight": active.params["weights"].get(k, 0),
                             "included": False,
                             "excludedReason": "统计窗口内没有执行记录, 该指标未参与计算"}
                            for k in (METRIC_ERRORS, METRIC_RETRIES, METRIC_DURATION)
                        ],
                        "notes": ["统计窗口内没有执行记录, 暂无数据, 不进行评分"],
                        "rawStats": {"runs": 0, "interruptedRuns": 0},
                    }
                snap["staleCurrentConfig"] = snap["configVersion"] != active.version_id
                snap["runCount"] = len(p["runs"]) if p else 0
                out.append(snap)
            return out

    def get_score_history(self, pipeline_id, limit=20):
        with self._lock:
            bucket = self._snapshots.get(pipeline_id, {})
            vids = self._snapshot_order.get(pipeline_id, [])[-limit:]
            return [json_safe(bucket[v]) for v in vids]


# 单例
health_store = HealthStore()
