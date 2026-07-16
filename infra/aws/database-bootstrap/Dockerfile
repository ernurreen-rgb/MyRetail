# syntax=docker/dockerfile:1.20@sha256:26147acbda4f14c5add9946e2fd2ed543fc402884fd75146bd342a7f6271dc1d

ARG POSTGRES_IMAGE=postgres:18.3-bookworm@sha256:80630f83606d8db77d30b3851b16a9f78be2d0d4dda6f7b82a1fdca5ebe3acba

FROM ${POSTGRES_IMAGE}

ARG OCI_REVISION=unknown
ARG OCI_SOURCE=https://github.com/ernurreen-rgb/MyRetail

LABEL org.opencontainers.image.source=${OCI_SOURCE} \
      org.opencontainers.image.revision=${OCI_REVISION} \
      org.opencontainers.image.title="MyRetail database role bootstrap"

ADD --checksum=sha256:e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3 \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    /etc/ssl/certs/aws-rds-global-bundle.pem

COPY --chmod=0555 infra/aws/database-bootstrap/bootstrap.sh /usr/local/bin/myretail-database-bootstrap
COPY --chmod=0444 infra/aws/database-bootstrap/bootstrap.sql /usr/local/share/myretail/bootstrap.sql

USER 999:999

ENTRYPOINT ["myretail-database-bootstrap"]
