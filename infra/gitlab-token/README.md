# GitLab credential — operator runbook

The worker reaches GitLab with **one** credential, `GITLAB_PRIVATE_TOKEN`. It is
used both as the `PRIVATE-TOKEN:` REST header and as the git credential
(`https://oauth2:<token>@…`).

**Granting the agent access to a project is an operations action, not a code
change.** Nothing in this repository decides which projects the agent may touch;
GitLab membership does. This file is how you perform that grant.

Companion runbook: [`../oidc-webhook/README.md`](../oidc-webhook/README.md)
(who is allowed to *trigger* a run). Design contract: `docs/auth.md`.

---

## 1. Which kind of token

Three GitLab token kinds work here, **interchangeably** — same header, same git
credential, no code branches on the kind. They differ only in blast radius:

| Kind | Covers | Use it for |
|---|---|---|
| **Project access token** | one project | single-project harnesses (local-gitlab, devstack, k8s, k3s) |
| **Group access token** | every project in the group, including nested subgroups | ver99 (`swebench/*` — all instances live under one group) |
| Personal access token (PAT) | **every project its owner can reach**, and it silently grows as they join more | local development only |

Prefer a bot-backed token over a PAT for three reasons, in descending order of
how often they actually bite:

1. **It survives people.** A PAT dies with its owner's account — someone leaves,
   and every harness pointed at that GitLab fails at 3am with a 401 nobody can
   attribute.
2. **Blast radius.** `settings/*.env` is baked into every image by
   `COPY settings/`; a leaked PAT is every project its owner can see.
3. **Audit.** Commits and MRs carry the bot identity, which is currently the
   only signal separating human changes from agent changes — `RunRecord` has no
   actor field yet (`docs/auth.md` §5).

## 2. Creating the token

Self-hosted GitLab, as a Maintainer/Owner of the project or group:

- **Project token** — Project → Settings → Access Tokens
- **Group token** — Group → Settings → Access Tokens

Set:

| Field | Value | Why |
|---|---|---|
| Role | **Developer** | pushes `auto/*` and opens MRs. Maintainer is over-granting: the worker **never merges** — it only reads MR state for the R10 short-circuit |
| Scopes | `api`, `write_repository` | `api` is needed for the CI job trace and for creating MRs; `read_api` cannot open an MR |
| Expiry | as short as your rotation cadence allows | access tokens are *forced* to expire — see §5 |

> gitlab.com's Free tier cannot create project/group access tokens (paid
> subscription required). Self-hosted has no such restriction. If the
> Access Tokens page is missing on gitlab.com, that is why.

## 3. Granting access (the authorization step)

- **Group token** — already covers every project in the group, including nested
  subgroups. Nothing further to do.
- **A project outside that group** — add the bot user (`group_<id>_bot_*` /
  `project_<id>_bot_*`) as a project **member** with role **Developer**.

Revoking is the same path in reverse, and it is the project owner's to make.
That is the point: whether the agent may act on a project is decided by that
project's owner, not by whoever sends the webhook.

### Branch protection

- Protect `main` and do **not** grant the bot push access. The agent submits an
  MR; a human merges it. The worker holds no merge permission and no code path
  that would use one.
- Do **not** protect `auto/*` — that is the namespace the agent pushes to.

One consequence surprises people during a first smoke test: GitLab ties
"may run a pipeline on this branch" to the branch's **push/merge** allowlist, so
once `main` is protected against Developers the bot can no longer trigger a
pipeline there —

```
POST /projects/:id/pipeline?ref=main
→ 400 "You do not have sufficient permission to run a pipeline on 'main'"
```

That is the role ceiling working, not a misconfiguration. It costs the agent
nothing: in a real run the `main` pipeline is started by a **human** push or
merge, and the agent only reacts to its failure. The agent's own pipeline runs
on the unprotected `auto/bf/*` branch it just pushed. When rehearsing the flow
by hand, trigger the `main` pipeline with an operator credential — using the
bot's token there is conflating two roles that this design deliberately keeps
apart.

## 4. Where the token lives

