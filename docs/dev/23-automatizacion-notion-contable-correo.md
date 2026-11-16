# 23 · Automatización de Notion — páginas "Contable" + disparador de correo F29 (guía ejecutable para LLM)

> **Misión:** dejar operativas, de punta a punta, **dos automatizaciones** sobre las páginas
> "Contable" de GCP en Notion:
> - **B · Disparador de correo:** un **botón "Enviar F29"** en cada fila de la página Contable que,
>   al apretarse, **envía al cliente un correo con el detalle de su F29** (por ahora, el **monto**
>   que ya está en la columna `Impuestos`).
> - **A · Duplicación mensual:** automatizar lo que GCP hace a mano cada mes — **duplicar la página
>   Contable del mes anterior** y **agregar los clientes nuevos**.
>
> **Naturaleza:** este es el nuevo foco del proyecto (07-jul-2026). Se **pausa** la automatización
> del SII (docs [`21`](21-resultado-spike-f29.md)/[`22`](22-via-oficial-certificado-digital-api-sii.md)).
> La base central `General Customers Data - AuditAI` queda como **repositorio maestro**; las páginas
> Contable siguen siendo el **motor operativo mensual** de GCP y el **disparador de correos**.
>
> **Este documento es autosuficiente.** Un LLM debe poder ejecutarlo sin volver a investigar. Está
> escrito paso a paso, clic a clic en la UI de Notion y con el código del backend incluido.

> ## 🆕 Estado actualizado (17-jul-2026)
> El **disparador de correo (B)** está en producción y muy endurecido. Los pendientes que este doc
> listaba quedaron resueltos: **migración a Render** ✅ (correo por SendGrid API, no SMTP);
> **App Passwords** ✅ superadas (la nube usa SendGrid; dominio autenticado → todos los
> `@inversoragcp.com` verificados); **`duplicar_mes.py` (A)** ✅ **cancelada** — Carlos duplica la
> página a mano y el sistema sigue funcionando sin tocar código (ver [`28`](28-cambio-de-mes-y-rotacion-manual.md)).
> **Mejoras nuevas del flujo F29** (detalle en [`31`](31-mejoras-robustez-correo-jul-2026.md)):
> - **Validación del Email** antes de SendGrid (celdas con RUT/basura se cortan con motivo claro).
> - **Fallback de Email por RUT a la base madre** (igual que RRHH; `nc.buscar_email_en_central`).
> - **Multi-destinatario**: la celda Email acepta varias direcciones (1ª = `to`, resto en CC).
> - **Reintento automático ante rebote** + **avisos de error a 2 audiencias** (asesor + dev).

---

## §0 · Orden de lectura y decisiones ya tomadas

**Leer antes de actuar (en este orden):**
1. [`../../AGENTS.md`](../../AGENTS.md) — reglas de oro (obligatorio).
2. [`09-seguridad-y-respaldo.md`](09-seguridad-y-respaldo.md) — protocolo de respaldo antes de escribir en Notion.
3. [`08-notion-general-customers-data.md`](08-notion-general-customers-data.md) — esquema de la base central.
4. Este documento completo.

**Decisiones del usuario (07-jul-2026) — NO re-preguntar:**
- **Contenido del correo** = el **monto que ya está en la columna `Impuestos`** de la fila Contable + datos básicos del cliente (nombre, período). Sin depender del SII ni del motor de cálculo.
- **Disparador** = **botón "Enviar F29" por fila** (no etiqueta/estado).
- **Duplicación mensual** = **duplicar el mes anterior + agregar los clientes nuevos**.

**Economía de tokens (para el LLM):** las respuestas de la API de Notion pueden ser enormes.
Guardar volcados en un directorio ignorado por git (`notion_automation/data/`) y examinarlos con
Grep / lecturas parciales; **nunca** imprimir filas completas ni credenciales en la conversación.

---

## §1 · Reglas inquebrantables

| # | Regla | Detalle |
|---|---|---|
| R1 | **La base original es sagrada** | La `General Customers Data` **original** (`1a23f5e4-…`) **jamás** se toca. Solo se trabaja con la **sandbox** `…- AuditAI` (`16f12147-…` / data source `4ff12147-…`) y con las páginas Contable **según R2**. |
| R2 | **Las páginas Contable NO son la sandbox** | `Contable Mayo/Junio/…` son bases **reales del cliente**. Escribir en ellas (agregar el botón, duplicar, resetear campos) exige **backup fechado + autorización explícita del usuario** (AGENTS.md regla 1/8). **Todo se prototipa primero sobre una COPIA de prueba**; recién con visto bueno + backup se opera sobre las reales. |
| R3 | **Credenciales y PII nunca en outputs** | `Clave SII`, `Rut`, `Email` no se imprimen, loguean ni salen en la conversación. El payload del webhook lleva **solo el `id` de la página**; el backend obtiene lo demás por API **en memoria**. |
| R4 | **El webhook se autentica** | Aunque la acción "Enviar webhook" de Notion no firma, el endpoint del backend **valida un secreto compartido** (header) y que el `id` pertenezca a una base Contable, antes de enviar cualquier correo. |
| R5 | **Confirmar antes de escritura masiva/destructiva** | Duplicar un mes o resetear campos en una base real = operación masiva: backup + dry-run (mostrar qué cambiará) + confirmación del usuario, con log de auditoría en `backups/` (fuera de git). |
| R6 | **Si algo no calza, no adivinar** | Etiqueta de UI distinta, propiedad inexistente, payload con otra forma ⇒ volcar evidencia, mostrar un resumen al usuario y decidir juntos. |

---

## §2 · Estado ya verificado (07-jul-2026) — no lo redescubras

### Entorno
- **Python 3.14.0** (`python`), **Node 22** disponibles. **uv NO instalado** → usar `venv`+`pip`.
- **`NOTION_TOKEN`** definido en el entorno (50 chars). API REST de Notion funciona con
  `Notion-Version: 2025-09-03` (ver `spike_f29/notion_lookup.py`).
- `.gitignore` ya protege secretos; agregar `notion_automation/data/` y `*.env`.

### IDs y esquemas confirmados
**Base central (sandbox) `General Customers Data - AuditAI`:**
- data source: `4ff12147-b3ea-82f4-98dd-072067524cdc`
- título = propiedad **`w`** · RUT = **`RUT`** · credencial = **`CLAVE SII`** · 331 clientes.

**`Contable Mayo`** (data source `fdb12147-b3ea-820b-8595-07535e078336`) — columnas relevantes:
| Propiedad | Tipo | Rol en la automatización |
|---|---|---|
| `Customers` | title | nombre del cliente (para el correo) |
| `Rut` | text | RUT (PII; clave de match con la central) |
| `Clave SII` | text | credencial 🔒 (no se usa aquí, no exponer) |
| `Email` | email | **destinatario del correo** |
| `Month` | text | mes de la página (se fija al duplicar) |
| `Impuestos` | number | **el monto del F29 → cuerpo del correo** |
| `Status` | status | flujo pago/declaración; write-back tras enviar (`1) Enviado y Pendiente`) |
| `Ventas`,`Compras`,`Pre-Imptos`,`PreImp` | checkbox | campos del mes (se resetean al duplicar) |
| `CRM`, `Adviser Accounting`, `Actividad Econ`, `Reportabilidad` | select/person | campos **estáticos** (se copian al duplicar) |
| `ARec`,`Control Solicitudes`,`emision de boletas `,`solicitud/informe /boletas` | status | campos del mes (se resetean) |

> ⚠️ Ojo: la central usa `w`/`RUT`; Contable usa `Customers`/`Rut`. **Normalizar el RUT**
> (sin puntos, con guión, dígito verificador en minúscula) antes de cruzar.

### Meses "Contable" existentes (búsqueda Notion 07-jul)
`Contable Mayo` (`37212147-…`, **retirada**), **`Contable Junio`** (`39612147-…`, base operativa actual), varias
`Contable Febrero (1)` (posibles duplicados/junk — no tocar sin preguntar).

