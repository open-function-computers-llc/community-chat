<script setup>
import { computed, ref, onMounted } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import { useFamiliesStore } from "@/stores/families";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import { api } from "@/api";
import { toast } from "@/composables/useToasts";
import Sidebar from "@/components/Sidebar.vue";
import Modal from "@/components/Modal.vue";
import Avatar from "@/components/Avatar.vue";

const families = useFamiliesStore();
const auth = useAuthStore();
const chat = useChatStore();
const router = useRouter();
const { me } = storeToRefs(auth);
const isAdmin = computed(() => !!me.value?.is_admin);

// tel: link for a stored phone number (strip non-digits so it dials cleanly).
function telHref(phone) {
  if (!phone) return null;
  const digits = String(phone).replace(/[^\d+]/g, "");
  return digits ? `tel:${digits}` : null;
}
const mobileSidebar = ref(false);
const chattingFor = ref(null);

async function chatWithFamily(f) {
  if (me.value?.family_id == null) {
    toast("You're not in a family yet", "error");
    return;
  }
  chattingFor.value = f.id;
  try {
    const room = await chat.openRoom(f.id);
    await chat.loadRoomHistory(room.id);
    router.push({ name: "room", params: { roomId: room.id } });
  } catch (e) {
    toast(e.message, "error");
  } finally {
    chattingFor.value = null;
  }
}

const newName = ref("");
const newDesc = ref("");
const creating = ref(false);

const editingId = ref(null);
const editName = ref("");
const editDesc = ref("");
const saving = ref(false);

const uploadingFor = ref(null);
const fileInputs = ref({});

function setFileInput(id, el) {
  if (el) fileInputs.value[id] = el;
}

// Avatar zoom modal (reuses the same pattern as the chat's MessageBubble).
// Anyone can open it for a family's avatar, but only your own family's
// avatar uses the upload flow (clicking your own family's avatar opens the
// file picker; everyone else's opens the zoom modal).
const avatarModal = ref(false);
const zoomFamily = ref(null); // { name, avatar_url }
// Which family's member list is expanded (avatar + name + phone/bio/email).
const expandedMembersFor = ref(null);

function toggleMembers(f) {
  expandedMembersFor.value = expandedMembersFor.value === f.id ? null : f.id;
}

function onAvatarClick(f) {
  if (f.id === me.value?.family_id) {
    fileInputs.value[f.id]?.click();
  } else {
    zoomFamily.value = { name: f.name, avatar_url: f.avatar_url };
    avatarModal.value = true;
  }
}

// Per-family inline invite management (admin only). We track one active invite
// per family and reuse it (instead of creating a new code every time), so an
// admin doesn't accidentally generate a pile of unused codes for one family.
const invitingFor = ref(null);
const inviteUses = ref(1);
const inviteResult = ref(null); // { familyId, code, link, uses, id?, fresh? }
const creatingInvite = ref(false);
const invites = ref([]); // all invites (admin) from /api/invites

function inviteLink(code) {
  return `${window.location.origin}/login?code=${code}`;
}

async function loadInvites() {
  try {
    invites.value = await api.get("/api/invites");
  } catch {
    /* non-admins get only their own invite; non-admins don't use this UI */
  }
}

// The currently-active invite for a family (if any).
function activeInviteFor(f) {
  return (
    invites.value.find(
      (i) => i.family_id === f.id && i.is_active && i.times_used < i.max_uses
    ) || null
  );
}

// Total remaining uses across a family's active invites (0 if none). Only
// admins can see the invite list, so this is 0 for everyone else.
function openInviteUses(f) {
  return invites.value
    .filter((i) => i.family_id === f.id && i.is_active)
    .reduce((sum, i) => sum + (i.max_uses - i.times_used), 0);
}

function startInvite(f) {
  if (invitingFor.value === f.id) {
    cancelInvite();
    return;
  }
  const existing = activeInviteFor(f);
  if (existing) {
    // Reuse the family's existing active invite rather than creating a new one.
    inviteResult.value = {
      id: existing.id,
      familyId: f.id,
      code: existing.code,
      link: inviteLink(existing.code),
      uses: existing.max_uses,
      fresh: false,
    };
  } else {
    inviteResult.value = null;
    inviteUses.value = 1;
  }
  invitingFor.value = f.id;
}

