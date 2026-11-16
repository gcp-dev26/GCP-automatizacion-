"""Backend AuditAI. Recibe el webhook del botón "Enviar F29", lee la fila
de Contable Junio en memoria, envía el correo desde la cuenta del asesor
asignado, y escribe de vuelta el Status. R3/R4.

Uso:
  python app.py                # arranca el servidor Flask en puerto 8000

Sin Notion (prueba local directa):
  python app.py --test <page_id>   # lee la fila y envía el correo sin webhook
"""
from __future__ import annotations
import os
import sys
import time
import threading
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, abort
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv
import datetime
try:
    from zoneinfo import ZoneInfo
    _TZ_CHILE = ZoneInfo("America/Santiago")   # maneja el horario de verano solo
except Exception:   # zoneinfo/tzdata no disponible: _fecha_local cae a UTC
    _TZ_CHILE = None
import notion_client as nc
import email_sender as es
import alertas
import diagnostico
import handlers.rrhh as rrhh_handler
import handlers.tickets as tickets_handler
import handlers.crm as crm_handler

load_dotenv()

# Configurar logging SIN PII (no loguear email, monto, RUT).
# FileHandler escribe directo a archivo (sin buffering de stdout/stderr).
_fh = RotatingFileHandler("auditai.log", maxBytes=200000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_fh, logging.StreamHandler()],
)
log = logging.getLogger("auditai")

app = Flask(__name__)

# Fase 0.2 (doc 27, H9): el WEBHOOK_SECRET es OBLIGATORIO. Sin él, cualquiera
# que conozca la URL podría disparar correos a los clientes. Si falta, los
# webhooks responden 503 (y /health lo delata) en vez de aceptar requests.
if not os.environ.get("WEBHOOK_SECRET"):
    logging.getLogger("auditai").critical(
        "WEBHOOK_SECRET NO esta configurado: los webhooks responderan 503 "
        "hasta que se defina la env var (Render -> Environment)."
    )


def _validar_secreto():
    """Valida X-AuditAI-Secret. 503 si el servidor no tiene secreto configurado
    (config incompleta, fail-safe); 401 si el header no coincide."""
    secreto_esperado = os.environ.get("WEBHOOK_SECRET", "")
    if not secreto_esperado:
        log.error("WEBHOOK_SECRET ausente en el servidor · path=%s · 503", request.path)
        abort(503, "backend sin WEBHOOK_SECRET configurado; avisar al administrador")
    if request.headers.get("X-AuditAI-Secret") != secreto_esperado:
        log.warning("secreto invalido o ausente · path=%s · 401", request.path)
        abort(401)


# --- Idempotencia anti-doble-correo (Fase 2.2, doc 27, H5) ---
# Un doble-click (o un reintento del cliente) no debe mandar dos correos al mismo
# destinatario. Se reserva el page_id al entrar; si el envio tuvo EXITO, la
# reserva vive DEDUPE_VENTANA_S y un segundo request lo ignora. Si el envio
# FALLO, se libera enseguida para permitir un reintento legitimo (no hubo correo
# que duplicar). Memoria del proceso: con gunicorn sync (1 worker, el default de
# este deploy) las requests son secuenciales, asi que el dict basta. Limitacion
# conocida: con varios workers/procesos el dedupe no se comparte entre ellos.
DEDUPE_VENTANA_S = 60
_dedupe_lock = threading.Lock()
_dedupe: dict[str, float] = {}


def _dedupe_reservar(page_id: str) -> bool:
    """True si se reservo (seguir procesando); False si es un duplicado reciente
    (ignorar). Limpia de paso las reservas vencidas."""
    ahora = time.time()
    with _dedupe_lock:
        for pid in [p for p, t in _dedupe.items() if ahora - t > DEDUPE_VENTANA_S]:
            del _dedupe[pid]
        if page_id in _dedupe:
            return False
        _dedupe[page_id] = ahora
        return True


def _dedupe_liberar(page_id: str) -> None:
    """Libera la reserva (tras un fallo) para permitir un reintento inmediato."""
    with _dedupe_lock:
        _dedupe.pop(page_id, None)


# Fallos donde apretar el botón otra vez SIN editar la fila no puede dar otro
# resultado: falta un dato o la configuración está mala. Para estos NO se libera
# la reserva del dedupe, así el reintento a ciegas se ignora en vez de disparar
# otro par de correos de error (asesor + dev).
#
# Motivo: 05-ago-2026 llegaron decenas de avisos por filas de RRHH sin
# `MONTO IMPOSICIONES|`. Cada clic repetido = 2 correos más, sin tope. Los fallos
# transitorios (timeout SMTP, 500 de Notion) SÍ se siguen liberando: ahí el
# segundo intento sí puede funcionar.
_FALLOS_PERSISTENTES = frozenset({
    "sin_monto", "sin_email", "sin_titulo", "email_invalido", "sin_mes",
    "config", "remitente_no_verificado", "asesor_pendiente",
})


def _fallo_persistente(motivo: str) -> bool:
    """True si reintentar sin tocar la fila es inútil (ver _FALLOS_PERSISTENTES).
    Reutiliza el clasificador de diagnostico.py para no duplicar la taxonomía.
    Ante cualquier duda devuelve False (= comportamiento anterior: liberar)."""
    if not motivo:
        return False
    try:
        return diagnostico.diagnosticar(motivo).categoria in _FALLOS_PERSISTENTES
    except Exception:
        return False


# Nombres EXACTOS de las propiedades en Contable Junio (ver esquema confirmado)
P_NOMBRE = "Customers"
P_EMAIL = "Email"
P_MES = "Month"
P_MONTO = "Impuestos"
P_STATUS = "Status"
P_HONORARIOS = "Honorarios Pendientes"
P_INFO_VALOR = "Valor-Info adicional"     # number — valor de la info adicional
P_INFO_MOTIVO = "Motivo-Info adicional"   # rich_text — motivo (Remanente, Saldo a favor, Pago adicional, Otro, o libre)
P_ADVISER = "Adviser Accounting"
P_ADJUNTOS = "Adjuntos"   # files & media — PDFs que se adjuntan al correo
P_MSG_ADJUNTOS = "Mensaje Adjuntos"   # rich_text — nota del asesor sobre los adjuntos
P_FECHA_ENVIO = "Fecha Envío"   # date — fecha y hora en la que se envió el correo
STATUS_ENVIADO = "1) Enviado y Pendiente"
# Categorías (columna 'asignacion ', con espacio final en Notion) cuyos clientes
# transfieren a la cuenta BCI de Gcp Patrimonio en vez de la Santander (16-nov-2026).
P_ASIGNACION = "asignacion"
CATEGORIAS_CUENTA_PATRIMONIO = {"27 bis", "cumplimiento"}


