"""Tela Devoluções: bipar a etiqueta de um pacote devolvido e achar pedido + nota.

Fluxo (projeto "Devoluções pelo rastreio"):
  rastreio -> pedido Shopee (banco do dashboard ou lista de devoluções da Shopee)
           -> pedido de venda no Bling (conta Ana) -> nota de saída (chave, itens, cliente)

FASE 3: além da consulta, gera as notas de devolução selecionadas como RASCUNHO
no Bling, copiando o modelo da devolução feita à mão (ver devolucoes_gerar.py), e
já envia cada uma à Sefaz, mostrando se foi autorizada ou rejeitada.

Acesso: só os logins JULIANA e LUCAS. A autorização do Bling (Conectar Bling) só LUCAS.
"""

import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, jsonify, redirect, request, session, url_for

import bling_client
import devolucoes_gerar as gerar
import historico
import models
from layout_v2 import ICONES, render_page

bp = Blueprint("devolucoes", __name__)

BRT = timezone(timedelta(hours=-3))
LIBERADOS = {"JULIANA", "LUCAS"}
DIAS_DEVOLUCOES_SHOPEE = 60   # quanto tempo para trás procurar na lista de devoluções
_get_shopee = [None]

ST_PRONTO = "pronto"
ST_NAO_ACHOU = "nao_encontrado"
ST_SEM_NOTA = "sem_nota"
ST_ERRO = "erro"

ROTULOS = {
    ST_PRONTO: ("Pronto", "ok"),
    ST_NAO_ACHOU: ("Rastreio não encontrado", "warn"),
    ST_SEM_NOTA: ("Sem nota no Bling", "warn"),
    ST_ERRO: ("Erro na consulta", "bad"),
    gerar.ST_NA_FILA: ("Na fila para gerar", "warn"),
    gerar.ST_GERANDO: ("Gerando nota…", "warn"),
    gerar.ST_GERADO: ("Nota criada no Bling", "ok"),
    gerar.ST_FALHOU: ("Falhou ao gerar", "bad"),
    gerar.ST_EMITIDA: ("Autorizada na Sefaz", "ok"),
    gerar.ST_REJEITADA: ("Rejeitada na Sefaz", "bad"),
}


# --------------------------------------------------------------------------- banco

def init_db():
    with models.get_conn() as conn:
        # WAL: leituras (bipagem, telas) não esperam as gravações das devoluções.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devolucoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rastreio TEXT NOT NULL,
                order_sn TEXT,
                origem TEXT,
                bling_pedido_id INTEGER,
                bling_nfe_id INTEGER,
                nfe_numero TEXT,
                nfe_serie TEXT,
                nfe_chave TEXT,
                nfe_situacao INTEGER,
                cliente_nome TEXT,
                cliente_doc TEXT,
                total REAL,
                status TEXT NOT NULL,
                motivo TEXT,
                nfe_devolucao_id INTEGER,
                nfe_devolucao_numero TEXT,
                bipado_por TEXT,
                removido INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dev_order ON devolucoes(order_sn)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devolucao_itens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                devolucao_id INTEGER NOT NULL,
                codigo TEXT,
                descricao TEXT,
                unidade TEXT,
                qtd_original REAL,
                qtd_devolver REAL,
                valor REAL,
                dados_json TEXT
            )
            """
        )
        # Cache da lista de devoluções da Shopee: rastreio de retorno -> pedido.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shopee_devolucoes (
                rastreio TEXT PRIMARY KEY,
                order_sn TEXT NOT NULL,
                return_sn TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS devolucoes_meta (chave TEXT PRIMARY KEY, valor TEXT)"
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(devolucoes)").fetchall()]
        if "nfe_devolucao_situacao" not in cols:
            conn.execute("ALTER TABLE devolucoes ADD COLUMN nfe_devolucao_situacao INTEGER")


def _agora():
    return datetime.utcnow().isoformat()


def _listar():
    with models.get_conn() as conn:
        devs = [dict(r) for r in conn.execute(
            "SELECT * FROM devolucoes WHERE removido = 0 AND (status NOT IN ('emitida') "
            "OR created_at >= ?) ORDER BY id DESC LIMIT 80",
            ((datetime.utcnow() - timedelta(hours=24)).isoformat(),),
        ).fetchall()]
        if devs:
            ids = [d["id"] for d in devs]
            marks = ",".join("?" for _ in ids)
            itens = conn.execute(
                f"SELECT * FROM devolucao_itens WHERE devolucao_id IN ({marks}) ORDER BY id", ids
            ).fetchall()
        else:
            itens = []
    por_dev = {}
    for it in itens:
        por_dev.setdefault(it["devolucao_id"], []).append(dict(it))
    for d in devs:
        d["itens"] = por_dev.get(d["id"], [])
        d["rotulo"], d["tom"] = ROTULOS.get(d["status"], (d["status"], "warn"))
        try:
            d["quando"] = datetime.fromisoformat(d["created_at"]).replace(
                tzinfo=timezone.utc).astimezone(BRT).strftime("%d/%m %H:%M")
        except (TypeError, ValueError):
            d["quando"] = ""
    return devs


def _ja_na_lista(rastreio, order_sn):
    with models.get_conn() as conn:
        if order_sn:
            row = conn.execute(
                "SELECT id FROM devolucoes WHERE removido = 0 AND (order_sn = ? OR rastreio = ?)",
                (order_sn, rastreio),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM devolucoes WHERE removido = 0 AND rastreio = ?", (rastreio,)
            ).fetchone()
        return row is not None


def _gravar(reg, itens):
    agora = _agora()
    with models.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO devolucoes (rastreio, order_sn, origem, bling_pedido_id, bling_nfe_id,
                 nfe_numero, nfe_serie, nfe_chave, nfe_situacao, cliente_nome, cliente_doc, total,
                 status, motivo, bipado_por, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                reg["rastreio"], reg.get("order_sn"), reg.get("origem"), reg.get("bling_pedido_id"),
                reg.get("bling_nfe_id"), reg.get("nfe_numero"), reg.get("nfe_serie"),
                reg.get("nfe_chave"), reg.get("nfe_situacao"), reg.get("cliente_nome"),
                reg.get("cliente_doc"), reg.get("total"), reg["status"], reg.get("motivo"),
                reg.get("bipado_por"), agora, agora,
            ),
        )
        dev_id = cur.lastrowid
        for it in itens:
            conn.execute(
                """INSERT INTO devolucao_itens (devolucao_id, codigo, descricao, unidade,
                     qtd_original, qtd_devolver, valor, dados_json) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    dev_id, it.get("codigo"), it.get("descricao"), it.get("unidade"),
                    it.get("quantidade"), it.get("quantidade"), it.get("valor"),
                    json.dumps(it, ensure_ascii=False),
                ),
            )
    return dev_id


