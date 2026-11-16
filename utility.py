"""Loja Utility dentro do mesmo painel, totalmente separada da Ana Baby.

Como funciona: as telas da Utility ficam em /utility/... e usam as MESMAS telas e regras
da separação da Ana Baby (sincronizar, A separar, Escanear, Produto Pendente, Concluídos,
Dia finalizado, PDFs e reimpressão de etiquetas), mas com um banco próprio
(/var/data/utility.db) e a autorização da Utility na Shopee. Enquanto uma tela da Utility
está aberta, tudo o que ela lê e grava vai para o banco da Utility; a Ana Baby não é tocada.

A autorização usa o mesmo app da Shopee do painel: o LUCAS clica em "Conectar a Utility"
no Dashboard da Utility, entra na Shopee com o login da Utility e autoriza.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, flash, g, has_request_context, redirect, request, session
from flask import url_for as _url_for

import models
from shopee_client import ShopeeClient

UT_DB = os.path.join(os.path.dirname(models.DB_PATH), "utility.db")
BRT = timezone(timedelta(hours=-3))

bp = Blueprint("utility", __name__, url_prefix="/utility")
_ctx = {}

# Telas da separação que a Utility reaproveita: (endereço, tela original, métodos)
TELAS = [
    ("/", "dashboard", ["GET"]),
    ("/sync", "sync", ["POST"]),
    ("/dia-finalizado", "dia_finalizado", ["POST"]),
    ("/scan", "scan", ["GET", "POST"]),
    ("/order/<order_sn>/complete", "complete_order", ["POST"]),
    ("/order/<order_sn>/pending", "pending_order", ["POST"]),
    ("/order/<order_sn>/missing-product", "missing_product", ["POST"]),
    ("/order/<order_sn>/reopen", "reopen_order", ["POST"]),
    ("/a-separar", "open_tab", ["GET"]),
    ("/pendencias", "pending_tab", ["GET"]),
    ("/concluidos", "completed_tab", ["GET"]),
    ("/picking-list.pdf", "picking_list_pdf", ["GET"]),
    ("/produto-pendente.pdf", "missing_products_pdf", ["GET"]),
    ("/etiquetas/reimprimir", "reimpressao.reimprimir", ["POST"]),
    ("/etiquetas/reimpressao/<int:tid>", "reimpressao.acompanhar", ["GET"]),
    ("/etiquetas/reimpressao/<int:tid>/estado", "reimpressao.estado", ["GET"]),
    ("/etiquetas/reimpressao/<int:tid>/arquivo", "reimpressao.arquivo", ["GET"]),
]


def nome_local(endpoint):
    return endpoint.replace(".", "_")


def na_utility():
    return has_request_context() and g.get("loja") == "utility"


def url_for(endpoint, **values):
    """url_for que, numa tela da Utility, aponta para a versão Utility da tela (se existir)."""
    if na_utility() and not endpoint.startswith(("utility.", ".")) and endpoint != "static":
        alvo = "utility." + nome_local(endpoint)
        if alvo in current_app.view_functions:
            endpoint = alvo
    return _url_for(endpoint, **values)


@bp.before_request
def _entrar():
    g.loja = "utility"
    g._ut_tok = models._db_atual.set(UT_DB)


@bp.teardown_request
def _sair(exc):
    tok = g.pop("_ut_tok", None)
    if tok is not None:
        try:
            models._db_atual.reset(tok)
        except ValueError:
            models._db_atual.set(None)


def _delegar(endpoint):
    def view(**kw):
        return current_app.view_functions[endpoint](**kw)
    view.__name__ = "ut_" + nome_local(endpoint)
    return view


for _regra, _ep, _metodos in TELAS:
    bp.add_url_rule(_regra, nome_local(_ep), _delegar(_ep), methods=_metodos)


# ------------------------------------------------------------------ conexão com a Shopee

def _https(url):
    return "https://" + url.split("://", 1)[1] if url.startswith("http://") else url


@bp.route("/shopee/conectar")
def conectar():
    if session.get("employee_name") != "LUCAS":
        flash("Só o LUCAS pode conectar a loja na Shopee.")
        return redirect(url_for("dashboard"))
    pid, key = _ctx["partner"]
    if not (pid and key):
        flash("Credenciais da Shopee não configuradas no servidor.")
        return redirect(url_for("dashboard"))
    volta = _https(_url_for("utility.shopee_callback", _external=True))
    return redirect(ShopeeClient(pid, key).get_authorization_url(volta))


@bp.route("/shopee/callback")
def shopee_callback():
    code = request.args.get("code")
    shop_id = request.args.get("shop_id")
    if not code or not shop_id:
        flash("Retorno da Shopee incompleto (faltou 'code' ou 'shop_id').")
        return redirect(url_for("dashboard"))
    with models.usar_db(models.DB_PATH):
        ana = models.get_shopee_token()
    if ana and str(ana["shop_id"]) == str(shop_id):
        flash("Essa autorização é da loja Ana Baby. Para a Utility, entre na Shopee com o login da Utility e autorize de novo.")
        return redirect(url_for("dashboard"))
    pid, key = _ctx["partner"]
    result = ShopeeClient(pid, key).get_access_token(code, shop_id)
    if "access_token" not in result:
        flash(f"Falha ao autorizar: {result.get('message') or result}")
        return redirect(url_for("dashboard"))
    models.save_shopee_token(shop_id, result["access_token"], result["refresh_token"], result.get("expire_in", 14400))
    flash(f"Utility (loja {shop_id}) conectada! Já pode sincronizar os pedidos.")
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------ fechamento automático 18h

def _fechamento_loop():
    time.sleep(300)
    feito = None
    while True:
        try:
            agora = datetime.now(BRT)
            dia = agora.date().isoformat()
            if agora.weekday() < 6 and agora.hour == 18 and feito != dia:
                with models.usar_db(UT_DB):
                    client = _ctx["get_client"]()
                    if client is not None:
                        r = None
                        for _ in range(30):
                            r = _ctx["finalizar_dia"](client, pausa=0.5)
                            if r is not None:
                                break
                            time.sleep(20)
                feito = dia
        except Exception:
            pass
        time.sleep(240)


def init_app(app, app_globals, get_client, finalizar_dia, partner):
    """app_globals: globals() do app.py, para as telas originais redirecionarem para a
    versão Utility quando estiverem servindo a Utility."""
    import reimpressao

    _ctx.update(get_client=get_client, finalizar_dia=finalizar_dia, partner=partner)
    with models.usar_db(UT_DB):
        models.init_db()
        reimpressao.init_db()
    app_globals["url_for"] = url_for
    reimpressao.url_for = url_for
    app.jinja_env.globals["url_for"] = url_for
    app.register_blueprint(bp)
    threading.Thread(target=_fechamento_loop, daemon=True).start()
