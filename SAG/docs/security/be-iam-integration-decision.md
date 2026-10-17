# BE–SAG IAM Integration Decision

> **Status:** The BE-backed SAG login, Project selection, and Project-scoped
> read path are implemented locally. The read-only document-status BFF path
> remains available. Confirmed SAG Source mappings are required before
> Project data can be shown.
>
> **Date:** 2026-10-17

## Decision

BE/Continuum remains the user identity and session authority. Users do not
enter separate SAG credentials in Continuum mode. From the SAG login page, the
browser starts an SSO redirect through the FE BFF. BE validates its existing
session and, when needed, sends the browser through the existing BE sign-in and
organization-selection flow before returning to SAG.

After validating the BE session and active Organization, BE lists the actor's
visible Projects and redirects to a Project chooser in SAG. The options in the
URL fragment are display data only. When the user selects one, the browser
returns its opaque ID to BE; BE calls `ProjectService.get` again before it
calls SAG's private `continuum-login` endpoint with the BE service token, user,
Organization, and exactly that Project. SAG maps the user to a technical local
`User` row and issues a SAG-only HS256 token scoped to that Organization and
Project, with a maximum 15-minute lifetime. The token is returned to the SAG
callback in a URL fragment and checked against the state held by that browser
tab. BE access tokens and `JWT_SECRET` are never sent to SAG. Continuum mode
disables SAG's local password/name login.

SAG uses this service-issued Project scope for interactive read and retrieval
routes. Source and document writes still require a separate BE assertion or
authorized service path; this flow adds no upload permission. Project selection
is not copied as a SAG membership or role, and each new selection is
revalidated by BE.

SAG may store the opaque BE `projectId` on its Sources/documents and use it as a
retrieval partition. That is data-scope metadata, not Project management. SAG
does not own BE users, Projects, memberships, roles, or permission decisions.
Every retrieval path must apply the validated Project scope before candidate
generation and top-k selection.

The current BE HS256 access-token and refresh-session design stays unchanged.
No custom per-user `X-SAG-Principal-Assertion`, new BE JWT claims, or shared
HMAC signing key is required for the BE-BFF path.

## Request flows

```text
SAG login:
  Browser opens SAG and chooses Continue with Continuum
  -> FE BFF calls BE's SAG login route with the browser's existing BE cookies
  -> BE validates the current session and active Organization or redirects through BE sign-in
  -> BE sends visible Project options to the state-checked SAG chooser
  -> the selected Project ID returns through the BE route
  -> BE validates it with ProjectService.get and calls SAG's private continuum-login endpoint
  -> SAG returns a Project-scoped SAG token to its state-checked callback
  -> SAG reads/retrieves only data tagged with that Project scope
```

Client-supplied Project IDs and chooser options are requests/display data, not
proof of access. BE rejects an unauthorized Project before asking SAG to issue
a scoped session. SAG's `continuum-login` endpoint must not be directly
reachable by an untrusted browser or public caller.

## Scope and impact

| Area | Required change | Impact |
|---|---|---|
| BE IAM | Reuse the existing authentication and Project authorization policies; do not change access-token issuance, refresh, or IAM persistence. | Low. |
| BE integration | Validate the BE session and Organization, list visible Projects, revalidate the selected Project, and issue scope through SAG's private login endpoint. | No IAM token/session format or persistence changes. |
| SAG service | Accept the private scoped login, create the technical SAG identity, and use the SAG token's trusted scope on read routes. | Writes still need a separate authorized path. |
| SAG web | Show Project options, return selection to BE, and store only the resulting short-lived SAG token. | No DATN-FE page change; the existing BE BFF forwards the query. |
| Existing SAG data | Associate each Source with a confirmed BE Project ID from an authoritative source. Keep unresolved Sources outside user search. | Requires data-owner reconciliation; never guess ownership. |

The current SAG web app sends its SAG token. Continuum read routes derive a
single-Project principal from that token, which only the private BE-to-SAG
login can mint in Continuum mode. Upload and other Source/Document mutation
routes continue to require their existing trusted assertion or service path.
The standalone/local path may remain for development or controlled
administration, but it is not the integrated user's production login.

### Implemented local slices

