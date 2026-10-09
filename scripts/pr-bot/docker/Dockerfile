# Ubuntu image with the toolchains the tld PR action expects from an
# ubuntu-latest GitHub runner (Go, Node, PHP/Composer, .NET, Java, Dart, Ruby,
# Rust). The end-to-end test installs the tld binary and every SCIP indexer with
# the same script the action uses, then drives the bot against a fixture repo.
#
# Build for linux/amd64: the scip-clang and scip-ruby releases only ship
# x86_64-linux artifacts, and the Dart Debian repository publishes amd64 only.
FROM --platform=linux/amd64 ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG GO_VERSION=1.26.2
ARG NODE_MAJOR=24
ARG DOTNET_CHANNEL=8.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git gnupg lsb-release python3 tar unzip xz-utils \
 && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) goarch=amd64 ;; \
      arm64) goarch=arm64 ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${goarch}.tar.gz" -o /tmp/go.tar.gz; \
    tar -C /usr/local -xzf /tmp/go.tar.gz; \
    rm /tmp/go.tar.gz
ENV PATH=/usr/local/go/bin:$PATH
ENV GOTOOLCHAIN=local

RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# PHP + the latest Composer. The distro Composer predates policy.advisories, so
# it would not exercise the advisory handling the php indexer install relies on.
RUN apt-get update \
 && apt-get install -y --no-install-recommends php-cli php-curl php-mbstring php-xml \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://getcomposer.org/installer -o /tmp/composer-setup.php \
 && php /tmp/composer-setup.php --install-dir=/usr/local/bin --filename=composer \
 && rm /tmp/composer-setup.php

# .NET SDK drives scip-dotnet; libicu is required by the runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libicu74 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
 && bash /tmp/dotnet-install.sh --channel "$DOTNET_CHANNEL" --install-dir /usr/share/dotnet \
 && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
 && rm /tmp/dotnet-install.sh
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1

# JDK backs the coursier-installed scip-java launcher.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-21-jdk-headless \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://dl-ssl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/dart.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/dart.gpg] https://storage.googleapis.com/download.dartlang.org/linux/debian stable main" \
      > /etc/apt/sources.list.d/dart_stable.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends dart \
 && rm -rf /var/lib/apt/lists/*

# Ruby backs the arm64 fallback; x86_64 downloads the scip-ruby binary directly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ruby-full \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://sh.rustup.rs -o /tmp/rustup.sh \
 && sh /tmp/rustup.sh -y --profile minimal --default-toolchain stable \
 && rm /tmp/rustup.sh
ENV PATH=/root/.cargo/bin:$PATH

WORKDIR /action
COPY scripts/pr-bot /action/scripts/pr-bot
ENTRYPOINT ["bash", "/action/scripts/pr-bot/docker/e2e.sh"]
