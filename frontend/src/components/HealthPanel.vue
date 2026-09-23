<template>
  <div class="panel">
    <div class="head">
      <h4>🩺 流水线健康评分</h4>
      <div class="head-actions">
        <el-button size="small" text @click="store.fetchHealth()" :loading="store.healthLoading">刷新列表</el-button>
        <el-button size="small" type="primary" plain @click="refreshAll" :loading="refreshing">
          用当前配置重新评分
        </el-button>
        <el-button size="small" text @click="showConfig = true" title="调整统计窗口、权重与等级分界">⚙ 配置</el-button>
      </div>
    </div>

    <div class="score-list">
      <div v-for="p in pipelines" :key="p.pipelineId" class="score-card" :class="cardClass(p)">
        <div class="card-top">
          <div class="p-name">{{ p.pipelineName }} <span class="p-id">#{{ p.pipelineId }}</span></div>
          <div v-if="p.status === 'NO_DATA'" class="badge nodata">暂无数据</div>
          <div v-else class="badge" :class="gradeClass(p.grade)">
            <span class="grade">{{ p.grade }}</span>
            <span class="score">{{ p.score }}<span class="unit"> 分</span></span>
          </div>
        </div>

        <div class="meta">
          <span>统计窗口 {{ p.windowUsed }}/{{ p.windowSize }} 次执行</span>
          <span v-if="p.rawStats.interruptedRuns">· {{ p.rawStats.interruptedRuns }} 次中途打断</span>
          <el-tag v-if="p.status === 'INTERRUPTED_PARTIAL'" size="small" type="warning" effect="plain">
            按已完成部分折算
          </el-tag>
          <el-tag v-if="p.staleCurrentConfig" size="small" type="info" effect="plain">
            旧配置版本评分
          </el-tag>
        </div>

        <!-- 指标明细: 哪一项参与/没参与计算一目了然 -->
        <div class="factors">
          <div v-for="b in p.breakdown" :key="b.metric" class="factor" :class="{ excluded: !b.included }">
            <div class="factor-head">
              <span class="f-label">{{ b.label }}</span>
              <span class="f-weight">权重 {{ Math.round(b.weight * 100) }}%</span>
              <span v-if="b.included" class="f-score">{{ b.score }}</span>
              <span v-else class="f-na">未参与计算</span>
            </div>
            <div v-if="b.included" class="bar">
              <div class="bar-fill" :style="{ width: (b.score || 0) + '%', background: scoreColor(b.score || 0) }"></div>
            </div>
            <div v-if="b.included" class="f-detail">{{ b.detail }}</div>
            <div v-else class="f-reason">⚠ {{ b.excludedReason }}</div>
          </div>
        </div>

        <!-- 打断折算等说明 -->
        <div v-for="(n, i) in p.notes" :key="'n'+i" class="note">📝 {{ n }}</div>
      </div>

      <div v-if="!pipelines.length" class="empty">
        尚无流水线。创建 DAG 后, 执行记录会自动进入健康统计窗口。
      </div>
    </div>

    <!-- ============ 配置抽屉: 窗口 / 权重 / 等级分界 ============ -->
    <el-drawer v-model="showConfig" title="评分配置调整" direction="rtl" size="380px">
      <div class="cfg-body">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px">
          调整后会生成新的不可变配置版本；已有历史评分保留原值，列表中标记为“旧配置版本评分”，
          点击“重新评分”后才按新版本计算。调度与重试行为不受影响。
        </el-alert>

        <div class="cfg-section">
          <label>统计窗口（最近 N 次执行）</label>
          <el-input-number v-model="draft.window" :min="1" :max="100" size="small" />
        </div>

        <div class="cfg-section">
          <label>指标权重（无需凑满 100%，计算时按可用指标归一化）</label>
          <div v-for="k in metricKeys" :key="k" class="weight-row">
            <span>{{ metricNames[k] }}</span>
            <el-slider v-model="draft.weights[k]" :min="0" :max="1" :step="0.05" show-input
                       style="flex:1" size="small" />
          </div>
        </div>

        <div class="cfg-section">
          <label>时长零分线（实际平均耗时达到计划时长的倍数时得 0 分）</label>
          <el-input-number v-model="draft.durationMaxRatio" :min="1.1" :max="5" :step="0.1" size="small" />
        </div>

        <div class="cfg-section">
          <label>等级分界（分数下限，最低档必须为 0）</label>
          <div v-for="(g, i) in draft.grades" :key="i" class="grade-row">
            <el-input v-model="g.grade" size="small" style="width:56px" maxlength="4" />
            <el-input v-model="g.label" size="small" style="width:90px" placeholder="名称" />
            <span>≥</span>
            <el-input-number v-model="g.min" :min="0" :max="100" :disabled="i === draft.grades.length - 1" size="small" />
            <el-button v-if="draft.grades.length > 1 && i !== draft.grades.length - 1"
                       size="small" text type="danger" @click="draft.grades.splice(i, 1)">删</el-button>
          </div>
          <el-button size="small" text type="primary" @click="addGrade">+ 增加一档</el-button>
        </div>

        <div class="cfg-section">
          <label>变更说明（可选）</label>
          <el-input v-model="note" size="small" placeholder="例如：更看出错、收紧 A 档" />
        </div>

        <div v-if="store.configError" class="cfg-error">{{ store.configError }}</div>

        <div class="cfg-actions">
          <el-button size="small" @click="loadActive">重置为当前值</el-button>
          <el-button size="small" type="primary" :loading="store.configSaving" @click="save">
            保存为新版本
          </el-button>
        </div>

        <el-divider>历史版本（旧评分仍保留在各自版本下）</el-divider>
        <div v-for="v in store.configVersions" :key="v.versionId" class="ver-row">
          <div>
            <span class="ver-id">{{ v.versionId.slice(0, 8) }}</span>
            <el-tag v-if="v.active" size="small" type="success" effect="plain" style="margin-left:6px">当前</el-tag>
            <div class="ver-note">{{ v.note || '（无说明）' }}</div>
            <div class="ver-detail">
              窗口 {{ v.params.window }} ·
              权重 {{ Math.round(v.params.weights.errors*100) }}/{{ Math.round(v.params.weights.retries*100) }}/{{ Math.round(v.params.weights.duration*100) }}
            </div>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useDAGStore } from '../store/dag'
