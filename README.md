# SpaceNet — AWS Ground Station Platform

> Global LEO satellite operations platform built on AWS Ground Station.
> Covers ground segment ingestion, fleet management, data pipeline, and security — from RF antenna to NOC dashboard.

**Organisation:** SpaceNet-IT | **Classification:** ITAR-controlled | **Compliance:** ITAR · EAR · NIST 800-53 · ISO 27001

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Protocol Stack](#protocol-stack)
- [Repository Structure](#repository-structure)
- [Design Documents](#design-documents)
- [Quick Start](#quick-start)
- [Terraform Deployment](#terraform-deployment)
- [Python Microservices](#python-microservices)
- [CI/CD Pipeline](#cicd-pipeline)
- [Security Controls](#security-controls)
- [Cost Estimate](#cost-estimate)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         LEO SATELLITE CONSTELLATION                      │
│              SN-001 · SN-002 · SN-003  (550 km circular orbit)          │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │  RF (S-band TT&C / X-band payload)
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     AWS GROUND STATION (managed)                         │
│    PERTH · SANTIAGO · FAIRBANKS · HAWAII · CAPE TOWN + 8 more sites     │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │  VITA 49 (VRT) over UDP — raw I/Q samples
                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              GROUND SEGMENT VPC  (ECS Fargate Dataflow Endpoint)         │
│   VITA49 Decoder → CCSDS TM Frame Parser → Virtual Channel Router        │
│   VC0=HK  VC1=Stored  VC2=Science  VC3=Fill                             │
└────────┬────────────────┬───────────────────────────┬───────────────────┘
         │                │                           │
         ▼                ▼                           ▼
   Kinesis Streams    IoT Core (MQTT/TLS)       S3 Raw Frames
   (4 streams)        satellite telemetry        WORM / 7yr
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                 FLEET MANAGEMENT VPC  (EKS 1.30 Private)                 │
│                                                                          │
│  contact-scheduler  │  telemetry-ingestor  │  command-dispatcher        │
│  orbit-propagator   │  anomaly-detector    │  fleet-state-manager        │
│  contact-reporter                                                        │
└──────────┬───────────────────────────────────────────┬───────────────────┘
           │                                           │
           ▼                                           ▼
    Timestream + DynamoDB                    Step Functions
    (telemetry time-series)                  (TC dual-operator approval)
           │
           ▼
    IoT TwinMaker + Managed Grafana  (NOC dashboards + digital twin)
```

---

## Protocol Stack

| Layer | Protocol | Standard | Port / Transport |
|---|---|---|---|
| RF digitisation | VITA 49 (VRT) | VITA-49.2 | UDP 55888 |
| Telemetry frames | CCSDS TM Transfer Frames | CCSDS 132.0-B-2 | VITA 49 payload |
| Space packets | CCSDS Space Packets | CCSDS 133.0-B-2 | Inside TM frames |
| File delivery | CFDP | CCSDS 727.0-B-5 | Over TM frames |
| Telecommand uplink | CCSDS TC + CLTU | CCSDS 232.0-B-4 | UDP uplink |
| Cloud streaming | Kinesis Data Streams | AWS | HTTPS/TCP |
| Real-time telemetry | MQTT 3.1.1 / TLS 1.3 | MQTT 3.1.1 | TLS 8883 |
| Raw frame archive | S3 PutObject | AWS | HTTPS |

### VITA 49 Signal Flow

```
Antenna → Demodulator → ADC → VITA 49 context packet (signal metadata)
                            → VITA 49 data packet   (I/Q samples, 32-bit float pairs)
                            → UDP → ECS Fargate Dataflow Endpoint
```

### CCSDS Virtual Channels

| VC ID | Content | Kinesis Stream |
|---|---|---|
| VC0 | Housekeeping (HK) telemetry | `spacenet-telemetry-hk` (10 shards) |
| VC1 | Stored / playback data | `spacenet-telemetry-science` (5 shards) |
| VC2 | Science / payload data | `spacenet-telemetry-science` (5 shards) |
| VC3 | Fill frames | Discarded |

---

## Repository Structure

```
aws-ground-station-poc/
│
├── docs/                              # Design documents (PDF)
│   ├── HLD_AWS_Ground_Station.pdf     # High-Level Design
│   ├── LLD_GS_001_Ground_Segment.pdf  # LLD — Ground Segment
│   ├── LLD_FM_001_Fleet_Management.pdf # LLD — Fleet Management
│   ├── LLD_DP_001_Data_Pipeline.pdf   # LLD — Data Pipeline
│   └── LLD_SEC_001_Security_Identity.pdf # LLD — Security & Identity
│
├── vita49/                            # VITA 49 encoder/decoder + I/Q simulator
├── ccsds/                             # CCSDS TM/TC frame encoder/decoder
├── kinesis/                           # Kinesis producer (local + AWS mode)
├── iot/                               # MQTT publisher (local + AWS IoT Core)
├── docker/                            # Dataflow endpoint container (full pipeline)
├── scripts/                           # POC runner + sample data generator
├── sample-data/                       # Pre-generated JSON samples (all 4 layers)
│
├── src/
│   ├── fleet-management/
│   │   ├── contact-scheduler/         # Pass prediction + Ground Station API
│   │   ├── telemetry-ingestor/        # Kinesis → Timestream writer
│   │   ├── command-dispatcher/        # CCSDS TC encoder + CLTU + Step Functions
│   │   ├── orbit-propagator/          # SGP4 + J2 propagator, ECI → ECEF
│   │   ├── anomaly-detector/          # Threshold rules + SageMaker endpoint
│   │   ├── fleet-state-manager/       # Satellite state machine
│   │   └── contact-reporter/          # Link budget + post-contact reports
│   ├── data-pipeline/
│   │   └── timestream_writer.py       # Kinesis Lambda consumer → Timestream
│   └── security/
│       └── soar-lambdas/
│           └── guardduty_responder.py # GuardDuty SOAR — triage + quarantine
│
├── terraform/
│   ├── modules/
│   │   ├── networking/                # VPC, subnets, NAT, flow logs, endpoints
│   │   ├── ground-station/            # Mission profile, antenna configs
│   │   ├── ecs-dataflow/              # ECR + ECS Fargate + auto-scaling
│   │   ├── kinesis-pipeline/          # Kinesis streams + Firehose + Lambda
│   │   ├── data-storage/              # S3 (WORM) + DynamoDB + Timestream + SQS
│   │   ├── security/                  # KMS + CloudTrail + GuardDuty + Sec Hub
│   │   └── eks-fleet/                 # EKS 1.30 + IRSA + App Mesh + Step Functions
│   ├── environments/
│   │   └── prod/
│   │       ├── security/              # Deploy first — outputs KMS keys + SNS ARNs
│   │       ├── data-pipeline/         # Deploy second — outputs stream + bucket ARNs
│   │       ├── ground-segment/        # Deploy third (parallel with fleet-management)
│   │       └── fleet-management/      # Deploy third (parallel with ground-segment)
│   └── pipelines/
│       └── github-actions.yml         # Full CI/CD pipeline
│
└── requirements.txt
```

---

## Design Documents

All PDFs are in `docs/`. Source Python (reportlab) scripts are co-located.

| Document | Ref | Description |
|---|---|---|
| `HLD_AWS_Ground_Station.pdf` | HLD-001 | 14-section HLD: architecture, account structure, data flows, NFRs, cost (~$89,500/month) |
| `LLD_GS_001_Ground_Segment.pdf` | LLD-GS-001 | ECS Fargate dataflow endpoint, VITA 49 decode, CCSDS VC routing |
| `LLD_FM_001_Fleet_Management.pdf` | LLD-FM-001 | EKS 1.30, 7 microservices, TC approval workflow, satellite state machine |
| `LLD_DP_001_Data_Pipeline.pdf` | LLD-DP-001 | Kinesis streams, Timestream tables, S3 lifecycle, DynamoDB GSIs, OpenSearch |
| `LLD_SEC_001_Security_Identity.pdf` | LLD-SEC-001 | KMS CMKs, CloudTrail WORM, GuardDuty, Security Hub (NIST 800-53), SOAR |

---

## Quick Start

### Prerequisites

```bash
python3 --version   # 3.11+
pip3 install -r requirements.txt
```

### Run the POC (no AWS credentials required)

```bash
# Full end-to-end simulation — all 5 protocol layers
python3 scripts/run_poc.py
```

This simulates a complete ground contact pass:
1. **VITA 49** — generates QPSK I/Q samples and encodes VRT packets
2. **CCSDS** — wraps telemetry in TM Transfer Frames with virtual channel routing
3. **Kinesis** — publishes HK, science, and event records (local simulator)
4. **MQTT** — publishes satellite telemetry topics (local simulator)
5. **Dataflow endpoint** — end-to-end pipeline orchestration

```bash
# Run individual microservices
python3 src/fleet-management/contact-scheduler/main.py
python3 src/fleet-management/orbit-propagator/main.py
python3 src/fleet-management/anomaly-detector/main.py
python3 src/fleet-management/command-dispatcher/main.py
python3 src/fleet-management/fleet-state-manager/main.py
python3 src/fleet-management/contact-reporter/main.py
python3 src/fleet-management/telemetry-ingestor/main.py
```

### Environment variables

All services default to `LOCAL_MODE=true` — no AWS credentials needed.

| Variable | Default | Description |
|---|---|---|
| `LOCAL_MODE` | `true` | Use local simulators instead of AWS SDK |
| `AWS_REGION` | `ap-southeast-2` | AWS deployment region |
| `KINESIS_HK_STREAM` | `spacenet-telemetry-hk` | HK telemetry stream name |
| `TIMESTREAM_DATABASE` | `spacenet-telemetry` | Timestream database |
| `DYNAMODB_SATELLITES_TABLE` | `spacenet-satellites` | Fleet state table |

Set `LOCAL_MODE=false` to connect to real AWS services (requires credentials and deployed infrastructure).

---

## Terraform Deployment

### Prerequisites

- Terraform >= 1.10
- AWS CLI configured with appropriate role
- S3 backend bucket: `spacenet-terraform-state`
- DynamoDB lock table: `spacenet-terraform-locks`

### Deployment order (dependency chain)

```
1. security          ← creates KMS keys, SNS topics, CloudTrail
        ↓
2. data-pipeline     ← creates Kinesis streams, S3 buckets, DynamoDB tables
        ↓
3a. ground-segment   ←─┐
3b. fleet-management ←─┘  (deploy in parallel — both depend on data-pipeline)
```

### Deploy manually

```bash
cd terraform/environments/prod

# 1. Security (always first)
cd security && terraform init && terraform apply && cd ..

# 2. Data Pipeline
cd data-pipeline && terraform init && terraform apply && cd ..

# 3. Ground Segment and Fleet Management (parallel)
cd ground-segment && terraform init && terraform apply &
cd fleet-management && terraform init && terraform apply &
wait
```

### Module summary

| Module | Key Resources |
|---|---|
| `networking` | VPC · 3 private subnets · NAT GW · 11 VPC interface endpoints · Flow Logs |
| `ground-station` | Mission profile · S/X-band antenna configs · Dataflow endpoint group |
| `ecs-dataflow` | ECR (immutable) · ECS Fargate (2 vCPU / 4 GB) · Auto-scaling (CPU 60%, max 10) |
| `kinesis-pipeline` | 4 streams (HK/science/events/raw) · Firehose → S3 · Lambda consumer · CW alarms |
| `data-storage` | 5 S3 buckets (WORM Object Lock) · 5 DynamoDB Global Tables · Timestream · SQS DLQ |
| `security` | 6 KMS CMKs · CloudTrail (7yr COMPLIANCE) · GuardDuty · Security Hub (FSBP + CIS + NIST) · SOAR |
| `eks-fleet` | EKS 1.30 private · 3 node groups · IRSA × 7 · Add-ons · ALB controller · Step Functions TC approval |

---

## Python Microservices

All services in `src/fleet-management/` share a common pattern:

- `LOCAL_MODE=true` — runs fully offline using local simulators
- `LOCAL_MODE=false` — connects to real AWS (Kinesis, Timestream, DynamoDB, Ground Station)
- Structured logging via Python `logging` module
- Dataclasses for all data models
- Full type hints

| Service | Entry Point | Key Dependencies |
|---|---|---|
| `contact-scheduler` | `main.py` | `boto3` groundstation, DynamoDB, EventBridge |
| `telemetry-ingestor` | `main.py` | Kinesis, Timestream, DynamoDB |
| `command-dispatcher` | `main.py` | CCSDS TC/CLTU encoder, Step Functions, DynamoDB |
| `orbit-propagator` | `main.py` | Two-body + J2 propagator, DynamoDB, IoT TwinMaker |
| `anomaly-detector` | `main.py` | Kinesis, SageMaker endpoint, SNS, DynamoDB |
| `fleet-state-manager` | `main.py` | EventBridge consumer, DynamoDB, SNS |
| `contact-reporter` | `main.py` | Friis link budget, Timestream query, S3, DynamoDB |

---

## CI/CD Pipeline

`terraform/pipelines/github-actions.yml` implements a full GitOps pipeline:

```
PR opened
  ├── tfsec (Terraform security scan)
  ├── Checkov (compliance scan — SARIF output)
  ├── terraform validate (all 4 components)
  └── terraform plan (posted as PR comment)

Merge to main
  ├── Deploy: security
  ├── Deploy: data-pipeline
  ├── Deploy: ground-segment  ─┐ (parallel)
  ├── Deploy: fleet-management ─┘
  └── Smoke tests:
        ├── ECS service health (running == desired)
        ├── Kinesis streams ACTIVE
        └── EKS cluster ACTIVE
```

**Required GitHub secrets:**
- `AWS_TERRAFORM_ROLE_ARN` — IAM role with OIDC trust for GitHub Actions

---

## Security Controls

| Control | Implementation |
|---|---|
| Encryption at rest | KMS CMK on all S3, Kinesis, DynamoDB, Timestream, Secrets Manager |
| Encryption in transit | TLS 1.3 enforced on all endpoints; mTLS via App Mesh |
| Zero trust networking | Private VPC only; PrivateLink for all AWS services; no IGW on processing subnets |
| Immutable audit trail | CloudTrail → S3 Object Lock (COMPLIANCE, 7 years) |
| Raw frame archive | S3 Object Lock COMPLIANCE mode, 7 year retention |
| Identity | IAM Identity Center SSO; IRSA for EKS pods; no long-lived credentials |
| TC command security | Dual-operator approval via Step Functions human task; KMS signing key |
| Threat detection | GuardDuty (S3 + K8s + malware) + Security Hub (NIST 800-53, CIS 1.4, FSBP) |
| Automated response | SOAR Lambda: auto-triage, EC2 quarantine, Security Hub update, SNS alert |
| ITAR compliance | SCPs enforcing data residency; Macie classification; geo-restricted APIs |
| IMDSv2 | Enforced on all EC2/EKS nodes (hop limit = 1) |
| Container hardening | `readOnlyRootFilesystem=true`; non-root user (1000:1000); no privilege escalation |

---

## Cost Estimate

| Component | Monthly (USD) |
|---|---|
| AWS Ground Station (100 contacts) | $54,000 |
| EKS cluster (3 regions, HA) | $8,000 |
| Kinesis + IoT Core | $3,500 |
| Timestream (1 TB/month) | $4,000 |
| S3 + DataSync (payload archive) | $2,500 |
| DynamoDB Global Tables | $1,800 |
| IoT TwinMaker + Managed Grafana | $1,200 |
| Direct Connect (2 × 10 Gbps) | $6,000 |
| Security Hub + GuardDuty + KMS | $1,500 |
| RDS Aurora (mission planning) | $1,200 |
| OpenSearch (log analytics) | $2,000 |
| Step Functions + Lambda + ECS | $800 |
| **Total estimate** | **~$86,500/month** |

> Costs vary by constellation size, contact frequency, and data volume. Ground Station contacts represent ~62% of total cost.

---

*SpaceNet-IT · www.spacenet-it.com*
*Architecture aligned with AWS Well-Architected Framework, NIST 800-53 Rev 5, and AWS Ground Station best practices*
