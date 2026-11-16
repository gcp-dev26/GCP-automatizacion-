"""Compone y envía el correo del F29.

Envío: API HTTPS de SendGrid si hay SENDGRID_API_KEY (nube/Render, que bloquea
SMTP); si no, SMTP de Gmail con App Password (local, ver asesores_smtp.json).

Carga la plantilla de email_templates/ y reemplaza {{marcadores}}.
Contenido: monto (Impuestos) + fecha límite + honorarios (con datos de
transferencia) + info adicional opcional.

El remitente es el asesor asignado al cliente (columna Adviser Accounting),
que debe estar verificado en SendGrid (Single Sender o dominio).
"""
from __future__ import annotations
import os
import smtplib
import ssl
import datetime
import json
import re
import base64
import mimetypes
import requests
from http_util import request_con_reintentos
from html import escape as _escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

LOGO_PATH = Path(__file__).parent.parent / "LOGO-GCP.png"

TEMPLATES = Path(__file__).parent / "email_templates"
ASESORES_JSON = Path(__file__).parent / "asesores_smtp.json"
# Copia local CON credenciales (gitignored). El versionado va SIN passwords (Fase 0, doc 27).
ASESORES_JSON_LOCAL = Path(__file__).parent / "asesores_smtp.local.json"
ASUNTO_BASE = "Resumen impuestos"   # el asunto se completa con el mes: "Resumen impuestos Junio"

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}
_MI = {v: k for k, v in _MESES.items()}
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

FERIADOS_CL = {
    2026: {
        "2026-01-01", "2026-04-03", "2026-04-04", "2026-05-01", "2026-05-21",
        "2026-06-21", "2026-06-29", "2026-07-16", "2026-08-15", "2026-09-18",
        "2026-09-19", "2026-10-12", "2026-10-31", "2026-11-01", "2026-12-08",
        "2026-12-25",
    },
}

# dev@ es el buzón de MONITOREO: recibe copia de TODO (BCC de cada correo a
# cliente, las confirmaciones de entrega y los errores) como respaldo y para
# supervisar el rendimiento del sistema. Ver admin_emails() y las confirmaciones.
MONITOR_EMAIL = "dev@inversoragcp.com"

# Contacto de soporte que se le muestra AL ASESOR en los avisos de error, para que
# sepa a quién escribirle cuando no lo puede resolver solo (pedido del 06-ago-2026).
# Los avisos dicen "no responder" —salen de una casilla automática— así que sin
# esto el asesor se quedaba sin salida cuando el diagnóstico no le alcanzaba.
SOPORTE_EMAIL = MONITOR_EMAIL
SOPORTE_WSP = "+56 9 8120 0756"
SOPORTE_WSP_URL = "https://wa.me/56981200756"   # sin espacios ni '+', formato wa.me

# BCC oculto en cada correo a cliente = SOLO dev@ (monitoreo). Carlos se sacó del
# respaldo (28-jul): ya NO recibe copia de los correos de otros asesores. Cada
# asesor (Carlos incluido) sigue recibiendo la confirmación de entrega de SUS
# propios envíos por el flujo por-asesor (avisar_entrega_ok_asesor), no por BCC.
BCC_EXTRA = [
    MONITOR_EMAIL,
]

# Cuenta de Gcp Patrimonio Estratégico (BCI): la usan los clientes F29 con
# categoría '27 Bis' o 'Cumplimiento' (columna 'asignacion ' del Contable), en
# vez de la Santander de Inversora GCP. Pedido del 16-nov-2026; ver doc 23.
CUENTA_PATRIMONIO = "patrimonio"
BANCO_PATRIMONIO_HTML = (
    "Banco Bci · Cuenta Corriente en pesos<br>N° 70839202<br>"
    "RUT: 78.456.673-0<br>Razón Social: Gcp Patrimonio Estratégico Limitada"
)
BANCO_PATRIMONIO_TXT = (
    "Banco Bci, Cuenta Corriente en pesos N° 70839202, "
    "RUT 78.456.673-0, Razón Social Gcp Patrimonio Estratégico Limitada"
)
EMAIL_COMPROBANTE_PATRIMONIO = "finanzas@inversoragcp.com"

PREVIRED_URL = "https://www.previred.com/wPortal/login/login.jsp"

BANCO_GCP_HTML = (
    "Banco Santander · Cuenta Corriente<br>N° 0-000-8577678-9<br>"
    "RUT: 76.976.672-3<br>Razón Social: Inversora GCP Ltda"
)
BANCO_GCP_TXT = (
    "Banco Santander, Cuenta Corriente N° 0-000-8577678-9, "
    "RUT 76.976.672-3, Razón Social Inversora GCP Ltda"
)


def _norm(s: str) -> str:
    """Normaliza un nombre para matching: minúsculas, sin tildes, sin espacios extra."""
    import unicodedata
    s = (s or "").lower().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


# Una (1) dirección individual: sin espacios ni separadores de lista, un solo
# @ y dominio con punto. Se aplica a CADA dirección de la celda por separado.
_EMAIL_VALIDO_RE = re.compile(r"^[^@\s;,/]+@[^@\s;,/]+\.[A-Za-z]{2,}$")
# Separadores admitidos en la celda Email para listar varias direcciones:
# coma, punto y coma, DOS PUNTOS, barra y espacios (asi soporta lo que ya escriben
# los asesores, ej. Hydroming: "a@x.cl b@y.cl / cobranza@x.cl").
#
# Los dos puntos se agregaron el 06-ago-2026: en el teclado latino ';' y ':' son la
# MISMA tecla, y un resbalon de shift dejaba el primer token como "correo@dominio.cl:"
# (invalido), asi que el correo no salia y el asesor solo veia un error generico
# (caso SOCIAL UP / Andrea). Un ':' nunca es parte de una direccion valida, asi que
# tratarlo como separador no admite nada que antes se rechazara con razon.
_SEP_DEST_RE = re.compile(r"[,;:/\s]+")