# ------------------------------------------------------------------------- busca

def _normalizar(rastreio):
    r = (rastreio or "").strip().upper().replace(" ", "")
    if "SPXLM" in r:
        r = r.split("SPXLM")[0]
    return r


CHAVES_RASTREIO = ("tracking_number", "reverse_tracking_number", "return_tracking_number")
SYNC_MIN_INTERVALO = 45        # segundos entre duas atualizações da lista da Shopee
SYNC_FUNDO_INTERVALO = 600     # ao abrir a tela, atualiza em segundo plano se passou disso
_sync_lock = threading.Lock()


def _meta_ler(chave):
    with models.get_conn() as conn:
        row = conn.execute("SELECT valor FROM devolucoes_meta WHERE chave = ?", (chave,)).fetchone()
        return row["valor"] if row else None


def _meta_gravar(chave, valor):
    with models.get_conn() as conn:
        conn.execute(
            "INSERT INTO devolucoes_meta (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (chave, str(valor)),
        )


def _cache_buscar(rastreio):
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT order_sn FROM shopee_devolucoes WHERE rastreio = ?", (rastreio,)
        ).fetchone()
        return row["order_sn"] if row else None


def _baixar_janela(client, ini, fim, campo):
    """Baixa todas as devoluções da Shopee de uma janela (até 15 dias) e guarda no cache."""
    salvos = 0
    for pagina in range(50):
        resp = client._shop_request(
            "GET", "/api/v2/returns/get_return_list",
            params={"page_no": pagina, "page_size": 100,
                    f"{campo}_from": ini, f"{campo}_to": fim},
        )
        if resp.get("error"):
            raise RuntimeError(resp.get("message") or resp.get("error"))
        corpo = resp.get("response") or {}
        lista = corpo.get("return") or corpo.get("return_list") or []
        linhas = []
        for dev in lista:
            sn = dev.get("order_sn")
            if not sn:
                continue
            for k in CHAVES_RASTREIO:
                r = _normalizar(str(dev.get(k) or ""))
                if r:
                    linhas.append((r, sn, str(dev.get("return_sn") or "")))
        if linhas:
            with models.get_conn() as conn:
                conn.executemany(
                    "INSERT INTO shopee_devolucoes (rastreio, order_sn, return_sn) VALUES (?,?,?) "
                    "ON CONFLICT(rastreio) DO UPDATE SET order_sn = excluded.order_sn, "
                    "return_sn = excluded.return_sn",
                    linhas,
                )
            salvos += len(linhas)
        if not corpo.get("more"):
            break
        time.sleep(0.3)  # folga para o servidor atender as outras telas
    return salvos


