import type { NextRequest } from "next/server";

import { tokenCookieName } from "@/lib/auth-cookie";

export const dynamic = "force-dynamic";

const SAFE_CONFLICT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const SAFE_PROJECT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

function resolveUpstreamBase(): string {
  const raw =
    process.env.SAG_API_INTERNAL_URL ||
    process.env.NEXT_PUBLIC_API_BASE ||
    "http://localhost:8000";
  return raw.replace(/\/+$/, "");
}

function resolveContinuumConflictUrl(
  projectId: string,
  resolutionId: string,
  action: "resolve" | "reopen",
): string | null {
  const apiBase = process.env.CONTINUUM_API_BASE_URL?.trim();
  if (apiBase) {
    return `${apiBase.replace(/\/+$/, "")}/api/v1/ai-engine/projects/${encodeURIComponent(projectId)}/conflicts/${encodeURIComponent(resolutionId)}/${action}`;
  }
  const ssoUrl = (
    process.env.CONTINUUM_SSO_URL ||
    process.env.NEXT_PUBLIC_CONTINUUM_SSO_URL ||
    ""
  ).trim();
  if (!ssoUrl) return null;
  const aiEngineBase = ssoUrl.replace(/\/auth\/sag-login\/?$/, "");
  if (aiEngineBase === ssoUrl) return null;
  return `${aiEngineBase}/projects/${encodeURIComponent(projectId)}/conflicts/${encodeURIComponent(resolutionId)}/${action}`;
}

function decodeSagJwtClaims(
  token: string | undefined,
): { projectId?: string; organizationId?: string } {
  if (!token) return {};
  try {
    const segment = token.split(".")[1];
    if (!segment) return {};
    const encoded = segment.replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(
      Buffer.from(
        encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "="),
        "base64",
      ).toString("utf8"),
    ) as {
      project_id?: unknown;
      allowed_project_ids?: unknown;
      organization_id?: unknown;
      tenant_id?: unknown;
    };
    const projectId =
      typeof claims.project_id === "string" && SAFE_PROJECT_ID_RE.test(claims.project_id)
        ? claims.project_id
        : Array.isArray(claims.allowed_project_ids) &&
            claims.allowed_project_ids.length === 1 &&
            typeof claims.allowed_project_ids[0] === "string" &&
            SAFE_PROJECT_ID_RE.test(claims.allowed_project_ids[0])
          ? claims.allowed_project_ids[0]
          : undefined;
    const organizationId =
      typeof claims.organization_id === "string" && claims.organization_id.trim()
        ? claims.organization_id.trim()
        : typeof claims.tenant_id === "string" && claims.tenant_id.trim()
          ? claims.tenant_id.trim()
          : undefined;
    return { projectId, organizationId };
  } catch {
    return {};
  }
}

function isSameOrigin(req: NextRequest): boolean {
  return req.headers.get("origin") === req.nextUrl.origin;
}

