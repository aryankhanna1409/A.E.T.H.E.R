#!/usr/bin/env bash
# Starts (or restarts with new settings) the AETHER Jenkins stack on this machine.
#
#   On EC2 (Terraform runs this at first boot; run it again after changing secrets or pulling changes):
#     sudo AETHER_SSM_PREFIX=/aether-jenkins /opt/aether/infra/jenkins/bootstrap.sh
#   Anywhere else with Docker: fill in .env from .env.example, then ./bootstrap.sh
#
# With AETHER_SSM_PREFIX set, secrets are read from SSM Parameter Store using the instance role,
# so they never sit in user data, the repository or shell history.
set -euo pipefail
cd "$(dirname "$0")"

log() { printf '[bootstrap] %s\n' "$*"; }

ssm_get() {
  aws ssm get-parameter --with-decryption --name "${AETHER_SSM_PREFIX}/$1" --query Parameter.Value --output text
}

set_env() {  # set_env KEY VALUE: replace or append one line of .env
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  { grep -v "^${key}=" .env 2>/dev/null || true; printf '%s=%s\n' "$key" "$value"; } > "$tmp"
  mv "$tmp" .env
}

command -v docker >/dev/null || { echo "Docker is not installed" >&2; exit 1; }
docker compose version >/dev/null || { echo "The Docker Compose plugin is not installed" >&2; exit 1; }

touch .env
chmod 600 .env

if [[ -n "${AETHER_SSM_PREFIX:-}" ]]; then
  if [[ -z "${AWS_DEFAULT_REGION:-}" ]]; then   # on EC2: ask the instance metadata service (IMDSv2)
    imds_token="$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
    AWS_DEFAULT_REGION="$(curl -fsS -H "X-aws-ec2-metadata-token: ${imds_token}" http://169.254.169.254/latest/meta-data/placement/region)"
    export AWS_DEFAULT_REGION
  fi
  log "Reading secrets from SSM under ${AETHER_SSM_PREFIX} (${AWS_DEFAULT_REGION})"
  set_env JENKINS_ADMIN_PASSWORD "$(ssm_get admin-password)"
  set_env GITHUB_TOKEN "$(ssm_get github-token)"
  set_env GITHUB_WEBHOOK_SECRET "$(ssm_get webhook-secret)"
  set_env GITHUB_USERNAME "$(ssm_get github-username)"
  set_env JENKINS_HOST "$(ssm_get jenkins-host)"
  grep -q '^WINDOWS_AGENT_ENABLED=' .env || set_env WINDOWS_AGENT_ENABLED false
fi

for key in JENKINS_HOST JENKINS_ADMIN_PASSWORD GITHUB_USERNAME GITHUB_TOKEN GITHUB_WEBHOOK_SECRET; do
  grep -q "^${key}=." .env || { echo "${key} is missing from .env" >&2; exit 1; }
done

if [[ ! -f secrets/agent_ed25519 ]]; then
  log "Generating the controller-to-agent SSH key"
  mkdir -p secrets
  ssh-keygen -q -t ed25519 -N '' -C 'jenkins-controller' -f secrets/agent_ed25519
fi
chmod 700 secrets
chown 1000:1000 secrets/agent_ed25519 2>/dev/null || true   # uid of the jenkins user in the image
chmod 600 secrets/agent_ed25519
set_env JENKINS_AGENT_SSH_PUBKEY "$(cat secrets/agent_ed25519.pub)"

log "Building and starting the stack"
docker compose up -d --build --remove-orphans
# Recreate the controller so it picks up changed secrets; configuration as code is reapplied at start.
docker compose up -d --force-recreate --no-deps jenkins

host="$(grep '^JENKINS_HOST=' .env | cut -d= -f2-)"
log "Jenkins will be up at https://${host}/ in a minute or two, once Caddy has its HTTPS certificate."
