# jev's WindowsAgentArena client. scripts/waa passes the pinned base image as BASE_IMAGE.
# The base image's Python 3.9 stays; the client runs from the 3.12 venv at /opt/waa.
# The client tree and this repo are mounted at run time, not copied in here.

ARG BASE_IMAGE=windowsarena/winarena-base
FROM ${BASE_IMAGE}

COPY src/win-arena-container/vm/setup/. /shared/
COPY src/win-arena-container/vm/unattend-files/dev_win11x64-enterprise-eval.xml /run/assets/win11x64-enterprise-eval.xml
ENV FOLDER_NAME=shared

COPY src/win-arena-container/client /client
COPY src/win-arena-container/entry_setup.sh /entry_setup.sh
COPY src/win-arena-container/start_client.sh /start_client.sh
COPY src/win-arena-container/start_vm.sh /start_vm.sh
COPY src/win-arena-container/entry.sh /entry.sh

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates dos2unix fuse \
    && find / -maxdepth 3 -type f -name "*.sh" -exec dos2unix {} \; && chmod +x /*.sh \
    && rm -rf /var/lib/apt/lists/*

ENV YRES="900"
ENV XRES="1440"
ENV RAM_SIZE="8G"
ENV CPU_CORES="8"
ENV VERSION="win11x64-enterprise-eval"
ENV DISK_SIZE="30G"
ENV ARGUMENTS="-qmp tcp:0.0.0.0:7200,server,nowait"

COPY --from=ghcr.io/astral-sh/uv:0.9.6 /uv /usr/local/bin/uv
RUN uv python install 3.12 \
    && uv venv /opt/waa --python 3.12
COPY src/win-arena-container/waa-requirements.txt /opt/waa/requirements.txt
RUN uv pip install --python /opt/waa --no-cache \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --index-strategy unsafe-best-match \
    -r /opt/waa/requirements.txt

ENTRYPOINT ["/bin/bash", "-c"]
