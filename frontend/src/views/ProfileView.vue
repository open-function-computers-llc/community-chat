<script setup>
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { api } from "@/api";
import { toast } from "@/composables/useToasts";
import Avatar from "@/components/Avatar.vue";

const router = useRouter();
const auth = useAuthStore();

const displayName = ref("");
const bio = ref("");
const avatarFile = ref(null);
const avatarPreview = ref(null);
const saving = ref(false);

onMounted(async () => {
  if (auth.user) {
    displayName.value = auth.user.display_name || "";
    bio.value = auth.user.bio || "";
  }
});

async function onFile(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    toast("Please choose an image file", "error");
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  const result = await api.upload("/api/files/upload", fd);
  avatarFile.value = result;
  avatarPreview.value = result.url;
}

async function save() {
  saving.value = true;
  try {
    const updated = await api.patch("/api/auth/me/profile", {
      display_name: displayName.value.trim() || null,
      bio: bio.value.trim() || null,
      avatar_url: avatarFile.value?.url || null,
    });
    auth.user = updated;
    toast("Profile saved!", "success");
    router.push("/");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="profile-page">
    <div class="profile-card card">
      <h1>Welcome, {{ auth.user?.handle }}! 🎉</h1>
      <p class="subtitle">Set up your profile before joining the chat.</p>

      <div class="avatar-row">
        <div class="avatar avatar-lg">
          <img v-if="avatarPreview" :src="avatarPreview" alt="" />
          <template v-else>{{ (displayName || auth.user?.display_name || "?").slice(0, 2).toUpperCase() }}</template>
        </div>
        <label class="btn btn-ghost btn-sm file-label">
          <input type="file" accept="image/*" @change="onFile" />
          Upload avatar
        </label>
      </div>

      <form @submit.prevent="save" class="profile-form">
        <label>Display name
          <input v-model="displayName" placeholder="Your name" maxlength="64" required />
        </label>
        <label>Bio (optional)
          <textarea v-model="bio" rows="3" placeholder="Tell everyone a little about yourself…" maxlength="500" />
        </label>
        <button class="btn submit-btn" type="submit" :disabled="saving">
          {{ saving ? "Saving…" : "Save & join the chat" }}
        </button>
        <button class="btn btn-ghost skip-btn" type="button" @click="router.push('/')">
          Skip for now
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.profile-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  /* Centered via the card's margin:auto (see LoginView) — not justify-content,
     so the top stays reachable when the form is taller than the viewport. */
  overflow-y: auto;
  padding: calc(20px + env(safe-area-inset-top)) calc(20px + env(safe-area-inset-right))
    calc(20px + env(safe-area-inset-bottom)) calc(20px + env(safe-area-inset-left));
}
.profile-card {
  width: 100%;
  max-width: 480px;
  padding: 32px;
  margin: auto;
}
.profile-card h1 { font-size: 22px; }
.subtitle { color: var(--text-muted); margin: 6px 0 20px; font-size: 14px; }
.avatar-row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 24px;
}
.file-label { position: relative; overflow: hidden; display: inline-block; }
.file-label input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
  width: 100%;
}
.profile-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.profile-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-muted);
  font-weight: 500;
}
.submit-btn { margin-top: 8px; }
.skip-btn { justify-content: center; }
</style>
