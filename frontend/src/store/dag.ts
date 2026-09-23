import { defineStore } from 'pinia'
import { ref } from 'vue'
import axios from 'axios'
import type { DAGWorkflow, ExecutionInfo, HealthScore, HealthConfig } from '@/types'
export const useDAGStore = defineStore('dag', () => {
  const loading = ref(false)
  const workflow = ref<DAGWorkflow | null>(null)
  const execution = ref<ExecutionInfo | null>(null)
  const wsConnected = ref(false)
  const workers = ref(3)
  const strategy = ref('fifo')
  const healthScores = ref<HealthScore[]>([])
  const healthConfig = ref<HealthConfig | null>(null)
  const healthLoading = ref(false)

  let ws: WebSocket|null = null
  function connectWS() {
    ws = new WebSocket(`ws://${location.hostname}:8000/ws`)
    ws.onopen = () => { wsConnected.value = true }
    ws.onmessage = (e) => {
      try { const d = JSON.parse(e.data); execution.value = d }
      catch {}
    }
  }

  async function createWorkflow(name: string) {
    loading.value = true
    try { const { data } = await axios.post('/api/workflow', { name }) ; workflow.value = data }
    finally { loading.value = false }
  }

  async function run() {
    if (!workflow.value) return
    loading.value = true
    try { const { data } = await axios.post('/api/run', { workflowId: workflow.value.id, workers: workers.value, strategy: strategy.value }) ; execution.value = data }
    finally { loading.value = false }
  }

  async function stop() {
    try { await axios.post('/api/stop') } catch {}
  }

  async function fetchHealth() {
    try { const { data } = await axios.get('/api/health') ; healthScores.value = data.scores || [] ; healthConfig.value = data.config || null } catch {}
  }

  async function refreshHealth() {
    healthLoading.value = true
    try { await axios.post('/api/health/refresh') ; await fetchHealth() }
    finally { healthLoading.value = false }
  }

  async function saveHealthConfig(cfg: Partial<HealthConfig>) {
    const { data } = await axios.put('/api/health/config', cfg)
    healthConfig.value = data
  }

  function disconnectWS() { ws?.close(); ws = null }
  return { loading, workflow, execution, wsConnected, workers, strategy,
           healthScores, healthConfig, healthLoading,
           connectWS, createWorkflow, run, stop, fetchHealth, refreshHealth, saveHealthConfig, disconnectWS }
})
