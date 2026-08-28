import re
import os
import sys
import json
import hmac
import hashlib
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler

# Agrega la RAÍZ del proyecto (un nivel arriba de api/) al path,
# para poder hacer `from invgate import ...` ya que invgate.py
# vive en la raíz, no dentro de api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
BOT_USER_ID = os.getenv("SLACK_BOT_USER_ID")


def verify_slack_signature(body, timestamp, signature):
    if not SLACK_SIGNING_SECRET:
        return False
    if not timestamp or not signature:
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
    except Exception as exc:
        _log(f"Error post_message: {exc}")


def find_slack_user_by_email(email):
    url = f"https://slack.com/api/users.lookupByEmail?email={urllib.parse.quote(email)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    try:
        with urllib.request.urlopen(req) as response:
            res = json.loads(response.read())
        if res.get("ok"):
            return res["user"]["id"]
    except Exception as exc:
        _log(f"Error find_slack_user_by_email: {exc}")
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
    except Exception as exc:
        _log(f"Error bot_already_replied: {exc}")
    return False


def extract_tickets(text):
    matches = re.findall(r"#?(\d{3,10})", text)
    return list(set(matches))


def _log(msg):
    print(f"[slack-bot] {msg}", flush=True)


class handler(BaseHTTPRequestHandler):
    def _respond(self, status=200, payload=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload or {}).encode())

    def do_GET(self):
        self._respond(405, {"error": "method not allowed"})

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)
        body = body_bytes.decode("utf-8")

        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")

        if not verify_slack_signature(body, timestamp, signature):
            _log("Firma inválida o timestamp fuera de ventana")
            self._respond(401, {"error": "invalid signature"})
            return

        if self.headers.get("X-Slack-Retry-Num"):
            _log("Ignorando retry de Slack")
            self._respond(200, {})
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            _log("Body JSON inválido")
            self._respond(200, {})
            return

        _log(f"payload keys={list(payload.keys())}")

        if payload.get("type") == "url_verification":
            _log("Respondiendo url_verification")
            self._respond(200, {"challenge": payload.get("challenge", "")})
            return

        event = payload.get("event") or {}
        _log(
            f"event type={event.get('type')} subtype={event.get('subtype')} "
            f"bot_id={event.get('bot_id')} text={event.get('text')!r}"
        )

        if event.get("subtype") or event.get("bot_id"):
            _log("Ignorando evento de bot o subtipo")
            self._respond(200, {})
            return

        if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
            _log("Ignorando reply en hilo")
            self._respond(200, {})
            return

        text = event.get("text") or ""
        tickets = extract_tickets(text)
        _log(f"tickets detectados={tickets}")
        if not tickets:
            self._respond(200, {})
            return

        channel = event.get("channel")
        thread_ts = event.get("ts")

        if not channel or not thread_ts:
            _log("Falta channel o ts en el evento")
            self._respond(200, {})
            return

        if bot_already_replied(channel, thread_ts):
            _log("Bot ya respondió en este hilo")
            self._respond(200, {})
            return

        from invgate import get_ticket_assignee_name, add_tag_to_ticket

        respuestas = []
        for ticket_id in tickets:
            _log(f"Procesando ticket #{ticket_id}")
            try:
                name, email, group_name, jira_key, resuelto = get_ticket_assignee_name(ticket_id)
                _log(
                    f"Ticket #{ticket_id} -> name={name!r} email={email!r} "
                    f"group={group_name!r} jira={jira_key!r} resuelto={resuelto}"
                )
            except Exception as exc:
                _log(f"Error invgate ticket #{ticket_id}: {exc}")
                continue

            if resuelto:
                respuestas.append(f"• #{ticket_id}: ya está resuelto/cerrado ✅")
                continue

            if name or group_name:
                try:
                    add_tag_to_ticket(ticket_id)
                except Exception as exc:
                    _log(f"Error add_tag ticket #{ticket_id}: {exc}")

            if not name and not group_name:
                _log(f"Ticket #{ticket_id}: sin asignación, no responde")
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
            _log(f"Enviando mensaje a Slack: {mensaje!r}")
            post_message(channel, mensaje, thread_ts)
        else:
            _log("Sin respuestas para enviar")

        self._respond(200, {})