def sincronizar_devolucoes_shopee(forcar=False):
    """Atualiza o cache local de devoluções da Shopee (rastreio de retorno -> pedido).

    Primeira vez: baixa os últimos 60 dias. Depois: só o que mudou desde a última
    atualização (normalmente uma página). Nunca roda duas ao mesmo tempo, e não roda
    de novo antes de SYNC_MIN_INTERVALO segundos, a não ser que forcar=True."""
    client = _get_shopee[0]() if _get_shopee[0] else None
    if client is None:
        return False
    if not _sync_lock.acquire(blocking=False):
        # Outra atualização já está rodando: não espera (não segura o servidor).
        return True
    try:
        agora = int(time.time())
        ultima_em = int(float(_meta_ler("shopee_ret_sync_em") or 0))
        if not forcar and agora - ultima_em < SYNC_MIN_INTERVALO:
            return True
        ate = int(float(_meta_ler("shopee_ret_sync_ate") or 0))
        limite = agora - DIAS_DEVOLUCOES_SHOPEE * 86400
        # Margem de 1 hora para trás, para não perder devolução atualizada na virada.
        desde = max(ate - 3600, limite) if ate else limite
        fim = agora
        while fim > desde:
            ini = max(fim - 15 * 86400 + 60, desde)
            try:
                # update_time pega também devoluções antigas que só agora ganharam rastreio.
                _baixar_janela(client, ini, fim, "update_time")
            except Exception:
                _baixar_janela(client, ini, fim, "create_time")
            fim = ini
        _meta_gravar("shopee_ret_sync_ate", agora)
        _meta_gravar("shopee_ret_sync_em", agora)
        return True
    except Exception:
        return False
    finally:
        _sync_lock.release()


def sincronizar_desde(ts_inicio):
    """Baixa a lista de devoluções da Shopee de ts_inicio até agora (usado pela importação
    do histórico, em segundo plano). Espera a vez se outra atualização estiver rodando."""
    client = _get_shopee[0]() if _get_shopee[0] else None
    if client is None:
        return
    with _sync_lock:
        fim = int(time.time())
        while fim > ts_inicio:
            ini = max(fim - 15 * 86400 + 60, ts_inicio)
            try:
                _baixar_janela(client, ini, fim, "create_time")
            except Exception:
                pass
            fim = ini


def _sincronizar_em_fundo_se_preciso():
    """Só a PRIMEIRA carga (60 dias) roda em segundo plano ao abrir a tela. Depois disso
    a lista é atualizada apenas quando uma bipagem não acha o rastreio no cache."""
    try:
        ja_teve = _meta_ler("shopee_ret_sync_ate")
    except Exception:
        ja_teve = "1"
    if not ja_teve and not _sync_lock.locked():
        threading.Thread(target=sincronizar_devolucoes_shopee, daemon=True).start()


def _buscar_nas_devolucoes_shopee(rastreio):
    """Rastreio de retorno: primeiro no cache local; se não estiver, atualiza o cache
    só com o que é novo na Shopee e procura de novo."""
    sn = _cache_buscar(rastreio)
    if sn:
        return sn
    sincronizar_devolucoes_shopee()
    return _cache_buscar(rastreio)


def consultar(rastreio):
    """Resolve rastreio -> pedido Shopee -> pedido Bling -> nota de saída. Só leitura."""
    reg = {"rastreio": rastreio}
    itens = []

    local = models.find_by_tracking(rastreio)
    antigo = None if local else historico.buscar(rastreio)
    if local:
        reg["order_sn"], reg["origem"] = local["order_sn"], "banco"
    elif antigo:
        reg["order_sn"], reg["origem"] = antigo, "histórico"
    else:
        sn = _buscar_nas_devolucoes_shopee(rastreio)
        if sn:
            reg["order_sn"], reg["origem"] = sn, "devoluções Shopee"
    if not reg.get("order_sn"):
        reg["status"] = ST_NAO_ACHOU
        reg["motivo"] = "Não está no banco do dashboard, no histórico desde junho nem na lista de devoluções da Shopee."
        return reg, itens

    try:
        # Atalho: nota de saída direto pelo número do pedido Shopee.
        nfes = []
        try:
            nfes = bling_client.buscar_nfe_saida_por_numero_loja(reg["order_sn"])
        except Exception:
            nfes = []
        # Se vier muita nota, o filtro não foi aplicado: ignora o atalho.
        if 0 < len(nfes) <= 3:
            validas = [n for n in nfes if n.get("situacao") in bling_client.NFE_AUTORIZADAS] or nfes
            nfe = bling_client.detalhe_nfe(validas[0]["id"])
            # Só aceita se a nota for mesmo deste pedido Shopee.
            if str(nfe.get("numeroPedidoLoja") or "").strip() == reg["order_sn"]:
                return _montar(reg, itens, nfe)
        pedidos = bling_client.buscar_pedido_por_numero_loja(reg["order_sn"])
        if not pedidos:
            reg["status"] = ST_SEM_NOTA
            reg["motivo"] = "Pedido não encontrado no Bling (conta Ana)."
            return reg, itens
        pedido = bling_client.detalhe_pedido(pedidos[0]["id"])
        reg["bling_pedido_id"] = pedido.get("id")
        nfe_id = (pedido.get("notaFiscal") or {}).get("id")
        if not nfe_id:
            reg["status"] = ST_SEM_NOTA
            reg["motivo"] = "O pedido no Bling não tem nota de saída ligada."
            reg["cliente_nome"] = (pedido.get("contato") or {}).get("nome")
            return reg, itens
        nfe = bling_client.detalhe_nfe(nfe_id)
    except bling_client.BlingErro as e:
        reg["status"] = ST_ERRO
        reg["motivo"] = f"Bling: {e}"
        return reg, itens
    except Exception as e:  # rede fora do ar etc.
        reg["status"] = ST_ERRO
        reg["motivo"] = f"Falha de conexão com o Bling: {e}"
        return reg, itens

    return _montar(reg, itens, nfe)


