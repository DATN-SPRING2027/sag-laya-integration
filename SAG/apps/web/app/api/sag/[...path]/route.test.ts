import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { tokenCookieName } from "@/lib/auth-cookie";
import { GET, POST } from "./route";

function makeJwt(payload: Record<string, unknown>): string {
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return `${header}.${body}.sig`;
}

describe("SAG Web API proxy route (XSS isolation & Conflict BFF forwarding)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete process.env.CONTINUUM_API_BASE_URL;
  });

  it("forces active HTML original downloads to safe attachment and strips browser-supplied privileged headers", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response("<script>alert(1)</script>", {
        status: 200,
        headers: {
          "content-type": "text/html; charset=utf-8",
          "content-disposition": 'inline; filename="payload.html"',
        },
      }),
    );

    const cookieName = tokenCookieName("localhost:3002");
    const req = new NextRequest("http://localhost:3002/api/sag/api/v1/sources/s1/documents/d1/file", {
      method: "GET",
      headers: {
        host: "localhost:3002",
        cookie: `${cookieName}=${makeJwt({ project_id: "proj-1" })}`,
        "x-sag-bff-service-token": "spoofed-secret",
        "x-continuum-role": "ADMIN",
        "x-continuum-capabilities": "conflict.resolve",
      },
    });

    const res = await GET(req, {
      params: Promise.resolve({ path: ["api", "v1", "sources", "s1", "documents", "d1", "file"] }),
    });

    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("application/octet-stream");
    expect(res.headers.get("content-disposition")).toContain("attachment");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    expect(res.headers.get("content-security-policy")).toBe("sandbox; default-src 'none'");

    const forwardedHeaders = fetchMock.mock.calls[0]?.[1]?.headers as Headers;
    expect(forwardedHeaders.get("x-sag-bff-service-token")).toBeNull();
    expect(forwardedHeaders.get("x-continuum-role")).toBeNull();
    expect(forwardedHeaders.get("x-continuum-capabilities")).toBeNull();
  });

  it("routes same-origin Conflict resolve/reopen writes through Continuum BE BFF with Continuum session & CSRF", async () => {
    process.env.CONTINUUM_API_BASE_URL = "http://localhost:3001";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ id: "res-1", status: "ACTIVE" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const cookieName = tokenCookieName("localhost:3002");
    const sagJwt = makeJwt({
      project_id: "507f1f77bcf86cd799439022",
      organization_id: "507f1f77bcf86cd799439011",
    });
    const req = new NextRequest("http://localhost:3002/api/sag/api/v1/conflicts/res-1/resolve", {
      method: "POST",
      headers: {
        host: "localhost:3002",
        origin: "http://localhost:3002",
        "content-type": "application/json",
        cookie: `${cookieName}=${sagJwt}; continuum_access_token=be-session; continuum_csrf=csrf-xyz`,
      },
      body: JSON.stringify({ action: "WINNER_B", reason: "Verified v2" }),
    });

    const res = await POST(req, {
      params: Promise.resolve({ path: ["api", "v1", "conflicts", "res-1", "resolve"] }),
    });

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://localhost:3001/api/v1/ai-engine/projects/507f1f77bcf86cd799439022/conflicts/res-1/resolve",
    );
    const forwardedHeaders = fetchMock.mock.calls[0]?.[1]?.headers as Headers;
    expect(forwardedHeaders.get("x-csrf-token")).toBe("csrf-xyz");
    expect(forwardedHeaders.get("x-expected-organization-id")).toBe("507f1f77bcf86cd799439011");
    expect(forwardedHeaders.get("x-sag-bff-service-token")).toBeNull();
  });

  it("blocks cross-origin Conflict resolve requests with 403", async () => {
    process.env.CONTINUUM_API_BASE_URL = "http://localhost:3001";
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const req = new NextRequest("http://localhost:3002/api/sag/api/v1/conflicts/res-1/resolve", {
      method: "POST",
      headers: {
        host: "localhost:3002",
        origin: "https://evil.example",
        "content-type": "application/json",
      },
      body: JSON.stringify({ action: "WINNER_B" }),
    });

    const res = await POST(req, {
      params: Promise.resolve({ path: ["api", "v1", "conflicts", "res-1", "resolve"] }),
    });

    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
