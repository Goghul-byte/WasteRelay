# Primary WhatsApp webhook entry point.
# The handler coordinates conversation state and delegates domain work to common modules.

import os
import json
import random
import uuid
from datetime import datetime, timezone, timedelta

from common import whatsapp_client
from common.dynamo import (
    get_conversation_state, set_conversation_state, clear_conversation_state,
    get_ngo_by_phone, get_restaurant_by_phone, put_restaurant,
    get_donation, put_donation, update_donation, upsert_dish,
)
from common.dish_classifier import extract_items, match_dish_category
from common.structured_input import parse_minutes, is_within_sanity_bound
from common import ngo_matcher
from common.safe_window import final_safe_window_minutes, compute_deadline, remaining_minutes, overall_deadline_for_items

VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]

CATEGORY_BUTTON_LABELS = {
    "cat_gravy": "gravy_curry",
    "cat_dry": "dry_fried",
    "cat_meat": "meat_seafood",
}
WASTE_STREAM_OPTIONS = [
    ("stream_wet", "Wet Waste"),
    ("stream_dry", "Dry Waste"),
    ("stream_sanitary", "Sanitary Waste"),
    ("stream_special", "Special / Hazardous"),
]
WASTE_STREAM_LABELS = dict(WASTE_STREAM_OPTIONS)
SPOILAGE_PRESETS = {  # T1b quick-select buttons -> minutes
    "spoil_30": 30, "spoil_60": 60, "spoil_120": 120, "spoil_180": 180,
}


# ---------- Lambda entry point ----------
def lambda_handler(event, context):
    method = event.get("httpMethod")

    if method == "GET":
        return _handle_verification(event)

    body = json.loads(event.get("body") or "{}")
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                _route_message(message.get("from"), message)

    return {"statusCode": 200, "body": "ok"}


def _handle_verification(event):
    # Meta calls this once when you save the webhook URL in the dashboard
    params = event.get("queryStringParameters") or {}
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return {"statusCode": 200, "body": params.get("hub.challenge", "")}
    return {"statusCode": 403, "body": "verification failed"}


# ---------- message content helpers ----------
def _message_text(message):
    return (message.get("text") or {}).get("body", "").strip()


def _button_id(message):
    interactive = message.get("interactive", {})
    reply = interactive.get("button_reply") or interactive.get("list_reply")
    return reply.get("id") if reply else None


def _location(message):
    return message.get("location")


# ---------- top-level routing: figure out who is texting us ----------
def _route_message(phone, message):
    ngo = get_ngo_by_phone(phone)
    if ngo:
        _handle_ngo_message(ngo, phone, message)
        return

    restaurant = get_restaurant_by_phone(phone)
    if restaurant is None:
        restaurant = {"restaurant_id": phone, "phone": phone, "name": f"Restaurant {phone[-4:]}"}
        put_restaurant(restaurant)
    _handle_restaurant_message(restaurant, phone, message)


# =========================================================
#  RESTAURANT SIDE
# =========================================================
RESTART_WORDS = {"restart", "reset", "start over"}


