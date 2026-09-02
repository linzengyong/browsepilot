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

/** GET /sessions 条目 —— 与 v2 后端 SessionInfo 对齐 */
export interface SessionSummary {
  id: string
  thread_id: string
  name: string
  pinned: boolean
  created_at: string
  updated_at: string
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
