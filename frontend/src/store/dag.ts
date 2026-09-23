import { defineStore } from 'pinia'
import { ref } from 'vue'
import axios from 'axios'
import type { DAGWorkflow, ExecutionInfo, HealthScoreResponse, HealthParams, HealthConfigVersion } from '@/types'
export const useDAGStore = defineStore('dag', () => {
  const loading = ref(false)
  const workflow = ref<DAGWorkflow | null>(null)
  const execution = ref<ExecutionInfo | null>(null)
  const wsConnected = ref(false)
  const workers = ref(3)
  const strategy = ref('fifo')
  const activeRunId = ref<string | null>(null)

  // ---- 流水线健康评分 ----
  const health = ref<HealthScoreResponse | null>(null)
  const healthLoading = ref(false)
  const configVersions = ref<HealthConfigVersion[]>([])
  const configSaving = ref(false)
  const configError = ref('')

  let ws: WebSocket|null = null
  function connectWS() {
    ws = new WebSocket(`ws://${location.hostname}:8000/ws`)
    ws.onopen = () => { wsConnected.value = true }
    ws.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data)
        execution.value = d
        if (d.runId) activeRunId.value = d.runId
        // 一次执行落库后(完成或被打断)后端会生成新评分快照, 顺手刷新列表
        if (d.completed) { activeRunId.value = null; fetchHealth() }
      } catch {}
    }
  }

  async function createWorkflow(name: string) {
    loading.value = true
    try { const { data } = await axios.post('/api/workflow', { name }) ; workflow.value = data ; await fetchHealth() }
    finally { loading.value = false }
  }

  async function run() {
    if (!workflow.value) return
    loading.value = true
    try {
      const { data } = await axios.post('/api/run', { workflowId: workflow.value.id, workers: workers.value, strategy: strategy.value })
      execution.value = data
      activeRunId.value = data.runId
    } finally { loading.value = false }
  }

  async function interruptActiveRun() {
    if (!activeRunId.value) return
    await axios.post(`/api/run/${activeRunId.value}/interrupt`)
  }

  async function fetchHealth() {
    healthLoading.value = true
    try {
      const { data } = await axios.get<HealthScoreResponse>('/api/health/scores')
      health.value = data
    } finally { healthLoading.value = false }
  }

  async function refreshHealth(pipelineId?: string) {
    await axios.post('/api/health/refresh' + (pipelineId ? `?pipelineId=${encodeURIComponent(pipelineId)}` : ''))
    await fetchHealth()
  }

  async function fetchConfigVersions() {
    const { data } = await axios.get('/api/health/config')
    configVersions.value = data.versions
    return data.active as HealthConfigVersion
  }

  async function updateConfig(params: HealthParams, note: string) {
    configSaving.value = true
    configError.value = ''
    try {
      await axios.put('/api/health/config', { params, note })
      // 配置变更不会改写旧评分, 列表拿到的仍是旧快照(带 stale 标记), 需要时再手动刷新
      await fetchHealth()
      await fetchConfigVersions()
    } catch (e: any) {
      configError.value = e?.response?.data?.detail || '配置保存失败'
      throw e
    } finally { configSaving.value = false }
  }

  function disconnectWS() { ws?.close(); ws = null }
  return {
    loading, workflow, execution, wsConnected, workers, strategy, activeRunId,
    health, healthLoading, configVersions, configSaving, configError,
    connectWS, createWorkflow, run, interruptActiveRun,
    fetchHealth, refreshHealth, fetchConfigVersions, updateConfig, disconnectWS
  }
})
