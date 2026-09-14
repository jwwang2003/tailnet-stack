# Build, versioning, and release workflow

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
  └── downstream/feishu-2026.09
        └── feature/<bounded-change>
              └── reviewed linear commits
                    └── production/feishu-2026.09
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
git worktree add --detach ../build-headscale-feishu-2026.09 5aff68b5b9921db5ccb88013bb1740077ab872fb
cd ../build-headscale-feishu-2026.09
git status --short
git rev-parse HEAD
go version
go mod download
go mod verify
go test -race ./hscontrol/...
mkdir -p dist
go build -trimpath -buildmode=pie -ldflags '-X main.version=v0.29.3-feishu.1' -o dist/headscale ./cmd/headscale
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
docker build --target final --build-arg HEADPLANE_VERSION=0.7.1-feishu.1 --build-arg IMAGE_TAG=0.7.1-feishu.1 -t headplane:0.7.1-feishu.1 .
```

For local Headplane checks, follow its pinned `package.json`: Node 24,
`pnpm@10.4.0`, `pnpm install --frozen-lockfile`, `pnpm typecheck`, and
`pnpm test:unit`. Its Go components use the toolchain declared in `go.mod` and the
upstream `build.sh`; avoid running only the web build when producing the complete
application image.

The pinned Casdoor Dockerfile has an explicit `STANDARD` target and defaults to a
different final target. Select `STANDARD` for the standalone application image:

```sh
docker build --target STANDARD -t casdoor:4.3.0-feishu.1 .
```

Upstream Dockerfiles contain mutable base image tags and package repositories.
These commands reproduce the selected application source, not necessarily
byte-for-byte identical images. For a reproducible production build, retain the
built image digest and build provenance, and pin the builder/runtime base digests
in an integration-owned build recipe. Do not silently edit the upstream source
checkout to change its toolchain. Private registry publication and deployment are
separate actions from a successful local build.

## Record and promote one complete release

1. Update all three `source_commit` fields and the resulting image references and
   digests in `versions.lock.yaml`. Include proxy, database, and sync image digests
   in the release manifest. Do not claim `compatibility_verified: true` before the
   following checks pass.
2. Validate the service configurations with their pinned binaries, then exercise
   Feishu login before and after synchronization, stable identity across both
   downstream clients, directory membership changes, and existing access revocation.
3. Test the exact candidate images in an isolated environment with a restored
   backup. Record migration outcomes, configuration checksums, and test evidence
   using `releases/manifest.example.yaml`. The manifest records the integration
   commit it describes; never attempt to store a commit's own SHA inside itself.
4. Commit the release evidence and fast-forward each service's production branch
   to the selected component commit. Create matching annotated release-series tags
   in the three forks and this repository. Component tags can share the series
   identifier, such as `feishu-2026.09.0-rc.1`; keep upstream tags unchanged.
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

After rollback, check existing client connectivity, new Feishu authentication,
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
