<script setup>
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { storeToRefs } from "pinia";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import Avatar from "./Avatar.vue";
import AboutModal from "./AboutModal.vue";

defineProps({
  mobileOpen: { type: Boolean, default: false },
});
const emit = defineEmits(["close"]);

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const chat = useChatStore();
const { rooms } = storeToRefs(chat);
const { me } = storeToRefs(auth);
const isAdmin = computed(() => !!me.value?.is_admin);
const showAbout = ref(false);

function roomLabel(room) {
  // Show the other family(ies) in the room (excluding the user's own family).
  const others = (room.families || []).filter((f) => f.id !== me.value?.family_id);
  if (!others.length) return "Family room";
  return others.map((f) => f.name).join(" · ");
}

function isNavActive(name) {
  return route.name === name;
}

function go(name, id) {
  emit("close");
  if (name === "room" && id) router.push({ name: "room", params: { roomId: id } });
  else if (name === "dm" && id) router.push({ name: "room", params: { roomId: id } });
  else router.push({ name });
}

watch(
  () => route.name,
  () => emit("close")
);
</script>

<template>
  <aside class="sidebar" :class="{ open: mobileOpen }">
    <button class="mobile-close" @click="emit('close')">✕</button>
    <div class="me-card">
      <Avatar :user="me" size="lg" />
      <div class="me-info">
        <div class="me-name">{{ me?.display_name }}</div>
        <div class="me-handle">@{{ me?.handle }}</div>
      </div>
    </div>

    <nav class="nav">
      <button class="nav-item" :class="{ active: isNavActive('chat') }" @click="go('chat')">
        <span class="nav-icon">🌐</span> Group Chat
      </button>
      <button v-if="isAdmin" class="nav-item" :class="{ active: isNavActive('members') }" @click="go('members')">
        <span class="nav-icon">👥</span> Members
      </button>
      <button class="nav-item" :class="{ active: isNavActive('families') }" @click="go('families')">
        <span class="nav-icon">🏠</span> Families
      </button>
      <button class="nav-item" :class="{ active: isNavActive('gallery') }" @click="go('gallery')">
        <span class="nav-icon">🖼️</span> Gallery
      </button>
      <button class="nav-item" :class="{ active: isNavActive('settings') }" @click="go('settings')">
        <span class="nav-icon">⚙️</span> Settings
      </button>
    </nav>

    <div class="dms-section">
      <div class="dms-header">Family Rooms</div>
      <button
        v-for="room in rooms"
        :key="room.id"
        class="dm-item"
        :class="{ active: isNavActive('room') && Number(route.params.roomId) === room.id }"
        @click="go('room', room.id)"
      >
        <span class="room-avatar">{{ roomLabel(room) === "Family room" ? "💬" : "🏠" }}</span>
        <div class="dm-info">
          <div class="dm-name">{{ roomLabel(room) }}</div>
          <div class="dm-preview">{{ room.last_message?.text || "No messages yet" }}</div>
        </div>
      </button>
      <div v-if="rooms.length === 0" class="dms-empty">
        No rooms yet.
        <router-link to="/families">Chat with a family →</router-link>
      </div>
    </div>

    <div class="sidebar-foot">
      <button class="nav-item about-item" @click="showAbout = true">
        <span class="nav-icon">ℹ️</span> About this app
      </button>
    </div>
  </aside>
  <AboutModal v-model:open="showAbout" />
  <button v-if="mobileOpen" class="mobile-overlay" @click="emit('close')"></button>
</template>

<style scoped>
.sidebar {
  width: 280px;
  background: var(--bg-elevated);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow-y: auto;
}
.me-card {
  display: flex;
  gap: 12px;
  padding: 16px;
  border-bottom: 1px solid var(--border);
  align-items: center;
}
.me-info { min-width: 0; }
.me-name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.me-handle { font-size: 12px; color: var(--text-muted); }
.nav { padding: 12px 8px; border-bottom: 1px solid var(--border); }
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 14px;
  transition: background 0.15s;
  text-align: left;
}
.nav-item:hover { background: var(--bg-hover); }
.nav-item.active { background: var(--accent-soft); color: var(--accent-hover); }
.nav-icon { font-size: 16px; width: 20px; text-align: center; }
.dms-section { flex: 1; padding: 12px 8px; overflow-y: auto; }
.dms-header {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  padding: 4px 12px 8px;
}
.dm-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  transition: background 0.15s;
  text-align: left;
}
.dm-item:hover { background: var(--bg-hover); }
.dm-item.active { background: var(--accent-soft); }
.room-avatar {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  background: var(--bg-hover);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  flex-shrink: 0;
}
.dm-info { flex: 1; min-width: 0; }
.dm-name { font-weight: 500; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dm-preview {
  font-size: 12px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dms-empty {
  padding: 16px 12px;
  font-size: 13px;
  color: var(--text-muted);
}
.sidebar-foot {
  border-top: 1px solid var(--border);
  padding: 8px;
  flex-shrink: 0;
}
.about-item { color: var(--text-muted); font-size: 13px; }
.mobile-close { display: none; }

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    inset: 0;
    z-index: 40;
    transform: translateX(-100%);
    transition: transform 0.2s;
    width: 280px;
  }
  .sidebar.open { transform: translateX(0); }
  .mobile-close {
    display: block;
    position: absolute;
    top: 12px;
    right: 12px;
    font-size: 16px;
    color: var(--text-muted);
    z-index: 2;
  }
  .mobile-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 39;
    border: none;
  }
}
</style>