def _montar(reg, itens, nfe):
    contato = nfe.get("contato") or {}
    reg.update(
        bling_nfe_id=nfe.get("id"),
        nfe_numero=str(nfe.get("numero") or ""),
        nfe_serie=str(nfe.get("serie") or ""),
        nfe_chave=nfe.get("chaveAcesso") or "",
        nfe_situacao=nfe.get("situacao"),
        cliente_nome=contato.get("nome"),
        cliente_doc=contato.get("numeroDocumento"),
        total=nfe.get("valorNota"),
    )
    for it in nfe.get("itens") or []:
        itens.append({
            "codigo": it.get("codigo"),
            "descricao": it.get("descricao"),
            "unidade": it.get("unidade"),
            "quantidade": it.get("quantidade"),
            "valor": it.get("valor"),
            "cfop": it.get("cfop"),
        })

    if nfe.get("tipo") not in (1, "1", None):
        reg["status"], reg["motivo"] = ST_SEM_NOTA, "A nota ligada ao pedido não é de saída."
    elif reg["nfe_situacao"] not in bling_client.NFE_AUTORIZADAS:
        situ = bling_client.NFE_SITUACOES.get(reg["nfe_situacao"], str(reg["nfe_situacao"]))
        reg["status"], reg["motivo"] = ST_SEM_NOTA, f"Nota de saída está '{situ}', não autorizada."
    elif not reg["nfe_chave"]:
        reg["status"], reg["motivo"] = ST_SEM_NOTA, "Nota de saída sem chave de acesso."
    else:
        reg["status"] = ST_PRONTO
    return reg, itens


# ------------------------------------------------------------------------ rotas

@bp.before_request
def _somente_liberados():
    if request.endpoint == "devolucoes.bling_callback":
        return None
    if session.get("employee_name") not in LIBERADOS:
        flash("A tela Devoluções é liberada só para JULIANA e LUCAS.")
        return redirect(url_for("dashboard"))
    return None


@bp.route("/devolucoes")
def painel():
    _sincronizar_em_fundo_se_preciso()
    devs = _listar()
    fila = [d for d in devs if d["status"] in (gerar.ST_NA_FILA, gerar.ST_GERANDO)]
    resumo = {
        "total": len(devs),
        "prontos": sum(1 for d in devs if d["status"] == ST_PRONTO),
        "gerados": sum(1 for d in devs if d["status"] in (gerar.ST_GERADO, gerar.ST_EMITIDA)),
        "emitidas": sum(1 for d in devs if d["status"] == gerar.ST_EMITIDA),
        "problemas": sum(1 for d in devs if d["status"] in (ST_NAO_ACHOU, ST_SEM_NOTA, ST_ERRO, gerar.ST_FALHOU, gerar.ST_REJEITADA)),
        "fila": len(fila),
    }
    modelo = None
    try:
        salvo = gerar._meta_ler("modelo_devolucao")
        modelo = json.loads(salvo) if salvo else None
    except Exception:
        modelo = None
    return render_page(
        "Devoluções", "devolucoes", TEMPLATE,
        devs=devs, resumo=resumo, ic=ICONES, modelo=modelo,
        hist=historico.situacao() if session.get("employee_name") == "LUCAS" else None,
        bling_configurado=bling_client.configurado(),
        bling_conectado=bling_client.conectado(),
        eh_lucas=session.get("employee_name") == "LUCAS",
    )


@bp.route("/devolucoes/fila")
def fila():
    """Consulta leve (sem montar a página) usada pela tela enquanto há notas sendo geradas."""
    with models.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM devolucoes WHERE removido = 0 AND status IN ('na_fila', 'gerando')"
        ).fetchone()[0]
    return jsonify({"fila": n})


@bp.route("/devolucoes/bipar", methods=["POST"])
def bipar():
    rastreio = _normalizar(request.form.get("rastreio"))
    if not rastreio:
        return redirect(url_for("devolucoes.painel"))
    if not bling_client.conectado():
        flash("Conecte o Bling antes de bipar.")
        return redirect(url_for("devolucoes.painel"))
    if _ja_na_lista(rastreio, None):
        flash(f"{rastreio} já está na lista.")
        return redirect(url_for("devolucoes.painel"))

    reg, itens = consultar(rastreio)
    if reg.get("order_sn") and _ja_na_lista(rastreio, reg["order_sn"]):
        flash(f"O pedido {reg['order_sn']} já está na lista (bipado com outra etiqueta).")
        return redirect(url_for("devolucoes.painel"))
    reg["bipado_por"] = session.get("employee_name")
    _gravar(reg, itens)
    if reg["status"] == ST_PRONTO:
        flash(f"{rastreio}: pedido {reg['order_sn']}, nota {reg['nfe_numero']} encontrada.")
    else:
        flash(f"{rastreio}: {reg.get('motivo')}")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/gerar", methods=["POST"])
