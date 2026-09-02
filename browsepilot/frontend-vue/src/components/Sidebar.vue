<!-- frontend-vue/src/components/Sidebar.vue -->
<template>
  <aside class="w-[220px] flex-shrink-0 bg-bg border-r border-border flex flex-col h-full">
    <!-- Brand -->
    <div class="p-4 pb-2">
      <div class="font-serif text-[20px] font-bold text-text-primary tracking-[-0.5px]">
        BrowsePilot
      </div>
      <div class="font-sans text-[11px] text-text-muted mt-0.5 tracking-[0.5px]">浏览器自动化 AI 助手</div>
    </div>

    <div class="border-t border-border mx-3"></div>

    <!-- New Session Button -->
    <div class="px-3 py-3">
      <button
        @click="onNewSession"
        class="w-full py-2 bg-accent text-white font-sans text-[12px] font-semibold tracking-[0.5px] border-none cursor-pointer"
      >
        新建会话
      </button>
    </div>

    <div class="border-t border-border mx-3"></div>

    <!-- Session List -->
    <div class="flex-1 min-h-0 overflow-y-auto px-3 py-2">
      <div class="font-sans text-[9px] text-text-muted-deep tracking-[1.5px] uppercase font-semibold mb-2">
        历史会话
      </div>

      <!-- Pinned section -->
      <template v-if="pinnedSessions.length > 0">
        <div class="font-sans text-[9px] text-text-disabled mb-1 tracking-[0.5px]">— 置顶 —</div>
        <div
          v-for="s in pinnedSessions"
          :key="s.id"
          class="group flex items-center justify-between py-1.5 px-2 cursor-pointer font-serif text-[13px] mb-0.5"
          :class="s.id === currentSessionId ? 'bg-surface border border-card-border' : 'hover:bg-surface'"
          @click="onSelectSession(s.id)"
        >
          <div class="flex-1 min-w-0">
            <div class="truncate" :class="s.id === currentSessionId ? 'text-accent' : 'text-text-body'">
              {{ displayName(s) }}
            </div>
            <div class="text-[10px] text-text-disabled">{{ s.id }}</div>
          </div>
          <!-- "···" menu button -->
          <div class="relative">
            <button
              @click.stop="toggleMenu(s.id)"
              class="opacity-0 group-hover:opacity-100 text-text-disabled hover:text-accent text-[14px] border-none bg-transparent cursor-pointer px-1"
            >
              ···
            </button>
            <!-- Dropdown menu -->
            <div v-if="openMenuId === s.id" class="absolute right-0 top-full mt-1 bg-bg border border-border shadow-lg z-10 w-[100px]">
              <button @click.stop="onRename(s)" class="block w-full text-left px-3 py-1.5 font-sans text-[11px] hover:bg-surface border-none bg-transparent cursor-pointer">重命名</button>
              <button @click.stop="onTogglePin(s)" class="block w-full text-left px-3 py-1.5 font-sans text-[11px] hover:bg-surface border-none bg-transparent cursor-pointer">{{ s.pinned ? '取消置顶' : '置顶' }}</button>
              <button @click.stop="onDeleteClick(s)" class="block w-full text-left px-3 py-1.5 text-[12px] font-serif hover:bg-surface text-red-500 border-none bg-transparent cursor-pointer">删除</button>
            </div>
          </div>
        </div>
      </template>

      <!-- Unpinned section -->
      <template v-if="unpinnedSessions.length > 0">
        <div v-if="pinnedSessions.length > 0" class="font-sans text-[9px] text-text-disabled mb-1 mt-2 tracking-[0.5px]">— 时间 —</div>
        <div
          v-for="s in unpinnedSessions"
          :key="s.id"
          class="group flex items-center justify-between py-1.5 px-2 cursor-pointer font-serif text-[13px]"
          :class="s.id === currentSessionId ? 'bg-surface border border-card-border' : 'hover:bg-surface'"
          @click="onSelectSession(s.id)"
        >
          <div class="flex-1 min-w-0">
            <div class="truncate" :class="s.id === currentSessionId ? 'text-accent' : 'text-text-body'">
              {{ displayName(s) }}
            </div>
            <div class="text-[10px] text-text-disabled">{{ s.id }}</div>
          </div>
          <div class="relative">
            <button
              @click.stop="toggleMenu(s.id)"
              class="opacity-0 group-hover:opacity-100 text-text-disabled hover:text-accent text-[14px] border-none bg-transparent cursor-pointer px-1"
            >
              ···
            </button>
            <div v-if="openMenuId === s.id" class="absolute right-0 top-full mt-1 bg-bg border border-border shadow-lg z-10 w-[100px]">
              <button @click.stop="onRename(s)" class="block w-full text-left px-3 py-1.5 font-sans text-[11px] hover:bg-surface border-none bg-transparent cursor-pointer">重命名</button>
              <button @click.stop="onTogglePin(s)" class="block w-full text-left px-3 py-1.5 font-sans text-[11px] hover:bg-surface border-none bg-transparent cursor-pointer">{{ s.pinned ? '取消置顶' : '置顶' }}</button>
              <button @click.stop="onDeleteClick(s)" class="block w-full text-left px-3 py-1.5 text-[12px] font-serif hover:bg-surface text-red-500 border-none bg-transparent cursor-pointer">删除</button>
            </div>
          </div>
        </div>
      </template>

      <div v-if="sessions.length === 0" class="text-text-disabled text-[11px] italic">
        暂无历史会话
      </div>
    </div>

    <!-- Current Session Info -->
    <div class="border-t border-border mx-3"></div>
    <div class="px-3 py-2 font-sans text-[10px] text-text-muted tracking-[0.5px]">
      <span v-if="currentSessionId">Session #{{ currentSessionId }}</span>
      <span v-else class="text-text-disabled italic">未连接</span>
    </div>

    <!-- Rename Modal -->
    <div v-if="renameTarget" class="fixed inset-0 bg-black/30 flex items-center justify-center z-20" @click.self="renameTarget = null">
      <div class="bg-bg border border-border p-4 w-[280px] shadow-xl">
        <div class="font-sans text-[12px] font-semibold text-text-primary mb-3">重命名会话</div>
        <input
          v-model="renameValue"
          @keyup.enter="onRenameConfirm"
          @keyup.escape="renameTarget = null"
          class="w-full border border-border px-2 py-1.5 text-[13px] font-serif bg-bg text-text-primary mb-3 outline-none"
          placeholder="输入新名称"
          ref="renameInput"
        />
        <div class="flex justify-end gap-2">
          <button @click="renameTarget = null" class="px-3 py-1 font-sans text-[11px] border border-border bg-transparent cursor-pointer">取消</button>
          <button @click="onRenameConfirm" class="px-3 py-1 font-sans text-[11px] font-semibold bg-accent text-white border-none cursor-pointer">确认</button>
        </div>
      </div>
    </div>

    <!-- Delete Confirm Modal -->
    <div v-if="deleteTarget" class="fixed inset-0 bg-black/30 flex items-center justify-center z-20" @click.self="deleteTarget = null">
      <div class="bg-bg border border-border p-4 w-[280px] shadow-xl">
        <div class="font-sans text-[12px] font-semibold text-text-primary mb-1">删除会话</div>
        <div class="text-[12px] text-text-muted mb-3">删除后该对话将不可恢复</div>
        <div class="flex justify-end gap-2">
          <button @click="deleteTarget = null" class="px-3 py-1 font-sans text-[11px] border border-border bg-transparent cursor-pointer">取消</button>
          <button @click="onDeleteConfirm" class="px-3 py-1 font-sans text-[11px] font-semibold bg-red-500 text-white border-none cursor-pointer">删除</button>
        </div>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, nextTick, watch } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useChatStore } from '@/stores/chat'
