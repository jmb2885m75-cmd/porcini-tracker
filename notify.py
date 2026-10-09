from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import requests

from dates import format_display_date, parse_user_date


def iso_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


parse_date = parse_user_date


def valid_date_string(value: Any) -> bool:
    try:
        parse_date(value)
        return True
    except (TypeError, ValueError):
        return False


def _load_alert_state(default: Dict[str, Any]) -> Any:
    from porcini import ALERT_STATE_PATH, load_json_safe
    return load_json_safe(ALERT_STATE_PATH, default)


def _dispatch_notification(cfg: Dict[str, Any], message: str) -> bool:
    from porcini import dispatch_notification
    return dispatch_notification(cfg, message)

DEFAULT_ALERT_THRESHOLD = 45


FRIDAY_POLICY_THURSDAY_ONLY = "thursday_alerted_only"


FRIDAY_POLICY_ALL_ABOVE = "all_above_threshold"


FRIDAY_POLICIES = (FRIDAY_POLICY_THURSDAY_ONLY, FRIDAY_POLICY_ALL_ABOVE)


MIN_ALERT_GAP_DAYS = 5


def build_alert_state() -> Dict[str, Any]:
    state = _load_alert_state( {"locations": {}})
    if not isinstance(state, dict) or not isinstance(state.get("locations"), dict):
        state = {"locations": {}}
    return state


def status_category(status: Optional[str]) -> str:
    """Collapse status text into coarse buckets; only a bucket change is a 'significant' status change."""
    s = (status or "").lower()
    if "frost" in s or "terminated" in s:
        return "terminated"
    if "delayed" in s:
        return "delayed"
    if "exhaustion" in s:
        return "exhausted"
    if "too dry" in s:
        return "dry"
    if "outside" in s:
        return "off_season"
    if "viable" in s:
        return "viable"
    return "watch"


def should_alert_for_location(location_name: str, current_score: int, status: str, threshold: int, state: Dict[str, Any], today: Optional[date] = None, minimum_gap_days: int = MIN_ALERT_GAP_DAYS) -> bool:
    """All three conditions must hold: threshold crossed upward, significant status change, >=5 days since last alert.

    last_score / last_status are the previous run's values (updated every run, see main()).
    """
    loc_state = state["locations"].get(location_name, {})
    last_score = loc_state.get("last_score")
    last_score = -1 if last_score is None else last_score
    last_date = loc_state.get("last_alert_date")
    today = today or datetime.now(timezone.utc).date()
    days_since = (today - parse_date(last_date)).days if last_date and valid_date_string(last_date) else 999
    crossed = last_score < threshold <= current_score
    status_changed = status_category(loc_state.get("last_status")) != status_category(status)
    return crossed and status_changed and days_since >= minimum_gap_days


def configured_alert_gap_days(cfg: Dict[str, Any]) -> int:
    try:
        return max(0, int(cfg.get("MIN_ALERT_GAP_DAYS", MIN_ALERT_GAP_DAYS)))
    except (TypeError, ValueError):
        return MIN_ALERT_GAP_DAYS


def notify_sync_failure(cfg: Dict[str, Any], reason: str) -> bool:
    message = f"❌ Porcini weather sync failed: {reason}"
    print(f"[ERROR] {message}")
    delivered = _dispatch_notification(cfg, message)
    print("[INFO] Failure notification delivered" if delivered else "[WARN] Failure notification was not delivered")
    return delivered


def resolve_friday_policy(cfg: Dict[str, Any]) -> str:
    policy = cfg.get("FRIDAY_POLICY", FRIDAY_POLICY_THURSDAY_ONLY)
    if policy not in FRIDAY_POLICIES:
        print(f"[WARN] Unknown FRIDAY_POLICY {policy!r}; using {FRIDAY_POLICY_THURSDAY_ONLY!r}")
        return FRIDAY_POLICY_THURSDAY_ONLY
    return policy