def _handle_restaurant_message(restaurant, phone, message):
    state = get_conversation_state(phone) or {}
    awaiting = state.get("awaiting")
    button_id = _button_id(message)
    text = _message_text(message)
    loc = _location(message)

    if text.strip().lower() in RESTART_WORDS:
        clear_conversation_state(phone)
        whatsapp_client.send_text(phone, "Sure, let's start fresh! What food do you have today? (e.g. 50kg Biryani)")
        return

    if awaiting is None:
        _start_new_submission(restaurant, phone, text)

    elif awaiting == "item_names":
        _start_new_submission(restaurant, phone, text)  # retry classification with the clarified names

    elif awaiting == "category_button" and button_id in CATEGORY_BUTTON_LABELS:
        _resolve_category_and_continue(restaurant, phone, state, button_id)

    elif awaiting == "waste_stream" and button_id in WASTE_STREAM_LABELS:
        _handle_waste_stream_choice(phone, state, button_id)

    elif awaiting == "location" and loc:
        _save_location_start_item_loop(restaurant, phone, state, loc)

    elif awaiting == "item_prep_time":
        _handle_item_prep_time_answer(restaurant, phone, state, text)

    elif awaiting == "item_safety":
        # Accept both an interactive-list tap and an ordinary text reply.  Text
        # fallback keeps the flow usable if WhatsApp does not render the list.
        _handle_item_safety_answer(restaurant, phone, state, button_id, text)

    elif awaiting == "item_safety_custom":
        _handle_item_safety_custom_number(restaurant, phone, state, text)

    elif awaiting == "item_safety_confirm":
        _handle_item_safety_confirm(restaurant, phone, state, button_id or text.strip().lower())

    else:
        # Reaching here usually means we're waiting on a button/list tap that never actually
        # arrived on the user's phone (e.g. a WhatsApp API hiccup). Re-send the same question
        # instead of leaving the conversation permanently stuck.
        _resend_current_step(phone, state, awaiting)


def _resend_current_step(phone, state, awaiting):
    nudge = "Please pick one of the options above to continue - or type RESTART to begin a new order."

    if awaiting == "category_button" and state.get("current_item"):
        item = state["current_item"]
        whatsapp_client.send_text(phone, nudge)
        whatsapp_client.send_buttons(
            phone, f"What type of dish is '{item['item_name']}'?",
            [("cat_gravy", "Gravy/Curry"), ("cat_dry", "Dry/Fried"), ("cat_meat", "Meat/Seafood")],
        )
        return

    if awaiting == "waste_stream" and state.get("items"):
        whatsapp_client.send_text(phone, nudge)
        _ask_waste_stream(phone)
        return

    if awaiting == "item_safety" and state.get("items"):
        whatsapp_client.send_text(phone, nudge)
        _ask_safety_for_current_item(phone, state)
        return

    if awaiting == "item_safety_confirm" and state.get("items") and "pending_item_safety_minutes" in state:
        item = _current_item(state)
        minutes = state["pending_item_safety_minutes"]
        whatsapp_client.send_text(phone, nudge)
        whatsapp_client.send_buttons(phone, f"Adding {minutes} minutes to the {item['item_name']}'s safe window. Confirm?",
                                      [("confirm", "Confirm"), ("change", "Change")])
        return

    # nothing we can meaningfully resend (or state is unrecognized/corrupted) - offer a clean way out
    whatsapp_client.send_text(phone, "Sorry, I didn't understand that. Type RESTART to begin a new order.")



def _start_new_submission(restaurant, phone, text):
    items = extract_items(text)
    if not items:
        set_conversation_state(phone, {"awaiting": "item_names"})
        whatsapp_client.send_text(phone, "What are the main items in this food? (e.g. Rice, Sambar)")
        return

    # classify each item; the first unmatched one triggers the button fallback (Tier 3)
    resolved, pending = [], list(items)
    while pending:
        item = pending.pop(0)
        category, confident = match_dish_category(item["item_name"])
        if confident:
            item["category"] = category
            resolved.append(item)
        else:
            set_conversation_state(phone, {
                "awaiting": "category_button",
                "resolved_items": resolved,
                "pending_items": pending,
                "current_item": item,
            })
            whatsapp_client.send_buttons(
                phone, f"What type of dish is '{item['item_name']}'?",
                [("cat_gravy", "Gravy/Curry"), ("cat_dry", "Dry/Fried"), ("cat_meat", "Meat/Seafood")],
            )
            return

    _finish_item_classification(restaurant, phone, resolved)


