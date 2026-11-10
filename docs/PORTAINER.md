# Deploying with Portainer

Portainer runs the dashboard as a **stack** — a Docker Compose file it
deploys and keeps up to date for you.

```
  ┌──────────────────┐    HTTP     ┌───────────────────────┐    WebSocket
  │  the wall panel  │ ──────────► │  the stack            │ ──────────► ┌───────────────┐
  │  a browser       │ ◄────────── │  on the target host   │ ◄────────── │ Home Assistant│
  └──────────────────┘             └───────────────────────┘             └───────────────┘
                                      │  reads and writes
                                      ▼
                            the offgrid-config volume
                          (the settings, and the token in them)
```

There are three pieces, and it is worth knowing which is which:

| Piece | Where it lives | Who changes it |
|---|---|---|
| **The image** | GitHub Container Registry | The GitHub workflow, on every push |
| **The stack** | `docker-compose.portainer.yml` in the repository | You, in a commit |
| **The settings** | The `offgrid-config` Docker volume | You, on the settings page |

## The thing that catches people out

**Portainer redeploys a stack without rebuilding it.** Under the hood it
runs `docker-compose up -d` — with no `--build`. So a redeploy updates the
*compose file* but keeps the *image it already has*, and a new commit
would not reach the running container.

That is why the image comes from the registry rather than from the
repository:

```
   push to main
        │
        ▼
   the GitHub workflow builds the image ──► ghcr.io/…/offgrid-dashboard:latest
        │
        ▼
   Portainer notices the repository has changed, re-pulls the image
        │
        ▼
   the new version is running
```

The workflow is [`.github/workflows/docker.yml`](../.github/workflows/docker.yml)
and it is already part of the repository. It signs in with the workflow's
own token, so there is no secret to set up.

### The order matters

Portainer notices that the *repository* has changed, and redeploys the
stack — re-pulling the image as it does. If it did that while the image
was still being built, it would find the old image, deploy it, and then
have no reason to look again until the next commit. The new code would
sit there unpublished until someone happened to commit again.

So the workflow makes one small commit **after** the image has been
pushed:

```
   push to main
        │
        ▼
   the workflow builds the image and pushes it to the registry
        │
        ▼
   the workflow commits "publish <sha> [skip ci]"    ← this is the signal
        │
        ▼
   Portainer polls, sees the change, re-pulls the image, restarts
        │
        ▼
   the new version is running
```

`[skip ci]` is there so that commit does not start the workflow all over
again. It is an empty commit — nothing in it but the message.

If Portainer does happen to poll in the window before the image is
ready, it redeploys the old image and then redeploys again when the
signal commit arrives, so the pipeline heals itself either way.

### A push that does not start the workflow

**Not every push starts a workflow.** A push made with an OAuth token —
the kind `gh auth login` and many editors put in the keychain — does
not, and the image for that commit is then never built. Nothing says
so: the push succeeds, the repository moves, and the image simply
never appears. The dashboard sits on the old version until somebody
happens to commit again.

A **classic personal access token** does start the workflow, with the
`repo` scope.

Either way the workflow does not depend on it any more:

- it asks the registry whether the image for this commit is already
  published, and does nothing if it is
- it also runs **every four hours**, which catches the push that did
  not trigger it

So a missed trigger costs a few hours, not forever.

### Polling, and why not a webhook

Portainer can also be told to redeploy on demand through a **webhook**,
which is the tidier mechanism — a workflow calls it the moment the image
is published, and there is no polling at all.

A webhook does not work here, though. The target is on a private
network: `minix.local` is a LAN address, and the runner that builds the
image is on the internet and cannot reach it. The workflow can push to
the registry, because the registry is public; it cannot call a webhook
on someone's home network.

So the stack polls every five minutes instead, and the signal commit is
what tells it there is something to fetch.

## Before you start

The repository is public, so Portainer can clone it without a
credential. The image in the registry is public too.

| What | Where the credential goes |
|---|---|
| Cloning the repository | Nothing — the repository is public |
| Pulling the image | Nothing — the package is public |

If you would rather keep the repository private, give the stack a
**GitHub token** (classic) with the `repo` scope in its
**Authentication → Basic** fields. A **deploy key** (an SSH key) works
instead of a token, and is tidier: it is scoped to this one repository.

## 1. The stack

**Stacks → Add stack → Git repository**

| Field | Value |
|---|---|
| Name | `offgrid` |
| Repository URL | `https://github.com/gosdenj/offgrid-dashboard.git` |
| Repository reference name | `refs/heads/main` |
| Compose path | `docker-compose.portainer.yml` |
| Authentication | None, if the repository is public |
| Environment variables | `OFFGRID_PORT` if the host port should not be 8767 |
| **Pull latest image** | **on** |
| **Poll repository for changes** | **on**, every 5 minutes |