def gerar_selecionadas():
    """Põe na fila as devoluções marcadas (com as quantidades ajustadas) e dispara a
    geração em segundo plano. Cada nota é criada como rascunho no Bling."""
    ids = [int(i) for i in request.form.getlist("ids") if str(i).isdigit()]
    if not ids:
        flash("Marque pelo menos uma devolução para gerar.")
        return redirect(url_for("devolucoes.painel"))
    try:
        gerar.obter_modelo()
    except Exception as e:
        flash(f"Não consegui preparar o modelo da nota: {e}")
        return redirect(url_for("devolucoes.painel"))
    postos = 0
    with models.get_conn() as conn:
        for dev_id in ids:
            dev = conn.execute(
                "SELECT status, nfe_devolucao_id FROM devolucoes WHERE id = ? AND removido = 0", (dev_id,)
            ).fetchone()
            if not dev or dev["nfe_devolucao_id"] or dev["status"] not in (ST_PRONTO, gerar.ST_FALHOU):
                continue
            itens = conn.execute(
                "SELECT id, qtd_original FROM devolucao_itens WHERE devolucao_id = ?", (dev_id,)
            ).fetchall()
            total = 0.0
            for it in itens:
                bruto = request.form.get(f"qtd_{it['id']}")
                try:
                    qtd = float(str(bruto).replace(",", ".")) if bruto not in (None, "") else float(it["qtd_original"] or 0)
                except ValueError:
                    qtd = float(it["qtd_original"] or 0)
                qtd = max(0.0, min(qtd, float(it["qtd_original"] or 0)))
                total += qtd
                conn.execute("UPDATE devolucao_itens SET qtd_devolver = ? WHERE id = ?", (qtd, it["id"]))
            if total <= 0:
                continue
            conn.execute(
                "UPDATE devolucoes SET status = ?, motivo = NULL, updated_at = ? WHERE id = ?",
                (gerar.ST_NA_FILA, _agora(), dev_id),
            )
            postos += 1
    if postos:
        gerar.iniciar_fila()
        flash(f"{postos} devolução(ões) na fila. As notas vão aparecer no Bling como Pendente.")
    else:
        flash("Nenhuma devolução marcada estava pronta para gerar.")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/<int:dev_id>/enviar", methods=["POST"])
def enviar_sefaz(dev_id):
    """Reenvia à Sefaz uma nota de devolução já criada (pendente ou rejeitada e corrigida)."""
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT nfe_devolucao_id, status FROM devolucoes WHERE id = ? AND removido = 0", (dev_id,)
        ).fetchone()
        if not row or not row["nfe_devolucao_id"] or row["status"] in (gerar.ST_EMITIDA, gerar.ST_NA_FILA, gerar.ST_GERANDO):
            return redirect(url_for("devolucoes.painel"))
        conn.execute("UPDATE devolucoes SET status = ?, motivo = NULL WHERE id = ?", (gerar.ST_NA_FILA, dev_id))
    gerar.iniciar_fila()
    flash("Enviando à Sefaz…")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/<int:dev_id>/situacao", methods=["POST"])
def atualizar_situacao(dev_id):
    try:
        gerar.atualizar_situacao(dev_id)
    except Exception as e:
        flash(f"Não consegui consultar o Bling: {e}")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/<int:dev_id>/previa")
def previa(dev_id):
    """Mostra o que seria enviado ao Bling, sem criar nada."""
    try:
        corpo = gerar.corpo_para(dev_id)
        modelo = gerar.obter_modelo()
        erro = None
    except Exception as e:
        corpo, modelo, erro = None, None, str(e)
    return render_page(
        "Prévia da devolução", "devolucoes", TEMPLATE_PREVIA,
        corpo=json.dumps(corpo, ensure_ascii=False, indent=2) if corpo else "",
        modelo=modelo, erro=erro, ic=ICONES,
    )


@bp.route("/devolucoes/historico/importar", methods=["POST"])
def importar_historico():
    """Importa (em segundo plano) os rastreios dos pedidos de junho para cá. Só LUCAS."""
    if session.get("employee_name") != "LUCAS":
        return redirect(url_for("devolucoes.painel"))
    if historico.iniciar():
        flash("Importação do histórico iniciada. Pode continuar usando o dashboard normalmente.")
    else:
        flash("A importação do histórico já está rodando.")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/modelo/atualizar", methods=["POST"])
def atualizar_modelo():
    try:
        m = gerar.obter_modelo(forcar=True)
        flash(f"Modelo atualizado: nota de devolução {m.get('numero')} ({m.get('cliente')}).")
    except Exception as e:
        flash(f"Não consegui atualizar o modelo: {e}")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/<int:dev_id>/remover", methods=["POST"])