def should_confirm_for_location(location_name: str, state: Dict[str, Any], today: Optional[date] = None,
                                policy: str = FRIDAY_POLICY_THURSDAY_ONLY, best_score: Optional[int] = None,
                                threshold: int = DEFAULT_ALERT_THRESHOLD) -> bool:
    """Friday final confirmation, at most once per day per spot. Who is eligible depends on `policy`:

    thursday_alerted_only: only spots that got a Thursday outlook alert within the last 2 days (default).
    all_above_threshold:   every spot whose best weekend score is >= threshold.
    """
    loc_state = state["locations"].get(location_name, {})
    today = today or datetime.now(timezone.utc).date()
    if loc_state.get("last_confirmation_date") == iso_date(today):
        return False
    if policy == FRIDAY_POLICY_ALL_ABOVE:
        return best_score is not None and best_score >= threshold
    last_date = loc_state.get("last_alert_date")
    if loc_state.get("last_alert_mode") != "weekend_outlook" or not last_date or not valid_date_string(last_date):
        return False
    return 0 <= (today - parse_date(last_date)).days <= 2


def send_telegram(token: str, chat_id: str, message: str) -> bool:
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    try:
        response = requests.post(url, data=payload, timeout=20)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            description = result.get("description", "invalid API response") if isinstance(result, dict) else "invalid API response"
            print(f"[WARN] Telegram alert failed: {description}")
            return False
        return True
    except Exception as exc:
        print(f"[WARN] Telegram alert failed: {str(exc).replace(token, '***')}")
        return False


def send_pushover(api_token: str, user_key: str, message: str) -> bool:
    if not api_token or not user_key:
        return False
    payload = {"token": api_token, "user": user_key, "message": message, "title": "Porcini Tracker"}
    try:
        response = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=20)
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[WARN] Pushover alert failed: {exc}")
        return False


def send_twilio(account_sid: str, auth_token: str, from_number: str, to_number: str, message: str) -> bool:
    if not (account_sid and auth_token and from_number and to_number):
        return False
    payload = {"From": from_number, "To": to_number, "Body": message}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        response = requests.post(url, data=payload, auth=(account_sid, auth_token), timeout=20)
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[WARN] Twilio alert failed: {exc}")
        return False


def dispatch_notification(cfg: Dict[str, Any], message: str) -> bool:
    settings = cfg.get("NOTIFICATION_SETTINGS", {})
    provider = str(settings.get("provider") or "").lower()
    if provider == "telegram":
        return send_telegram(settings.get("token", ""), settings.get("chat_id_or_recipient", ""), message)
    elif provider == "pushover":
        return send_pushover(settings.get("api_token", ""), settings.get("user_key", ""), message)
    elif provider == "twilio":
        return send_twilio(settings.get("account_sid", ""), settings.get("auth_token", ""), settings.get("from_number", ""), settings.get("to_number", ""), message)
    else:
        print("[WARN] No active notification provider configured")
        return False


def build_alert_message(results: List[Tuple[str, int, str, str]], dashboard_url: str, mode: str = "default", threshold: int = DEFAULT_ALERT_THRESHOLD) -> str:
    from report import MODE_FINAL, MODE_OUTLOOK
    from scoring import score_verdict_tag
    ranking = sorted(results, key=lambda item: item[1], reverse=True)[:3]
    if mode == MODE_OUTLOOK:
        lines = ["🍄 Weekend Porcini Outlook (Thursday, Ranked):"]
    elif mode == MODE_FINAL:
        lines = ["🍄 Final Go/No-Go Confirmation (Friday):"]
    else:
        lines = ["🍄 Weekend Porcini Forecast (Ranked):"]
    for index, result in enumerate(ranking, 1):
        if index > 1:
            lines.append("")
        name, score, best_day, status = result[:4]
        verdict = f"{'GO' if score >= threshold else 'NO-GO'} - " if mode == MODE_FINAL else ""
        tag = score_verdict_tag(score, status, threshold)
        lines.append(f"{index}. {name} - {verdict}{score}/100 index – {tag} ({best_day}) | {status}")
        if len(result) > 4 and result[4]:
            lines.append(f"   Why: {result[4]}")
    lines.append("")
    lines.append(f"🌐 Dashboard: {dashboard_url}")
    return "\n".join(lines)
