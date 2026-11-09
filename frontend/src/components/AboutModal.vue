<script setup>
import Modal from "./Modal.vue";

// The app's "About" dialog. Also the home of the unDraw attribution for the
// brand illustration used in our emails (backend/assets, attached inline by
// app/routers/email.py).
defineProps({
  open: { type: Boolean, default: false },
});
defineEmits(["update:open"]);
</script>

<template>
  <Modal
    :open="open"
    title="About this app"
    :max-width="440"
    :max-height="520"
    @update:open="$emit('update:open', $event)"
  >
    <div class="about">
      <div class="about-logo">💬</div>
      <h3>Community Chat</h3>
      <p class="about-blurb">
        A small, private place for families and friends to catch up — group
        chat, family rooms, a shared gallery, and notifications the way you
        want them: instant or digest, by email or push. Self-hosted, so your
        conversations stay on your own server.
      </p>
      <p class="about-credit">
        The illustration in our emails is “Family” from
        <a href="https://undraw.co/" target="_blank" rel="noopener noreferrer">unDraw</a>,
        the open-source illustration project by Katerina Limpitsouni — free to
        use, and credited here with gratitude.
      </p>
    </div>
  </Modal>
</template>

<style scoped>
.about {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 10px;
  padding: 6px 4px;
}
.about-logo { font-size: 40px; line-height: 1; }
.about h3 { font-size: 17px; }
.about-blurb { color: var(--text); font-size: 13px; line-height: 1.55; margin: 0; }
.about-credit { color: var(--text-muted); font-size: 12px; line-height: 1.55; margin: 0; }
.about-credit a { color: var(--accent-hover); }
</style>
