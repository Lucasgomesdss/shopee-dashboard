"""Cópia única, para o serviço próprio de Devoluções, do que já existia no dashboard.

Roda sozinha uma vez, em segundo plano, quando o serviço novo sobe com o banco vazio.
Traz: devoluções bipadas (com itens e notas já criadas), o cache de devoluções da
Shopee, o modelo da nota e os rastreios dos pedidos do banco da separação.
O token do Bling NÃO vem: o serviço novo é autorizado de novo (endereço novo).
"""

import threading

import requests

import models

_estado = {"rodando": False, "msg": ""}

COLS_DEV = [
    "id", "rastreio", "order_sn", "origem", "bling_pedido_id", "bling_nfe_id", "nfe_numero",
    "nfe_serie", "nfe_chave", "nfe_situacao", "cliente_nome", "cliente_doc", "total", "status",
    "motivo", "nfe_devolucao_id", "nfe_devolucao_numero", "nfe_devolucao_situacao",
    "bipado_por", "removido", "created_at", "updated_at",
]
COLS_ITEM = ["id", "devolucao_id", "codigo", "descricao", "unidade", "qtd_original",
             "qtd_devolver", "valor", "dados_json"]
META_COPIAR = {"modelo_devolucao", "shopee_ret_sync_ate", "shopee_ret_sync_em"}


def _meta(conn, chave, valor=None):
    if valor is None:
        row = conn.execute("SELECT valor FROM devolucoes_meta WHERE chave = ?", (chave,)).fetchone()
        return row["valor"] if row else None
    conn.execute(
        "INSERT INTO devolucoes_meta (chave, valor) VALUES (?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor", (chave, str(valor)))


def copiar(dashboard_url, token):
    r = requests.get(f"{dashboard_url}/interno/devolucoes-export",
                     headers={"X-Interno": token}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"dashboard respondeu HTTP {r.status_code}")
    d = r.json()
    with models.get_conn() as conn:
        if _meta(conn, "migrado_do_dashboard") == "1":
            return "já copiado antes"
        for dev in d.get("devolucoes", []):
            # Linha que estava "gerando" no dashboard volta como pronta/gerada para conferir.
            if dev.get("status") in ("na_fila", "gerando"):
                dev["status"] = "gerado" if dev.get("nfe_devolucao_id") else "pronto"
            vals = [dev.get(c) for c in COLS_DEV]
            conn.execute(
                f"INSERT OR IGNORE INTO devolucoes ({', '.join(COLS_DEV)}) VALUES ({', '.join('?' for _ in COLS_DEV)})",
                vals,
            )
        for it in d.get("devolucao_itens", []):
            conn.execute(
                f"INSERT OR IGNORE INTO devolucao_itens ({', '.join(COLS_ITEM)}) VALUES ({', '.join('?' for _ in COLS_ITEM)})",
                [it.get(c) for c in COLS_ITEM],
            )
        conn.executemany(
            "INSERT OR IGNORE INTO shopee_devolucoes (rastreio, order_sn, return_sn) VALUES (?,?,?)",
            [(x["rastreio"], x["order_sn"], x.get("return_sn")) for x in d.get("shopee_devolucoes", [])],
        )
        linhas = [(x["rastreio"], x["order_sn"], x.get("criado_em") or 0) for x in d.get("rastreios_historico", [])]
        linhas += [(str(x["rastreio"]).strip().upper(), x["order_sn"], 0) for x in d.get("pedidos", [])]
        conn.executemany(
            "INSERT OR IGNORE INTO rastreios_historico (rastreio, order_sn, criado_em) VALUES (?,?,?)",
            linhas,
        )
        for m in d.get("meta", []):
            if m["chave"] in META_COPIAR:
                _meta(conn, m["chave"], m["valor"])
        _meta(conn, "migrado_do_dashboard", "1")
    return (f"{len(d.get('devolucoes', []))} devoluções e "
            f"{len(linhas)} rastreios copiados do dashboard")


def _rodar(dashboard_url, token):
    _estado["rodando"] = True
    try:
        _estado["msg"] = copiar(dashboard_url, token)
    except Exception as e:
        _estado["msg"] = f"Cópia do dashboard falhou: {e}"
    finally:
        _estado["rodando"] = False


def init_app(app, dashboard_url, token):
    if not (dashboard_url and token):
        return
    with models.get_conn() as conn:
        ja = _meta(conn, "migrado_do_dashboard")
    if ja != "1":
        threading.Thread(target=_rodar, args=(dashboard_url, token), daemon=True).start()