def parse_destinatarios(raw: str) -> list[str]:
    """Divide la celda Email en direcciones individuales (sin validar formato).
    Sirve tanto para el envío como para el filtro anti-BCC del webhook: cualquier
    dirección de la celda cuenta como destinatario legítimo del cliente."""
    return [x for x in _SEP_DEST_RE.split((raw or "").strip()) if x]


def _validar_destinatarios(raw: str) -> list[str]:
    """Valida la columna Email ANTES de llamar a SendGrid y devuelve la lista de
    direcciones (la 1ra es el 'to', el resto van en CC). Deduplica sin perder el
    orden. Lanza ValueError si no hay ninguna o si ALGUNA no es un correo válido.

    Notion no valida su columna Email: si la celda trae otro dato (visto en
    producción: un RUT + usuario SII), SendGrid rechaza con un 400 críptico en
    inglés y de paso recibe ese dato sensible. Acá se corta antes, con un motivo
    claro para el aviso al asesor. El mensaje NO incluye el valor de la celda
    porque el motivo termina en los logs (política sin PII)."""
    tokens = parse_destinatarios(raw)
    if not tokens:
        raise ValueError(
            "La columna Email de la fila está vacía o no contiene una direccion de correo. "
            "Corrige la celda Email en Notion y vuelve a apretar el boton. El correo NO se envio."
        )
    for t in tokens:
        if not _EMAIL_VALIDO_RE.match(t):
            raise ValueError(
                "La columna Email de la fila contiene un valor que no es una direccion de correo "
                "valida (parece otro dato: un RUT, un usuario, o texto suelto). Si son varios "
                "correos, sepáralos con coma. Corrige la celda Email en Notion y vuelve a apretar "
                "el boton. El correo NO se envio."
            )
    seen, out = set(), []
    for t in tokens:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _cargar_asesores() -> dict:
    """Carga el directorio de asesores (y sus credenciales SMTP si las hay).

    Orden de prioridad (Fase 0 del plan de robustez, doc 27):
    1. Env var ASESORES_SMTP_JSON (Render/Cloud Run) — el JSON completo como string.
    2. asesores_smtp.local.json (gitignored) — copia local CON passwords, solo dev.
    3. asesores_smtp.json (versionado) — SIN passwords; en la nube basta porque
       el envío va por SendGrid API (no usa SMTP).
    Así el repo nunca lleva credenciales y el fallback SMTP local sigue funcionando.
    """
    raw = os.environ.get("ASESORES_SMTP_JSON")
    if raw:
        return json.loads(raw)
    if ASESORES_JSON_LOCAL.is_file():
        with open(ASESORES_JSON_LOCAL, encoding="utf-8") as f:
            return json.load(f)
    with open(ASESORES_JSON, encoding="utf-8") as f:
        return json.load(f)


def _buscar_asesor_por_nombre(nombre_asesor: str) -> dict | None:
    """Busca el asesor en asesores_smtp.json por nombre normalizado.
    Devuelve el dict del asesor o None si no lo encuentra."""
    data = _cargar_asesores()
    norm = _norm(nombre_asesor)
    for email, info in data.items():
        if email.startswith("_"):
            continue
        if info.get("nombre_norm") == norm:
            return {"email": email, **info}
    return None


def clp(monto: str | int | float) -> str:
    try:
        n = int(round(float(monto)))
        return "$" + f"{n:,}".replace(",", ".")
    except (ValueError, TypeError):
        return "$0"


_MONEY_RE = re.compile(r"\$\s?\d[\d.]*")
_VARS_RE = re.compile(
    r"(remanente|saldo a favor|crédito fiscal|débito fiscal|impuesto único|"
    r"retención|reajuste|multa|interés|PPM)",
    re.IGNORECASE,
)


def _resaltar(texto: str) -> str:
    """Escapa HTML y pone en negrita los montos ($…) y las variables clave
    (remanente, saldo a favor, etc.) del texto libre de 'Info Adicional'."""
    t = _escape(texto)
    t = _MONEY_RE.sub(lambda m: f"<b>{m.group(0)}</b>", t)
    t = _VARS_RE.sub(lambda m: f"<b>{m.group(0)}</b>", t)
    return t