def _resolve_category_and_continue(restaurant, phone, state, button_id):
    category = CATEGORY_BUTTON_LABELS[button_id]
    item = state["current_item"]
    item["category"] = category
    upsert_dish(item["item_name"], category, confirmed_by=phone)  # Tier 4 auto-learning

    resolved = state.get("resolved_items", []) + [item]
    pending = state.get("pending_items", [])

    while pending:
        next_item = pending.pop(0)
        cat, confident = match_dish_category(next_item["item_name"])
        if confident:
            next_item["category"] = cat
            resolved.append(next_item)
        else:
            set_conversation_state(phone, {
                "awaiting": "category_button", "resolved_items": resolved,
                "pending_items": pending, "current_item": next_item,
            })
            whatsapp_client.send_buttons(
                phone, f"What type of dish is '{next_item['item_name']}'?",
                [("cat_gravy", "Gravy/Curry"), ("cat_dry", "Dry/Fried"), ("cat_meat", "Meat/Seafood")],
            )
            return

    _finish_item_classification(restaurant, phone, resolved)


def _finish_item_classification(restaurant, phone, resolved_items):
    # WasteSlot: show India's four source-segregation streams before location.
    set_conversation_state(phone, {"awaiting": "waste_stream", "items": resolved_items})
    _ask_waste_stream(phone)


def _ask_waste_stream(phone):
    whatsapp_client.send_list(
        phone,
        "Which waste stream does this pickup belong to?",
        WASTE_STREAM_OPTIONS,
        section_title="Waste category",
    )


def _handle_waste_stream_choice(phone, state, button_id):
    selected_stream = WASTE_STREAM_LABELS[button_id]

    # The current prototype retains the implemented FeedBridge food workflow.
    # Other streams are visible in the WasteSlot intake but are not routed yet.
    if button_id != "stream_wet":
        whatsapp_client.send_text(
            phone,
            f"{selected_stream} selected. This prototype currently continues with the Wet Waste food-rescue flow.",
        )
        _ask_waste_stream(phone)
        return

    state["waste_stream"] = selected_stream
    state["awaiting"] = "location"
    set_conversation_state(phone, state)
    total = ", ".join(f"{i['quantity']}{i['unit']} {i['item_name']}" for i in state["items"])
    whatsapp_client.send_location_request(
        phone,
        f"Wet Waste selected. {total} logged. Quick question - what's your pickup location?",
    )


def _save_location_start_item_loop(restaurant, phone, state, loc):
    state["restaurant_lat"] = loc["latitude"]
    state["restaurant_lng"] = loc["longitude"]
    state["item_index"] = 0
    state["item_answers"] = []  # parallel to state["items"]; each entry: {prep_time_iso, restaurant_estimate_minutes, spoiled}
    state["awaiting"] = "item_prep_time"
    set_conversation_state(phone, state)
    _ask_prep_time_for_current_item(phone, state)


def _current_item(state):
    # DynamoDB returns numeric values as Decimal.  Decimal cannot be used as a
    # Python list index, so cast it before indexing on every new webhook call.
    return state["items"][int(state["item_index"])]


def _ask_prep_time_for_current_item(phone, state):
    item = _current_item(state)
    whatsapp_client.send_text(
        phone, f"How long ago was the {item['item_name']} cooked? Reply with just the number of minutes (e.g. 30, 60, 90)."
    )


def _ask_safety_for_current_item(phone, state):
    item = _current_item(state)
    whatsapp_client.send_list(  # T1b - more than 3 options so use a list message
        phone, f"How much longer do you think the {item['item_name']} will stay safe to eat? "
               "Choose an option, or reply with the number of minutes.",
        [("spoil_30", "30 mins"), ("spoil_60", "1 hour"), ("spoil_120", "2 hours"),
         ("spoil_180", "3 hours"), ("spoil_custom", "Custom"),
         ("spoil_unsafe", "Spoiled / Unsafe"), ("spoil_skip", "Skip")],
    )