import type { SessionSummary } from '@/types'

const sessionStore = useSessionStore()
const chatStore = useChatStore()

const sessions = computed(() => sessionStore.sessions)
const currentSessionId = computed(() => chatStore.sessionId)
const pinnedSessions = computed(() => sessions.value.filter(s => s.pinned))
const unpinnedSessions = computed(() => sessions.value.filter(s => !s.pinned))

const openMenuId = ref<string | null>(null)
const renameTarget = ref<SessionSummary | null>(null)
const renameValue = ref('')
const renameInput = ref<HTMLInputElement | null>(null)
const deleteTarget = ref<SessionSummary | null>(null)

onMounted(() => {
  sessionStore.fetchList()
})

function displayName(s: SessionSummary): string {
  return s.name || s.id
}

function toggleMenu(id: string) {
  openMenuId.value = openMenuId.value === id ? null : id
}

function onSelectSession(id: string) {
  openMenuId.value = null
  chatStore.sessionId = id
  sessionStore.loadSessionHistory(id)
}

function onNewSession() {
  chatStore.reset()
  chatStore.messages = []
  chatStore.sessionId = null
}

async function onRename(s: SessionSummary) {
  openMenuId.value = null
  renameTarget.value = s
  renameValue.value = s.name || ''
  await nextTick()
  renameInput.value?.focus()
}

async function onRenameConfirm() {
  if (renameTarget.value && renameValue.value.trim()) {
    await sessionStore.renameSession(renameTarget.value.id, renameValue.value.trim())
  }
  renameTarget.value = null
  renameValue.value = ''
}

async function onTogglePin(s: SessionSummary) {
  openMenuId.value = null
  await sessionStore.togglePin(s.id)
}

function onDeleteClick(s: SessionSummary) {
  openMenuId.value = null
  deleteTarget.value = s
}

async function onDeleteConfirm() {
  if (deleteTarget.value) {
    const id = deleteTarget.value.id
    if (id === chatStore.sessionId) {
      chatStore.reset()
      chatStore.messages = []
      chatStore.sessionId = null
    }
    await sessionStore.deleteSession(id)
  }
  deleteTarget.value = null
}

// Close dropdown on outside click
watch(openMenuId, (val) => {
  if (val) {
    setTimeout(() => {
      document.addEventListener('click', () => { openMenuId.value = null }, { once: true })
    }, 0)
  }
})
</script>
