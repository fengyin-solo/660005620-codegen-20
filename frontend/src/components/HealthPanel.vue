<template>
  <div class="panel">
    <div class="hd">
      <h4>💚 流水线健康评分</h4>
      <div class="hd-btns">
        <el-button size="small" text @click="showCfg = !showCfg">⚙ 权重</el-button>
        <el-button size="small" type="primary" :loading="store.healthLoading" @click="store.refreshHealth()">刷新评分</el-button>
      </div>
    </div>

    <div v-if="showCfg && cfg" class="cfg">
      <div class="cfg-row">
        <label>出错权重</label><el-input-number v-model="cfg.w_error" :min="0" :max="1" :step="0.05" size="small"/>
        <label>重试权重</label><el-input-number v-model="cfg.w_retry" :min="0" :max="1" :step="0.05" size="small"/>
        <label>时长权重</label><el-input-number v-model="cfg.w_duration" :min="0" :max="1" :step="0.05" size="small"/>
      </div>
      <div class="cfg-row">
        <label>A ≥</label><el-input-number v-model="cfg.grade_a" :min="0" :max="100" size="small"/>
        <label>B ≥</label><el-input-number v-model="cfg.grade_b" :min="0" :max="100" size="small"/>
        <label>C ≥</label><el-input-number v-model="cfg.grade_c" :min="0" :max="100" size="small"/>
        <label>窗口</label><el-input-number v-model="cfg.window_size" :min="1" :max="100" size="small"/>
      </div>
      <div class="cfg-row">
        <el-button size="small" type="success" @click="saveCfg">保存配置</el-button>
        <span class="cfg-hint">仅影响下次刷新,历史评分保持原值</span>
      </div>
    </div>

    <div v-for="s in store.healthScores" :key="s.workflowId" class="score-card">
      <div class="sc-head">
        <span class="sc-name">#{{ s.workflowId }} {{ s.workflowName }}</span>
        <span class="grade" :class="gradeClass(s.grade)">{{ s.grade }}</span>
        <span class="sc-score">{{ s.score === null ? '暂无数据' : s.score + ' 分' }}</span>
      </div>
      <div class="sc-meta">窗口内 {{ s.runsInWindow }}/{{ s.windowSize }} 次执行 · {{ fmtTime(s.computedAt) }} 计算</div>
      <div class="comp" v-for="(c, key) in s.components" :key="key">
        <span class="c-name">{{ compName(key) }}</span>
        <template v-if="c.available">
          <div class="c-bar"><div class="c-fill" :style="{width: (c.score || 0) + '%', background: barColor(c.score)}"></div></div>
          <span class="c-val">{{ c.score }} · {{ c.detail }}</span>
        </template>
        <span v-else class="c-miss">未参与计算</span>
      </div>
      <div v-for="(n, i) in s.notes" :key="i" class="note">⚠ {{ n }}</div>
      <div v-if="s.history.length > 1" class="hist">
        历史评分:
        <span v-for="(h, i) in s.history.slice().reverse()" :key="i" class="hist-item">
          {{ h.score === null ? '暂无数据' : h.score }}<i v-if="i < s.history.length - 1"> →</i>
        </span>
      </div>
    </div>
    <div v-if="!store.healthScores.length" class="empty">尚未计算评分,点击「刷新评分」生成</div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useDAGStore } from '../store/dag'
import type { HealthConfig } from '@/types'

const store = useDAGStore()
const showCfg = ref(false)
const cfg = ref<HealthConfig | null>(null)

onMounted(() => store.fetchHealth())
watch(() => store.healthConfig, (c) => { if (c) cfg.value = { ...c } }, { immediate: true })

async function saveCfg() {
  if (!cfg.value) return
  try {
    await store.saveHealthConfig(cfg.value)
    ElMessage.success('配置已保存,下次刷新评分时生效')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '配置校验失败')
  }
}

const compName = (k: string) => ({ error: '出错', retry: '重试', duration: '时长' }[k as string] || k)
const gradeClass = (g: string) => ({ A: 'g-a', B: 'g-b', C: 'g-c', D: 'g-d' }[g] || 'g-none')
const barColor = (s: number | null) => (s ?? 0) >= 90 ? '#22c55e' : (s ?? 0) >= 75 ? '#84cc16' : (s ?? 0) >= 60 ? '#fbbf24' : '#ef4444'
const fmtTime = (t: number) => new Date(t * 1000).toLocaleTimeString()
</script>

<style scoped>
.panel{background:#1a1a2e;border-radius:8px;padding:10px;border:1px solid #2a2a4a}
.hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.hd h4{color:#4ade80;font-size:12px}
.hd-btns{display:flex;gap:4px;align-items:center}
.cfg{background:#14142b;border-radius:6px;padding:6px;margin-bottom:8px}
.cfg-row{display:flex;gap:6px;align-items:center;margin:4px 0;flex-wrap:wrap}
.cfg-row label{font-size:10px;color:#888;white-space:nowrap}
.cfg-row :deep(.el-input-number){width:86px}
.cfg-hint{font-size:10px;color:#64748b}
.score-card{background:#14142b;border-radius:6px;padding:8px;margin-bottom:8px}
.sc-head{display:flex;align-items:center;gap:8px}
.sc-name{font-size:12px;color:#e0e0e0;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grade{font-weight:800;font-size:13px;padding:1px 8px;border-radius:4px}
.g-a{background:#22c55e22;color:#22c55e}.g-b{background:#84cc1622;color:#84cc16}
.g-c{background:#fbbf2422;color:#fbbf24}.g-d{background:#ef444422;color:#ef4444}
.g-none{background:#4a556822;color:#94a3b8}
.sc-score{font-size:12px;color:#bb86fc;font-weight:700}
.sc-meta{font-size:10px;color:#64748b;margin:2px 0 6px}
.comp{display:flex;align-items:center;gap:6px;margin:3px 0;font-size:10px}
.c-name{color:#94a3b8;width:28px;flex-shrink:0}
.c-bar{flex:1;height:6px;background:#0f0f23;border-radius:3px;overflow:hidden}
.c-fill{height:100%;border-radius:3px;transition:width .3s}
.c-val{color:#888;min-width:110px}
.c-miss{color:#64748b;background:#4a556822;padding:1px 6px;border-radius:3px}
.note{font-size:10px;color:#fbbf24;margin-top:3px}
.hist{font-size:10px;color:#64748b;margin-top:5px}
.hist-item{color:#94a3b8}.hist-item i{color:#475569;font-style:normal}
.empty{color:#4a5568;font-size:11px}
</style>