def _parse_plain_minutes(text):
    """Strict numeric-only parse for the cook-time question - digits only, no 'hour'/'mins' wording."""
    stripped = text.strip()
    if not stripped.isdigit():
        return None
    return int(stripped)


def _handle_item_prep_time_answer(restaurant, phone, state, text):
    minutes_ago = _parse_plain_minutes(text)
    if not is_within_sanity_bound(minutes_ago):
        whatsapp_client.send_text(phone, "Please reply with just a number of minutes (e.g. 30, 60, 90) - between 1 and 180.")
        return

    prep_time_iso = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    state["item_answers"].append({"prep_time_iso": prep_time_iso})  # restaurant_estimate/spoiled filled in next step
    state["awaiting"] = "item_safety"
    set_conversation_state(phone, state)
    _ask_safety_for_current_item(phone, state)


def _handle_item_safety_choice(restaurant, phone, state, button_id):
    if button_id == "spoil_custom":
        state["awaiting"] = "item_safety_custom"
        set_conversation_state(phone, state)
        item = _current_item(state)
        whatsapp_client.send_text(phone, f"Enter total extra minutes needed for the {item['item_name']} (e.g., 40):")
        return

    current_answer = state["item_answers"][-1]
    if button_id == "spoil_unsafe":
        current_answer["restaurant_estimate_minutes"] = None
        current_answer["spoiled"] = True
    else:
        current_answer["restaurant_estimate_minutes"] = None if button_id == "spoil_skip" else SPOILAGE_PRESETS.get(button_id)
        current_answer["spoiled"] = False

    set_conversation_state(phone, state)
    _advance_to_next_item_or_finalize(restaurant, phone, state)


def _handle_item_safety_answer(restaurant, phone, state, button_id, text):
    """Handle either a WhatsApp list selection or a plain-text fallback."""
    if button_id:
        _handle_item_safety_choice(restaurant, phone, state, button_id)
        return

    normalized = text.strip().lower()
    if normalized in {"spoiled", "unsafe", "spoiled / unsafe"}:
        _handle_item_safety_choice(restaurant, phone, state, "spoil_unsafe")
        return
    if normalized == "skip":
        _handle_item_safety_choice(restaurant, phone, state, "spoil_skip")
        return

    minutes = parse_minutes(text)
    if not is_within_sanity_bound(minutes):
        item = _current_item(state)
        whatsapp_client.send_text(
            phone,
            f"Please reply with the number of minutes the {item['item_name']} will remain safe "
            "(1 to 180), or type SPOILED or SKIP.",
        )
        return

    current_answer = state["item_answers"][-1]
    current_answer["restaurant_estimate_minutes"] = minutes
    current_answer["spoiled"] = False
    set_conversation_state(phone, state)
    _advance_to_next_item_or_finalize(restaurant, phone, state)


def _handle_item_safety_custom_number(restaurant, phone, state, text):
    minutes = parse_minutes(text)
    if not is_within_sanity_bound(minutes):
        whatsapp_client.send_text(phone, "That doesn't look right - please enter a number between 1 and 180 minutes")
        return
    state["awaiting"] = "item_safety_confirm"
    state["pending_item_safety_minutes"] = minutes
    set_conversation_state(phone, state)
    item = _current_item(state)
    whatsapp_client.send_buttons(phone, f"Adding {minutes} minutes to the {item['item_name']}'s safe window. Confirm?",
                                  [("confirm", "Confirm"), ("change", "Change")])


def _handle_item_safety_confirm(restaurant, phone, state, button_id):
    if button_id == "change":
        state["awaiting"] = "item_safety"
        set_conversation_state(phone, state)
        _ask_safety_for_current_item(phone, state)
        return

    if button_id != "confirm":
        whatsapp_client.send_text(phone, "Please choose Confirm or Change.")
        return

    current_answer = state["item_answers"][-1]
    current_answer["restaurant_estimate_minutes"] = state.pop("pending_item_safety_minutes", None)
    current_answer["spoiled"] = False
    set_conversation_state(phone, state)
    _advance_to_next_item_or_finalize(restaurant, phone, state)


