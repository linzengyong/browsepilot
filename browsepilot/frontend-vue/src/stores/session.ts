// frontend-vue/src/stores/session.ts
import { defineStore } from 'pinia'
import type { SessionSummary } from '@/types'
import { useChatStore } from './chat'

const API_BASE = '/api'

export const useSessionStore = defineStore('session', {
  state: () => ({
    sessions: [] as SessionSummary[],
    loading: false,
  }),

  actions: {
    async fetchList() {
      this.loading = true
      try {
        const resp = await fetch(`${API_BASE}/sessions`)
        if (resp.ok) {
          this.sessions = await resp.json()
        }
      } catch {
        // Backend unavailable — silently ignore
      } finally {
        this.loading = false
      }
    },

    async deleteSession(id: string) {
      try {
        const resp = await fetch(`${API_BASE}/sessions/${id}`, { method: 'DELETE' })
        if (resp.ok) {
          this.sessions = this.sessions.filter(s => s.id !== id)
        }
      } catch {
        // Silently ignore
      }
    },

    async renameSession(id: string, name: string) {
      try {
        const resp = await fetch(`${API_BASE}/sessions/${id}/rename`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        })
        if (resp.ok) {
          const s = this.sessions.find(s => s.id === id)
          if (s) s.name = name
        }
      } catch {
        // Silently ignore
      }
    },

    async togglePin(id: string) {
      try {
        // v2：无请求体，后端翻转 pinned 并返回新状态 {pinned: bool}
        const resp = await fetch(`${API_BASE}/sessions/${id}/pin`, {
          method: 'PATCH',
        })
        if (resp.ok) {
          // Re-fetch from backend for correct sort order
          // (backend sorts pinned-first, then created_at desc within each group)
          await this.fetchList()
        }
      } catch {
        // Silently ignore
      }
    },

    async loadSessionHistory(sessionId: string) {
      try {
        // v2: GET /history/{id} → {session: SessionInfo, turns: [{task, answer, created_at}]}
        const resp = await fetch(`${API_BASE}/history/${sessionId}`)
        if (!resp.ok) return
        const data = await resp.json()
        const chatStore = useChatStore()
        const turns: Array<{ task: string; answer: string; created_at: string }> =
          data.turns || []
        const lastTurn = turns.length ? turns[turns.length - 1] : null
        chatStore.viewHistory(
          sessionId,
          lastTurn ? lastTurn.task : '',
          lastTurn ? lastTurn.answer : '',
        )
        chatStore.turns = turns.map((t, i) => ({ turn_index: i, task: t.task }))
        chatStore.currentTurnIndex = turns.length ? turns.length - 1 : 0
      } catch {
        // Silently ignore
      }
    },
  },
})