def _procesar_page(page_id: str) -> dict:
    """Lee la fila, envía el correo, actualiza Status. Devuelve {ok, ...}.
    No loguea PII (email, monto, RUT)."""
    page = nc.get_page(page_id)
    _validar_contable_vigente(page)   # R4: nada de correos desde una copia vieja
    props = page["properties"]

    email = nc.plain(props.get(P_EMAIL, {}))
    nombre = nc.plain(props.get(P_NOMBRE, {}))
    rut = nc.plain(props.get("Rut", {}))
    mes = nc.plain(props.get(P_MES, {}))
    monto = nc.plain(props.get(P_MONTO, {}))
    honorarios = nc.plain(props.get(P_HONORARIOS, {}))
    info_valor = nc.plain(props.get(P_INFO_VALOR, {}))
    info_motivo = nc.plain(props.get(P_INFO_MOTIVO, {}))
    asesor_nombres = nc.people_names(props.get(P_ADVISER, {}))
    nombre_asesor = asesor_nombres[0] if asesor_nombres else ""
    adjuntos = nc.files(props.get(P_ADJUNTOS, {}))
    msg_adjuntos = nc.plain(props.get(P_MSG_ADJUNTOS, {}))
    clave_asig = _clave_prop(props, P_ASIGNACION)
    categoria = nc.plain(props.get(clave_asig, {})) if clave_asig else ""
    cuenta = (es.CUENTA_PATRIMONIO
              if categoria.strip().lower() in CATEGORIAS_CUENTA_PATRIMONIO else "")

    # El mes se determina PRIORITARIAMENTE desde el título de la base parent
    # ("Contable Julio" -> "Julio 2026"). Cuando Carlos duplica una página
    # Contable y la renombra, las filas conservan el Month del mes anterior
    # (Notion no resetea el valor al duplicar), pero el TÍTULO de la base sí
    # refleja el mes nuevo. Por eso el título es la fuente principal; el Month
    # de la fila queda como fallback solo si el título no se puede parsear
    # (caso base sin el patrón "Contable <Mes>"). Ver doc 28 §4.
    mes_fila = mes
    mes_titulo = nc.derivar_month_desde_base(page)
    mes = mes_titulo or mes_fila
    mes_origen = "titulo_base" if mes_titulo else ("month_fila" if mes_fila else "vacio")

    # Log diagnostico del cambio de mes (doc 28 §4.a). El mes NO es PII
    # (es un periodo publico "Julio 2026"). db_id tampoco. nos ayuda a
    # detectar si get_database_title no esta retornando lo esperado.
    db_id = (page.get("parent") or {}).get("database_id", "")
    log.info(
        "diagnostico mes · page_id=%s db_id=%s mes_fila=%r mes_titulo=%r mes_final=%r origen=%s",
        page_id, db_id, mes_fila, mes_titulo, mes, mes_origen,
    )

    # Log sin PII (resto)
    log.info(
        "page_id=%s cliente=%r asesor=%r mes_present=%s mes_origen=%s hono_present=%s info_valor_present=%s info_motivo_present=%s msg_adj_present=%s adjuntos_n=%d cuenta=%s",
        page_id, nombre, nombre_asesor, bool(mes), mes_origen,
        bool(honorarios), bool(info_valor), bool(info_motivo), bool(msg_adjuntos), len(adjuntos),
        cuenta or "gcp",
    )

    def _alertar_error(motivo: str):
        alertas.avisar_fallo_asesor(nombre_asesor, nombre, mes, motivo,
                                    flujo="f29", page_id=page_id)

    # Fallback igual que RRHH: si la fila del Contable no trae Email, buscarlo
    # por RUT en la base madre (solo lectura). Cierra los casos de filas con
    # Email vacio cuyo correo SI existe en la central (visto 15-jul: CONSTRUGLOBAL,
    # Neurocirugia, NATALIA, ZOE). Si la central tampoco lo tiene, se alerta.
    if not email and rut:
        encontrado = nc.buscar_email_en_central(rut)
        if encontrado:
            email = encontrado
            log.info("email F29 recuperado desde base central · page_id=%s", page_id)

    if not email:
        _alertar_error("Fila sin correo electrónico (Email) y no se encontró en la base central por RUT.")
        return {"ok": False, "motivo": "fila sin Email (ni en la fila ni en la base central)"}

    if not mes:
        _alertar_error("Fila sin mes (ni Month ni título de la base parent con patrón 'Contable <Mes>'), necesario para calcular la fecha límite.")
        return {"ok": False, "motivo": "fila sin mes (ni Month ni título parseable de la base parent), necesario para fecha límite"}

    try:
        remitente = es.enviar(
            destinatario=email,
            nombre=nombre,
            mes=mes,
            monto=monto or "0",
            nombre_asesor=nombre_asesor,
            honorarios=honorarios,
            info_valor=info_valor,
            info_motivo=info_motivo,
            msg_adjuntos=msg_adjuntos,
            adjuntos=adjuntos,
            custom_args={"page_id": page_id, "flujo": "f29"},
            cuenta=cuenta,
        )
        log.info("correo enviado OK · page_id=%s remitente=%s", page_id, remitente)
    except ValueError as exc:
        log.error("error envio · page_id=%s · %s", page_id, exc)
        _alertar_error(str(exc))
        return {"ok": False, "motivo": str(exc)}
    except Exception as exc:
        log.error("error SMTP · page_id=%s · %s", page_id, exc)
        _alertar_error(f"Error de envío: {exc}")
        return {"ok": False, "motivo": f"error SMTP: {exc}"}

    # Write-back del Status y Fecha Envío. Tolerante: escribe solo las columnas
    # que existen en la fila. Un PATCH con una propiedad inexistente falla
    # completo, y antes eso tumbaba tambien el Status en silencio (la columna
    # date se llamaba "Fecha", no "Fecha Envío"). Ver doc 24 §5.1.
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    updates = {}
    if P_STATUS in props:
        updates[P_STATUS] = {"status": {"name": STATUS_ENVIADO}}
    else:
        log.warning("columna %r no existe en Contable; no se escribe status", P_STATUS)
    if P_FECHA_ENVIO in props:
        updates[P_FECHA_ENVIO] = {"date": {"start": now_iso}}
    else:
        log.warning("columna %r no existe en Contable; no se escribe fecha", P_FECHA_ENVIO)
    if updates:
        try:
            nc.update_props(page_id, updates)
            log.info("write-back OK (%s) · page_id=%s -> %s", ", ".join(updates), page_id, STATUS_ENVIADO)
        except Exception as exc:
            log.warning("no se pudo actualizar status/fecha · page_id=%s · %s", page_id, exc)

    return {"ok": True, "remitente": remitente}


def _estructura(d, profundidad=0):
    """Devuelve un dict anidado con solo keys y tipos (sin valores = sin PII)."""
    if profundidad > 4 or not isinstance(d, dict):
        return type(d).__name__
    return {k: _estructura(v, profundidad + 1) for k, v in d.items()}


def _contexto_request() -> str:
    """Quién mandó la request y con qué forma de body, para el correo de aviso.

    Sin PII de clientes: solo cabeceras de transporte. Nace del 06-ago-2026:
    llegaron 400 con el payload vacío y no había manera de distinguir a un
    asesor apretando el botón de alguien probando la configuración del webhook
    en Notion (el 'Test' de la acción manda un body vacío). Solo se llama al
    construir un aviso, así que el costo no está en el camino feliz."""
    ua = request.headers.get("User-Agent") or "(sin User-Agent)"
    # Render corre detrás de un proxy: la IP real viene en X-Forwarded-For.
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "")
    ip = ip.split(",")[0].strip() or "(desconocida)"
    try:
        crudo = request.get_data(cache=True) or b""
    except Exception:
        crudo = b""
    if not crudo:
        forma = "body VACIO (0 bytes) — tipico del 'Test' del webhook en Notion o de un boton sin contenido configurado"
    elif request.get_json(force=True, silent=True) is None:
        forma = f"body ILEGIBLE, no es JSON ({len(crudo)} bytes)"
    else:
        forma = f"body JSON de {len(crudo)} bytes"
    ct = request.headers.get("Content-Type") or "(ninguno)"
    return f"IP {ip} · User-Agent: {ua} · Content-Type: {ct} · {forma}"


def _extraer_plano_notion(v: dict) -> str:
    """Extrae un valor plano de un objeto propiedad Notion, con o sin campo
    `type` explicito. Prueba rich_text, title, email, url, number, phone_number,
    select, status, people. Devuelve '' si no encuentra nada."""
    if not isinstance(v, dict):
        return ""
    t = v.get("type")
    # Si tiene type, usar la via estandar
    if t:
        try:
            p = nc.plain(v)
            if p:
                return p
        except Exception:
            pass
    # Fallbacks sin type: buscar campos conocidos directamente
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
    """Busca recursivamente la primera clave cuyo nombre este en `claves`
    (case-insensitive). Devuelve (valor, ruta) o (None, ""). Profundidad
    limitada. Maneja 3 formatos: string plano, objeto propiedad Notion
    (con o sin `type`), y dict anidado. No loguea valores (PII)."""
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
    return (
        len(s) == 36
        and s.count("-") == 4
        and all(c in "0123456789abcdef-" for c in s.lower())
    )


_DS_VIGENTE_TTL_S = 300
_ds_vigente_cache: dict[str, tuple[float, str]] = {}


