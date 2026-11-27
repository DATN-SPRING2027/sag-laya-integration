import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { DELETE, GET, POST } from "./route";
import { getToken } from "@/lib/auth";

const SAMPLE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJhdXRoX21vZGUiOiJjb250aW51dW0ifQ.c2lnbmF0dXJl";

describe("SAG Web HttpOnly session route (SAG-SEC-005 / SEC-BE-001)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete process.env.CONTINUUM_EXCHANGE_URL;
  });

  it("exchanges Continuum one-time code server-side and sets HttpOnly SameSite=Lax cookie", async () => {
    process.env.CONTINUUM_EXCHANGE_URL = "http://localhost:3001/api/v1/ai-engine/auth/sag-exchange";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ accessToken: SAMPLE_JWT, projectId: "proj-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const req = new NextRequest("https://sag.local/api/auth/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", host: "sag.local" },
      body: JSON.stringify({ code: "one-time-code-123", state: "state-abc" }),
    });
    const res = await POST(req);
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:3001/api/v1/ai-engine/auth/sag-exchange",
      expect.objectContaining({ method: "POST" }),
    );
    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie.toLowerCase()).toContain("samesite=lax");
    expect(setCookie).toContain("Secure");
    expect(setCookie).toContain("Max-Age=900");
  });

  it("clears the HttpOnly cookie on DELETE and never exposes token via getToken()", async () => {
    const req = new NextRequest("http://localhost:3002/api/auth/session", {
      method: "DELETE",
      headers: { host: "localhost:3002" },
    });
    const res = await DELETE(req);
    expect(res.status).toBe(200);
    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("Max-Age=0");

    const getRes = await GET(new NextRequest("http://localhost:3002/api/auth/session"));
    expect(await getRes.json()).toEqual({ authenticated: false });
    expect(getToken()).toBeNull();
  });
});