def _es_habil(d: datetime.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in FERIADOS_CL.get(d.year, set())


def fecha_limite(periodo: str) -> datetime.date | None:
    p = periodo.strip().lower().split()
    mes = next((_MESES[x] for x in p if x in _MESES), None)
    anio = next((int(x) for x in p if x.isdigit() and len(x) == 4), None)
    if not mes or not anio:
        return None
    m2, a2 = (mes + 1, anio) if mes < 12 else (1, anio + 1)
    d = datetime.date(a2, m2, 20)
    while not _es_habil(d):
        d += datetime.timedelta(days=1)
    return d


def fecha_limite_rrhh(periodo: str) -> datetime.date | None:
    """'Junio 2026' → día 13 del mes SIGUIENTE; si no es hábil,
    se traslada al siguiente día hábil. Siempre a las 13:45."""
    p = periodo.strip().lower().split()
    mes = next((_MESES[x] for x in p if x in _MESES), None)
    anio = next((int(x) for x in p if x.isdigit() and len(x) == 4), None)
    if not mes or not anio:
        return None
    m2, a2 = (mes + 1, anio) if mes < 12 else (1, anio + 1)
    d = datetime.date(a2, m2, 13)
    while not _es_habil(d):
        d += datetime.timedelta(days=1)
    return d


def fecha_larga(d: datetime.date) -> str:
    return f"{_DIAS[d.weekday()]} {d.day} de {_MI[d.month]} de {d.year}"


def _mes_nombre(periodo: str) -> str:
    """Nombre del mes (capitalizado) del periodo. 'Junio 2026' -> 'Junio'. '' si no se reconoce."""
    for p in (periodo or "").strip().lower().split():
        if p in _MESES:
            return p.capitalize()
    return ""


def _variantes(monto_str: str, periodo: str) -> tuple[str, str]:
    try:
        n = float(monto_str)
    except (ValueError, TypeError):
        n = 0.0
    if n > 0:
        return "Impuesto a pagar", ""
    if n < 0:
        return "Saldo a favor", "Este mes no paga IVA: el saldo queda a su favor y se arrastra al próximo período."
    return "Sin pago este mes", f"Su Formulario 29 del período {periodo} se declara sin movimiento; este mes no paga IVA."


def _bloque_fecha(d: datetime.date) -> tuple[str, str]:
    f = fecha_larga(d)
    html = (
        f'<p style="margin:0 0 18px 0;font-size:14px;line-height:1.6;color:#3a4658;">'
        f'<b>Fecha límite de pago:</b> {f}.</p>'
    )
    txt = f"Fecha límite de pago: {f}."
    return html, txt


def _bloque_fecha_rrhh(d: datetime.date) -> tuple[str, str]:
    f = fecha_larga(d)
    html = (
        f'<p style="margin:0 0 8px 0;font-size:14px;line-height:1.6;color:#3a4658;">'
        f'<b>Plazo hasta</b> {f} a las 13.45 horas.</p>'
        f'<p style="margin:0 0 18px 0;font-size:14px;line-height:1.6;color:#3a4658;">'
        f'Puede pagar directamente en la página de '
        f'<a href="{PREVIRED_URL}" style="color:#0B1F3A;font-weight:700;">Previred</a>.</p>'
    )
    txt = (
        f"Plazo hasta {f} a las 13.45 horas.\n"
        f"Puede pagar directamente en la pagina de Previred: {PREVIRED_URL}"
    )
    return html, txt


def _datos_banco(cuenta: str) -> tuple[str, str]:
    """(html, txt) de la cuenta a la que transfiere el cliente."""
    if cuenta == CUENTA_PATRIMONIO:
        return BANCO_PATRIMONIO_HTML, BANCO_PATRIMONIO_TXT
    return BANCO_GCP_HTML, BANCO_GCP_TXT


def _bloque_cuenta_pago(cuenta: str) -> tuple[str, str]:
    """Recuadro 'Transfiera el impuesto a esta cuenta' (solo cuenta Patrimonio,
    cuando hay impuesto a pagar): esos clientes le transfieren el IVA a GCP."""
    if cuenta != CUENTA_PATRIMONIO:
        return "", ""
    html = (
        '<div style="margin:0 0 18px 0;background:#eef3fb;border:1px solid #cdd8f0;'
        'border-left:4px solid #0B1F3A;border-radius:12px;padding:18px 22px;">'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
        'color:#0B1F3A;margin-bottom:10px;">Datos para transferir el impuesto</div>'
        f'<div style="font-size:14px;color:#3a4658;line-height:1.9;">{BANCO_PATRIMONIO_HTML}</div>'
        '<div style="margin-top:10px;font-size:13px;color:#3a4658;">Enviar comprobante a: '
        f'<a href="mailto:{EMAIL_COMPROBANTE_PATRIMONIO}" style="color:#0B1F3A;font-weight:700;">'
        f'{EMAIL_COMPROBANTE_PATRIMONIO}</a></div></div>'
    )
    txt = (f"Datos para transferir el impuesto: {BANCO_PATRIMONIO_TXT}. "
           f"Enviar comprobante a: {EMAIL_COMPROBANTE_PATRIMONIO}.")
    return html, txt


def _bloque_honorarios(monto_h: str, cuenta: str = "") -> tuple[str, str]:
    try:
        h = float(monto_h)
    except (ValueError, TypeError):
        h = 0.0
    if h <= 0:
        return "", ""
    m = clp(h)
    banco_html, banco_txt = _datos_banco(cuenta)
    html = (
        '<div style="margin:0 0 18px 0;background:#fff8ec;border:1px solid #f2e2bf;'
        'border-left:4px solid #E7A100;border-radius:12px;padding:18px 22px;">'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;'
        'color:#9a7400;margin-bottom:6px;">Honorarios de la asesoría</div>'
        f'<div style="font-size:26px;font-weight:800;color:#9a7400;'
        f'font-variant-numeric:tabular-nums;line-height:1.05;">{m}</div>'
        '<div style="margin-top:12px;padding:12px 14px;background:#fff;border:1px solid #e6d9b8;'
        'border-radius:8px;font-size:13px;color:#3a4658;line-height:1.7;">'
        f'<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
        f'color:#9a7400;margin-bottom:6px;">Datos para la transferencia</div>'
        f'{banco_html}</div></div>'
    )
    txt = (
        f"Honorarios de la asesoría: {m}. "
        f"Datos para la transferencia: {banco_txt}."
    )
    return html, txt


def _combinar_info(valor: str, motivo: str) -> str:
    """Combina valor (numero) + motivo (select) en un texto natural.
    - ambos: 'Remanente: $417.798'
    - solo valor: '$417.798'
    - solo motivo: 'Remanente'
    - ninguno: ''"""
    v = (valor or "").strip()
    m = (motivo or "").strip()
    if v and m:
        return f"{m}: {clp(v)}"
    if v:
        return clp(v)
    if m:
        return m
    return ""


def _bloque_info(valor: str, motivo: str) -> tuple[str, str]:
    """Bloque de informacion adicional. Recibe valor (numero) + motivo (select).
    Si ambos vacios, no se inyecta (devuelve ('', ''))."""
    t = _combinar_info(valor, motivo)
    if not t:
        return "", ""
    t_html = _resaltar(t)
    html = (
        '<div style="margin:0 0 18px 0;background:#f4f6fa;border:1px solid #dfe5ee;'
        'border-radius:10px;padding:14px 18px;">'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
        'color:#5a6b82;margin-bottom:3px;">Información adicional</div>'
        f'<div style="font-size:14px;color:#3a4658;line-height:1.55;">{t_html}</div></div>'
    )
    txt = f"Información adicional: {t}"
    return html, txt


def _bloque_adjuntos(mensaje: str) -> tuple[str, str]:
    """Nota/mensaje del asesor sobre los archivos adjuntos. Vacio -> ('', '')."""
    t = (mensaje or "").strip()
    if not t:
        return "", ""
    t_html = _escape(t).replace("\n", "<br>")
    html = (
        '<div style="margin:0 0 18px 0;background:#eef4ff;border:1px solid #d3e0f5;'
        'border-left:4px solid #0B1F3A;border-radius:10px;padding:14px 18px;">'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
        'color:#3a5a8c;margin-bottom:3px;">Archivos adjuntos</div>'
        f'<div style="font-size:14px;color:#3a4658;line-height:1.55;">{t_html}</div></div>'
    )
    txt = f"Archivos adjuntos: {t}"
    return html, txt


def render(
    nombre: str, periodo: str, monto: str, asesor: str, contacto: str,
    logo_url: str, honorarios: str = "", info_valor: str = "", info_motivo: str = "",
    msg_adjuntos: str = "", firma_html: str = "",
    template: str = "f29_email",
    titulo_override: str = "", mensaje_override: str = "",
    bloque_fecha_override: str = "", linea_fecha_override: str = "",
    extra_vars: dict | None = None,
    cuenta: str = "",
) -> tuple[str, str]:
    """Carga la plantilla y reemplaza los marcadores. Devuelve (html, txt).
    `cuenta`: '' (Inversora GCP, Santander) o CUENTA_PATRIMONIO (BCI)."""
    if titulo_override:
        titulo, msg = titulo_override, mensaje_override
    else:
        titulo, msg = _variantes(monto, periodo)
    try:
        n = float(monto)
    except (ValueError, TypeError):
        n = 0.0

    if bloque_fecha_override:
        b_fecha = (bloque_fecha_override, linea_fecha_override)
    else:
        b_fecha = ("", "")
        if n > 0:
            d = fecha_limite(periodo)
            if d:
                b_fecha = _bloque_fecha(d)
            # Cuenta Patrimonio: el recuadro de transferencia va pegado a la
            # fecha límite (mismo marcador), así no se tocan las plantillas.
            b_cta = _bloque_cuenta_pago(cuenta)
            b_fecha = (b_fecha[0] + b_cta[0],
                       "\n".join(x for x in (b_fecha[1], b_cta[1]) if x))
    b_hono = _bloque_honorarios(honorarios, cuenta)
    b_info = _bloque_info(info_valor, info_motivo)
    b_adj = _bloque_adjuntos(msg_adjuntos)
    info_texto = _combinar_info(info_valor, info_motivo)

    vars_ = {
        "logo_url": "cid:logo-gcp",
        "bloque_firma": firma_html,
        "nombre_cliente": nombre,
        "periodo": periodo,
        "monto": clp(monto),
        "titulo_resultado": titulo,
        "mensaje_resultado": mensaje_override if mensaje_override else msg,
        "bloque_mensaje_resultado": (
            f'<div style="font-size:14px;color:#c9d6ea;margin-top:10px;line-height:1.5;">{mensaje_override if mensaje_override else msg}</div>'
            if (mensaje_override or msg) else ""
        ),
        "asesor": asesor,
        "contacto_email": contacto,
        "bloque_fecha_limite": b_fecha[0],
        "linea_fecha_limite": b_fecha[1],
        "bloque_honorarios": b_hono[0],
        "linea_honorarios": b_hono[1],
        "bloque_info_adicional": b_info[0],
        "linea_info_adicional": b_info[1],
        "info_adicional_texto": info_texto,
        "bloque_adjuntos": b_adj[0],
        "linea_adjuntos": b_adj[1],
    }
    html_path = TEMPLATES / f"{template}.html"
    txt_path = TEMPLATES / f"{template}.txt"
    if not html_path.is_file():
        html_path = TEMPLATES / "f29_email.html"
    if not txt_path.is_file():
        txt_path = TEMPLATES / "f29_email.txt"
    html = html_path.read_text(encoding="utf-8")
    txt = txt_path.read_text(encoding="utf-8")
    if extra_vars:
        vars_.update({k: (v if isinstance(v, str) else str(v)) for k, v in extra_vars.items()})
    for k, v in vars_.items():
        html = html.replace("{{" + k + "}}", v)
        txt = txt.replace("{{" + k + "}}", v)
    return html, txt


def _firma_texto_html(nombre: str, cargo=None) -> str:
    """Firma de TEXTO del asesor (cuando no tiene firma PNG). Por defecto el
    subtítulo es 'GCP · Asesoría Contable'; si el asesor define 'firma_cargo' en
    asesores_smtp.json (una lista de líneas o un string), esas líneas reemplazan
    el subtítulo genérico (ej. Andrea González: 'Especialista en RRHH' /
    'Gestión y Control Tributario'). Sin firma_cargo, comportamiento idéntico al
    anterior (retrocompatible)."""
    if isinstance(cargo, str):
        cargo = [cargo]
    lineas = [str(l).strip() for l in (cargo or []) if str(l).strip()]
    sub = "<br/>".join(_escape(l) for l in lineas) if lineas else "GCP · Asesoría Contable"
    return (
        f'<p style="margin:0 0 24px 0;font-size:14px;line-height:1.5;">'
        f'<b>{_escape(nombre)}</b><br/>'
        f'<span style="color:#5a6b82;">{sub}</span></p>'
    )


MAX_ADJUNTOS_MB = 15   # tope total de adjuntos (los correos rebotan pasados ~25 MB)


def _descargar_adjuntos(adjuntos):
    """Descarga los archivos de la columna 'Adjuntos' de Notion (URLs firmadas
    temporales) -> [(nombre, bytes, mime)]. Respeta un tope total de tamano;
    si se excede, deja de agregar. No loguea contenido."""
    out, total = [], 0
    tope = MAX_ADJUNTOS_MB * 1024 * 1024
    for a in (adjuntos or []):
        url = (a or {}).get("url")
        nombre = (a or {}).get("name") or "adjunto"
        if not url:
            continue
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except Exception:
            continue
        data = r.content
        if total + len(data) > tope:
            break
        total += len(data)
        mime = mimetypes.guess_type(nombre)[0] or "application/octet-stream"
        out.append((nombre, data, mime))
    return out


def enviar(
    destinatario: str,
    nombre: str,
    mes: str,
    monto: str,
    nombre_asesor: str = "",
    honorarios: str = "",
    info_valor: str = "",
    info_motivo: str = "",
    msg_adjuntos: str = "",
    adjuntos: list | None = None,
    contacto: str | None = None,
    logo_url: str | None = None,
    template: str = "f29_email",
    asunto: str | None = None,
    extra_vars: dict | None = None,
    custom_args: dict | None = None,
    cuenta: str = "",
) -> str:
    """Envía el correo por SMTP de Gmail. El remitente es el asesor del cliente.
    Devuelve el email del remitente usado (para log sin PII del destinatario).

    `destinatario` puede traer VARIAS direcciones (la celda Email de Notion con
    coma/espacio/;///): la 1ra es el 'to' y el resto van en CC (caso Hydroming)."""
    _dests = _validar_destinatarios(destinatario)
    destinatario, cc_dests = _dests[0], _dests[1:]
    contacto = contacto or os.environ.get("EMAIL_CONTACTO", "contacto@gcp.cl")
    logo_url = logo_url or os.environ.get("LOGO_URL", "https://gcp.cl/logo.png")

    # Determinar remitente: el asesor del cliente, o fallback a EMAIL_FROM
    remitente_email = os.environ.get("EMAIL_FROM", "notificaciones@inversoragcp.com")
    remitente_pass = None
    asesor_firma = nombre_asesor or "Equipo GCP"
    asesor_info = None

    if nombre_asesor:
        asesor_info = _buscar_asesor_por_nombre(nombre_asesor)
        if asesor_info and asesor_info.get("pendiente"):
            raise ValueError(
                f"Asesor '{nombre_asesor}' ({asesor_info['email']}) marcado como pendiente. "
                f"No se puede enviar desde su cuenta todavía."
            )
        if asesor_info:
            remitente_email = asesor_info["email"]
            asesor_firma = asesor_info.get("nombre", nombre_asesor)
            remitente_pass = asesor_info.get("password")   # solo lo usa el fallback SMTP local

    # Firma del asesor: imagen inline (CID) si tiene firma_png; si no, texto simple con su nombre.
    # firma_png puede ser ruta absoluta o relativa al paquete notion_automation/ (cloud-ready).
    firma_png = None
    if asesor_info and asesor_info.get("firma_png"):
        p = Path(asesor_info["firma_png"])
        if not p.is_absolute():
            p = Path(__file__).parent / p
        if p.is_file():
            firma_png = str(p)
    if firma_png:
        firma_html = (
            f'<img src="cid:firma-asesor" alt="{asesor_firma}" border="0" '
            f'style="width:600px;max-width:100%;height:auto;display:block;border:none;outline:none;margin:6px 0 8px;">'
        )
    else:
        firma_html = _firma_texto_html(asesor_firma, (asesor_info or {}).get("firma_cargo"))

    if template == "rrhh_email":
        d_rrhh = fecha_limite_rrhh(mes)
        b_fecha_rrhh = _bloque_fecha_rrhh(d_rrhh) if d_rrhh else ("", "")
        html, txt = render(
            nombre, mes, monto, asesor_firma, contacto, logo_url,
            honorarios, info_valor, info_motivo, msg_adjuntos, firma_html,
            template=template,
            titulo_override="Imposiciones a pagar",
            bloque_fecha_override=b_fecha_rrhh[0],
            linea_fecha_override=b_fecha_rrhh[1],
        )
    else:
        html, txt = render(
            nombre, mes, monto, asesor_firma, contacto, logo_url,
            honorarios, info_valor, info_motivo, msg_adjuntos, firma_html,
            template=template,
            extra_vars=extra_vars,
            cuenta=cuenta,
        )

    # Asunto: si se pasa explícito, usarlo; si no, dinámico por mes (F29).
    if asunto:
        asunto_final = asunto
    else:
        mes_nombre = _mes_nombre(mes)
        asunto_final = f"{ASUNTO_BASE} {mes_nombre}" if mes_nombre else ASUNTO_BASE

    # Descargar adjuntos (PDFs de la columna "Adjuntos" de Notion), con tope de tamano.
    adjuntos_bin = _descargar_adjuntos(adjuntos)

    # El remitente (asesor) viaja en custom_args para que el Event Webhook sepa a
    # quien confirmarle la entrega o avisarle el rebote, sin leer columnas por
    # flujo. Solo lo usa el envio SendGrid; el fallback SMTP local lo ignora.
    custom_args = {**(custom_args or {}), "remitente": remitente_email}

    # === Envío ===
    # En la nube (Render BLOQUEA SMTP) se usa la API HTTPS de SendGrid si hay SENDGRID_API_KEY.
    # En local, si no hay key, cae al SMTP de Gmail (requiere App Password del asesor).
    if os.environ.get("SENDGRID_API_KEY"):
        _enviar_via_sendgrid(
            os.environ["SENDGRID_API_KEY"],
            remitente_email, asesor_firma, destinatario, asunto_final, html, txt, firma_png,
            adjuntos_bin, custom_args, cc_dests,
        )
        return remitente_email

    # --- Fallback local: SMTP Gmail (puerto 465, SSL) ---
    if not remitente_pass:
        raise ValueError(
            f"No hay SENDGRID_API_KEY ni App Password SMTP para {remitente_email}. "
            f"Configura SendGrid (nube) o el App Password en asesores_smtp.json (local)."
        )

    # Mensaje "related" para que las imágenes inline (logo + firma) se vean en el cuerpo.
    msg = MIMEMultipart("related")
    msg["Subject"] = asunto_final
    msg["From"] = f"{asesor_firma} · GCP <{remitente_email}>"
    msg["To"] = destinatario
    if cc_dests:
        msg["Cc"] = ", ".join(cc_dests)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(txt, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    if LOGO_PATH.is_file():
        with open(LOGO_PATH, "rb") as fh:
            _logo = MIMEImage(fh.read(), _subtype="png")
        _logo.add_header("Content-ID", "<logo-gcp>")
        _logo.add_header("Content-Disposition", "inline", filename="logo-gcp.png")
        msg.attach(_logo)

    if firma_png:
        _sub = "jpeg" if firma_png.lower().endswith((".jpg", ".jpeg")) else "png"
        with open(firma_png, "rb") as fh:
            _firma = MIMEImage(fh.read(), _subtype=_sub)
        _firma.add_header("Content-ID", "<firma-asesor>")
        _firma.add_header("Content-Disposition", "inline", filename=f"firma-asesor.{_sub}")
        msg.attach(_firma)

    # Adjuntos (PDFs) descargados de Notion
    for _name, _data, _mime in adjuntos_bin:
        _maintype, _, _subt = _mime.partition("/")
        _part = MIMEBase(_maintype or "application", _subt or "octet-stream")
        _part.set_payload(_data)
        encoders.encode_base64(_part)
        _part.add_header("Content-Disposition", "attachment", filename=_name)
        msg.attach(_part)

    pass_clean = remitente_pass.replace(" ", "")
    # Sobre real: destinatario + CC visibles + BCC (asesor + BCC_EXTRA) ocultos.
    # Se deduplica por si un CC coincide con el asesor/Carlos.
    _ya = {destinatario.lower()}
    rcpt = [destinatario]
    for _cc in cc_dests:
        if _cc.lower() not in _ya:
            _ya.add(_cc.lower())
            rcpt.append(_cc)
    for _bcc in [remitente_email, *BCC_EXTRA]:
        if _bcc.lower() not in _ya:
            _ya.add(_bcc.lower())
            rcpt.append(_bcc)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(remitente_email, pass_clean)
        server.sendmail(remitente_email, rcpt, msg.as_string())

    return remitente_email


def _enviar_via_sendgrid(api_key, remitente_email, asesor_firma, destinatario,
                         asunto, html, txt, firma_png, adjuntos_bin=None,
                         custom_args=None, cc=None):
    """Envía el correo por la API HTTPS de SendGrid (Render bloquea SMTP).
    Logo + firma van como adjuntos inline (content_id) para verse en el cuerpo.
    El remitente (remitente_email) DEBE estar verificado en SendGrid (Single
    Sender o dominio), si no SendGrid responde 403.
    `cc`: direcciones adicionales del cliente en copia visible (caso Hydroming)."""
    attachments = []

    def _inline(path, cid, filename, ctype):
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        attachments.append({
            "content": b64, "type": ctype, "filename": filename,
            "disposition": "inline", "content_id": cid,
        })

    if LOGO_PATH.is_file():
        _inline(LOGO_PATH, "logo-gcp", "logo-gcp.png", "image/png")
    if firma_png:
        ctype = "image/jpeg" if firma_png.lower().endswith((".jpg", ".jpeg")) else "image/png"
        _inline(firma_png, "firma-asesor", "firma-asesor", ctype)

    # Adjuntos (PDFs) — disposition "attachment" (no inline)
    for _name, _data, _mime in (adjuntos_bin or []):
        attachments.append({
            "content": base64.b64encode(_data).decode("ascii"),
            "type": _mime, "filename": _name, "disposition": "attachment",
        })

    # CC visible: direcciones extra del cliente (celda Email con varios correos),
    # sin repetir el destinatario principal.
    cc_clean, _vistos = [], {destinatario.lower()}
    for _cc in (cc or []):
        if _cc.lower() not in _vistos:
            _vistos.add(_cc.lower())
            cc_clean.append(_cc)

    # BCC al asesor + extra (BCC_EXTRA): copia oculta. SendGrid exige que
    # to/cc/bcc no compartan direcciones, por eso se excluyen las ya usadas.
    bcc_set = set()
    bcc_set.add(remitente_email.lower())
    for _bcc in BCC_EXTRA:
        bcc_set.add(_bcc.lower())
    bcc_set -= _vistos   # fuera destinatario y cualquier CC ya incluido
    personalization = {"to": [{"email": destinatario}]}
    if cc_clean:
        personalization["cc"] = [{"email": e} for e in cc_clean]
    if bcc_set:
        personalization["bcc"] = [{"email": e} for e in sorted(bcc_set)]

    payload = {
        "personalizations": [personalization],
        "from": {"email": remitente_email, "name": f"{asesor_firma} · GCP"},
        "subject": asunto,
        "content": [
            {"type": "text/plain", "value": txt},
            {"type": "text/html", "value": html},
        ],
    }
    if attachments:
        payload["attachments"] = attachments

    # custom_args {page_id, flujo}: SendGrid los devuelve tal cual en cada
    # evento del Event Webhook (doc 30) — así el evento delivered/bounce vuelve
    # con la fila de Notion exacta a actualizar. Valores SIEMPRE string.
    if custom_args:
        payload["custom_args"] = {str(k): str(v) for k, v in custom_args.items()}

    # Reintentos ante 5xx/429/timeout transitorios de SendGrid (Fase 2, H6): un
    # hipo no debe perder el correo. El 403 (remitente no verificado) NO se
    # reintenta (es 4xx definitivo) -> cae directo al ValueError de abajo.
    r = request_con_reintentos(
        "POST",
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    if r.status_code not in (200, 202):
        raise ValueError(f"SendGrid rechazo el envio (HTTP {r.status_code}): {r.text[:300]}")


def admin_emails() -> list[str]:
    """Correos admin desde ADMIN_ALERT_EMAIL (separados por coma) + el buzón de
    monitoreo dev@ (SIEMPRE incluido, aunque la env no esté seteada). Así dev@
    recibe todos los errores/avisos técnicos. Usado por enviar_aviso_dev y por
    alertas.avisar_excepcion_admin / avisar_resumen_reconciliacion."""
    raw = os.environ.get("ADMIN_ALERT_EMAIL", "")
    envs = [e.strip() for e in raw.split(",") if e.strip()]
    out: list[str] = []
    for e in [*envs, MONITOR_EMAIL]:
        if e.lower() not in {x.lower() for x in out}:
            out.append(e)
    return out


def _post_aviso(destinatarios: list[str], asunto: str, texto: str, html: str,
                cc: list[str] | None = None) -> bool:
    """Envía un aviso simple por la API de SendGrid (sin adjuntos ni inline).
    Best-effort: nunca lanza; devuelve False si falta config o falla la red."""
    try:
        api_key = os.environ.get("SENDGRID_API_KEY")
        to = [d for d in (destinatarios or []) if d]
        if not (api_key and to):
            return False
        remitente = os.environ.get("EMAIL_FROM", "notificaciones@inversoragcp.com")
        personalization = {"to": [{"email": d} for d in to]}
        cc_clean = [c for c in (cc or []) if c and c.lower() not in {d.lower() for d in to}]
        if cc_clean:
            personalization["cc"] = [{"email": c} for c in cc_clean]
        payload = {
            "personalizations": [personalization],
            "from": {"email": remitente, "name": "AuditAI · Avisos"},
            "subject": asunto,
            "content": [
                {"type": "text/plain", "value": texto},
                {"type": "text/html", "value": html},
            ],
        }
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        return r.status_code in (200, 202)
    except Exception:
        return False


def _lista_html(pasos: list[str]) -> str:
    items = "".join(f'<li style="margin:0 0 6px 0;">{_escape(p)}</li>' for p in pasos)
    return f'<ol style="margin:8px 0 0 0;padding-left:20px;color:#3a4658;font-size:14px;line-height:1.6;">{items}</ol>'


def _bloque_soporte_html() -> str:
    """Recuadro de contacto para el asesor en los avisos de error: a quién escribirle
    cuando el diagnóstico no le alcanza. El aviso sale de una casilla que no se
    responde, así que sin esto el asesor quedaba sin salida."""
    return (
        '<div style="margin:14px 0 0;padding:12px 14px;background:#f4f7fb;'
        'border:1px solid #dbe4f0;border-radius:10px;">'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.06em;'
        'text-transform:uppercase;color:#5a6b82;margin-bottom:6px;">¿Dudas o sigue sin funcionar?</div>'
        '<p style="margin:0;font-size:13px;line-height:1.7;color:#3a4658;">'
        f'Correo: <a href="mailto:{SOPORTE_EMAIL}" style="color:#0B1F3A;font-weight:700;">{SOPORTE_EMAIL}</a><br>'
        f'WhatsApp: <a href="{SOPORTE_WSP_URL}" style="color:#0B1F3A;font-weight:700;">{SOPORTE_WSP}</a>'
        '</p></div>'
    )


def enviar_aviso_asesor(email_asesor: str, cliente: str, mes: str, diag) -> bool:
    """Aviso DIDÁCTICO al asesor (contador) cuando un correo no se pudo enviar.
    Lenguaje simple, sin tecnicismos, con pasos accionables en Notion. `diag` es
    un diagnostico.Diagnostico. Best-effort: nunca lanza."""
    puede = diag.puede_asesor
    chip = ("✅ Esto lo puedes resolver tú" if puede
            else "🔧 Esto lo resuelve el equipo técnico")
    chip_bg = "#eaf7ee" if puede else "#eef2fb"
    chip_bd = "#bfe3c8" if puede else "#cdd8f0"
    chip_fg = "#1d7a3a" if puede else "#33489a"
    # Asunto = el titular del diagnóstico (dice QUÉ pasó, ej. "El correo de X
    # salió pero NO le llegó al cliente"), para que el asesor lo entienda sin
    # abrir. "Acción requerida" solo si él puede resolverlo; si no, "Aviso".
    prefijo = "Acción requerida" if puede else "Aviso"
    asunto = f"{prefijo}: {diag.titulo}"

    texto = (
        f"{diag.titulo}\n\n"
        f"{'Lo puedes resolver tú.' if puede else 'Lo resuelve el equipo técnico.'}\n\n"
        f"{diag.explicacion_asesor}\n\n"
        "Qué hacer:\n" + "".join(f"  {i}. {p}\n" for i, p in enumerate(diag.pasos_asesor, 1))
        + f"\n¿Sigue sin funcionar o tienes dudas? Escríbenos:\n"
          f"  Correo:   {SOPORTE_EMAIL}\n"
          f"  WhatsApp: {SOPORTE_WSP}\n"
        + "\n— Aviso automatico del sistema AuditAI (no responder a este correo)."
    )
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;color:#0B1F3A;">'
        f'<div style="display:inline-block;background:{chip_bg};border:1px solid {chip_bd};'
        f'color:{chip_fg};font-size:12px;font-weight:700;border-radius:999px;padding:5px 12px;margin-bottom:12px;">{chip}</div>'
        '<div style="background:#fff8f6;border:1px solid #f3d6cf;border-left:4px solid #d9663f;'
        'border-radius:12px;padding:18px 22px;">'
        f'<div style="font-size:17px;font-weight:800;margin:0 0 8px 0;">{_escape(diag.titulo)}</div>'
        f'<p style="margin:0 0 14px 0;font-size:14px;line-height:1.6;color:#3a4658;">{_escape(diag.explicacion_asesor)}</p>'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#9a4a2a;">Qué hacer</div>'
        f'{_lista_html(diag.pasos_asesor)}'
        '</div>'
        f'<p style="margin:14px 2px 0;font-size:13px;color:#5a6b82;">Cliente: <b>{_escape(cliente)}</b> · Período: <b>{_escape(mes or "—")}</b></p>'
        f'{_bloque_soporte_html()}'
        '<p style="color:#8593a8;font-size:12px;margin-top:6px;">Aviso automatico del sistema AuditAI (no responder a este correo).</p>'
        '</div>'
    )
    return _post_aviso([email_asesor], asunto, texto, html)


def enviar_confirmacion_entrega_asesor(email_asesor: str, cliente: str, periodo: str = "",
                                       flujo: str = "", fecha: str = "") -> bool:
    """Confirmación POSITIVA al asesor: el correo SÍ le llegó al cliente (evento
    'delivered' de SendGrid). Es la contraparte de enviar_aviso_asesor (que avisa
    los fallos): así el asesor recibe por correo tanto el «llegó» como el «no
    llegó», sin tener que abrir Notion. Best-effort: nunca lanza."""
    cuando = f" el {fecha}" if fecha else ""
    per_html = f" · Período: {_escape(periodo)}" if periodo else ""
    asunto = f"✅ Le llegó al cliente: correo de {cliente}" + (f" ({periodo})" if periodo else "")
    texto = (
        f"Buenas noticias: el correo de {cliente} fue ENTREGADO en el buzón del cliente{cuando}.\n\n"
        f"Cliente: {cliente}" + (f"\nPeríodo: {periodo}" if periodo else "") + "\n\n"
        "Esto confirma que el correo LLEGÓ (no necesariamente que lo haya abierto). "
        "En Notion, la columna 'Entrega Correo' de la fila también lo muestra.\n\n"
        "— Confirmación automática del sistema AuditAI (no responder)."
    )
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;color:#0B1F3A;">'
        '<div style="display:inline-block;background:#eaf7ee;border:1px solid #bfe3c8;'
        'color:#1d7a3a;font-size:12px;font-weight:700;border-radius:999px;padding:5px 12px;margin-bottom:12px;">✅ Correo entregado</div>'
        '<div style="background:#f2fbf5;border:1px solid #cdeed7;border-left:4px solid #1c7c4a;'
        'border-radius:12px;padding:18px 22px;">'
        f'<div style="font-size:17px;font-weight:800;margin:0 0 8px 0;">El correo de {_escape(cliente)} le llegó al cliente</div>'
        f'<p style="margin:0;font-size:14px;line-height:1.6;color:#3a4658;">Fue entregado en el buzón del cliente{_escape(cuando)}. '
        'Confirma que <b>llegó</b> (no necesariamente que lo haya abierto).</p>'
        '</div>'
        f'<p style="margin:14px 2px 0;font-size:13px;color:#5a6b82;">Cliente: <b>{_escape(cliente)}</b>{per_html}</p>'
        '<p style="color:#8593a8;font-size:12px;margin-top:6px;">En Notion, la columna «Entrega Correo» de la fila también lo muestra. Confirmación automática de AuditAI (no responder).</p>'
        '</div>'
    )
    # dev@ (monitoreo) recibe copia de cada confirmación de entrega: si hay asesor,
    # va como CC; si no se resolvió el asesor, dev@ pasa a ser el destinatario.
    destinatarios = [e for e in [email_asesor] if e]
    return _post_aviso(destinatarios or [MONITOR_EMAIL], asunto, texto, html,
                       cc=[MONITOR_EMAIL] if destinatarios else None)


def enviar_aviso_dev(admins: list[str], cliente: str, mes: str, diag,
                     motivo: str = "", flujo: str = "", page_id: str = "") -> bool:
    """Aviso TÉCNICO al desarrollador/administrador: causa raíz, detalle para
    depurar, y una query lista para pegar en un chat con un LLM y resolverlo.
    Best-effort: nunca lanza. Si no hay admins, no envía (queda el log)."""
    asunto = f"[DEV] Fallo {flujo or 'envío'} · {cliente} · {diag.categoria}"
    resuelve = "Requiere corrección de DATO (asesor)" if diag.puede_asesor else "Requiere acción del DEV/config"

    texto = (
        f"Fallo de envío · categoría: {diag.categoria}\n"
        f"Cliente: {cliente} · Período: {mes or '—'} · flujo: {flujo or '—'} · page_id: {page_id or '—'}\n"
        f"Clasificación: {resuelve}\n\n"
        f"MOTIVO (crudo): {motivo}\n\n"
        f"CAUSA RAÍZ:\n{diag.causa_raiz}\n\n"
        f"DETALLE:\n{diag.detalle_dev}\n\n"
        f"QUERY PARA RESOLVER CON UN LLM (copiar y pegar en el chat):\n{diag.query_llm}\n\n"
        "— AuditAI · canal DEV (no responder)."
    )
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:680px;color:#0B1F3A;">'
        '<div style="display:inline-block;background:#eef2fb;border:1px solid #cdd8f0;color:#33489a;'
        'font-size:12px;font-weight:700;border-radius:999px;padding:5px 12px;margin-bottom:12px;">🔧 Canal DEV</div>'
        f'<div style="font-size:17px;font-weight:800;margin:0 0 4px 0;">{_escape(diag.titulo)}</div>'
        f'<p style="margin:0 0 14px 0;font-size:13px;color:#5a6b82;">'
        f'categoría <b>{_escape(diag.categoria)}</b> · {_escape(resuelve)}</p>'
        '<table style="border-collapse:collapse;font-size:13px;color:#3a4658;margin:0 0 14px 0;">'
        f'<tr><td style="padding:2px 10px 2px 0;color:#8593a8;">Cliente</td><td style="padding:2px 0;"><b>{_escape(cliente)}</b></td></tr>'
        f'<tr><td style="padding:2px 10px 2px 0;color:#8593a8;">Período</td><td style="padding:2px 0;">{_escape(mes or "—")}</td></tr>'
        f'<tr><td style="padding:2px 10px 2px 0;color:#8593a8;">Flujo</td><td style="padding:2px 0;">{_escape(flujo or "—")}</td></tr>'
        f'<tr><td style="padding:2px 10px 2px 0;color:#8593a8;">page_id</td><td style="padding:2px 0;font-family:monospace;">{_escape(page_id or "—")}</td></tr>'
        '</table>'
        f'{_bloque_dev("Motivo (crudo)", motivo)}'
        f'{_bloque_dev("Causa raíz", diag.causa_raiz)}'
        f'{_bloque_dev("Detalle para depurar", diag.detalle_dev)}'
        '<div style="font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#33489a;margin:16px 0 6px;">Query para resolver con un LLM</div>'
        '<pre style="white-space:pre-wrap;word-break:break-word;background:#0B1F3A;color:#e7edf7;'
        f'border-radius:10px;padding:14px 16px;font-family:Consolas,Menlo,monospace;font-size:12.5px;line-height:1.5;margin:0;">{_escape(diag.query_llm)}</pre>'
        '<p style="color:#8593a8;font-size:12px;margin-top:12px;">AuditAI · canal DEV (no responder).</p>'
        '</div>'
    )
    return _post_aviso(admins, asunto, texto, html)


def _bloque_dev(titulo: str, valor: str) -> str:
    if not (valor or "").strip():
        return ""
    return (
        f'<div style="font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#5a6b82;margin:12px 0 4px;">{_escape(titulo)}</div>'
        f'<div style="font-size:13.5px;color:#3a4658;line-height:1.55;background:#f4f6fa;border:1px solid #dfe5ee;border-radius:8px;padding:10px 14px;">{_escape(valor)}</div>'
    )
