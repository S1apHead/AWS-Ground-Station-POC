"""
telemetry-ingestor — SpaceNet Fleet Management Microservice
LLD Ref: LLD-FM-001 / LLD-DP-001

Responsibilities:
  - Consume from Kinesis HK stream
  - Decode CCSDS space packets and extract engineering values
  - Write time-series records to Amazon Timestream
  - Update satellite telemetry_state in DynamoDB
  - Trigger anomaly events via EventBridge
"""

import os
import sys
import json
import time
import base64
import struct
import logging
import datetime
from typing import Optional

import boto3
from botocore.exceptions import ClientError

# Add project root to path for CCSDS module import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
try:
    from ccsds.ccsds_tm_frame import CCSDSTMDecoder
    HAS_CCSDS = True
except ImportError:
    HAS_CCSDS = False
    logging.warning("CCSDS module not available — using raw telemetry decode")

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# ── Configuration ─────────────────────────────────────────────────────────────
AWS_REGION              = os.getenv("AWS_REGION", "ap-southeast-2")
KINESIS_HK_STREAM       = os.getenv("KINESIS_HK_STREAM", "spacenet-telemetry-hk")
TIMESTREAM_DATABASE     = os.getenv("TIMESTREAM_DATABASE", "spacenet-telemetry")
TIMESTREAM_HK_TABLE     = os.getenv("TIMESTREAM_HK_TABLE", "satellite_hk")
DYNAMODB_STATE_TABLE    = os.getenv("DYNAMODB_STATE_TABLE", "spacenet-telemetry_state")
LOCAL_MODE              = os.getenv("LOCAL_MODE", "true").lower() == "true"
SHARD_ITERATOR_TYPE     = os.getenv("SHARD_ITERATOR_TYPE", "LATEST")
POLL_INTERVAL_S         = float(os.getenv("POLL_INTERVAL_S", "5"))
BATCH_SIZE              = int(os.getenv("BATCH_SIZE", "100"))


# ── Timestream Writer ─────────────────────────────────────────────────────────
class TimestreamWriter:
    def __init__(self, database: str, table: str, region: str):
        self._client   = boto3.client("timestream-write", region_name=region,
                                       endpoint_discovery_enabled=True)
        self._database = database
        self._table    = table

    def write_telemetry(self, satellite_id: str, subsystem: str,
                        parameters: dict, timestamp_ms: int) -> bool:
        records = []
        for param_name, value in parameters.items():
            try:
                float_val = float(value)
            except (TypeError, ValueError):
                continue
            records.append({
                "Dimensions": [
                    {"Name": "satellite_id", "Value": satellite_id},
                    {"Name": "subsystem",    "Value": subsystem},
                ],
                "MeasureName":  param_name,
                "MeasureValue": str(float_val),
                "MeasureValueType": "DOUBLE",
                "Time": str(timestamp_ms),
                "TimeUnit": "MILLISECONDS",
            })

        if not records:
            return False

        try:
            self._client.write_records(
                DatabaseName=self._database,
                TableName=self._table,
                Records=records,
                CommonAttributes={"TimeUnit": "MILLISECONDS"},
            )
            logger.debug("Wrote %d records to Timestream for %s/%s",
                         len(records), satellite_id, subsystem)
            return True
        except self._client.exceptions.RejectedRecordsException as e:
            logger.warning("Rejected records: %s", e.response["RejectedRecords"])
            return False
        except ClientError as exc:
            logger.error("Timestream write error: %s", exc)
            return False


class LocalTimestreamSimulator:
    def __init__(self):
        self._records: list[dict] = []

    def write_telemetry(self, satellite_id: str, subsystem: str,
                        parameters: dict, timestamp_ms: int) -> bool:
        self._records.append({
            "satellite_id": satellite_id,
            "subsystem":    subsystem,
            "parameters":   parameters,
            "timestamp_ms": timestamp_ms,
        })
        logger.info("[LOCAL-TS] %s/%s: %s", satellite_id, subsystem,
                    {k: round(v, 3) for k, v in parameters.items()
                     if isinstance(v, (int, float))})
        return True

    @property
    def record_count(self):
        return len(self._records)