def _advance_to_next_item_or_finalize(restaurant, phone, state):
    # Normalize DynamoDB Decimal values before doing list-oriented work.
    state["item_index"] = int(state["item_index"]) + 1
    if state["item_index"] < len(state["items"]):
        state["awaiting"] = "item_prep_time"
        set_conversation_state(phone, state)
        _ask_prep_time_for_current_item(phone, state)
    else:
        _finalize_all_items(restaurant, phone, state)


def _responder_label(donation):
    return "biogas/manure plant" if donation.get("org_type") == "biogas_plant" else "NGO"


SPOILED_PICKUP_WINDOW_MINUTES = 180  # demo stand-in: reuses the NGO table/flow as a "plant" pickup window


def _build_donation(restaurant, phone, state, item_answer_pairs, org_type):
    items = [item for item, ans in item_answer_pairs]
    item_deadlines = []
    for item, ans in item_answer_pairs:
        if ans.get("spoiled"):
            window_minutes = SPOILED_PICKUP_WINDOW_MINUTES
        else:
            window_minutes = final_safe_window_minutes(item["category"], ans.get("restaurant_estimate_minutes"))
        item_deadlines.append(compute_deadline(ans["prep_time_iso"], window_minutes))
    overall_deadline = overall_deadline_for_items(item_deadlines)
    earliest_prep = min(ans["prep_time_iso"] for _, ans in item_answer_pairs)

    donation_id = str(uuid.uuid4())
    donation = {
        "donation_id": donation_id,
        "restaurant_id": restaurant["restaurant_id"],
        "restaurant_phone": phone,
        "restaurant_name": restaurant["name"],
        "restaurant_lat": state["restaurant_lat"],
        "restaurant_lng": state["restaurant_lng"],
        "items": items,
        "prep_time": earliest_prep,
        "safe_window_deadline": overall_deadline.isoformat(),
        "current_ngo_id": None,
        "excluded_ngo_ids": [],
        "status": "awaiting_ngo_response",
        "org_type": org_type,
        "history": [{"event": "created", "at": datetime.now(timezone.utc).isoformat()}],
    }
    put_donation(donation)
    return donation


def _finalize_all_items(restaurant, phone, state):
    item_answer_pairs = list(zip(state["items"], state["item_answers"]))
    safe_pairs = [(item, ans) for item, ans in item_answer_pairs if not ans.get("spoiled")]
    spoiled_pairs = [(item, ans) for item, ans in item_answer_pairs if ans.get("spoiled")]

    clear_conversation_state(phone)

    donations_to_offer = []  # (donation, org_type, label)
    if safe_pairs:
        donation = _build_donation(restaurant, phone, state, safe_pairs, org_type="ngo")
        donations_to_offer.append((donation, "ngo", "NGO"))
    if spoiled_pairs:
        donation = _build_donation(restaurant, phone, state, spoiled_pairs, org_type="biogas_plant")
        donations_to_offer.append((donation, "biogas_plant", "Biogas/Manure Plant"))

    if safe_pairs and spoiled_pairs:
        whatsapp_client.send_text(
            phone, "Splitting this into two pickups - the still-safe items are going out to nearby NGOs, "
                   "and the spoiled ones to a nearby Biogas/Manure plant.",
        )
    elif safe_pairs:
        whatsapp_client.send_text(phone, "Thanks! We're pinging nearby NGOs now. Hang tight - we'll update you the moment one accepts.")  # T2
    else:
        whatsapp_client.send_text(phone, "Got it - marking this as spoiled. Pinging nearby Biogas and Manure plants for pickup.")

    for donation, org_type, label in donations_to_offer:
        target, distance = ngo_matcher.find_next_ngo(donation, org_type=org_type)
        if target is None:
            ngo_matcher.cancel_unclaimed(donation)
            continue
        ngo_matcher.send_offer(donation, target, distance, label=label)


