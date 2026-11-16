"""Cuenta BCI de GCP Patrimonio para clientes '27 Bis' y 'Cumplimiento'
(pedido del 16-nov-2026).

Los clientes cuya columna 'asignacion ' es '27 Bis' o 'Cumplimiento' transfieren
a la cuenta BCI de Gcp Patrimonio Estratégico Limitada, no a la Santander de
Inversora GCP. En su correo F29:
- con impuesto a pagar aparece SIEMPRE un recuadro con la cuenta BCI y el correo
  de finanzas para el comprobante (aunque no tengan honorarios);
- el recuadro de honorarios (si lo hay) usa la misma cuenta BCI, nunca la Santander.
El resto de los clientes no cambia.
"""
from unittest.mock import patch
import app as A
import notion_client as nc
import email_sender as es

SANTANDER = "8577678"
BCI = "70839202"


def _render(monto="150000", honorarios="", cuenta=""):
    return es.render("CLIENTE DEMO SPA", "Octubre 2026", monto, "Asesor", "c@gcp.cl",
                     "logo", honorarios=honorarios, cuenta=cuenta)


class TestRenderCuentaPatrimonio:
    def test_datos_de_la_cuenta_bci(self):
        assert "Gcp Patrimonio Estratégico Limitada" in es.BANCO_PATRIMONIO_TXT
        assert "78.456.673-0" in es.BANCO_PATRIMONIO_TXT
        assert "Bci" in es.BANCO_PATRIMONIO_TXT and BCI in es.BANCO_PATRIMONIO_TXT

    def test_con_impuesto_a_pagar_muestra_bci_y_comprobante_sin_honorarios(self):
        html, txt = _render(cuenta="patrimonio")
        for doc in (html, txt):
            assert BCI in doc
            assert "finanzas@inversoragcp.com" in doc
            assert SANTANDER not in doc

    def test_sin_impuesto_ni_honorarios_no_muestra_cuenta(self):
        html, txt = _render(monto="0", cuenta="patrimonio")
        assert BCI not in html and BCI not in txt

    def test_honorarios_usan_bci_nunca_santander(self):
        html, txt = _render(monto="0", honorarios="85000", cuenta="patrimonio")
        for doc in (html, txt):
            assert BCI in doc
            assert SANTANDER not in doc

    def test_cliente_normal_sigue_con_santander(self):
        html, txt = _render(honorarios="85000")
        for doc in (html, txt):
            assert SANTANDER in doc
            assert BCI not in doc


PID = "12345678-1234-1234-1234-123456789012"


def _page(asignacion):
    props = {
        A.P_NOMBRE: {"type": "title", "title": [{"plain_text": "CLIENTE DEMO SPA"}]},
        A.P_EMAIL: {"type": "email", "email": "cliente@example.com"},
        A.P_MONTO: {"type": "number", "number": 12345},
        A.P_ADVISER: {"type": "people", "people": [{"name": "Sebastián Robles"}]},
    }
    if asignacion is not None:
        # En Notion la columna se llama 'asignacion ' (con espacio al final).
        props["asignacion "] = {"type": "select",
                                "select": {"name": asignacion} if asignacion else None}
    return {"id": PID,
            "parent": {"database_id": "db", "data_source_id": nc.DS_CONTABLE_JUNIO},
            "last_edited_time": "2026-11-16T12:00:00.000Z",
            "properties": props}


def _cuenta_enviada(asignacion) -> str:
    with patch.object(nc, "get_page", return_value=_page(asignacion)), \
         patch.object(nc, "derivar_month_desde_base", return_value="Octubre 2026"), \
         patch.object(es, "enviar", return_value="asesor@gcp.com") as m_enviar, \
         patch.object(nc, "update_props"), \
         patch.object(A.alertas, "avisar_fallo_asesor"):
        assert A._procesar_page(PID)["ok"] is True
    return m_enviar.call_args.kwargs["cuenta"]


class TestHandlerEligeCuenta:
    def test_27_bis_usa_patrimonio(self):
        assert _cuenta_enviada("27 Bis") == "patrimonio"

    def test_cumplimiento_usa_patrimonio(self):
        assert _cuenta_enviada("Cumplimiento") == "patrimonio"

    def test_otra_categoria_usa_cuenta_normal(self):
        assert _cuenta_enviada("Grupo") == ""

    def test_sin_categoria_o_sin_columna_usa_cuenta_normal(self):
        assert _cuenta_enviada("") == ""
        assert _cuenta_enviada(None) == ""
