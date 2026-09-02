// frontend-vue/src/api/chat.ts
// V2 API 客户端 —— 目标后端:8001（src/browsepilot），SSE 事件为扁平
// `data: {"type": "...", ...}` 帧（区别于旧后端的 `{"event": ..., "data": ...}`）。

const API_BASE = '/api'

/** v2 SSE 事件：{"type": "phase"|"answer"|"done"|"error"|"human_pending", ...} */
interface StreamEvent {
  type: string
  [key: string]: any
}

async function readSSEStream(
  resp: Response,
  onEvent: (eventType: string, data: Record<string, any>) => void,
): Promise<void> {
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
          const event: StreamEvent = JSON.parse(dataLine.slice(6))
          if (event && typeof event.type === 'string') {
            // 把整个事件对象作为 data 传给 store（含 type 字段）
            onEvent(event.type, event)
          }
        } catch {
          // Skip unparseable frames
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

async function ensureOk(resp: Response, label: string): Promise<void> {
  if (resp.ok) return
  // FastAPI 错误体为 {"detail": "..."}；429（限流/会话预算）也走这里
  let detail = ''
  try {
    const data = await resp.json()
    detail = data.detail || JSON.stringify(data)
  } catch {
    detail = await resp.text()
  }
  throw new Error(`${label} (${resp.status}): ${detail}`)
}

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

  await ensureOk(resp, '后端错误')
  await readSSEStream(resp, onEvent)
}

export async function resumeSession(
  sessionId: string,
  humanResponse: string,
  onEvent: (eventType: string, data: Record<string, any>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/chat/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, human_response: humanResponse }),
    signal,
  })

  await ensureOk(resp, '恢复错误')
  await readSSEStream(resp, onEvent)
}
