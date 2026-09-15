# Build, versioning, and release workflow

The integration repository is `jwwang2003/tailnet-stack`. Its `main` branch
contains provider-neutral development; `downstream/integrated-2026.09` tracks
the integration series. Product forks retain their own branch names and exact
source pins. Release branches and commit IDs stay fixed for reproducible
deployment, including historical Feishu releases. See
[naming and migration](branding-migration.md) when updating an existing checkout.

`versions.lock.yaml` is the shared source of truth for the source tuple. An upstream
tag identifies the official base; `upstream_commit` is its peeled commit and
`source_commit` is the exact downstream commit that will be built. Keeping both
fields makes the local patch set reviewable. The initial source pins were resolved
with `git ls-remote` against the official repositories on 2026-09-14.

## Branch ownership and linear history

Use the same release series in all three service forks and this integration
repository:

```text
official release commit
  └── downstream/integrated-2026.09
        └── feature/<bounded-change>
              └── reviewed linear commits
                    └── production/integrated-2026.09
```

`main` (or Casdoor's `master`) remains the fork-sync branch. Do not develop,
cherry-pick, or merge integrations into it. Headscale's baseline comes from
`release-branch/0.29` at `v0.29.3`; Headplane and Casdoor begin at official tags
`v0.7.1` and `v4.3.0`. Production uses exact commits, not whichever commit a moving
upstream release branch happens to contain.

Configure the requested identity in each repository:

```sh
git config user.name 'JUN WEI WANG'
git config user.email 'wjw_03@outlook.com'
```

Create a worktree for each agent from the selected downstream base. Agents must
own disjoint files and must not switch the coordinator's checkout branch. Commit
at meaningful milestones: source/configuration contract, working implementation,
and reviewed release evidence. Review commits before integrating them with
`git cherry-pick` or `git merge --ff-only`; do not create merge commits. If multiple
features begin at the same base, cherry-pick their reviewed commits sequentially
onto the integration branch. Rebase only unpublished feature work. Preserve
published production history and release tags.

On a new upstream version, create a new release series at its official commit and
replay only still-needed downstream patches. Do not rewrite the existing production
branch. Change the integration lock in one commit after all component refs are
known; the Git SHA of that lock commit identifies the complete candidate. Keep
the old manifest and images available for rollback.

## Downstream patch series layout

Each fork carries a short linear series on top of its official tag. Keep the
patches separate so that each can be reviewed, replaced or retired on its own
when the baseline moves; the retirement condition is part of the commit message.

| Fork | Patch | Retire when |
| --- | --- | --- |
| Headscale | Pure upstream backports (PKCE method validation, provider context timeout, callback CSRF/state hardening), one commit each naming the upstream SHA | The new baseline contains that SHA; skip the commit during replay |
| Headscale | Atomic callback-state consumption (our delta, tests in `oidc_state_test.go` and `types/config_oidc_test.go`) | Upstream checks the cache `Remove` result |
| Headscale | Version override for downstream clients | Never automatically; keep small |
| Headscale | Localization infrastructure (`templates/auth_locale.go`, one insertion in the shared body helper) | Upstream ships a language control |
| Headscale | Translated authentication labels (literals wrapped at their call sites; helper signatures unchanged) | Expect a few label-site conflicts when upstream rewrites those pages |
| Headscale | Form attribute escaping helper (`templates/attr.go`) | The HTML library escapes attributes itself |
| Headplane | i18n infrastructure (`app/i18n`, provider, format helpers, translation coverage test) | Upstream ships localization |
| Headplane | Shared components translate their own copy props | With the infrastructure |
| Headplane | Translated screens, one commit per area (chrome, login, machines, users, DNS, settings, ACL, SSH); literal-to-call replacements only | Rebase each area separately; the coverage test flags wording drift |
| Headplane | Language selector placement (one header insertion) | Upstream ships a selector |
| Headplane | Branding (`app/components/organization-brand.tsx`, `app/server/branding.server.ts`, one header insertion) | Upstream ships configurable branding |
| Headplane | Docs, changelog and build changes | With the feature they document |
| Casdoor | OAuth/directory identity alignment, Lark cursor encoding, Chinese profile labels, partial-update snapshot fix (`object/user_clone.go`) | Upstream fixes the same defect; compare before dropping |

On an upstream bump, replay the series in order with `git rebase --onto <new tag>
<old tag> <branch>`, skip retired backports explicitly (patch ids differ after
adaptation, so Git will not drop them for you), rerun the fork's focused tests,
the Headplane coverage test, the worker suite and the Casdoor authorization
probe, then update the lock in one commit. The restructuring measured on
2026-09-15 against the upstream `main` branches of that day reduced replay
conflicts from 35 to 6 hunks for Headscale and from 5 to 1 for Headplane.

## Build Headscale from the exact source

The integration helpers require Python 3 and PyYAML. Install
`requirements-build.txt` in a virtual environment. From this integration checkout,
with the three component repositories as siblings, run:

```sh
python3 scripts/release.py verify-sources ..
bash scripts/build-headscale.sh ..
bash scripts/build-products.sh ..
```

If outbound registry/dependency access requires the Windows SSH tunnel, use the [Docker proxy recipe](docker-build-proxy.md). The opt-in command is `BUILD_PROXY_URL=http://127.0.0.1:17890 bash scripts/build-products.sh ..`; daemon pulls also need their separate proxy setting.

The first helper checks that all three source checkouts are clean, are outside
`main`/`master`, and match their `source_commit`. The build helpers repeat that
check. The native Headscale binary goes into ignored `dist/headscale/`; the image
builder creates local tags from the lock. These helpers do not publish images,
deploy services, run the complete test matrix, or promote branches. Casdoor must
be built after the lock points at the required downstream identity patch; a stock
Casdoor image is not interchangeable with that patched build.

Requirements for the pinned baseline: Git, Go 1.26.5 or the selected newer
compatible toolchain, a C compiler when building with the race detector, and
network access to fetch modules. Record the exact Go toolchain used. The upstream
`flake.lock` also provides a pinned development environment through `nix develop`.
Do not update module dependencies while producing a release build.

These commands run from the `headscale` repository. For a patched release, replace
the commit and downstream version with `source_commit` and the approved release
version before executing:

```sh
git worktree add --detach ../build-headscale-integrated-2026.09 "$(python3 ../tailnet-stack/scripts/release.py field headscale source_commit)"
cd ../build-headscale-integrated-2026.09
git status --short
git rev-parse HEAD
go version
go mod download
go mod verify
go test -race ./hscontrol/...
mkdir -p dist
go build -trimpath -buildmode=pie -ldflags '-X github.com/juanfont/headscale/hscontrol/types.VersionOverride=v0.29.3-integrated.1' -o dist/headscale ./cmd/headscale
./dist/headscale version
sha256sum dist/headscale
```

`go build` above mirrors the upstream Makefile's version injection and Linux PIE
build mode without requiring the Makefile's unrelated formatting tools. Build on
the target architecture or record the chosen `GOOS`/`GOARCH` and validate that
binary on the target host. Preserve the binary checksum, source SHA, toolchain,
test output, and the license alongside the artifact.

For an unmodified component, consuming an official release image is also valid.
Pull the exact version and record its repository digest; deploy using
`repository@sha256:...`. A tag is a source selection aid, while the retained image
digest identifies the artifact actually tested. Never substitute `latest` during
deployment or rollback.

## Build Headplane and Casdoor

Use isolated worktrees at each `source_commit`. The pinned Headplane Dockerfile
already builds its Go components and web application, and provides an explicit
`final` production target. From that worktree:

```sh
docker build --target final --build-arg HEADPLANE_VERSION=0.7.1-integrated.1 --build-arg IMAGE_TAG=0.7.1-integrated.1 -t headplane:0.7.1-integrated.1 .
```

For local Headplane checks, follow its pinned `package.json`: Node 24,
`pnpm@10.4.0`, `pnpm install --frozen-lockfile`, `pnpm typecheck`, and
`pnpm test:unit`. Its Go components use the toolchain declared in `go.mod` and the
upstream `build.sh`; avoid running only the web build when producing the complete
application image.

The pinned Casdoor Dockerfile has an explicit `STANDARD` target and defaults to a
different final target. Select `STANDARD` for the standalone application image:

```sh
docker build --target STANDARD -t casdoor:4.3.0-integrated.1 .
```

Upstream Dockerfiles contain mutable base image tags and package repositories.
These commands reproduce the selected application source, not necessarily
byte-for-byte identical images. For a reproducible production build, retain the
built image digest and build provenance, and pin the builder/runtime base digests
in an integration-owned build recipe. Do not silently edit the upstream source
checkout to change its toolchain. Private registry publication and deployment are
separate actions from a successful local build.

## Publish Docker images to Huawei SWR

Create an SWR organization and grant your IAM user push access. Export
`HUAWEI_AK` and `HUAWEI_SK` in the shell running the script; use the original
access-key pair, not the password copied from a generated login command.
Environment files are not loaded automatically. Keep them outside Git.

From the integration checkout, preview the six-image upload:

```sh
python3 scripts/push-swr.py --region cn-east-3 --organization YOUR_ORGANIZATION --dry-run
```

Then upload:

```sh
python3 scripts/push-swr.py --region cn-east-3 --organization YOUR_ORGANIZATION
```

Alternatively export `SWR_REGION` and `SWR_ORG` and run the script without flags.
The region determines both `swr.REGION.myhuaweicloud.com` and the login username
`REGION@AK`. An existing `SWR_REGISTRY` must match the selected region. This
implements Huawei's **general long-term login** using HMAC-SHA256, with the
derived password supplied to `docker login --password-stdin`. It does not
generate enhanced/temporary login credentials. Docker retains the login using
its configured credential store. See
[Huawei's login procedure](https://support.huaweicloud.com/usermanual-swr/swr_01_1000.html)
and [Docker login](https://docs.docker.com/reference/cli/docker/login/).

Source names come from `versions.lock.yaml` and `image-inputs.json`; all six
images must already exist in the selected Docker daemon. Podman and Docker have
separate image stores: build/load the images into Docker first and ensure their
tags match those source files. The script checks every image's platform against
`image-inputs.json` before login or upload. Dry run prints the plan only; it does
not check local images. No build, pull, or architecture conversion is performed.

Destination repositories are `headscale`, `headplane`, `casdoor`, `sync`,
`postgres`, and `caddy`. The default destination tag is the Headscale source
image tag with its architecture suffix, e.g. `2026.09-rc.4-amd64` (an existing
matching suffix is not repeated). Use `--tag` to select a different destination
tag. Run from the ARM64 candidate checkout with matching ARM64 images to
publish for that platform; changing a tag does not change image architecture.

Successful pushes record Docker's registry manifest digests under
`.runtime/swr-digests/REGION/ORGANIZATION/TAG/COMPONENT.txt`. These are registry
digests, not the offline image IDs. Uploads stop on the first error; earlier
successful pushes remain in SWR. Reusing a destination tag can update that tag.
Record the resulting `repository@sha256:...` references in the release inventory
and deployment configuration. Uploading alone does not satisfy promotion gates.

SWR Basic has restrictions on OCI manifests. Unlike `podman push`, Docker has no
`--format v2s2` or `--digestfile` option; the script does not pretend to convert
image formats. Use compatible Docker image manifests and inspect any rejection
against [SWR's upload requirements](https://support.huaweicloud.com/intl/zh-cn/usermanual-swr/swr_01_0011.html).

## Record and promote one complete release

1. Update all three `source_commit` fields and the resulting image references and
   digests in `versions.lock.yaml`. Include proxy, database, and sync image digests
   in the release manifest. Do not claim `compatibility_verified: true` before the
   following checks pass.
2. Validate the service configurations with their pinned binaries, then exercise
   selected-provider login before and after account lifecycle changes, stable identity across both
   downstream clients, directory membership changes, and existing access revocation.
3. Test the exact candidate images in an isolated environment with a restored
   backup. Record migration outcomes, configuration checksums, and test evidence
   using `releases/manifest.example.yaml`. The manifest records the integration
   commit it describes; never attempt to store a commit's own SHA inside itself.
4. Commit the release evidence and fast-forward each service's production branch
   to the selected component commit. Create matching annotated release-series tags
   in the three forks and this repository. Component tags can share the series
   identifier, such as `integrated-2026.09.0-rc.1`; keep upstream tags unchanged.
5. Deploy the complete candidate tuple. Promote to a stable tag only after the
   tenant pilot and restore checks pass. A production branch is a deployment
   pointer, not evidence by itself that those checks passed.

Before promotion, run:

```sh
python3 scripts/release.py check-promotion releases/your-release.yaml
```

The check requires source pins, matching immutable image references, the lock
checksum, validation records, migration assessments, and restore evidence. Each
validation entry has `passed: true` and an `evidence` field pointing to its test
report. It rejects the initial candidate manifest by design. It validates recorded
evidence fields; the reviewer must still inspect the referenced reports. No Git
branch or remote is changed by this check.

## Upgrade and rollback

Review each upstream release's migration notes. Capture a consistent pre-upgrade
backup of databases, Headscale identity keys, Casdoor signing keys, relevant
sessions, sync state, and configuration, then retain the previous images. Stop
writes during an offline snapshot. Keep backups encrypted and separate from Git.

Upgrade the tested tuple during a maintenance window. On failure, stop the new
services before restoring the previous release's databases and configuration;
restore the matching keys and then start the retained old images. Never start an
old Headscale binary against a database migrated by a newer release. Database
rollback means restoring the pre-migration snapshot, not editing migration tables
or merely changing an image tag.

After rollback, check existing client connectivity, new authentication through the configured provider,
Headplane access, and directory reconciliation. Record the restore timestamp and
the amount of data lost since the snapshot. Keep synchronization paused until its
stored identity mappings and current target database agree.

## Offline releases without registry hosting

Use [local build and SSH delivery](local-build-deploy.md). The bundle records the
integration commit, the lock/input file hashes, all six local image IDs/platforms,
and a SHA-256 checksum of `images.tar`. Import verifies those identities before
updating runtime image references. `deploy/compose.offline.yaml` disables pulls.

For production evidence, copy the existing release manifest example, set
`distribution: offline`, fill `bundle_sha256` from the bundle's `archive.sha256`,
and use each bundle image's `alias` in the release `images` mapping. Retain all
existing live validation, backup/restore, migration, and compatibility requirements.
Offline image IDs are not registry manifest digests; no registry subscription is
required to record them. Run both the runtime bundle check and the release evidence check:

```sh
python3 scripts/image-bundle.py check --bundle /path/to/bundle --runtime .runtime
python3 scripts/release.py check-promotion /path/to/completed-release.yaml \
  --bundle-manifest /path/to/bundle/manifest.json
```

The source lock must be marked compatible only after that exact tuple has passed
acceptance. If changing the source lock or worker changes its recorded commit,
build/export a new matching bundle; do not edit old manifests to bypass source checks.
The evidence checker does not replace checking the actual loaded images and tar file.
