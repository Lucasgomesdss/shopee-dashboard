"""Histórico de rastreios para as Devoluções (pedidos antigos, de junho para cá).

Guarda só rastreio -> número do pedido, numa tabela própria (rastreios_historico).
NÃO mexe na tabela de pedidos da separação: nada aparece em A separar, nos
contadores ou no Desempenho. Usado apenas pela tela Devoluções para achar o pedido
quando o pacote volta com a etiqueta de ida de um pedido antigo.

A importação roda em segundo plano, devagar (folga entre chamadas à Shopee), pode
ser interrompida pelo reinício do servidor e continua de onde parou.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import models

BRT = timezone(timedelta(hours=-3))
INICIO_PADRAO = datetime(2026, 6, 1, tzinfo=BRT)
JANELA = 15 * 86400            # limite da Shopee por consulta
PAUSA = 0.4                    # segundos entre chamadas, para não pesar no servidor
STATUS_ALTERNATIVOS = [
    "READY_TO_SHIP", "PROCESSED", "SHIPPED", "TO_CONFIRM_RECEIVE",
    "COMPLETED", "TO_RETURN", "IN_CANCEL", "CANCELLED",
]

_get_shopee = [None]
_sync_devolucoes = [None]   # função da tela Devoluções que baixa a lista de devoluções da Shopee
_lock = threading.Lock()


# ------------------------------------------------------------------------- banco

def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rastreios_historico (
                rastreio TEXT PRIMARY KEY,
                order_sn TEXT NOT NULL,
                criado_em INTEGER
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS devolucoes_meta (chave TEXT PRIMARY KEY, valor TEXT)"
        )


def _meta(chave, valor=None):
    with models.get_conn() as conn:
        if valor is None:
            row = conn.execute("SELECT valor FROM devolucoes_meta WHERE chave = ?", (chave,)).fetchone()
            return row["valor"] if row else None
        conn.execute(
            "INSERT INTO devolucoes_meta (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (chave, str(valor)),
        )


def buscar(rastreio):
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT order_sn FROM rastreios_historico WHERE rastreio = ?", (rastreio,)
        ).fetchone()
        return row["order_sn"] if row else None


def situacao():
    """Resumo para a tela: quantos rastreios salvos, até onde chegou, se está rodando."""
    with models.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM rastreios_historico").fetchone()[0]
    ate = _meta("hist_ate")
    erro = _meta("hist_erro")
    return {
        "total": total,
        "rodando": _lock.locked(),
        "ate": datetime.fromtimestamp(int(float(ate)), BRT).strftime("%d/%m/%Y") if ate else None,
        "concluido": _meta("hist_concluido") == "1",
        "erro": erro or None,
        "inicio": INICIO_PADRAO.strftime("%d/%m/%Y"),
    }


# ------------------------------------------------------------------- importação

def _normalizar(r):
    r = (r or "").strip().upper().replace(" ", "")
    if "SPXLM" in r:
        r = r.split("SPXLM")[0]
    return r


def _listar_pedidos(client, ini, fim, status, cursor):
    params = {
        "time_range_field": "create_time",
        "time_from": ini,
        "time_to": fim,
        "page_size": 100,
        "cursor": cursor,
    }
    if status:
        params["order_status"] = status
    return client._shop_request("GET", "/api/v2/order/get_order_list", params=params)


def _salvar_detalhes(client, sns):
    for i in range(0, len(sns), 50):
        lote = sns[i:i + 50]
        resp = client._shop_request(
            "GET", "/api/v2/order/get_order_detail",
            params={"order_sn_list": ",".join(lote), "response_optional_fields": "package_list"},
        )
        linhas = []
        for od in (resp.get("response") or {}).get("order_list", []):
            for pkg in od.get("package_list") or []:
                r = _normalizar(pkg.get("tracking_number"))
                if r:
                    linhas.append((r, od["order_sn"], int(od.get("create_time") or 0)))
        if linhas:
            with models.get_conn() as conn:
                conn.executemany(
                    "INSERT INTO rastreios_historico (rastreio, order_sn, criado_em) VALUES (?,?,?) "
                    "ON CONFLICT(rastreio) DO UPDATE SET order_sn = excluded.order_sn",
                    linhas,
                )
        time.sleep(PAUSA)


def _janela(client, ini, fim):
    """Importa uma janela de até 15 dias. Tenta sem filtro de status; se a Shopee exigir,
    passa por cada status."""
    statuses = [None]
    primeira = _listar_pedidos(client, ini, fim, None, "")
    if primeira.get("error"):
        statuses = STATUS_ALTERNATIVOS
    for st in statuses:
        cursor = ""
        for _ in range(200):  # trava: até 20 mil pedidos por janela e status
            resp = primeira if (st is None and cursor == "") else _listar_pedidos(client, ini, fim, st, cursor)
            if resp.get("error"):
                if st is None:
                    raise RuntimeError(resp.get("message") or resp.get("error"))
                break  # status não aceito nesta loja: segue para o próximo
            corpo = resp.get("response") or {}
            sns = [o["order_sn"] for o in corpo.get("order_list", [])]
            if sns:
                _salvar_detalhes(client, sns)
            time.sleep(PAUSA)
            prox = corpo.get("next_cursor") or ""
            if not corpo.get("more") or not prox or prox == cursor:
                break
            cursor = prox


def _rodar():
    with _lock:
        client = _get_shopee[0]() if _get_shopee[0] else None
        if client is None:
            _meta("hist_erro", "Loja Shopee não conectada.")
            return
        _meta("hist_erro", "")
        agora = int(time.time())
        ini = int(float(_meta("hist_ate") or INICIO_PADRAO.timestamp()))
        try:
            while ini < agora:
                fim = min(ini + JANELA, agora)
                _janela(client, ini, fim)
                ini = fim
                _meta("hist_ate", ini)   # progresso salvo: reinício continua daqui
            # Também a lista de devoluções da Shopee desde junho (rastreios de retorno).
            if _meta("hist_devolucoes_ok") != "1" and _sync_devolucoes[0]:
                _sync_devolucoes[0](int(INICIO_PADRAO.timestamp()))
                _meta("hist_devolucoes_ok", "1")
            _meta("hist_concluido", "1")
        except Exception as e:
            _meta("hist_erro", f"Parou em {datetime.fromtimestamp(ini, BRT):%d/%m}: {e}")


def iniciar():
    if _lock.locked():
        return False
    threading.Thread(target=_rodar, daemon=True).start()
    return True


def atualizar_recentes():
    """Depois de concluída, cada nova chamada só busca o que entrou desde a última vez."""
    if _meta("hist_concluido") == "1":
        iniciar()


def init_app(get_shopee_client, sync_devolucoes_desde=None):
    _get_shopee[0] = get_shopee_client
    _sync_devolucoes[0] = sync_devolucoes_desde
    init_db()
    # Se o servidor reiniciou no meio da importação, continua sozinha depois de 60 s.
    try:
        if _meta("hist_ate") and _meta("hist_concluido") != "1":
            threading.Timer(60, iniciar).start()
    except Exception:
        pass
