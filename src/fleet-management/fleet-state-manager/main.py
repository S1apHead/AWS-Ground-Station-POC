"""
fleet-state-manager — SpaceNet Fleet Management Microservice
LLD Ref: LLD-FM-001

Satellite State Machine:
  NOMINAL → DEGRADED → SAFE_MODE → EMERGENCY
  Any state → NOMINAL (on recovery)

Responsibilities:
  - Consume anomaly and telemetry events from EventBridge
  - Evaluate state transition rules for each satellite
  - Update DynamoDB satellites table with current state
  - Publish state change events to SNS NOC topic
  - Enforce command holds in SAFE_MODE and EMERGENCY states
"""

import os
import json
import logging
import datetime
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

AWS_REGION        = os.getenv("AWS_REGION", "ap-southeast-2")
DYNAMODB_SATS     = os.getenv("DYNAMODB_SATELLITES_TABLE", "spacenet-satellites")
SNS_NOC_TOPIC     = os.getenv("SNS_NOC_TOPIC_ARN", "")
LOCAL_MODE        = os.getenv("LOCAL_MODE", "true").lower() == "true"


class SatelliteState(str, Enum):
    NOMINAL    = "NOMINAL"
    DEGRADED   = "DEGRADED"
    SAFE_MODE  = "SAFE_MODE"
    EMERGENCY  = "EMERGENCY"


# State transition rules
# Maps (current_state, event_type) → new_state
TRANSITION_TABLE = {
    (SatelliteState.NOMINAL,   "anomaly.HIGH"):              SatelliteState.DEGRADED,
    (SatelliteState.NOMINAL,   "anomaly.CRITICAL"):          SatelliteState.SAFE_MODE,
    (SatelliteState.NOMINAL,   "contact.lost"):              SatelliteState.DEGRADED,
    (SatelliteState.DEGRADED,  "anomaly.CRITICAL"):          SatelliteState.SAFE_MODE,
    (SatelliteState.DEGRADED,  "anomaly.resolved"):          SatelliteState.NOMINAL,
    (SatelliteState.DEGRADED,  "contact.restored"):          SatelliteState.NOMINAL,
    (SatelliteState.SAFE_MODE, "anomaly.CRITICAL"):          SatelliteState.EMERGENCY,
    (SatelliteState.SAFE_MODE, "operator.recovery_command"): SatelliteState.DEGRADED,
    (SatelliteState.EMERGENCY, "operator.recovery_command"): SatelliteState.SAFE_MODE,
}

# Command hold states — no TC commands allowed
COMMAND_HOLD_STATES = {SatelliteState.SAFE_MODE, SatelliteState.EMERGENCY}


@dataclass
class StateTransition:
    satellite_id:  str
    from_state:    str
    to_state:      str
    event_type:    str
    timestamp:     str
    triggered_by:  str
    command_hold:  bool


class FleetStateManager:
    def __init__(self):
        self._states: dict[str, SatelliteState] = {}
        self._history: list[StateTransition] = []
        self._local_mode = LOCAL_MODE

        if not LOCAL_MODE:
            import boto3
            self._dynamo = boto3.resource("dynamodb", region_name=AWS_REGION)
            self._tbl    = self._dynamo.Table(DYNAMODB_SATS)
            self._sns    = boto3.client("sns", region_name=AWS_REGION)

    def get_state(self, satellite_id: str) -> SatelliteState:
        return self._states.get(satellite_id, SatelliteState.NOMINAL)

    def process_event(self, satellite_id: str, event_type: str,
                      triggered_by: str = "system") -> Optional[StateTransition]:
        """Evaluate state transition for incoming event."""
        current = self.get_state(satellite_id)
        new_state = TRANSITION_TABLE.get((current, event_type))

        if new_state is None:
            logger.debug("No transition for %s in state %s on event %s",
                         satellite_id, current, event_type)
            return None

        transition = StateTransition(
            satellite_id = satellite_id,
            from_state   = current.value,
            to_state     = new_state.value,
            event_type   = event_type,
            timestamp    = datetime.datetime.utcnow().isoformat() + "Z",
            triggered_by = triggered_by,
            command_hold = new_state in COMMAND_HOLD_STATES,
        )

        self._states[satellite_id] = new_state
        self._history.append(transition)

        logger.warning(
            "[STATE] %s: %s → %s (event=%s, cmd_hold=%s)",
            satellite_id, current.value, new_state.value,
            event_type, transition.command_hold,
        )

        if not LOCAL_MODE:
            self._persist(satellite_id, new_state, transition)
            self._notify(transition)

        return transition

    def is_command_hold(self, satellite_id: str) -> bool:
        return self.get_state(satellite_id) in COMMAND_HOLD_STATES

    def _persist(self, satellite_id: str, new_state: SatelliteState,
                 transition: StateTransition):
        try:
            self._tbl.update_item(
                Key={"satellite_id": satellite_id},
                UpdateExpression=(
                    "SET current_state = :s, last_transition = :t, "
                    "command_hold = :c"
                ),
                ExpressionAttributeValues={
                    ":s": new_state.value,
                    ":t": transition.timestamp,
                    ":c": transition.command_hold,
                },
            )
        except Exception as exc:
            logger.error("DynamoDB update error: %s", exc)

    def _notify(self, transition: StateTransition):
        if SNS_NOC_TOPIC and transition.to_state in ("SAFE_MODE", "EMERGENCY"):
            try:
                import boto3
                sns = boto3.client("sns", region_name=AWS_REGION)
                sns.publish(
                    TopicArn=SNS_NOC_TOPIC,
                    Subject=f"[{transition.to_state}] {transition.satellite_id} state change",
                    Message=json.dumps(asdict(transition), indent=2),
                )
            except Exception as exc:
                logger.error("SNS notify error: %s", exc)

    def print_fleet_status(self):
        print(f"\n{'='*50}")
        print("Fleet State Summary")
        print(f"{'='*50}")
        for sat_id, state in sorted(self._states.items()):
            hold_str = " ⚠ CMD HOLD" if state in COMMAND_HOLD_STATES else ""
            print(f"  {sat_id:12s}  {state.value:12s}{hold_str}")


# ── Test ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mgr = FleetStateManager()

    # Simulate event sequence
    events = [
        ("SN-001", "anomaly.HIGH",              "anomaly-detector"),
        ("SN-001", "anomaly.CRITICAL",           "anomaly-detector"),
        ("SN-002", "contact.lost",               "contact-scheduler"),
        ("SN-002", "anomaly.CRITICAL",           "anomaly-detector"),
        ("SN-001", "operator.recovery_command",  "ops-alice"),
        ("SN-002", "operator.recovery_command",  "ops-bob"),
        ("SN-001", "anomaly.resolved",           "anomaly-detector"),
    ]

    transitions = []
    for sat_id, event_type, triggered_by in events:
        t = mgr.process_event(sat_id, event_type, triggered_by)
        if t:
            transitions.append(t)

    mgr.print_fleet_status()

    print(f"\n  {len(transitions)} transitions recorded")
    print(f"\n  Command holds active:")
    for sat_id in ["SN-001", "SN-002"]:
        hold = mgr.is_command_hold(sat_id)
        print(f"    {sat_id}: {'HOLD' if hold else 'CLEAR'}")
