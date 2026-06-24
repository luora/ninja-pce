# ninja-pce GNS3 lab container
# Build from the repository root:
#   docker build -t ninja-pce:gns3 -f gns3/docker/ninja-pce/Dockerfile .
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NINJA_PCE_LAB=/lab \
    NINJA_PCE_OUTPUTS=/outputs

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      iproute2 \
      iputils-ping \
      jq \
      less \
      nano \
      net-tools \
      netcat-openbsd \
      openssh-client \
      tcpdump \
      telnet \
      traceroute \
      vim-tiny \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ninja-pce
COPY . /opt/ninja-pce
RUN pip install --no-cache-dir -e '.[gns3]'

RUN mkdir -p /lab /outputs /startup
COPY gns3/docker/ninja-pce/entrypoint.sh /startup/entrypoint.sh
RUN chmod +x /startup/entrypoint.sh

VOLUME ["/lab", "/outputs"]
WORKDIR /lab
CMD ["/startup/entrypoint.sh"]
