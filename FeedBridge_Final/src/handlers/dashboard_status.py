"""FeedBridge Control Center status reader and same-origin dashboard page."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from common.dynamo import get_donation, get_ngo, update_donation
from common.safe_window import remaining_minutes


PAGE_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=_json_default),
    }


def _html_response():
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": PAGE_PATH.read_text(encoding="utf-8"),
    }


def _event_exists(history, event_name, **criteria):
    for entry in history:
        if entry.get("event") != event_name:
            continue
        if all(entry.get(key) == value for key, value in criteria.items()):
            return True
    return False


def _record_progress_events(donation):
    """Persist dashboard-observable escalation progress without changing domain logic.

    The existing ngo_matcher intentionally owns escalation but does not append a
    dashboard event. We detect excluded NGOs here and append a neutral
    "escalation_detected" event when the dashboard polls after an escalation.
    """
    history = donation.get("history", [])
    changed = False
    now = datetime.now(timezone.utc).isoformat()

    excluded_ids = [str(x) for x in donation.get("excluded_ngo_ids", [])]
    for ngo_id in excluded_ids:
        if not _event_exists(history, "escalation_detected", ngo_id=ngo_id):
            history.append({
                "event": "escalation_detected",
                "at": now,
                "ngo_id": ngo_id,
            })
            changed = True

    current_ngo_id = donation.get("current_ngo_id")
    if current_ngo_id and not _event_exists(history, "ngo_selected", ngo_id=current_ngo_id):
        ngo = get_ngo(current_ngo_id)
        history.append({
            "event": "ngo_selected",
            "at": now,
            "ngo_id": current_ngo_id,
            "ngo_name": ngo.get("name") if ngo else None,
            "distance_km": donation.get("offer_distance_km"),
        })
        changed = True

    if current_ngo_id and donation.get("status") == "awaiting_ngo_response" \
            and not _event_exists(history, "whatsapp_sent", ngo_id=current_ngo_id):
        # This mirrors the initial dashboard create marker: send_offer() has
        # already executed the existing WhatsApp adapter before this status poll.
        history.append({
            "event": "whatsapp_sent",
            "at": now,
            "ngo_id": current_ngo_id,
            "channel": "whatsapp_offer",
        })
        changed = True

    if changed:
        update_donation(donation["donation_id"], {"history": history})
        donation["history"] = history
    return donation


def _friendly_event(entry):
    names = {
        "donation_received": "Donation received",
        "food_classified": "Food classified",
        "rssl_calculated": "Safety window calculated",
        "ngo_selected": "NGO selected",
        "whatsapp_sent": "WhatsApp notification sent",
        "escalation_detected": "Escalation detected",
        "confirmed": "Pickup confirmed",
        "eta_delay": "ETA delayed",
        "picked_up": "Food picked up",
        "no_ngo_available": "No eligible NGO available",
        "created": "Donation created",
    }
    result = dict(entry)
    result["label"] = names.get(entry.get("event"), entry.get("event", "Event"))
    return result


def lambda_handler(event, context):
    path = event.get("path", "") or event.get("rawPath", "")
    params = event.get("queryStringParameters") or {}

    # GET /prod/dashboard serves the HTML from the same Lambda so the browser
    # calls the API on the same origin and needs no CORS change to FeedBridgeApi.
    if path.endswith("/dashboard") and not params.get("donation_id"):
        return _html_response()

    donation_id = params.get("donation_id")
    if not donation_id:
        return _json_response(400, {"error": "donation_id is required"})

    try:
        donation = get_donation(donation_id)
        if not donation:
            return _json_response(404, {"error": "Donation not found"})

        donation = _record_progress_events(donation)
        current_ngo_id = donation.get("current_ngo_id")
        ngo = get_ngo(current_ngo_id) if current_ngo_id else None

        remaining = max(0.0, float(remaining_minutes(donation["safe_window_deadline"])))
        history = [_friendly_event(item) for item in donation.get("history", [])]

        # If the donation has been created through the existing WhatsApp path,
        # history may contain a different naming convention. The dashboard still
        # exposes the real persisted event log without mutating the old handler.
        classified = any(item.get("event") == "food_classified" for item in history) or all(
            bool(item.get("category")) for item in donation.get("items", [])
        )
        safety_calculated = bool(donation.get("safe_window_deadline"))
        matching_completed = bool(current_ngo_id) or donation.get("status") in {"unclaimed", "cancelled"}
        ngo_selected = bool(current_ngo_id)
        whatsapp_sent = any(item.get("event") == "whatsapp_sent" for item in history)

        distance = donation.get("offer_distance_km")
        try:
            distance = float(distance) if distance is not None else None
        except (TypeError, ValueError):
            pass

        payload = {
            "donation_id": donation_id,
            "status": donation.get("status"),
            "rssl_remaining_minutes": round(remaining, 1),
            "safe_window_deadline": donation.get("safe_window_deadline"),
            "temperature_c": donation.get("temperature_c"),
            "food": donation.get("items", []),
            "matched_ngo": {
                "ngo_id": current_ngo_id,
                "name": ngo.get("name") if ngo else None,
                "distance_km": distance,
            } if current_ngo_id else None,
            "checklist": {
                "food_classified": classified,
                "safety_window_calculated": safety_calculated,
                "ngo_matching_completed": matching_completed,
                "ngo_selected": ngo_selected,
                "whatsapp_notification_sent": whatsapp_sent,
            },
            "event_log": history,
        }
        return _json_response(200, payload)

    except Exception as exc:
        print(f"DashboardStatusFunction error: {exc}")
        return _json_response(500, {"error": "Internal server error while reading donation status."})
