<template>
  <div class="app-root">
    <header class="top-bar">
      <h1>🔀 分布式任务工作流DAG编排与执行引擎</h1>
      <div class="tools">
        <el-input v-model="wfName" placeholder="工作流名称" size="small" style="width:160px"/>
        <el-button size="small" @click="create" :loading="store.loading">创建DAG</el-button>
        <el-select v-model="store.workers" size="small" style="width:100px">
          <el-option :value="1" label="1 Worker"/><el-option :value="3" label="3 Workers"/><el-option :value="5" label="5 Workers"/>
        </el-select>
        <el-select v-model="store.strategy" size="small" style="width:100px">
          <el-option value="fifo" label="FIFO"/><el-option value="priority" label="优先级"/><el-option value="max_concurrent" label="最大并发"/>
        </el-select>
        <el-button type="success" size="small" @click="run" :disabled="!store.workflow" :loading="store.loading">▶ 执行</el-button>
        <el-button v-if="store.activeRunId" type="warning" size="small" plain @click="store.interruptActiveRun()">■ 中断</el-button>
        <el-radio-group v-model="view" size="small">
          <el-radio-button value="dag">DAG 画布</el-radio-button>
          <el-radio-button value="health">🩺 健康评分</el-radio-button>
        </el-radio-group>
        <span class="ws-dot" :class="{on:store.wsConnected}"></span>
      </div>
    </header>
    <div class="main-grid">
      <div v-show="view==='dag'" class="dag-area">
        <DAGCanvas />
      </div>
      <div v-show="view==='health'" class="health-area">
        <HealthPanel />
      </div>
      <div class="side-area">
        <LogPanel />
        <CircuitBreakerPanel />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import DAGCanvas from './components/DAGCanvas.vue'
import LogPanel from './components/LogPanel.vue'
import CircuitBreakerPanel from './components/CircuitBreakerPanel.vue'
import HealthPanel from './components/HealthPanel.vue'
import { useDAGStore } from './store/dag'
const store = useDAGStore()
const wfName = ref('data-pipeline')
const view = ref<'dag' | 'health'>('dag')
function create() { store.createWorkflow(wfName.value) }
function run() { store.run() }
onMounted(() => store.connectWS())
onUnmounted(() => store.disconnectWS())
</script>

<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:#0c0c1d;color:#e0e0e0}
.app-root{height:100vh;display:flex;flex-direction:column}
.top-bar{display:flex;justify-content:space-between;align-items:center;padding:10px 20px;background:#1a1a2e;border-bottom:1px solid #2a2a4a}
.top-bar h1{font-size:1rem;color:#bb86fc}
.tools{display:flex;gap:6px;align-items:center}
.ws-dot{width:8px;height:8px;border-radius:50%;background:#ef4444}.ws-dot.on{background:#22c55e}
.main-grid{display:grid;grid-template-columns:1fr 320px;flex:1;overflow:hidden}
.dag-area{background:#0f0f23;position:relative;overflow:hidden}
.health-area{background:#0f0f23;overflow:hidden;display:flex;padding:10px}
.health-area > *{flex:1;min-height:0}
.side-area{display:flex;flex-direction:column;gap:8px;padding:8px;overflow-y:auto;background:#14142b}
</style>