"""
AWS IoT Core — MQTT Telemetry Publisher
-----------------------------------------
Publishes real-time satellite telemetry to AWS IoT Core via MQTT/TLS.
IoT Core acts as the device registry and pub/sub broker.

Topic Structure:
  spacenet/satellites/{satellite_id}/telemetry/hk       ← Housekeeping
  spacenet/satellites/{satellite_id}/telemetry/adcs     ← Attitude
  spacenet/satellites/{satellite_id}/telemetry/power    ← Power
  spacenet/satellites/{satellite_id}/events/contact     ← Contact events
  spacenet/satellites/{satellite_id}/events/anomaly     ← Anomalies
  spacenet/satellites/{satellite_id}/commands/response  ← TC acknowledgements
  spacenet/ground-stations/{gs_id}/status               ← Ground station status

IoT Core Rules Engine routes messages to:
  → Timestream (time-series storage)
  → Lambda (processing / alerting)
  → IoT TwinMaker (digital twin updates)
  → Kinesis (fan-out to other consumers)

Local POC: uses paho-mqtt to localhost broker (mosquitto)
AWS POC:   swap broker/port/certs for IoT Core endpoint
"""

import json
import time
import random
import threading
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────
@dataclass
class IoTConfig:
    # Local (mosquitto) for POC
    broker_host:   str = "localhost"
    broker_port:   int = 1883
    client_id:     str = "spacenet-ground-processor-001"
    use_tls:       bool = False  # Set True + provide certs for AWS IoT Core
    ca_cert:       Optional[str] = None   # AWS IoT Core root CA
    client_cert:   Optional[str] = None   # Device certificate
    private_key:   Optional[str] = None   # Device private key

    # AWS IoT Core endpoint (replace for real deployment)
    # broker_host = "xxxxxxxxx.iot.ap-southeast-2.amazonaws.com"
    # broker_port = 8883
    # use_tls     = True


# ── Local simulator (no broker required for POC demo) ─────────────────────────
class LocalMQTTSimulator:
    """Simulates MQTT publish without a broker — for standalone POC demo"""

    def __init__(self, client_id: str):
        self.client_id     = client_id
        self.connected     = True
        self.messages_sent = 0
        self.total_bytes   = 0
        self._subscriptions = {}
        self._log = []

    def publish(self, topic: str, payload: str, qos: int = 1) -> dict:
        self.messages_sent += 1
        self.total_bytes   += len(payload.encode("utf-8"))
        entry = {
            "topic":     topic,
            "qos":       qos,
            "bytes":     len(payload.encode("utf-8")),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload":   json.loads(payload),
        }
        self._log.append(entry)
        # Trigger any local subscriptions
        for sub_topic, callback in self._subscriptions.items():
            if self._topic_matches(sub_topic, topic):
                callback(topic, json.loads(payload))
        return {"mid": self.messages_sent, "rc": 0}

    def subscribe(self, topic: str, callback):
        self._subscriptions[topic] = callback

    def _topic_matches(self, pattern: str, topic: str) -> bool:
        p_parts = pattern.split("/")
        t_parts = topic.split("/")
        if len(p_parts) != len(t_parts) and "#" not in p_parts:
            return False
        for p, t in zip(p_parts, t_parts):
            if p == "#":
                return True
            if p != "+" and p != t:
                return False
        return True

    def get_stats(self) -> dict:
        return {
            "client_id":     self.client_id,
            "connected":     self.connected,
            "messages_sent": self.messages_sent,
            "total_bytes":   self.total_bytes,
        }


