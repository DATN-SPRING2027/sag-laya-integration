"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { setToken } from "@/lib/auth";

type ProjectOption = { id: string; name: string };

export default function ContinuumSsoPage() {
  const router = useRouter();
  const t = useTranslations("Login");
  const [projects, setProjects] = useState<ProjectOption[] | null>(null);
  const [selectingProjectId, setSelectingProjectId] = useState<string | null>(null);
  const [selectionFailed, setSelectionFailed] = useState(false);

  useEffect(() => {
    const state = new URLSearchParams(window.location.search).get("state");
    const result = new URLSearchParams(window.location.hash.slice(1));
    const token = result.get("token");
    const expectedState = window.sessionStorage.getItem("sag-continuum-sso-state");
    window.history.replaceState(null, "", "/sso");

    if (!state || state !== expectedState) {
      window.sessionStorage.removeItem("sag-continuum-sso-state");
      router.replace("/login?sso=failed");
      return;
    }

    if (token) {
      if (token.length > 8192 || token.split(".").length !== 3) {
        window.sessionStorage.removeItem("sag-continuum-sso-state");
        router.replace("/login?sso=failed");
        return;
      }
      window.sessionStorage.removeItem("sag-continuum-sso-state");
      setToken(token, 15 * 60);
      router.replace("/chat");
      return;
    }

    const encodedProjects = result.get("projects");
    try {
      const value: unknown = encodedProjects ? JSON.parse(encodedProjects) : null;
      if (
        !Array.isArray(value) ||
        value.some(
          (project) =>
            !project ||
            typeof project.id !== "string" ||
            !project.id ||
            typeof project.name !== "string",
        )
      ) {
        throw new Error("Invalid Project options");
      }
      setProjects(value as ProjectOption[]);
    } catch {
      window.sessionStorage.removeItem("sag-continuum-sso-state");
      router.replace("/login?sso=failed");
    }
  }, [router]);

  function selectProject(projectId: string) {
    const state = window.sessionStorage.getItem("sag-continuum-sso-state");
    const configuredUrl = process.env.NEXT_PUBLIC_CONTINUUM_SSO_URL;
    if (!state || !configuredUrl) {
      setSelectionFailed(true);
      return;
    }
    try {
      const url = new URL(configuredUrl);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        throw new Error("Invalid sign-in URL");
      }
      url.searchParams.set("state", state);
      url.searchParams.set("projectId", projectId);
      setSelectingProjectId(projectId);
      window.location.assign(url.toString());
    } catch {
      setSelectionFailed(true);
      setSelectingProjectId(null);
    }
  }

  if (projects !== null) {
    return (
      <main className="grid min-h-[100svh] place-items-center px-5 py-8">
        <section className="w-full max-w-lg rounded-lg border bg-background/90 p-6 shadow-lift">
          <h1 className="font-display text-2xl font-semibold">{t("selectProjectTitle")}</h1>
          <p className="mt-2 text-sm text-muted-foreground">{t("selectProjectDescription")}</p>
          {selectionFailed ? (
            <p role="alert" className="mt-4 text-sm text-destructive">
              {t("projectSelectionFailed")}
            </p>
          ) : null}
          {projects.length === 0 ? (
            <p className="mt-6 rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              {t("noAccessibleProjects")}
            </p>
          ) : (
            <div className="mt-5 grid gap-2">
              {projects.map((project) => (
                <Button
                  key={project.id}
                  type="button"
                  variant="outline"
                  className="h-auto justify-start whitespace-normal px-4 py-3 text-left"
                  disabled={selectingProjectId !== null}
                  onClick={() => selectProject(project.id)}
                >
                  {selectingProjectId === project.id ? <Spinner /> : null}
                  <span className="truncate">{project.name}</span>
                </Button>
              ))}
            </div>
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="grid min-h-[100svh] place-items-center">
      <Spinner />
    </main>
  );
}
