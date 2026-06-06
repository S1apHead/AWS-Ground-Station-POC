"""
anomaly-detector — SpaceNet Fleet Management Microservice
LLD Ref: LLD-FM-001

Responsibilities:
  - Consume HK telemetry from Kinesis stream
  - Apply threshold-based rules (fast, deterministic)
  - Invoke SageMaker anomaly detection endpoint (ML-based)
  - Publish anomaly events to EventBridge + SNS
  - Write anomaly records to DynamoDB anomalies table
"""

import os
import json
import time
import logging
import datetime
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

AWS_REGION        = os.getenv("AWS_REGION", "ap-southeast-2")
KINESIS_HK_STREAM = os.getenv("KINESIS_HK_STREAM", "spacenet-telemetry-hk")
DYNAMODB_ANOMALIES = os.getenv("DYNAMODB_ANOMALIES_TABLE", "spacenet-anomalies")
SM_ENDPOINT       = os.getenv("SAGEMAKER_ENDPOINT", "spacenet-anomaly-detector")
SNS_NOC_TOPIC     = os.getenv("SNS_NOC_TOPIC_ARN", "")
LOCAL_MODE        = os.getenv("LOCAL_MODE", "true").lower() == "true"


@dataclass
class AnomalyEvent:
    anomaly_id:    str
    satellite_id:  str
    subsystem:     str
    parameter:     str
    measured_value: float
    threshold:     float
    severity:      str          # CRITICAL | HIGH | MEDIUM | LOW
    description:   str
    timestamp:     str
    detector:      str          # THRESHOLD | ML | COMBINED
    acknowledged:  bool = False


# ── Threshold Rules ───────────────────────────────────────────────────────────
THRESHOLD_RULES = {
    "power": {
        "bus_voltage_v":         {"low": 24.0, "high": 32.0, "critical_low": 22.0, "critical_high": 34.0},
        "battery_soc_pct":       {"low": 20.0, "high": 100.0, "critical_low": 10.0, "critical_high": 100.0},
        "battery_temp_c":        {"low": -10.0, "high": 45.0, "critical_low": -20.0, "critical_high": 55.0},
        "solar_array_current_a": {"low": 0.0, "high": 8.0, "critical_low": 0.0, "critical_high": 10.0},
    },
    "thermal": {
        "obc_temp_c":      {"low": -10.0, "high": 60.0, "critical_low": -20.0, "critical_high": 70.0},
        "payload_temp_c":  {"low": -5.0,  "high": 45.0, "critical_low": -15.0, "critical_high": 55.0},
    },
    "adcs": {
        "attitude_error_deg":    {"low": 0.0, "high": 1.0, "critical_low": 0.0, "critical_high": 5.0},
        "reaction_wheel_rpm":    {"low": 0.0, "high": 5000.0, "critical_low": 0.0, "critical_high": 6000.0},
        "sun_pointing_err_deg":  {"low": 0.0, "high": 2.0, "critical_low": 0.0, "critical_high": 10.0},
    },
    "comms": {
        "uplink_snr_db":   {"low": 5.0, "high": 40.0, "critical_low": 3.0, "critical_high": 50.0},
        "bit_error_rate":  {"low": 0.0, "high": 1e-5, "critical_low": 0.0, "critical_high": 1e-3},
    },
}


class ThresholdDetector:
    def check(self, subsystem: str, parameter: str, value: float) -> Optional[dict]:
        rules = THRESHOLD_RULES.get(subsystem, {}).get(parameter)
        if not rules:
            return None

        severity = None
        description = None

        if value < rules.get("critical_low", float("-inf")):
            severity    = "CRITICAL"
            description = f"{parameter} critically low: {value:.3f} (limit: {rules['critical_low']})"
            threshold   = rules["critical_low"]
        elif value > rules.get("critical_high", float("inf")):
            severity    = "CRITICAL"
            description = f"{parameter} critically high: {value:.3f} (limit: {rules['critical_high']})"
            threshold   = rules["critical_high"]
        elif value < rules.get("low", float("-inf")):
            severity    = "HIGH"
            description = f"{parameter} below warning: {value:.3f} (limit: {rules['low']})"
            threshold   = rules["low"]
        elif value > rules.get("high", float("inf")):
            severity    = "HIGH"
            description = f"{parameter} above warning: {value:.3f} (limit: {rules['high']})"
            threshold   = rules["high"]

        if severity:
            return {"severity": severity, "description": description, "threshold": threshold}
        return None


