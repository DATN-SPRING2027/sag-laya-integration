import type { NextRequest } from "next/server";

import { tokenCookieName } from "@/lib/auth-cookie";

export const dynamic = "force-dynamic";

function resolveUpstreamBase(): string {
  const raw =
    process.env.SAG_API_INTERNAL_URL ||
    process.env.NEXT_PUBLIC_API_BASE ||
    "http://localhost:8000";
  return raw.replace(/\/+$/, "");
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
  const cookieToken = req.cookies.get(tokenCookieName(req.headers.get("host") ?? ""))?.value;
  if (incomingAuth && incomingAuth.trim() && incomingAuth.trim().toLowerCase() !== "bearer") {
    headers.set("authorization", incomingAuth);
  } else if (cookieToken) {
    headers.set("authorization", `Bearer ${cookieToken}`);
  }

  const method = req.method.toUpperCase();
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
