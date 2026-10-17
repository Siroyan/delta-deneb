FROM node:24-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.12.24 AS uv

FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

USER root

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv /uv /uvx /usr/local/bin/

ARG AWS_CLI_VERSION=2.38.1
# The base image's Yarn repository is unnecessary for this npm-based project.
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl groff less unzip \
    && case "$(dpkg --print-architecture)" in \
        amd64) aws_arch=x86_64 ;; \
        arm64) aws_arch=aarch64 ;; \
        *) exit 1 ;; \
    esac \
    && curl --fail --silent --show-error --location --retry 3 \
        "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}-${AWS_CLI_VERSION}.zip" \
        --output /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp/aws-cli-install \
    && /tmp/aws-cli-install/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws-cli-install /var/lib/apt/lists/*

# Keep Linux packages separate from a host-side .venv in the mounted repository.
ENV UV_PROJECT_ENVIRONMENT=/home/vscode/.venvs/delta-deneb \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    AWS_DEFAULT_REGION=ap-northeast-1 \
    AWS_PAGER=""

USER vscode
WORKDIR /workspaces/delta-deneb
