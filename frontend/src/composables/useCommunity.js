import { ref } from "vue";
import { api } from "@/api";

// The community this deployment is branded for (backend COMMUNITY_NAME env
// var, served by GET /api/config). Fetched once at startup; every brand
// surface (nav, login, welcome, footer, document title) reads this — so a
// rebrand is an env change + restart, never a frontend rebuild.
const DEFAULT_NAME = "Community Chat";
const name = ref(DEFAULT_NAME);
let loaded = false;

export function useCommunity() {
  return { name };
}

export async function loadCommunity() {
  if (loaded) return name.value;
  try {
    const cfg = await api.get("/api/config");
    if (cfg && cfg.community_name) name.value = cfg.community_name;
  } catch {
    // Offline/config failure: keep the product name.
  }
  loaded = true;
  // Mirror the server-injected <title> (serves the same string before the
  // SPA loads; this keeps it correct across client-side navigation).
  document.title = `${name.value} | Powered by ${DEFAULT_NAME}`;
  return name.value;
}

export const POWERED_BY = DEFAULT_NAME;
