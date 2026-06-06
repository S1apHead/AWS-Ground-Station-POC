# AWS Ground Station — POC

## Protocol Stack

```
SATELLITE
   │
   │  RF (S-band / X-band / Ka-band)
   ▼
AWS GROUND STATION ANTENNA
   │
   │  VITA 49 (VRT) packets over UDP  ← raw digitised IF samples
   ▼
DATAFLOW ENDPOINT (EC2 / ECS in customer VPC)
   │
   │  CCSDS TM Transfer Frames  ← decoded telemetry frames
   ▼
FRAME PROCESSOR (Lambda / ECS)
   │
   ├──► Kinesis Data Streams  ← engineering telemetry (JSON)
   ├──► IoT Core (MQTT/TLS)   ← real-time device telemetry
   └──► S3                    ← raw frame archive
        │
        ▼
     Timestream / OpenSearch / TwinMaker
```

## Protocol Summary

| Layer | Protocol | Standard | Transport |
|-------|----------|----------|-----------|
| RF digitisation | VITA 49 (VRT) | VITA-49.2 | UDP |
| Telemetry frames | CCSDS TM | CCSDS 132.0-B-2 | UDP/VITA49 payload |
| File delivery | CFDP | CCSDS 727.0-B-5 | Over TM frames |
| Telecommand | CCSDS TC | CCSDS 232.0-B-4 | UDP uplink |
| Cloud streaming | Kinesis KPL | AWS | TCP/HTTPS |
| Device telemetry | MQTT/TLS | MQTT 3.1.1 | TLS 1.3 |
| Bulk archive | S3 PutObject | AWS | HTTPS |

## POC Components

- `vita49/`     — VITA 49 packet generator + parser
- `ccsds/`      — CCSDS TM/TC frame encoder/decoder
- `kinesis/`    — Kinesis producer (streams decoded telemetry)
- `iot/`        — IoT Core MQTT publisher
- `lambda/`     — Lambda frame processor function
- `docker/`     — Dataflow endpoint container
- `scripts/`    — Run POC end-to-end
- `sample-data/`— Pre-generated sample packets and frames

## Quick Start

```bash
# Install dependencies
pip3 install -r requirements.txt

# Generate sample VITA 49 + CCSDS data
python3 scripts/generate_sample_data.py

# Run local UDP dataflow endpoint simulation
python3 docker/dataflow_endpoint.py

# Stream sample data
python3 scripts/run_poc.py
```