def _ds_vigente(prefijo: str, conocido: str) -> str:
    """Data source de la planilla '<prefijo> <mes>' vigente, resuelta por TÍTULO
    en runtime. Las planillas se renombran en sitio ('Contable Junio' ->
    'Contable Julio'), así que el id no cambia pero la lista estática del código
    queda con el nombre viejo; y el día que se estrene una base nueva, el search
    la encuentra sola. Si el search falla, cae a `conocido`.

    Cachea 5 min POR PREFIJO: desde que el guard R4 valida la base, esto corre en
    CADA clic, y la planilla vigente cambia una vez al mes. El TTL corto hace que
    un cambio de mes se note solo, sin reiniciar el servicio."""
    ahora = time.time()
    ts, val = _ds_vigente_cache.get(prefijo, (0.0, ""))
    if val and ahora - ts < _DS_VIGENTE_TTL_S:
        return val
    try:
        import reconciliar
        val = reconciliar.resolver_ds_actual(prefijo, conocido)
    except Exception as exc:
        log.warning("no se pudo resolver la planilla vigente de %s, uso la conocida: %s", prefijo, exc)
        val = conocido
    _ds_vigente_cache[prefijo] = (ahora, val)
    return val


def _ds_contable_vigente() -> str:
    """Data source del Contable del mes vigente."""
    return _ds_vigente("Contable", nc.DS_CONTABLE_JUNIO)


def _ds_rrhh_vigente() -> str:
    """Data source de la planilla RRHH del mes vigente."""
    return _ds_vigente("RRHH", rrhh_handler.DS_ID)


def _ds_de_la_fila(page: dict) -> str:
    """Data source (sin guiones) de la base donde vive la fila, o '' si no se
    puede determinar. El page object trae `parent.data_source_id` (API
    2025-09-03); si solo viniera `database_id`, se resuelve."""
    parent = page.get("parent") or {}
    ds = parent.get("data_source_id") or ""
    if ds:
        return ds.replace("-", "")
    db = parent.get("database_id") or ""
    if not db:
        return ""
    try:
        return (nc.get_data_source_id(db) or "").replace("-", "")
    except Exception as exc:
        log.warning("no se pudo resolver el data source de la fila: %s", exc)
        return ""


def _validar_base_vigente(page: dict, prefijo: str, vigente: str) -> None:
    """R4 (doc 23): la fila DEBE vivir en la planilla '<prefijo> <mes>' vigente.

    Incidente 06-ago-2026: al cambiar de mes se duplica la planilla como
    respaldo ('Contable Junio (1)'). El duplicado se lleva la columna botón,
    apunta al mismo backend y sus filas siguen clicables. Sin esta validación
    el backend mandaba el correo igual — 200 OK y un cliente recibiendo el F29
    de un mes viejo. El 400 que se vio fue la versión ruidosa del problema; la
    silenciosa (99 filas con Email en ese duplicado) era la peligrosa.

    Autorizada = SOLO la vigente resuelta en runtime. Las listas estáticas
    (DS_CONTABLES, DS_RRHH) no se usan como allowlist a propósito: el día que se
    trabaje sobre una copia nueva en vez de resetear en sitio, la base de esa
    lista pasa a ser el respaldo — seguiría autorizada y volveríamos al correo
    con datos viejos. Siguen siendo el fallback DENTRO de `_ds_vigente()`, que es
    donde corresponde: solo si el search de Notion se cae.

    Fail-closed SOLO cuando sabemos que la base es otra. Si no se puede
    determinar (Notion cambia la forma del page object), se deja pasar con un
    warning: cortar todos los envíos por un cambio de forma sería peor."""
    ds_fila = _ds_de_la_fila(page)
    if not ds_fila:
        log.warning("no se pudo determinar la base de la fila; R4 no aplicada")
        return

    vigente = (vigente or "").replace("-", "")
    if not vigente or ds_fila == vigente:
        return

    titulo = ""
    try:
        titulo = nc.get_database_title((page.get("parent") or {}).get("database_id", "")) or ""
    except Exception:
        pass
    # Nombrar la planilla correcta convierte el error en una instrucción: el
    # asesor se resuelve solo, sin escribirle a nadie.
    vigente_nombre = ""
    try:
        import reconciliar
        vigente_nombre = reconciliar.nombre_base_actual(prefijo, "")
    except Exception:
        pass
    log.warning("fila fuera de la planilla %s vigente · base=%r ds=%s", prefijo, titulo, ds_fila[:8])
    abort(403, (f"esta fila esta en {titulo or 'una planilla desconocida'!r}, que no es la "
                "planilla del mes vigente"
                + (f". La vigente es {vigente_nombre!r}" if vigente_nombre else "")
                + ". Los respaldos y los meses ya cerrados no envian correos: "
                "apreta el boton en la planilla del mes en curso."))


def _validar_contable_vigente(page: dict) -> None:
    """R4 para el flujo F29 (planillas 'Contable <Mes>')."""
    _validar_base_vigente(page, "Contable", _ds_contable_vigente())


def _validar_rrhh_vigente(page: dict) -> None:
    """R4 para el flujo RRHH (planillas 'RRHH <MES> <AÑO>').

    No existía hasta el 19-ago-2026: cuando se implementó el de Contable
    (06-ago), RRHH estrenaba una base nueva cada mes y el respaldo con botón
    clicable no existía como concepto. Desde el cambio de mes del 18-ago RRHH
    rota igual que Contable (duplicar respaldo + renombrar la original), así que
    el mismo riesgo aplica acá. Ver doc 28 §19."""
    _validar_base_vigente(page, "RRHH", _ds_rrhh_vigente())


def _buscar_identificador_base(data: dict) -> tuple[str, str]:
    """Identificador de la fila común a TODOS los botones: `page_id` → payload
    tipo page-object (`data.id` / `entity.id`, que mandan las automatizaciones
    nuevas de Notion) → `Rut`. Devuelve (ident, ruta) o ("", "").

    Vive acá y no duplicado en cada endpoint por el incidente del 06-ago-2026: el
    fallback de page-object se habia agregado solo a _procesar_webhook_generico,
    asi que /enviar-f29 rechazaba con 400 los payloads que RRHH sí aceptaba."""
    ident, ruta = _buscar_clave(data, ["page_id"])
    if ident:
        return ident, ruta
    for k in ("data", "entity"):
        v = data.get(k)
        pid = v.get("id") if isinstance(v, dict) else None
        if isinstance(pid, str) and _es_uuid(pid):
            return pid, f"{k}.id"
    ident, ruta = _buscar_clave(data, ["Rut", "rut", "RUT"])
    return (ident, ruta) if ident else ("", "")