class AnomalyDetector:
    def __init__(self):
        self._threshold  = ThresholdDetector()
        self._anomalies: list[AnomalyEvent] = []
        self._local_mode = LOCAL_MODE

        if not LOCAL_MODE:
            import boto3
            self._sm    = boto3.client("sagemaker-runtime", region_name=AWS_REGION)
            self._sns   = boto3.client("sns", region_name=AWS_REGION)
            self._dynamo = boto3.resource("dynamodb", region_name=AWS_REGION)
            self._tbl   = self._dynamo.Table(DYNAMODB_ANOMALIES)

    def process_telemetry(self, satellite_id: str, subsystem: str,
                          parameters: dict, timestamp_ms: int) -> list[AnomalyEvent]:
        found = []
        for param, value in parameters.items():
            try:
                float_val = float(value)
            except (TypeError, ValueError):
                continue

            result = self._threshold.check(subsystem, param, float_val)
            if result:
                import uuid
                event = AnomalyEvent(
                    anomaly_id    = str(uuid.uuid4())[:8],
                    satellite_id  = satellite_id,
                    subsystem     = subsystem,
                    parameter     = param,
                    measured_value = float_val,
                    threshold     = result["threshold"],
                    severity      = result["severity"],
                    description   = result["description"],
                    timestamp     = datetime.datetime.utcfromtimestamp(
                                        timestamp_ms / 1000
                                    ).isoformat() + "Z",
                    detector      = "THRESHOLD",
                )
                found.append(event)
                self._anomalies.append(event)
                logger.warning(
                    "[ANOMALY] %s %s/%s: %s [%s]",
                    satellite_id, subsystem, param,
                    result["description"], result["severity"],
                )

                if not LOCAL_MODE:
                    self._publish(event)

        return found

    def _publish(self, event: AnomalyEvent):
        # DynamoDB
        try:
            self._tbl.put_item(Item={**asdict(event), "acknowledged": False})
        except Exception as exc:
            logger.error("DynamoDB write error: %s", exc)

        # SNS for CRITICAL / HIGH
        if event.severity in ("CRITICAL", "HIGH") and SNS_NOC_TOPIC:
            try:
                self._sns.publish(
                    TopicArn=SNS_NOC_TOPIC,
                    Subject=f"[{event.severity}] {event.satellite_id} — {event.description[:80]}",
                    Message=json.dumps(asdict(event), indent=2),
                    MessageAttributes={
                        "severity":      {"DataType": "String", "StringValue": event.severity},
                        "satellite_id":  {"DataType": "String", "StringValue": event.satellite_id},
                    },
                )
            except Exception as exc:
                logger.error("SNS publish error: %s", exc)

    @property
    def anomaly_count(self) -> int:
        return len(self._anomalies)


# ── Test ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    detector = AnomalyDetector()

    test_telemetry = [
        ("SN-001", "power",   {"bus_voltage_v": 21.0, "battery_soc_pct": 85.0}),
        ("SN-001", "thermal", {"obc_temp_c": 25.0, "payload_temp_c": 18.0}),
        ("SN-001", "adcs",    {"attitude_error_deg": 6.0, "reaction_wheel_rpm": 1500.0}),
        ("SN-002", "power",   {"bus_voltage_v": 28.0, "battery_soc_pct": 5.0}),
        ("SN-002", "comms",   {"uplink_snr_db": 2.0, "bit_error_rate": 5e-3}),
    ]

    all_anomalies: list[AnomalyEvent] = []
    ts = int(time.time() * 1000)
    for sat_id, subsystem, params in test_telemetry:
        anomalies = detector.process_telemetry(sat_id, subsystem, params, ts)
        all_anomalies.extend(anomalies)
        ts += 500

    print(f"\n{'='*60}")
    print(f"SpaceNet Anomaly Detector — {detector.anomaly_count} anomalies detected")
    print(f"{'='*60}")
    for a in all_anomalies:
        print(f"  [{a.severity:8s}] {a.satellite_id} {a.subsystem}/{a.parameter}")
        print(f"             {a.description}")
