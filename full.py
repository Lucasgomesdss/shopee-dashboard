"""Full: monitoramento dos pedidos e do estoque do Shopee Full (Fulfilled by Shopee).

Tela separada, só de leitura na Shopee e no Bling -- não mexe na separação, na
bipagem nem em nada da operação. Tabelas próprias (full_*).

- Pedidos do Full: a cada hora o sistema lista os pedidos criados nas últimas ~26h,
  consulta o detalhe só dos que ainda não conhece e guarda os que são Full
  (fulfillment_flag = fulfilled_by_shopee), com os itens. Os pedidos Full dos últimos
  3 dias são reconferidos para tirar os cancelados. Na primeira vez, busca os
  últimos 60 dias.
- Estoque: uma vez por dia de madrugada (ou no botão), lê o estoque de todos os
  anúncios: o que está nos armazéns da Shopee (shopee_stock = Full) e o que está no
  galpão (seller_stock).
- Custo: preço de custo de cada SKU lido do Bling (conta Ana), junto com o estoque.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, redirect, request, session, url_for

import models
from layout_v2 import ICONES, render_page

bp = Blueprint("full", __name__)

BRT = timezone(timedelta(hours=-3))
LIBERADOS = {"JULIANA", "LUCAS"}
PAUSA = 0.25                         # segundos entre consultas à Shopee
HISTORICO_DIAS = 60                  # histórico mantido (para comparar períodos)
ABERTOS = ("READY_TO_SHIP", "PROCESSED", "RETRY_SHIP")   # Full ainda não despachou
IPI_PADRAO = 6.5                     # % de IPI somado ao preço de custo do Bling
RECONFERIR_DIAS = 3                  # pedidos Full reconferidos (cancelamentos)
STATUS_FORA = ("UNPAID", "CANCELLED", "IN_CANCEL")
HORA_ESTOQUE = 5                     # atualização diária do estoque, às 5h

_get_shopee = [None]
_lock_pedidos = threading.Lock()
_lock_estoque = threading.Lock()


# ------------------------------------------------------------------ banco

def init_db():
    with models.get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS full_vistos (
                order_sn TEXT PRIMARY KEY,
                fbs INTEGER NOT NULL,
                create_time INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS full_pedidos (
                order_sn TEXT PRIMARY KEY,
                dia TEXT NOT NULL,
                create_time INTEGER NOT NULL,
                status TEXT NOT NULL,
                atualizado TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_full_pedidos_dia ON full_pedidos(dia);
            CREATE TABLE IF NOT EXISTS full_itens (
                order_sn TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                model_id INTEGER NOT NULL,
                sku TEXT,
                nome TEXT,
                variacao TEXT,
                qtd INTEGER NOT NULL,
                PRIMARY KEY (order_sn, item_id, model_id)
            );
            CREATE TABLE IF NOT EXISTS full_estoque (
                item_id INTEGER NOT NULL,
                model_id INTEGER NOT NULL,
                sku TEXT,
                nome TEXT,
                variacao TEXT,
                full_qtd INTEGER NOT NULL,
                full_locais TEXT,
                galpao_qtd INTEGER NOT NULL,
                PRIMARY KEY (item_id, model_id)
            );
            CREATE TABLE IF NOT EXISTS full_custos (
                sku TEXT PRIMARY KEY,
                custo REAL,
                nome TEXT
            );
            CREATE TABLE IF NOT EXISTS full_meta (
                chave TEXT PRIMARY KEY,
                valor TEXT
            );
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(full_custos)")}
        if "saldo" not in cols:
            conn.execute("ALTER TABLE full_custos ADD COLUMN saldo REAL")
        if "formato" not in cols:
            conn.execute("ALTER TABLE full_custos ADD COLUMN formato TEXT")
        for tabela in ("full_itens", "full_estoque"):
            if "imagem" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN imagem TEXT")


def _meta(chave, valor=None):
    with models.get_conn() as conn:
        if valor is None:
            r = conn.execute("SELECT valor FROM full_meta WHERE chave = ?", (chave,)).fetchone()
            return r["valor"] if r else None
        conn.execute(
            "INSERT INTO full_meta (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (chave, str(valor)),
        )


def _agora():
    return datetime.now(BRT)


def _dia(ts):
    return datetime.fromtimestamp(int(ts), BRT).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ Shopee

def _get(client, caminho, **params):
    time.sleep(PAUSA)
    r = client._shop_request("GET", caminho, params=params)
    if r.get("error"):
        raise RuntimeError(f"{caminho}: {r.get('message') or r.get('error')}")
    return r.get("response") or {}


def _listar_pedidos(client, desde, ate):
    """order_sn criados entre desde e ate (em janelas de até 15 dias)."""
    sns = []
    fim = ate
    while fim > desde:
        ini = max(fim - 15 * 86400 + 60, desde)
        cursor = ""
        for _ in range(500):
            resp = _get(
                client, "/api/v2/order/get_order_list",
                time_range_field="create_time", time_from=ini, time_to=fim,
                page_size=100, cursor=cursor,
            )
            sns += [(o["order_sn"]) for o in resp.get("order_list", [])]
            nxt = resp.get("next_cursor") or ""
            if not resp.get("more") or not nxt or nxt == cursor:
                break
            cursor = nxt
        fim = ini
    return list(dict.fromkeys(sns))


def coletar_pedidos(client=None):
    """Busca os pedidos novos e guarda os do Full. Devolve um resumo em texto."""
    if not _lock_pedidos.acquire(False):
        return None
    try:
        client = client or _get_shopee[0]()
        if client is None:
            raise RuntimeError("Loja não conectada à Shopee.")
        agora = int(time.time())
        inicio_hist = agora - HISTORICO_DIAS * 86400
        primeira = (_meta("pedidos_inicio") or "9999") > _dia(inicio_hist + 86400)
        desde = inicio_hist if primeira else agora - 26 * 3600
        sns = _listar_pedidos(client, desde, agora)

        limite_reconf = agora - RECONFERIR_DIAS * 86400
        with models.get_conn() as conn:
            vistos = {}
            for i in range(0, len(sns), 500):
                parte = sns[i:i + 500]
                q = ",".join("?" * len(parte))
                for r in conn.execute(f"SELECT order_sn, fbs, create_time FROM full_vistos WHERE order_sn IN ({q})", parte):
                    vistos[r["order_sn"]] = r
            # Recentes (cancelamentos) e os que ainda estão em aberto (para saber quando
            # o Full despachou).
            abertos = ",".join("?" * len(ABERTOS))
            reconf = [r["order_sn"] for r in conn.execute(
                f"""SELECT v.order_sn FROM full_vistos v LEFT JOIN full_pedidos p ON p.order_sn = v.order_sn
                    WHERE v.fbs = 1 AND (v.create_time >= ? OR (p.status IN ({abertos}) AND v.create_time >= ?))""",
                (limite_reconf, *ABERTOS, agora - 20 * 86400))]

        # Consulta: os que nunca vimos + os do Full recentes (status pode ter mudado).
        consultar = [sn for sn in sns if sn not in vistos]
        consultar += [sn for sn in reconf if sn not in consultar]

        novos_full = 0
        agora_txt = _agora().isoformat()
        for i in range(0, len(consultar), 50):
            lote = consultar[i:i + 50]
            det = _get(
                client, "/api/v2/order/get_order_detail",
                order_sn_list=",".join(lote),
                response_optional_fields="item_list,fulfillment_flag",
            ).get("order_list", [])
            with models.get_conn() as conn:
                for od in det:
                    sn = od["order_sn"]
                    status = od.get("order_status") or ""
                    ct = int(od.get("create_time") or agora)
                    fbs = 1 if od.get("fulfillment_flag") == "fulfilled_by_shopee" else 0
                    # Pedido ainda não pago pode não ter a marcação definitiva: não grava
                    # como visto, para olhar de novo na próxima rodada.
                    if status != "UNPAID" or fbs:
                        conn.execute(
                            "INSERT INTO full_vistos (order_sn, fbs, create_time) VALUES (?, ?, ?) "
                            "ON CONFLICT(order_sn) DO UPDATE SET fbs = excluded.fbs",
                            (sn, fbs, ct),
                        )
                    if not fbs:
                        continue
                    existia = conn.execute("SELECT 1 FROM full_pedidos WHERE order_sn = ?", (sn,)).fetchone()
                    conn.execute(
                        "INSERT INTO full_pedidos (order_sn, dia, create_time, status, atualizado) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(order_sn) DO UPDATE SET status = excluded.status, atualizado = excluded.atualizado",
                        (sn, _dia(ct), ct, status, agora_txt),
                    )
                    if not existia:
                        novos_full += 1
                    conn.execute("DELETE FROM full_itens WHERE order_sn = ?", (sn,))
                    for it in od.get("item_list", []):
                        conn.execute(
                            "INSERT OR REPLACE INTO full_itens (order_sn, item_id, model_id, sku, nome, variacao, qtd, imagem) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                sn, int(it.get("item_id") or 0), int(it.get("model_id") or 0),
                                (it.get("model_sku") or it.get("item_sku") or "").strip(),
                                it.get("item_name") or "", it.get("model_name") or "",
                                int(it.get("model_quantity_purchased") or 1),
                                (it.get("image_info") or {}).get("image_url") or "",
                            ),
                        )
        if primeira:
            _meta("pedidos_inicio", _dia(desde))
        _meta("pedidos_atualizado", agora_txt)
        _meta("pedidos_erro", "")
        return f"{len(consultar)} pedido(s) consultado(s), {novos_full} novo(s) do Full."
    except Exception as e:
        _meta("pedidos_erro", f"{_agora().strftime('%d/%m %H:%M')}: {str(e)[:200]}")
        raise
    finally:
        _lock_pedidos.release()


def _estoques(stock_info):
    si = stock_info or {}
    full_locais = [(s.get("location_id") or "", int(s.get("stock") or 0)) for s in (si.get("shopee_stock") or [])]
    full = sum(q for _, q in full_locais)
    galpao = sum(int(s.get("stock") or 0) for s in (si.get("seller_stock") or []))
    locais = ", ".join(f"{loc}: {q}" for loc, q in full_locais if q)
    return full, locais, galpao


def atualizar_estoque(client=None):
    """Lê o estoque (Full e galpão) de todos os anúncios e o custo no Bling."""
    if not _lock_estoque.acquire(False):
        return None
    try:
        client = client or _get_shopee[0]()
        if client is None:
            raise RuntimeError("Loja não conectada à Shopee.")
        ids, offset = [], 0
        for _ in range(200):
            resp = _get(client, "/api/v2/product/get_item_list", offset=offset, page_size=100,
                        item_status=["NORMAL", "UNLIST"])
            ids += [x["item_id"] for x in resp.get("item", [])]
            if not resp.get("has_next_page"):
                break
            offset = resp.get("next_offset")

        linhas = []
        com_variacao = []
        for i in range(0, len(ids), 50):
            base = _get(client, "/api/v2/product/get_item_base_info",
                        item_id_list=",".join(str(x) for x in ids[i:i + 50])).get("item_list", [])
            for it in base:
                img = ((it.get("image") or {}).get("image_url_list") or [""])[0]
                if it.get("has_model"):
                    com_variacao.append((it["item_id"], it.get("item_name") or "", img))
                    continue
                full, locais, galpao = _estoques(it.get("stock_info_v2"))
                linhas.append((it["item_id"], 0, (it.get("item_sku") or "").strip(), it.get("item_name") or "", "", full, locais, galpao, img))

        for item_id, nome, img in com_variacao:
            resp = _get(client, "/api/v2/product/get_model_list", item_id=item_id)
            opcoes = ((resp.get("tier_variation") or [{}])[0].get("option_list") or [])
            for m in resp.get("model", []):
                full, locais, galpao = _estoques(m.get("stock_info_v2"))
                img_var = img
                try:
                    img_var = ((opcoes[(m.get("tier_index") or [0])[0]].get("image") or {}).get("image_url")) or img
                except (IndexError, AttributeError):
                    pass
                linhas.append((item_id, int(m["model_id"]), (m.get("model_sku") or "").strip(), nome,
                               m.get("model_name") or "", full, locais, galpao, img_var))

        with models.get_conn() as conn:
            conn.execute("DELETE FROM full_estoque")
            conn.executemany(
                "INSERT OR REPLACE INTO full_estoque (item_id, model_id, sku, nome, variacao, full_qtd, full_locais, galpao_qtd, imagem) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                linhas,
            )
        _meta("estoque_atualizado", _agora().isoformat())
        _meta("estoque_dia", _agora().strftime("%Y-%m-%d"))
        _meta("estoque_erro", "")
        msg = f"Estoque de {len(linhas)} SKU(s) atualizado."
        try:
            msg += " " + atualizar_custos()
        except Exception as e:
            _meta("custo_erro", f"{_agora().strftime('%d/%m %H:%M')}: {str(e)[:200]}")
            msg += f" Custo não atualizado: {e}"
        return msg
    except Exception as e:
        _meta("estoque_erro", f"{_agora().strftime('%d/%m %H:%M')}: {str(e)[:200]}")
        raise
    finally:
        _lock_estoque.release()


# ------------------------------------------------------------------ Bling (custo)

def atualizar_custos():
    """Preço de custo de cada produto no Bling (conta Ana), pelo código (= SKU)."""
    import bling_client

    if not bling_client.conectado():
        raise RuntimeError("Bling não conectado.")
    custos = {}
    sem_custo_na_lista = []
    for tipo in (None, "V"):          # produtos simples/pais e também as variações
        for pagina in range(1, 400):
            params = {"pagina": pagina, "limite": 100}
            if tipo:
                params["tipo"] = tipo
            dados = bling_client._get("produtos", params).get("data") or []
            for p in dados:
                cod = (p.get("codigo") or "").strip()
                if not cod:
                    continue
                saldo = float((p.get("estoque") or {}).get("saldoVirtualTotal") or 0)
                formato = p.get("formato") or ""
                if "precoCusto" in p:
                    custos[cod] = (float(p.get("precoCusto") or 0), p.get("nome") or "", saldo, formato)
                else:
                    sem_custo_na_lista.append((cod, p.get("id"), p.get("nome") or "", saldo, formato))
            if len(dados) < 100:
                break

    # Se a lista não trouxer o custo, busca o detalhe só dos SKUs que têm estoque.
    if sem_custo_na_lista:
        with models.get_conn() as conn:
            com_estoque = {r["sku"] for r in conn.execute(
                "SELECT sku FROM full_estoque WHERE full_qtd > 0 OR galpao_qtd > 0")}
        for cod, pid, nome, saldo, formato in sem_custo_na_lista:
            if cod in custos or cod not in com_estoque or not pid:
                continue
            d = bling_client._get(f"produtos/{pid}").get("data") or {}
            custo = d.get("precoCusto")
            if custo is None:
                custo = (d.get("fornecedor") or {}).get("precoCusto")
            custos[cod] = (float(custo or 0), nome, saldo, formato)

    with models.get_conn() as conn:
        conn.execute("DELETE FROM full_custos")
        conn.executemany(
            "INSERT OR REPLACE INTO full_custos (sku, custo, nome, saldo, formato) VALUES (?, ?, ?, ?, ?)",
            [(k, v[0], v[1], v[2], v[3]) for k, v in custos.items()],
        )
    _meta("custo_atualizado", _agora().isoformat())
    _meta("custo_erro", "")
    return f"Custo de {len(custos)} produto(s) lido do Bling."


# ------------------------------------------------------------------ rotina

def _loop():
    time.sleep(180)   # deixa o servidor subir antes
    while True:
        try:
            coletar_pedidos()
        except Exception:
            pass
        try:
            agora = _agora()
            if (_meta("estoque_dia") != agora.strftime("%Y-%m-%d") and agora.hour >= HORA_ESTOQUE) or not _meta("estoque_atualizado"):
                atualizar_estoque()
        except Exception:
            pass
        time.sleep(3600)


# ------------------------------------------------------------------ tela

def _liberado():
    if not session.get("employee_name"):
        return redirect(url_for("login"))
    if session.get("employee_name") not in LIBERADOS:
        flash("A tela Full é liberada só para JULIANA e LUCAS.")
        return redirect(url_for("dashboard"))
    return None


def _quando(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(BRT).strftime("%d/%m às %H:%M")
    except ValueError:
        return iso


def _brl(v):
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _num(v):
    return f"{int(v):,}".replace(",", ".")


def ipi_pct():
    try:
        return float(_meta("ipi_pct") or IPI_PADRAO)
    except ValueError:
        return IPI_PADRAO


def _var(atual, anterior):
    """Variação percentual (None quando não há base de comparação)."""
    if not anterior:
        return None
    return (atual - anterior) / anterior * 100


def dados_painel(periodo=7):
    hoje = _agora().date()
    d = lambda n: (hoje - timedelta(days=n)).strftime("%Y-%m-%d")
    dias7 = [d(i) for i in range(6, -1, -1)]
    dias_periodo = [d(i) for i in range(periodo - 1, -1, -1)]
    dias14 = [d(i) for i in range(13, -1, -1)]
    # Comparação só com dias completos (hoje ainda está acontecendo): os últimos N dias
    # fechados contra os N dias antes deles.
    comp_atual = [d(i) for i in range(periodo, 0, -1)]
    comp_ant = [d(i) for i in range(2 * periodo, periodo, -1)]
    sete_atual = [d(i) for i in range(7, 0, -1)]
    sete_ant = [d(i) for i in range(14, 7, -1)]
    fora = ",".join("?" * len(STATUS_FORA))
    abertos_q = ",".join("?" * len(ABERTOS))
    inicio = min(dias_periodo[0], dias14[0], comp_ant[0])
    fator_ipi = 1 + ipi_pct() / 100

    with models.get_conn() as conn:
        vendas = conn.execute(
            f"""SELECT p.dia, i.item_id, i.model_id, i.sku, i.nome, i.variacao, MAX(i.imagem) AS imagem,
                       SUM(i.qtd) AS q, COUNT(DISTINCT p.order_sn) AS pedidos
                FROM full_itens i JOIN full_pedidos p ON p.order_sn = i.order_sn
                WHERE p.dia >= ? AND p.status NOT IN ({fora})
                GROUP BY p.dia, i.item_id, i.model_id""",
            (inicio, *STATUS_FORA),
        ).fetchall()
        pedidos_dia = {r["dia"]: r["n"] for r in conn.execute(
            f"SELECT dia, COUNT(*) AS n FROM full_pedidos WHERE dia >= ? AND status NOT IN ({fora}) GROUP BY dia",
            (inicio, *STATUS_FORA))}
        em_aberto = conn.execute(
            f"SELECT COUNT(*) AS n FROM full_pedidos WHERE status IN ({abertos_q})", ABERTOS).fetchone()["n"]
        estoque = [dict(r) for r in conn.execute("SELECT * FROM full_estoque")]
        bling = [dict(r) for r in conn.execute("SELECT sku, custo, saldo, formato FROM full_custos")]
    custos = {b["sku"]: (b["custo"] or 0) * fator_ipi for b in bling}
    # Galpão = estoque do Bling (conta Ana, dona da verdade do estoque), sem os kits
    # (composições), que não têm estoque próprio. O estoque do vendedor na Shopee não
    # serve para isso: o mesmo produto aparece em vários anúncios e seria somado várias vezes.
    validos = [b for b in bling if (b["formato"] or "") != "E"]
    saldo_bling = {b["sku"]: max(b["saldo"] or 0, 0) for b in validos}
    tot_galpao = sum(saldo_bling.values())
    custo_galpao = sum(max(b["saldo"] or 0, 0) * (b["custo"] or 0) for b in validos) * fator_ipi
    sem_custo = sum(1 for b in validos if (b["saldo"] or 0) > 0 and not b["custo"])

    linhas = {}
    for e in estoque:
        linhas[(e["item_id"], e["model_id"])] = {
            "sku": e["sku"], "nome": e["nome"], "variacao": e["variacao"], "imagem": e.get("imagem") or "",
            "full": e["full_qtd"], "locais": e["full_locais"],
            "galpao": saldo_bling.get(e["sku"]) if e["sku"] in saldo_bling else None, "dias": {},
        }
    unid_dia = {}
    for v in vendas:
        k = (v["item_id"], v["model_id"])
        ln = linhas.setdefault(k, {"sku": v["sku"], "nome": v["nome"], "variacao": v["variacao"], "imagem": "",
                                   "full": None, "locais": "", "galpao": None, "dias": {}})
        if not ln["imagem"] and v["imagem"]:
            ln["imagem"] = v["imagem"]
        ln["dias"][v["dia"]] = ln["dias"].get(v["dia"], 0) + v["q"]
        unid_dia[v["dia"]] = unid_dia.get(v["dia"], 0) + v["q"]

    tabela = []
    tot_full = 0
    custo_full = 0.0
    for ln in linhas.values():
        custo = custos.get(ln["sku"]) if ln["sku"] else None
        full = ln["full"] or 0
        tot_full += full
        if custo:
            custo_full += full * custo
        vend_periodo = sum(ln["dias"].get(x, 0) for x in dias_periodo)
        # Na tela só entra o que tem a ver com o Full: estoque no Full ou venda no período.
        if not full and not vend_periodo:
            continue
        media = vend_periodo / periodo
        atual = sum(ln["dias"].get(x, 0) for x in comp_atual)
        ant = sum(ln["dias"].get(x, 0) for x in comp_ant)
        tabela.append({
            **ln,
            "custo": custo or None,
            "custo_full": (full * custo) if custo else None,
            "por_dia": [ln["dias"].get(x, 0) for x in dias7],
            "periodo": vend_periodo,
            "media": media,
            "cobertura": (full / media) if media else None,
            "var": _var(atual, ant),
            "novo": bool(atual and not ant),
        })
    tabela.sort(key=lambda x: (-x["periodo"], -(x["full"] or 0)))

    barras = [{"dia": x, "rot": datetime.strptime(x, "%Y-%m-%d").strftime("%d/%m"),
               "unid": unid_dia.get(x, 0), "pedidos": pedidos_dia.get(x, 0)} for x in dias14]
    maior = max([b["unid"] for b in barras] + [1])
    for b in barras:
        b["alt"] = round(100 * b["unid"] / maior)

    s_atual = sum(unid_dia.get(x, 0) for x in sete_atual)
    s_ant = sum(unid_dia.get(x, 0) for x in sete_ant)
    p_atual = sum(unid_dia.get(x, 0) for x in comp_atual)
    p_ant = sum(unid_dia.get(x, 0) for x in comp_ant)
    return {
        "tabela": tabela,
        "dias7": [datetime.strptime(x, "%Y-%m-%d").strftime("%d/%m") for x in dias7],
        "periodo": periodo,
        "barras": barras,
        "hoje_unid": unid_dia.get(dias7[-1], 0),
        "hoje_ped": pedidos_dia.get(dias7[-1], 0),
        "ontem_unid": unid_dia.get(dias7[-2], 0),
        "ontem_ped": pedidos_dia.get(dias7[-2], 0),
        "sete_unid": sum(unid_dia.get(x, 0) for x in dias7),
        "sete_ped": sum(pedidos_dia.get(x, 0) for x in dias7),
        "sete_comp": s_atual, "sete_comp_ant": s_ant, "sete_var": _var(s_atual, s_ant),
        "per_comp": p_atual, "per_comp_ant": p_ant, "per_var": _var(p_atual, p_ant),
        "em_aberto": em_aberto,
        "tot_full": tot_full, "tot_galpao": tot_galpao,
        "custo_full": custo_full, "custo_galpao": custo_galpao,
        "sem_custo": sem_custo,
        "ipi": ipi_pct(),
        "skus_full": sum(1 for x in tabela if x["full"]),
    }


TEMPLATE = """
<style>
.fu-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:18px}
.fu-card{background:#fff;border-radius:14px;padding:14px 16px}
.fu-card .l{font-size:12px;color:#4a5262;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.fu-card .n{font-size:28px;font-weight:800;line-height:1.15;margin-top:4px}
.fu-card .s{font-size:13px;color:#4a5262;margin-top:2px}
.fu-card.custo{border:2px solid #e0b800}
.fu-bars{display:flex;align-items:flex-end;gap:6px;height:130px;padding-top:18px}
.fu-bar{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;min-width:0}
.fu-bar i{display:block;width:100%;background:#fdd32b;border-radius:6px 6px 0 0;min-height:2px}
.fu-bar b{font-size:11px;margin-bottom:3px}
.fu-bar span{font-size:10px;color:#5b6474;margin-top:4px;white-space:nowrap}
.fu-bar.hoje i{background:#1f2633}
.fu-tw{overflow-x:auto}
.fu-t{width:100%;border-collapse:collapse;font-size:13px}
.fu-t th{text-align:right;font-size:11px;color:#4a5262;text-transform:uppercase;letter-spacing:.3px;padding:8px 8px;border-bottom:2px solid #e3e6ec;white-space:nowrap;position:sticky;top:0;background:#fff}
.fu-t td{text-align:right;padding:8px 8px;border-bottom:1px solid #eef0f3;white-space:nowrap}
.fu-t th:nth-child(-n+3),.fu-t td:nth-child(-n+3){text-align:left}
.fu-t td.prod{white-space:normal;min-width:220px;max-width:340px}
.fu-t .var{color:#5b6474;font-size:12px}
.fu-t .dia{color:#4a5262}
.fu-t .zero{color:#c3c8d1}
.fu-t .b{font-weight:800}
.fu-t .baixo{color:#9b1c1c;font-weight:800}
.fu-mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.fu-per{display:flex;gap:6px;align-items:center;font-size:13px}
.fu-per a{padding:5px 12px;border-radius:999px;background:#fff;border:1px solid #d5d9e1;color:#1f2633;text-decoration:none}
.fu-per a.on{background:#1f2633;color:#fff;border-color:#1f2633}
.up{color:#0d6b3f;font-weight:800}.down{color:#9b1c1c;font-weight:800}
.fu-foto{width:44px;height:44px;border-radius:8px;object-fit:cover;background:#eef0f3;display:block}
.fu-aviso{background:#fff7d6;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:14px}
.fu-erro{background:#fde0e0;color:#9b1c1c;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:14px}
.fu-busca{font:inherit;font-size:13px;padding:7px 12px;border:1px solid #c7ccd6;border-radius:10px;min-width:220px}
@media(max-width:900px){.fu-cards{grid-template-columns:1fr 1fr}}
@media(max-width:560px){.fu-cards{grid-template-columns:1fr}}
</style>
<div style="max-width:1300px">
  <div class="v2-h"><div>
    <h1>Full</h1>
    <div class="v2-sub">Pedidos e estoque do Shopee Full. Pedidos atualizados a cada hora{% if ped_at %} (último: {{ ped_at }}){% endif %} · estoque e custo uma vez por dia{% if est_at %} (último: {{ est_at }}){% endif %}.</div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <form method="post" action="{{ url_for('full.atualizar', o='pedidos') }}"><button class="v2-btn sec" type="submit">{{ ic.recarregar|safe }} Atualizar pedidos</button></form>
    <form method="post" action="{{ url_for('full.atualizar', o='estoque') }}"><button class="v2-btn sec" type="submit">{{ ic.recarregar|safe }} Atualizar estoque e custo</button></form>
  </div>
  </div>

  {% if rodando %}<div class="fu-aviso">Atualizando em segundo plano ({{ rodando }}). Recarregue a página em alguns minutos.</div>{% endif %}
  {% for e in erros %}<div class="fu-erro">{{ e }}</div>{% endfor %}
  {% if not ped_at %}<div class="fu-aviso">Primeira carga: o sistema está buscando os pedidos dos últimos 60 dias. Pode levar alguns minutos.</div>{% endif %}

  <div class="fu-cards">
    <div class="fu-card"><div class="l">Vendas Full hoje</div><div class="n">{{ num(d.hoje_unid) }} <small style="font-size:14px">unid.</small></div><div class="s">{{ num(d.hoje_ped) }} pedidos · ontem {{ num(d.ontem_unid) }} unid. / {{ num(d.ontem_ped) }} pedidos</div></div>
    <div class="fu-card"><div class="l">Em aberto no Full</div><div class="n">{{ num(d.em_aberto) }} <small style="font-size:14px">pedidos</small></div><div class="s">pagos que o Full ainda não despachou</div></div>
    <div class="fu-card"><div class="l">Vendas Full · últimos 7 dias</div><div class="n">{{ num(d.sete_unid) }} <small style="font-size:14px">unid.</small></div><div class="s">{{ num(d.sete_ped) }} pedidos · média {{ '%.0f'|format(d.sete_unid/7) }} unid./dia</div>
      <div class="s">{% if d.sete_var is not none %}<span class="{{ 'up' if d.sete_var >= 0 else 'down' }}">{{ '▲' if d.sete_var >= 0 else '▼' }} {{ '%+.0f'|format(d.sete_var) }}%</span> nos 7 dias fechados ({{ num(d.sete_comp) }}) vs 7 anteriores ({{ num(d.sete_comp_ant) }}){% else %}sem base para comparar ainda{% endif %}</div></div>
    <div class="fu-card"><div class="l">Estoque no Full</div><div class="n">{{ num(d.tot_full) }} <small style="font-size:14px">unid.</small></div><div class="s">{{ d.skus_full }} SKU(s) com estoque no Full</div></div>
    <div class="fu-card custo"><div class="l">Custo do estoque no Full</div><div class="n">{{ brl(d.custo_full) }}</div><div class="s">custo do Bling + IPI {{ ('%.1f'|format(d.ipi)).replace('.', ',') }}% × estoque no Full</div></div>
    <div class="fu-card custo"><div class="l">Custo do estoque no galpão</div><div class="n">{{ brl(d.custo_galpao) }}</div><div class="s">{{ num(d.tot_galpao) }} unid. no Bling (sem kits) · total Full + galpão: <b>{{ brl(d.custo_full + d.custo_galpao) }}</b></div></div>
  </div>
  <form method="post" action="{{ url_for('full.salvar_ipi') }}" class="v2-sub" style="margin:-8px 0 14px;display:flex;gap:6px;align-items:center">
    Custo = preço de custo do Bling + IPI de <input name="ipi" value="{{ ('%.2f'|format(d.ipi)).rstrip('0').rstrip('.').replace('.', ',') }}" style="width:52px;font:inherit;padding:2px 6px;border:1px solid #c7ccd6;border-radius:6px">%
    <button class="v2-btn sec" type="submit" style="padding:3px 10px">Salvar</button>
  </form>
  {% if d.sem_custo %}<div class="fu-aviso">{{ d.sem_custo }} produto(s) com estoque no Bling estão sem preço de custo e ficaram fora do valor do custo.</div>{% endif %}

  <div class="v2-panel" style="margin-bottom:18px">
    <h3>Unidades vendidas no Full por dia · últimos 14 dias</h3>
    <div class="fu-bars">
      {% for b in d.barras %}<div class="fu-bar{% if loop.last %} hoje{% endif %}" title="{{ b.rot }}: {{ b.unid }} unid. em {{ b.pedidos }} pedidos"><b>{{ b.unid }}</b><i style="height:{{ b.alt }}%"></i><span>{{ b.rot }}</span></div>{% endfor %}
    </div>
  </div>

  <div class="v2-panel">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
      <h3 style="margin:0">Por SKU</h3>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <input class="fu-busca" id="fu-busca" placeholder="Filtrar SKU ou produto">
        <div class="fu-per">Média de:
          {% for p in (7, 15, 30) %}<a href="{{ url_for('full.painel', p=p) }}" class="{{ 'on' if d.periodo == p }}">{{ p }} dias</a>{% endfor %}
        </div>
      </div>
    </div>
    <div class="fu-tw">
    <table class="fu-t" id="fu-t">
      <thead><tr>
        <th></th><th>SKU</th><th>Produto</th>
        {% for dd in d.dias7 %}<th class="dia">{{ dd }}</th>{% endfor %}
        <th>Vendas {{ d.periodo }}d</th><th>Média/dia</th><th title="Últimos {{ d.periodo }} dias fechados contra os {{ d.periodo }} dias anteriores">Variação</th>
        <th>Full</th><th>Dura (dias)</th><th>Galpão</th><th>Custo unit.</th><th>Custo no Full</th>
      </tr></thead>
      <tbody>
      {% for r in d.tabela %}
      <tr>
        <td>{% if r.imagem %}<img class="fu-foto" src="{{ r.imagem }}" loading="lazy" alt="">{% else %}<span class="fu-foto"></span>{% endif %}</td>
        <td class="fu-mono">{{ r.sku or '—' }}</td>
        <td class="prod">{{ r.nome }}{% if r.variacao %}<div class="var">{{ r.variacao }}</div>{% endif %}</td>
        {% for q in r.por_dia %}<td class="{{ 'zero' if not q }}">{{ q }}</td>{% endfor %}
        <td class="b">{{ num(r.periodo) }}</td>
        <td>{{ '%.1f'|format(r.media)|replace('.', ',') }}</td>
        <td>{% if r.var is not none %}<span class="{{ 'up' if r.var >= 0 else 'down' }}">{{ '▲' if r.var >= 0 else '▼' }} {{ '%+.0f'|format(r.var) }}%</span>{% elif r.novo %}<span class="up">novo</span>{% else %}—{% endif %}</td>
        <td class="b" title="{{ r.locais }}">{{ num(r.full) if r.full is not none else '—' }}</td>
        <td class="{{ 'baixo' if r.cobertura is not none and r.cobertura < 15 }}">{{ '%.0f'|format(r.cobertura) if r.cobertura is not none else '—' }}</td>
        <td>{{ num(r.galpao) if r.galpao is not none else '—' }}</td>
        <td>{{ brl(r.custo) if r.custo else '—' }}</td>
        <td>{{ brl(r.custo_full) if r.custo_full is not none else '—' }}</td>
      </tr>
      {% else %}
      <tr><td colspan="18" style="text-align:center;padding:24px;color:#5b6474">Ainda sem dados do Full. Clique em "Atualizar pedidos" e "Atualizar estoque e custo".</td></tr>
      {% endfor %}
      </tbody>
    </table>
    </div>
    <div class="v2-sub" style="margin-top:8px">"Dura (dias)" = estoque no Full ÷ média de vendas por dia do período escolhido. Em vermelho quando dura menos de 15 dias. "Variação" compara os últimos {{ d.periodo }} dias fechados com os {{ d.periodo }} dias anteriores (hoje não entra, porque ainda está acontecendo). Pedidos cancelados ou não pagos não entram.</div>
  </div>
</div>
<script>
(function(){
  var b=document.getElementById('fu-busca'), rows=document.querySelectorAll('#fu-t tbody tr');
  b.addEventListener('input',function(){
    var q=b.value.toLowerCase();
    rows.forEach(function(r){ r.style.display = r.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none'; });
  });
})();
</script>
"""


@bp.route("/full")
def painel():
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    try:
        periodo = int(request.args.get("p") or 7)
    except ValueError:
        periodo = 7
    if periodo not in (7, 15, 30):
        periodo = 7
    d = dados_painel(periodo)
    rodando = []
    if _lock_pedidos.locked():
        rodando.append("pedidos")
    if _lock_estoque.locked():
        rodando.append("estoque e custo")
    erros = [x for x in (
        ("Pedidos: " + _meta("pedidos_erro")) if _meta("pedidos_erro") else "",
        ("Estoque: " + _meta("estoque_erro")) if _meta("estoque_erro") else "",
        ("Custo (Bling): " + _meta("custo_erro")) if _meta("custo_erro") else "",
    ) if x]
    return render_page(
        "Full", "full", TEMPLATE, d=d, ic=ICONES, num=_num, brl=_brl,
        ped_at=_quando(_meta("pedidos_atualizado")), est_at=_quando(_meta("estoque_atualizado")),
        rodando=" e ".join(rodando), erros=erros,
    )


@bp.route("/full/ipi", methods=["POST"])
def salvar_ipi():
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    try:
        v = float((request.form.get("ipi") or "").replace(",", ".").replace("%", "").strip())
        if not 0 <= v <= 50:
            raise ValueError
        _meta("ipi_pct", v)
        flash("IPI salvo: " + f"{v:g}".replace(".", ",") + "%. O custo do estoque já foi recalculado.")
    except ValueError:
        flash("Digite o IPI em %, por exemplo 6,5.")
    return redirect(url_for("full.painel"))


@bp.route("/full/atualizar/<o>", methods=["POST"])
def atualizar(o):
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    alvo = coletar_pedidos if o == "pedidos" else atualizar_estoque
    trava = _lock_pedidos if o == "pedidos" else _lock_estoque
    if trava.locked():
        flash("Já está atualizando. Recarregue a página em alguns minutos.")
    else:
        def rodar():
            try:
                alvo()
            except Exception:
                pass
        threading.Thread(target=rodar, daemon=True).start()
        flash("Atualizando em segundo plano. Recarregue a página em 1 ou 2 minutos.")
    return redirect(url_for("full.painel"))


def init_app(app, get_shopee_client):
    _get_shopee[0] = get_shopee_client
    init_db()
    app.register_blueprint(bp)
    threading.Thread(target=_loop, daemon=True).start()
