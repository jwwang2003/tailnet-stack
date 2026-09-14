# Candidate build recipe. Pin both base references by digest for a production rebuild.
ARG GO_BUILDER=golang:1.26.5
ARG RUNTIME_IMAGE=gcr.io/distroless/static-debian12:nonroot
FROM ${GO_BUILDER} AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download && go mod verify
COPY . .
ARG HEADSCALE_VERSION=v0.29.3-feishu.1
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-X main.version=${HEADSCALE_VERSION}" -o /out/headscale ./cmd/headscale

FROM ${RUNTIME_IMAGE}
COPY --from=builder /out/headscale /usr/local/bin/headscale
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/headscale"]
CMD ["serve"]
