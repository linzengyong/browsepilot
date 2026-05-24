<!-- frontend-vue/src/components/ChatPanel.vue -->
<template>
  <div class="flex flex-col h-full min-h-0">
    <!-- HITL banner -->
    <div v-if="store.phase === 'waiting_human'"
         class="bg-amber-50 border-b border-amber-300 px-4 py-3">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="text-amber-600 text-sm">⏸</span>
          <div>
            <p class="text-amber-800 font-semibold text-sm">等待人工操作</p>
            <p class="text-amber-600 text-xs mt-0.5">{{ store.humanPrompt }}</p>
          </div>
        </div>
        <button @click="store.resumeChat()"
                :disabled="store.processing"
                class="bg-amber-500 text-white px-4 py-1.5 rounded text-sm hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
          继续
        </button>
      </div>
    </div>

    <!-- Messages area -->
    <div ref="scrollRef" class="flex-1 min-h-0 overflow-y-auto px-4">
      <div
        v-if="messages.length === 0 && !processing"
        class="flex items-center justify-center h-full min-h-[200px] font-serif text-[14px] italic text-text-disabled"
      >
        输入指令，开始浏览器自动化任务
      </div>

      <ChatMessage
        v-for="(msg, i) in messages"
        :key="i"
        :role="msg.role"
        :content="msg.content"
      />

      <!-- Processing indicator -->
      <div v-if="processing" class="py-3 px-3">
        <ThinkingIndicator />
      </div>
    </div>

    <!-- History banner -->
    <div v-if="isViewingHistory" class="px-4 pt-2">
      <div class="bg-surface border border-card-border px-3 py-2 font-sans text-[11px] text-text-muted">
        正在查看历史会话 — 发送消息将继续此会话
      </div>
    </div>
    <!-- Input — always shown -->
    <div class="px-4 pb-3">
      <ChatInput :disabled="processing" @submit="onSubmit" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, watch, ref, nextTick } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useSSE } from '@/composables/useSSE'
import ChatMessage from './ChatMessage.vue'
import ChatInput from './ChatInput.vue'
import ThinkingIndicator from './ThinkingIndicator.vue'

const store = useChatStore()
const { startTask } = useSSE()
const scrollRef = ref<HTMLDivElement>()

const messages = computed(() => store.messages)
const processing = computed(() => store.processing)
const isViewingHistory = computed(() => store.isViewingHistory)

// Auto-scroll to bottom when new messages arrive
watch(
  () => [store.messages.length, store.phaseMessage],
  async () => {
    await nextTick()
    if (scrollRef.value) {
      scrollRef.value.scrollTop = scrollRef.value.scrollHeight
    }
  },
)

function onSubmit(task: string) {
  startTask(task)
}
</script>