The login route contract is in BE's `docs/openapi/iam-v1.openapi.json`:
`GET /api/v1/ai-engine/auth/sag-login?state={uuidv4}[&projectId={id}]`. Without
`projectId`, it returns `302` to Continuum sign-in when no active BE
Organization session is available, then returns through
`/api/auth/sag/continue`; otherwise it redirects to SAG with visible Project
options in the fragment. With `projectId`, it validates the Project with
`ProjectService.get`, asks SAG to issue a Project-scoped session, and redirects
to the state-checked callback with the SAG token in the fragment. Both stages
are no-store. The state remains in the SAG browser tab across selection.

SAG stores a technical user record in its own database, but its credentials
are random and unusable in Continuum mode. This is an identity mapping needed
by the current SAG API dependencies; it is not a copied BE account or a
Project/permission mirror. SAG access tokens expire after at most 15 minutes.

The BE login flow itself can therefore be tried once both services are running
and `SAG_BFF_SERVICE_TOKEN` is configured to the same 32+ character secret in
BE and SAG. The current browser callback does not refresh an expired SAG token;
the user must restart SSO after it expires.

#### Read-only document-status slice

The BE exposes `GET /api/v1/ai-engine/projects/:projectId/documents/:documentId/versions/:versionNo/status`.
It validates the existing BE session, checks the user can read that Project
with the current `ProjectService`, then calls SAG with a service credential and
that single Project scope. SAG accepts this context only on the project-scoped
document status route; the BE access token and `JWT_SECRET` are never sent to
SAG. The Project ID is also used as this slice's SAG security partition.

The route is read-only and has this contract:

| Part | Contract |
|---|---|
| Request | `GET /api/v1/ai-engine/projects/{projectId}/documents/{documentId}/versions/{versionNo}/status`; no body. |
| Browser authentication | Existing BE session cookie (`continuum_access`) or the existing BE bearer form. The BE validates the token and ACTIVE Organization membership. |
| Project scope | `projectId` is a requested scope, not proof. BE requires `ProjectService.get` to resolve the Project for the authenticated actor before calling SAG; inaccessible Projects return 404. |
| BE → SAG | Private HTTP(S) API URL plus a distinct shared service token (32+ characters). BE sends actor, Organization, Project, and a security partition equal to the Project ID. No BE user credential is forwarded. |
| Response | SAG `DocumentVersionStatusResponse`: `document_id`, `version_no`, `status`, `search_status`, `knowledge_status`, readiness flags, current stage, stage progress, and error details. |
| Cache | `Cache-Control: no-store`. |
| Errors | 400 invalid version; 401 invalid BE session; 404 inaccessible Project or missing document version; 502 invalid/failed SAG response; 503 missing integration config or unavailable SAG. |

Call it through the existing FE BFF at `/api/backend/ai-engine/...` from the
BE frontend origin so the browser keeps using the BE host-only cookie. The
current SAG web app is not yet wired to this route.

BE IAM currently documents `project.read` and Project administration
permissions, but no document-upload permission. This change adds no upload
route, document-write permission, or document-processing capability. SAG's
interactive reads use the Project scope that BE checked before issuing the
SAG token; confirmed Source-to-Project mappings still decide which SAG data is
visible. Document mutations continue to require their existing trusted
assertion or service path.

## Relationship to existing SAG ACL drafts

The [Principal Assertion Contract](principal-assertion-contract.md) describes a
direct-to-SAG principal-assertion trust model. It is not the chosen trust model
for the BE-BFF flow in this decision. Keep it only for a future requirement
where a browser or another service must call SAG directly and BE cannot proxy
the request.

The [Project-to-Source Mapping Contract](project-to-source-mapping-contract.md)
may describe how SAG stores and enforces Source scope. It must not be read as
granting SAG ownership of BE Projects or user permissions. For integrated
requests, BE decides access; SAG consumes the resulting Project scope and
filters its own data.

## When to choose a different IAM design

If SAG must be an independently authenticated application on a separate
origin, use a shared OIDC identity provider and audience-specific tokens. A
shared identity provider solves login only. Project access must still come
from a Project Authorization API or a separate shared authorization service;
copying BE memberships into SAG creates a second, potentially stale authority.
Extracting Project authorization into a shared service is a larger migration
and is not required for the initial BE-integrated document flow.
