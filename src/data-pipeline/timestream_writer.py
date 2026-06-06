"""
timestream-writer — SpaceNet Data Pipeline
LLD Ref: LLD-DP-001

Kinesis Enhanced Fan-Out consumer that writes HK telemetry to Timestream.
Handles backpressure, batching, and rejected records.
"""

import os
import json
import time
import logging
import datetime
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

AWS_REGION          = os.getenv("AWS_REGION", "ap-southeast-2")
KINESIS_HK_STREAM   = os.getenv("KINESIS_HK_STREAM", "spacenet-telemetry-hk")
TIMESTREAM_DATABASE = os.getenv("TIMESTREAM_DATABASE", "spacenet-telemetry")
TIMESTREAM_HK_TABLE = os.getenv("TIMESTREAM_HK_TABLE", "satellite_hk")
LOCAL_MODE          = os.getenv("LOCAL_MODE", "true").lower() == "true"
MAX_BATCH_RECORDS   = 100   # Timestream max records per write_records call
FLUSH_INTERVAL_S    = 5


class TimestreamBatchWriter:
    """Batches telemetry records and flushes to Timestream."""

    def __init__(self, database: str, table: str, region: str, local: bool = True):
        self._database = database
        self._table    = table
        self._local    = local
        self._buffer: list[dict] = []
        self._flushed  = 0
        self._rejected = 0

        if not local:
            self._client = boto3.client(
                "timestream-write",
                region_name=region,
                endpoint_discovery_enabled=True,
            )

    def add_telemetry(self, satellite_id: str, subsystem: str,
                      param: str, value: float, timestamp_ms: int):
        self._buffer.append({
            "Dimensions": [
                {"Name": "satellite_id", "Value": satellite_id},
                {"Name": "subsystem",    "Value": subsystem},
            ],
            "MeasureName":      param,
            "MeasureValue":     str(value),
            "MeasureValueType": "DOUBLE",
            "Time":             str(timestamp_ms),
            "TimeUnit":         "MILLISECONDS",
        })
        if len(self._buffer) >= MAX_BATCH_RECORDS:
            self.flush()

    def flush(self) -> int:
        if not self._buffer:
            return 0

        batch = self._buffer[:MAX_BATCH_RECORDS]
        self._buffer = self._buffer[MAX_BATCH_RECORDS:]

        if self._local:
            logger.info("[LOCAL-TS] Flush %d records → %s.%s",
                        len(batch), self._database, self._table)
            self._flushed += len(batch)
            return len(batch)

        try:
            self._client.write_records(
                DatabaseName=self._database,
                TableName=self._table,
                Records=batch,
            )
            self._flushed += len(batch)
            return len(batch)
        except self._client.exceptions.RejectedRecordsException as e:
            rejected = e.response["RejectedRecords"]
            self._rejected += len(rejected)
            logger.warning("Rejected %d records: %s", len(rejected), rejected[:2])
            return len(batch) - len(rejected)
        except ClientError as exc:
            logger.error("Timestream write error: %s", exc)
            return 0

    @property
    def stats(self) -> dict:
        return {"flushed": self._flushed, "rejected": self._rejected,
                "buffered": len(self._buffer)}


def process_kinesis_record(record: dict, writer: TimestreamBatchWriter) -> int:
    """Extract telemetry from a Kinesis record and buffer to Timestream."""
    written = 0
    try:
        payload = json.loads(record.get("Data", record.get("data", "{}")))
        satellite_id = payload.get("satellite_id", "UNKNOWN")
        tm = payload.get("telemetry", {})
        subsystem = tm.get("subsystem", "hk")
        ev = tm.get("engineering_values", {})
        ts_ms = payload.get("timestamp_ms", int(time.time() * 1000))

        for param, value in ev.items():
            try:
                writer.add_telemetry(satellite_id, subsystem, param,
                                     float(value), ts_ms)
                written += 1
            except (TypeError, ValueError):
                pass
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("Record decode error: %s", exc)

    return written


# ── Lambda handler (Kinesis event source mapping) ─────────────────────────────
def lambda_handler(event, context):
    """AWS Lambda entry point for Kinesis event source mapping."""
    writer = TimestreamBatchWriter(
        TIMESTREAM_DATABASE, TIMESTREAM_HK_TABLE, AWS_REGION, local=LOCAL_MODE
    )

    total_written = 0
    for record in event.get("Records", []):
        total_written += process_kinesis_record(record, writer)

    writer.flush()
    logger.info("Processed %d parameters, stats: %s", total_written, writer.stats)
    return {"statusCode": 200, "body": writer.stats}


if __name__ == "__main__":
    # Local test with sample Kinesis-like records
    sample_event = {
        "Records": [
            {
                "Data": json.dumps({
                    "satellite_id": "SN-001",
                    "telemetry": {
                        "subsystem": "power",
                        "engineering_values": {
                            "bus_voltage_v": 28.4,
                            "battery_soc_pct": 87.1,
                        },
                    },
                    "timestamp_ms": int(time.time() * 1000),
                })
            },
            {
                "Data": json.dumps({
                    "satellite_id": "SN-002",
                    "telemetry": {
                        "subsystem": "thermal",
                        "engineering_values": {
                            "obc_temp_c": 24.5,
                            "payload_temp_c": 19.2,
                        },
                    },
                    "timestamp_ms": int(time.time() * 1000) + 1000,
                })
            },
        ]
    }

    result = lambda_handler(sample_event, None)
    print(f"\nTimestream Writer result: {result}")
