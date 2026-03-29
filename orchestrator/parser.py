"""
parse_message: parses raw JSON bytes passed through from the gateway.

The gateway does zero transformation — it serializes the POST body and writes it directly to the stream.
The parser is therefore responsible for extracting type / bug_id from the payload
and constructing the corresponding event object.

Supported message formats (used by both integration tests and real callers):

  bug_reported:
    {"type": "bug_reported", "bug_id": "BUG-123", ...}

  bug_fix_validation_status:
    {"type": "bug_fix_validation_status", "bug_id": "BUG-123", "status": "passed", ...}

Extra fields are preserved in event.raw; the parser imposes no restrictions.
"""
import json
from typing import Union
import logging
import re

from .models import BugReportedEvent, ValidationStatusEvent, OtherEvent

ParsedEvent = Union[BugReportedEvent, ValidationStatusEvent, OtherEvent]

logger = logging.getLogger(__name__)

class ParseError(Exception):
    pass

# f"auto/bug_{bug_id}-patch_{branch_id}"
def parse_branch(branch_name: str) -> bool:
    # branch_name = "auto/fix-{bug_id}-{branch_id}"
    m = re.match(r"^auto/bug_(\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2}_\d{1})-patch_(\d{2}_\d{2}_\d{2}_\d{1})$", branch_name)
    return m
    
def parse_message(raw_bytes: bytes) -> ParsedEvent:
    payload = json.loads(raw_bytes)
    logger.debug(f"{payload=}")
    
    if "object_attributes" not in payload:
        raise ParseError("payload does not have key object_attributes.")
    branch_name = payload["object_attributes"]["ref"]
    object_attributes_status = payload["object_attributes"]["status"]
    parse_branch_res = parse_branch(branch_name)
    is_auto_fix_branch = bool(parse_branch_res)
    logger.debug(f"branch_name={branch_name}, is_auto_fix_branch={is_auto_fix_branch}, object_attributes_status={object_attributes_status}")
    if not is_auto_fix_branch:
        if payload["object_kind"] == "pipeline" and  object_attributes_status== "failed":
            logger.info("BugReportedEvent")
            project_id = payload.get("project",{}).get("id","")
            project_web_url = payload.get("project",{}).get("web_url","")
            job_id = payload.get("builds",[{}])[0].get("id","")
            return BugReportedEvent(project_id=project_id, project_web_url=project_web_url, job_id=job_id, raw=payload)
    else:
        bug_id = parse_branch_res.groups()[0]
        logger.info("ValidationStatusEvent")
        
        return ValidationStatusEvent(bug_id=bug_id, status=object_attributes_status, raw=payload)
    logger.info("OtherEvent")       
    return OtherEvent(raw=payload)
    
"""    
def parse_message(raw_bytes: bytes) -> ParsedEvent:
    #Parse the data bytes in the stream as a concrete event object.
    try:
        data = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ParseError(f"JSON decode failed: {e}") from e

    if not isinstance(data, dict):
        raise ParseError(f"Expected a JSON object, got: {type(data).__name__}")

    msg_type = data.get("type")
    bug_id = data.get("bug_id")

    if not msg_type:
        raise ParseError(f"Missing 'type' field: {data}")
    if not bug_id:
        raise ParseError(f"Missing 'bug_id' field: {data}")

    if msg_type == "bug_reported":
        return BugReportedEvent(bug_id=bug_id, raw=data)

    if msg_type == "bug_fix_validation_status":
        status = data.get("status", "")
        if not status:
            raise ParseError(f"Missing 'status' in bug_fix_validation_status: {data}")
        return ValidationStatusEvent(bug_id=bug_id, status=status, raw=data)

    raise ParseError(f"Unknown message type: {msg_type!r}")
"""