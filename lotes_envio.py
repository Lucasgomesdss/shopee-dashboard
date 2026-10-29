"""Etiquetas — etapa 2: organizar o envio de lotes na Shopee e gerar o .txt ZPL (Zebra).

Fluxo de um trabalho (roda em segundo plano, um por vez):
  1. Recalcula os lotes na hora (lista fresca da Shopee) e pega os pedidos dos lotes marcados.
  2. Confere de novo que cada pedido ainda está "A enviar" (READY_TO_SHIP) — nunca organiza duas vezes.
  3. Organiza o envio (ship_order) com coleta Shopee Xpress: endereço de coleta da loja e o
     primeiro horário que a própria Shopee oferece.
  4. Espera a Shopee gerar o rastreio e baixa as etiquetas térmicas (ZPL), na ordem do lote
     (mesmo produto junto), reaproveitando a rotina da reimpressão.
  5. Guarda um .txt por lote para baixar (fica no histórico da tela).

ATENÇÃO: o passo 3 muda o status do pedido na Shopee e não tem volta pelo sistema.
Por isso: só JULIANA/LUCAS, confirmação na tela e campo "quantidade" para testar com poucos.
"""

import json
import re
import threading
import unicodedata
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, flash, redirect, request, session, url_for

import lotes
import models
import reimpressao

bp = Blueprint("lotes_envio", __name__)

BRT = timezone(timedelta(hours=-3))
LIBERADOS = {"JULIANA", "LUCAS"}
PAUSA = 0.4                 # entre chamadas de organizar envio
ESPERA_RASTREIO = 120       # segundos esperando a Shopee gerar o rastreio
BATIMENTO_MORTO = 180       # trabalho sem sinal de vida há mais que isso = processo caiu
_get_client = [None]
_lock = threading.Lock()


# ------------------------------------------------------------------ banco

