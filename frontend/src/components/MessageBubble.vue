<script setup>
import { ref } from "vue";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import { timeStr } from "@/composables/useTime";
import Avatar from "./Avatar.vue";
import Reactions from "./Reactions.vue";
import Modal from "./Modal.vue";

const props = defineProps({
  message: Object,
  channel: { type: String, default: "group" },
  peerId: { type: Number, default: null },
  isOwn: Boolean,
  // False when this message is a continuation of the previous one (same
  // author, short gap): the avatar + name + timestamp are suppressed.
  firstInGroup: { type: Boolean, default: true },
});

const auth = useAuthStore();
const chat = useChatStore();
const menuOpen = ref(false);
const editing = ref(false);
const editText = ref(props.message.text || "");
const lightbox = ref(false);
const avatarModal = ref(false);

// Only other people's avatars open the zoom modal (clicking your own is a no-op).
function openAvatar() {
  if (props.isOwn) return;
  avatarModal.value = true;
}

async function saveEdit() {
  if (props.channel !== "group") return;
  const trimmed = editText.value.trim();
  if (!trimmed || trimmed === props.message.text) {
    editing.value = false;
    return;
  }
  await chat.editGroupMessage(props.message.id, trimmed);
  editing.value = false;
}

async function deleteMessage() {
  if (!confirm("Delete this message?")) return;
  await chat.deleteGroupMessage(props.message.id);
}

function isImage(file) {
  return file?.file_content_type?.startsWith("image/");
}

