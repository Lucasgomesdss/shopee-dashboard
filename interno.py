"""Canal interno do dashboard para o serviço próprio de Devoluções.

Só responde com o cabeçalho X-Interno igual à variável INTERNO_TOKEN (texto secreto
de pelo menos 20 caracteres, configurado nos dois serviços). Sem ela, as rotas nem
existem (404).

- /interno/shopee-token: o token atual da Shopee. Quem renova é sempre o dashboard,
  então os dois serviços nunca brigam pela renovação.
- /interno/devolucoes-export: cópia única, para a mudança de serviço, das devoluções
  já bipadas aqui e dos rastreios dos pedidos que estão no banco da separação.
  Somente leitura: não altera nada no dashboard.
"""

import hmac
import os

from flask import Blueprint, abort, jsonify, request

import models

bp = Blueprint("interno", __name__)
_get_shopee = [None]
TOKEN = os.environ.get("INTERNO_TOKEN", "")


def _autorizado():
    if len(TOKEN) < 20:
        return False
    return hmac.compare_digest(request.headers.get("X-Interno", ""), TOKEN)


@bp.route("/interno/shopee-token")
def shopee_token():
    if not _autorizado():
        abort(404)
    client = _get_shopee[0]() if _get_shopee[0] else None  # renova se estiver perto de vencer
    tok = models.get_shopee_token()
    if not client or not tok:
        abort(503)
    return jsonify({
        "shop_id": tok["shop_id"],
        "access_token": tok["access_token"],
        "expires_at": tok["expires_at"],
    })


def _tabela(conn, nome, sql):
    existe = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)
    ).fetchone()
    return [dict(r) for r in conn.execute(sql).fetchall()] if existe else []


@bp.route("/interno/devolucoes-export")
def devolucoes_export():
    if not _autorizado():
        abort(404)
    with models.get_conn() as conn:
        dados = {
            "devolucoes": _tabela(conn, "devolucoes", "SELECT * FROM devolucoes WHERE removido = 0"),
            "devolucao_itens": _tabela(
                conn, "devolucao_itens",
                "SELECT i.* FROM devolucao_itens i JOIN devolucoes d ON d.id = i.devolucao_id "
                "WHERE d.removido = 0",
            ),
            "shopee_devolucoes": _tabela(conn, "shopee_devolucoes", "SELECT * FROM shopee_devolucoes"),
            "rastreios_historico": _tabela(conn, "rastreios_historico", "SELECT * FROM rastreios_historico"),
            "pedidos": [dict(r) for r in conn.execute(
                "SELECT tracking_number AS rastreio, order_sn FROM orders "
                "WHERE tracking_number IS NOT NULL AND tracking_number <> ''"
            ).fetchall()],
            "meta": _tabela(conn, "devolucoes_meta", "SELECT * FROM devolucoes_meta"),
        }
    return jsonify(dados)


def init_app(app, get_shopee_client):
    _get_shopee[0] = get_shopee_client
    app.register_blueprint(bp)