def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS etiquetas_trabalhos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,            -- rodando, concluido, erro
                lotes TEXT,                      -- json: rótulos pedidos
                limite INTEGER,
                total INTEGER DEFAULT 0,
                organizados TEXT DEFAULT '[]',   -- json: order_sn já organizados neste trabalho
                falhas TEXT DEFAULT '[]',        -- json: [[order_sn, motivo]]
                etapa TEXT,
                iniciado_por TEXT,
                criado_em TEXT,
                batimento TEXT,
                fim_em TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS etiquetas_arquivos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trabalho_id INTEGER,
                lote TEXT,
                nome TEXT,
                qtd INTEGER,
                conteudo TEXT,
                criado_em TEXT
            )
            """
        )


def _agora():
    return datetime.utcnow().isoformat()


def _upd(tid, **campos):
    campos["batimento"] = _agora()
    for k in ("organizados", "falhas", "lotes"):
        if k in campos and not isinstance(campos[k], str):
            campos[k] = json.dumps(campos[k], ensure_ascii=False)
    sets = ", ".join(f"{k} = ?" for k in campos)
    with models.get_conn() as conn:
        conn.execute(f"UPDATE etiquetas_trabalhos SET {sets} WHERE id = ?", (*campos.values(), tid))


def _trabalho_ativo():
    """Trabalho rodando com sinal de vida recente (vale entre processos do servidor)."""
    limite = (datetime.utcnow() - timedelta(seconds=BATIMENTO_MORTO)).isoformat()
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM etiquetas_trabalhos WHERE status = 'rodando' AND batimento >= ? ORDER BY id DESC LIMIT 1",
            (limite,),
        ).fetchone()
        # Rodando mas sem sinal de vida: o processo caiu no meio.
        conn.execute(
            "UPDATE etiquetas_trabalhos SET status = 'erro', etapa = 'Parou no meio (servidor reiniciou). "
            "Use “Gerar arquivo dos já organizados”.' WHERE status = 'rodando' AND batimento < ?",
            (limite,),
        )
        return dict(row) if row else None


# ------------------------------------------------------------------ Shopee

def _endereco_e_horario(client, order_sn):
    """Endereço de coleta da loja e o primeiro horário que a Shopee oferece para este pedido."""
    r = client._shop_request("GET", "/api/v2/logistics/get_shipping_parameter", params={"order_sn": order_sn})
    if r.get("error"):
        raise RuntimeError(r.get("message") or r.get("error"))
    resp = r.get("response") or {}
    precisa = (resp.get("info_needed") or {})
    if "pickup" not in precisa:
        raise RuntimeError("este pedido não é por coleta (%s)" % (", ".join(precisa) or "sem informação"))
    enderecos = (resp.get("pickup") or {}).get("address_list") or []
    if not enderecos:
        raise RuntimeError("a Shopee não ofereceu endereço de coleta")

    def peso(a):
        flags = a.get("address_flag") or []
        return (0 if "pickup_address" in flags else 1, 0 if "default_address" in flags else 1)

    end = sorted(enderecos, key=peso)[0]
    slots = end.get("time_slot_list") or []
    if not slots:
        raise RuntimeError("a Shopee não ofereceu horário de coleta")
    return end["address_id"], slots[0]["pickup_time_id"]


def _organizar(client, order_sn):
    end_id, horario = _endereco_e_horario(client, order_sn)
    r = client._shop_request(
        "POST", "/api/v2/logistics/ship_order",
        json_body={"order_sn": order_sn, "pickup": {"address_id": end_id, "pickup_time_id": horario}},
    )
    if r.get("error"):
        raise RuntimeError(r.get("message") or r.get("error"))


def _status(client, sns):
    """order_sn -> status atual na Shopee ('SEM_NF' se a nota fiscal ainda não chegou)."""
    out = {}
    for i in range(0, len(sns), 50):
        for od in lotes.detalhes(client, sns[i:i + 50]):
            st = od.get("order_status")
            out[od["order_sn"]] = "SEM_NF" if st == "READY_TO_SHIP" and not lotes.tem_nota(od) else st
    return out


def _esperar_rastreios(client, sns, tid):
    """Espera a Shopee gerar o rastreio dos pedidos recém-organizados."""
    faltam = list(sns)
    ini = time.time()
    while faltam and time.time() - ini < ESPERA_RASTREIO:
        novos = []
        for sn in faltam:
            try:
                r = client._shop_request("GET", "/api/v2/logistics/get_tracking_number", params={"order_sn": sn})
                tn = ((r.get("response") or {}).get("tracking_number") or "").strip()
            except Exception:
                tn = ""
            if not tn:
                novos.append(sn)
            time.sleep(0.2)
        faltam = novos
        _upd(tid, etapa=f"Aguardando a Shopee gerar o rastreio ({len(sns) - len(faltam)}/{len(sns)})")
        if faltam:
            time.sleep(4)
    return faltam


def _gerar_arquivo(client, tid, lote, sns, falhas):
    """Baixa o ZPL dos pedidos (na ordem dada) e guarda um .txt para este lote."""
    textos = []
    for i in range(0, len(sns), reimpressao.LOTE):
        _upd(tid, etapa=f"Baixando etiquetas de {lote} ({i}/{len(sns)})")
        t = reimpressao._lote(client, sns[i:i + reimpressao.LOTE], falhas)
        if t:
            textos.append(t)
    if not textos:
        return 0
    qtd = len(sns) - sum(1 for sn, _ in falhas if sn in set(sns))
    limpo = re.sub(r"[^A-Za-z0-9 ._-]", "", unicodedata.normalize("NFKD", lote).encode("ascii", "ignore").decode())
    nome = "ETIQUETAS %s %d %s.txt" % (re.sub(r"\s+", " ", limpo).strip(), qtd, datetime.now(BRT).strftime("%d-%m %H%M"))
    with models.get_conn() as conn:
        conn.execute(
            "INSERT INTO etiquetas_arquivos (trabalho_id, lote, nome, qtd, conteudo, criado_em) VALUES (?,?,?,?,?,?)",
            (tid, lote, nome, qtd, "\n".join(textos) + "\n", _agora()),
        )
    return qtd


def _rodar(tid, rotulos, limite):
    with _lock:
        falhas = []
        organizados = []
        try:
            client = _get_client[0]()
            if client is None:
                raise RuntimeError("Loja ainda não autorizada na Shopee.")
            _upd(tid, etapa="Conferindo os lotes na Shopee")
            snap = lotes.calcular(client)          # lista fresca, não a da tela
            pedidos = snap.get("pedidos") or {}
            plano = []                             # [(lote, [sns])] na ordem dos lotes marcados
            restante = limite or 10 ** 9
            for rot in rotulos:
                sns = (pedidos.get(rot) or [])[:restante]
                restante -= len(sns)
                if sns:
                    plano.append((rot, sns))
                if restante <= 0:
                    break
            total = sum(len(s) for _, s in plano)
            _upd(tid, total=total)
            if not total:
                raise RuntimeError(
                    "o lote %s mudou desde a última atualização da tela (pedidos já organizados, "
                    "cancelados ou o lote ficou pequeno e foi para DIVERSOS UNITÁRIOS). "
                    "Clique em Atualizar agora e escolha de novo." % ", ".join(rotulos))

            # Confere de novo o status logo antes de organizar (nunca organiza duas vezes).
            status = _status(client, [sn for _, s in plano for sn in s])
            for lote, sns in plano:
                feitos = []
                for n, sn in enumerate(sns, 1):
                    _upd(tid, etapa=f"Organizando envio — {lote} ({n}/{len(sns)})",
                         organizados=organizados, falhas=falhas)
                    if status.get(sn) == "SEM_NF":
                        falhas.append([sn, "aguardando nota fiscal — não organizado"])
                        continue
                    if status.get(sn) != "READY_TO_SHIP":
                        falhas.append([sn, f"não está mais 'A enviar' ({status.get(sn) or 'sem status'})"])
                        continue
                    try:
                        _organizar(client, sn)
                        organizados.append(sn)
                        feitos.append(sn)
                    except Exception as e:
                        falhas.append([sn, f"organizar envio: {e}"])
                    time.sleep(PAUSA)
                if not feitos:
                    continue
                sem_rastreio = _esperar_rastreios(client, feitos, tid)
                for sn in sem_rastreio:
                    falhas.append([sn, "a Shopee ainda não gerou o rastreio — use “Gerar arquivo dos já organizados” daqui a pouco"])
                prontos = [sn for sn in feitos if sn not in set(sem_rastreio)]
                f_lote = []
                if prontos:
                    _gerar_arquivo(client, tid, lote, prontos, f_lote)
                falhas.extend([list(x) for x in f_lote])
                _upd(tid, organizados=organizados, falhas=falhas)
            _upd(tid, status="concluido", etapa="Pronto", organizados=organizados, falhas=falhas, fim_em=_agora())
        except Exception as e:
            _upd(tid, status="erro", etapa=f"Parou: {e}", organizados=organizados, falhas=falhas, fim_em=_agora())
    # Atualiza os números da tela depois de organizar (fora da trava).
    try:
        lotes.atualizar()
    except Exception:
        pass


def _refazer_arquivo(tid):
    """Para trabalho que parou no meio: gera o .txt dos pedidos que já foram organizados."""
    with _lock:
        with models.get_conn() as conn:
            row = conn.execute("SELECT * FROM etiquetas_trabalhos WHERE id = ?", (tid,)).fetchone()
        if not row:
            return
        sns = json.loads(row["organizados"] or "[]")
        falhas = json.loads(row["falhas"] or "[]")
        _upd(tid, status="rodando", etapa="Gerando arquivo dos já organizados")
        try:
            client = _get_client[0]()
            pendentes = _esperar_rastreios(client, sns, tid)
            prontos = [sn for sn in sns if sn not in set(pendentes)]
            f = []
            if prontos:
                _gerar_arquivo(client, tid, "JÁ ORGANIZADOS", prontos, f)
            falhas = [x for x in falhas if x[0] not in set(sns)] + [list(x) for x in f] + \
                     [[sn, "ainda sem rastreio na Shopee"] for sn in pendentes]
            _upd(tid, status="concluido", etapa="Pronto", falhas=falhas, fim_em=_agora())
        except Exception as e:
            _upd(tid, status="erro", etapa=f"Parou: {e}", fim_em=_agora())


# ------------------------------------------------------------------ dados para a tela

def resumo_tela():
    """Trabalho atual/último e arquivos recentes, para a tela Etiquetas."""
    ativo = _trabalho_ativo()
    with models.get_conn() as conn:
        ultimo = conn.execute("SELECT * FROM etiquetas_trabalhos ORDER BY id DESC LIMIT 1").fetchone()
        arquivos = [dict(r) for r in conn.execute(
            "SELECT id, trabalho_id, lote, nome, qtd, criado_em FROM etiquetas_arquivos "
            "ORDER BY id DESC LIMIT 15"
        ).fetchall()]
    t = dict(ultimo) if ultimo else None
    if t:
        t["n_org"] = len(json.loads(t["organizados"] or "[]"))
        t["falhas_l"] = json.loads(t["falhas"] or "[]")
    for a in arquivos:
        try:
            a["quando"] = datetime.fromisoformat(a["criado_em"]).replace(tzinfo=timezone.utc).astimezone(BRT).strftime("%d/%m %H:%M")
        except (TypeError, ValueError):
            a["quando"] = ""
    return {"ativo": bool(ativo), "trabalho": t, "arquivos": arquivos,
            "pode": session.get("employee_name") in LIBERADOS}


# ------------------------------------------------------------------ rotas

@bp.route("/lotes/organizar", methods=["POST"])
def organizar():
    if session.get("employee_name") not in LIBERADOS:
        flash("Organizar envio é liberado só para JULIANA e LUCAS.")
        return redirect(url_for("lotes.painel"))
    rotulos = [r for r in request.form.getlist("lote") if r]
    if len(rotulos) != 1:
        flash("Escolha um lote por vez.")
        return redirect(url_for("lotes.painel"))
    try:
        limite = int(request.form.get("limite") or 0) or None
    except ValueError:
        limite = None
    if _trabalho_ativo() or _lock.locked():
        flash("Já tem um lote sendo organizado. Espere terminar.")
        return redirect(url_for("lotes.painel"))
    with models.get_conn() as conn:
        tid = conn.execute(
            "INSERT INTO etiquetas_trabalhos (status, lotes, limite, etapa, iniciado_por, criado_em, batimento) "
            "VALUES ('rodando', ?, ?, 'Começando', ?, ?, ?)",
            (json.dumps(rotulos, ensure_ascii=False), limite, session.get("employee_name"), _agora(), _agora()),
        ).lastrowid
    threading.Thread(target=_rodar, args=(tid, rotulos, limite), daemon=True).start()
    flash("Organizando o envio. A página atualiza sozinha; os arquivos aparecem embaixo quando ficarem prontos.")
    return redirect(url_for("lotes.painel"))


@bp.route("/lotes/trabalho/<int:tid>/refazer", methods=["POST"])
def refazer(tid):
    if session.get("employee_name") not in LIBERADOS:
        return redirect(url_for("lotes.painel"))
    if _trabalho_ativo() or _lock.locked():
        flash("Já tem um lote sendo organizado. Espere terminar.")
        return redirect(url_for("lotes.painel"))
    threading.Thread(target=_refazer_arquivo, args=(tid,), daemon=True).start()
    flash("Gerando o arquivo dos pedidos já organizados.")
    return redirect(url_for("lotes.painel"))


@bp.route("/lotes/arquivo/<int:aid>")
def baixar(aid):
    if not session.get("employee_name"):
        return redirect(url_for("login"))
    with models.get_conn() as conn:
        row = conn.execute("SELECT nome, conteudo FROM etiquetas_arquivos WHERE id = ?", (aid,)).fetchone()
    if not row:
        flash("Arquivo não encontrado.")
        return redirect(url_for("lotes.painel"))
    return Response(
        row["conteudo"],
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="%s"' % row["nome"].replace('"', "")},
    )


def init_app(app, get_client):
    _get_client[0] = get_client
    init_db()
    app.register_blueprint(bp)