### Capacidad nativa de Notion (2026) — CONFIRMADA
La acción **"Enviar webhook" (Send webhook)** existe en **botones** y **automatizaciones de base**:
HTTP **POST** a una URL, con **headers** y **cuerpo** configurables. Solo POST, máx. 5 por
automatización. Es no-code, sin plugins. Fuentes: [Notion · Webhook actions](https://www.notion.com/help/webhook-actions),
[Database automations](https://www.notion.com/help/database-automations).

> ⚠️ **Descubrimiento clave (07-jul, tras E2E exitoso):** la UI del botón en Notion **no permite
> escribir el body JSON a mano**. Solo deja **agregar "contenido" seleccionando propiedades
> existentes de la base** (Customers, Rut, Email, etc.) — **no existe "Page ID" como variable
> seleccionable**. Solución adoptada: **enviar `Rut` + `Email` + `Customers`** como body; el backend
> identifica la fila **por `Rut`** (único por cliente). Ver §5.4 para el detalle del payload real.

### Código reutilizable
- `spike_f29/notion_lookup.py` — patrón REST con `NOTION_TOKEN`, credenciales en memoria. **Base del `notion_client.py` nuevo.**
- `spike_f29/motor_f29.py` — cálculo F29 (para enriquecer el correo cuando se reactive el SII).

---

## §3 · Arquitectura general (evolución multi-automatización)

> 🔁 **09-jul-2026:** la arquitectura se expande de una sola automatización (F29) a un
> **sistema multi-handler** que soporta F29, RRHH y Tickets desde el mismo backend.
> Ver [`24-arquitectura-multi-automatizacion.md`](24-arquitectura-multi-automatizacion.md).
> **Este doc se mantiene como la guía operativa del handler F29** (el resto de handlers
> tienen su propia lógica en `handlers/`).

```text
┌─────────────────────────── AUTOMATIZACIÓN B (correo) ───────────────────────────┐
│ Notion (fila Contable)                     Backend AuditAI            Proveedor  │
│  [Botón "Enviar F29"] --Send webhook POST--> /webhook/f29  --compone--> correo → │
│        (payload: page_id + secreto)          (handlers/f29.py)          Email     │
│                                               lee fila por API,        cliente   │
│                                               en memoria)                        │
│                       <---- write-back Status="Enviado" + fecha ------            │
└──────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────── AUTOMATIZACIÓN B (RRHH) ──────────────────────────────────┐
│ Notion (RRHH JUNIO 2026)                  Backend AuditAI                Correo  │
│  [Botón "Enviar RRHH"] --Send webhook--> /webhook/rrhh  --compone--> imposiciones│
│                        (handlers/rrhh.py)                           al cliente   │
└──────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────── AUTOMATIZACIÓN A (duplicación mensual) ────────────────────┐
│ duplicar_mes.py  →  crea "Contable <Mes>" (esquema+filas del mes anterior)         │
│                     resetea campos del mes · fija Month                            │
│                     + agrega clientes nuevos (en central, ausentes en el mes; RUT) │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Estructura de archivos** (paquete `notion_automation/`, junto a `spike_f29/`):
```
notion_automation/
  README.md
  requirements.txt
  .env.example
  notion_client.py          # helpers REST (leer/escribir fila, query por RUT)
  email_sender.py           # carga plantilla + envío SMTP/SendGrid + BCC_EXTRA
  app.py                    # backend: router POST /webhook/<tipo>
  duplicar_mes.py           # Fase 2: duplicación mensual + reconciliación
  handlers/                 # 🆕 modular — un handler por página
    f29.py                  #   handler del botón "Enviar F29"
    rrhh.py                 #   🆕 handler del botón "Enviar RRHH" (en impl.)
    tickets.py              #   🆕 handler del botón "Enviar Ticket" (planif.)
  email_templates/
    f29_email.html / .txt   # (existente)
    rrhh_email.html / .txt  # 🆕
    tickets_email.html/.txt # 🆕 (planif.)
    README.md
    preview-correo-f29.html
  firmas/                   # PNG de firmas de asesores
  data/                     # volcados (gitignored)
```

> 📐 **Cada handler** define su data source, columnas, plantilla y lógica de forma autocontenida.
> Todo lo común (API Notion, envío de correos, BCC a Carlos/Andrea, credenciales de asesores)
> se comparte. Para el detalle completo de la arquitectura multi-handler y cómo agregar
> nuevas automatizaciones, ver [`24-arquitectura-multi-automatizacion.md`](24-arquitectura-multi-automatizacion.md).

---

## §4 · FASE 0 — Entorno y secretos (una sola vez)

```powershell
cd c:\Users\Hp\Documents\AuditAI
mkdir notion_automation
cd notion_automation
python -m venv .venv
.venv\Scripts\python -m pip install --quiet requests flask python-dotenv sendgrid
```

Crear `notion_automation/.env` (NUNCA se commitea; `.gitignore` cubre `*.env`):
```
NOTION_TOKEN=<ya está en el entorno; repetir aquí o leer del entorno>
WEBHOOK_SECRET=<generar uno largo aleatorio, p. ej. 32 bytes hex>
EMAIL_API_KEY=<API key del proveedor de correo>
EMAIL_FROM=notificaciones@<dominio-verificado-de-GCP>
```
Agregar a `../.gitignore` la línea `notion_automation/data/` (los `*.env` ya están cubiertos).

> **Decisión de implementación:** proveedor de correo. Recomendado **SendGrid** o **Resend**
> (API HTTP simple). Requiere **dominio remitente verificado** (idealmente de GCP) para no caer en
> spam. El ejemplo de §5.2 usa SendGrid; adaptar si se elige otro.

---

## §5 · FASE 1 — Correo por botón (automatización B)

### 5.0 · Copia de prueba (R2 — obligatorio antes de tocar lo real)
En Notion, **duplicar manualmente** `Contable Mayo` (menú `•••` → **Duplicar**) y renombrar la copia
a **`ZZ_PRUEBA Contable`**. **Todo el desarrollo de Fase 1 se hace sobre esta copia.** Obtener su
`data source id` con la herramienta MCP `fetch` o dejándolo pasar por el backend en el primer webhook.

### 5.1 · `notion_client.py` (helpers REST)
```python
"""Helpers REST para Notion. Reutiliza el patrón de spike_f29/notion_lookup.py.
Credenciales solo en memoria (R3): nunca imprime Clave SII, Rut ni Email."""
from __future__ import annotations
import os, requests

API = "https://api.notion.com/v1"
VER = "2025-09-03"

def _headers() -> dict:
    tok = os.environ["NOTION_TOKEN"]
    return {"Authorization": f"Bearer {tok}", "Notion-Version": VER,
            "Content-Type": "application/json"}

def get_page(page_id: str) -> dict:
    r = requests.get(f"{API}/pages/{page_id}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

def plain(prop: dict) -> str:
    """Texto plano de title/rich_text/email/number."""
    t = prop.get("type")
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in prop.get(t, [])).strip()
    if t == "email":
        return (prop.get("email") or "").strip()
    if t == "number":
        n = prop.get("number")
        return "" if n is None else str(n)
    if t == "status":
        s = prop.get("status")
        return (s or {}).get("name", "")
    return ""

def update_props(page_id: str, properties: dict) -> None:
    r = requests.patch(f"{API}/pages/{page_id}", headers=_headers(),
                       json={"properties": properties}, timeout=30)
    r.raise_for_status()

def query_data_source(ds_id: str, body: dict | None = None) -> list[dict]:
    """Query paginado de un data source (API nueva)."""
    out, cursor = [], None
    while True:
        payload = dict(body or {})
        if cursor:
            payload["start_cursor"] = cursor
        r = requests.post(f"{API}/data_sources/{ds_id}/query", headers=_headers(),
                          json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        out += data.get("results", [])
        if not data.get("has_more"):
            return out
        cursor = data["next_cursor"]
```

### 5.2 · `email_sender.py` (plantilla + envío)
> **Plantilla ya creada:** [`../../notion_automation/email_templates/`](../../notion_automation/email_templates/)
> — `f29_email.html` (email-safe, tablas + estilos inline), `f29_email.txt` (respaldo), `README.md`
> con las variables y variantes, y `preview-correo-f29.html` (vista previa interactiva con el logo
> incrustado y los 3 bloques opcionales). El backend **carga estos archivos y reemplaza los
> marcadores `{{...}}`** en vez de tener el HTML embebido.
>
> **4 reglas del usuario (07-jul-2026) — ya aplicadas en la plantilla y en este código:**
> 1. **Asunto fijo:** `"Asesoría Honorario"` (sin variables).
> 2. **Fecha límite sin paréntesis:** solo la fecha (`lunes 22 de junio de 2026.`), sin texto redundante.
> 3. **Info adicional flexible:** bloque **opcional** (si la celda está vacía no aparece) y **variable**
>    (no siempre es el remanente — GCP escribe lo que corresponda).
> 4. **Honorarios + datos de transferencia:** si el cliente debe honorarios (> 0), el bloque incluye
>    los **datos bancarios de GCP** (Santander · Cta. Cte. 0-000-8577678-9 · RUT 76.976.672-3 ·
>    Inversora GCP Ltda).

```python
"""Compone y envía el correo del F29. Carga la plantilla de email_templates/ y
reemplaza {{marcadores}}. Contenido: monto (Impuestos) + fecha límite + honorarios
(con datos de transferencia) + info adicional opcional. Sin desglose del SII por ahora."""
from __future__ import annotations
import os, datetime
from pathlib import Path
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

TEMPLATES = Path(__file__).parent / "email_templates"
ASUNTO = "Asesoría Honorario"   # (1) feedback usuario: asunto fijo, sin variables

_MESES = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
          "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
_MI = {v: k for k, v in _MESES.items()}
_DIAS = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]

# Feriados NACIONALES de Chile (excluye regionales). Fuente: date.nager.at/api/v3/PublicHolidays/{año}/CL
# (la API del gobierno apis.digital.gob.cl está DEPRECADA). Refrescar del API en producción y cachear;
# la lista fija es respaldo y DEBE revisarse cada año (feriados movibles: Viernes Santo, etc.).
FERIADOS_CL = {
  2026: {"2026-01-01","2026-04-03","2026-04-04","2026-05-01","2026-05-21","2026-06-21",
         "2026-06-29","2026-07-16","2026-08-15","2026-09-18","2026-09-19","2026-10-12",
         "2026-10-31","2026-11-01","2026-12-08","2026-12-25"},
}

# (4) Datos bancarios de GCP para recibir honorarios (fijos). Solo se muestran si hay honorarios > 0.
BANCO_GCP_HTML = ("Banco Santander · Cuenta Corriente<br>N° 0-000-8577678-9<br>"
                  "RUT: 76.976.672-3<br>Razón Social: Inversora GCP Ltda")
BANCO_GCP_TXT  = ("Banco Santander, Cuenta Corriente N° 0-000-8577678-9, "
                  "RUT 76.976.672-3, Razón Social Inversora GCP Ltda")

def clp(monto: str | int | float) -> str:
    """Peso chileno: separador de miles '.', prefijo '$' (260143 → '$260.143')."""
    try:
        n = int(round(float(monto)))
        return "$" + f"{n:,}".replace(",", ".")
    except (ValueError, TypeError):
        return "$0"

def _es_habil(d: datetime.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in FERIADOS_CL.get(d.year, set())

def fecha_limite(periodo: str) -> datetime.date | None:
    """'Mayo 2026' → día 20 del mes SIGUIENTE; si no es hábil (fin de semana o feriado
    chileno), se traslada al siguiente día hábil."""
    p = periodo.strip().lower().split()
    mes  = next((_MESES[x] for x in p if x in _MESES), None)
    anio = next((int(x) for x in p if x.isdigit() and len(x) == 4), None)
    if not mes or not anio:
        return None
    m2, a2 = (mes + 1, anio) if mes < 12 else (1, anio + 1)
    d = datetime.date(a2, m2, 20)
    while not _es_habil(d):
        d += datetime.timedelta(days=1)
    return d

def fecha_larga(d: datetime.date) -> str:
    return f"{_DIAS[d.weekday()]} {d.day} de {_MI[d.month]} de {d.year}"

def _variantes(monto_str: str, periodo: str) -> tuple[str, str]:
    """(titulo_resultado, mensaje_resultado) según el signo del monto."""
    try:
        n = float(monto_str)
    except (ValueError, TypeError):
        n = 0.0
    if n > 0:
        return "Total a pagar", f"Corresponde al Formulario 29 del período {periodo}."
    if n < 0:
        return "Saldo a favor", "Este mes no debe pagar; el saldo a favor se arrastra al período siguiente."
    return "Declaración sin pago", f"Su Formulario 29 del período {periodo} se declara sin pago este mes."

def _bloque_fecha(d: datetime.date) -> tuple[str, str]:
    """(2) Solo la fecha, sin texto entre paréntesis."""
    f = fecha_larga(d)
    html = (f'<p style="margin:0 0 18px 0;font-size:14px;line-height:1.6;color:#3a4658;">'
            f'<b>Fecha límite de pago:</b> {f}.</p>')
    txt  = f"Fecha límite de pago: {f}."
    return html, txt

def _bloque_honorarios(monto_h: str) -> tuple[str, str]:
    """(4) Honorarios pendientes + datos de transferencia. Solo si > 0; si no, ("", "")."""
    try:
        h = float(monto_h)
    except (ValueError, TypeError):
        h = 0.0
    if h <= 0:
        return "", ""
    m = clp(h)
    html = (f'<div style="margin:0 0 18px 0;background:#fff8ec;border:1px solid #f2e2bf;'
            f'border-left:4px solid #E7A100;border-radius:10px;padding:14px 18px;">'
            f'<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
            f'color:#9a7400;margin-bottom:3px;">Honorarios de asesoría · pendientes</div>'
            f'<div style="font-size:14px;color:#3a4658;line-height:1.55;">Además, tiene '
            f'<b>honorarios de asesoría pendientes por {m}</b> correspondientes a nuestros servicios '
            f'del mes. (Independiente del impuesto del F29.)</div>'
            f'<div style="margin-top:10px;padding-top:10px;border-top:1px solid #f2e2bf;'
            f'font-size:13px;color:#3a4658;line-height:1.7;">'
            f'<b>Datos para la transferencia:</b><br>{BANCO_GCP_HTML}</div></div>')
    txt  = (f"Honorarios de asesoría pendientes: {m} (independiente del impuesto del F29). "
            f"Transferir a: {BANCO_GCP_TXT}.")
    return html, txt

def _bloque_info(texto: str) -> tuple[str, str]:
    """(3) Info adicional flexible y opcional: si la celda está vacía, no aparece.
    No siempre es el remanente — GCP escribe lo que corresponda (texto libre)."""
    t = (texto or "").strip()
    if not t:
        return "", ""
    html = (f'<div style="margin:0 0 18px 0;background:#f4f6fa;border:1px solid #dfe5ee;'
            f'border-radius:10px;padding:14px 18px;">'
            f'<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
            f'color:#5a6b82;margin-bottom:3px;">Información adicional</div>'
            f'<div style="font-size:14px;color:#3a4658;line-height:1.55;">{t}</div></div>')
    txt  = f"Información adicional: {t}"
    return html, txt

def render(nombre: str, periodo: str, monto: str, asesor: str, contacto: str,
           logo_url: str, honorarios: str = "", info_adicional: str = "") -> tuple[str, str]:
    """Carga la plantilla y reemplaza los marcadores. Devuelve (html, txt)."""
    titulo, msg = _variantes(monto, periodo)
    try:
        n = float(monto)
    except (ValueError, TypeError):
        n = 0.0
    # Bloques en orden fijo: fecha · honorarios · info. Cada uno es ("", "") si no corresponde.
    b_fecha = ("", "")
    if n > 0:                                    # solo si hay algo que pagar
        d = fecha_limite(periodo)
        if d:
            b_fecha = _bloque_fecha(d)
    b_hono  = _bloque_honorarios(honorarios)     # "" si honorarios <= 0
    b_info  = _bloque_info(info_adicional)       # "" si la celda está vacía

    vars_ = {
        "logo_url": logo_url, "nombre_cliente": nombre, "periodo": periodo, "monto": clp(monto),
        "titulo_resultado": titulo, "mensaje_resultado": msg,
        "asesor": asesor, "contacto_email": contacto,
        "bloque_fecha_limite": b_fecha[0], "linea_fecha_limite": b_fecha[1],
        "bloque_honorarios":   b_hono[0],  "linea_honorarios":   b_hono[1],
        "bloque_info_adicional": b_info[0], "linea_info_adicional": b_info[1],
        "info_adicional_texto": (info_adicional or "").strip(),
    }
    html = (TEMPLATES / "f29_email.html").read_text(encoding="utf-8")
    txt  = (TEMPLATES / "f29_email.txt").read_text(encoding="utf-8")
    for k, v in vars_.items():
        html = html.replace("{{" + k + "}}", v)
        txt  = txt.replace("{{" + k + "}}", v)
    return html, txt

def enviar(destinatario: str, nombre: str, mes: str, monto: str,
           honorarios: str = "", info_adicional: str = "",
           asesor: str = "Equipo GCP", contacto: str | None = None,
           logo_url: str | None = None) -> None:
    contacto = contacto or os.environ.get("EMAIL_CONTACTO", "contacto@gcp.cl")
    logo_url = logo_url or os.environ.get("LOGO_URL", "https://gcp.cl/logo.png")
    html, txt = render(nombre, mes, monto, asesor, contacto, logo_url, honorarios, info_adicional)
    msg = Mail(from_email=os.environ["EMAIL_FROM"], to_emails=destinatario,
               subject=ASUNTO, html_content=html, plain_content=txt)
    SendGridAPIClient(os.environ["EMAIL_API_KEY"]).send(msg)
```

### 5.2.b · Detalle de las reglas de los 3 bloques opcionales (notas sobre el código de §5.2)
El correo arma **3 bloques opcionales** antes de enviar. Cada uno se inyecta solo si corresponde y
se construye con funciones en `email_sender.py` (ver §5.2). Reglas:

- **Fecha límite (`fecha_limite()` + `_bloque_fecha()`):** plazo = **día 20 del mes SIGUIENTE** al
  período; si no es hábil (fin de semana o **feriado chileno**), se traslada al **siguiente día
  hábil**. **(2)** Solo la fecha, sin texto entre paréntesis (feedback usuario).
  - Solo se inyecta si el **monto > 0** (hay algo que pagar). Si es 0/negativo, no se cobra aún.
  - Ej.: **Mayo 2026** → 20-jun (sábado) y 21-jun (domingo **y** Día de los Pueblos Indígenas) → **lunes 22-jun-2026**.
  - Feriados: nacionales, desde `date.nager.at/api/v3/PublicHolidays/{año}/CL` (la API del gobierno
    `apis.digital.gob.cl` está **deprecada**), con lista fija `FERIADOS_CL` de respaldo. 🔁 En
    producción, refrescar del API y cachear; la lista fija **debe revisarse cada año** (hay feriados
    movibles: Viernes Santo, Día de los Pueblos Indígenas por el solsticio, etc.).

- **Honorarios pendientes + datos de transferencia (`_bloque_honorarios()`):** lee la columna
  **`Honorarios Pendientes`** (number). **Solo si > 0** se inyecta `{{bloque_honorarios}}`, que
  **incluye los datos bancarios de GCP** para transferir (fijos, en `BANCO_GCP_HTML`/`BANCO_GCP_TXT`):
  **Banco Santander · Cuenta Corriente N° 0-000-8577678-9 · RUT 76.976.672-3 · Razón Social
  Inversora GCP Ltda**. **(4)** Es el honorario de la asesoría (a GCP) — **distinto** del impuesto
  del F29 (uno al SII, otro a GCP).

- **Información adicional (`_bloque_info()`):** lee la columna **`Info Adicional`** (text). Si tiene
  contenido, se inyecta `{{bloque_info_adicional}}` con ese texto **verbatim**; si está vacía, `""`.
  **(3)** Es un bloque **variable** (feedback usuario): **puede no aparecer** y **no siempre es el
  remanente** — GCP escribe ahí lo que corresponda. El HTML de cada bloque está en
  `email_templates/README.md` y una vista previa interactiva en `preview-correo-f29.html`.

### 5.2.c · Columnas nuevas a crear en Contable (para hacer el correo realidad)
Carlos autorizó agregar en Notion lo necesario. Se agregan a la base Contable **(operación aditiva,
reversible; aun así: R2/R5 → backup + confirmación antes de tocar la base real):**

| Columna nueva | Tipo | Uso en el correo |
|---|---|---|
| **`Enviar Correo F29`** | button | dispara el webhook (§5.4) |
| **`Honorarios Pendientes`** | number | honorarios pendientes → `{{bloque_honorarios}}` (incluye datos de transferencia GCP) |
| **`Valor-Info adicional`** 🆕 | number | valor numérico de la info adicional (ej. 417798) |
| **`Motivo-Info adicional`** 🆕 | select | motivo: Remanente / Saldo a favor / Pago adicional / Otro (el asesor puede escribir nuevas) |

> Ya existentes que se reutilizan: `Customers`, `Email`, `Month`, `Impuestos`, `Status`.
> La **fecha límite NO necesita columna** (se calcula desde `Month`).
>
> 🆕 **Split de "Info Adicional" (07-jul, feedback usuario):** la columna original `Info Adicional`
> (rich_text libre) se dividió en dos — `Valor-Info adicional` (number) + `Motivo-Info adicional`
> (select con dropdown + escritura). El backend las combina con `_combinar_info()` en un texto
> natural: ambos → "Remanente: $417.798" · solo valor → "$417.798" · solo motivo → "Remanente" ·
> ninguno → bloque omitido. La columna vieja se eliminó (backup previo en
> `backups/contable-junio/2026-07-07_2037_pre-info-split_InfoAdicional.csv`).

### 5.2.d · `Month` — el asesor no lo tipea (feedback usuario 07-jul)

> **Decisión (07-jul-2026, feedback del usuario):** la columna `Month` es el **período del F29**
> (fijo por página Contable — ej. "Junio 2026"), **no** el mes de edición. Para evitar trabajo
> innecesario y errores del asesor, **este valor se fija automáticamente** y el asesor nunca lo
> tipea por fila.

**Por qué NO auto-actualizar en cada edición:** si `Month` reflejara el mes *de edición*, la
fecha límite (día 20 del mes siguiente al período) se rompería. Ej: editar en agosto una fila
de Contable Junio → `Month` = "Agosto 2026" → correo diría "vence 20 septiembre" → incorrecto.

**Implementación (2 capas, "Opción A + fallback C"):**

1. **Opción A — `bulk_set_month.py` (bulk-set una vez por página):** fija `Month` a un valor
   constante para todas las filas de una Contable existente. Ejecutado el 07-jul-2026 sobre
   Contable Junio: **289/289 filas** con `Month = "Junio 2026"` (287 venían vacías + 1 con valor
   erróneo "1,8" sobrescrito + 1 ya correcta). Backup previo en
   `backups/contable-junio/2026-07-07_2012_pre-month-bulk.csv`. En la Fase 2 (`duplicar_mes.py`),
   el valor se fija automáticamente al crear cada nueva página mensual — el asesor nunca lo ve vacío.

2. **Fallback C — `derivar_month_desde_base()` (en `notion_client.py`):** si una fila llega al
   webhook con `Month` vacío, el backend deriva el período desde el título de la base parent
   ("Contable Junio" → "Junio 2026"), combinando el mes del título con el año de
   `last_edited_time`. Corrección diciembre/enero: el F29 de diciembre se hace en enero del
   año siguiente. **Limitación:** si se edita mucho tiempo después del período, el año puede
   desfasarse — es un fallback de emergencia, no la vía principal (la vía principal es el bulk-set).

**Formato del valor:** `"Junio 2026"` (mes capitalizado en español + año de 4 dígitos), igual
que la fila `ZZ_TEST AuditAI`.

**Script:** `notion_automation/bulk_set_month.py` — soporta `--apply` y dry-run (por defecto).
Backup previo automático a `backups/contable-junio/`. No loguea PII.

### 5.3 · `app.py` (backend — endpoint del webhook)

> **Implementación real (07-jul-2026, E2E verificado):** el backend no recibe `page_id` directo
> (Notion no lo expone como variable de botón — ver §5.4). Recibe **`Rut`** (+ `Email`/`Customers`
> como validación) y **busca la fila por RUT** en Contable Junio vía `query_data_source` con filtro
> `rich_text.equals`. El extractor es **recursivo y tolerante** porque Notion anida las propiedades
> dentro de `{"source": {...}, "data": {...}}` (descubierto en vivo). Ver §5.4 para el formato exacto.

```python
"""Backend AuditAI. Recibe el webhook del botón "Enviar Correo F29", identifica
la fila por RUT (Notion no expone page_id como variable de botón), lee la fila
de Contable Junio en memoria, envía el correo desde la cuenta del asesor
asignado, y escribe de vuelta el Status. R3/R4.

Uso:
  python app.py                       # arranca el servidor Flask en 0.0.0.0:8000
  python app.py --test <page_id>      # prueba directa por page_id (sin webhook)
  python app.py --test-rut <rut>      # prueba directa por RUT (busca en Contable Junio)
"""
from __future__ import annotations
import os, sys, logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, abort
from dotenv import load_dotenv
import notion_client as nc
import email_sender as es

load_dotenv()

# Logging SIN PII. FileHandler escribe directo (sin buffering).
_fh = RotatingFileHandler("auditai.log", maxBytes=200000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_fh, logging.StreamHandler()],
)
log = logging.getLogger("auditai")
app = Flask(__name__)

# Nombres EXACTOS de las propiedades en Contable Junio
P_NOMBRE      = "Customers"
P_EMAIL       = "Email"
P_MES         = "Month"
P_MONTO       = "Impuestos"
P_STATUS      = "Status"
P_HONORARIOS  = "Honorarios Pendientes"
P_INFO        = "Info Adicional"
P_ADVISER     = "Adviser Accounting"
STATUS_ENVIADO = "1) Enviado y Pendiente"


def _estructura(d, profundidad=0):
    """Dict anidado con solo keys y tipos (sin valores = sin PII), para diagnóstico."""
    if profundidad > 4 or not isinstance(d, dict):
        return type(d).__name__
    return {k: _estructura(v, profundidad + 1) for k, v in d.items()}


def _extraer_plano_notion(v: dict) -> str:
    """Extrae valor plano de un objeto propiedad Notion (con o sin `type` explicito)."""
    if not isinstance(v, dict):
        return ""
    t = v.get("type")
    if t:
        try:
            p = nc.plain(v)
            if p:
                return p
        except Exception:
            pass
    for campo in ("rich_text", "title"):
        arr = v.get(campo)
        if isinstance(arr, list):
            txt = "".join(x.get("plain_text", "") for x in arr if isinstance(x, dict)).strip()
            if txt:
                return txt
    for campo in ("email", "url", "phone_number", "number"):
        val = v.get(campo)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int, float)):
            return str(val)
    sel = v.get("select") or v.get("status")
    if isinstance(sel, dict) and sel.get("name"):
        return sel["name"]
    return ""


