<script setup>
import { ref } from "vue";
import { useAuthStore } from "@/stores/auth";
import { useChatStore } from "@/stores/chat";

const props = defineProps({
  reactions: { type: Array, default: () => [] },
  messageId: Number,
  channel: { type: String, default: "group" },
  roomId: { type: Number, default: null },
  isOwn: { type: Boolean, default: false },
});

// Channels where a reaction can be added/removed (group + family rooms).
function canReact() {
  return props.channel === "group" || props.channel === "room";
}

const auth = useAuthStore();
const chat = useChatStore();
const pickerOpen = ref(false);
const whoOpen = ref(null); // emoji whose "who reacted" popup is open

const EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🎉", "👀", "🥳", "😍", "🤔", "👎", "🔥", "✨", "💪", "🥰"];

function nameFor(userId) {
  const u = auth.users.find((x) => x.id === userId);
  return u?.display_name || `@${userId}`;
}

function whoReacted(emoji) {
  const r = props.reactions.find((x) => x.emoji === emoji);
  if (!r) return [];
  return r.user_ids.map((id) => nameFor(id));
}

function hasReacted(emoji) {
  const r = props.reactions.find((x) => x.emoji === emoji);
  return r ? r.user_ids.includes(auth.user?.id) : false;
}

function _apply(emoji) {
  if (props.channel === "room") {
    if (hasReacted(emoji)) chat.removeRoomReaction(props.roomId, props.messageId, emoji);
    else chat.addRoomReaction(props.roomId, props.messageId, emoji);
  } else if (props.channel === "group") {
    if (hasReacted(emoji)) chat.removeReaction(props.messageId, emoji);
    else chat.addReaction(props.messageId, emoji);
  }
}

function toggleWho(emoji, event) {
  // Never let a user react to their own message — on own messages the pill
  // just opens the "who reacted" popup. Otherwise (group/room, not own),
  // clicking toggles the reaction.
  if (props.isOwn || !canReact()) {
    whoOpen.value = whoOpen.value === emoji ? null : emoji;
    return;
  }
  whoOpen.value = null;
  _apply(emoji);
}

function pick(emoji) {
  pickerOpen.value = false;
  _apply(emoji);
}
</script>

<template>
  <div class="reactions" v-if="reactions.length || !isOwn" @click="whoOpen = null">
    <div class="reaction-row">
      <button
        v-for="r in reactions"
        :key="r.emoji"
        class="reaction-pill"
        :class="{ mine: r.user_ids.includes(auth.user?.id), open: whoOpen === r.emoji }"
        @click.stop="toggleWho(r.emoji, $event)"
        :title="whoOpen === r.emoji ? '' : r.user_ids.length + ' reaction(s)'"
      >
        {{ r.emoji }} {{ r.count }}
        <span v-if="whoOpen === r.emoji" class="who-popup">
          <span class="who-title">{{ r.emoji }} — {{ r.count }} {{ r.count === 1 ? "person" : "people" }}</span>
          <span v-for="n in whoReacted(r.emoji)" :key="n" class="who-name">{{ n }}</span>
        </span>
      </button>
      <button
        v-if="!isOwn && canReact()"
        class="reaction-add"
        @click.stop="whoOpen = null; pickerOpen = !pickerOpen"
        title="Add reaction"
      >
        😊+
      </button>
    </div>
    <div v-if="pickerOpen && canReact() && !isOwn" class="picker" @click.stop>
      <button v-for="e in EMOJIS" :key="e" class="picker-emoji" @click="pick(e)">{{ e }}</button>
    </div>
  </div>
</template>

<style scoped>
.reactions { margin-top: 4px; position: relative; }
.reaction-row { display: flex; flex-wrap: wrap; gap: 4px; }
/* The add-reaction button is absolutely positioned so it never consumes
   vertical space; MessageBubble reveals it on hover via :deep(). When the
   row has no pills (no reactions yet) the container collapses to zero height. */
.reactions:empty { margin-top: 0; }
.reaction-pill {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--bg-hover);
  font-size: 13px;
  border: 1px solid transparent;
  transition: border-color 0.15s;
  overflow: visible;
}
.reaction-pill:hover { border-color: var(--text-muted); }
.reaction-pill.mine { background: var(--accent-soft); border-color: var(--accent); }
.reaction-pill.open { border-color: var(--accent); z-index: 15; }
.who-popup {
  position: absolute;
  top: 100%;
  left: 0;
  margin-top: 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow);
  padding: 8px 10px;
  min-width: 140px;
  max-width: 240px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  z-index: 14;
}
.who-title { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 2px; }
.who-name { font-size: 13px; color: var(--text); }
.reaction-add {
  position: absolute;
  top: -6px;
  right: 0;
  font-size: 12px;
  padding: 1px 7px;
  border-radius: 10px;
  color: var(--text-muted);
  border: 1px dashed var(--border);
  background: var(--bg-elevated);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s;
}
.reaction-add:hover { border-color: var(--text-muted); }
.picker {
  position: absolute;
  bottom: 100%;
  left: 0;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px;
  display: grid;
  grid-template-columns: repeat(8, 1fr);
  gap: 4px;
  box-shadow: var(--shadow);
  z-index: 10;
}
.picker-emoji {
  font-size: 18px;
  padding: 4px;
  border-radius: 4px;
  transition: background 0.1s;
}
.picker-emoji:hover { background: var(--bg-hover); }
</style>