async function forwardConflictWriteViaContinuumBff(
  req: NextRequest,
  resolutionId: string,
  action: "resolve" | "reopen",
  cookieToken: string | undefined,
): Promise<Response> {
  if (!isSameOrigin(req)) {
    return new Response(JSON.stringify({ detail: "Invalid request origin" }), {
      status: 403,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  }
  if (!SAFE_CONFLICT_ID_RE.test(resolutionId)) {
    return new Response(JSON.stringify({ detail: "Invalid conflict resolution ID" }), {
      status: 400,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  }
  const { projectId, organizationId } = decodeSagJwtClaims(cookieToken);
  if (!projectId) {
    return new Response(
      JSON.stringify({ detail: "Active single-project Continuum session is required for conflict review" }),
      {
        status: 403,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
      },
    );
  }
  const bffUrl = resolveContinuumConflictUrl(projectId, resolutionId, action);
  if (!bffUrl) {
    return new Response(
      JSON.stringify({ detail: "Continuum BFF endpoint is not configured for conflict review" }),
      {
        status: 503,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
      },
    );
  }
  const cookieHeader = req.headers.get("cookie");
  if (!cookieHeader) {
    return new Response(
      JSON.stringify({ detail: "Continuum reviewer session is required" }),
      {
        status: 401,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
      },
    );
  }
  const headers = new Headers();
  headers.set("accept", "application/json");
  headers.set("content-type", req.headers.get("content-type") || "application/json");
  headers.set("cookie", cookieHeader);
  const csrfToken =
    req.cookies.get("__Host-csrf")?.value ||
    req.cookies.get("continuum_csrf")?.value ||
    req.cookies.get("csrf_token")?.value ||
    req.cookies.get("continuum_csrf_token")?.value ||
    req.headers.get("x-csrf-token");
  if (csrfToken) {
    headers.set("x-csrf-token", csrfToken);
  }
  if (organizationId) {
    headers.set("x-expected-organization-id", organizationId);
  }
  const requestId = req.headers.get("x-request-id");
  if (requestId) {
    headers.set("x-request-id", requestId);
  }

  const bodyText = await req.text();
  const upstream = await fetch(bffUrl, {
    method: "POST",
    headers,
    body: bodyText || "{}",
    redirect: "manual",
    cache: "no-store",
  });
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: {
      "Content-Type": upstream.headers.get("content-type") || "application/json",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

async function proxyToSag(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await context.params;
  if (!Array.isArray(path) || path.length === 0 || path.some((seg) => seg === ".." || seg.includes("\\"))) {
    return new Response(JSON.stringify({ detail: "Invalid proxy path" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const method = req.method.toUpperCase();
  const cookieToken = req.cookies.get(tokenCookieName(req.headers.get("host") ?? ""))?.value;

  // Route interactive Conflict Inbox writes through Continuum BE BFF for live IAM reviewer checks.
  if (
    method === "POST" &&
    path.length === 5 &&
    path[0] === "api" &&
    path[1] === "v1" &&
    path[2] === "conflicts" &&
    (path[4] === "resolve" || path[4] === "reopen")
  ) {
    return forwardConflictWriteViaContinuumBff(req, path[3], path[4], cookieToken);
  }

  const targetUrl = `${resolveUpstreamBase()}/${path.join("/")}${req.nextUrl.search}`;
  const headers = new Headers();
  const forwardHeaderNames = [
    "accept",
    "accept-language",
    "content-type",
    "x-request-id",
    "last-event-id",
  ];
  for (const name of forwardHeaderNames) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }

  const incomingAuth = req.headers.get("authorization");
  if (incomingAuth && incomingAuth.trim() && incomingAuth.trim().toLowerCase() !== "bearer") {
    headers.set("authorization", incomingAuth);
  } else if (cookieToken) {
    headers.set("authorization", `Bearer ${cookieToken}`);
  }

  const hasBody = method !== "GET" && method !== "HEAD";
  const init: RequestInit = {
    method,
    headers,
    redirect: "manual",
    cache: "no-store",
  };
  if (hasBody && req.body) {
    init.body = req.body;
    (init as Record<string, unknown>).duplex = "half";
  }

  const upstream = await fetch(targetUrl, init);
  const responseHeaders = new Headers();
  const passthroughHeaders = [
    "content-type",
    "content-disposition",
    "cache-control",
    "x-request-id",
    "x-accel-buffering",
  ];
  for (const name of passthroughHeaders) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  responseHeaders.set("x-content-type-options", "nosniff");
  const contentType = responseHeaders.get("content-type")?.split(";")[0].trim().toLowerCase();
  const activeContent = contentType && new Set([
    "text/html", "application/xhtml+xml", "image/svg+xml", "application/xml", "text/xml",
  ]).has(contentType);
  if (upstream.ok && (path.at(-1) === "file" || activeContent)) {
    responseHeaders.set("content-type", "application/octet-stream");
    const disposition = responseHeaders.get("content-disposition");
    responseHeaders.set(
      "content-disposition",
      disposition ? disposition.replace(/^[^;]+/, "attachment") : "attachment",
    );
    responseHeaders.set("content-security-policy", "sandbox; default-src 'none'");
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export const GET = proxyToSag;
export const POST = proxyToSag;
export const PUT = proxyToSag;
export const PATCH = proxyToSag;
export const DELETE = proxyToSag;