def _buscar_clave(d, claves, profundidad=0, _ruta=""):
    """Busca recursivamente la primera clave en `claves` (case-insensitive).
    Devuelve (valor, ruta) o (None, ""). Maneja: string plano, objeto propiedad
    Notion (con/sin `type`), y dict anidado. No loguea valores (PII)."""
    if profundidad > 6 or not isinstance(d, dict):
        return None, ""
    claves_lc = {k.lower() for k in claves}
    for k, v in d.items():
        ruta_n = f"{_ruta}.{k}" if _ruta else k
        if k.lower() in claves_lc:
            if isinstance(v, (str, int, float)):
                return str(v), ruta_n
            if isinstance(v, dict):
                plano = _extraer_plano_notion(v)
                if plano:
                    return plano, ruta_n
        if isinstance(v, dict):
            val, r = _buscar_clave(v, claves, profundidad + 1, ruta_n)
            if val is not None:
                return val, r
    return None, ""


def _es_uuid(s: str) -> bool:
    return (len(s) == 36 and s.count("-") == 4
            and all(c in "0123456789abcdef-" for c in s.lower()))


def _procesar_page(page_id: str) -> dict:
    """Lee la fila, envía el correo, actualiza Status. No loguea PII."""
    page = nc.get_page(page_id)
    props = page["properties"]
    email      = nc.plain(props.get(P_EMAIL, {}))
    nombre     = nc.plain(props.get(P_NOMBRE, {}))
    mes        = nc.plain(props.get(P_MES, {}))
    monto      = nc.plain(props.get(P_MONTO, {}))
    honorarios = nc.plain(props.get(P_HONORARIOS, {}))
    info       = nc.plain(props.get(P_INFO, {}))
    asesor_nombres = nc.people_names(props.get(P_ADVISER, {}))
    nombre_asesor = asesor_nombres[0] if asesor_nombres else ""

    log.info("page_id=%s cliente=%r asesor=%r mes=%r monto_present=%s hono_present=%s info_present=%s",
             page_id, nombre, nombre_asesor, mes, bool(monto), bool(honorarios), bool(info))

    if not email:
        return {"ok": False, "motivo": "fila sin Email"}
    if not mes:
        return {"ok": False, "motivo": "fila sin Month (necesario para fecha límite)"}

    try:
        remitente = es.enviar(destinatario=email, nombre=nombre, mes=mes, monto=monto or "0",
                              nombre_asesor=nombre_asesor, honorarios=honorarios, info_adicional=info)
        log.info("correo enviado OK · page_id=%s remitente=%s", page_id, remitente)
    except ValueError as exc:
        log.error("error envio · page_id=%s · %s", page_id, exc)
        return {"ok": False, "motivo": str(exc)}
    except Exception as exc:
        log.error("error SMTP · page_id=%s · %s", page_id, exc)
        return {"ok": False, "motivo": f"error SMTP: {exc}"}

    try:
        nc.update_props(page_id, {P_STATUS: {"status": {"name": STATUS_ENVIADO}}})
        log.info("status actualizado · page_id=%s → %s", page_id, STATUS_ENVIADO)
    except Exception as exc:
        log.warning("no se pudo actualizar status · page_id=%s · %s", page_id, exc)
    return {"ok": True, "remitente": remitente}


