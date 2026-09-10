<script setup>
import { ref } from "vue";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import { api } from "@/api";
import { toast } from "@/composables/useToasts";

const props = defineProps({
  message: Object,
  channel: { type: String, default: "group" },
  peerId: { type: Number, default: null },
});
const emit = defineEmits(["send-typing"]);

const auth = useAuthStore();
const chat = useChatStore();
const me = auth.user;

const text = ref("");
const replyToId = ref(null);
const replyPreview = ref(null);
const attachFile = ref(null);
const uploading = ref(false);
const fileInput = ref(null);
let typingSentAt = 0;

function onInput() {
  // Typing indicators only apply to the group chat; family rooms don't use
  // them (noisy with multiple members).
  if (text.value.length > 0 && props.channel === "group") {
    const now = Date.now();
    if (now - typingSentAt > 1500) {
      typingSentAt = now;
      chat.sendTyping("group");
    }
  }
}

async function onFileSelected(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    toast("File too large (max 10 MB)", "error");
    fileInput.value.value = "";
    return;
  }
  // Always start with no attachment so a thumbnail only appears once the
  // upload has actually succeeded and returned a URL.
  attachFile.value = null;
  uploading.value = true;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const result = await api.upload("/api/files/upload", fd);
    if (result && result.url) attachFile.value = result;
    else toast("Upload failed — no file returned", "error");
  } catch (err) {
    toast(err.message || "Upload failed", "error");
  } finally {
    // Reset in case the upload was interrupted (e.g. page hidden/aborted) and
    // the await never settled, which would otherwise leave the spinner stuck.
    uploading.value = false;
    fileInput.value.value = "";
  }
}

async function submit() {
  if (uploading.value) return;
  const trimmed = text.value.trim();
  const fileUrl = attachFile.value?.url;
  // Never send an empty message: guard on the real file URL, not just the
  // presence of the object, so a stale/partial attachment can't slip through.
  if (!trimmed && !fileUrl) return;
  const payload = {
    text: trimmed,
    file_url: fileUrl,
    file_name: attachFile.value?.filename,
    file_content_type: attachFile.value?.content_type,
  };
  try {
    if (props.channel === "group") {
      await chat.sendGroupMessage({ ...payload, replyToId: replyToId.value });
    } else {
      // channel === "room": peerId is the room id.
      await chat.sendRoomMessage(props.peerId, payload);
    }
  } catch (err) {
    // Keep the text + attachment so the user can retry.
    toast(err.message || "Message failed to send", "error");
    return;
  }
  text.value = "";
  attachFile.value = null;
  replyToId.value = null;
  replyPreview.value = null;
  emit("send-typing");
}

function keydown(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
}

function setReply() {
  replyToId.value = props.message?.id ?? null;
  replyPreview.value = props.message;
}

function cancelReply() {
  replyToId.value = null;
  replyPreview.value = null;
}

function isImage(file) {
  return file?.content_type?.startsWith("image/");
}
</script>

<template>
  <div class="compose">
    <div v-if="replyPreview" class="reply-bar">
      <span class="reply-label">Replying to {{ replyPreview.author.display_name }}:</span>
      <span class="reply-text">{{ replyPreview.text }}</span>
      <button class="reply-cancel" @click="cancelReply">✕</button>
    </div>
    <div v-if="attachFile" class="attach-bar">
      <img v-if="isImage(attachFile)" :src="attachFile.url" class="attach-thumb" />
      <span v-else class="attach-name">📎 {{ attachFile.filename }}</span>
      <button class="reply-cancel" @click="attachFile = null">✕</button>
    </div>
    <div class="compose-row">
      <input ref="fileInput" type="file" class="file-input" @change="onFileSelected" />
      <button class="attach-btn" title="Attach file" @click="fileInput.click">📎</button>
      <textarea
        v-model="text"
        class="compose-text"
        rows="1"
        placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
        @input="onInput"
        @keydown="keydown"
      />
      <button class="btn send-btn" :disabled="(!text.trim() && !attachFile?.url) || uploading" @click="submit">
        <span v-if="uploading">…</span>
        <span v-else>Send</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.compose {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg-elevated);
}
.reply-bar,
.attach-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  margin-bottom: 8px;
  background: var(--accent-soft);
  border-radius: var(--radius-sm);
  font-size: 13px;
}
.reply-label { color: var(--accent-hover); font-weight: 500; white-space: nowrap; }
.reply-text {
  flex: 1;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.reply-cancel { color: var(--text-muted); font-size: 12px; }
.attach-thumb { width: 32px; height: 32px; object-fit: cover; border-radius: 4px; }
.attach-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.compose-row { display: flex; align-items: flex-end; gap: 8px; }
.file-input { display: none; }
.attach-btn {
  font-size: 18px;
  padding: 8px;
  border-radius: var(--radius-sm);
  transition: background 0.15s;
}
.attach-btn:hover { background: var(--bg-hover); }
.compose-text {
  flex: 1;
  max-height: 140px;
  overflow-y: auto;
}
.send-btn { align-self: stretch; }
</style>
