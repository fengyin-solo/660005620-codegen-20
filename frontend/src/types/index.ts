export interface TaskNode { id: string; name: string; deps: string[]; x: number; y: number; status: string; startTime?: number; endTime?: number; retries: number }
export interface DAGWorkflow { id: number; name: string; nodes: TaskNode[]; edges: [string,string][] }
export interface ExecutionLog { taskId: string; status: string; timestamp: number; message: string }
export interface CircuitBreaker { taskId: string; failureCount: number; state: string; cooldownUntil: number }
export interface ExecutionInfo { runId?: string; workflow: DAGWorkflow; logs: ExecutionLog[]; circuitBreakers: CircuitBreaker[]; completed: boolean; interrupted?: boolean }

export interface HealthGrade { grade: string; label: string; min: number }
export interface HealthParams {
  window: number
  weights: { errors: number; retries: number; duration: number }
  durationBaseline: string
  durationMaxRatio: number
  grades: HealthGrade[]
}
export interface HealthConfigVersion { versionId: string; createdAt: number; note: string; params: HealthParams; active?: boolean }
export interface HealthBreakdownItem {
  metric: 'errors' | 'retries' | 'duration'
  label: string
  weight: number
  included: boolean
  score?: number
  detail?: string
  excludedReason?: string
}
export interface PipelineHealth {
  pipelineId: string
  pipelineName: string
  configVersion: string | null
  generatedAt: number | null
  status: 'SCORED' | 'NO_DATA' | 'INTERRUPTED_PARTIAL'
  score: number | null
  grade: string | null
  gradeLabel?: string
  windowUsed: number
  windowSize: number
  breakdown: HealthBreakdownItem[]
  notes: string[]
  rawStats: { runs: number; interruptedRuns: number; errors?: number | null; retries?: number | null; attempts?: number | null; avgDuration?: number | null }
  staleCurrentConfig: boolean
  runCount: number
}
export interface HealthScoreResponse { activeConfig: HealthConfigVersion; pipelines: PipelineHealth[] }