@app.post("/enviar-f29")
def enviar_f29():
    tiene_secreto = bool(request.headers.get("X-AuditAI-Secret"))
    log.info("request recibida · path=/enviar-f29 · tiene_secreto=%s", tiene_secreto)

    # R4: validar secreto compartido
    secreto_esperado = os.environ.get("WEBHOOK_SECRET", "")
    if secreto_esperado:
        if request.headers.get("X-AuditAI-Secret") != secreto_esperado:
            log.warning("secreto invalido o ausente · respondiendo 401")
            abort(401)

    data = request.get_json(force=True, silent=True) or {}
    log.info("estructura payload: %s", _estructura(data))   # sin PII: solo keys y tipos

    # Identificador: priorizar page_id (de source), luego Rut en cualquier nivel
    ident, ruta = _buscar_clave(data, ["page_id"])
    if not ident:
        ident, ruta = _buscar_clave(data, ["Rut", "rut", "RUT"])
    log.info("identificador en ruta=%r (valor no se loguea)", ruta)

    if not ident:
        abort(400, "no se encontro page_id ni Rut en el payload")

    if _es_uuid(ident):
        page_id = ident
        log.info("usando page_id directo (sin query Notion)")
    else:
        page_id = nc.find_page_by_rut(ident)
        if not page_id:
            log.warning("RUT no encontrado en Contable Junio (RUT no se loguea)")
            abort(404, "no se encontro fila con ese Rut")

    resultado = _procesar_page(page_id)
    return resultado, 200


