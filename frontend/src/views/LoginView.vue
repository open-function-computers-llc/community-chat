<script setup>
import { ref, onMounted } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { toast } from "@/composables/useToasts";

const router = useRouter();
const route = useRoute();
const auth = useAuthStore();

const tab = ref("login");
const handle = ref("");
const password = ref("");
const inviteCode = ref("");
const displayName = ref("");
const email = ref("");
const phone = ref("");
const error = ref("");
const loading = ref(false);

onMounted(() => {
  if (route.query.code) {
    inviteCode.value = String(route.query.code);
    tab.value = "register";
  }
});

async function submit() {
  error.value = "";
  loading.value = true;
  try {
    if (tab.value === "login") {
      await auth.login({ handle: handle.value.trim(), password: password.value });
      toast("Welcome back!", "success");
      router.push("/");
    } else {
      const data = await auth.register({
        invite_code: inviteCode.value.trim(),
        handle: handle.value.trim(),
        password: password.value,
        display_name: displayName.value.trim() || null,
        email: email.value.trim() || null,
        phone: phone.value.trim() || null,
      });
      toast("Account created!", "success");
      if (data.is_new_user) router.push("/profile");
      else router.push("/");
    }
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card card">
      <div class="login-header">
        <div class="login-logo">💬</div>
        <h1>Community Chat</h1>
        <p class="subtitle">Sign in to catch up with everyone</p>
      </div>

      <div class="tabs">
        <button :class="{ active: tab === 'login' }" @click="tab = 'login'">Sign in</button>
        <button :class="{ active: tab === 'register' }" @click="tab = 'register'">Join with invite</button>
      </div>

      <form @submit.prevent="submit" class="login-form">
        <template v-if="tab === 'register'">
          <label>Invite code
            <input v-model="inviteCode" placeholder="e.g. ABC12345" autocomplete="off" required />
          </label>
          <label>Display name
            <input v-model="displayName" placeholder="Your name" maxlength="64" />
          </label>
          <label>Username (handle)
            <input v-model="handle" placeholder="yourname" minlength="3" maxlength="32" required />
          </label>
          <label>Email (optional)
            <input v-model="email" type="email" placeholder="you@example.com" />
          </label>
          <label>Phone (optional)
            <input v-model="phone" type="tel" placeholder="(555) 123-4567" />
          </label>
        </template>

        <label>
          {{ tab === "login" ? "Username" : "Confirm username (handle)" }}
          <input v-model="handle" placeholder="yourname" minlength="3" maxlength="32" required />
        </label>
        <label>Password
          <input v-model="password" type="password" placeholder="••••••••" minlength="6" required />
        </label>

        <p v-if="error" class="error">{{ error }}</p>
        <button class="btn submit-btn" type="submit" :disabled="loading">
          {{ loading ? "…" : tab === "login" ? "Sign in" : "Create account" }}
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  /* No justify-content: center — centering a flex item that overflows clips its
     TOP (unreachable). The card uses margin:auto instead, which centers it when
     it fits but pins it to the top when it's taller than the viewport. */
  /* When the card is taller than the viewport (e.g. the register tab on a
     small iPhone) this scrolls it instead of clipping. */
  overflow-y: auto;
  /* Keep the card clear of the iOS safe area (URL bar / home indicator). */
  padding: calc(20px + env(safe-area-inset-top)) calc(20px + env(safe-area-inset-right))
    calc(20px + env(safe-area-inset-bottom)) calc(20px + env(safe-area-inset-left));
  background: radial-gradient(circle at 50% 30%, #1e1b4b 0%, var(--bg) 60%);
}
.login-card {
  width: 100%;
  max-width: 420px;
  padding: 32px;
  /* Centers the card when it's short; when it's taller than the viewport this
     (instead of justify-content:center) keeps the top reachable and lets the
     page scroll to the bottom. */
  margin: auto;
}
.login-header { text-align: center; margin-bottom: 24px; }
.login-logo { font-size: 48px; }
.login-header h1 { font-size: 24px; margin-top: 8px; }
.subtitle { color: var(--text-muted); font-size: 14px; margin-top: 4px; }
.tabs {
  display: flex;
  border-bottom: 1px solid var(--border);
  margin-bottom: 20px;
}
.tabs button {
  flex: 1;
  padding: 10px;
  color: var(--text-muted);
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tabs button.active {
  color: var(--text);
  border-bottom-color: var(--accent);
}
.login-form { display: flex; flex-direction: column; gap: 14px; }
.login-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-muted);
  font-weight: 500;
}
.error { color: var(--danger); font-size: 13px; }
.submit-btn { margin-top: 8px; width: 100%; }
</style>