| Deployment | Location |
|---|---|
| Subprocess harnesses (local-gitlab, devstack, aws-gitlab, remote-eval) | `settings/worker_<ENV>.env` — `chmod 600`, owned by the service account |
| docker-compose / ECS | image-baked `settings/*.env`, or an injected env var |
| k8s / k3s | the `sdlcma-secrets` Secret, injected via `envFrom` |

**Precedence:** an environment variable beats the env file, so a Secret
overrides the image-baked value. In production, keep a placeholder in the env
file and inject the real token.

```bash
kubectl create secret generic sdlcma-secrets -n sdlcma \
  --from-literal=GITLAB_PRIVATE_TOKEN=glpat-... \
  --from-literal=LLM_API_KEY=...
```

> ⚠️ This worked in neither direction before `gitlab_private_token` became a
> *declared* settings field: pydantic-settings reverses env-var-vs-env-file
> priority for undeclared `extra` fields, so the image-baked value silently won
> and the Secret did nothing. Pinned by `tests/test_gitlab_token_check.py`.

> ⚠️ An environment variable that exists but is **empty** also wins — it does
> not fall back to the file. A Secret missing that key, or a stray
> `export GITLAB_PRIVATE_TOKEN=`, yields an empty token. The startup preflight
> (§6) catches it with a message that says so.

Not yet solved (see `docs/auth.md` §5): `settings/*.env` is still copied into
images, and `.dockerignore` deliberately does not exclude `*.env`. Harmless for
self-hosted local images; it matters on the ECR path.

## 5. Rotation

Access tokens **must** expire (365 days maximum). Rotation is therefore
scheduled work, not an incident:

1. The worker warns on every run once the token is within **14 days** of expiry:
   `gitlab_token_check: GitLab token expires in N day(s)`.
2. Rotate via the UI, or:
   ```bash
   curl -sX POST -H "PRIVATE-TOKEN: $ADMIN_TOKEN" \
     "$GITLAB_API/projects/<id>/access_tokens/<token_id>/rotate"
   ```
   (`/groups/<id>/access_tokens/<id>/rotate` for a group token.) The response
   carries the new token — it is shown once.
3. Update the Secret / env file, then restart the **orchestrator**: the spawner
   hands each worker `os.environ.copy()`, so workers pick up the new value only
   after the orchestrator restarts.

## 6. Startup preflight

Every GitLab-mode worker runs `services/gitlab_token_check.py` before doing any
work. It aborts on four unambiguous, operator-fixable conditions:

| Condition | Message names |
|---|---|
| token empty | which env file was consulted, and the empty-env-var trap |
| HTTP 401 | expired / revoked / wrong |
| cannot reach the target project (403/404) | the project id, the token kind, and this file |
| role below Developer | the actual role and the required one |

Everything it cannot decide — a network failure, a GitLab that omits
`permissions`, an older instance without `/personal_access_tokens/self` —
logs a warning and lets the run proceed. A preflight must not become a new way
for runs to die.

Aborting is safe with respect to restarts: the worker still writes
`worker:completed:{bug_id}`, so HealthMonitor does not restart a
deterministically-misconfigured worker three times over.

Bypass for a one-off: `GITLAB_SKIP_TOKEN_CHECK=1`.

Each run logs which credential it used — the only such record today:

```
phase_marker phase=gitlab_token_check bug_id=… kind=group user=group_7_bot_ab \
  project=42 role=Developer scopes=api,write_repository expires_in_days=87
```

## 7. What this token must NOT be used for

These need Maintainer/Owner or instance-level rights, are **one-off operator
actions**, and should run with a human's own PAT passed through the shell —
never written into an env file or an image:

| Action | Where |
|---|---|
| Create groups / projects | `infra/swebench-gitlab/setup_instance.py` |
| Delete protected branches | same |
| Install the project webhook | `infra/*/setup.sh` |
| Fixture repos, pipeline triggers, smoke/regression scripts | `tools/gitlab_fixture_repos.py`, `tools/trigger_concurrent_pipelines.py`, `infra/*/regression.sh` |

A project access token cannot create projects at all — it is attached to one
that already exists.
