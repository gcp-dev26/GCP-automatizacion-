"""Handler del botón de cobranza en la página '🔓 Gestión de Cobros' (ex 'CRM Comercial').

Cobranza de honorarios por servicios contables. A diferencia de F29/RRHH/Tickets
(donde el remitente sale del asesor asignado a la fila), aquí el remitente es
FIJO: finanzas@inversoragcp.com (registrado como remitente virtual 'Finanzas' en
asesores_smtp.json). Es el único correo que envía desde esta página.

- Destinatario: el cliente (columna 'email', tipo email, en minúscula).
- Contenido: recordatorio de pago + monto (columna 'Honorario del mes',
  opcional) + la cuenta bancaria de GCP (constante BANCO_GCP_* de email_sender).
- Adjuntos: los archivos de la columna 'Archivos y multimedia' (ej. la factura)
  van adjuntos al correo (opcional; descarga y tope en email_sender).
- Identificación de la fila: por 'Sw' (title) — ver el router en app.py.
- Avisos de error: didáctico a Carlos + técnico al admin (doc 24 §8 y doc 32).

Ver doc 32.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
import notion_client as nc
import email_sender as es
import alertas

log = logging.getLogger("auditai")

# Data source de CRM Comercial (verificado por API, ver doc 32)
DS_ID = "2961d0d2-de59-4fd5-b340-8930f6275101"

# Remitente FIJO de esta página (entrada 'Finanzas' en asesores_smtp.json).
REMITENTE = "Finanzas"
# Destinatario del aviso DIDÁCTICO ante un fallo (el técnico va al admin). El
# usuario pidió "Admin + Carlos": Carlos gestiona esta cobranza. Ver doc 32.
ALERTA_ASESOR = "Carlos Cereceda"

ASUNTO = "Honorarios servicios contables Inversora GCP"

# Columnas (nombres EXACTOS verificados por API — Notion distingue mayúsculas)
SW = "Sw"                            # title = nombre / razón social del cliente
EMAIL_CLIENTE = "email"              # tipo email (minúscula)
TARIFA = "Honorario del mes"         # number → monto de la cobranza (opcional)
ADJUNTOS = "Archivos y multimedia"   # files → se adjuntan al correo (opcional)

# Write-back (columnas nuevas creadas en CRM Comercial, doc 32). 'Fecha envío'
# con e minúscula, igual que RRHH/Tickets (verificar el nombre EXACTO en Notion).
STATUS_COL = "Estado Correo"
STATUS_ENVIADO = "Listo"   # opción del status en CRM (Sin empezar/En curso/Listo), como RRHH
FECHA_COL = "Fecha envío"

MENSAJE_ESTANDAR = (
    "Junto con saludar, adjuntamos al presente correo factura de honorarios "
    "de los servicios contables y tributarios.\n\n"
    "Agradecemos su pronto pago."
)


def _bloque_monto(monto: str) -> tuple[str, str]:
    """Tarjeta 'Total a pagar' con la tarifa mensual. Monto vacío o 0 → ('', ''):
    el correo va sin tarjeta, solo con la cuenta bancaria."""
    try:
        n = float(monto)
    except (ValueError, TypeError):
        n = 0.0
    if n <= 0:
        return "", ""
    m = es.clp(monto)
    html = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 18px 0;"><tr><td style="background:#0B1F3A;border-radius:14px;'
        'padding:22px 26px;"><div style="font-size:12px;font-weight:700;letter-spacing:.12em;'
        'text-transform:uppercase;color:#8fb4ee;margin-bottom:8px;">Total a pagar</div>'
        f'<div style="font-size:40px;font-weight:800;color:#ffffff;line-height:1;'
        f'font-variant-numeric:tabular-nums;">{m}</div></td></tr></table>'
    )
    txt = f"Total a pagar: {m}"
    return html, txt


# Cuenta bancaria en caja dorada (mismo estilo cálido que el bloque de honorarios
# del F29), como prefirió el usuario. Exclusiva del correo de Finanzas.
_BANCO_CARD_HTML = (
    '<div style="margin:0 0 18px 0;background:#fff8ec;border:1px solid #f2e2bf;'
    'border-left:4px solid #E7A100;border-radius:12px;padding:18px 22px;">'
    '<div style="font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
    'color:#9a7400;margin-bottom:10px;">Datos para la transferencia</div>'
    '<div style="font-size:14px;color:#3a4658;line-height:1.9;">'
    f'{es.BANCO_GCP_HTML}</div></div>'
)
_BANCO_TXT = es.BANCO_GCP_TXT


def procesar(page_id: str) -> dict:
    """Lee la fila, envía la cobranza desde Finanzas y escribe el write-back.
    Devuelve {'ok': bool, 'remitente': str, 'motivo': str (si falla)}.
    No loguea PII (email, monto, nombre del cliente)."""
    props = nc.get_page(page_id)["properties"]
    cliente = nc.plain(props.get(SW, {}))
    email = nc.plain(props.get(EMAIL_CLIENTE, {}))
    monto = nc.plain(props.get(TARIFA, {}))
    adjuntos = nc.files(props.get(ADJUNTOS, {}))

    log.info("crm page_id=%s cliente_present=%s email_present=%s monto_present=%s adjuntos_n=%d",
             page_id, bool(cliente), bool(email), bool(monto), len(adjuntos))

    def _alertar(motivo: str):
        # Didáctico → Carlos; técnico → admin (ADMIN_ALERT_EMAIL). Ver doc 32.
        alertas.avisar_fallo_asesor(ALERTA_ASESOR, cliente, "", motivo,
                                    flujo="crm", page_id=page_id)

    if not email:
        motivo = "La fila no tiene correo (columna 'email' vacía). El correo NO se envió."
        _alertar(motivo)
        return {"ok": False, "motivo": "fila sin email"}
    if not cliente:
        motivo = "La fila no tiene 'Sw' (nombre del cliente), necesario para el saludo."
        _alertar(motivo)
        return {"ok": False, "motivo": "fila sin Sw"}

    b_monto = _bloque_monto(monto)
    t_html = es._escape(MENSAJE_ESTANDAR).replace("\n", "<br>")
    extra_vars = {
        "bloque_mensaje": (
            '<p style="margin:0 0 18px 0;font-size:15px;line-height:1.6;color:#3a4658;">'
            f'{t_html}</p>'
        ),
        "linea_mensaje": MENSAJE_ESTANDAR,
        "bloque_monto": b_monto[0],
        "linea_monto": b_monto[1],
        "bloque_banco": _BANCO_CARD_HTML,
        "linea_banco": _BANCO_TXT,
    }

    try:
        remitente = es.enviar(
            destinatario=email,
            nombre=cliente,
            mes="",            # CRM no usa periodo/fecha límite
            monto="0",         # el monto va por extra_vars (tarjeta), no por el flujo F29
            nombre_asesor=REMITENTE,   # remitente FIJO = Finanzas
            adjuntos=adjuntos,
            template="crm_email",
            asunto=ASUNTO,
            extra_vars=extra_vars,
            custom_args={"page_id": page_id, "flujo": "crm"},
        )
        log.info("correo crm enviado OK · page_id=%s remitente=%s", page_id, remitente)
    except ValueError as exc:
        log.error("error envio crm · page_id=%s · %s", page_id, exc)
        _alertar(str(exc))
        return {"ok": False, "motivo": str(exc)}
    except Exception as exc:
        log.error("error SMTP crm · page_id=%s · %s", page_id, exc)
        motivo = f"error SMTP: {exc}"
        _alertar(motivo)
        return {"ok": False, "motivo": motivo}

    # Write-back tolerante: solo columnas presentes en la fila (doc 24 §5.1). Un
    # PATCH con una propiedad inexistente falla completo y tumbaría el status.
    now_iso = datetime.now(timezone.utc).isoformat()
    updates = {}
    if STATUS_COL in props:
        updates[STATUS_COL] = {"status": {"name": STATUS_ENVIADO}}
    else:
        log.warning("columna %r no existe en CRM; no se escribe status", STATUS_COL)
    if FECHA_COL in props:
        updates[FECHA_COL] = {"date": {"start": now_iso}}
    else:
        log.warning("columna %r no existe en CRM; no se escribe fecha", FECHA_COL)
    if updates:
        try:
            nc.update_props(page_id, updates)
            log.info("write-back OK (%s) · page_id=%s", ", ".join(updates), page_id)
        except Exception as exc:
            log.warning("no se pudo actualizar status/fecha · page_id=%s · %s", page_id, exc)

    return {"ok": True, "remitente": remitente}
