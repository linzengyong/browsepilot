// frontend-vue/src/api/chat.ts
import type { ReplayStep } from '@/types'

const API_BASE = '/api'

export async function streamChat(
  task: string,
  sessionId: string | null,
  onEvent: (eventType: string, data: Record<string, any>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task, session_id: sessionId }),
    signal,
  })

  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`后端错误 (${resp.status}): ${text}`)
  }

  const reader = resp.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      while (true) {
        const idx = buffer.indexOf('\n\n')
        if (idx === -1) break
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)

        const lines = frame.split('\n')
        const dataLine = lines.find(l => l.startsWith('data: '))
        if (!dataLine) continue

        try {
          const event = JSON.parse(dataLine.slice(6))
          onEvent(event.event, event.data)
        } catch {
          // Skip unparseable frames
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export async function resumeSession(
  sessionId: string,
  onEvent: (eventType: string, data: Record<string, any>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/chat/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
    signal,
  })

  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`恢复错误 (${resp.status}): ${text}`)
  }

  const reader = resp.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      while (true) {
        const idx = buffer.indexOf('\n\n')
        if (idx === -1) break
        const frame = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)

        const lines = frame.split('\n')
        const dataLine = lines.find(l => l.startsWith('data: '))
        if (!dataLine) continue

        try {
          const event = JSON.parse(dataLine.slice(6))
          onEvent(event.event, event.data)
        } catch {
          // Skip unparseable frames
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export async function fetchReplay(sessionId: string): Promise<ReplayStep[]> {
  const resp = await fetch(`${API_BASE}/replay/${sessionId}`)
  if (!resp.ok) throw new Error(`获取回放失败 (${resp.status})`)
  return resp.json()
}