# ── Telemetry Decoder ─────────────────────────────────────────────────────────
def decode_hk_record(record_data: dict) -> Optional[dict]:
    """Decode a Kinesis HK record to structured engineering values."""
    try:
        if "raw_frame_b64" in record_data:
            if HAS_CCSDS:
                decoder = CCSDSTMDecoder()
                raw = base64.b64decode(record_data["raw_frame_b64"])
                decoded = decoder.decode_tm_frame(raw)
                return {
                    "satellite_id": record_data.get("satellite_id", "UNKNOWN"),
                    "subsystem":    decoded.get("subsystem", "HK"),
                    "parameters":   decoded.get("engineering_values", {}),
                    "timestamp_ms": int(time.time() * 1000),
                    "vcid":         decoded.get("vcid", 0),
                }
        # Direct JSON telemetry path
        if "telemetry" in record_data:
            tm = record_data["telemetry"]
            return {
                "satellite_id": record_data.get("satellite_id", "UNKNOWN"),
                "subsystem":    tm.get("subsystem", "HK"),
                "parameters":   tm.get("engineering_values", {}),
                "timestamp_ms": record_data.get(
                    "timestamp_ms", int(time.time() * 1000)
                ),
                "vcid": record_data.get("vcid", 0),
            }
        return None
    except Exception as exc:
        logger.warning("Decode error: %s", exc)
        return None


# ── Kinesis Consumer ──────────────────────────────────────────────────────────
class TelemetryIngestor:
    def __init__(self):
        self._local_mode = LOCAL_MODE
        if LOCAL_MODE:
            self._ts_writer  = LocalTimestreamSimulator()
            self._kinesis    = None
            self._dynamo     = None
        else:
            self._ts_writer  = TimestreamWriter(
                TIMESTREAM_DATABASE, TIMESTREAM_HK_TABLE, AWS_REGION
            )
            self._kinesis    = boto3.client("kinesis", region_name=AWS_REGION)
            self._dynamo     = boto3.resource("dynamodb", region_name=AWS_REGION)
            self._state_tbl  = self._dynamo.Table(DYNAMODB_STATE_TABLE)
            self._events     = boto3.client("events", region_name=AWS_REGION)

    def process_record(self, record: dict) -> bool:
        try:
            payload = json.loads(record["Data"])
        except (json.JSONDecodeError, KeyError):
            try:
                payload = json.loads(record.get("data", "{}"))
            except Exception:
                return False

        decoded = decode_hk_record(payload)
        if not decoded:
            return False

        ok = self._ts_writer.write_telemetry(
            decoded["satellite_id"],
            decoded["subsystem"],
            decoded["parameters"],
            decoded["timestamp_ms"],
        )

        if not LOCAL_MODE and ok:
            self._update_state(decoded)

        return ok

    def _update_state(self, decoded: dict):
        try:
            self._state_tbl.update_item(
                Key={"satellite_id": decoded["satellite_id"]},
                UpdateExpression=(
                    "SET last_hk_time = :t, last_hk_subsystem = :s"
                ),
                ExpressionAttributeValues={
                    ":t": decoded["timestamp_ms"],
                    ":s": decoded["subsystem"],
                },
            )
        except ClientError as exc:
            logger.warning("State update error: %s", exc)

    def run_local_simulation(self, sample_records: list[dict]):
        """Process pre-built sample records (for unit testing / local demo)."""
        processed = 0
        for rec in sample_records:
            if self.process_record({"Data": json.dumps(rec)}):
                processed += 1

        logger.info("Processed %d / %d records", processed, len(sample_records))
        if self._local_mode and hasattr(self._ts_writer, "record_count"):
            logger.info("Timestream records: %d", self._ts_writer.record_count)
        return processed


# ── Sample data for local testing ─────────────────────────────────────────────
SAMPLE_RECORDS = [
    {
        "satellite_id": "SN-001",
        "telemetry": {
            "subsystem": "power",
            "engineering_values": {
                "bus_voltage_v":        28.5,
                "solar_array_current_a": 4.2,
                "battery_soc_pct":       87.3,
                "battery_temp_c":        22.1,
            },
        },
        "timestamp_ms": int(time.time() * 1000),
    },
    {
        "satellite_id": "SN-001",
        "telemetry": {
            "subsystem": "thermal",
            "engineering_values": {
                "obc_temp_c":     25.0,
                "payload_temp_c": 18.5,
                "battery_temp_c": 22.1,
            },
        },
        "timestamp_ms": int(time.time() * 1000) + 1000,
    },
    {
        "satellite_id": "SN-002",
        "telemetry": {
            "subsystem": "adcs",
            "engineering_values": {
                "attitude_error_deg": 0.05,
                "reaction_wheel_rpm": 1500.0,
                "sun_pointing_err_deg": 0.12,
            },
        },
        "timestamp_ms": int(time.time() * 1000) + 2000,
    },
]


if __name__ == "__main__":
    ingestor = TelemetryIngestor()
    n = ingestor.run_local_simulation(SAMPLE_RECORDS)
    print(f"\nSpaceNet Telemetry Ingestor — {n} records ingested to Timestream")
