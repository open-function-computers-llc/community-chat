<script setup>
import { onMounted, onUnmounted, ref, computed } from "vue";
import { storeToRefs } from "pinia";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import Sidebar from "@/components/Sidebar.vue";
import MessageBubble from "@/components/MessageBubble.vue";
import ComposeBox from "@/components/ComposeBox.vue";
import { onWsEvent } from "@/stores/auth";

const auth = useAuthStore();
const chat = useChatStore();
const { groupMessages } = storeToRefs(chat);
const { me, typingNames, wsStatus } = storeToRefs(auth);

const scrollRef = ref(null);
const mobileSidebar = ref(false);
const loadingOlder = ref(false);
const refreshing = ref(false);
let wsUnsub = null;

const showTyping = computed(() => typingNames.value.length > 0);
// A banner invites a manual refresh while we're not fully live (dropped
// connection, mid-reconnect) — the usual way to catch up on missed messages.
const connectionLive = computed(() => wsStatus.value === "connected");

// Always an instant jump: "smooth" can lag behind fast message bursts and
// leave the newest message sitting below the fold.
function scrollToEnd() {
  requestAnimationFrame(() => {
    if (scrollRef.value) {
      scrollRef.value.scrollTo({ top: scrollRef.value.scrollHeight, behavior: "auto" });
    }
  });
}

// Re-fetch the latest page of messages (catch-up after a dropped connection,
// or on manual refresh). Keeps what we already have if the fetch fails.
async function refresh() {
  if (refreshing.value) return;
  refreshing.value = true;
  try {
    await chat.loadGroupMessages();
    scrollToEnd();
  } finally {
    refreshing.value = false;
  }
}

async function loadOlder() {
  if (loadingOlder.value) return;
  const first = groupMessages.value[0];
  if (!first) return;
  loadingOlder.value = true;
  const el = scrollRef.value;
  const prevHeight = el?.scrollHeight;
  const prevTop = el?.scrollTop;
  await chat.loadGroupMessages(first.id, 50);
  requestAnimationFrame(() => {
    if (el) el.scrollTop = el.scrollHeight - prevHeight + prevTop;
  });
  loadingOlder.value = false;
}

function onScroll() {
  if (scrollRef.value && scrollRef.value.scrollTop < 80) {
    loadOlder();
  }
}

function handleWs(msg) {
  if (msg.type === "message.new" && msg.channel === "group") {
    const el = scrollRef.value;
    const nearBottom = el ? el.scrollHeight - el.scrollTop - el.clientHeight < 120 : true;
    if (nearBottom || msg.message.author.id === me.value?.id) {
      scrollToEnd();
    }
  }
}

onMounted(() => {
  wsUnsub = onWsEvent(handleWs);
  requestAnimationFrame(() => scrollToEnd());
});

onUnmounted(() => {
  if (wsUnsub) wsUnsub();
});
</script>

<template>
  <div class="chat-layout">
    <Sidebar :mobile-open="mobileSidebar" @close="mobileSidebar = false" />

    <section class="chat-main">
      <header class="chat-header">
        <button class="mobile-toggle" @click="mobileSidebar = true">☰</button>
        <span class="header-title">Group Chat</span>
        <span v-if="showTyping" class="typing-indicator">{{ typingNames.join(", ") }} typing…</span>
        <span class="header-spacer" />
        <button
          class="refresh-btn"
          :title="connectionLive ? 'Refresh messages' : 'Refresh — connection not fully live'"
          :disabled="refreshing"
          @click="refresh"
        >
          {{ refreshing ? "…" : "↻" }}
        </button>
      </header>

      <div v-if="!connectionLive" class="connection-banner">
        <span>Connection {{ wsStatus === "offline" ? "offline" : "unstable" }} — some messages may be missing.</span>
        <button class="connection-refresh" :disabled="refreshing" @click="refresh">
          {{ refreshing ? "Refreshing…" : "Refresh" }}
        </button>
      </div>

      <div ref="scrollRef" class="messages" @scroll="onScroll">
        <div v-if="groupMessages.length === 0" class="empty-state">
          <div class="empty-icon">💬</div>
          <h2>Welcome to Community Chat!</h2>
          <p>Say hi to everyone — this is where our little corner of the internet lives.</p>
        </div>
        <MessageBubble
          v-for="msg in groupMessages"
          :key="msg.id"
          :message="msg"
          channel="group"
          :is-own="msg.author?.id === me?.id"
        />
      </div>

      <ComposeBox channel="group" @send-typing="scrollToEnd" />
    </section>
  </div>
</template>

<style scoped>
.chat-layout {
  flex: 1;
  display: flex;
  overflow: hidden;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chat-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-elevated);
}
.header-title { font-weight: 600; font-size: 16px; }
.typing-indicator { color: var(--accent-hover); font-size: 13px; font-style: italic; }
.header-spacer { flex: 1; }
.refresh-btn {
  flex-shrink: 0;
  background: var(--bg-hover);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: var(--radius-sm);
  width: 30px;
  height: 30px;
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s, border-color 0.15s;
}
.refresh-btn:hover:not(:disabled) { background: var(--accent-soft); border-color: var(--accent); }
.refresh-btn:disabled { opacity: 0.5; cursor: default; }
.connection-banner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 8px 16px;
  font-size: 13px;
  color: #fff;
  background: #b45309; /* amber-700 */
  border-bottom: 1px solid #92400e;
}
.connection-banner .connection-refresh {
  background: rgba(255, 255, 255, 0.18);
  border: 1px solid rgba(255, 255, 255, 0.4);
  color: #fff;
  border-radius: var(--radius-sm);
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
}
.connection-banner .connection-refresh:hover:not(:disabled) { background: rgba(255, 255, 255, 0.3); }
.connection-banner .connection-refresh:disabled { opacity: 0.6; cursor: default; }
.mobile-toggle { display: none; }
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px 16px;
  display: flex;
  flex-direction: column;
}
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: var(--text-muted);
  padding: 40px;
}
.empty-icon { font-size: 56px; margin-bottom: 16px; }
.empty-state h2 { color: var(--text); margin-bottom: 8px; }

@media (max-width: 768px) {
  .mobile-toggle {
    display: inline-block;
    font-size: 18px;
    color: var(--text);
  }
  .bubble-col { max-width: 85% !important; }
}
</style>
