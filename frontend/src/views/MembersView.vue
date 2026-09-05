<script setup>
import { ref, onMounted } from "vue";
import { storeToRefs } from "pinia";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";
import { useFamiliesStore } from "@/stores/families";
import { api } from "@/api";
import { toast } from "@/composables/useToasts";
import { dateOnly, dateAndTime } from "@/composables/useTime";
import Sidebar from "@/components/Sidebar.vue";
import Avatar from "@/components/Avatar.vue";

const auth = useAuthStore();
const chat = useChatStore();
const families = useFamiliesStore();
const { me, otherUsers } = storeToRefs(auth);
const mobileSidebar = ref(false);

const showInvite = ref(false);
const inviteCode = ref("");
const inviteMaxUses = ref(1);
const inviteNote = ref("");
const inviteFamilyId = ref(null);
const creatingInvite = ref(false);

const members = ref([]);
const deletingId = ref(null);

onMounted(async () => {
  await loadMembers();
  await families.load().catch(() => {});
});

async function loadMembers() {
  try {
    members.value = await api.get("/api/auth/admin/users");
  } catch {}
}

function timeStr(iso) {
  return dateOnly(iso);
}

function lastActiveStr(iso) {
  return dateAndTime(iso);
}

function familyBadge(u) {
  return u.family_name || null;
}

function familyAvatar(u) {
  const f = families.byId(u.family_id);
  return f?.avatar_url || null;
}

