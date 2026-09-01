"""
Cliente de la API de InvGate Service Management.
Reemplaza a zendesk.py manteniendo una interfaz similar para api/slack.py:

    get_ticket_assignee_name(ticket_id) -> (name, email, group_name, jira_key, resuelto)
    add_tag_to_ticket(ticket_id, tag)    -> bool

Autenticación: Basic (usuario/clave de servicio), NO OAuth2. Se intentó
client_credentials pero la instancia de InvGate devuelve tokens con
'scopes' siempre vacío (bug reportado a soporte de InvGate) — Basic Auth
con una credencial de servicio funciona correctamente y es lo que se usa
acá.

Lógica de asignación (distinta a Zendesk):
  - Si el ticket NO fue escalado a Jira (custom field 'Enviar a Jira'
    en false/vacío): se informa el agente asignado (assigned_id).
  - Si el ticket SÍ fue escalado a Jira: se informa el equipo del
    custom field 'Equipo', y se intenta extraer la key de Jira
    (ej. 'QI-445') de la nota interna automática que genera la
    integración InvGate-Jira.
"""

import os
import re
import requests

INVGATE_DOMAIN = os.getenv("INVGATE_DOMAIN")  # ej: ubits.sd.cloud.invgate.net
INVGATE_BASIC_USER = os.getenv("INVGATE_BASIC_USER")
INVGATE_BASIC_PASSWORD = os.getenv("INVGATE_BASIC_PASSWORD")

INVGATE_API_BASE_URL = os.getenv(
    "INVGATE_API_BASE_URL", f"https://{INVGATE_DOMAIN}/api/v1"
)

# uids de custom fields confirmados en la instancia de UBITS
CUSTOM_FIELD_EQUIPO = int(os.getenv("INVGATE_CUSTOM_FIELD_EQUIPO", "26"))
CUSTOM_FIELD_ENVIADO_A_JIRA = int(os.getenv("INVGATE_CUSTOM_FIELD_ENVIADO_A_JIRA", "29"))

# Regex para extraer la key de Jira (ej. QI-445) de la nota interna
# automática: "Se creó el issue en Jira: QI-445" con link a
# https://<dominio>.atlassian.net/browse/QI-445
_JIRA_KEY_RE = re.compile(r"atlassian\.net/browse/([A-Z][A-Z0-9]*-\d+)")

_AUTH = (INVGATE_BASIC_USER, INVGATE_BASIC_PASSWORD)


def _get(resource: str, params: dict):
    url = f"{INVGATE_API_BASE_URL}/{resource}"
    response = requests.get(url, auth=_AUTH, params=params, timeout=10)
    if response.status_code != 200:
        return None
    return response.json()


def _post(resource: str, payload: dict):
    url = f"{INVGATE_API_BASE_URL}/{resource}"
    response = requests.post(url, auth=_AUTH, json=payload, timeout=10)
    if response.status_code != 200:
        return None
    return response.json()


def _extract_jira_key(ticket_id):
    """Busca la key de Jira en las notas/comentarios del ticket."""
    comments = _get("incident.comment", {"request_id": ticket_id}) or []
    for comment in comments:
        message = comment.get("message") or ""
        match = _JIRA_KEY_RE.search(message)
        if match:
            return match.group(1)
    return None


def get_ticket_assignee_name(ticket_id):
    """
    Devuelve (name, email, group_name, jira_key, resuelto, resuelto_por, resuelto_por_email)
    para un ticket de InvGate.

    - Ticket ya resuelto/cerrado: (None, None, None, None, True, nombre_o_None, email_o_None)
    - Ticket escalado a Jira: (None, None, nombre_del_equipo, jira_key, False, None, None)
    - Ticket no escalado: (nombre_agente, email_agente, nombre_grupo, None, False, None, None)
    """
    incident = _get("incident", {"id": ticket_id})
    if not incident:
        return None, None, None, None, False, None, None

    if incident.get("solved_at"):
        resuelto_por, resuelto_por_email = _get_resolver_name(incident)
        return None, None, None, None, True, resuelto_por, resuelto_por_email

    custom_fields = incident.get("custom_fields") or {}
    enviado_a_jira = custom_fields.get(str(CUSTOM_FIELD_ENVIADO_A_JIRA)) is True

    if enviado_a_jira:
        equipo_raw = custom_fields.get(str(CUSTOM_FIELD_EQUIPO)) or {}
        # Viene como {"<hash>": "<Nombre legible>"}; tomamos el valor.
        group_name = next(iter(equipo_raw.values()), None) if equipo_raw else None
        jira_key = _extract_jira_key(ticket_id)

        # El agente asignado sigue siendo el mismo que escaló el ticket a
        # Jira, así que lo buscamos igual que en la rama no-escalada para
        # poder etiquetarlo en el mensaje de Slack.
        assigned_id = incident.get("assigned_id")
        name, email = None, None
        if assigned_id:
            user = _get("user", {"id": assigned_id})
            if user:
                name = user.get("name") or user.get("username")
                email = user.get("email")

        return name, email, group_name, jira_key, False, None, None

    # No escalado: comportamiento equivalente al de Zendesk (agente asignado)
    assigned_id = incident.get("assigned_id")
    group_id = incident.get("assigned_group_id")

    name, email = None, None
    if assigned_id:
        user = _get("user", {"id": assigned_id})
        if user:
            name = user.get("name") or user.get("username")
            email = user.get("email")

    group_name = None
    if not name and group_id:
        group = _get("groups", {"id": group_id})
        if group:
            items = group if isinstance(group, list) else [group]
            if items:
                group_name = items[0].get("name")

    return name, email, group_name, None, False, None, None


def _get_resolver_name(incident):
    """
    Determina quién resolvió el ticket.

    Confirmado con un caso real: esta instancia de InvGate no tiene un
    campo separado de "resuelto por" en el incidente — se usa el
    'assigned_id', que al momento del cierre sigue apuntando al agente
    que lo resolvió.
    """
    resolver_id = incident.get("assigned_id")
    if not resolver_id:
        return None, None

    user = _get("user", {"id": resolver_id})
    if not user:
        return None, None
    name = user.get("name") or user.get("username")
    email = user.get("email")
    return name, email


def _log(message):
    print(f"[invgate] {message}")


def add_tag_to_ticket(ticket_id, tag="ticket_priorizado"):
    """
    InvGate no tiene un recurso de "tags" como Zendesk. Como equivalente,
    esto agrega una nota interna (no visible para el cliente) al ticket.

    Requiere INVGATE_BOT_AUTHOR_ID (ID del usuario/agente que InvGate va
    a mostrar como autor de la nota).
    """
    author_id = os.getenv("INVGATE_BOT_AUTHOR_ID")
    if not author_id:
        return False

    data = _post(
        "incident.comment",
        {
            "request_id": ticket_id,
            "author_id": int(author_id),
            "comment": f"[bot] {tag}",
            "customer_visible": 0,
        },
    )
    return bool(data and data.get("status") == "OK")