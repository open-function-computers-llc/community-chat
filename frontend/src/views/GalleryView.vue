<script setup>
import { ref, onMounted } from "vue";
import { storeToRefs } from "pinia";
import { useAuthStore } from "@/stores/auth";
import { fileDate } from "@/composables/useTime";
import { toast } from "@/composables/useToasts";
import Sidebar from "@/components/Sidebar.vue";

const auth = useAuthStore();
const { me } = storeToRefs(auth);
const mobileSidebar = ref(false);
const myFiles = ref([]);
const lightbox = ref(null);
const deleting = ref(null); // id of the file currently being deleted

onMounted(async () => {
  const { api } = await import("@/api");
  myFiles.value = await api.get("/api/files/me");
});

function isImage(f) {
  return f.content_type.startsWith("image/");
}

function timeStr(iso) {
  return fileDate(iso);
}

async function removeFile(f) {
  if (!confirm(`Delete "${f.filename}"? This can't be undone. Messages that referenced it will show a placeholder.`)) return;
  const { api } = await import("@/api");
  deleting.value = f.id;
  try {
    await api.del(`/api/files/${f.id}`);
    myFiles.value = myFiles.value.filter((x) => x.id !== f.id);
    toast("File deleted", "success");
  } catch (err) {
    toast(err.message || "Could not delete file", "error");
  } finally {
    deleting.value = null;
  }
}
</script>

<template>
  <div class="gallery-layout">
    <Sidebar :mobile-open="mobileSidebar" @close="mobileSidebar = false" />

    <section class="gallery-main">
      <header class="page-header">
        <button class="mobile-toggle" @click="mobileSidebar = true">☰</button>
        <h1>My Gallery</h1>
        <span class="count">{{ myFiles.length }} file{{ myFiles.length === 1 ? "" : "s" }}</span>
      </header>

      <div v-if="myFiles.length === 0" class="empty-state">
        <div class="empty-icon">📭</div>
        <h2>No files yet</h2>
        <p>Files you upload in chat will appear here.</p>
      </div>

      <div v-else class="file-grid">
        <div v-for="f in myFiles" :key="f.id" class="file-card">
          <div class="file-preview">
            <img v-if="isImage(f)" :src="f.url" :alt="f.filename" @click="lightbox = f" />
            <a v-else :href="f.url" target="_blank" class="file-file-icon">📄</a>
            <button
              v-if="deleting !== f.id"
              class="file-delete"
              title="Delete this file"
              aria-label="Delete this file"
              @click="removeFile(f)"
            >✕</button>
            <span v-else class="file-deleting">…</span>
          </div>
          <div class="file-info">
            <span class="file-name" :title="f.filename">{{ f.filename }}</span>
            <span class="file-meta">{{ (f.size / 1024).toFixed(1) }} KB · {{ timeStr(f.created_at) }}</span>
          </div>
        </div>
      </div>
    </section>

    <div v-if="lightbox" class="lightbox" @click="lightbox = null">
      <img :src="lightbox.url" class="lightbox-img" @click.stop />
      <button class="lightbox-close" @click="lightbox = null">✕</button>
    </div>
  </div>
</template>

<style scoped>
.gallery-layout {
  flex: 1;
  display: flex;
  overflow: hidden;
}
.gallery-main {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}
.page-header h1 { font-size: 22px; }
.count { color: var(--text-muted); font-size: 14px; }
.mobile-toggle { display: none; }
.empty-state {
  text-align: center;
  color: var(--text-muted);
  padding: 60px 20px;
}
.empty-icon { font-size: 56px; margin-bottom: 16px; }
.empty-state h2 { color: var(--text); margin-bottom: 8px; }
.file-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 14px;
  max-width: 900px;
}
.file-card {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.file-preview {
  aspect-ratio: 1;
  background: var(--bg-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  position: relative;
}
.file-preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  cursor: zoom-in;
}
.file-file-icon { font-size: 48px; }
.file-delete {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, background 0.15s;
}
.file-card:hover .file-delete,
.file-delete:focus-visible { opacity: 1; }
.file-delete:hover { background: var(--danger, #dc3545); }
.file-deleting {
  position: absolute;
  top: 8px;
  right: 10px;
  color: #fff;
  font-size: 13px;
}
.file-info {
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.file-name {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-meta { font-size: 11px; color: var(--text-muted); }
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
.lightbox-close {
  position: absolute;
  top: 20px;
  right: 20px;
  font-size: 24px;
  color: #fff;
}

@media (max-width: 768px) {
  .mobile-toggle { display: inline-block; font-size: 18px; color: var(--text); }
  .file-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
