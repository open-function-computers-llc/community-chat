<script setup>
import { storeToRefs } from "pinia";
import { useCommunity } from "@/composables/useCommunity";
import { useAuthStore } from "@/stores/auth";

const emit = defineEmits(["logout"]);
const { name: communityName } = useCommunity();
const auth = useAuthStore();
const { wsStatus } = storeToRefs(auth);
</script>

<template>
  <nav class="navbar">
    <div class="brand">
      <span class="brand-icon">💬</span>
      <span class="brand-name">{{ communityName }}</span>
    </div>
    <div class="nav-right">
      <div class="status" :class="wsStatus">
        <span class="dot" :class="wsStatus === 'connected' ? 'dot-green' : 'dot-gray'" />
        <span class="status-text">{{ wsStatus }}</span>
      </div>
      <button class="btn btn-ghost btn-sm logout-btn" @click="emit('logout')">
        Log out
      </button>
    </div>
  </nav>
</template>

<style scoped>
.navbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.brand { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.brand-icon { font-size: 20px; }
.nav-right { display: flex; align-items: center; gap: 12px; }
.status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
}
.logout-btn {
  font-size: 13px;
  color: var(--text-muted);
  padding: 4px 10px;
}
.logout-btn:hover { color: var(--text); background: var(--bg-hover); }

@media (max-width: 768px) {
  .status-text { display: none; }
}
</style>
