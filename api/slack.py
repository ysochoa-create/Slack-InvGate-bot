import re
import os
import json
import hmac
import hashlib
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from invgate import get_ticket_assignee_name, add_tag_to_ticket

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
BOT_USER_ID = os.getenv("SLACK_BOT_USER_ID")  # agregar al .env y Vercel

def verify_slack_signature(body, timestamp, signature):
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
    urllib.request.urlopen(req)

def find_slack_user_by_email(email):
    url = f"https://slack.com/api/users.lookupByEmail?email={email}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    res = json.loads(urllib.request.urlopen(req).read())
    if res.get("ok"):
        return res["user"]["id"]
    return None

def bot_already_replied(channel, thread_ts):
    """Verifica si el bot ya respondió en este hilo"""
    url = f"https://slack.com/api/conversations.replies?channel={channel}&ts={thread_ts}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    res = json.loads(urllib.request.urlopen(req).read())
    if not res.get("ok"):
        return False
    messages = res.get("messages", [])
    for msg in messages[1:]:  # skip el mensaje original
        if msg.get("bot_id") or msg.get("app_id"):
            return True
    return False

def extract_tickets(text):
    """Detecta tickets con o sin # — evita falsos positivos con números cortos"""
    matches = re.findall(r"#?(\d{5,8})", text)
    return list(set(matches))

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        from invgate import get_ticket_assignee_name

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length)
        body = body_bytes.decode("utf-8")

        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(body, timestamp, signature):
            self.send_response(401)
            self.end_headers()
            return
        # Ignorar reintentos de Slack
        if self.headers.get("X-Slack-Retry-Num"):
            self.send_response(200)
            self.end_headers()
            return

        payload = json.loads(body)

        if payload.get("type") == "url_verification":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": payload["challenge"]}).encode())
            return

        self.send_response(200)
        self.end_headers()

        event = payload.get("event", {})

        # Ignorar bots y mensajes editados
        if event.get("subtype") or event.get("bot_id"):
            return

        # Ignorar si es respuesta dentro de un hilo (no el mensaje raíz)
        if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
            return

        text = event.get("text", "")
        tickets = extract_tickets(text)
        if not tickets:
            return

        channel = event["channel"]
        thread_ts = event["ts"]

        # No responder si el bot ya respondió en este hilo
        if bot_already_replied(channel, thread_ts):
            return

        respuestas = []
        for ticket_id in tickets:
            name, email, group_name, jira_key, resuelto = get_ticket_assignee_name(ticket_id)

            if resuelto:
                respuestas.append(f"• #{ticket_id}: ya está resuelto/cerrado ✅")
                continue

            if name or group_name:
                add_tag_to_ticket(ticket_id) 

            if not name and not group_name:
                continue  # si no encuentra nada, no responde


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
            post_message(channel, mensaje, thread_ts)