import type { PipelineHealth, HealthParams } from '../types'

const store = useDAGStore()
const refreshing = ref(false)
const showConfig = ref(false)
const note = ref('')

const metricKeys = ['errors', 'retries', 'duration'] as const
const metricNames: Record<string, string> = { errors: '出错次数', retries: '重试比例', duration: '整体处理时长' }

const pipelines = computed(() => store.health?.pipelines || [])

const draft = ref<HealthParams>({
  window: 10,
  weights: { errors: 0.4, retries: 0.3, duration: 0.3 },
  durationBaseline: 'planned',
  durationMaxRatio: 2.0,
  grades: [
    { grade: 'A', label: '健康', min: 85 },
    { grade: 'B', label: '良好', min: 70 },
    { grade: 'C', label: '一般', min: 55 },
    { grade: 'D', label: '较差', min: 40 },
    { grade: 'E', label: '危险', min: 0 },
  ],
})

function cloneActive() {
  const a = store.health?.activeConfig
  if (a) draft.value = JSON.parse(JSON.stringify(a.params))
}
async function loadActive() {
  const active = await store.fetchConfigVersions()
  draft.value = JSON.parse(JSON.stringify(active.params))
  note.value = ''
}
function addGrade() {
  const grades = draft.value.grades
  const lowest = grades[grades.length - 1].min
  grades.splice(grades.length - 1, 0, { grade: '新', label: '', min: Math.max(1, lowest + 10) })
}
async function save() {
  try {
    await store.updateConfig(JSON.parse(JSON.stringify(draft.value)), note.value)
    ElMessage.success('已保存为新配置版本，历史评分保持原值')
    showConfig.value = false
  } catch { /* 错误已展示在抽屉里 */ }
}
async function refreshAll() {
  refreshing.value = true
  try { await store.refreshHealth(); ElMessage.success('已按当前配置版本重新评分') }
  finally { refreshing.value = false }
}

