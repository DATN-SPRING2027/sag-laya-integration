import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { tokenCookieName } from "@/lib/auth-cookie";

const MAX_JWT_LENGTH = 8192;
const DEFAULT_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;
const CONTINUUM_MAX_AGE_SECONDS = 15 * 60;

function isValidJwt(token: unknown): token is string {
  return (
    typeof token === "string" &&
    token.length > 0 &&
    token.length <= MAX_JWT_LENGTH &&
    token.split(".").length === 3
  );
}

function isSecureRequest(req: NextRequest): boolean {
  const proto = req.headers.get("x-forwarded-proto") ?? req.nextUrl.protocol;
  return proto.startsWith("https") || process.env.NODE_ENV === "production";
}

function resolveContinuumExchangeUrl(): string | null {
  const explicit = process.env.CONTINUUM_EXCHANGE_URL || process.env.NEXT_PUBLIC_CONTINUUM_EXCHANGE_URL;
  if (explicit && explicit.trim()) {
    return explicit.trim();
  }
  const ssoUrl = process.env.CONTINUUM_SSO_URL || process.env.NEXT_PUBLIC_CONTINUUM_SSO_URL;
  if (!ssoUrl || !ssoUrl.trim()) {
    return null;
  }
  return ssoUrl.trim().replace(/\/sag-login\/?$/, "/sag-exchange");
}

function isContinuumToken(token: string): boolean {
  try {
    const segment = token.split(".")[1];
    if (!segment) return false;
    const encoded = segment.replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(
      Buffer.from(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "="), "base64").toString("utf8"),
    ) as { auth_mode?: unknown };
    return claims.auth_mode === "continuum";
  } catch {
    return false;
  }
}

export async function GET(req: NextRequest) {
  const cookieName = tokenCookieName(req.headers.get("host") ?? "");
  const hasSession = Boolean(req.cookies.get(cookieName)?.value);
  return NextResponse.json(
    { authenticated: hasSession },
    { headers: { "Cache-Control": "no-store" } },
  );
}

function isSameOrigin(req: NextRequest): boolean {
  return req.headers.get("origin") === req.nextUrl.origin;
}

export async function POST(req: NextRequest) {
  if (!isSameOrigin(req)) {
    return NextResponse.json({ error: "Invalid request origin" }, { status: 403 });
  }
  if (req.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") {
    return NextResponse.json({ error: "Expected JSON" }, { status: 415 });
  }
  let body: { code?: unknown; state?: unknown; token?: unknown; maxAgeSeconds?: unknown };
  try {
    body = (await req.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "Invalid JSON payload" }, { status: 400 });
  }

  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400 });
  }

  let resolvedToken: string | null = null;
  let maxAge =
    typeof body.maxAgeSeconds === "number" && Number.isFinite(body.maxAgeSeconds) && body.maxAgeSeconds > 0
      ? Math.floor(body.maxAgeSeconds)
      : DEFAULT_MAX_AGE_SECONDS;

  if (typeof body.code === "string" && body.code.trim()) {
    const exchangeUrl = resolveContinuumExchangeUrl();
    if (!exchangeUrl) {
      return NextResponse.json({ error: "Continuum exchange endpoint is not configured" }, { status: 503 });
    }
    try {
      const upstream = await fetch(exchangeUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code: body.code.trim(),
          state: typeof body.state === "string" ? body.state : undefined,
        }),
        cache: "no-store",
      });
      if (!upstream.ok) {
        return NextResponse.json({ error: "SSO code exchange failed" }, { status: 401 });
      }
      const exchanged = (await upstream.json()) as { accessToken?: unknown; access_token?: unknown };
      const candidate = exchanged.accessToken ?? exchanged.access_token;
      if (!isValidJwt(candidate)) {
        return NextResponse.json({ error: "Invalid token returned by SSO exchange" }, { status: 502 });
      }
      resolvedToken = candidate;
      maxAge = Math.min(maxAge, CONTINUUM_MAX_AGE_SECONDS);
    } catch {
      return NextResponse.json({ error: "SSO exchange service unavailable" }, { status: 502 });
    }
  } else if (isValidJwt(body.token)) {
    resolvedToken = body.token;
    if (isContinuumToken(resolvedToken)) {
      maxAge = Math.min(maxAge, CONTINUUM_MAX_AGE_SECONDS);
    }
  } else {
    return NextResponse.json({ error: "Valid token or SSO code is required" }, { status: 400 });
  }

  const cookieName = tokenCookieName(req.headers.get("host") ?? "");
  const response = NextResponse.json(
    { ok: true },
    { headers: { "Cache-Control": "no-store" } },
  );
  response.cookies.set(cookieName, resolvedToken, {
    httpOnly: true,
    sameSite: "lax",
    secure: isSecureRequest(req),
    path: "/",
    maxAge,
  });
  return response;
}

export async function DELETE(req: NextRequest) {
  if (!isSameOrigin(req)) {
    return NextResponse.json({ error: "Invalid request origin" }, { status: 403 });
  }
  const cookieName = tokenCookieName(req.headers.get("host") ?? "");
  const response = NextResponse.json(
    { ok: true },
    { headers: { "Cache-Control": "no-store" } },
  );
  response.cookies.set(cookieName, "", {
    httpOnly: true,
    sameSite: "lax",
    secure: isSecureRequest(req),
    path: "/",
    maxAge: 0,
  });
  return response;
}
