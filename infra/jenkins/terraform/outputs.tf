output "jenkins_url" {
  description = "Jenkins web UI. Sign in as admin."
  value       = "https://${local.host}/"
}

output "jenkins_admin_password" {
  description = "Password for the admin user. Read it with: terraform output -raw jenkins_admin_password"
  value       = random_password.admin.result
  sensitive   = true
}

output "github_webhook_url" {
  description = "Payload URL for the GitHub webhook."
  value       = "https://${local.host}/github-webhook/"
}

output "github_webhook_secret" {
  description = "Secret for the GitHub webhook. Read it with: terraform output -raw github_webhook_secret"
  value       = random_password.webhook.result
  sensitive   = true
}

output "readme_badge" {
  description = "Markdown for the build status badge."
  value       = "[![Build Status](https://${local.host}/buildStatus/icon?job=aether%2Fmain)](https://${local.host}/job/aether/job/main/)"
}

output "instance_id" {
  value = aws_instance.jenkins.id
}

output "shell_command" {
  description = "Open a shell on the instance (needs the AWS CLI Session Manager plugin), or use EC2 > Connect > Session Manager in the console."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.jenkins.id}"
}
