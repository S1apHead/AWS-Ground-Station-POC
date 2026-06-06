"""
command-dispatcher — SpaceNet Fleet Management Microservice
LLD Ref: LLD-FM-001

Responsibilities:
  - Receive TC commands from authenticated API
  - Validate command structure and APID authorisation
  - CFDP protocol encoding for file transfers
  - Submit dual-operator approval via Step Functions
  - Dispatch approved commands to AWS Ground Station uplink during contact
  - Persist command audit trail to DynamoDB (immutable entries)
"""

import os
import sys
import json
import struct
import logging
import datetime
import hashlib
from dataclasses import dataclass, asdict
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

AWS_REGION         = os.getenv("AWS_REGION", "ap-southeast-2")
DYNAMODB_COMMANDS  = os.getenv("DYNAMODB_COMMANDS_TABLE", "spacenet-commands")
SFN_APPROVAL_ARN   = os.getenv("SFN_TC_APPROVAL_ARN", "")
LOCAL_MODE         = os.getenv("LOCAL_MODE", "true").lower() == "true"

# CCSDS TC parameters (CCSDS 232.0-B-4)
SPACECRAFT_ID      = int(os.getenv("SPACECRAFT_ID", "100"))
TC_VCID            = int(os.getenv("TC_VCID", "0"))


@dataclass
class TCCommand:
    command_id:     str
    satellite_id:   str
    apid:           int          # Application Process ID (11-bit)
    sequence_count: int
    payload_hex:    str          # Hex-encoded command payload
    priority:       str          # CRITICAL | HIGH | NORMAL
    operator_id:    str
    timestamp:      str
    status:         str = "PENDING_APPROVAL"
    command_type:   str = "TELECOMMAND"


@dataclass
class CLTUFrame:
    """CCSDS CLTU frame — Communications Link Transmission Unit"""
    cltu_bytes: bytes
    crc: int
    sequence_number: int


# ── CCSDS TC Encoding ─────────────────────────────────────────────────────────
class CCSDSTCEncoder:
    """
    Encodes TC Space Packets per CCSDS 133.0-B-2 and wraps in
    CLTU (Communications Link Transmission Unit) per CCSDS 231.0-B-4.
    """

    # CLTU start/tail sequences
    CLTU_START = bytes([0xEB, 0x90])
    CLTU_TAIL  = bytes([0xC5, 0xC5, 0xC5, 0xC5, 0xC5, 0xC5, 0xC5, 0x79])
    BCH_POLY   = 0xA9  # BCH polynomial for CLTU

    def encode_space_packet(
        self, apid: int, seq_count: int, payload: bytes
    ) -> bytes:
        """Build a CCSDS TC Space Packet header + payload."""
        # Primary header (6 bytes)
        # Packet ID: version=000, type=1 (TC), sec_hdr=0, APID
        packet_id = (0b000 << 13) | (0b1 << 12) | (apid & 0x7FF)
        # Packet Seq Ctrl: grouping=11 (standalone), seq_count
        seq_ctrl  = (0b11 << 14) | (seq_count & 0x3FFF)
        data_len  = len(payload) - 1  # per CCSDS, length field = bytes - 1

        header = struct.pack(">HHH", packet_id, seq_ctrl, data_len)
        return header + payload

    def bch_encode_byte(self, data: int, codeword: int) -> int:
        """BCH code word computation for CLTU codeblock."""
        for _ in range(8):
            if (codeword ^ data) & 0x80:
                codeword = ((codeword << 1) & 0xFF) ^ self.BCH_POLY
            else:
                codeword = (codeword << 1) & 0xFF
            data = (data << 1) & 0xFF
        return codeword

    def encode_cltu(self, tc_packet: bytes) -> CLTUFrame:
        """Wrap TC packet in CLTU for transmission."""
        # Pad to multiple of 7 bytes (CLTU codeblock size)
        padded = tc_packet
        while len(padded) % 7 != 0:
            padded += b"\x55"  # idle pattern

        # Build codeblocks with BCH parity
        codeblocks = bytearray()
        for i in range(0, len(padded), 7):
            block = padded[i:i+7]
            parity = 0
            for byte in block:
                parity = self.bch_encode_byte(byte, parity)
            codeblocks.extend(block + bytes([parity]))

        cltu_bytes = self.CLTU_START + bytes(codeblocks) + self.CLTU_TAIL
        crc = sum(cltu_bytes) & 0xFFFF  # simplified CRC for audit

        import random
        return CLTUFrame(cltu_bytes=cltu_bytes, crc=crc,
                         sequence_number=random.randint(1, 65535))