function cardClass(p: PipelineHealth) {
  if (p.status === 'NO_DATA') return 'nodata-card'
  return ['grade-card', 'g-' + (p.grade || '').toLowerCase()]
}
function gradeClass(grade: string | null) {
  return 'g-' + (grade || '').toLowerCase()
}
function scoreColor(v: number) {
  if (v >= 85) return 'linear-gradient(90deg,#22c55e,#4ade80)'
  if (v >= 70) return 'linear-gradient(90deg,#84cc16,#a3e635)'
  if (v >= 55) return 'linear-gradient(90deg,#f59e0b,#fbbf24)'
  if (v >= 40) return 'linear-gradient(90deg,#f97316,#fb923c)'
  return 'linear-gradient(90deg,#ef4444,#f87171)'
}

onMounted(() => { store.fetchHealth(); store.fetchConfigVersions(); cloneActive() })
</script>

<style scoped>
.panel{background:#1a1a2e;border-radius:8px;padding:10px;border:1px solid #2a2a4a;display:flex;flex-direction:column;min-height:0;flex:1}
.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px}
.head h4{color:#22d3ee;font-size:12px;white-space:nowrap}
.head-actions{display:flex;gap:2px;flex-shrink:0}
.score-list{overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding-right:2px}
.score-card{background:#14142b;border:1px solid #26264a;border-radius:6px;padding:8px 10px}
.nodata-card{opacity:.85}
.card-top{display:flex;justify-content:space-between;align-items:center}
.p-name{color:#e2e8f0;font-size:12px;font-weight:600}
.p-id{color:#64748b;font-size:10px;font-weight:400}
.badge{display:flex;align-items:baseline;gap:6px;padding:2px 10px;border-radius:12px;background:#22c55e22}
.badge .grade{font-size:16px;font-weight:800}
.badge .score{font-size:13px;font-weight:700}
.badge .unit{font-size:9px;font-weight:400;opacity:.8}
.badge.nodata{background:#33415533;color:#94a3b8;font-size:11px}
.badge.g-a{color:#22c55e}.badge.g-b{color:#a3e635}.badge.g-c{color:#fbbf24}.badge.g-d{color:#fb923c}.badge.g-e{color:#f87171}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;color:#7b849b;font-size:10px;margin:5px 0}
.factors{display:flex;flex-direction:column;gap:5px;margin-top:4px}
.factor{background:#1a1a2e;border-radius:4px;padding:4px 8px;border-left:2px solid #22d3ee55}
.factor.excluded{border-left-color:#475569;opacity:.75}
.factor-head{display:flex;align-items:center;gap:8px;font-size:10px}
.f-label{color:#cbd5e1;font-weight:600;min-width:64px}
.f-weight{color:#64748b}
.f-score{margin-left:auto;color:#22d3ee;font-weight:700}
.f-na{margin-left:auto;color:#94a3b8;font-style:italic}
.bar{height:4px;background:#26264a;border-radius:2px;margin-top:3px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .3s}
.f-detail{color:#8b95ad;font-size:9px;margin-top:2px}
.f-reason{color:#fbbf24;font-size:9px;margin-top:2px}
.note{color:#c9a86a;font-size:9px;margin-top:5px;line-height:1.4}
.empty{color:#64748b;font-size:11px;text-align:center;padding:20px 8px}
.cfg-body{padding:0 16px 20px}
.cfg-section{margin-bottom:14px}
.cfg-section label{display:block;color:#475569;font-size:11px;margin-bottom:6px}
.weight-row{display:flex;align-items:center;gap:10px;font-size:12px;color:#334155}
.grade-row{display:flex;gap:6px;align-items:center;margin-bottom:6px}
.cfg-error{color:#ef4444;font-size:11px;margin-bottom:8px}
.cfg-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}
.ver-row{padding:6px 8px;border-bottom:1px solid #eef0f5;font-size:11px}
.ver-id{font-family:monospace;color:#409eff;font-weight:700}
.ver-note{color:#303133;margin:2px 0}
.ver-detail{color:#909399;font-size:10px}
</style>
