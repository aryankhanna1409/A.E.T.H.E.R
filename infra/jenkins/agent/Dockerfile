# Linux build agent: runs lint, unit tests and releases. The controller connects to it over SSH
# on the private Docker network; nothing on this container is exposed to the internet.
FROM jenkins/ssh-agent:9.9.0-jdk21

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -c "import sys; assert sys.version_info >= (3, 11), sys.version"