# ── Command Dispatcher ────────────────────────────────────────────────────────
class CommandDispatcher:
    def __init__(self):
        self._encoder   = CCSDSTCEncoder()
        self._local_mode = LOCAL_MODE

        if not LOCAL_MODE:
            self._dynamo  = boto3.resource("dynamodb", region_name=AWS_REGION)
            self._tbl     = self._dynamo.Table(DYNAMODB_COMMANDS)
            self._sfn     = boto3.client("stepfunctions", region_name=AWS_REGION)

        # APID whitelist (command validation)
        self._valid_apids = {
            0x01: "SafeMode",
            0x02: "NominalMode",
            0x10: "AttitudeCommand",
            0x11: "ManoeuvreEnable",
            0x20: "PayloadPower",
            0x21: "PayloadMode",
            0x30: "GroundContactConfig",
            0xFF: "Ping",
        }

    def submit_command(self, cmd: TCCommand) -> dict:
        """Validate, encode, and submit command for approval."""
        import uuid

        # 1. Validate APID
        if cmd.apid not in self._valid_apids:
            return {"status": "REJECTED", "reason": f"Invalid APID 0x{cmd.apid:04X}"}

        # 2. Encode TC packet
        payload = bytes.fromhex(cmd.payload_hex.replace("0x", ""))
        tc_packet = self._encoder.encode_space_packet(
            cmd.apid, cmd.sequence_count, payload
        )
        cltu = self._encoder.encode_cltu(tc_packet)

        # 3. Compute command digest for audit
        digest = hashlib.sha256(cltu.cltu_bytes).hexdigest()
        cmd.command_id = cmd.command_id or str(uuid.uuid4())[:8]

        result = {
            "command_id":     cmd.command_id,
            "satellite_id":   cmd.satellite_id,
            "apid":           f"0x{cmd.apid:04X}",
            "apid_name":      self._valid_apids[cmd.apid],
            "cltu_size_bytes": len(cltu.cltu_bytes),
            "cltu_crc":       f"0x{cltu.crc:04X}",
            "digest_sha256":  digest,
            "status":         "PENDING_APPROVAL",
        }

        if LOCAL_MODE:
            logger.info("[LOCAL-TC] Command submitted:")
            logger.info("  ID:      %s", cmd.command_id)
            logger.info("  APID:    0x%04X (%s)", cmd.apid, self._valid_apids[cmd.apid])
            logger.info("  CLTU:    %d bytes", len(cltu.cltu_bytes))
            logger.info("  Digest:  %s", digest[:16] + "...")
        else:
            # Persist to DynamoDB
            self._tbl.put_item(Item={
                **asdict(cmd),
                "cltu_b64":   cltu.cltu_bytes.hex(),
                "digest":     digest,
                "ttl":        int(datetime.datetime.utcnow().timestamp()) + 86400 * 365,
            })
            # Submit to Step Functions for dual-operator approval
            self._sfn.start_execution(
                stateMachineArn=SFN_APPROVAL_ARN,
                name=f"tc-{cmd.command_id}",
                input=json.dumps({
                    "command": asdict(cmd),
                    "cltu_hex": cltu.cltu_bytes.hex(),
                    "digest": digest,
                }),
            )
            result["sfn_execution"] = f"tc-{cmd.command_id}"

        return result


# ── Test ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    dispatcher = CommandDispatcher()

    cmds = [
        TCCommand(
            command_id="CMD-001",
            satellite_id="SN-001",
            apid=0xFF,
            sequence_count=1,
            payload_hex="DEADBEEF",
            priority="NORMAL",
            operator_id="ops-alice",
            timestamp=datetime.datetime.utcnow().isoformat(),
        ),
        TCCommand(
            command_id="CMD-002",
            satellite_id="SN-001",
            apid=0x10,
            sequence_count=2,
            payload_hex="0000803F0000003F0000803F",  # attitude quaternion
            priority="HIGH",
            operator_id="ops-bob",
            timestamp=datetime.datetime.utcnow().isoformat(),
        ),
    ]

    print(f"\n{'='*60}")
    print("SpaceNet Command Dispatcher")
    print(f"{'='*60}")
    for cmd in cmds:
        result = dispatcher.submit_command(cmd)
        print(f"\n  {result['apid_name']} ({result['apid']})")
        print(f"  CLTU:   {result['cltu_size_bytes']} bytes")
        print(f"  CRC:    {result['cltu_crc']}")
        print(f"  Status: {result['status']}")