@app.get("/health")
def health():
    return {"ok": True, "service": "auditai-f29"}, 200


if __name__ == "__main__":
    if "--test-rut" in sys.argv:
        idx = sys.argv.index("--test-rut")
        _run_test_rut(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else print("Uso: --test-rut <rut>")
        sys.exit(0)
    if "--test" in sys.argv:
        idx = sys.argv.index("--test")
        _run_test(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else print("Uso: --test <page_id>")
        sys.exit(0)
    port = int(os.environ.get("PORT", "8000"))
    # host="0.0.0.0" obligatorio en la nube (Render/Railway/Cloud Run).
    log.info("arrancando backend en 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
```

**Y en `notion_client.py` se agrega** `find_page_by_rut()` (búsqueda por RUT en Contable Junio):

```python
# Data source de Contable Junio (base operativa)
DS_CONTABLE_JUNIO = "09b12147-b3ea-8337-a218-87538eab23fc"

def find_page_by_rut(rut: str) -> str | None:
    """Busca el page_id por RUT en Contable Junio. Devuelve None si no hay match.
    No loguea el RUT (PII). Asume RUT unico por cliente."""
    body = {"filter": {"property": "Rut", "rich_text": {"equals": rut}}, "page_size": 5}
    results = query_data_source(DS_CONTABLE_JUNIO, body)
    if not results:
        return None
    return results[0]["id"]
```

### 5.4 · Notion UI — agregar el botón y configurar el webhook (clic a clic)

> **⚠️ Importante — lección aprendida del E2E real (07-jul-2026).** La UI del botón de Notion
> **NO permite escribir el body JSON a mano** ni seleccionar un "Page ID" como variable. Solo
> permite **agregar "contenido" eligiendo propiedades existentes de la base** (Customers, Rut,
> Email, etc.). Por eso **el identificador de la fila es el `Rut`**, no el `page_id`. La sección
> `§5.4.legacy` (al final) conserva las instrucciones previas (con `page_id`) solo como referencia
> histórica; **no usarlas** — el backend ya fue adaptado para buscar por RUT (ver §5.3).

**Configuración real del botón "Enviar Correo F29" (en la base Contable Junio real):**

1. Abrir la base **Contable Junio** como tabla → botón **`+`** al final de las columnas →
   **New property** → tipo **Button** → nombrarla **`Enviar Correo F29`**.
2. En la config del botón → **Add step** → elegir **Send webhook** (si aparecen "Edit pages" u
   otras, elegir específicamente **Send webhook**).
3. **URL:** `https://<url-publica-del-backend>/enviar-f29`
   - En prototipo local con ngrok: `https://xxxx.ngrok-free.dev/enviar-f29`.
   - En producción (Render/Railway/Cloud Run): URL estable del deploy.
4. **Method:** POST (único soportado por Notion).
5. **Encabezado personalizado (header):** agregar **uno obligatorio** (aunque Notion diga
   "opcional", el backend lo requiere — R4):
   - **Clave / Key:** `X-AuditAI-Secret`
   - **Valor / Value:** el valor de `WEBHOOK_SECRET` (en `.env`; ej.
     `<WEBHOOK_SECRET>`).
6. **Contenido (body):** agregar las siguientes **propiedades de la base** (no escribir JSON a
   mano — seleccionarlas de la lista que muestra Notion):
   - **`Rut`** ← **clave de identificación de la fila** (el backend busca por RUT)
   - **`Email`** ← validación extra (opcional, no la usa el backend)
   - **`Customers`** ← validación extra (opcional, no la usa el backend)

   No hace falta agregar más columnas: el backend lee el resto (Month, Impuestos, Honorarios
   Pendientes, Info Adicional, Adviser Accounting) directo de Notion vía API por `page_id`.

7. Guardar. La columna `Enviar Correo F29` queda con un botón por fila.

**Payload real que envía Notion (confirmado por el log del backend, 07-jul):**

```json
{
  "source": { "type": "button", "page_id": "..." },
  "data":   { "Rut": "...", "Email": "...", "Customers": "..." }
}
```

> El backend extrae el identificador con un **buscador recursivo** (`_buscar_clave`) que
> prioriza `page_id` (si viene en `source`) y, si no, busca `Rut` en cualquier nivel de
> anidamiento. Así es **tolerante a futuros cambios de formato de Notion**. El RUT **no se
> loguea** (PII, R3).

**Por qué `page_id` como fallback (aunque no se selecciona en la UI):** Notion lo incluye
**automáticamente** dentro de `source.page_id` en cada webhook de botón (aunque la UI no lo
liste como variable seleccionable). El extractor lo encuentra ahí y, si coincide con un UUID
válido (36 chars, 4 guiones), **lo usa directo sin hacer query a Notion** (más rápido). Solo
si por alguna razón `source.page_id` no viniera, cae al RUT y hace `find_page_by_rut()`.

#### 5.4.legacy · Instrucciones previas (con `page_id`) — NO USAR, solo referencia histórica

> Las instrucciones originales asumían que se podía enviar `page_id` como body. En la práctica
> Notion no lo expone como variable seleccionable. Se conservan aquí para entender por qué el
> backend soporta ambos caminos (RUT y page_id):
> 1. Tipo Button, nombre `Enviar F29`.
> 2. Add step → Send webhook.
> 3. URL: `https://xxxx.ngrok-free.app/enviar-f29`.
> 4. Method: POST.
> 5. Headers: `X-AuditAI-Secret` = `WEBHOOK_SECRET`.
> 6. Body: `{ "page_id": "<ID de esta página>" }` (variable "Page ID").

### 5.5 · Túnel para exponer el backend local (prototipo) + SMTP Gmail por asesor

> **E2E usa Gmail SMTP (no SendGrid).** Cada asesor envía desde **su propia cuenta de Gmail**
> (no un remitente genérico). Gmail requiere **2FA + App Password** (las contraseñas normales
> son rechazadas desde 2022). El mapeo asesor → credenciales SMTP vive en
> `notion_automation/asesores_smtp.json` (gitignored, ver R3). Solo **Sebastián Robles** tiene
> App Password configurada y funcionando (verificada en el E2E), al igual que Constanza y Carlos. 2 asesores pendientes
> (Andrea, Matilde) — Andrea y Matilde además no tienen contraseña normal.

```powershell
# terminal 1: backend (con logging a auditai.log)
cd c:\Users\Hp\Documents\AuditAI\notion_automation
.venv\Scripts\python -u app.py
# terminal 2: túnel (ngrok instalado en C:\ngrok\, authtoken configurado una vez)
ngrok http 8000
# copiar la URL https://xxxx.ngrok-free.app  →  usarla en el paso 5.4.3
```

> **ngrok:** la URL **cambia en cada reinicio**. En producción (Fase 3) se migra a Render /
> Railway / Cloud Run para tener una **URL estable**. El health check del backend está en
> `GET /health` → `{"ok": true, "service": "auditai-f29"}`.

### 5.6 · Prueba end-to-end (criterio de aceptación de Fase 1) — ✅ VERIFICADO 07-jul-2026

> **E2E exitoso.** El flujo completo botón → webhook → backend → correo → write-back funcionó
> sobre la fila de prueba `ZZ_TEST AuditAI` en Contable Junio (base operativa). Detalle:
>
> - **Fila de prueba:** `ZZ_TEST AuditAI` (page_id `39612147-b3ea-810f-b761-d610a3be1ce3`),
>   Email = `fbrunel@miuandes.cl`, Impuestos = 12345, Honorarios = 85000,
>   Info Adicional = "A su favor queda un remanente de $417.798…", Adviser = Sebastián Robles,
>   Month = "Junio 2026", Rut = `12345678-9` (RUT ficticio asignado para el test).
> - **Resultado:** correo enviado OK desde `sebastianrobles@inversoragcp.com` (App Password de
>   Gmail configurada en `asesores_smtp.json`), Status → "1) Enviado y Pendiente".
> - **Backend:** Flask en `0.0.0.0:8000`, expuesto vía ngrok
>   (`https://submerge-mousiness-rasping.ngrok-free.dev`). Log estructural sin PII en `auditai.log`.

**Pasos de la prueba (reproducibles):**
1. En la fila de prueba `ZZ_TEST AuditAI` de Contable Junio, fijar `Email` (casilla propia),
   `Impuestos` (ej. 12345), `Month` (ej. "Junio 2026"), `Honorarios Pendientes` (ej. 85000),
   `Info Adicional` (texto libre) y `Rut` (un RUT válido de prueba).
2. Apretar el botón **`Enviar Correo F29`** de esa fila.
3. Verificar: (a) el backend recibe el POST (log del endpoint, sin PII), (b) llega el correo a
   la casilla con **el monto formateado** ($12.345) y el nombre/mes correctos, (c) la fila queda
   con `Status = "1) Enviado y Pendiente"`.
4. Caso borde: fila **sin `Email`** → apretar botón → el backend responde `{"ok": false,
   "motivo": "fila sin Email"}` y **no** cambia el estado; verificar que no rompe.

**Depuración de la raíz del problema (debugging E2E, 07-jul):** el botón fallaba con
"no se pudo ejecutar el botón" y el log mostraba `400 "falta Rut"`. Causa raíz: Notion anida
las propiedades en `{"source": {...}, "data": {...}}`, no en la raíz del JSON. El extractor
original (`_extraer_rut`) buscaba en la raíz. **Solución:** extractor recursivo `_buscar_clave`
que encuentra `page_id` o `Rut` en cualquier nivel (ver §5.3), más `_extraer_plano_notion`
para manejar objetos propiedad Notion con o sin campo `type`. Validado con 5 casos de test
(string plano, objeto propiedad, page_id en source, anidado en `data.properties`, y caso
vacío) — los 5 pasan.

---

## §6 · FASE 2 — Duplicación mensual (automatización A)

> **Objetivo:** reproducir el hábito de GCP — **duplicar la Contable del mes anterior** + **agregar
> los clientes nuevos** (los que están en la base central y no en el mes duplicado, match por RUT).
> Prototipar contra bases de prueba (R2/R5).

### 6.1 · Lógica de `duplicar_mes.py`
Entradas: `--origen <data_source_id del mes anterior>` · `--mes "Contable Julio"` · `--parent <page_id
donde viven las bases Contable>` · `--dry-run`.

Pasos:
1. **Leer el esquema** del data source origen (propiedades y opciones de select/status): `GET
   /v1/databases/{id}` o `fetch` MCP. Guardar el mapa de propiedades.
2. **Crear la base nueva** `POST /v1/databases` con `parent = {page_id}`, `title = "Contable <Mes>"`
   y las **mismas propiedades** (Notion recrea las opciones de select/status). ⚠️ La API pública no
   clona una base en un paso; se recrea propiedad por propiedad. **Verificar** que todos los tipos
   se soporten; los `rollup`/`relation`/`person` pueden requerir manejo especial.
3. **Leer todas las filas** del origen: `query_data_source(origen)` (paginado).
4. **Crear cada fila** en la base nueva (`POST /v1/pages`, parent = data source nuevo), copiando los
   **campos estáticos** (`Customers`, `Rut`, `Clave SII`, `Email`, `CRM`, `Adviser Accounting`,
   `Actividad Econ`, `Reportabilidad`, `datos socio`) y **reseteando los campos del mes**:
   - `Status` → sin valor (o "Not started") · `Impuestos` → vacío · `Ventas`/`Compras`/`Pre-Imptos`/
     `PreImp` → false · status de proceso (`ARec`, `Control Solicitudes`, `emision de boletas`,
     `solicitud/informe /boletas`) → su valor "sin empezar".
   - `Month` → el mes nuevo.
5. **Agregar clientes nuevos:** `query_data_source(central `4ff12147-…`)`; normalizar RUT
   (`w`→nombre, `RUT`); construir el set de RUTs ya presentes en la base nueva; por cada cliente de
   la central cuyo RUT **no** está, **crear su fila** en la base nueva con los datos que aporte la
   central (nombre, RUT, Clave SII, Email si existe). Registrar cuántos se agregaron.
6. **`--dry-run`:** no escribe; imprime el plan (cuántas filas copia, cuántos nuevos, qué campos
   resetea) y lo guarda en `data/dry-run-<mes>.json`. **Solo tras revisión + confirmación** se corre
   sin `--dry-run` (R5), con backup previo.

> **✅ Confirmado (07-jul-2026):** GCP **agrega los clientes nuevos primero en la base central**.
> Por tanto la fuente del paso 5 es la central (`4ff12147-…`), match por `Rut`. Sin ambigüedad.

> **Alternativa pragmática** (si la recreación por API resulta frágil): GCP hace el **duplicado
> nativo** (menú `•••` → Duplicar, 1 clic, copia esquema+filas) y `duplicar_mes.py` solo hace los
> pasos **4-reset** y **5-nuevos** sobre esa copia. Menos código, mismo resultado.

### 6.2 · Disparo de la Fase A
- **Opción botón:** un botón "Generar mes nuevo" en una página de control → webhook → backend corre
  `duplicar_mes.py`. 
- **Opción agendada:** job del backend el día 1 de cada mes (cuando esté en la nube 24/7).
- Decidir con el usuario; para el prototipo, correr el script a mano.

### 6.3 · Criterio de aceptación de Fase 2
Correr contra una base **destino de prueba** partiendo de una copia de Mayo:
- esquema replicado (todas las columnas relevantes), `Month` correcto, **campos del mes reseteados**,
  **campos estáticos copiados**, clientes de la central ausentes en Mayo **agregados** (match por
  RUT), **cero duplicados**. Verificar contra el `dry-run`.

---

## §7 · FASE 3 — Endurecer y llevar a la nube (24/7)

- **Backend a la nube:** desplegar `app.py` en un servicio con URL estable (Render / Railway /
  Azure App Service / AWS Lambda+API Gateway / GCP Cloud Run). Reemplazar la URL de ngrok en el
  botón (§5.4.4) por la definitiva.
- **Secretos** (`NOTION_TOKEN`, `WEBHOOK_SECRET`, `EMAIL_API_KEY`) al **gestor de secretos** del
  proveedor, nunca en el repo.
- **Dominio de correo** verificado (SPF/DKIM) para entregabilidad.
- **Idempotencia:** evitar doble envío si el botón se aprieta dos veces (ej. no reenviar si `Status`
  ya es "Enviado" dentro del mismo período; o registrar envíos).
- **Observabilidad:** log de envíos (sin PII: page_id + timestamp + resultado) para auditoría.
- **Aplicar a las bases reales:** con backup + confirmación (R5), replicar el botón `Enviar F29` en
  `Contable Junio` (y meses siguientes) y apuntar al backend de producción.

---

## §8 · Seguridad (detalle)
- **Payload mínimo:** el webhook manda solo `page_id`. El backend obtiene `Email`/`Impuestos`/nombre
  por API en memoria. **`Clave SII` jamás** se lee ni se envía en este flujo.
- **Secreto del webhook** (`X-AuditAI-Secret`): sin él, el endpoint responde 401. Evita que un
  tercero que descubra la URL dispare correos.
- **Validación de origen:** el backend puede además verificar que el `page_id` pertenece a una base
  Contable conocida (lista blanca de data source ids) antes de enviar.
- **Logs sin PII:** registrar `page_id`, timestamp y resultado; **nunca** email, monto ni RUT.
- **Escrituras en bases reales:** siempre backup fechado (a `backups/`, fuera de git) + dry-run +
  confirmación del usuario (R5). Prototipo siempre sobre copias `ZZ_PRUEBA…`.

---

## §9 · Contingencias

| Situación | Acción |
|---|---|
| Notion 429 (rate limit) | Backoff ≥60 s, máx. 3 reintentos. **Nota clave:** el 429 `collection_router_upstream` que aparece al explorar viene del **motor SQL** (`query_data_sources` vía conector IA), muy limitado. **La automatización NO usa esa vía:** lee **1 página por su ID** (`GET /v1/pages/{id}`) con la **API REST estándar** (~3 req/s por integración) y **token propio** — un `GET` por clic de botón, muy por debajo del límite. Por eso el rate limit de exploración **no afecta** a producción. |
| El webhook no llega al backend | Verificar URL pública (ngrok activo), header del secreto, y que el botón tenga la acción **Send webhook** (no "Edit pages"). Revisar el historial de la automatización en Notion. **En el E2E real (07-jul) el webhook SÍ llegó** — el fallo era de parsing del body, no de conectividad. |
| **Payload con otra forma que `{"page_id": …}` (RESUELTO)** | **Caso real (07-jul):** Notion envía `{"source": {...}, "data": {...}}` con las propiedades anidadas en `data`, no en la raíz. Además la UI no expone `page_id` como variable seleccionable. **Solución aplicada:** extractor recursivo `_buscar_clave` (ver §5.3) que encuentra `page_id` (en `source`) o `Rut` (en cualquier nivel), más `_extraer_plano_notion` para objetos propiedad con/sin `type`. El body del botón lleva `Rut`+`Email`+`Customers` (propiedades seleccionables). El `page_id` viene gratis en `source.page_id` (Notion lo incluye automáticamente). |
| Correo no llega / va a spam | Verificar dominio remitente (SPF/DKIM), revisar respuesta del proveedor. Probar primero a una casilla propia. |
| Fila sin `Email` | El backend responde ok:false sin enviar; no cambia estado. GCP completa el email. |
| `Status` no acepta el valor escrito | Confirmar el nombre EXACTO de la opción de status (`1) Enviado y Pendiente`) en el esquema; los status son sensibles a texto. |
| Duplicación: tipo de propiedad no soportado por la API (rollup/relation) | Usar la alternativa pragmática (§6.1): duplicado nativo + solo reset+nuevos por API. |
| Cualquier pantalla/comportamiento inesperado en bases reales | Detenerse, no escribir, preguntar al usuario (R6). |

---

## §10 · Checklist de éxito total
- [x] **Plantillas del correo creadas** (`notion_automation/email_templates/`): `f29_email.html`,
      `f29_email.txt`, `README.md` (variables, variantes, 4 reglas del usuario) y
      `preview-correo-f29.html` (vista previa interactiva con logo incrustado). Asunto fijo
      `"Asesoría Honorario"`, fecha sin paréntesis, honorarios con datos de transferencia de GCP
      (Santander · 0-000-8577678-9 · RUT 76.976.672-3 · Inversora GCP Ltda), info adicional
      flexible y opcional.
- [x] Fase 0: `notion_automation/` con venv, deps y `.env` (secretos fuera de git). **Dependencias
      finales:** `flask`, `requests`, `python-dotenv`, `jinja2` (NO SendGrid — se usa SMTP Gmail).
- [x] `notion_client.py`, `email_sender.py`, `app.py` escritos y arrancando (`host="0.0.0.0"`,
      `--test` y `--test-rut`, logging a `auditai.log` sin PII, env var fallback en
      `_cargar_asesores`).
- [x] Columnas nuevas en Contable Junio creadas: `Enviar Correo F29` (button, creada desde la
      UI — la API no soporta `button`), `Honorarios Pendientes` (number, vía API),
      `Info Adicional` (rich_text, vía API). Backup previo:
      `backups/contable-junio/2026-07-07_pre-columnas.csv` (288 filas).
- [x] Botón `Enviar Correo F29` + Send webhook configurado en Contable Junio (header secreto +
      body con Rut/Email/Customers — ver §5.4).
- [x] **Prueba E2E exitosa (07-jul-2026):** botón → correo a `fbrunel@miuandes.cl` con el monto
      correcto desde `sebastianrobles@inversoragcp.com` → `Status = "1) Enviado y Pendiente"`.
      Fila de prueba `ZZ_TEST AuditAI` (Rut `12345678-9` asignado para el test).
- [x] Extractor recursivo robusto (`_buscar_clave` + `_extraer_plano_notion`) — tolerante al
      formato real `{"source": {...}, "data": {...}}` de Notion. 5/5 casos de test pasan.
- [ ] **Caso borde sin Email** (verificado manualmente via `--test`, falta clic real del botón).
- [ ] App Passwords de los 2 asesores restantes (Andrea, Matilde) — pendiente
      tras confirmar que Sebastián, Constanza y Carlos funcionan.
- [ ] `duplicar_mes.py` con `--dry-run` correcto sobre bases de prueba (reset + nuevos por RUT, sin duplicados).
- [x] Cero credenciales/PII en logs o conversación (R3) — log estructural solo keys y tipos.
- [x] Bases reales tocadas con backup + confirmación (R2/R5) — Contable Junio con backup previo.
- [ ] (Fase 3) Backend en la nube con URL estable (Render), secretos en gestor, dominio de correo verificado.
- [ ] Documentar el resultado final en un doc `24-resultado-automatizacion-notion.md` (qué se construyó, IDs, endpoints, lecciones).

---

## §11 · Referencias
- [`AGENTS.md`](../../AGENTS.md) · [`08-notion-general-customers-data.md`](08-notion-general-customers-data.md) · [`09-seguridad-y-respaldo.md`](09-seguridad-y-respaldo.md) · [`18-integracion-impuesto-unico-imposiciones.md`](18-integracion-impuesto-unico-imposiciones.md)
- Código reutilizable: `spike_f29/notion_lookup.py`, `spike_f29/motor_f29.py`.
- Notion: [Webhook actions](https://www.notion.com/help/webhook-actions) · [Database automations](https://www.notion.com/help/database-automations) · [API Webhooks 2026](https://fazm.ai/blog/notion-api-webhooks-support-2026) · [API reference](https://developers.notion.com/reference/intro).

---

**Anterior:** [`22-via-oficial-certificado-digital-api-sii.md`](22-via-oficial-certificado-digital-api-sii.md) · **Volver al** [`README`](README.md)

---

## Cambio 16-nov-2026 — cuenta BCI (Gcp Patrimonio) para clientes 27 Bis y Cumplimiento

Los clientes del Contable cuya columna **`asignacion `** (select, con espacio final) es **`27 Bis`** o
**`Cumplimiento`** transfieren a la cuenta de **Gcp Patrimonio Estratégico Limitada** (Banco Bci, cuenta
corriente en pesos N° 70839202, RUT 78.456.673-0), no a la Santander de Inversora GCP.

- `app._procesar_page` lee la categoría (búsqueda tolerante con `_clave_prop`) y pasa
  `cuenta=es.CUENTA_PATRIMONIO` a `es.enviar` → `es.render`. Categorías en
  `CATEGORIAS_CUENTA_PATRIMONIO` (app.py); datos bancarios en `BANCO_PATRIMONIO_*` (email_sender.py).
- **Con impuesto a pagar:** recuadro "Datos para transferir el impuesto" con la cuenta BCI y
  "Enviar comprobante a: finanzas@inversoragcp.com", aunque la fila no tenga honorarios. Va en el mismo
  marcador que la fecha límite (no se tocaron las plantillas).
- **Honorarios:** el recuadro de honorarios de esos clientes muestra la cuenta BCI, nunca la Santander.
- Resto de las categorías: sin cambios. El log del envío incluye `cuenta=gcp|patrimonio`.
- Tests: `tests/test_f29_cuenta_patrimonio.py`.
