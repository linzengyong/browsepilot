// frontend-vue/src/stores/chat.ts
import { defineStore } from 'pinia'
import type { ChatMessage, AgentPhase, ChatState, SSEEvent } from '@/types'
import { resumeSession } from '@/api/chat'

export const useChatStore = defineStore('chat', {
  state: (): ChatState => ({
    messages: [],
    processing: false,
    phase: null,
    phaseMessage: '',
    planSteps: [],
    currentStepIndex: 0,
    totalSteps: 0,
    screenshot: null,
    promptTokens: 0,
    completionTokens: 0,
    tokenEstimated: false,
    sessionId: null,
    error: null,
    isViewingHistory: false,
    turns: [] as Array<{ turn_index: number; task: string }>,
    currentTurnIndex: 0,
    humanPrompt: '',
  }),

  getters: {
    totalTokens: (state) => state.promptTokens + state.completionTokens,
  },

  actions: {
    reset() {
      this.phase = null
      this.phaseMessage = ''
      this.planSteps = []
      this.currentStepIndex = 0
      this.totalSteps = 0
      this.screenshot = null
      this.promptTokens = 0
      this.completionTokens = 0
      this.tokenEstimated = false
      this.error = null
      this.isViewingHistory = false
      this.turns = []
      this.currentTurnIndex = 0
      this.humanPrompt = ''
    },

    // v2 SSE 事件分发：phase(start) / answer / done / error / human_pending。
    // 事件对象整体作为 data 传入（含 type 字段，见 api/chat.ts）。
    dispatchEvent(event: SSEEvent) {
      const d = event.data
      switch (event.event) {
        case 'phase':
          // phase/start 公布 effective session_id（匿名会话由服务端生成），
          // 客户端必须采纳它才能寻址 /history 与 /chat/resume。
          if (d.phase === 'start') {
            this.sessionId = d.session_id || this.sessionId
            this.turns = [{ turn_index: 0, task: d.task || '' }]
            this.currentTurnIndex = 0
            this.phase = 'executing'
            this.phaseMessage = '任务执行中...'
          }
          break

        case 'answer':
          // v2 一次性给出完整回答（无 answer_chunk 流式分片）
          {
            const lastMsg = this.messages[this.messages.length - 1]
            if (lastMsg && lastMsg.role === 'assistant' && lastMsg._streaming) {
              lastMsg.content = d.text || ''
              lastMsg._streaming = false
            } else {
              this.messages.push({ role: 'assistant', content: d.text || '' })
            }
          }
          this.phase = null
          this.phaseMessage = ''
          break

        case 'done': {
          // 结束帧：stop_reason ∈ {"", "completed", "error", ...}；
          // error 情况 message 已由 error 事件展示，这里只收尾。
          const last = this.messages[this.messages.length - 1]
          if (last && last.role === 'assistant' && (last as any)._streaming) {
            (last as any)._streaming = false
          }
          if (d.stop_reason === 'error' && !this.error) {
            // 防御：error 事件缺失时（done 单独携带 stop_reason=error）也给出可见提示
            this.error = (d.result_data && d.result_data.message) || '任务失败'
            this.messages.push({ role: 'assistant', content: `❌ 错误: ${this.error}` })
          }
          this.phase = null
          this.phaseMessage = ''
          break
        }

        case 'error':
          this.error = d.message || '未知错误'
          this.messages.push({
            role: 'assistant',
            content: `❌ 错误: ${d.message || '未知错误'}`,
          })
          break

        case 'human_pending':
          // HITL 暂停：流在此结束（无 answer/done）。采纳事件携带的
          // session_id（服务端生成的匿名会话 id 只在 phase/start 与
          // human_pending 中公布）。
          this.phase = 'waiting_human'
          this.humanPrompt = d.human_prompt || ''
          if (d.session_id) this.sessionId = d.session_id
          break

        default:
          break
      }
    },

    addUserMessage(task: string) {
      this.messages.push({ role: 'user', content: task })
      this.processing = true
      this.isViewingHistory = false
    },

    finishProcessing() {
      this.processing = false
    },

    viewHistory(sessionId: string, task: string, answer: string, tokenUsage?: { prompt: number; completion: number }) {
      this.sessionId = sessionId
      this.messages = [
        { role: 'user', content: task },
        { role: 'assistant', content: answer },
      ]
      if (tokenUsage) {
        this.promptTokens = tokenUsage.prompt || 0
        this.completionTokens = tokenUsage.completion || 0
      }
      this.processing = false
      this.phase = null
      this.screenshot = null
      this.planSteps = []
      this.currentStepIndex = 0
      this.totalSteps = 0
      this.isViewingHistory = true
    },

    setWaitingHuman(prompt: string) {
      this.phase = 'waiting_human'
      this.humanPrompt = prompt
    },

    // 返回是否成功：失败时 UI 必须回到 waiting_human（HITL 横幅 + 输入重现），
    // 否则停在 executing 死状态（横幅消失、无任何 pending 可恢复）。
    async resumeChat(humanResponse: string): Promise<boolean> {
      if (!this.sessionId) return false
      this.phase = 'executing'
      this.phaseMessage = '继续执行任务...'
      this.processing = true

      try {
        await resumeSession(this.sessionId, humanResponse, (eventType, data) => {
          this.dispatchEvent({ event: eventType, data })
        })
        return true
      } catch (e: any) {
        const msg = e.message || '未知错误'
        this.error = '恢复失败: ' + msg
        this.messages.push({ role: 'assistant', content: `❌ 恢复失败: ${msg}` })
        // HTTP 层失败（429 限流等）后服务端仍处于 pending，把阶段恢复为
        // waiting_human：humanPrompt 未动（来自原 human_pending 事件），
        // ChatPanel 在成功前不清空输入 —— 横幅重现即可直接重试
        this.phase = 'waiting_human'
        this.phaseMessage = ''
        return false
      } finally {
        this.processing = false
        // resume 成功后后端会 record_turn —— 刷新会话列表（动态 import 避免
        // 与 session store 的静态 import 形成循环依赖）
        const { useSessionStore } = await import('./session')
        useSessionStore().fetchList()
      }
    },
  },
})