# =========================================================
#  NGO SIDE
# =========================================================
def _handle_ngo_message(ngo, phone, message):
    state = get_conversation_state(phone) or {}
    awaiting = state.get("awaiting")
    button_id = _button_id(message)
    text = _message_text(message)

    if button_id and (button_id.startswith("accept_") or button_id.startswith("decline_")):
        _handle_offer_response(ngo, phone, button_id)

    elif awaiting == "eta_leg1" and button_id:
        _handle_eta_button(ngo, phone, state, button_id, leg="leg1")
    elif awaiting == "eta_leg1_custom":
        _handle_eta_custom_number(ngo, phone, state, text, leg="leg1")
    elif awaiting == "eta_leg1_confirm" and button_id:
        _handle_eta_confirm(ngo, phone, state, button_id, leg="leg1")

    elif awaiting == "eta_leg2" and button_id:
        _handle_eta_button(ngo, phone, state, button_id, leg="leg2")
    elif awaiting == "eta_leg2_custom":
        _handle_eta_custom_number(ngo, phone, state, text, leg="leg2")
    elif awaiting == "eta_leg2_confirm" and button_id:
        _handle_eta_confirm(ngo, phone, state, button_id, leg="leg2")

    elif awaiting == "in_transit":
        _handle_in_transit_command(ngo, phone, state, text, button_id)
    elif awaiting == "eta_modify" and button_id:
        _handle_eta_button(ngo, phone, state, button_id, leg="modify")
    elif awaiting == "eta_modify_custom":
        _handle_eta_custom_number(ngo, phone, state, text, leg="modify")
    elif awaiting == "eta_modify_confirm" and button_id:
        _handle_eta_confirm(ngo, phone, state, button_id, leg="modify")

    elif awaiting == "tight_window_cooler" and button_id:
        _handle_cooler_response(ngo, phone, state, button_id)

    elif awaiting == "awaiting_otp":
        _handle_otp_entry(ngo, phone, state, text)

    else:
        whatsapp_client.send_text(phone, "Sorry, I didn't understand that. Please try again.")


def _handle_offer_response(ngo, phone, button_id):
    action, donation_id = button_id.split("_", 1)
    donation = get_donation(donation_id)
    if donation is None or donation["status"] != "awaiting_ngo_response":
        whatsapp_client.send_text(phone, "This pickup is no longer available.")
        return

    if action == "decline":
        ngo_matcher.escalate(donation)
        return

    # accepted - check the tight-window rule (Section 3.7) before collecting ETAs
    remaining = remaining_minutes(donation["safe_window_deadline"])
    if remaining < 15:
        set_conversation_state(phone, {"awaiting": "tight_window_cooler", "donation_id": donation_id})
        whatsapp_client.send_buttons(
            phone, f"This pickup has a tight window (~{int(remaining)} min left). Do you have a cooler/cold box ready for transport?",
            [("cooler_yes", "Yes, ready"), ("cooler_no", "No, skip")],
        )
        return

    _start_eta_collection(donation, phone)


def _handle_cooler_response(ngo, phone, state, button_id):
    donation = get_donation(state["donation_id"])
    if button_id == "cooler_no":
        ngo_matcher.escalate(donation)
        return
    _start_eta_collection(donation, phone)


def _start_eta_collection(donation, phone):
    update_donation(donation["donation_id"], {"status": "eta_collection"})
    whatsapp_client.send_buttons(
        phone, "Good - you're confirmed as the responder. How long until you reach the restaurant?",
        [("15", "15 mins"), ("30", "30 mins"), ("custom", "Custom mins")],  # Cancel handled as a 4th option below
    )
    whatsapp_client.send_buttons(phone, "Or:", [("cancel", "Cancel Pickup")])
    set_conversation_state(phone, {"awaiting": "eta_leg1", "donation_id": donation["donation_id"]})