def _procesar_webhook_generico(handler, nombre_handler: str):
    """Lógica común para todos los webhooks de botones Notion.
    - Valida X-AuditAI-Secret
    - Extrae page_id o RUT del payload
    - Llama al handler específico con el page_id
    handler: función que recibe (page_id) -> dict
    """
    tiene_secreto = bool(request.headers.get("X-AuditAI-Secret"))
    log.info("request recibida · path=%s · tiene_secreto=%s", request.path, tiene_secreto)

    _validar_secreto()   # 503 si el server no tiene secreto; 401 si no coincide (H9)

    data = request.get_json(force=True, silent=True) or {}
    log.info("estructura payload %s: %s", nombre_handler, _estructura(data))

    # Fase 1.1 (doc 27, H7): TODO lo de aca abajo va envuelto en captura de
    # excepciones no previstas (bug, 500 de Notion, timeout raro). abort()
    # lanza HTTPException (400/401/404/503) y esos SI deben propagar tal cual
    # (son respuestas intencionales, no errores). page_id arranca vacio: si
    # la excepcion ocurre antes de identificar la fila, no hay nada que
    # loguear como identificador (evita filtrar un RUT si `ident` era PII).
    page_id = ""
    try:
        ident, ruta = _buscar_identificador_base(data)
        # Fallback RRHH: muchas filas tienen el RUT (title) vacio -> identificar
        # por CLIENTE (contingencia doc 25 §9). Solo si no hubo page_id ni RUT.
        prop_busqueda = "RUT"
        if not ident and nombre_handler == "RRHH":
            ident, ruta = _buscar_clave(data, ["CLIENTE", "Cliente", "cliente"])
            prop_busqueda = "CLIENTE"
        if not ident and nombre_handler == "TICKETS":
            ident, ruta = _buscar_clave(data, ["Tarea", "tarea", "Nombre"])
            prop_busqueda = "Tarea"
        # CRM Comercial: identificar por 'Sw' (title). El botón de Notion no manda
        # un page_id utilizable (doc 24 §6), así que el Sw es el camino principal.
        if not ident and nombre_handler == "CRM":
            ident, ruta = _buscar_clave(data, ["Sw", "sw"])
            prop_busqueda = "Sw"
        log.info("identificador en ruta=%r (valor no se loguea)", ruta)

        if not ident:
            abort(400, f"no se encontro page_id, Rut ni CLIENTE en el payload ({nombre_handler})")

        if _es_uuid(ident):
            page_id = ident
            log.info("usando page_id directo · %s", nombre_handler)
        else:
            if nombre_handler == "RRHH":
                # Base del mes VIGENTE resuelta en runtime: un DS fijo mandaba el
                # correo con los datos del mes anterior (31-jul-2026).
                page_id = nc.find_page_by_rut_generico(ident, _ds_rrhh_vigente(), prop_busqueda)
            elif nombre_handler == "TICKETS":
                from handlers.tickets import DS_ID as DS
                page_id = nc.find_page_by_rut_generico(ident, DS, prop_busqueda)
            elif nombre_handler == "CRM":
                from handlers.crm import DS_ID as DS
                # 'Sw' es title -> filtro title (no rich_text). Ver doc 32.
                page_id = nc.find_page_by_title_generico(ident, DS, prop_busqueda)
            else:
                page_id = nc.find_page_by_rut(ident, _ds_contable_vigente())
            if not page_id:
                log.warning("%s no encontrado · %s", prop_busqueda, nombre_handler)
                abort(404, f"no se encontro fila con ese {prop_busqueda} en {nombre_handler}")

        # R4 para los flujos que rotan de mes. Cuesta un GET extra por clic (el
        # handler vuelve a leer la fila), y a cambio un clic en el respaldo del
        # mes cerrado no manda el correo con los datos viejos. Va ANTES del
        # dedupe: un rechazo no debe consumir la reserva del page_id.
        # Tickets y CRM no rotan (backlog permanente), así que no aplican.
        if nombre_handler == "RRHH":
            _validar_rrhh_vigente(nc.get_page(page_id))

        if not _dedupe_reservar(page_id):
            log.info("duplicado ignorado (dedupe <%ds) · %s", DEDUPE_VENTANA_S, nombre_handler)
            return {"ok": True, "duplicado": True, "motivo": "ya procesado hace segundos, se ignora"}, 200
        exito = False
        motivo_fallo = ""
        try:
            resultado = handler(page_id)
            exito = bool(resultado.get("ok"))
            if not exito:
                motivo_fallo = resultado.get("motivo", "")
        finally:
            # Se libera solo si el reintento inmediato tiene sentido. Si falta un
            # dato en la fila, la reserva se mantiene: apretar de nuevo sin cargarlo
            # daría el mismo fallo y otro par de correos de error. Si el handler
            # reventó (motivo_fallo=""), se libera como antes: puede ser transitorio.
            if not exito and not _fallo_persistente(motivo_fallo):
                _dedupe_liberar(page_id)
    except HTTPException as exc:
        # Un 4xx acá es un clic REAL del botón que se rechazó: el asesor ve el error
        # en Notion y antes no se enteraba nadie más (caso Andrea, 02-ago). 401/503
        # los filtra alertas (ocurren antes de autenticar).
        alertas.avisar_boton_rechazado(nombre_handler, exc.code or 0,
                                       exc.description or "", page_id or "",
                                       str(_estructura(data)), _contexto_request())
        raise
    except Exception as exc:
        alertas.avisar_excepcion_admin(nombre_handler, page_id or "", exc)
        return {"ok": False, "motivo": "error interno del servidor; administrador notificado"}, 500

    return resultado, 200


@app.post("/webhook/rrhh")
def webhook_rrhh():
    """Webhook del botón 'Enviar Correo RRHH' en RRHH JUNIO 2026."""
    return _procesar_webhook_generico(rrhh_handler.procesar, "RRHH")


_TICKETS_TIPOS = {"avance", "completado", "cobranza"}


@app.post("/webhook/tickets/<tipo>")
def webhook_tickets(tipo):
    """Webhook de los botones de Tickets - Servicios. <tipo> elige la plantilla."""
    if tipo not in _TICKETS_TIPOS:
        abort(404, f"tipo de correo tickets desconocido: {tipo}")
    return _procesar_webhook_generico(
        lambda page_id: tickets_handler.procesar(page_id, tipo), "TICKETS"
    )


@app.post("/webhook/crm")
def webhook_crm():
    """Webhook del botón de cobranza en 'CRM Comercial'. Remitente fijo Finanzas."""
    return _procesar_webhook_generico(crm_handler.procesar, "CRM")


@app.post("/enviar-f29")
def enviar_f29():
    # Log al inicio: la request llego (sin PII). Antes del check del secreto.
    tiene_secreto = bool(request.headers.get("X-AuditAI-Secret"))
    log.info("request recibida · path=/enviar-f29 · tiene_secreto=%s", tiene_secreto)

    # R4 + H9: secreto compartido OBLIGATORIO (503 si el server no lo tiene)
    _validar_secreto()

    data = request.get_json(force=True, silent=True) or {}
    # Dump estructural completo (solo keys y tipos, sin valores = sin PII)
    log.info("estructura payload: %s", _estructura(data))

    # Fase 1.1 (doc 27, H7): captura de excepciones no previstas, igual que en
    # _procesar_webhook_generico. abort() (HTTPException) propaga tal cual.
    page_id = ""
    try:
        # Identificador: page_id / page-object / Rut (comun a todos los botones) y,
        # como ultimo recurso, el nombre del cliente — mismo patron que RRHH usa con
        # CLIENTE, porque hay filas del Contable con el Rut vacio.
        ident, ruta = _buscar_identificador_base(data)
        prop_busqueda = "Rut"
        if not ident:
            ident, ruta = _buscar_clave(data, ["Customers", "customers", "Cliente", "cliente"])
            prop_busqueda = P_NOMBRE
        log.info("identificador en ruta=%r (valor no se loguea)", ruta)

        if not ident:
            abort(400, "no se encontro page_id, Rut ni Customers en el payload")

        if _es_uuid(ident):
            page_id = ident
            log.info("usando page_id directo (sin query Notion)")
        elif prop_busqueda == P_NOMBRE:
            # 'Customers' es title -> filtro title (un rich_text sobre un title da 400)
            page_id = nc.find_page_by_title_generico(ident, _ds_contable_vigente(), P_NOMBRE) or ""
            if not page_id:
                log.warning("Customers no encontrado en el Contable vigente")
                abort(404, "no se encontro fila con ese Customers")
        else:
            # La planilla VIGENTE primero (resuelta por título en runtime): con
            # solo la lista estática, el día que aparece una planilla nueva este
            # fallback encuentra la fila del mes ANTERIOR — el bug que RRHH tuvo
            # el 31-jul y que Contable arrastraba igual. Ver doc 28 §19.
            page_id = nc.find_page_by_rut(ident, _ds_contable_vigente()) or ""
            if not page_id:
                log.warning("RUT no encontrado en el Contable (RUT no se loguea)")
                abort(404, "no se encontro fila con ese Rut")

        if not _dedupe_reservar(page_id):
            log.info("duplicado ignorado (dedupe <%ds) · F29", DEDUPE_VENTANA_S)
            return {"ok": True, "duplicado": True, "motivo": "ya procesado hace segundos, se ignora"}, 200
        exito = False
        motivo_fallo = ""
        try:
            resultado = _procesar_page(page_id)
            exito = bool(resultado.get("ok"))
            if not exito:
                motivo_fallo = resultado.get("motivo", "")
        finally:
            # Mismo criterio que _procesar_webhook_generico: los fallos por dato
            # faltante NO liberan la reserva (reintentar a ciegas solo genera más
            # correos de error); los transitorios sí.
            if not exito and not _fallo_persistente(motivo_fallo):
                _dedupe_liberar(page_id)
    except HTTPException as exc:
        alertas.avisar_boton_rechazado("F29", exc.code or 0, exc.description or "",
                                       page_id or "", str(_estructura(data)),
                                       _contexto_request())
        raise
    except Exception as exc:
        alertas.avisar_excepcion_admin("F29", page_id, exc)
        return {"ok": False, "motivo": "error interno del servidor; administrador notificado"}, 500

    return resultado, 200