def remover(dev_id):
    with models.get_conn() as conn:
        conn.execute(
            "UPDATE devolucoes SET removido = 1, updated_at = ? WHERE id = ? AND nfe_devolucao_id IS NULL "
            "AND status NOT IN ('na_fila', 'gerando')",
            (_agora(), dev_id),
        )
    flash("Removido da lista.")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/devolucoes/<int:dev_id>/reconsultar", methods=["POST"])
def reconsultar(dev_id):
    """Para quem deu erro ou não tinha nota: consulta de novo (ex.: a nota foi emitida depois)."""
    with models.get_conn() as conn:
        row = conn.execute("SELECT * FROM devolucoes WHERE id = ? AND removido = 0", (dev_id,)).fetchone()
    if not row or row["nfe_devolucao_id"]:
        return redirect(url_for("devolucoes.painel"))
    reg, itens = consultar(row["rastreio"])
    reg["bipado_por"] = row["bipado_por"]
    with models.get_conn() as conn:
        conn.execute("UPDATE devolucoes SET removido = 1 WHERE id = ?", (dev_id,))
    _gravar(reg, itens)
    flash(f"{row['rastreio']}: consultado de novo — {ROTULOS.get(reg['status'], (reg['status'],))[0]}.")
    return redirect(url_for("devolucoes.painel"))


@bp.route("/bling/autorizar")
def bling_autorizar():
    if session.get("employee_name") != "LUCAS":
        flash("Só o LUCAS pode conectar o Bling.")
        return redirect(url_for("devolucoes.painel"))
    if not bling_client.configurado():
        flash("Faltam BLING_CLIENT_ID e BLING_CLIENT_SECRET no Render.")
        return redirect(url_for("devolucoes.painel"))
    state = secrets.token_urlsafe(16)
    session["bling_state"] = state
    return redirect(bling_client.url_autorizacao(state))


@bp.route("/bling/callback")
def bling_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state or state != session.pop("bling_state", None):
        flash("Retorno do Bling inválido ou expirado. Tente 'Conectar Bling' de novo.")
        return redirect(url_for("devolucoes.painel"))
    try:
        bling_client.trocar_code(code)
    except Exception as e:
        flash(f"Não consegui conectar o Bling: {e}")
        return redirect(url_for("devolucoes.painel"))
    flash("Bling (conta Ana) conectado. Já pode bipar as devoluções.")
    return redirect(url_for("devolucoes.painel"))


