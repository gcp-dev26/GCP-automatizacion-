"""Tests del handler CRM Comercial (cobranza de honorarios, remitente fijo
Finanzas). Cubre: remitente fijo, monto opcional, la cuenta bancaria siempre
presente, avisos a Carlos ante fallos, y write-back tolerante."""
from unittest.mock import patch
import notion_client as nc
import email_sender as es
import alertas
import handlers.crm as crm


def _page_crm(email="cliente@x.com", sw="Constructora Demo SpA", tarifa=150000,
              archivos=None):
    return {
        "properties": {
            crm.SW: {"type": "title", "title": [{"plain_text": sw}]},
            crm.EMAIL_CLIENTE: {"type": "email", "email": email},
            crm.TARIFA: {"type": "number", "number": tarifa},
            crm.ADJUNTOS: {"type": "files", "files": archivos or []},
        }
    }


class TestCRMCobranza:
    def test_sin_email_avisa_a_carlos(self):
        with patch.object(nc, "get_page", return_value=_page_crm(email="")), \
             patch.object(alertas, "avisar_fallo_asesor") as m:
            r = crm.procesar("p1")
        assert r["ok"] is False and "email" in r["motivo"]
        # el aviso didáctico va a Carlos (el técnico va al admin)
        assert m.call_args[0][0] == "Carlos Cereceda"
        assert m.call_args.kwargs["flujo"] == "crm"

    def test_sin_sw_avisa(self):
        with patch.object(nc, "get_page", return_value=_page_crm(sw="")), \
             patch.object(alertas, "avisar_fallo_asesor") as m:
            r = crm.procesar("p2")
        assert r["ok"] is False and "Sw" in r["motivo"]
        assert m.called

    def test_envia_desde_finanzas_con_monto_y_banco(self):
        capt = {}
        with patch.object(nc, "get_page", return_value=_page_crm()), \
             patch.object(es, "enviar", side_effect=lambda **kw: capt.update(kw) or "finanzas@inversoragcp.com"), \
             patch.object(nc, "update_props") as up:
            r = crm.procesar("p3")
        assert r["ok"] is True
        # remitente FIJO: siempre Finanzas, no un asesor de la fila
        assert capt["nombre_asesor"] == "Finanzas"
        assert capt["asunto"] == crm.ASUNTO
        assert capt["custom_args"] == {"page_id": "p3", "flujo": "crm"}
        ev = capt["extra_vars"]
        assert "$150.000" in ev["linea_monto"]        # tarjeta de monto
        assert "Santander" in ev["linea_banco"]        # cuenta bancaria presente
        # write-back de status + fecha (columnas presentes en la fila de test? no)
        # -> en esta fila no existen las columnas de write-back, así que update
        #    no se llama (tolerante). Se valida el camino "con columnas" abajo.
        assert not up.called

    def test_sin_tarifa_va_sin_tarjeta_pero_con_banco(self):
        capt = {}
        with patch.object(nc, "get_page", return_value=_page_crm(tarifa=None)), \
             patch.object(es, "enviar", side_effect=lambda **kw: capt.update(kw) or "finanzas@inversoragcp.com"), \
             patch.object(nc, "update_props"):
            r = crm.procesar("p4")
        assert r["ok"] is True
        ev = capt["extra_vars"]
        assert ev["linea_monto"] == ""                 # sin monto -> sin tarjeta
        assert "Santander" in ev["linea_banco"]        # pero la cuenta SIEMPRE va

    def test_write_back_tolerante_escribe_status_y_fecha(self):
        page = _page_crm()
        page["properties"][crm.STATUS_COL] = {"type": "status", "status": {"name": "Sin empezar"}}
        page["properties"][crm.FECHA_COL] = {"type": "date", "date": None}
        capt = {}
        with patch.object(nc, "get_page", return_value=page), \
             patch.object(es, "enviar", return_value="finanzas@inversoragcp.com"), \
             patch.object(nc, "update_props", side_effect=lambda pid, up: capt.update(up)):
            r = crm.procesar("p5")
        assert r["ok"] is True
        assert capt[crm.STATUS_COL]["status"]["name"] == crm.STATUS_ENVIADO
        assert "start" in capt[crm.FECHA_COL]["date"]

    def test_fallo_envio_avisa_con_motivo(self):
        with patch.object(nc, "get_page", return_value=_page_crm()), \
             patch.object(es, "enviar", side_effect=ValueError("SendGrid HTTP 403")), \
             patch.object(alertas, "avisar_fallo_asesor") as m:
            r = crm.procesar("p6")
        assert r["ok"] is False and "403" in r["motivo"]
        assert m.called and "403" in m.call_args[0][3]

    def test_columnas_de_gestion_de_cobros(self):
        # Nombres EXACTOS de la base '🔓 Gestión de Cobros' (verificados por API
        # el 05-oct-2026): la antigua 'Tarifa de cobro mensual' ya no existe.
        assert crm.TARIFA == "Honorario del mes"
        assert crm.ADJUNTOS == "Archivos y multimedia"

    def test_adjunta_los_archivos_de_la_fila(self):
        archivos = [
            {"name": "factura-octubre.pdf", "type": "file",
             "file": {"url": "https://s3.notion/factura.pdf"}},
            {"name": "detalle.pdf", "type": "external",
             "external": {"url": "https://ejemplo.cl/detalle.pdf"}},
        ]
        capt = {}
        with patch.object(nc, "get_page", return_value=_page_crm(archivos=archivos)), \
             patch.object(es, "enviar", side_effect=lambda **kw: capt.update(kw) or "finanzas@inversoragcp.com"), \
             patch.object(nc, "update_props"):
            r = crm.procesar("p7")
        assert r["ok"] is True
        assert capt["adjuntos"] == [
            {"name": "factura-octubre.pdf", "url": "https://s3.notion/factura.pdf"},
            {"name": "detalle.pdf", "url": "https://ejemplo.cl/detalle.pdf"},
        ]

    def test_sin_archivos_envia_igual_sin_adjuntos(self):
        page = _page_crm()
        del page["properties"][crm.ADJUNTOS]   # columna ausente: no debe romper
        capt = {}
        with patch.object(nc, "get_page", return_value=page), \
             patch.object(es, "enviar", side_effect=lambda **kw: capt.update(kw) or "finanzas@inversoragcp.com"), \
             patch.object(nc, "update_props"):
            r = crm.procesar("p8")
        assert r["ok"] is True
        assert capt["adjuntos"] == []