# --- Reset de mes (doc 29) -------------------------------------------------
# Al rotar el mes (doc 28 §15: duplicar respaldo + renombrar la base original),
# las filas conservan los valores operativos del mes anterior. El botón
# "Reset Mes" limpia SOLO los campos dinámicos; los estáticos (Customers, Rut,
# Clave SII, Email, asesor, ...) NUNCA se tocan.
#
# Los ADJUNTOS son dinámicos en las dos planillas (corregido el 19-ago-2026):
# son los PDFs que se mandan en el correo DE ESE MES (doc 23 y doc 25 §77), no
# documentos permanentes del cliente. El doc 29 los había clasificado como
# estáticos y por eso Contable nunca los limpiaba: al revisar la planilla
# operativa había archivos de junio conviviendo con los de julio, es decir,
# clientes recibiendo el PDF de un mes que no era el suyo. El respaldo '(1)'
# del mes cerrado los conserva, que es justo para lo que existe.
#
# Limpiar una propiedad via PATCH requiere el payload vacío del TIPO correcto
# (rich_text -> lista vacía, number/date -> null); un null "crudo" a nivel de
# propiedad es un 400 de la API de Notion.
# Nombres de status verificados contra el esquema REAL de Contable Junio via
# API (13-jul-2026): "Status" usa "Not started" (default); "ARec" y "emision
# de boletas" usan "Sin empezar". Ventas/Compras/Control Solicitudes/
# solicitud-informe NO existen en Contable Junio: quedan por si aparecen en
# bases de otros meses (el matching tolerante los saltea si no están).
RESET_CONTABLE = {
    "Month": {"rich_text": []},
    "Impuestos": {"number": None},
    "Status": {"status": {"name": "Not started"}},
    "Ventas": {"checkbox": False},
    "Compras": {"checkbox": False},
    "Pre-Imptos": {"checkbox": False},
    "PreImp": {"checkbox": False},
    "ARec": {"status": {"name": "Sin empezar"}},
    "Control Solicitudes": {"status": {"name": "Sin empezar"}},
    "emision de boletas": {"status": {"name": "Sin empezar"}},
    "solicitud/informe /boletas": {"status": {"name": "Sin empezar"}},
    "Honorarios Pendientes": {"number": None},
    "Valor-Info adicional": {"number": None},
    "Motivo-Info adicional": {"rich_text": []},
    "Fecha Envío": {"date": None},
    "Adjuntos": {"files": []},              # PDFs del mes → se adjuntan al correo
    "Mensaje Adjuntos": {"rich_text": []},  # nota del asesor sobre esos PDFs
    "Entrega Correo": {"rich_text": []},    # "✅ Entregado · fecha" del envío del mes (doc 30)
    "Confirmar Reset": {"checkbox": False},
}
# RRHH JUNIO 2026 — nombres y opciones de status verificados contra el esquema
# real via API (15-jul-2026). El título de la base es "RUT" (no "Customers"),
# pero _titulo_fila() encuentra la propiedad title por su tipo, así que la fila
# de control RESET_MES funciona igual. Se resetean los campos del mes (montos,
# estados del flujo, correo, adjuntos del mes); NO se tocan los estáticos:
# CLIENTE, RUT, Email, ASISTENTE, USUARIO/CLAVE (Previred), DTGO, Nº. Trab.
RESET_RRHH = {
    "MONTO IMPOSICIONES|": {"number": None},   # el '|' es parte del nombre real
    "IMPUESTO ÚNICO": {"number": None},
    "Estado Correo": {"status": {"name": "Sin empezar"}},
    "Fecha envío": {"date": None},
    "Previred": {"status": {"name": "Not started"}},
    "Liquidaciones": {"status": {"name": "Not started"}},
    "Adjuntos": {"files": []},                 # liquidaciones del mes → se limpian
    "Comentario-Adjuntos": {"rich_text": []},
    "Confirmar reset": {"checkbox": False},
}
RESET_TICKETS: dict = {}   # Fase 3 (doc 29) — PENDIENTE: Tickets no rota por mes
                           # (cada fila es un trámite individual). Decisión aplazada
                           # por el usuario (15-jul); el tipo "tickets" da 400 hasta
                           # definir el alcance (solo Estado Correo vs correo+montos).
RESET_POR_TIPO = {"contable": RESET_CONTABLE, "rrhh": RESET_RRHH, "tickets": RESET_TICKETS}

# Columnas que se PRESERVAN a propósito: son del cliente, no del mes.
#
# No las usa el reset para decidir nada — para eso está el RESET_* de arriba.
# Existen para poder distinguir "esto no se toca" de "nadie lo clasificó todavía":
# al resetear, toda columna que no esté ni en el RESET_* ni acá se reporta en el
# log y en la respuesta. Los dos bugs de esta familia vivieron meses sin que nadie
# los viera — `Adjuntos` (PDFs de junio saliendo en correos de julio) y
# `Entrega Correo` (la confirmación de entrega del mes pasado, 102 filas) — y los
# dos habrían salido en el primer reset con esta lista puesta. Cuando aparezca una
# columna nueva, el mes que viene alguien la ve y decide: o al RESET_* o acá.
ESTATICAS_CONTABLE = {
    "Customers", "Rut", "Clave SII", "Email", "Email (1)", "CRM",
    "Adviser Accounting", "Actividad Econ", "Reportabilidad", "datos socio",
    "Place", "ID Central", "asignacion", "Seleccionar",
    "Archivos y multimedia",
    "Fecha",   # fechas sueltas de 2025/2026 cargadas a mano; no es del mes
}
ESTATICAS_RRHH = {
    "RUT", "CLIENTE", "Email", "ASISTENTE", "USUARIO", "CLAVE", "DTGO",
    "Nº. Trab.", "ID Central",
}
ESTATICAS_POR_TIPO = {"contable": ESTATICAS_CONTABLE, "rrhh": ESTATICAS_RRHH,
                      "tickets": set()}

# Tipos que Notion no deja escribir por API: no pueden resetearse ni aunque se
# quisiera, así que no tiene sentido reportarlos como "sin clasificar".
_TIPOS_NO_ESCRIBIBLES = frozenset({
    "button", "formula", "rollup", "unique_id", "created_time", "created_by",
    "last_edited_time", "last_edited_by",
})


def _columnas_sin_clasificar(props: dict, campos_reset: dict, estaticas: set) -> list[str]:
    """Columnas de la planilla que ni se resetean ni están declaradas estáticas.
    Mismo matching tolerante que `_clave_prop` (mayúsculas y espacios al borde:
    en Contable la columna real es `'asignacion '`, con espacio final)."""
    conocidas = {c.strip().lower() for c in campos_reset} | {e.strip().lower() for e in estaticas}
    return sorted(
        nombre for nombre, prop in props.items()
        if isinstance(prop, dict)
        and prop.get("type") not in _TIPOS_NO_ESCRIBIBLES
        and nombre.strip().lower() not in conocidas
    )

P_CONFIRMAR_RESET = "Confirmar Reset"   # checkbox — safety switch OBLIGATORIO
FILA_CONTROL_RESET = "RESET_MES"        # la confirmación se marca en esta fila de
                                        # control (título que CONTENGA "RESET_MES",
                                        # p. ej. "⚙️ RESET_MES"). Es una fila dedicada,
                                        # distinta de ZZ_TEST (pruebas de correo).

# Un reset por base a la vez: ~330 PATCHes tardan ~2 min y corren en un hilo de
# fondo (gunicorn en Render corre con timeout default de 30s, ver render.yaml:
# una request síncrona de 2 min mataría el worker y bloquearía los webhooks F29).
_resets_activos: set[str] = set()
_resets_lock = threading.Lock()


def _titulo_fila(props: dict) -> str:
    """Texto de la propiedad title de una fila (Customers/RUT/Tarea según base)."""
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return nc.plain(prop)
    return ""


def _clave_prop(props: dict, nombre: str) -> str | None:
    """Nombre REAL de una propiedad, tolerando mayúsculas y espacios al borde.
    En el esquema real de Contable Junio (verificado 13-jul-2026 via API) hay
    'Confirmar reset' (r minúscula) y 'emision de boletas ' (espacio al final):
    un match exacto los perdería en silencio."""
    objetivo = nombre.strip().lower()
    for k in props:
        if isinstance(k, str) and k.strip().lower() == objetivo:
            return k
    return None