# --- shared structured-minutes logic for leg1 / leg2 / modify (Section 3.4) ---
def _handle_eta_button(ngo, phone, state, button_id, leg):
    donation = get_donation(state["donation_id"])
    if button_id == "cancel":
        clear_conversation_state(phone)
        ngo_matcher.escalate(donation, f"The responding {_responder_label(donation)} cancelled - we've offered it to the next nearest one.")
        return
    if button_id == "custom":
        state["awaiting"] = f"eta_{leg}_custom"
        set_conversation_state(phone, state)
        whatsapp_client.send_text(phone, "Enter total extra minutes needed (e.g., 40):")
        return
    _apply_eta_minutes(ngo, phone, state, int(button_id), leg)


def _handle_eta_custom_number(ngo, phone, state, text, leg):
    minutes = parse_minutes(text)
    if not is_within_sanity_bound(minutes):
        whatsapp_client.send_text(phone, "That doesn't look right - please enter a number between 1 and 180 minutes")
        return
    state["awaiting"] = f"eta_{leg}_confirm"
    state["pending_minutes"] = minutes
    set_conversation_state(phone, state)
    whatsapp_client.send_buttons(phone, f"Adding {minutes} minutes to pickup ETA. Confirm?",
                                  [("confirm", "Confirm"), ("change", "Change")])


def _handle_eta_confirm(ngo, phone, state, button_id, leg):
    if button_id == "change":
        state["awaiting"] = leg if leg == "modify" else f"eta_{leg}"
        set_conversation_state(phone, state)
        whatsapp_client.send_buttons(phone, "Okay - pick again:",
                                      [("15", "15 mins"), ("30", "30 mins"), ("custom", "Custom mins"), ("cancel", "Cancel Pickup")])
        return
    donation = get_donation(state["donation_id"])
    _apply_eta_minutes(get_ngo_by_phone(phone), phone, state, state["pending_minutes"], leg)


def _apply_eta_minutes(ngo, phone, state, minutes, leg):
    donation = get_donation(state["donation_id"])

    if leg == "leg1":
        update_donation(donation["donation_id"], {"eta_leg1_minutes": minutes})
        set_conversation_state(phone, {"awaiting": "eta_leg2", "donation_id": donation["donation_id"]})
        whatsapp_client.send_buttons(
            phone, "Got it. Now - how long from pickup to your drop-off point?",
            [("15", "15 mins"), ("30", "30 mins"), ("custom", "Custom mins"), ("cancel", "Cancel Pickup")],
        )
        return

    if leg == "leg2":
        donation["eta_leg2_minutes"] = minutes
        update_donation(donation["donation_id"], {"eta_leg2_minutes": minutes})
        _validate_and_confirm_pickup(donation, phone)
        return

    if leg == "modify":
        # backend rule: add the delta to current_eta_timestamp, never recompute from the original ETA
        new_eta = (datetime.fromisoformat(donation["current_eta_timestamp"]) + timedelta(minutes=float(minutes))).isoformat()
        history = donation.get("history", []) + [{"event": "eta_delay", "minutes": minutes, "new_eta": new_eta}]
        update_donation(donation["donation_id"], {"current_eta_timestamp": new_eta, "history": history})
        donation["current_eta_timestamp"] = new_eta
        _recheck_in_transit_window(donation, phone)


