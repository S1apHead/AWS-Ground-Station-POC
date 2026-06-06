variable "name_prefix"         { type = string }
variable "kms_admin_role_arns" { type = list(string) }
variable "kms_user_role_arns"  { type = list(string) }
variable "sns_alert_arns"      { type = list(string); default = [] }
variable "is_org_trail"        { type = bool; default = false }
variable "tags"                { type = map(string); default = {} }