# --- De qué planilla hablamos: el botón NO tiene que saberlo ----------------
# El header X-Reset-DB fue la causa del incidente del 18-ago-2026 (doc 28 §19).
# RRHH estrenaba una base NUEVA cada mes: el botón se duplicaba con ella y su
# header seguía apuntando a la base de junio. Cuando esa base se fue a la
# papelera, el reset empezó a fallar (500 'error leyendo la base') y no había
# forma de saber por qué sin leer los logs. Notion manda el page_id de la fila
# donde se apretó el botón: desde ahí se resuelve la planilla REAL, y el botón
# se queda sin configuración que pueda quedar vieja. Los headers siguen valiendo
# como fallback (curl, tests, y botones viejos que no manden datos de la fila).
def _base_del_clic(data: dict) -> tuple[str, str, str]:
    """(data_source_id, título, page_id) de la planilla donde se apretó el botón.
    ('', '', '') si el payload no trae una fila legible."""
    ident, _ruta = _buscar_identificador_base(data)
    page_id = ident if _es_uuid(ident) else ""
    if not page_id:
        return "", "", ""
    try:
        page = nc.get_page(page_id)
    except Exception as exc:
        log.warning("reset-mes: no se pudo leer la fila del clic · page_id=%s · %s", page_id, exc)
        return "", "", page_id
    parent = page.get("parent") or {}
    ds_id = parent.get("data_source_id") or ""
    db_id = parent.get("database_id") or ""
    if not ds_id and db_id:
        try:
            ds_id = nc.get_data_source_id(db_id)
        except Exception as exc:
            log.warning("reset-mes: no se pudo resolver el data source de la fila · %s", exc)
    titulo = ""
    if db_id:
        try:
            titulo = nc.get_database_title(db_id) or ""
        except Exception as exc:
            log.warning("reset-mes: no se pudo leer el titulo de la base · %s", exc)
    log.info("reset-mes: planilla del clic · titulo=%r ds=%s", titulo, (ds_id or "")[:8])
    return ds_id, titulo, page_id


_PREFIJO_A_TIPO = {"contable": "contable", "rrhh": "rrhh"}


def _tipo_desde_titulo(titulo: str) -> str:
    """'RRHH AGOSTO 2026' -> 'rrhh'; 'Contable Julio' -> 'contable'; '' si no se
    reconoce. Así el botón tampoco necesita el header X-Reset-Tipo: el tipo de
    planilla ya está en su nombre."""
    partes = (titulo or "").strip().split()
    return _PREFIJO_A_TIPO.get(partes[0].lower(), "") if partes else ""


def _vigente_del_tipo(tipo: str) -> tuple[str, str]:
    """(prefijo, data source vigente) para un tipo de planilla; ('', '') si el
    tipo no rota por mes (tickets) o no se reconoce."""
    if tipo == "contable":
        return "Contable", _ds_contable_vigente()
    if tipo == "rrhh":
        return "RRHH", _ds_rrhh_vigente()
    return "", ""


def _validar_planilla_reseteable(ds_id: str, titulo: str, tipo: str) -> None:
    """El reset es IRREVERSIBLE: borra los datos operativos de todas las filas.
    Por eso se niega a correr sobre las dos planillas donde sería un desastre:

    - un respaldo ('RRHH JULIO 2026 (1)'): es la única copia del mes cerrado;
    - un mes ya cerrado que no es el vigente.

    Fail-open si no se puede determinar (sin título o sin vigente resuelta): con
    el checkbox 'Confirmar reset' de por medio, bloquear un reset legítimo por no
    poder leer un título sería peor que el riesgo que cubre."""
    import reconciliar
    if titulo and reconciliar.es_copia(titulo):
        log.warning("reset-mes rechazado: %r es un respaldo", titulo)
        abort(403, (f"{titulo!r} es un respaldo (la copia '(n)' del mes cerrado) y el reset "
                    "la dejaria vacia: es la unica copia de esos datos. Apreta el boton en la "
                    "planilla del mes vigente."))
    prefijo, vigente = _vigente_del_tipo(tipo)
    if not vigente or not ds_id:
        return
    if ds_id.replace("-", "") == vigente.replace("-", ""):
        return
    vigente_nombre = ""
    try:
        vigente_nombre = reconciliar.nombre_base_actual(prefijo, "")
    except Exception:
        pass
    log.warning("reset-mes rechazado: %r no es la planilla vigente de %s", titulo, prefijo)
    abort(403, (f"{titulo or 'esta planilla'!r} no es la planilla del mes vigente"
                + (f" (la vigente es {vigente_nombre!r})" if vigente_nombre else "")
                + ". El reset solo corre sobre la planilla en curso, para no borrar "
                "el historico de un mes ya cerrado."))


def _reset_aplicar(tipo: str, ds_id: str, campos_reset: dict, filas: list[dict]) -> dict:
    """Aplica el reset fila por fila (corre en un hilo de fondo). Tolerante:
    escribe solo las columnas que existen en cada fila (doc 24 §5.1) y un fallo
    en una fila no aborta el resto. Si hubo fallos, avisa al admin. No loguea
    PII (solo page_ids y conteos)."""
    reseteadas, fallidas = 0, 0
    try:
        for fila in filas:
            page_id = fila.get("id", "")
            props = fila.get("properties", {}) or {}
            # matching tolerante: escribe con el nombre REAL de la columna
            # ('Confirmar reset', 'emision de boletas ') aunque difiera en
            # mayúsculas o espacios del nombre canónico del RESET dict
            updates = {}
            for campo, valor in campos_reset.items():
                real = _clave_prop(props, campo)
                if real:
                    updates[real] = valor
            if not (page_id and updates):
                continue
            try:
                nc.update_props(page_id, updates)
                reseteadas += 1
            except Exception as exc:
                fallidas += 1
                log.warning("reset-mes: fallo al resetear fila · page_id=%s · %s", page_id, exc)
        log.info(
            "reset-mes completado · tipo=%s · filas_reseteadas=%d · filas_fallidas=%d",
            tipo, reseteadas, fallidas,
        )
        if fallidas:
            alertas.avisar_excepcion_admin(
                "RESET", ds_id,
                RuntimeError(f"reset-mes tipo={tipo}: {fallidas} filas fallaron ({reseteadas} OK)"),
            )
    except Exception as exc:
        alertas.avisar_excepcion_admin("RESET", ds_id, exc)
    finally:
        with _resets_lock:
            _resets_activos.discard(ds_id)
    return {"ok": True, "tipo": tipo, "filas_reseteadas": reseteadas, "filas_fallidas": fallidas}


def _lanzar_reset(tipo: str, ds_id: str, campos_reset: dict, filas: list[dict]) -> None:
    """Arranca el reset en un hilo daemon (el endpoint responde enseguida)."""
    threading.Thread(
        target=_reset_aplicar, args=(tipo, ds_id, campos_reset, filas), daemon=True
    ).start()


