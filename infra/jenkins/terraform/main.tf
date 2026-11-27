# One EC2 instance running the Jenkins stack in Docker (infra/jenkins/docker-compose.yml):
# Caddy for HTTPS, the Jenkins controller and a Linux build agent. No SSH: shell access is through
# AWS Systems Manager Session Manager, and secrets live in SSM Parameter Store.

locals {
  name       = "aether-jenkins"
  ssm_prefix = "/aether-jenkins"
  host       = var.jenkins_host != "" ? var.jenkins_host : "${replace(aws_eip.jenkins.public_ip, ".", "-")}.sslip.io"
}

data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

# --- Network ---------------------------------------------------------------------------------------

resource "aws_security_group" "jenkins" {
  name        = local.name
  description = "Jenkins for AETHER: HTTPS in (GitHub webhooks, browser, Windows agent), everything out"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.jenkins.id
  description       = "HTTPS"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.jenkins.id
  description       = "HTTP, for the Let's Encrypt challenge and the redirect to HTTPS"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.jenkins.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_eip" "jenkins" {
  domain = "vpc"
  tags   = { Name = local.name }
}

resource "aws_eip_association" "jenkins" {
  instance_id   = aws_instance.jenkins.id
  allocation_id = aws_eip.jenkins.id
}

# --- Secrets ---------------------------------------------------------------------------------------

resource "random_password" "admin" {
  length  = 24
  special = false
}

resource "random_password" "webhook" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "secrets" {
  for_each = {
    "admin-password" = random_password.admin.result
    "webhook-secret" = random_password.webhook.result
    "github-token"   = var.github_token
  }
  name  = "${local.ssm_prefix}/${each.key}"
  type  = "SecureString"
  value = each.value
}

resource "aws_ssm_parameter" "settings" {
  for_each = {
    "github-username" = var.github_username
    "jenkins-host"    = local.host
  }
  name  = "${local.ssm_prefix}/${each.key}"
  type  = "String"
  value = each.value
}

# --- Instance role: Session Manager access and read-only access to its own parameters ---------------

resource "aws_iam_role" "jenkins" {
  name = local.name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.jenkins.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "read_parameters" {
  name = "read-aether-jenkins-parameters"
  role = aws_iam_role.jenkins.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:GetParameters"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}/*"
    }]
  })
}

resource "aws_iam_instance_profile" "jenkins" {
  name = local.name
  role = aws_iam_role.jenkins.name
}

# --- Instance --------------------------------------------------------------------------------------

resource "aws_instance" "jenkins" {
  ami                    = data.aws_ssm_parameter.ubuntu.value
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.jenkins.name
  vpc_security_group_ids = [aws_security_group.jenkins.id]

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url   = var.repo_url
    repo_ref   = var.repo_ref
    ssm_prefix = local.ssm_prefix
  })

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.volume_size_gb
    encrypted   = true
  }

  # A newer Ubuntu image should not replace a running Jenkins; rebuild deliberately with -replace.
  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = local.name }

  depends_on = [aws_ssm_parameter.secrets, aws_ssm_parameter.settings]
}

# --- Optional cost guard ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email != "" ? 1 : 0
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
