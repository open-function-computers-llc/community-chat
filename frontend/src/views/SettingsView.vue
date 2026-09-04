<script setup>
import { computed, ref, onMounted } from "vue";
import { storeToRefs } from "pinia";
import { useAuthStore } from "@/stores/auth";
import { useFamiliesStore } from "@/stores/families";
import { api } from "@/api";
import { toast } from "@/composables/useToasts";
import { usePush, pushStatus, subscribePush, unsubscribePush } from "@/composables/usePush";
import Sidebar from "@/components/Sidebar.vue";
import Avatar from "@/components/Avatar.vue";

const auth = useAuthStore();
const families = useFamiliesStore();
const { me } = storeToRefs(auth);
const isAdmin = computed(() => !!me.value?.is_admin);
const myInvite = computed(() => invites.value[0] || null);
const mobileSidebar = ref(false);

const displayName = ref("");
const email = ref("");
const phone = ref("");
const bio = ref("");
const avatarPreview = ref(null);
const savingProfile = ref(false);

const currentPassword = ref("");
const newPassword = ref("");
const savingPassword = ref(false);

const dnd = ref(false);
const notifyMentions = ref(true);
const notifyReplies = ref(true);
const emailNotifications = ref(false);
const emailAvailable = ref(false);
const savingSettings = ref(false);

const push = usePush();
// Local checkbox state. Bound to the input so a failed subscribe (which leaves
// push.subscribed false) can be visually reverted — Vue won't reset a
// :checked binding whose reactive value didn't change.
const pushToggle = ref(false);

const invites = ref([]);
const inviteMaxUses = ref(1);
const inviteNote = ref("");
const inviteFamilyId = ref(null);
const creatingInvite = ref(false);

onMounted(async () => {
  if (me.value) {
    displayName.value = me.value.display_name || "";
    email.value = me.value.email || "";
    phone.value = me.value.phone || "";
    bio.value = me.value.bio || "";
    avatarPreview.value = me.value.avatar_url;
  }
  await pushStatus();
  pushToggle.value = push.subscribed;
  try {
    const s = await api.get("/api/auth/me/settings");
    dnd.value = s.do_not_disturb;
    notifyMentions.value = s.notify_mentions;
    notifyReplies.value = s.notify_replies;
    emailNotifications.value = s.email_notifications;
  } catch {}
  try {
    const e = await api.get("/api/email/status");
    emailAvailable.value = e.enabled && e.has_email;
  } catch {
    emailAvailable.value = false;
  }
  loadInvites();
  families.load().catch(() => {});
});

async function loadInvites() {
  try {
    invites.value = await api.get("/api/invites");
  } catch {}
}

async function saveProfile() {
  savingProfile.value = true;
  try {
    const updated = await api.patch("/api/auth/me/profile", {
      display_name: displayName.value.trim() || null,
      email: email.value.trim() || null,
      phone: phone.value.trim() || null,
      bio: bio.value.trim() || null,
      avatar_url: avatarPreview.value,
    });
    auth.user = updated;
    toast("Profile updated", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    savingProfile.value = false;
  }
}

async function onAvatarFile(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    toast("Choose an image file", "error");
    return;
  }
  try {
    // Dedicated avatar endpoint: server center-crops to a square, resizes to
    // 500x500, and stores WebP. It updates the profile's avatar_url directly.
    const fd = new FormData();
    fd.append("file", file);
    const updated = await api.upload("/api/auth/me/avatar", fd);
    auth.user = updated;
    avatarPreview.value = updated.avatar_url;
    toast("Avatar updated", "success");
  } catch (err) {
    toast(err.message || "Could not upload avatar", "error");
  }
  // Reset the input so the same file can be re-selected.
  e.target.value = "";
}