TEMPLATE = """
<style>
.dv-scan{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.dv-scan input{flex:1;min-width:260px;font:inherit;font-size:18px;padding:12px 16px;border:2px solid #c7ccd6;border-radius:12px;background:#fff;letter-spacing:.5px}
.dv-scan input:focus{outline:none;border-color:#fdd32b}
.dv-tag{display:inline-block;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
.dv-tag.ok{background:#d9f3e6;color:#0d6b3f}
.dv-tag.warn{background:#fff1cc;color:#7a5300}
.dv-tag.bad{background:#fde0e0;color:#9b1c1c}
.dv-row{border-top:1px solid #e3e6ec;padding:14px 0;display:grid;grid-template-columns:28px 150px 1fr auto;gap:14px;align-items:start}
.dv-row:first-of-type{border-top:0}
.dv-row input[type=checkbox]{width:20px;height:20px;margin-top:2px;cursor:pointer}
.dv-mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.dv-sub{font-size:12px;color:#5b6474;margin-top:3px}
.dv-itens{margin:8px 0 0;padding:0;list-style:none;font-size:13px}
.dv-itens li{padding:3px 0;display:flex;align-items:center;gap:6px}
.dv-qtd{width:58px;font:inherit;padding:3px 6px;border:1px solid #c7ccd6;border-radius:8px;text-align:center}
.dv-acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.dv-wrap .v2-panel{margin-bottom:18px}
.dv-aviso{background:#fff;border-left:4px solid #fdd32b;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:14px}
.dv-bar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.dv-ok{color:#0d6b3f;font-weight:600}
.dv-cards4{grid-template-columns:repeat(4,minmax(0,1fr))}
@media (max-width:700px){.dv-row{grid-template-columns:28px 1fr}.dv-row>div:nth-child(3),.dv-row>div:nth-child(4){grid-column:2}.dv-acts{justify-content:flex-start}.dv-cards4{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>

<div class="dv-wrap" style="max-width:1100px">
  <div class="v2-h">
    <div>
      <h1>Devoluções</h1>
      <div class="v2-sub">Bipe a etiqueta do pacote que voltou, confira os itens e gere as notas. Cada nota é criada no Bling e <b>enviada à Sefaz</b> na hora; a linha mostra se foi autorizada.</div>
    </div>
  </div>

  {% if not bling_configurado %}
  <div class="dv-aviso">Faltam as variáveis <b>BLING_CLIENT_ID</b> e <b>BLING_CLIENT_SECRET</b> no Render.</div>
  {% elif not bling_conectado %}
  <div class="dv-aviso">O Bling (conta Ana) ainda não está conectado.
    {% if eh_lucas %}<a class="v2-btn" style="margin-left:10px" href="{{ url_for('devolucoes.bling_autorizar') }}">Conectar Bling</a>
    {% else %}Peça para o LUCAS conectar.{% endif %}
  </div>
  {% endif %}
  {% if bling_conectado and eh_lucas %}<div class="dv-sub" style="margin:-6px 0 12px">Bling conectado. <a href="{{ url_for('devolucoes.bling_autorizar') }}">Reconectar</a> (use depois de mudar permissões do app no Bling).</div>{% endif %}
  {% if hist %}<div class="dv-sub" style="margin:-6px 0 12px">Histórico desde {{ hist.inicio }}: <b>{{ hist.total }}</b> rastreios salvos{% if hist.ate %}, até {{ hist.ate }}{% endif %}.
    {% if hist.rodando %}<b>Importando…</b> (atualize a página para ver o andamento)
    {% else %}<form method="post" action="{{ url_for('devolucoes.importar_historico') }}" style="display:inline"><button class="v2-btn sec" type="submit" style="padding:2px 8px">{% if hist.concluido %}atualizar histórico{% elif hist.ate %}continuar importação{% else %}importar pedidos desde junho{% endif %}</button></form>{% endif %}
    {% if hist.erro %}<span style="color:#9b1c1c"> {{ hist.erro }}</span>{% endif %}</div>{% endif %}

  {% if resumo.fila %}
  <div class="dv-aviso"><b>Gerando notas…</b> faltam {{ resumo.fila }}. A página atualiza quando terminar (pode continuar usando o dashboard).</div>
  {% endif %}

  <div class="v2-panel">
    <form class="dv-scan" method="post" action="{{ url_for('devolucoes.bipar') }}">
      <input name="rastreio" placeholder="Bipe ou digite o rastreio da etiqueta" autocomplete="off" autofocus {% if not bling_conectado %}disabled{% endif %}>
      <button class="v2-btn" type="submit" {% if not bling_conectado %}disabled{% endif %}>Buscar</button>
    </form>
  </div>

  <div class="v2-cards dv-cards4">
    <div class="v2-card"><div><div class="l">Na lista</div><div class="n">{{ resumo.total }}</div></div></div>
    <div class="v2-card" style="border-color:#2a78d6"><div><div class="l">Prontos para gerar</div><div class="n">{{ resumo.prontos }}</div></div></div>
    <div class="v2-card" style="border-color:#1baf7a"><div><div class="l">Autorizadas na Sefaz</div><div class="n">{{ resumo.emitidas }}</div></div></div>
    <div class="v2-card" style="border-color:#eb6834"><div><div class="l">Com problema</div><div class="n">{{ resumo.problemas }}</div></div></div>
  </div>

  <form id="fgerar" method="post" action="{{ url_for('devolucoes.gerar_selecionadas') }}"></form>
  <div class="v2-panel">
    <div class="dv-bar">
      <h3 style="margin:0">Devoluções a gerar</h3>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <label class="dv-sub" style="margin:0;cursor:pointer"><input type="checkbox" id="dv-todos"> marcar todos os prontos</label>
        <button class="v2-btn" type="submit" form="fgerar" onclick="return confirm('Criar as notas de devolução marcadas e ENVIAR à Sefaz?')">Gerar notas de devolução</button>
      </div>
    </div>
    {% if modelo %}<div class="dv-sub" style="margin-bottom:6px">Modelo usado: nota de devolução {{ modelo.numero }} ({{ modelo.cliente }}).
      <form method="post" action="{{ url_for('devolucoes.atualizar_modelo') }}" style="display:inline"><button class="v2-btn sec" type="submit" style="padding:2px 8px">atualizar modelo</button></form></div>{% endif %}
    {% if not devs %}<p class="v2-sub">Nenhum pacote bipado ainda.</p>{% endif %}
    {% for d in devs %}
    {% set pode = d.status in ('pronto', 'falhou') and not d.nfe_devolucao_id %}
    <div class="dv-row">
      <div>{% if pode %}<input type="checkbox" name="ids" value="{{ d.id }}" form="fgerar" class="dv-sel{% if d.status == 'pronto' %} dv-pronto{% endif %}">{% endif %}</div>
      <div>
        <div class="dv-mono"><b>{{ d.rastreio }}</b></div>
        <div class="dv-sub">{{ d.quando }} · {{ d.bipado_por or '' }}</div>
        <div style="margin-top:6px"><span class="dv-tag {{ d.tom }}">{{ d.rotulo }}</span></div>
      </div>
      <div>
        {% if d.order_sn %}<div><b>Pedido</b> <span class="dv-mono">{{ d.order_sn }}</span> <span class="dv-sub">({{ d.origem }})</span></div>{% endif %}
        {% if d.cliente_nome %}<div class="dv-sub">{{ d.cliente_nome }}{% if d.cliente_doc %} · {{ d.cliente_doc }}{% endif %}</div>{% endif %}
        {% if d.nfe_numero %}<div class="dv-sub">Nota de saída {{ d.nfe_numero }}{% if d.nfe_serie %}/{{ d.nfe_serie }}{% endif %}{% if d.total %} · R$ {{ '%.2f'|format(d.total)|replace('.', ',') }}{% endif %}</div>
        <div class="dv-sub dv-mono">{{ d.nfe_chave }}</div>{% endif %}
        {% if d.nfe_devolucao_id %}<div class="dv-ok" style="margin-top:4px{% if d.status != 'emitida' %};color:#7a5300{% endif %}">Nota de devolução nº {{ d.nfe_devolucao_numero }}{% if d.status == 'emitida' %} autorizada na Sefaz.{% elif d.status == 'rejeitada' %} rejeitada pela Sefaz.{% elif d.status in ('na_fila', 'gerando') %} enviando…{% else %} criada no Bling, ainda não autorizada.{% endif %}</div>{% endif %}
        {% if d.motivo %}<div class="dv-sub" style="color:#9b1c1c">{{ d.motivo }}</div>{% endif %}
        {% if d.itens %}<ul class="dv-itens">
          {% for it in d.itens %}<li>
            {% if pode %}<input class="dv-qtd" type="number" name="qtd_{{ it.id }}" form="fgerar" min="0" max="{{ (it.qtd_original or 0)|round(0)|int }}" step="1" value="{{ (it.qtd_devolver if it.qtd_devolver is not none else it.qtd_original)|round(0)|int }}" title="Quantidade que voltou">
            <span class="dv-sub">de {{ (it.qtd_original or 0)|round(0)|int }} ×</span>
            {% else %}{{ ((it.qtd_devolver if d.nfe_devolucao_id else it.qtd_original) or 0)|round(0)|int }} ×{% endif %}
            {{ it.descricao }} <span class="dv-sub">{{ it.codigo }}</span></li>{% endfor %}
        </ul>{% endif %}
      </div>
      <div class="dv-acts">
        {% if pode %}<a class="v2-btn sec" href="{{ url_for('devolucoes.previa', dev_id=d.id) }}">Ver prévia</a>{% endif %}
        {% if d.nfe_devolucao_id and d.status in ('gerado', 'rejeitada') %}
        <form method="post" action="{{ url_for('devolucoes.enviar_sefaz', dev_id=d.id) }}"><button class="v2-btn" type="submit">Enviar à Sefaz</button></form>
        <form method="post" action="{{ url_for('devolucoes.atualizar_situacao', dev_id=d.id) }}"><button class="v2-btn sec" type="submit">Atualizar situação</button></form>
        {% endif %}
        {% if d.status in ('nao_encontrado', 'sem_nota', 'erro') %}
        <form method="post" action="{{ url_for('devolucoes.reconsultar', dev_id=d.id) }}"><button class="v2-btn sec" type="submit">Consultar de novo</button></form>
        {% endif %}
        {% if not d.nfe_devolucao_id and d.status not in ('na_fila', 'gerando') %}
        <form method="post" action="{{ url_for('devolucoes.remover', dev_id=d.id) }}"><button class="v2-btn sec" type="submit">Remover</button></form>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
</div>
<script>
(function(){
  var fila = {{ resumo.fila }};
  if (fila > 0) {
    var timer = setInterval(function(){
      if (document.hidden) return;
      fetch('{{ url_for('devolucoes.fila') }}', {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(j){
        if (j.fila !== fila) { clearInterval(timer); location.reload(); }
      }).catch(function(){});
    }, 8000);
  }
  var t=document.getElementById('dv-todos');
  if(t) t.addEventListener('change',function(){
    document.querySelectorAll('.dv-pronto').forEach(function(c){c.checked=t.checked;});
  });
})();
</script>
"""