function initials(user) {
  const name = user?.display_name || user?.handle || "?";
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

function openLightbox() {
  lightbox.value = true;
}
</script>

<template>
  <div class="bubble-wrap" :class="{ own: isOwn, continuation: !firstInGroup }">
    <Avatar
      v-if="!isOwn && channel === 'group' && firstInGroup"
      :user="message.author"
      size="sm"
      class="avatar-clickable"
      role="button"
      :aria-label="'View ' + (message.author.display_name || 'avatar') + 's avatar'"
      @click="openAvatar"
    />
    <div class="bubble-col">
      <div v-if="!isOwn && firstInGroup" class="meta-row">
        <span class="author">{{ message.author.display_name }}</span>
        <span class="time">{{ timeStr(message.created_at) }}</span>
        <span v-if="message.edited_at" class="edited">(edited)</span>
      </div>

      <div v-if="message.reply_to" class="reply-quote">
        <span class="quote-author">{{ message.reply_to.author.display_name }}:</span>
        {{ message.reply_to.text }}
      </div>

      <div class="bubble" :class="{ 'no-bg': isOwn }">
        <div v-if="editing" class="edit-area">
          <textarea v-model="editText" rows="2" class="edit-input" @keydown.enter.prevent="saveEdit" />
          <div class="edit-actions">
            <button class="btn btn-sm" @click="saveEdit">Save</button>
            <button class="btn btn-ghost btn-sm" @click="editing = false; editText = message.text">Cancel</button>
          </div>
        </div>
        <template v-else>
          <p v-if="message.text" class="text" @dblclick="channel === 'group' && isOwn && (editing = true)">
            {{ message.text }}
          </p>
          <div v-if="message.file_url" class="file-attach">
            <img
              v-if="isImage(message)"
              :src="message.file_url"
              class="file-img"
              @click="openLightbox"
            />
            <a v-else :href="message.file_url" target="_blank" class="file-link">
              📎 {{ message.file_name || "attachment" }}
            </a>
          </div>
        </template>
      </div>

      <Reactions
        :reactions="message.reactions"
        :message-id="message.id"
        :channel="channel"
        :room-id="channel === 'room' ? message.room_id : null"
        :peer-id="peerId"
        :is-own="isOwn"
      />

      <div v-if="isOwn && channel === 'group'" class="actions">
        <button class="action-btn" @click="menuOpen = !menuOpen">⋯</button>
        <div v-if="menuOpen" class="menu">
          <button @click="editing = true; menuOpen = false; editText = message.text">✏️ Edit</button>
          <button class="danger" @click="deleteMessage">🗑️ Delete</button>
        </div>
      </div>
    </div>

    <div v-if="lightbox && isImage(message)" class="lightbox" @click="lightbox = false">
      <img :src="message.file_url" class="lightbox-img" @click.stop />
    </div>

    <Modal v-model:open="avatarModal" :title="message.author?.display_name || 'Avatar'">
      <img
        v-if="message.author?.avatar_url"
        :src="message.author.avatar_url"
        class="avatar-zoom"
        :alt="message.author.display_name || 'avatar'"
      />
      <div v-else class="avatar-zoom-placeholder">{{ initials(message.author) }}</div>
    </Modal>
  </div>
</template>

<style scoped>
.bubble-wrap {
  display: flex;
  gap: 8px;
  padding: 4px 0;
  position: relative;
}
.bubble-wrap.own { flex-direction: row-reverse; }
/* Continuation of the same author: tighter vertical rhythm, and indent the
   bubble so it lines up under the first message of the run (past the avatar
   column that's only rendered on the first message). */
.bubble-wrap.continuation { padding-top: 0; }
.bubble-wrap.continuation:not(.own) .bubble-col { padding-left: 36px; }
.bubble-col {
  max-width: 70%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  position: relative;
}
.bubble-wrap.own .bubble-col { align-items: flex-end; }
.meta-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-bottom: 2px;
  font-size: 12px;
}
.author { font-weight: 600; }
.time { color: var(--text-muted); }
.edited { color: var(--text-muted); font-size: 11px; }
.reply-quote {
  font-size: 12px;
  color: var(--text-muted);
  border-left: 2px solid var(--accent);
  padding: 4px 8px;
  margin-bottom: 4px;
  background: var(--bg-hover);
  border-radius: 4px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.quote-author { color: var(--accent-hover); font-weight: 500; }
.bubble {
  padding: 8px 12px;
  border-radius: var(--radius);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  word-break: break-word;
  max-width: 100%;
}
.bubble.no-bg { background: var(--accent-soft); border-color: var(--accent); }
.text { white-space: pre-wrap; font-size: 14px; }
.file-attach { margin-top: 4px; }
.file-img {
  max-width: 320px;
  max-height: 240px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  display: block;
}
.file-link { font-size: 13px; display: inline-block; }
.edit-area { width: 100%; min-width: 240px; }
.edit-input { width: 100%; font-size: 14px; }
.edit-actions { display: flex; gap: 6px; margin-top: 6px; }
.actions { position: absolute; top: -4px; right: 0; }
.action-btn {
  font-size: 14px;
  color: var(--text-muted);
  padding: 2px 6px;
  border-radius: 4px;
}
.action-btn:hover { background: var(--bg-hover); }
.menu {
  position: absolute;
  top: 100%;
  right: 0;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow);
  z-index: 20;
  min-width: 120px;
  overflow: hidden;
}
.menu button {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 12px;
  font-size: 13px;
}
.menu button:hover { background: var(--bg-hover); }
.menu button.danger { color: var(--danger); }
.lightbox {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: zoom-out;
}
.lightbox-img { max-width: 92vw; max-height: 92vh; object-fit: contain; }
.avatar-clickable { cursor: zoom-in; }
/* The add-reaction button (inside the Reactions child) is hidden until the
   whole message is hovered, so it never takes up vertical space. */
.bubble-col:hover :deep(.reaction-add) { opacity: 1; pointer-events: auto; }
.avatar-zoom {
  max-width: 100%;
  max-height: 100%;
  width: auto;
  height: auto;
  border-radius: var(--radius-sm);
  display: block;
}
.avatar-zoom-placeholder {
  width: 200px;
  height: 200px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 64px;
  font-weight: 600;
  color: #fff;
  background: var(--accent);
}
</style>
