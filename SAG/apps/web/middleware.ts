import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { tokenCookieName } from "@/lib/auth-cookie";

const PUBLIC_PATHS = ["/login", "/sso"];

type TokenClaims = {
  auth_mode?: unknown;
  org_id?: unknown;
  project_id?: unknown;
  security_partition?: unknown;
  exp?: unknown;
};

function tokenClaims(token: string | undefined): TokenClaims | null {
  const segment = token?.split(".")[1];
  if (!segment) return null;
  try {
    const encoded = segment.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(
      atob(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "=")),
    ) as TokenClaims;
  } catch {
    return null;
  }
}

export function middleware(req: NextRequest) {
  const token = req.cookies.get(tokenCookieName(req.headers.get("host") ?? ""))?.value;
  const claims = tokenClaims(token);
  const authenticated = process.env.NEXT_PUBLIC_CONTINUUM_SSO_URL
    ? claims?.auth_mode === "continuum" &&
      typeof claims.org_id === "string" &&
      typeof claims.project_id === "string" &&
      claims.security_partition === claims.project_id &&
      typeof claims.exp === "number" &&
      claims.exp > Date.now() / 1000
    : Boolean(token);
  const { pathname } = req.nextUrl;

  if (pathname === "/") {
    const url = req.nextUrl.clone();
    url.pathname = authenticated ? "/chat" : "/login";
    return NextResponse.redirect(url);
  }

  const isPublic = PUBLIC_PATHS.some((p) => pathname.startsWith(p));

  if (!authenticated && !isPublic) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  if (authenticated && isPublic && pathname !== "/sso") {
    const url = req.nextUrl.clone();
    url.pathname = "/chat";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.).*)"],
};