async function deleteMember(u) {
  if (deletingId.value) return;
  const msg = `Delete ${u.display_name}? This removes their messages, DMs, and files permanently.`;
  if (!confirm(msg)) return;
  deletingId.value = u.id;
  try {
    await api.del(`/api/auth/users/${u.id}`);
    members.value = members.value.filter((m) => m.id !== u.id);
    toast(`${u.display_name} deleted`, "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    deletingId.value = null;
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
    inviteCode.value = invite.code;
    inviteNote.value = "";
    toast("Invite created", "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    creatingInvite.value = false;
  }
}

async function copyInvite() {
  if (!inviteCode.value) return;
  await navigator.clipboard.writeText(inviteCode.value);
  toast("Copied to clipboard", "success");
}

function inviteLink() {
  return `${window.location.origin}/login?code=${inviteCode.value}`;
}

async function copyLink() {
  await navigator.clipboard.writeText(inviteLink());
  toast("Copied link", "success");
}
</script>

<template>
  <div class="members-layout">
    <Sidebar :mobile-open="mobileSidebar" @close="mobileSidebar = false" />

    <section class="members-main">
      <header class="page-header">
        <button class="mobile-toggle" @click="mobileSidebar = true">☰</button>
        <h1>Members</h1>
        <span class="count">{{ otherUsers.length + 1 }} member{{ otherUsers.length ? "s" : "" }}</span>
        <button class="btn invite-cta" @click="showInvite = !showInvite">
          {{ showInvite ? "Close" : "➕ Invite a member" }}
        </button>
      </header>

      <div v-if="showInvite" class="invite-card card">
        <h2>Invite someone to join</h2>
        <p class="hint">
          Share this invite code (or the link) with a family member or friend. They use it on
          the <strong>Join with invite</strong> tab to create their account.
        </p>

        <div class="invite-form">
          <label>
            Invite into
            <select v-model="inviteFamilyId" class="family-select">
              <option :value="null">No family</option>
              <option v-for="f in families.families" :key="f.id" :value="f.id">
                {{ f.name }}
              </option>
            </select>
          </label>
          <label>
            Max uses
            <input v-model.number="inviteMaxUses" type="number" min="1" max="100" class="invite-num" />
          </label>
          <label class="flex1">
            Note (optional)
            <input v-model="inviteNote" placeholder="e.g. for Grandma" maxlength="200" />
          </label>
          <button class="btn" :disabled="creatingInvite" @click="createInvite">
            {{ creatingInvite ? "Creating…" : "Create invite" }}
          </button>
        </div>

        <p v-if="families.families.length === 0" class="hint invite-families-hint">
          No families yet.
          <router-link to="/families">Create one</router-link> to group new members.
        </p>

        <div v-if="inviteCode" class="invite-result">
          <div class="invite-code-row">
            <code class="invite-code">{{ inviteCode }}</code>
            <button class="btn btn-ghost btn-sm" @click="copyInvite">Copy code</button>
          </div>
          <div class="invite-link-row">
            <span class="invite-link-label">Link:</span>
            <code class="invite-link">{{ inviteLink() }}</code>
            <button class="btn btn-ghost btn-sm" @click="copyLink">Copy link</button>
          </div>
        </div>
      </div>

      <div class="member-table-wrap card">
        <table class="member-table">
          <thead>
            <tr>
              <th>Member</th>
              <th>Family</th>
              <th class="num">Group msgs</th>
              <th class="num">Room msgs</th>
              <th>Joined</th>
              <th>Last active</th>
              <th class="actions-col"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in members" :key="u.id" :class="{ self: u.id === me?.id }">
              <td class="member-cell">
                <Avatar :user="u" size="sm" />
                <div class="member-cell-info">
                  <div class="member-name">
                    {{ u.display_name }}
                    <span v-if="u.id === me?.id" class="you-tag">You</span>
                  </div>
                  <div class="member-handle">@{{ u.handle }}</div>
                </div>
              </td>
              <td>
                <span v-if="familyBadge(u)" class="family-badge">
                  <img v-if="familyAvatar(u)" :src="familyAvatar(u)" class="badge-avatar" alt="" />
                  {{ familyBadge(u) }}
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td class="num">{{ u.group_message_count ?? 0 }}</td>
              <td class="num">{{ u.room_message_count ?? 0 }}</td>
              <td class="muted">{{ timeStr(u.created_at) }}</td>
              <td class="muted">{{ lastActiveStr(u.last_active_at) }}</td>
              <td class="actions-col">
                <div class="row-actions">
                  <button
                    class="btn btn-ghost btn-sm danger-text"
                    :disabled="u.id === me?.id || deletingId === u.id"
                    @click="deleteMember(u)"
                  >
                    {{ deletingId === u.id ? "Deleting…" : "Delete" }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="members.length === 0" class="invites-empty">No members yet.</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.members-layout {
  flex: 1;
  display: flex;
  overflow: hidden;
}
.members-main {
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
.count { color: var(--text-muted); font-size: 14px; margin-left: auto; }
.invite-cta { margin-left: 12px; }
.mobile-toggle { display: none; }
.invite-card {
  max-width: 640px;
  margin-bottom: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.invite-card h2 { font-size: 16px; }
.hint { color: var(--text-muted); font-size: 13px; line-height: 1.5; }
.invite-form {
  display: flex;
  align-items: flex-end;
  gap: 12px;
}
.invite-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 500;
}
.invite-form label.flex1 { flex: 1; }
.invite-num { width: 80px; }
.family-select { width: 100%; min-width: 140px; }
.invite-families-hint { margin-top: 4px; }
.invite-result {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: var(--accent-soft);
  border-radius: var(--radius-sm);
}
.invite-code-row,
.invite-link-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.invite-code {
  font-family: monospace;
  font-size: 18px;
  letter-spacing: 0.1em;
  background: var(--bg);
  padding: 6px 12px;
  border-radius: 6px;
}
.invite-link-row {
  align-items: center;
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
.member-table-wrap {
  padding: 0;
  overflow-x: auto;
  max-width: none;
}
.member-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
  min-width: 720px;
}
.member-table thead th {
  text-align: left;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.member-table th.num, .member-table td.num { text-align: right; }
.member-table th.actions-col { text-align: right; }
.member-table tbody td {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.member-table tbody tr:last-child td { border-bottom: none; }
.member-table tbody tr.self { background: var(--accent-soft); }
.member-table td.muted { color: var(--text-muted); white-space: nowrap; }
.member-cell { display: flex; align-items: center; gap: 10px; min-width: 180px; }
.member-cell-info { min-width: 0; }
.member-name { font-weight: 600; font-size: 14px; display: flex; align-items: center; }
.you-tag {
  background: var(--accent);
  color: #fff;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 6px;
  margin-left: 6px;
}
.member-handle { font-size: 12px; color: var(--text-muted); }
.family-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: var(--accent-soft);
  color: var(--accent-hover);
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 10px;
  white-space: nowrap;
}
.badge-avatar { width: 16px; height: 16px; border-radius: 4px; object-fit: cover; }
.row-actions { display: flex; gap: 6px; justify-content: flex-end; }

@media (max-width: 768px) {
  .mobile-toggle { display: inline-block; font-size: 18px; color: var(--text); }
}
</style>