@app.post("/reset-mes")
def reset_mes():
    """Webhook del botón 'Reset Mes' (doc 29). La planilla y el tipo salen de la
    FILA donde se apretó el botón (los headers X-Reset-DB / X-Reset-Tipo son solo
    fallback, ver `_base_del_clic`). Valida que no sea un respaldo ni un mes
    cerrado, exige el checkbox 'Confirmar reset' en la fila RESET_MES, y resetea
    los campos dinámicos de TODAS las filas en un hilo de fondo. Responde 202."""
    tiene_secreto = bool(request.headers.get("X-AuditAI-Secret"))
    log.info("request recibida · path=/reset-mes · tiene_secreto=%s", tiene_secreto)

    _validar_secreto()

    data = request.get_json(force=True, silent=True) or {}
    log.info("estructura payload RESET: %s", _estructura(data))
    page_id_clic = ""
    try:
        # Fuente principal: la fila donde se apretó el botón. Fallback: headers.
        ds_id, titulo, page_id_clic = _base_del_clic(data)
        db_header = (
            request.headers.get("X-Reset-DB")
            or data.get("database_id")
            or data.get("data_source_id")
            or ""
        ).strip()
        tipo_header = (request.headers.get("X-Reset-Tipo") or data.get("tipo") or "").strip().lower()

        if not ds_id:
            if not db_header:
                abort(400, "no pude identificar la planilla a resetear: el boton no mando la fila "
                           "donde se apreto (page_id) y tampoco llego el header X-Reset-DB. "
                           "Revisar la accion 'Send webhook' del boton: tiene que incluir la fila.")
            log.info("reset-mes: sin fila en el payload, uso el header X-Reset-DB")
            ds_id = nc.get_data_source_id(db_header)
            try:
                titulo = nc.get_database_title(db_header) or ""
            except Exception as exc:
                log.warning("reset-mes: no se pudo leer el titulo de la base del header · %s", exc)

        tipo = _tipo_desde_titulo(titulo) or tipo_header
        if tipo_header and tipo != tipo_header:
            # El título es el dato vivo; el header es config que puede quedar vieja.
            log.warning("reset-mes: el tipo del titulo (%r) manda sobre el header (%r)", tipo, tipo_header)
        if tipo not in RESET_POR_TIPO:
            abort(400, f"tipo desconocido: {tipo!r} (esperado: contable, rrhh o tickets)")
        campos_reset = RESET_POR_TIPO[tipo]
        if not campos_reset:
            abort(400, f"reset de tipo {tipo!r} aún no implementado (doc 29, Fase 3)")

        _validar_planilla_reseteable(ds_id, titulo, tipo)

        try:
            filas = nc.query_data_source(ds_id, {"page_size": 100})
        except Exception as exc:
            # El 18-ago este 500 decía solo "error leyendo la base" y no había
            # forma de saber cuál era "la base" sin ir a los logs de Render.
            alertas.avisar_excepcion_admin("RESET", ds_id or db_header, exc)
            return {"ok": False, "motivo": f"error leyendo la planilla {titulo or ds_id[:8]!r}; "
                                           "administrador notificado"}, 500

        # Safety switch OBLIGATORIO (doc 29): checkbox en la fila RESET_MES.
        fila_control = next(
            (f for f in filas if FILA_CONTROL_RESET in _titulo_fila(f.get("properties", {}) or {})),
            None,
        )
        if fila_control is None:
            abort(400, f"no se encontró la fila de control {FILA_CONTROL_RESET!r} en la base; el reset la requiere")
        props_control = fila_control.get("properties", {}) or {}
        clave_confirmar = _clave_prop(props_control, P_CONFIRMAR_RESET)
        if not clave_confirmar:
            abort(400, f"la base no tiene la columna checkbox {P_CONFIRMAR_RESET!r}; creala primero")
        confirmado = (props_control.get(clave_confirmar) or {}).get("checkbox", False)
        if not confirmado:
            abort(400, f"Marcá el checkbox {P_CONFIRMAR_RESET!r} en la fila {FILA_CONTROL_RESET} primero")

        # Columnas nuevas que nadie clasificó: se avisa, no se toca nada. Es el
        # radar de la familia de bugs "columna del mes que el reset no limpia".
        sin_clasificar = _columnas_sin_clasificar(
            (filas[0].get("properties") if filas else {}) or {},
            campos_reset, ESTATICAS_POR_TIPO.get(tipo, set()))
        if sin_clasificar:
            log.warning("reset-mes: columnas SIN CLASIFICAR en %r (ni se resetean ni están "
                        "declaradas estáticas): %s", titulo, sin_clasificar)

        with _resets_lock:
            if ds_id in _resets_activos:
                log.info("reset-mes duplicado ignorado (ya hay un reset en curso) · tipo=%s", tipo)
                return {"ok": True, "duplicado": True, "motivo": "ya hay un reset en curso para esta base"}, 200
            _resets_activos.add(ds_id)
    except HTTPException as exc:
        # Un reset rechazado era invisible: el asesor veía "error" en Notion y no
        # se enteraba nadie más (18-ago-2026: el reset falló y se descubrió al
        # día siguiente). Ahora el rechazo llega al admin con payload y origen.
        alertas.avisar_boton_rechazado("RESET", exc.code or 0, exc.description or "",
                                       page_id_clic, str(_estructura(data)), _contexto_request())
        raise

    _lanzar_reset(tipo, ds_id, campos_reset, filas)
    log.info("reset-mes lanzado en fondo · tipo=%s · planilla=%r · filas_totales=%d",
             tipo, titulo, len(filas))
    return {"ok": True, "en_proceso": True, "tipo": tipo, "planilla": titulo,
            "filas_totales": len(filas), "columnas_sin_clasificar": sin_clasificar}, 202


# --- Confirmación de entrega real via SendGrid Event Webhook (doc 30) ------
# El 202 de la API de SendGrid significa "aceptado para envío", NO "entregado".
# SendGrid postea los eventos reales (delivered / bounce / dropped) a
# /webhook/sendgrid; cada correo viaja con custom_args {page_id, flujo}, así el
# evento vuelve con la fila exacta de Notion a actualizar. El write-back del
# Status al 202 se mantiene (optimista); este webhook ENRIQUECE/CORRIGE:
# escribe la columna "Entrega Correo" (rich_text) y, si el correo rebotó,
# avisa al asesor. Escritura tolerante: sin la columna, solo queda el log.
P_ENTREGA = "Entrega Correo"
EVENTOS_ENTREGA = {"delivered", "bounce", "dropped"}

# --- Reintento automático ante el PRIMER bounce (evita falsas alarmas) ------
# Caso real (15-jul-2026, Mockenau): SendGrid reportó bounce por un hipo DNS
# del dominio del cliente ("unable to get mx info"); el reenvío manual 25 min
# después entregó OK. Ante el PRIMER bounce de una fila se agenda UN reintento
# automático (sin alarmar al asesor todavía); si el reintento también rebota,
# el segundo evento bounce sí dispara el aviso. "dropped" no se reintenta:
# significa que SendGrid suprimió el envío (lista de supresión) y reintentar
# da el mismo resultado. Estado en memoria del proceso: si el server se
# reinicia en la ventana, el reintento se pierde, pero la columna
# "Entrega Correo" queda diciendo que había un reintento agendado (visible).
REINTENTO_BOUNCE_S = 600   # 10 min: tiempo para que el DNS/MX del receptor se recupere
_reintentos_lock = threading.Lock()
_reintentos_hechos: set[str] = set()


def _handler_por_flujo(flujo: str):
    """Handler de reenvío según el custom_arg 'flujo' que viaja en cada correo
    ('f29', 'rrhh', 'tickets-<tipo>'). None si no se reconoce."""
    if flujo == "f29":
        return _procesar_page
    if flujo == "rrhh":
        return rrhh_handler.procesar
    if flujo == "crm":
        return crm_handler.procesar
    if flujo.startswith("tickets-"):
        tipo = flujo.split("-", 1)[1]
        if tipo in _TICKETS_TIPOS:
            return lambda page_id: tickets_handler.procesar(page_id, tipo)
    return None


def _agendar_reintento(page_id: str, flujo: str) -> bool:
    """Agenda UN reintento de envío en REINTENTO_BOUNCE_S segundos (thread
    daemon). True si quedó agendado; False si esa fila ya tuvo su reintento
    en este proceso o el flujo no se reconoce (en ambos casos el caller debe
    avisar al asesor como siempre)."""
    handler = _handler_por_flujo(flujo)
    if handler is None:
        return False
    with _reintentos_lock:
        if page_id in _reintentos_hechos:
            return False
        _reintentos_hechos.add(page_id)

    def _reintentar():
        try:
            resultado = handler(page_id)
            log.info("reintento post-bounce ejecutado · page_id=%s · flujo=%s · ok=%s",
                     page_id, flujo, resultado.get("ok"))
        except Exception as exc:
            log.error("reintento post-bounce fallo · page_id=%s · flujo=%s · %s", page_id, flujo, exc)

    t = threading.Timer(REINTENTO_BOUNCE_S, _reintentar)
    t.daemon = True
    t.start()
    log.info("reintento post-bounce agendado en %ds · page_id=%s · flujo=%s",
             REINTENTO_BOUNCE_S, page_id, flujo)
    return True


def _fecha_local(ts) -> str:
    """Formatea un timestamp epoch (segundos, UTC) a HORA DE CHILE
    'dd-mm-YYYY HH:MM hrs'. Usa America/Santiago (aplica el horario de verano
    automaticamente, sin offset fijo que se rompa medio ano). Si zoneinfo/tzdata
    no esta disponible, cae a UTC etiquetado (nunca lanza)."""
    if not isinstance(ts, (int, float)):
        return ""
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    if _TZ_CHILE is not None:
        try:
            return dt.astimezone(_TZ_CHILE).strftime("%d-%m-%Y %H:%M hrs")
        except Exception:
            pass
    return dt.strftime("%d-%m-%Y %H:%M UTC")


