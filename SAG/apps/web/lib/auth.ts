// Server-managed HttpOnly session cookie helpers (SAG-SEC-005 / SEC-BE-001).
// Tokens are never persisted in JS-readable document.cookie or localStorage;
// authenticated API requests are proxied server-side via /api/sag/[...path].
import { tokenCookieName } from "@/lib/auth-cookie";

function tokenKey(): string {
  return tokenCookieName(window.location.host);
}

export async function setToken(token: string, maxAgeSeconds = 60 * 60 * 24 * 7): Promise<void> {
  if (typeof window === "undefined") return;
  const response = await fetch("/api/auth/session", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, maxAgeSeconds }),
  });
  if (!response.ok) {
    throw new Error(`Failed to establish session (${response.status})`);
  }
}

export async function exchangeContinuumCode(code: string, state: string): Promise<void> {
  if (typeof window === "undefined") return;
  const response = await fetch("/api/auth/session", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, state }),
  });
  if (!response.ok) {
    throw new Error(`Continuum SSO code exchange failed (${response.status})`);
  }
}

export function getToken(): string | null {
  // HttpOnly session cookie is intentionally unreadable from client JavaScript.
  return null;
}

export function clearToken(): void {
  if (typeof window === "undefined") return;
  // Expire any legacy non-HttpOnly cookie left over from earlier builds.
  if (typeof document !== "undefined") {
    document.cookie = `${tokenKey()}=; path=/; max-age=0; SameSite=Lax`;
  }
  void fetch("/api/auth/session", {
    method: "DELETE",
    credentials: "same-origin",
  }).catch(() => {});
}
