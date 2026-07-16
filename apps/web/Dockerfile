# syntax=docker/dockerfile:1.20@sha256:26147acbda4f14c5add9946e2fd2ed543fc402884fd75146bd342a7f6271dc1d

ARG NODE_IMAGE=node:24-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d

FROM ${NODE_IMAGE} AS builder

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /build

COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci --ignore-scripts

COPY apps/web ./apps/web
RUN npm run build --workspace @myretail/web


FROM ${NODE_IMAGE} AS runner

ARG OCI_REVISION=unknown
ARG OCI_SOURCE=https://github.com/ernurreen-rgb/MyRetail

LABEL org.opencontainers.image.source=${OCI_SOURCE} \
      org.opencontainers.image.revision=${OCI_REVISION} \
      org.opencontainers.image.title="MyRetail web"

ENV HOME=/nonexistent \
    HOSTNAME=0.0.0.0 \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production \
    PORT=3000

RUN groupadd --gid 10001 myretail \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent myretail \
    && install --directory --owner=10001 --group=10001 --mode=0750 /app

WORKDIR /app

COPY --from=builder --chown=10001:10001 /build/apps/web/.next/standalone ./
COPY --from=builder --chown=10001:10001 /build/apps/web/.next/static ./apps/web/.next/static

WORKDIR /app/apps/web

USER 10001:10001

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]

CMD ["node", "server.js"]