# ── MQTT Publisher ────────────────────────────────────────────────────────────
class SatelliteMQTTPublisher:

    def __init__(self, satellite_id: str, config: IoTConfig,
                 local_mode: bool = True):
        self.satellite_id = satellite_id
        self.config       = config
        self.local_mode   = local_mode

        if local_mode:
            self.client = LocalMQTTSimulator(config.client_id)
        else:
            import paho.mqtt.client as mqtt
            self.client = mqtt.Client(client_id=config.client_id,
                                      protocol=mqtt.MQTTv311)
            if config.use_tls:
                self.client.tls_set(
                    ca_certs    = config.ca_cert,
                    certfile    = config.client_cert,
                    keyfile     = config.private_key,
                )
            self.client.connect(config.broker_host, config.broker_port, 60)
            self.client.loop_start()

    def _base_topic(self) -> str:
        return f"spacenet/satellites/{self.satellite_id}"

    def publish_hk(self, telemetry: dict) -> dict:
        topic   = f"{self._base_topic()}/telemetry/hk"
        payload = json.dumps({
            "schema":    "hk_v1",
            "ts":        int(time.time() * 1000),
            "satellite": self.satellite_id,
            **{k: v for k, v in telemetry.items()
               if not k.startswith("orb_") and k != "satellite_id"},
        })
        return self.client.publish(topic, payload, qos=1)

    def publish_adcs(self, telemetry: dict) -> dict:
        topic   = f"{self._base_topic()}/telemetry/adcs"
        payload = json.dumps({
            "schema":    "adcs_v1",
            "ts":        int(time.time() * 1000),
            "satellite": self.satellite_id,
            "mode":      telemetry.get("adc_mode"),
            "roll_deg":  telemetry.get("adc_roll_deg"),
            "pitch_deg": telemetry.get("adc_pitch_deg"),
            "yaw_deg":   telemetry.get("adc_yaw_deg"),
        })
        return self.client.publish(topic, payload, qos=1)

    def publish_power(self, telemetry: dict) -> dict:
        topic   = f"{self._base_topic()}/telemetry/power"
        payload = json.dumps({
            "schema":        "power_v1",
            "ts":            int(time.time() * 1000),
            "satellite":     self.satellite_id,
            "bus_voltage_v": telemetry.get("pwr_bus_voltage_v"),
            "bus_current_a": telemetry.get("pwr_bus_current_a"),
            "battery_soc":   telemetry.get("pwr_battery_soc_pct"),
            "solar_power_w": telemetry.get("pwr_solar_power_w"),
            "in_eclipse":    telemetry.get("pwr_in_eclipse"),
        })
        return self.client.publish(topic, payload, qos=1)

    def publish_orbital(self, telemetry: dict) -> dict:
        topic   = f"{self._base_topic()}/telemetry/orbital"
        payload = json.dumps({
            "schema":       "orbital_v1",
            "ts":           int(time.time() * 1000),
            "satellite":    self.satellite_id,
            "altitude_km":  telemetry.get("orb_altitude_km"),
            "velocity_kms": telemetry.get("orb_velocity_km_s"),
            "latitude_deg": telemetry.get("orb_latitude_deg"),
            "longitude_deg":telemetry.get("orb_longitude_deg"),
        })
        return self.client.publish(topic, payload, qos=1)

    def publish_anomaly(self, parameter: str, value: float,
                         threshold: float, severity: str = "WARNING") -> dict:
        topic   = f"{self._base_topic()}/events/anomaly"
        payload = json.dumps({
            "schema":     "anomaly_v1",
            "ts":         int(time.time() * 1000),
            "satellite":  self.satellite_id,
            "severity":   severity,
            "parameter":  parameter,
            "value":      value,
            "threshold":  threshold,
            "alert_id":   f"ALT-{int(time.time()*1000)}",
        })
        return self.client.publish(topic, payload, qos=1)

    def publish_tc_response(self, command_id: str, apid: str,
                             status: str, detail: str = "") -> dict:
        topic   = f"{self._base_topic()}/commands/response"
        payload = json.dumps({
            "schema":     "tc_response_v1",
            "ts":         int(time.time() * 1000),
            "satellite":  self.satellite_id,
            "command_id": command_id,
            "apid":       apid,
            "status":     status,  # "ACK" | "NAK" | "EXECUTED" | "FAILED"
            "detail":     detail,
        })
        return self.client.publish(topic, payload, qos=1)

    def get_stats(self) -> dict:
        return self.client.get_stats()


# ── Demo ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sys.path.append("..")
    from ccsds.ccsds_tm_frame import SatelliteTelemetrySimulator

    from rich.console import Console
    from rich.table import Table as RTable
    from rich import box

    console = Console()
    console.print("\n[bold cyan]AWS IoT Core — MQTT Telemetry Publisher POC[/bold cyan]")
    console.print("[dim]MQTT 3.1.1 | QoS 1 | Topic hierarchy simulation[/dim]\n")

    config    = IoTConfig()
    sat_sim   = SatelliteTelemetrySimulator(satellite_name="SPACENET-1A")
    publisher = SatelliteMQTTPublisher("SPACENET-1A", config, local_mode=True)

    # Subscribe to anomaly events (local simulation)
    alerts_received = []
    def on_anomaly(topic, payload):
        alerts_received.append(payload)
        console.print(f"  [red]🚨 ANOMALY received on [{topic}]: {payload['parameter']} = {payload['value']}[/red]")

    publisher.client.subscribe("spacenet/satellites/SPACENET-1A/events/anomaly", on_anomaly)

    console.print("[yellow]Publishing telemetry across all topics...[/yellow]\n")

    topics_published = {}
    for i in range(10):
        hk = sat_sim.generate_hk_telemetry()

        r1 = publisher.publish_hk(hk)
        r2 = publisher.publish_adcs(hk)
        r3 = publisher.publish_power(hk)
        r4 = publisher.publish_orbital(hk)

        # Inject anomaly at frame 5
        if i == 5:
            hk["thm_battery_temp_c"] = 53.8
            publisher.publish_anomaly("thm_battery_temp_c", 53.8, 45.0, "WARNING")

        time.sleep(0.05)

    # TC response simulation
    publisher.publish_tc_response("CMD-001", "0x180", "EXECUTED", "Safe mode entered")

    # Show published topics
    console.print("\n[bold yellow]► Published MQTT Topics[/bold yellow]")
    topic_counts = {}
    for log in publisher.client._log:
        t = log["topic"]
        topic_counts[t] = topic_counts.get(t, 0) + 1

    t = RTable(box=box.ROUNDED, header_style="bold blue")
    t.add_column("Topic", style="cyan")
    t.add_column("Messages", justify="right", style="green")
    t.add_column("Last Payload Keys", style="dim")
    for topic_pub, count in topic_counts.items():
        last = next((l for l in reversed(publisher.client._log)
                     if l["topic"] == topic_pub), None)
        keys = ", ".join(list(last["payload"].keys())[:5]) + "..." if last else ""
        t.add_row(topic_pub, str(count), keys)
    console.print(t)

    # Sample payload
    console.print("\n[bold yellow]► Sample HK Payload (MQTT)[/bold yellow]")
    hk_records = [l for l in publisher.client._log
                  if "telemetry/hk" in l["topic"]]
    if hk_records:
        console.print_json(json.dumps(hk_records[0]["payload"], indent=2))

    # Stats
    console.print("\n[bold yellow]► Publisher Statistics[/bold yellow]")
    stats = publisher.get_stats()
    t2 = RTable(box=box.SIMPLE, show_header=False)
    t2.add_column("K", style="cyan")
    t2.add_column("V")
    for k, v in stats.items():
        t2.add_row(k, str(v))
    console.print(t2)
