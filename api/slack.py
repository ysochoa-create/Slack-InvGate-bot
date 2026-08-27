import re
import os
import sys
import json
import hmac
import hashlib
import time
import urllib.request
import urllib.parse
from invgate import get_ticket_assignee_name, add_tag_to_ticket

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
BOT_USER_ID = os.getenv("SLACK_BOT_USER_ID")


def verify_slack_signature(body, timestamp, signature):
    if not SLACK_SIGNING_SECRET:
        return False
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{body}"
    my_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(my_sig, signature)


def post_message(channel, text, thread_ts):
    data = json.dumps({
        "channel": channel,
        "text": text,
        "thread_ts": thread_ts
    }).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req) as response:
            response.read()
    except Exception:
        pass


def find_slack_user_by_email(email):
    url = f"https://slack.com/api/users.lookupByEmail?email={urllib.parse.quote(email)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    try:
        with urllib.request.urlopen(req) as response:
            res = json.loads(response.read())
        if res.get("ok"):
            return res["user"]["id"]
    except Exception:
        pass
    return None


def bot_already_replied(channel, thread_ts):
    url = f"https://slack.com/api/conversations.replies?channel={channel}&ts={thread_ts}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    try:
        with urllib.request.urlopen(req) as response:
            res = json.loads(response.read())
        if not res.get("ok"):
            return False
        messages = res.get("messages", [])
        for msg in messages[1:]:
            if msg.get("bot_id") or msg.get("app_id"):
                return True
    except Exception:
        pass
    return False


def extract_tickets(text):
    matches = re.findall(r"#?(\d{5,8})", text)
    return list(set(matches))


def _wsgi_start(start_response, status="200 OK", headers=None):
    if headers is None:
        headers = [("Content-Type", "application/json")]
    start_response(status, headers)
    return [b"{}"]


def handler(environ, start_response):
    if environ.get("REQUEST_METHOD") != "POST":
        return _wsgi_start(start_response, "405 Method Not Allowed")

    content_length = int(environ.get("CONTENT_LENGTH", 0))
    body_bytes = environ["wsgi.input"].read(content_length)
    body = body_bytes.decode("utf-8")

    timestamp = environ.get("HTTP_X_SLACK_REQUEST_TIMESTAMP", "")
    signature = environ.get("HTTP_X_SLACK_SIGNATURE", "")

    if not verify_slack_signature(body, timestamp, signature):
        return _wsgi_start(start_response, "401 Unauthorized")

    if environ.get("HTTP_X_SLACK_RETRY_NUM"):
        return _wsgi_start(start_response)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _wsgi_start(start_response)

    if payload.get("type") == "url_verification":
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"challenge": payload.get("challenge", "")}).encode()]

    event = payload.get("event", {})

    if event.get("subtype") or event.get("bot_id"):
        return _wsgi_start(start_response)

    if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        return _wsgi_start(start_response)

    text = event.get("text", "")
    tickets = extract_tickets(text)
    if not tickets:
        return _wsgi_start(start_response)

    channel = event.get("channel")
    thread_ts = event.get("ts")

    if not channel or not thread_ts:
        return _wsgi_start(start_response)

    if bot_already_replied(channel, thread_ts):
        return _wsgi_start(start_response)

    respuestas = []
    for ticket_id in tickets:
        try:
            name, email, group_name, jira_key, resuelto = get_ticket_assignee_name(ticket_id)
        except Exception:
            continue

        if resuelto:
            respuestas.append(f"• #{ticket_id}: ya está resuelto/cerrado ✅")
            continue

        if name or group_name:
            try:
                add_tag_to_ticket(ticket_id)
            except Exception:
                pass

        if not name and not group_name:
            continue

        if name and email:
            slack_user_id = find_slack_user_by_email(email)
            mention = f"<@{slack_user_id}>" if slack_user_id else name
            respuestas.append(f"• #{ticket_id}: asignado a {mention}")
        elif group_name and jira_key:
            respuestas.append(
                f"• #{ticket_id}: escalado a Jira (*{jira_key}*), equipo *{group_name}*"
            )
        elif group_name:
            respuestas.append(f"• #{ticket_id}: asignado al equipo *{group_name}*")

    if respuestas:
        mensaje = "Hola, lo tenemos en el radar:\n" + "\n".join(respuestas)
        try:
            post_message(channel, mensaje, thread_ts)
        except Exception:
            pass

    return _wsgi_start(start_response)