def _procesar_evento_sendgrid(ev: dict) -> bool:
    """Procesa UN evento del Event Webhook. True si actualizó la fila.
    Ignora eventos sin page_id (correos ajenos al backend, ej. avisos admin) y
    eventos de las copias BCC (asesor/Carlos): solo el destinatario CLIENTE
    (ev.email == Email de la fila) actualiza la fila. No loguea PII."""
    tipo = ev.get("event", "")
    page_id = str(ev.get("page_id", "") or "")
    if tipo not in EVENTOS_ENTREGA or not _es_uuid(page_id):
        return False

    page = nc.get_page(page_id)
    props = page.get("properties", {}) or {}

    # Filtro anti-BCC: un "delivered" de la copia del asesor no debe pisar un
    # "bounce" del cliente. Cuentan los eventos de CUALQUIER dirección de la
    # celda Email del cliente (puede tener varias: destinatario + CC, ej.
    # Hydroming); las copias BCC (asesor/Carlos) no están en esa celda.
    email_evento = str(ev.get("email", "") or "").strip().lower()
    clave_email = _clave_prop(props, "Email")
    emails_fila = {
        e.lower() for e in es.parse_destinatarios(nc.plain(props.get(clave_email, {})))
    } if clave_email else set()
    if emails_fila and email_evento and email_evento not in emails_fila:
        log.info("evento sendgrid de copia BCC ignorado · evento=%s · page_id=%s", tipo, page_id)
        return False

    fecha = _fecha_local(ev.get("timestamp"))   # hora de Chile (no UTC)

    # Primer bounce de la fila: reintento automático, sin alarma todavía.
    reintento_agendado = False
    if tipo == "bounce":
        reintento_agendado = _agendar_reintento(page_id, str(ev.get("flujo", "") or ""))

    if tipo == "delivered":
        texto = f"✅ Entregado · {fecha}" if fecha else "✅ Entregado"
    else:
        razon = str(ev.get("reason", "") or "")[:200]
        texto = f"❌ No entregado ({tipo})" + (f" · {fecha}" if fecha else "")
        if reintento_agendado:
            texto += f" · reintento automático en {REINTENTO_BOUNCE_S // 60} min"
        if razon:
            texto += f" · {razon}"

    clave_entrega = _clave_prop(props, P_ENTREGA)
    if clave_entrega:
        nc.update_props(page_id, {clave_entrega: {"rich_text": [{"text": {"content": texto}}]}})
    else:
        log.warning("columna %r no existe en la base de page_id=%s; entrega solo en log", P_ENTREGA, page_id)
    log.info("evento sendgrid procesado · evento=%s · page_id=%s · flujo=%s", tipo, page_id, ev.get("flujo", ""))

    # Confirmación POSITIVA al asesor: el correo SÍ llegó al cliente. Contraparte
    # del aviso de rebote de abajo, para que el asesor vea "llegó / no llegó" por
    # correo (no solo en la columna). El remitente (asesor) viaja en custom_args.
    # Se puede silenciar con AVISAR_ENTREGA_OK=0 si resulta ruidoso (1 por correo).
    if tipo == "delivered" and os.environ.get("AVISAR_ENTREGA_OK", "1") != "0":
        alertas.avisar_entrega_ok_asesor(
            str(ev.get("remitente", "") or ""),
            _titulo_fila(props),
            nc.plain(props.get("Month", {}) or {}),
            str(ev.get("flujo", "") or ""),
            fecha,
        )

    if tipo != "delivered" and not reintento_agendado:
        cliente = _titulo_fila(props)
        asesores = nc.people_names(props.get("Adviser Accounting", {}) or {})
        mes = nc.plain(props.get("Month", {}) or {})
        ya_reintentado = tipo == "bounce" and page_id in _reintentos_hechos
        alertas.avisar_fallo_asesor(
            asesores[0] if asesores else "", cliente, mes,
            f"El correo fue ACEPTADO por SendGrid pero NO llegó al cliente (evento: {tipo}).",
            flujo=str(ev.get("flujo", "") or "f29"),
            page_id=page_id,
            extra={
                "bounce_reason": str(ev.get("reason", "") or "")[:200],
                "ya_reintentado": ya_reintentado,
            },
        )
    return True


@app.post("/webhook/sendgrid")
def webhook_sendgrid():
    """Event Webhook de SendGrid (doc 30). Autenticación por token compartido
    en la query (?token=...): SendGrid no permite headers custom aquí. Procesa
    el batch completo y responde 200 aunque algún evento falle (si no, SendGrid
    reintenta el batch ENTERO y duplicaría los que sí se procesaron)."""
    token = os.environ.get("SENDGRID_WEBHOOK_TOKEN", "")
    if not token:
        log.error("SENDGRID_WEBHOOK_TOKEN ausente en el servidor · 503")
        abort(503, "backend sin SENDGRID_WEBHOOK_TOKEN configurado")
    if request.args.get("token", "") != token:
        log.warning("token invalido o ausente en /webhook/sendgrid · 401")
        abort(401)

    eventos = request.get_json(force=True, silent=True) or []
    if isinstance(eventos, dict):
        eventos = [eventos]
    actualizados = 0
    for ev in eventos:
        if not isinstance(ev, dict):
            continue
        try:
            if _procesar_evento_sendgrid(ev):
                actualizados += 1
        except Exception as exc:
            log.warning("evento sendgrid fallo · evento=%s · %s", ev.get("event", ""), exc)
    log.info("webhook sendgrid · eventos=%d · filas_actualizadas=%d", len(eventos), actualizados)
    return {"ok": True, "eventos": len(eventos), "filas_actualizadas": actualizados}, 200


@app.get("/health")
def health():
    # Solo booleanos/conteos, nunca valores (sin PII). Permite diagnosticar
    # desde afuera un deploy con env vars faltantes (H9) o el canal de alertas
    # del admin sin configurar (Fase 1.2). admin_alerts_configurados = cuantos
    # correos quedaron en ADMIN_ALERT_EMAIL (separados por coma).
    return {
        "ok": True,
        "service": "auditai-f29",
        "version": "2026-08-30-rrhh-redaccion-imposiciones",
        "webhook_secret_configurado": bool(os.environ.get("WEBHOOK_SECRET")),
        "admin_alerts_configurados": len(es.admin_emails()),
        "sendgrid_webhook_token_configurado": bool(os.environ.get("SENDGRID_WEBHOOK_TOKEN")),
    }, 200


def _run_test(page_id: str):
    """Modo prueba local: lee la fila y envía el correo sin webhook."""
    log.info("MODO TEST · page_id=%s", page_id)
    resultado = _procesar_page(page_id)
    print(f"\nResultado: {resultado}")


def _run_test_rut(rut: str):
    """Modo prueba por RUT: busca la fila en Contable Junio y envía el correo."""
    log.info("MODO TEST-RUT (RUT no se loguea)")
    page_id = nc.find_page_by_rut(rut)
    if not page_id:
        print("No se encontro fila con ese Rut en Contable Junio")
        sys.exit(1)
    log.info("fila encontrada · page_id=%s", page_id)
    resultado = _procesar_page(page_id)
    print(f"\nResultado: {resultado}")


if __name__ == "__main__":
    if "--test-rut" in sys.argv:
        idx = sys.argv.index("--test-rut")
        if idx + 1 < len(sys.argv):
            _run_test_rut(sys.argv[idx + 1])
        else:
            print("Uso: python app.py --test-rut <rut>")
        sys.exit(0)

    if "--test" in sys.argv:
        idx = sys.argv.index("--test")
        if idx + 1 < len(sys.argv):
            _run_test(sys.argv[idx + 1])
        else:
            print("Uso: python app.py --test <page_id>")
        sys.exit(0)

    port = int(os.environ.get("PORT", "8000"))
    # host="0.0.0.0" es obligatorio en la nube (Render/Railway/Cloud Run) para que el tráfico
    # del balanceador llegue al contenedor. En local no afecta (sigue accesible en localhost:8000).
    log.info("arrancando backend en 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
