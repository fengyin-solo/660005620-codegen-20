export interface TaskNode { id: string; name: string; deps: string[]; x: number; y: number; status: string; startTime?: number; endTime?: number; retries: number }
export interface DAGWorkflow { id: number; name: string; nodes: TaskNode[]; edges: [string,string][] }
export interface ExecutionLog { taskId: string; status: string; timestamp: number; message: string }
export interface CircuitBreaker { taskId: string; failureCount: number; state: string; cooldownUntil: number }
export interface ExecutionInfo { workflow: DAGWorkflow; logs: ExecutionLog[]; circuitBreakers: CircuitBreaker[]; completed: boolean }

export interface HealthComponent { score: number | null; weight: number; available: boolean; detail: string }
export interface HealthHistoryItem { score: number | null; grade: string; computedAt: number }
export interface HealthScore {
  workflowId: number; workflowName: string; score: number | null; grade: string;
  runsInWindow: number; windowSize: number; computedAt: number;
  components: Record<'error' | 'retry' | 'duration', HealthComponent>;
  notes: string[]; history: HealthHistoryItem[]
}
export interface HealthConfig {
  w_error: number; w_retry: number; w_duration: number;
  grade_a: number; grade_b: number; grade_c: number; window_size: number
}