function cancelInvite() {
  invitingFor.value = null;
  inviteResult.value = null;
  inviteUses.value = 1;
}

// Revoke the invite just shown (drops it; a new one can be created after).
async function revokeInvite(f) {
  const inv = activeInviteFor(f);
  if (!inv) return;
  if (!confirm(`Revoke the ${f.name} invite ${inv.code}?`)) return;
  try {
    await api.del(`/api/invites/${inv.id}`);
    inviteResult.value = null;
    await loadInvites();
    toast("Invite revoked", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function createInvite(f) {
  if (creatingInvite.value) return;
  creatingInvite.value = true;
  try {
    const invite = await api.post("/api/invites", {
      max_uses: inviteUses.value,
      family_id: f.id,
      note: `for ${f.name}`,
    });
    inviteResult.value = {
      id: invite.id,
      familyId: f.id,
      code: invite.code,
      link: inviteLink(invite.code),
      uses: invite.max_uses,
      fresh: true,
    };
    toast("Invite created", "success");
    await loadInvites();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    creatingInvite.value = false;
  }
}

async function copyText(text, label) {
  await navigator.clipboard.writeText(text);
  toast(`${label} copied to clipboard`, "success");
}

onMounted(async () => {
  await families.load(true).catch((e) => toast(e.message, "error"));
  if (isAdmin.value) loadInvites();
});

async function create() {
  const name = newName.value.trim();
  if (!name) return;
  creating.value = true;
  try {
    await families.create({ name, description: newDesc.value.trim() || null });
    newName.value = "";
    newDesc.value = "";
    toast("Family created", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    creating.value = false;
  }
}

function startEdit(f) {
  editingId.value = f.id;
  editName.value = f.name;
  editDesc.value = f.description || "";
}

async function saveEdit() {
  const name = editName.value.trim();
  if (!name) return;
  saving.value = true;
  try {
    await families.update(editingId.value, { name, description: editDesc.value.trim() || null });
    editingId.value = null;
    toast("Family updated", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    saving.value = false;
  }
}

function cancelEdit() {
  editingId.value = null;
}

function onAvatarFile(f, e) {
  const file = e.target.files?.[0];
  e.target.value = "";
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    toast("Please choose an image file", "error");
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    toast("Image too large (max 10 MB)", "error");
    return;
  }
  uploadingFor.value = f.id;
  families
    .uploadAvatar(f.id, file)
    .then(() => toast("Family photo updated", "success"))
    .catch((err) => toast(err.message, "error"))
    .finally(() => {
      uploadingFor.value = null;
    });
}

async function remove(f) {
  if (!confirm(`Delete family "${f.name}"? This will also unassign any pending invites that point at it.`)) return;
  try {
    await families.remove(f.id);
    toast("Family deleted", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}
</script>

<template>
  <div class="families-layout">
    <Sidebar :mobile-open="mobileSidebar" @close="mobileSidebar = false" />

    <section class="families-main">
      <header class="page-header">
        <button class="mobile-toggle" @click="mobileSidebar = true">☰</button>
        <h1>Families</h1>
        <span class="count">{{ families.families.length }} famil{{ families.families.length === 1 ? "y" : "ies" }}</span>
      </header>

      <p class="page-hint">
        Group members into families. When you send an invite, choose which family the new member
        joins — they're added to it automatically.
      </p>

      <!-- Create (admin only) -->
      <div v-if="isAdmin" class="card create-card">
        <h2>New family</h2>
        <div class="create-row">
          <input v-model="newName" placeholder="e.g. Holsapples" maxlength="80" class="name-input" />
          <input v-model="newDesc" placeholder="Description (optional)" maxlength="280" class="desc-input" />
          <button class="btn" :disabled="creating || !newName.trim()" @click="create">
            {{ creating ? "Creating…" : "Create" }}
          </button>
        </div>
      </div>

      <!-- List -->
      <div v-if="families.families.length === 0" class="empty-state">
        <div class="empty-icon">🏠</div>
        <h2>No families yet</h2>
        <p>{{ isAdmin ? "Create your first family above, then invite people into it." : "An admin hasn't created any families yet." }}</p>
      </div>

      <div v-else class="family-list">
        <div v-for="f in families.families" :key="f.id" class="family-card card">
          <div v-if="isAdmin && editingId === f.id" class="edit-row">
            <input v-model="editName" maxlength="80" class="name-input" />
            <input v-model="editDesc" maxlength="280" class="desc-input" />
            <div class="edit-actions">
              <button class="btn btn-sm" :disabled="saving" @click="saveEdit">Save</button>
              <button class="btn btn-ghost btn-sm" @click="cancelEdit">Cancel</button>
            </div>
          </div>
          <div v-else class="family-body">
            <!-- Hidden file input; opened programmatically when the user clicks
                 their OWN family's avatar. Other families' avatars open the
                 zoom modal instead (see onAvatarClick). -->
            <input
              v-if="isAdmin || f.id === me?.family_id"
              :ref="(el) => setFileInput(f.id, el)"
              type="file"
              accept="image/*"
              style="display: none"
              @change="onAvatarFile(f, $event)"
            />
            <div
              class="family-avatar-wrap"
              :class="{
                clickable: uploadingFor !== f.id && (isAdmin || f.id === me?.family_id),
                zoomable: !(isAdmin || f.id === me?.family_id),
              }"
              role="button"
              :aria-label="
                isAdmin || f.id === me?.family_id
                  ? `Change ${f.name}'s photo`
                  : `View ${f.name}'s avatar`
              "
              @click="onAvatarClick(f)"
            >
              <div class="family-avatar">
                <img v-if="f.avatar_url" :src="f.avatar_url" :alt="f.name" />
                <span v-else class="avatar-placeholder">🏠</span>
                <span
                  v-if="
                    isAdmin || f.id === me?.family_id
                  "
                  class="avatar-hint"
                >
                  {{ uploadingFor === f.id ? "Uploading…" : f.avatar_url ? "Change" : "Add" }}
                </span>
              </div>
            </div>
            <div class="family-info">
              <div class="family-name">{{ f.name }}</div>
              <div v-if="f.description" class="family-desc">{{ f.description }}</div>
              <div class="family-meta">
                <span>{{ f.member_count }} member{{ f.member_count === 1 ? "" : "s" }}</span>
                <button
                  v-if="f.member_count > 0"
                  class="members-toggle"
                  :class="{ active: expandedMembersFor === f.id }"
                  @click="toggleMembers(f)"
                >
                  {{ expandedMembersFor === f.id ? "Hide members" : "Show members" }}
                </button>
              </div>

              <!-- Member roster: avatar + name always; phone/bio/email on expand. -->
              <ul v-if="f.member_count > 0" class="member-list" :class="{ open: expandedMembersFor === f.id }">
                <li v-for="u in f.members" :key="u.id" class="member-row">
                  <Avatar :user="u" size="sm" class="member-avatar" />
                  <div class="member-identity">
                    <div class="member-name">{{ u.display_name }}</div>
                    <ul v-if="expandedMembersFor === f.id" class="member-detail">
                      <li v-if="u.phone">
                        <span class="member-detail-label">📞</span>
                        <a :href="telHref(u.phone)">{{ u.phone }}</a>
                      </li>
                      <li v-if="u.bio"><span class="member-detail-label">👤</span><span>{{ u.bio }}</span></li>
                      <li v-if="u.email">
                        <span class="member-detail-label">✉️</span>
                        <a :href="`mailto:${u.email}`">{{ u.email }}</a>
                      </li>
                    </ul>
                  </div>
                </li>
              </ul>

              <!-- No users yet (e.g. an invite was sent but nobody's joined).
                   Admins see the number of open invite slots. -->
              <p v-else class="no-members">
                <template v-if="openInviteUses(f) > 0">
                  No users yet — up to {{ openInviteUses(f) }} family member{{ openInviteUses(f) === 1 ? "" : "s" }} can join this family.
                </template>
                <template v-else>No users yet — invite someone to fill this family.</template>
              </p>
            </div>
            <div class="family-actions">
              <button
                v-if="f.id !== me?.family_id"
                class="btn btn-sm"
                :disabled="chattingFor === f.id"
                @click="chatWithFamily(f)"
              >
                {{ chattingFor === f.id ? "Opening…" : "💬 Chat" }}
              </button>
              <button v-else class="btn btn-ghost btn-sm" disabled>Your family</button>
              <template v-if="isAdmin">
                <button class="btn btn-ghost btn-sm" @click="startEdit(f)">Edit</button>
                <button
                  class="btn btn-ghost btn-sm"
                  :class="{ active: invitingFor === f.id }"
                  :title="activeInviteFor(f) ? 'Has an active invite' : 'Create an invite'"
                  @click="startInvite(f)"
                >
                  ✉️ Invite
                  <span v-if="activeInviteFor(f)" class="invite-badge" title="Active invite">·</span>
                </button>
                <button class="btn btn-ghost btn-sm danger-text" @click="remove(f)">Delete</button>
              </template>
            </div>
          </div>

          <!-- Inline invite panel (admin only) -->
          <div v-if="isAdmin && invitingFor === f.id" class="invite-panel">
            <div class="invite-panel-head">
              <span class="invite-panel-title">Invite to {{ f.name }}</span>
              <button class="btn btn-ghost btn-sm" @click="cancelInvite">Close</button>
            </div>
            <div v-if="!inviteResult" class="invite-prompt">
              <p class="invite-prompt-note">
                No active invite for {{ f.name }} yet. Create one to share.
              </p>
              <label class="invite-uses-label">
                Times it can be used
                <input
                  v-model.number="inviteUses"
                  type="number"
                  min="1"
                  max="100"
                  class="invite-uses"
                />
              </label>
              <button class="btn btn-sm" :disabled="creatingInvite" @click="createInvite(f)">
                {{ creatingInvite ? "Creating…" : "Create invite" }}
              </button>
            </div>
            <div v-else class="invite-result">
              <p class="invite-result-note">
                {{ inviteResult.fresh ? "New invite created." : "Reusing the family's active invite." }}
              </p>
              <div class="invite-code-row">
                <code class="invite-code">{{ inviteResult.code }}</code>
                <button class="btn btn-ghost btn-sm" @click="copyText(inviteResult.code, 'Code')">Copy code</button>
              </div>
              <div class="invite-link-row">
                <span class="invite-link-label">Link:</span>
                <code class="invite-link">{{ inviteResult.link }}</code>
                <button class="btn btn-ghost btn-sm" @click="copyText(inviteResult.link, 'Link')">Copy link</button>
              </div>
              <div class="invite-result-foot">
                <p class="invite-uses-note">{{ inviteResult.uses }} use{{ inviteResult.uses === 1 ? "" : "s" }} · new members join {{ f.name }}</p>
                <button
                  class="btn btn-ghost btn-sm danger-text"
                  @click="revokeInvite(f)"
                >
                  Revoke
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- Zoom a family's avatar (for families that aren't yours). Mirrors the
         avatar-zoom modal in the chat's MessageBubble. -->
    <Modal v-model:open="avatarModal" :title="zoomFamily?.name || 'Family avatar'">
      <img
        v-if="zoomFamily?.avatar_url"
        :src="zoomFamily.avatar_url"
        class="family-avatar-zoom"
        :alt="zoomFamily?.name || 'family avatar'"
      />
      <div v-else class="family-avatar-zoom-placeholder">🏠</div>
    </Modal>
  </div>
</template>

<style scoped>
.families-layout { flex: 1; display: flex; overflow: hidden; }
.families-main { flex: 1; overflow-y: auto; padding: 20px; }
.page-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.page-header h1 { font-size: 22px; }
.count { color: var(--text-muted); font-size: 14px; margin-left: auto; }
.mobile-toggle { display: none; }
.page-hint { color: var(--text-muted); font-size: 13px; margin-bottom: 20px; max-width: 640px; line-height: 1.5; }
.create-card { max-width: 640px; margin-bottom: 20px; display: flex; flex-direction: column; gap: 14px; }
.create-card h2 { font-size: 16px; }
.create-row { display: flex; gap: 10px; align-items: center; }
.name-input { flex: 0 0 200px; }
.desc-input { flex: 1; }
.empty-state { text-align: center; color: var(--text-muted); padding: 60px 20px; }
.empty-icon { font-size: 56px; margin-bottom: 16px; }
.empty-state h2 { color: var(--text); margin-bottom: 8px; }
.family-list { display: flex; flex-direction: column; gap: 12px; max-width: 640px; }
.family-card { padding: 16px; }
.family-body { display: flex; align-items: center; gap: 16px; }
.family-avatar-wrap {
  position: relative;
  cursor: default;
  flex-shrink: 0;
  display: inline-block;
  border-radius: var(--radius);
  -webkit-tap-highlight-color: transparent;
}
.family-avatar-wrap.clickable { cursor: pointer; }
.family-avatar-wrap.zoomable { cursor: zoom-in; }
.family-avatar {
  width: 64px;
  height: 64px;
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-hover);
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  border: 1px solid var(--border);
  transition: border-color 0.15s;
}
.family-avatar-wrap.clickable:hover .family-avatar,
.family-avatar-wrap.zoomable:hover .family-avatar { border-color: var(--accent); }
.family-avatar img { width: 100%; height: 100%; object-fit: cover; }
.avatar-placeholder { font-size: 28px; }
.avatar-hint {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: opacity 0.15s;
}
.family-avatar-wrap:hover .avatar-hint { opacity: 1; }
.family-info { flex: 1; min-width: 0; }
.family-name { font-weight: 600; font-size: 16px; }
.family-desc { font-size: 13px; color: var(--text-muted); margin-top: 4px; white-space: pre-wrap; }
.family-meta { font-size: 12px; color: var(--text-muted); margin-top: 6px; display: flex; align-items: center; gap: 10px; }
.members-toggle {
  background: none;
  border: none;
  padding: 0;
  font-size: 12px;
  color: var(--accent);
  cursor: pointer;
  text-decoration: underline;
}
.members-toggle.active { color: var(--text-muted); text-decoration: none; }
.no-members { font-size: 13px; color: var(--text-muted); margin-top: 8px; font-style: italic; }
.member-list {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.member-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
}
.member-avatar { flex-shrink: 0; }
.member-identity { min-width: 0; flex: 1; }
.member-name { font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.member-detail { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.member-detail li { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-muted); }
.member-detail a { color: #fff; text-decoration: none; word-break: break-all; }
.member-detail a:hover { text-decoration: underline; color: var(--accent); }
.member-detail-label { flex-shrink: 0; }
.family-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.family-actions .btn.active { background: var(--accent-soft); color: var(--accent-hover); border-color: var(--accent); }
.edit-row { display: flex; flex-direction: column; gap: 10px; }
.edit-actions { display: flex; gap: 8px; }
.danger-text { color: var(--danger); }
.invite-panel {
  margin-top: 12px;
  padding: 14px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.invite-panel-head { display: flex; align-items: center; justify-content: space-between; }
.invite-panel-title { font-weight: 600; font-size: 14px; }
.invite-prompt { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.invite-prompt-note { width: 100%; margin: 0; font-size: 13px; color: var(--text-muted); }
.invite-result-note { margin: 0; font-size: 13px; color: var(--text-muted); }
.invite-result-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.invite-badge { color: var(--success); font-weight: 700; margin-left: 2px; }
.invite-uses-label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
}
.invite-uses { width: 90px; }
.invite-result {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: var(--accent-soft);
  border-radius: var(--radius-sm);
}
.invite-code-row,
.invite-link-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.invite-code {
  font-family: monospace;
  font-size: 18px;
  letter-spacing: 0.08em;
  background: var(--bg);
  padding: 6px 12px;
  border-radius: 6px;
}
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
.invite-uses-note { font-size: 12px; color: var(--text-muted); margin: 0; }

/* Avatar zoom modal (other families) — mirrors MessageBubble's avatar-zoom. */
.family-avatar-zoom {
  max-width: 100%;
  max-height: 100%;
  width: auto;
  height: auto;
  border-radius: var(--radius);
  display: block;
}
.family-avatar-zoom-placeholder {
  width: 220px;
  height: 220px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 96px;
  background: var(--bg-hover);
  border: 1px solid var(--border);
}

@media (max-width: 768px) {
  .mobile-toggle { display: inline-block; font-size: 18px; color: var(--text); }
  .create-row { flex-wrap: wrap; }
  .name-input, .desc-input { flex: 1 1 100%; }
  .family-body { flex-direction: column; align-items: flex-start; }
}
</style>