TEMPLATE_PREVIA = """
<div style="max-width:1000px">
  <div class="v2-h"><div>
    <h1>Prévia da nota de devolução</h1>
    <div class="v2-sub">É isto que será enviado ao Bling ao gerar. Nada foi criado.</div>
  </div><a class="v2-btn sec" href="{{ url_for('devolucoes.painel') }}">Voltar</a></div>
  {% if erro %}<div class="v2-panel" style="color:#9b1c1c">{{ erro }}</div>{% endif %}
  {% if modelo %}<div class="v2-panel" style="margin-bottom:16px;font-size:14px">
    Modelo: nota de devolução <b>{{ modelo.numero }}</b> ({{ modelo.cliente }}) · natureza {{ modelo.natureza_id }} · loja {{ modelo.loja_id }} · CFOP final {{ modelo.cfop_final or '—' }}
  </div>{% endif %}
  {% if corpo %}<div class="v2-panel"><pre style="white-space:pre-wrap;font-size:12px;margin:0">{{ corpo }}</pre></div>{% endif %}
</div>
"""


def init_app(app, get_shopee_client):
    _get_shopee[0] = get_shopee_client
    bling_client.init_db()
    init_db()
    historico.init_app(get_shopee_client, sincronizar_desde)
    app.register_blueprint(bp)
    try:
        gerar.retomar_se_preciso()
    except Exception:
        pass