async function changePassword() {
  savingPassword.value = true;
  try {
    await api.post("/api/auth/me/password", {
      current_password: currentPassword.value,
      new_password: newPassword.value,
    });
    currentPassword.value = "";
    newPassword.value = "";
    toast("Password changed", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    savingPassword.value = false;
  }
}

async function saveSettings() {
  savingSettings.value = true;
  try {
    await api.put("/api/auth/me/settings", {
      do_not_disturb: dnd.value,
      notify_mentions: notifyMentions.value,
      notify_replies: notifyReplies.value,
      email_notifications: emailNotifications.value,
    });
    toast("Settings saved", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    savingSettings.value = false;
  }
}

async function onPushToggle(checked) {
  if (checked) {
    const ok = await subscribePush();
    pushToggle.value = ok; // revert the checkbox if the subscribe failed
    if (!ok && push.error) toast(push.error, "error");
    else toast("Notifications enabled", "success");
  } else {
    const ok = await unsubscribePush();
    pushToggle.value = !ok;
    toast(ok ? "Notifications disabled" : "Could not disable notifications", ok ? "success" : "error");
  }
}

async function createInvite() {
  creatingInvite.value = true;
  try {
    const invite = await api.post("/api/invites", {
      max_uses: inviteMaxUses.value,
      note: inviteNote.value.trim() || null,
      family_id: inviteFamilyId.value || null,
    });
    invites.value.unshift(invite);
    inviteNote.value = "";
    toast("Invite created: " + invite.code, "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    creatingInvite.value = false;
  }
}

async function copyInvite(code) {
  await navigator.clipboard.writeText(code);
  toast("Copied to clipboard", "success");
}

async function copyLink(code) {
  await navigator.clipboard.writeText(inviteLink(code));
  toast("Link copied to clipboard", "success");
}

async function revokeInvite(invite) {
  if (!confirm(`Revoke invite ${invite.code}?`)) return;
  await api.del(`/api/invites/${invite.id}`);
  await loadInvites();
  toast("Invite revoked", "success");
}

function inviteLink(code) {
  return `${window.location.origin}/login?code=${code}`;
}
</script>

<template>
  <div class="settings-layout">
    <Sidebar :mobile-open="mobileSidebar" @close="mobileSidebar = false" />

    <section class="settings-main">
      <header class="page-header">
        <button class="mobile-toggle" @click="mobileSidebar = true">☰</button>
        <h1>Settings</h1>
      </header>

      <div class="settings-grid">
        <div class="card settings-card">
          <h2>Profile</h2>
          <div class="avatar-row">
            <Avatar :user="{ ...me, avatar_url: avatarPreview }" size="lg" />
            <label class="btn btn-ghost btn-sm file-label">
              <input type="file" accept="image/*" @change="onAvatarFile" />
              Change avatar
            </label>
          </div>
          <label>Display name
            <input v-model="displayName" maxlength="64" />
          </label>
          <label>Email
            <input v-model="email" type="email" />
          </label>
          <label>Phone
            <input v-model="phone" type="tel" />
          </label>
          <label>Bio
            <textarea v-model="bio" rows="3" maxlength="500" />
          </label>
          <button class="btn" @click="saveProfile" :disabled="savingProfile">
            {{ savingProfile ? "Saving…" : "Save profile" }}
          </button>
        </div>

        <div class="card settings-card">
          <h2>Notifications</h2>

          <!-- Web push (works even when the app/tab is closed) -->
          <div class="push-block">
            <label class="toggle-row">
              <span class="push-label">
                <span>Push notifications</span>
                <small v-if="!push.supported">Not supported in this browser</small>
                <small v-else-if="push.permission === 'denied'">Blocked — re-enable in your browser's site settings</small>
                <small v-else-if="push.permission === 'default'">Alerts when a new message arrives and the app is closed</small>
                <small v-else-if="push.subscribed">On — you'll get alerts for new group &amp; family messages</small>
              </span>
              <input
                type="checkbox"
                class="toggle"
                v-model="pushToggle"
                :disabled="!push.supported || push.permission === 'denied' || push.busy"
                @change="onPushToggle($event.target.checked)"
              />
            </label>
            <p v-if="push.error" class="push-error">{{ push.error }}</p>
          </div>

          <!-- Email notifications (delivered to the profile email address) -->
          <div class="push-block">
            <label class="toggle-row">
              <span class="push-label">
                <span>Email notifications</span>
                <small v-if="!emailAvailable && !me.email">Add an email in your profile to use this</small>
                <small v-else-if="!emailAvailable">Not available on this server yet</small>
                <small v-else-if="emailNotifications">On — new messages will be emailed to {{ me.email }}</small>
                <small v-else>Off — you'll only get alerts in the app</small>
              </span>
              <input
                type="checkbox"
                class="toggle"
                v-model="emailNotifications"
                :disabled="!emailAvailable"
              />
            </label>
          </div>

          <label class="toggle-row">
            <span>Do not disturb</span>
            <input v-model="dnd" type="checkbox" class="toggle" />
          </label>
          <label class="toggle-row">
            <span>Notify on mentions</span>
            <input v-model="notifyMentions" type="checkbox" class="toggle" />
          </label>
          <label class="toggle-row">
            <span>Notify on replies</span>
            <input v-model="notifyReplies" type="checkbox" class="toggle" />
          </label>
          <button class="btn" @click="saveSettings" :disabled="savingSettings">
            {{ savingSettings ? "Saving…" : "Save settings" }}
          </button>
        </div>

        <div class="card settings-card">
          <h2>Change password</h2>
          <label>Current password
            <input v-model="currentPassword" type="password" autocomplete="current-password" />
          </label>
          <label>New password
            <input v-model="newPassword" type="password" minlength="6" autocomplete="new-password" />
          </label>
          <button class="btn" @click="changePassword" :disabled="savingPassword || !currentPassword || newPassword.length < 6">
            {{ savingPassword ? "Changing…" : "Update password" }}
          </button>
        </div>

        <div class="card settings-card">
          <h2>{{ isAdmin ? "Invite friends" : "Invite your family" }}</h2>

          <!-- Non-admin: read-only view of the invite that brought them in -->
          <template v-if="!isAdmin">
            <p class="hint">
              Use this invite to add the rest of your family members. Once it's used up
              ({{ myInvite ? `${myInvite.times_used}/${myInvite.max_uses}` : "—" }}),
              ask an admin for a new one.
            </p>
            <div v-if="myInvite" class="my-invite">
              <div class="my-invite-row">
                <code class="invite-code">{{ myInvite.code }}</code>
                <button class="btn btn-ghost btn-sm" @click="copyInvite(myInvite.code)">Copy code</button>
              </div>
              <div class="my-invite-row">
                <span class="invite-link-label">Link:</span>
                <code class="invite-link">{{ inviteLink(myInvite.code) }}</code>
                <button class="btn btn-ghost btn-sm" @click="copyLink(myInvite.code)">Copy link</button>
              </div>
              <div class="invite-meta">
                <span v-if="myInvite.family_name" class="family-badge">🏠 {{ myInvite.family_name }}</span>
                <span v-if="myInvite.note">📝 {{ myInvite.note }}</span>
                <span>{{ myInvite.times_used }}/{{ myInvite.max_uses }} used</span>
                <span v-if="myInvite.is_active" class="dot dot-green" title="Active" />
                <span v-else class="dot dot-gray" title="Used up or revoked" />
              </div>
            </div>
            <div v-else class="invites-empty">
              No invite is on file for your account. Ask an admin to create one for you.
            </div>
          </template>

          <!-- Admin: create + manage invites -->
          <template v-else>
            <p class="hint">
              Share an invite code with a family member or friend so they can join.
              Pick a family to add them to, or leave it as "No family".
              Manage families under <router-link to="/families">Families</router-link>.
            </p>
            <div class="invite-create">
              <div class="invite-row">
                <label class="invite-label">
                  Family
                  <select v-model="inviteFamilyId" class="family-select">
                    <option :value="null">No family</option>
                    <option v-for="f in families.families" :key="f.id" :value="f.id">
                      {{ f.name }}
                    </option>
                  </select>
                </label>
                <label class="invite-label">
                  Uses
                  <input v-model.number="inviteMaxUses" type="number" min="1" max="100" class="invite-num" />
                </label>
                <label class="invite-label flex1">
                  Note
                  <input v-model="inviteNote" placeholder="e.g. for Grandma" maxlength="200" />
                </label>
              </div>
              <button class="btn" @click="createInvite" :disabled="creatingInvite">
                {{ creatingInvite ? "Creating…" : "Create invite" }}
              </button>
            </div>

            <div class="invite-list">
              <div v-for="inv in invites" :key="inv.id" class="invite-item" :class="{ inactive: !inv.is_active }">
                <div class="invite-code-wrap">
                  <code class="invite-code">{{ inv.code }}</code>
                  <button class="btn btn-ghost btn-sm" @click="copyInvite(inv.code)">Copy</button>
                </div>
                <div class="invite-meta">
                  <span v-if="inv.family_name" class="family-badge">🏠 {{ inv.family_name }}</span>
                  <span v-if="inv.note">📝 {{ inv.note }}</span>
                  <span>{{ inv.times_used }}/{{ inv.max_uses }} used</span>
                  <span v-if="inv.is_active" class="dot dot-green" title="Active" />
                  <span v-else class="dot dot-gray" title="Used up or revoked" />
                </div>
                <button v-if="inv.is_active" class="btn btn-ghost btn-sm danger-text" @click="revokeInvite(inv)">
                  Revoke
                </button>
              </div>
              <div v-if="invites.length === 0" class="invites-empty">No invites yet.</div>
            </div>
          </template>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.settings-layout {
  flex: 1;
  display: flex;
  overflow: hidden;
}
.settings-main {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
.page-header { margin-bottom: 20px; }
.page-header h1 { font-size: 22px; }
.mobile-toggle { display: none; }
.settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
  max-width: 900px;
}
.settings-card h2 { font-size: 16px; margin-bottom: 16px; }
.settings-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.settings-card label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-muted);
  font-weight: 500;
}
.avatar-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.file-label { position: relative; overflow: hidden; display: inline-block; }
.file-label input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
  width: 100%;
}
.hint { color: var(--text-muted); font-size: 13px; }
.toggle-row {
  flex-direction: row !important;
  align-items: center;
  justify-content: space-between;
  color: var(--text) !important;
}
.toggle {
  width: 40px;
  height: 22px;
  appearance: none;
  background: var(--border);
  border-radius: 11px;
  position: relative;
  cursor: pointer;
  transition: background 0.2s;
  flex-shrink: 0;
}
.toggle::after {
  content: "";
  position: absolute;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  top: 2px;
  left: 2px;
  transition: left 0.2s;
}
.toggle:checked { background: var(--accent); }
.toggle:checked::after { left: 20px; }
.push-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
}
.push-label { display: flex; flex-direction: column; gap: 2px; }
.push-label small { color: var(--text-muted); font-size: 11px; font-weight: 400; }
.push-error { color: var(--danger); font-size: 12px; }
.invite-create {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
}
.invite-row { display: flex; gap: 12px; }
.invite-label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--text-muted); }
.invite-label.flex1 { flex: 1; }
.invite-num { width: 70px; }
.family-select { width: 100%; min-width: 140px; }
.family-badge {
  background: var(--accent-soft);
  color: var(--accent-hover);
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 8px;
}
.invite-list { display: flex; flex-direction: column; gap: 10px; }
.invite-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.invite-item.inactive { opacity: 0.6; }
.invite-code-wrap { display: flex; align-items: center; gap: 8px; }
.invite-code {
  font-family: monospace;
  font-size: 14px;
  background: var(--bg);
  padding: 4px 8px;
  border-radius: 4px;
  letter-spacing: 0.05em;
}
.invite-meta {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted);
  align-items: center;
}
.danger-text { color: var(--danger); }
.invites-empty { color: var(--text-muted); font-size: 13px; padding: 8px; }
.my-invite {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  background: var(--accent-soft);
  border-radius: var(--radius-sm);
}
.my-invite-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.invite-link-label { font-size: 12px; color: var(--text-muted); }
.invite-link {
  flex: 1;
  font-size: 12px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  background: var(--bg);
  padding: 6px 10px;
  border-radius: 6px;
}

@media (max-width: 768px) {
  .mobile-toggle { display: inline-block; font-size: 18px; color: var(--text); }
  .invite-row { flex-direction: column; }
}
</style>
