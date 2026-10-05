# 32 · Automatización CRM Comercial — correo de cobranza

> **Propósito:** botón por fila en la base **🔓 CRM Comercial** → `/webhook/crm` → correo de
> **cobranza de honorarios** al cliente, desde un remitente **FIJO** (`finanzas@inversoragcp.com`),
> con la cuenta bancaria de GCP y (opcional) el monto de la tarifa mensual.
>
> **Fuente de verdad: el código.** Manda `handlers/crm.py`, `app.py`, `email_sender.py`,
> `email_templates/crm_email.*`. Este doc describe lo construido y verificado el **20-jul-2026**.

---

## §0 · Lo que la distingue de F29/RRHH/Tickets

La novedad de esta automatización es el **remitente único fijo**. En F29/RRHH/Tickets el correo sale
de la cuenta del **asesor asignado a la fila** (columna people/select). Aquí **no**: todos los correos
de esta página salen de **`finanzas@inversoragcp.com`**, la cuenta del área de Finanzas. Es "el único
correo que envía en esta página".

Se implementó **sin tocar `email_sender.py`**: `finanzas@` se registró como un **remitente virtual**
en `asesores_smtp.json` (entrada `"Finanzas"`), reutilizando toda la lógica de remitente/firma
existente. Como el **Domain Authentication** de `inversoragcp.com` ya está verificado en SendGrid,
envía sin configuración adicional.

---

## §1 · Identidad de la base (verificado por API, 20-jul-2026)

| Campo | Valor |
|---|---|
| Título | 🔓 CRM Comercial |
| database ID | `d50c74969fbe4ec98164f74410f3633b` |
| data source ID | `2961d0d2-de59-4fd5-b340-8930f6275101` |

---

## §2 · Columnas usadas (nombres EXACTOS)

**Lectura:**

| Rol | Columna | Tipo | Nota |
|---|---|---|---|
| Nombre / razón social | `Sw` | title | también es el **identificador** de la fila |
| Destinatario | `email` | email | **minúscula** |
| Monto (opcional) | `Honorario del mes` | number | si vacío/0 → correo sin tarjeta de monto |
| Adjuntos (opcional) | `Archivos y multimedia` | files | se adjuntan al correo (ej. la factura); vacío → sin adjuntos |

**Write-back (3 columnas creadas el 20-jul vía `update_data_source`):**

| Columna | Tipo | Valor que escribe el backend |
|---|---|---|
| `Estado Correo` | status | **`Listo`** (opciones auto-creadas: `Sin empezar` / `En curso` / `Listo`) |
| `Fecha envío` | date | timestamp UTC del envío (e **minúscula**) |
| `Entrega Correo` | rich_text | lo escribe `/webhook/sendgrid` (`✅ Entregado` / `❌ …`) |

> ⚠️ El status auto-creado **no** tiene opción "Enviado"; por eso el sent status es **`Listo`**
> (`STATUS_ENVIADO` en `handlers/crm.py`), igual que RRHH. Write-back **tolerante**: si una columna
> no está en la fila, solo se loguea (nunca tumba el envío ya hecho).

---

## §3 · Correo

- **Remitente:** `finanzas@inversoragcp.com`, mostrado como **"Finanzas · GCP"**.
- **Firma (texto):** **Finanzas** / *Inversora GCP Ltda* (`firma_cargo` en `asesores_smtp.json`).
- **Asunto (fijo):** `Honorarios servicios contables Inversora GCP`.
- **Plantilla:** `email_templates/crm_email.html` + `.txt`.
- **Cuerpo:** recordatorio de pago de honorarios + tarjeta de monto (si hay tarifa) + **cuenta
  bancaria GCP** (constante `BANCO_GCP_HTML`/`_TXT` de `email_sender.py`, la misma de Tickets cobranza) +
  cierre. Marcadores: `bloque_mensaje`, `bloque_monto`, `bloque_banco` (+ variantes `linea_*` para el txt).

Cuenta bancaria (ya constante en el código, no se duplicó):

```
Banco Santander · Cuenta Corriente
N° 0-000-8577678-9
RUT: 76.976.672-3
Razón Social: Inversora GCP Ltda
```

---

## §4 · Flujo técnico

1. **Botón Notion** (Send webhook) → `POST /webhook/crm`, header `X-AuditAI-Secret`.
2. `app.py :: webhook_crm()` → `_procesar_webhook_generico(crm_handler.procesar, "CRM")`.
3. **Identificación de la fila:** por `Sw` (title). El botón de Notion no manda un `page_id` utilizable
   (doc 24 §6), así que el `Sw` es el camino principal → `nc.find_page_by_title_generico(sw, DS_ID, "Sw")`
   (filtro **`title`**, no `rich_text`; un rich_text sobre un title da 400 — por eso el helper nuevo).
