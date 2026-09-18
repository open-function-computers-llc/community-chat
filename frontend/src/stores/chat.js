import { defineStore } from "pinia";
import { api } from "@/api";
import { onWsEvent, useAuthStore } from "./auth";

export const useChatStore = defineStore("chat", {
  state: () => ({
    groupMessages: [],
    rooms: [],
    roomMessages: {}, // roomId -> [message]
    roomMeta: {}, // roomId -> { families: [...] }
    _wsUnsub: null,
  }),
  getters: {
    activeRooms: (s) => s.rooms.filter((r) => r.last_at),
  },
  actions: {
    initWs() {
      if (this._wsUnsub) return;
      this._wsUnsub = onWsEvent((event, msg) => this._handleWs(msg));
    },
    _handleWs(msg) {
      switch (msg.type) {
        case "message.new":
          if (msg.channel === "group") this._addGroupMessage(msg.message);
          break;
        case "message.edited":
          this._editGroupMessage(msg.message);
          break;
        case "message.deleted":
          this._deleteGroupMessage(msg.message_id);
          break;
        case "dm.new":
          this._addRoomMessage(msg);
          break;
        case "dm.edited":
          this._editRoomMessage(msg.message);
          break;
        case "dm.deleted":
          this._deleteRoomMessage(msg.room_id, msg.message_id);
          break;
        case "reaction.changed":
          this._updateReaction(msg);
          break;
        case "typing":
          break;
      }
    },
    async loadGroupMessages(beforeId = null, limit = 50) {
      const qs = new URLSearchParams({ limit: String(limit) });
      if (beforeId) qs.set("before_id", String(beforeId));
      const messages = await api.get(`/api/messages?${qs}`);
      if (beforeId) this.groupMessages = [...messages, ...this.groupMessages];
      else this.groupMessages = messages;
      return messages;
    },
    _addGroupMessage(message) {
      if (this.groupMessages.some((m) => m.id === message.id)) return;
      this.groupMessages.push(message);
    },
    _editGroupMessage(message) {
      const idx = this.groupMessages.findIndex((m) => m.id === message.id);
      if (idx >= 0) this.groupMessages.splice(idx, 1, message);
    },
    _deleteGroupMessage(id) {
      this.groupMessages = this.groupMessages.filter((m) => m.id !== id);
    },
    async sendGroupMessage({ text, file_url, file_name, file_content_type, replyToId }) {
      const message = await api.post("/api/messages", {
        text,
        file_url,
        file_name,
        file_content_type,
        reply_to_id: replyToId,
      });
      this._addGroupMessage(message);
      return message;
    },
    async editGroupMessage(id, text) {
      const message = await api.patch(`/api/messages/${id}`, { text });
      this._editGroupMessage(message);
      return message;
    },
    async deleteGroupMessage(id) {
      await api.del(`/api/messages/${id}`);
      this._deleteGroupMessage(id);
    },
    async addReaction(messageId, emoji) {
      const result = await api.post(`/api/messages/${messageId}/reactions/${encodeURIComponent(emoji)}`);
      this._applyReaction(this.groupMessages.find((m) => m.id === messageId), result);
      return result;
    },
    async removeReaction(messageId, emoji) {
      const result = await api.del(`/api/messages/${messageId}/reactions/${encodeURIComponent(emoji)}`);
      const msg = this.groupMessages.find((m) => m.id === messageId);
      if (msg) msg.reactions = msg.reactions.filter((r) => r.emoji !== emoji);
      return result;
    },
    _applyReaction(message, result) {
      if (!message) return;
      const existing = message.reactions.find((r) => r.emoji === result.emoji);
      if (existing) {
        existing.user_ids = result.user_ids;
        existing.count = result.count;
      } else {
        message.reactions.push(result);
      }
    },
    _updateReaction(msg) {
      let message = null;
      if (msg.channel === "room") {
        for (const key in this.roomMessages) {
          message = this.roomMessages[key].find((m) => m.id === msg.message_id);
          if (message) break;
        }
      } else {
        message = this.groupMessages.find((m) => m.id === msg.message_id);
      }
      if (!message) return;
      if (!message.reactions) message.reactions = [];
      if (msg.count === 0) {
        message.reactions = message.reactions.filter((r) => r.emoji !== msg.emoji);
        return;
      }
      const existing = message.reactions.find((r) => r.emoji === msg.emoji);
      if (existing) {
        existing.user_ids = msg.user_ids;
        existing.count = msg.count;
      } else {
        message.reactions.push({ emoji: msg.emoji, user_ids: msg.user_ids, count: msg.count });
      }
    },
    async loadRooms() {
      this.rooms = await api.get("/api/dms/rooms");
    },
    async openRoom(familyId) {
      const room = await api.post("/api/dms/rooms", { family_id: familyId });
      this.roomMeta[room.id] = { families: room.families };
      if (!this.rooms.some((r) => r.id === room.id)) {
        this.rooms.unshift({
          id: room.id,
          families: room.families,
          families_all: room.families,
          last_message: null,
          last_at: null,
        });
      }
      return room;
    },
    // Room messages arrive with a `sender` field; group messages use `author`.
    // The shared MessageBubble expects `author`, so normalize room messages at
    // every point they enter the store. This keeps the UI consistent (it always
    // reads message.author) instead of special-casing room vs. group.
    _normalizeRoomMessage(message) {
      if (message && !message.author) message.author = message.sender;
      return message;
    },
    async loadRoomHistory(roomId) {
      const data = await api.get(`/api/dms/rooms/${roomId}`);
      const messages = (data.messages || []).map((m) => this._normalizeRoomMessage(m));
      this.roomMessages[roomId] = messages;
      if (data.families) this.roomMeta[roomId] = { families: data.families };
      return messages;
    },
    _addRoomMessage(msg) {
      const message = this._normalizeRoomMessage(msg.message);
      const roomId = message.room_id;
      if (!this.roomMessages[roomId]) this.roomMessages[roomId] = [];
      if (this.roomMessages[roomId].some((m) => m.id === message.id)) return;
      this.roomMessages[roomId].push(message);
      const room = this.rooms.find((r) => r.id === roomId);
      if (room) {
        room.last_message = message;
        room.last_at = message.created_at;
      }
    },
    _editRoomMessage(message) {
      const roomId = message.room_id;
      const msgs = this.roomMessages[roomId];
      if (!msgs) return;
      const idx = msgs.findIndex((m) => m.id === message.id);
      if (idx >= 0) msgs.splice(idx, 1, this._normalizeRoomMessage(message));
    },
    _deleteRoomMessage(roomId, messageId) {
      const msgs = this.roomMessages[roomId];
      if (!msgs) return;
      this.roomMessages[roomId] = msgs.filter((m) => m.id !== messageId);
      const room = this.rooms.find((r) => r.id === roomId);
      // Refresh the sidebar preview if the last message was the one deleted.
      if (room && room.last_message?.id === messageId) {
        const remaining = this.roomMessages[roomId];
        room.last_message = remaining.length ? remaining[remaining.length - 1] : null;
        room.last_at = room.last_message?.created_at || null;
      }
    },
    async sendRoomMessage(roomId, { text, file_url, file_name, file_content_type }) {
      const message = this._normalizeRoomMessage(
        await api.post(`/api/dms/rooms/${roomId}`, {
          text,
          file_url,
          file_name,
          file_content_type,
        })
      );
      if (!this.roomMessages[roomId]) this.roomMessages[roomId] = [];
      if (!this.roomMessages[roomId].some((m) => m.id === message.id)) {
        this.roomMessages[roomId].push(message);
      }
      const room = this.rooms.find((r) => r.id === roomId);
      if (room) {
        room.last_message = message;
        room.last_at = message.created_at;
      }
      return message;
    },
    async editRoomMessage(roomId, messageId, text) {
      const message = this._normalizeRoomMessage(
        await api.patch(`/api/dms/rooms/${roomId}/messages/${messageId}`, { text })
      );
      const msgs = this.roomMessages[roomId] || [];
      const idx = msgs.findIndex((m) => m.id === messageId);
      if (idx >= 0) msgs.splice(idx, 1, message);
      return message;
    },
    async deleteRoomMessage(roomId, messageId) {
      await api.del(`/api/dms/rooms/${roomId}/messages/${messageId}`);
      const msgs = this.roomMessages[roomId] || [];
      this.roomMessages[roomId] = msgs.filter((m) => m.id !== messageId);
    },
    async addRoomReaction(roomId, messageId, emoji) {
      const result = await api.post(`/api/dms/rooms/${roomId}/messages/${messageId}/reactions/${encodeURIComponent(emoji)}`);
      const msgs = this.roomMessages[roomId] || [];
      const msg = msgs.find((m) => m.id === messageId);
      if (msg) this._applyReaction(msg, result);
      return result;
    },
    async removeRoomReaction(roomId, messageId, emoji) {
      const result = await api.del(`/api/dms/rooms/${roomId}/messages/${messageId}/reactions/${encodeURIComponent(emoji)}`);
      const msgs = this.roomMessages[roomId] || [];
      const msg = msgs.find((m) => m.id === messageId);
      if (msg) {
        msg.reactions = msg.reactions.filter((r) => r.emoji !== emoji);
      }
      return result;
    },
    get _meId() {
      return useAuthStore().user?.id;
    },
  },
});
