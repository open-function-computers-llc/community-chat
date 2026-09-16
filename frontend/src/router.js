import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "@/stores/auth";

const routes = [
  { path: "/login", name: "login", component: () => import("@/views/LoginView.vue") },
  {
    path: "/profile",
    name: "profile",
    component: () => import("@/views/ProfileView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/",
    name: "chat",
    component: () => import("@/views/ChatView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/members",
    name: "members",
    component: () => import("@/views/MembersView.vue"),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: "/families",
    name: "families",
    component: () => import("@/views/FamiliesView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/room/:roomId",
    name: "room",
    component: () => import("@/views/RoomView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/gallery",
    name: "gallery",
    component: () => import("@/views/GalleryView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/settings",
    name: "settings",
    component: () => import("@/views/SettingsView.vue"),
    meta: { requiresAuth: true },
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: "login" };
  }
  if (to.meta.requiresAdmin && !auth.user?.is_admin) {
    return { name: "chat" };
  }
  // A logged-in visitor to /login should be bounced off the login screen.
  // Where they land depends on whether their profile is set up (has a bio):
  // first-timers (blank bio) go to the onboarding profile page; everyone else
  // goes to the group chat. auth.user may not be loaded yet on a fresh page
  // load (it starts null until refreshUser() resolves), so load it first.
  if (to.name === "login" && auth.isAuthenticated) {
    if (!auth.user) {
      try {
        await auth.refreshUser();
      } catch {
        // If the refresh fails we still have a valid token; default to chat.
      }
    }
    return auth.user?.bio ? { name: "chat" } : { name: "profile" };
  }
});

export default router;
