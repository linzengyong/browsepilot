// frontend-vue/src/types/index.ts

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  _streaming?: boolean
}

export type AgentPhase = 'planning' | 'executing' | 'reflecting' | 'replanning' | 'answering' | 'waiting_human'

export interface SSEEvent {
  event: string
  data: Record<string, any>
}

export interface SessionSummary {
  id: string
  task_summary: string
  created_at: string
  status: string
  custom_name?: string
  pinned?: boolean
  turn_count?: number
}

export interface ReplayStep {
  step_index: number
  step: string
  screenshot_path: string
  timestamp: string
  result: Record<string, any>
}

export interface ChatState {
  messages: ChatMessage[]
  processing: boolean
  phase: AgentPhase | null
  phaseMessage: string
  planSteps: string[]
  currentStepIndex: number
  totalSteps: number
  screenshot: string | null
  promptTokens: number
  completionTokens: number
  sessionId: string | null
  error: string | null
  isViewingHistory: boolean
  tokenEstimated: boolean
  turns: Array<{ turn_index: number; task: string }>
  currentTurnIndex: number
  humanPrompt: string
}