def _validate_and_confirm_pickup(donation, phone):
    # Section 3.5: check leg1+leg2 sum against the remaining safe window
    total_minutes = donation["eta_leg1_minutes"] + donation["eta_leg2_minutes"]
    remaining = remaining_minutes(donation["safe_window_deadline"])

    if total_minutes >= remaining:
        clear_conversation_state(phone)
        whatsapp_client.send_text(
            phone, "Thanks for responding - unfortunately with your ETA the food would spoil before "
                   f"drop-off, so we can't confirm this pickup. We've offered it to the next nearest {_responder_label(donation)}.",
        )
        ngo_matcher.escalate(donation)
        return

    otp = f"{random.randint(1000, 9999)}"
    current_eta = (datetime.now(timezone.utc) + timedelta(minutes=float(total_minutes))).isoformat()
    update_donation(donation["donation_id"], {
        "status": "confirmed", "otp_code": otp, "current_eta_timestamp": current_eta,
        "history": donation.get("history", []) + [{"event": "confirmed", "eta": current_eta}],
    })
    donation["current_eta_timestamp"] = current_eta

    set_conversation_state(phone, {"awaiting": "in_transit", "donation_id": donation["donation_id"]})
    whatsapp_client.send_text(
        phone, f"Confirmed! Pickup code: {otp}. Show/enter this at the restaurant to complete custody transfer. "
               f"Reply MODIFY if you're running late, or CANCEL if you can no longer make it.",
    )
    accepted_label = "Biogas/Manure plant" if donation.get("org_type") == "biogas_plant" else "NGO"
    whatsapp_client.send_text(
        donation["restaurant_phone"],
        f"Good news! An {accepted_label} accepted and is on the way. Estimated pickup in about {total_minutes} min.",  # T3
    )


def _handle_in_transit_command(ngo, phone, state, text, button_id):
    donation = get_donation(state["donation_id"])
    upper = text.upper()

    if upper.startswith("MODIFY"):
        state["awaiting"] = "eta_modify"
        set_conversation_state(phone, state)
        whatsapp_client.send_buttons(
            phone, "No problem - how many extra minutes do you need? This isn't a penalty, we just want to keep the food safe.",
            [("15", "15 mins"), ("30", "30 mins"), ("custom", "Custom mins"), ("cancel", "Cancel Pickup")],
        )
    elif upper.startswith("CANCEL"):
        clear_conversation_state(phone)
        ngo_matcher.escalate(donation, f"The responding {_responder_label(donation)} cancelled - we've offered it to the next nearest one.")
    elif upper.isdigit() and len(upper) == 4:
        _handle_otp_entry(ngo, phone, state, text)
    else:
        whatsapp_client.send_text(phone, "Reply MODIFY, CANCEL, or send the 4-digit pickup code once you're at the restaurant.")


def _recheck_in_transit_window(donation, phone):
    # Section 3.7: re-check runs the moment a delay is reported, no waiting for the next scheduled check
    remaining = remaining_minutes(donation["safe_window_deadline"])
    eta_minutes_from_now = (datetime.fromisoformat(donation["current_eta_timestamp"]) - datetime.now(timezone.utc)).total_seconds() / 60

    if eta_minutes_from_now >= remaining:
        clear_conversation_state(phone)
        label = _responder_label(donation)
        ngo_matcher.escalate(donation, f"The {label}'s updated ETA would exceed the food-safety window - we've offered it to the next nearest {label}.")
        return

    set_conversation_state(phone, {"awaiting": "in_transit", "donation_id": donation["donation_id"]})
    whatsapp_client.send_text(phone, "Thanks for the update - you're still good for pickup.")


def _handle_otp_entry(ngo, phone, state, text):
    donation = get_donation(state["donation_id"])
    if text.strip() != donation.get("otp_code"):
        whatsapp_client.send_text(phone, "That code doesn't match - please check with the restaurant staff and try again.")
        return

    update_donation(donation["donation_id"], {
        "status": "picked_up",
        "history": donation.get("history", []) + [{"event": "picked_up", "at": datetime.now(timezone.utc).isoformat()}],
    })
    clear_conversation_state(phone)
    whatsapp_client.send_text(phone, "Custody transfer confirmed. Thank you for the rescue!")
    whatsapp_client.send_text(donation["restaurant_phone"], "Pickup confirmed - thank you for reducing food waste today!")
