"""
guardduty-responder — SOAR Lambda
LLD Ref: LLD-SEC-001

Triggered by EventBridge rule: GuardDuty HIGH/CRITICAL findings (severity >= 7.0)

Actions:
  1. Enrich finding with asset context
  2. Route to severity-specific response playbook
  3. Publish structured alert to SNS NOC topic
  4. Update Security Hub finding status
  5. For CRITICAL findings: quarantine EC2/EKS node via Security Group
"""

import os
import json
import logging
import datetime
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION       = os.getenv("AWS_REGION", "ap-southeast-2")
SNS_SECURITY_ARN = os.getenv("SNS_SECURITY_TOPIC_ARN", "")
SNS_NOC_ARN      = os.getenv("SNS_NOC_TOPIC_ARN", "")
QUARANTINE_SG_ID = os.getenv("QUARANTINE_SG_ID", "")

# Minimum severity to trigger automated quarantine
QUARANTINE_SEVERITY_THRESHOLD = float(os.getenv("QUARANTINE_SEVERITY_THRESHOLD", "9.0"))


def lambda_handler(event, context):
    """Main Lambda entry point — receives GuardDuty finding from EventBridge."""
    logger.info("SOAR triggered: %s", json.dumps(event, default=str))

    detail = event.get("detail", {})
    finding_id   = detail.get("id", "UNKNOWN")
    severity     = float(detail.get("severity", 0.0))
    finding_type = detail.get("type", "UNKNOWN")
    account_id   = detail.get("accountId", "UNKNOWN")
    region       = detail.get("region", AWS_REGION)

    # Classify severity
    if severity >= 9.0:
        severity_label = "CRITICAL"
    elif severity >= 7.0:
        severity_label = "HIGH"
    elif severity >= 4.0:
        severity_label = "MEDIUM"
    else:
        severity_label = "LOW"

    # Extract affected resource
    resource = detail.get("resource", {})
    resource_type = resource.get("resourceType", "UNKNOWN")

    response = {
        "finding_id":     finding_id,
        "severity":       severity,
        "severity_label": severity_label,
        "finding_type":   finding_type,
        "resource_type":  resource_type,
        "account_id":     account_id,
        "region":         region,
        "actions_taken":  [],
        "timestamp":      datetime.datetime.utcnow().isoformat() + "Z",
    }

    # ── Action 1: Enrich finding ──────────────────────────────────────────────
    enriched = enrich_finding(detail, resource)
    response["enrichment"] = enriched

    # ── Action 2: Publish alert to SNS ───────────────────────────────────────
    if SNS_NOC_ARN:
        alert_sent = send_noc_alert(severity_label, finding_type, enriched)
        if alert_sent:
            response["actions_taken"].append("sns_alert_sent")

    if SNS_SECURITY_ARN and severity >= 7.0:
        sec_alert_sent = send_security_alert(detail, enriched)
        if sec_alert_sent:
            response["actions_taken"].append("security_alert_sent")

    # ── Action 3: Update Security Hub finding ────────────────────────────────
    sh_updated = update_security_hub(finding_id, account_id, region, severity_label)
    if sh_updated:
        response["actions_taken"].append("securityhub_updated")

    # ── Action 4: Auto-quarantine for CRITICAL findings ───────────────────────
    if severity >= QUARANTINE_SEVERITY_THRESHOLD and resource_type == "Instance":
        instance_id = (resource.get("instanceDetails", {})
                               .get("instanceId", ""))
        if instance_id and QUARANTINE_SG_ID:
            quarantined = quarantine_instance(instance_id)
            if quarantined:
                response["actions_taken"].append(f"quarantine:{instance_id}")

    # ── Action 5: EKS node isolation ─────────────────────────────────────────
    if severity >= QUARANTINE_SEVERITY_THRESHOLD and "Kubernetes" in finding_type:
        pod_info = detail.get("service", {}).get("resourceRole", "")
        logger.warning("[SOAR] Kubernetes threat detected — pod: %s", pod_info)
        response["actions_taken"].append("eks_threat_logged")

    logger.info("SOAR response: %s", json.dumps(response))
    return response


def enrich_finding(detail: dict, resource: dict) -> dict:
    """Enrich with EC2/EKS/IAM context."""
    enrichment = {
        "resource_type": resource.get("resourceType"),
        "tags":          resource.get("instanceDetails", {}).get("tags", []),
    }

    # Add IP/network context
    service = detail.get("service", {})
    action  = service.get("action", {})
    if "networkConnectionAction" in action:
        nc = action["networkConnectionAction"]
        enrichment["remote_ip"]      = nc.get("remoteIpDetails", {}).get("ipAddressV4")
        enrichment["remote_country"] = (nc.get("remoteIpDetails", {})
                                          .get("country", {}).get("countryName"))
        enrichment["remote_port"]    = nc.get("remotePortDetails", {}).get("port")
        enrichment["direction"]      = nc.get("connectionDirection")

    return enrichment


def send_noc_alert(severity_label: str, finding_type: str, enrichment: dict) -> bool:
    try:
        sns = boto3.client("sns", region_name=AWS_REGION)
        sns.publish(
            TopicArn=SNS_NOC_ARN,
            Subject=f"[SOAR/{severity_label}] GuardDuty: {finding_type}",
            Message=json.dumps({
                "severity":    severity_label,
                "type":        finding_type,
                "enrichment":  enrichment,
                "action":      "Review Security Hub and check SOAR logs",
                "timestamp":   datetime.datetime.utcnow().isoformat(),
            }, indent=2),
            MessageAttributes={
                "severity": {"DataType": "String", "StringValue": severity_label},
            },
        )
        return True
    except ClientError as exc:
        logger.error("SNS publish error: %s", exc)
        return False


def send_security_alert(detail: dict, enrichment: dict) -> bool:
    try:
        sns = boto3.client("sns", region_name=AWS_REGION)
        sns.publish(
            TopicArn=SNS_SECURITY_ARN,
            Subject=f"[SECURITY] GuardDuty HIGH/CRITICAL: {detail.get('type')}",
            Message=json.dumps({
                "finding":    detail,
                "enrichment": enrichment,
            }, indent=2),
        )
        return True
    except ClientError as exc:
        logger.error("Security SNS error: %s", exc)
        return False


def update_security_hub(finding_id: str, account_id: str, region: str,
                        severity_label: str) -> bool:
    try:
        sh = boto3.client("securityhub", region_name=AWS_REGION)
        sh.batch_update_findings(
            FindingIdentifiers=[{
                "Id":          finding_id,
                "ProductArn":  f"arn:aws:securityhub:{region}:{account_id}:product/{account_id}/default",
            }],
            Workflow={"Status": "IN_PROGRESS"},
            Note={
                "Text": f"SOAR auto-triaged [{severity_label}] at {datetime.datetime.utcnow().isoformat()}Z",
                "UpdatedBy": "spacenet-soar-lambda",
            },
        )
        return True
    except ClientError as exc:
        logger.error("Security Hub update error: %s", exc)
        return False


def quarantine_instance(instance_id: str) -> bool:
    """Isolate EC2 instance by replacing SGs with quarantine SG."""
    try:
        ec2 = boto3.client("ec2", region_name=AWS_REGION)
        ec2.modify_instance_attribute(
            InstanceId=instance_id,
            Groups=[QUARANTINE_SG_ID],
        )
        logger.warning("[SOAR] QUARANTINED instance %s → SG %s",
                       instance_id, QUARANTINE_SG_ID)
        return True
    except ClientError as exc:
        logger.error("Quarantine failed for %s: %s", instance_id, exc)
        return False