The last two are what make it automatic. *Pull latest image* is the
re-pull of the registry image; without it, Portainer would redeploy the
old image and nothing would change.

Deploy it. The first deployment pulls the image, creates the volume and
starts the container.

### The same thing over the API

If you would rather not click through the form:

```bash
curl -X POST "http://minix.local:9000/api/stacks?type=2&method=repository&endpointId=2" \
  -H "X-API-Key: $PORTAINER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "Name": "offgrid",
    "RepositoryURL": "https://github.com/gosdenj/offgrid-dashboard.git",
    "RepositoryReferenceName": "refs/heads/main",
    "ComposeFile": "docker-compose.portainer.yml",
    "AutoUpdate": {
      "Interval": "5m",
      "ForceUpdate": false,
      "ForcePullImage": true
    },
    "Env": []
  }'
```

`type=2` is a Docker Compose stack and `method=repository` means the
compose file comes from the repository; `endpointId` is the environment
in **Endpoints**. The API key goes in `X-API-Key`, not in
`Authorization: Bearer`.

## 2. The settings

The settings file — the Home Assistant address, the long-lived access
token, the site's coordinates — lives in a **named Docker volume**,
`offgrid-config`, mounted at `/config`.

A volume rather than a folder on the host, for three reasons:

- it is the **same** wherever the container runs, with no path to get wrong
- it **survives a redeploy** — a redeploy replaces the container, not the
  volume
- it does not depend on the host and the container sharing a filesystem,
  which they do not if Portainer is a container with Docker Desktop
  underneath it

The volume takes its ownership from the image, where `/config` belongs to
uid 1000 — the uid the container runs as. So the dashboard can write to it
without anybody having to `chown` anything.

To fill the settings in, open **http://<target-ip>:8767/settings** and
save them. The first save writes the file into the volume.

To read them back:

```bash
docker run --rm -v offgrid-config:/config ghcr.io/gosdenj/offgrid-dashboard:latest \
  cat /config/config.json
```

### Getting a token

Home Assistant, bottom left, on your own profile page, scroll to the
bottom, **Long-lived access tokens**, create one, and copy it. It is
shown once.

### Setting the token without the settings page

The container can write the first settings file from its environment, so
the token can be put in the stack's **Environment variables** tab:

| Variable | What it is |
|---|---|
| `OFFGRID_HASS_URL` | the Home Assistant address |
| `OFFGRID_HA_TOKEN` | the long-lived access token |
| `OFFGRID_LAT` | the site's latitude |
| `OFFGRID_LON` | the site's longitude, east-positive |
| `OFFGRID_TZ` | the site's timezone |

These are read **once**, when the settings file is not there yet. After
that the settings page is the way to change them, and the stack's
environment variables are left alone.

### If the volume ever gets the wrong ownership

```bash
docker run --rm -u 0 -v offgrid-config:/config alpine chown -R 1000:1000 /config
```

## 3. The updates

With polling on, Portainer checks the repository every five minutes. When
the commit has changed, it pulls the new image and recreates the container.

**There is no rollback.** That is the one thing Portainer does not do. If
a new version breaks the dashboard, the way back is to check out the last
good commit and let Portainer redeploy it:

```bash
git revert <the bad commit>
git push
```

or, from the stack's page, **Pull and redeploy** after moving `main` back
to the last good commit.

## 4. The screen

The wall panel is a browser and an address:

```
http://<target-ip>:8767/
```

The panel is fixed at 800×480, and the page scales itself to fit whatever
screen it is on.

## The catch

**The dashboard has no authentication.** Anything that can reach the port
can read the page — which carries the Home Assistant access token — and
can change the settings.

On a box with a public address, do not publish the port. Keep it on the
LAN, put a password in front of it, or reach it over SSH:

```bash
ssh -L 8767:127.0.0.1:8767 you@your-server
```

The token is in the page's source, so anything that can read the page can
read the token. That is the same bargain the other dashboards in this
family make, and the reason the port belongs on the LAN.

## When something goes wrong

**The stack will not deploy** — the container's logs say why. The usual
one is the image: `docker pull ghcr.io/gosdenj/offgrid-dashboard:latest` on
the target will say whether it is a credentials problem or a missing
package.

**The dashboard says "Connecting…"** — it has no settings, or the token
was refused. Open `/settings` and check them. The page's own banner says
"Token rejected" when the token itself is the problem.

**The settings will not save** — the container cannot write to the volume.
See *If the volume ever gets the wrong ownership* above.

**A new commit did not arrive** — check that the workflow ran (the
*Actions* tab in the repository), that the image was published (the
*Packages* tab), and that *Pull latest image* is on in the stack.
