variable "region" {
  description = "AWS region. ap-south-1 (Mumbai) is closest to India; any region works."
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  description = "t3.small (2 vCPU, 2 GiB, plus 2 GiB swap) runs the controller and one Linux agent. Use t3.medium for faster builds."
  type        = string
  default     = "t3.small"
}

variable "volume_size_gb" {
  description = "Root disk size. Holds Docker images, Jenkins home and build workspaces."
  type        = number
  default     = 30
}

variable "github_username" {
  description = "GitHub account that owns the token."
  type        = string
  default     = "aryankhanna1409"
}

variable "github_token" {
  description = "Fine-grained GitHub token for the AETHER repo (see docs/CI_CD.md for the permissions). Put it in terraform.tfvars, never in git."
  type        = string
  sensitive   = true
}

variable "repo_url" {
  description = "Repository the instance clones to get this infrastructure code."
  type        = string
  default     = "https://github.com/aryankhanna1409/A.E.T.H.E.R.git"
}

variable "repo_ref" {
  description = "Branch or tag of repo_url to run the Jenkins stack from."
  type        = string
  default     = "main"
}

variable "jenkins_host" {
  description = "DNS name for Jenkins. Empty means <elastic-ip>.sslip.io, a free name that resolves to the IP and gets a real HTTPS certificate."
  type        = string
  default     = ""
}

variable "budget_email" {
  description = "Optional: email for a monthly AWS budget alert. Empty skips the budget."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Budget alert threshold in USD when budget_email is set."
  type        = number
  default     = 25
}