4. `handlers/crm.py :: procesar(page_id)`: lee `Sw`/`email`/`Tarifa`, arma el correo, `es.enviar(...)`
   con `nombre_asesor="Finanzas"` (remitente fijo) y `custom_args={page_id, flujo:"crm"}`.
5. **Write-back tolerante:** `Estado Correo=Listo` + `Fecha envío=now`.
6. **Confirmación de entrega:** SendGrid postea a `/webhook/sendgrid`; el `flujo="crm"` está registrado
   en `_handler_por_flujo`, así que el **reintento automático ante el primer rebote** también aplica, y
   la columna `Entrega Correo` se actualiza igual que en los otros flujos.

**Avisos de error (según lo pedido: "Admin + Carlos"):** ante cualquier fallo conocido (sin `email`,
sin `Sw`, rechazo de SendGrid), `alertas.avisar_fallo_asesor("Carlos Cereceda", …, flujo="crm")` manda
el **didáctico a Carlos** y el **técnico al admin** (`ADMIN_ALERT_EMAIL`).

---

## §5 · Configurar el botón en Notion (pasos)

En **🔓 CRM Comercial**:

1. Agregar una propiedad tipo **Button** (ej. nombre "Enviar cobranza").
2. Acción **Send webhook**:
   - **URL:** `https://<tu-servicio-render>/webhook/crm`
   - **Headers:** `X-AuditAI-Secret` = el mismo valor de `WEBHOOK_SECRET` en Render.
   - **Content / propiedades a incluir:** asegurarse de incluir **`Sw`** (es el identificador de la fila).
3. (Opcional) Fijar `Estado Correo = Sin empezar` como default en filas nuevas para leer de un vistazo
   cuáles ya se enviaron (`Listo`).

> **Antes de apretar en un cliente real:** probar en una fila de prueba (con un `email` propio) y
> confirmar que llega el correo, que `Estado Correo` pasa a `Listo` y que `Fecha envío` se rellena.

---

## §6 · Piezas creadas / modificadas

| Archivo | Cambio |
|---|---|
| `handlers/crm.py` | **nuevo** — handler de cobranza (remitente fijo, monto opcional, banco, write-back). |
| `email_templates/crm_email.html` / `.txt` | **nuevas** — plantilla de cobranza. |
| `asesores_smtp.json` | **+entrada** `finanzas@inversoragcp.com` (remitente virtual "Finanzas"). |
| `notion_client.py` | **+`find_page_by_title_generico()`** (búsqueda por columna title). |
| `app.py` | **+endpoint** `/webhook/crm`, ramas del router para `"CRM"`, `flujo="crm"` en el reintento. |
| `tests/test_crm.py` | **nuevo** — 6 tests (remitente fijo, monto, banco, avisos a Carlos, write-back). |
| Notion (CRM Comercial) | **+3 columnas** `Estado Correo` (status), `Fecha envío` (date), `Entrega Correo` (text). |

---

## §7 · Limitaciones conocidas (MVP)

- **Bounce → aviso:** en `_procesar_evento_sendgrid` (app.py) el aviso de rebote lee `Adviser Accounting`
  y `Month`, que **no existen** en CRM Comercial; para un rebote CRM el aviso sale con asesor/período
  vacíos hacia `notificaciones@`+admin (no hacia Carlos). El write-back de `Entrega Correo` sí funciona
  (usa `_clave_prop`, genérico). Aceptable para el MVP; si molesta, se rutea el aviso por `flujo`.
- **Búsqueda por `Sw`:** exige match **exacto** del título (mayúsculas/espacios). Si el botón llegara a
  mandar un `page_id` utilizable en el futuro, ese camino tiene prioridad automática.

---

**Anterior:** [`31-mejoras-robustez-correo-jul-2026.md`](31-mejoras-robustez-correo-jul-2026.md) · **Volver al** [`README`](README.md)

---

## §9 · Cambio 05-oct-2026 — monto del mes + adjuntos

La base fue renombrada a **🔓 Gestión de Cobros** (mismo data source `2961d0d2-…`, verificado por API).
La columna `Tarifa de cobro mensual` **ya no existía**, por lo que el correo salía sin tarjeta de monto.

- **Monto:** ahora se lee de `Honorario del mes` (number). Misma regla: vacío/0 → sin tarjeta.
- **Adjuntos:** los archivos de `Archivos y multimedia` se adjuntan al correo, reutilizando la misma
  descarga que Tickets (`nc.files` + `es._descargar_adjuntos`, tope total `MAX_ADJUNTOS_MB` = 15 MB).
  Columna ausente o vacía → el correo sale igual, sin adjuntos.
- **Tests:** 3 nuevos en `tests/test_crm.py` (nombres de columnas, adjuntos, sin adjuntos).
- Las URLs de archivos subidos a Notion expiran ~1 h, pero se leen en el mismo momento del clic,
  así que no afecta.

