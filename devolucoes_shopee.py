"""Devoluções Shopee: todas as devoluções importadas direto da Shopee, sem bipar.

- Importa sozinho (a cada 20 min) as devoluções dos últimos 60 dias.
- Mostra o que precisa de resposta (proposta do comprador, pedido novo, prova pedida)
  com o prazo, e deixa responder daqui: aceitar, aceitar a proposta, fazer contraproposta
  e contestar com fotos. Cada ação pede confirmação e mostra a resposta da Shopee.
- Acompanha o envio de volta (rastreio reverso). Quando a transportadora marca como
  entregue e ninguém bipou o pacote na tela Devoluções em 2 dias, a devolução fica em
  alerta vermelho ("Entregue e não bipada").

Separado da bipagem: só LÊ a tabela `devolucoes` (para saber se o pacote foi bipado).
Tabelas próprias: dvs_devolucoes, dvs_acoes, dvs_meta. Acesso: JULIANA e LUCAS.
"""

import json
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, flash, jsonify, redirect, request, session, url_for

import models
from layout_v2 import ICONES, render_page

bp = Blueprint("devshopee", __name__)

BRT = timezone(timedelta(hours=-3))
LIBERADOS = {"JULIANA", "LUCAS"}
DIAS = 60                    # quanto tempo para trás importar
INTERVALO = 20 * 60          # importação automática
ALERTA_DIAS = 2              # entregue e não bipada há mais que isso = alerta
PAUSA = 0.25                 # entre chamadas à Shopee
MAX_RASTREIOS = 150          # consultas de rastreio por rodada
MAX_DETALHES = 120           # consultas de detalhe por rodada
ABERTOS = ("REQUESTED", "PROCESSING", "JUDGING", "SELLER_DISPUTE")
# A página de detalhe da Central do Vendedor usa um id interno que a API não devolve:
# o link abre a lista de devoluções e o número do pedido vai copiado para colar na busca.
SELLER_CENTER = "https://seller.shopee.com.br/portal/sale/returnrefundcancel"
IMG_BASE = "https://down-br.img.susercontent.com/file/"

_get_shopee = [None]
_lock = threading.Lock()

# ------------------------------------------------------------------ textos

STATUS_PT = {
    "REQUESTED": "Solicitada (aguardando resposta)",
    "PROCESSING": "Em andamento",
    "ACCEPTED": "Aceita — cliente reembolsado",
    "CANCELLED": "Cancelada",
    "JUDGING": "Em análise pela Shopee",
    "SELLER_DISPUTE": "Contestada — Shopee analisando",
    "CLOSED": "Encerrada",
}
MOTIVO_PT = {
    "NONE": "Sem motivo",
    "NOT_RECEIPT": "Não recebeu",
    "NON_RECEIPT": "Não recebeu",
    "WRONG_ITEM": "Produto errado",
    "ITEM_DAMAGED": "Chegou danificado",
    "DAMAGED": "Chegou danificado",
    "DAMAGED_OTHERS": "Danificado",
    "DIFF_DESC": "Diferente do anúncio",
    "DIFFERENT_DESCRIPTION": "Diferente do anúncio",
    "MUTUAL_AGREE": "Acordo com o vendedor",
    "OTHER": "Outro",
    "CHANGE_MIND": "Desistência (se arrependeu)",
    "ITEM_MISSING": "Faltando item",
    "MISSING_ITEM": "Faltando item",
    "EXPECTATION_FAILED": "Não atendeu a expectativa",
    "ITEM_FAKE": "Produto falsificado",
    "PHYSICAL_DMG": "Dano físico",
    "FUNCTIONAL_DMG": "Com defeito",
    "ITEM_WRONGDAMAGED": "Produto errado ou danificado",
    "WRONG_DAMAGED_ITEM": "Produto errado ou danificado",
    "USED": "Produto usado",
    "EMPTY_PARCEL": "Pacote vazio",
    "LATE_DELIVERY": "Entrega atrasada",
}
LOG_PT = {
    "": "Ainda não postado",
    "LOGISTICS_NOT_STARTED": "Envio de volta não iniciado",
    "LOGISTICS_PENDING_ARRANGE": "Aguardando o cliente postar",
    "LOGISTICS_READY": "Aguardando o cliente postar",
    "LOGISTICS_REQUEST_CREATED": "Aguardando o cliente postar",
    "LOGISTICS_PICKUP_DONE": "Postado — a caminho de volta",
    "LOGISTICS_PICKUP_RETRY": "Coleta remarcada",
    "LOGISTICS_PICKUP_FAILED": "Coleta falhou",
    "LOGISTICS_DELIVERY_DONE": "Entregue na empresa (pela transportadora)",
    "LOGISTICS_DELIVERY_FAILED": "Entrega de volta falhou",
    "LOGISTICS_REQUEST_CANCELED": "Envio de volta cancelado",
    "LOGISTICS_LOST": "Extraviado",
}
SOLUCAO_PT = {
    "RETURN_REFUND": "devolver o produto e receber reembolso",
    "REFUND": "reembolso sem devolver o produto",
    "REFUND_ONLY": "reembolso sem devolver o produto",
    "": "",
}
SOLUCAO_NUM_PT = {0: "Devolver o produto e reembolsar", 1: "Reembolsar sem devolução (cliente fica com o produto)"}
# Motivos de contestação que a Shopee oferece (o texto vem da própria Shopee, sem nome curto).
MOTIVO_CONTESTACAO = {
    82: "Produto devolvido danificado / com problema",
    83: "Pacote devolvido vazio ou com itens errados",
    84: "Produto devolvido é diferente do que foi enviado",
    86: "Outro motivo (enviar provas)",
    89: "Produto devolvido com sinais de uso",
}
TIPO_PT = {
    "RRAOC": "Devolução (cliente posta)",
    "RRBOC": "Devolução (coleta)",
}
PROVA_PEDIDA = ("PENDING", "REQUESTED", "PENDING_UPLOAD")
LOG_A_CAMINHO = ("LOGISTICS_PICKUP_DONE", "LOGISTICS_PICKUP_RETRY", "LOGISTICS_DELIVERY_FAILED")
LOG_FINAIS = ("LOGISTICS_DELIVERY_DONE", "LOGISTICS_REQUEST_CANCELED", "LOGISTICS_LOST")

# Situação de cada devolução na tela, na ordem dos filtros.
SITUACOES = [
    ("responder", "Responder agora", "bad"),
    ("sem_bipar", f"Entregue há +{ALERTA_DIAS} dias e não bipada", "bad"),
    ("bipar", "Entregue — falta bipar", "warn"),
    ("caminho", "A caminho de volta", "warn"),
    ("aguardando", "Aguardando o cliente postar", "neutro"),
    ("analise", "Em análise pela Shopee", "warn"),
    ("conferida", "Recebida e bipada", "ok"),
    ("encerrada", "Encerrada", "neutro"),
]
SIT_NOME = {k: n for k, n, _ in SITUACOES}
SIT_TOM = {k: t for k, _, t in SITUACOES}


# ------------------------------------------------------------------ banco

def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dvs_devolucoes (
                return_sn TEXT PRIMARY KEY,
                order_sn TEXT,
                status TEXT,
                neg_status TEXT,
                motivo TEXT,
                texto TEXT,
                valor REAL,
                valor_orig REAL,
                criado INTEGER,
                atualizado_shopee INTEGER,
                prazo INTEGER,
                prazo_vendedor INTEGER,
                prazo_envio INTEGER,
                prova_status TEXT,
                prova_prazo INTEGER,
                comp_status TEXT,
                precisa_envio INTEGER,
                tipo TEXT,
                rastreio TEXT,
                comprador TEXT,
                itens TEXT,
                fotos TEXT,
                videos TEXT,
                oferta TEXT,
                log_status TEXT,
                log_atualizado INTEGER,
                log_hist TEXT,
                entregue_em INTEGER,
                log_consultado INTEGER,
                det_consultado INTEGER,
                det_versao INTEGER,
                visto_em TEXT,
                mudou_em TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dvs_order ON dvs_devolucoes(order_sn)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dvs_rastreio ON dvs_devolucoes(rastreio)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dvs_acoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_sn TEXT NOT NULL,
                acao TEXT NOT NULL,
                dados TEXT,
                resultado TEXT,
                ok INTEGER NOT NULL DEFAULT 0,
                quem TEXT,
                em TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dvs_acoes_sn ON dvs_acoes(return_sn)")
        conn.execute("CREATE TABLE IF NOT EXISTS dvs_meta (chave TEXT PRIMARY KEY, valor TEXT)")


def _meta(chave, valor=None):
    with models.get_conn() as conn:
        if valor is None:
            r = conn.execute("SELECT valor FROM dvs_meta WHERE chave = ?", (chave,)).fetchone()
            return r["valor"] if r else None
        conn.execute(
            "INSERT INTO dvs_meta (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (chave, str(valor)),
        )


def _agora_iso():
    return datetime.now(BRT).isoformat(timespec="seconds")


# ------------------------------------------------------------------ Shopee

class ErroShopee(Exception):
    pass


def _cliente():
    client = _get_shopee[0]() if _get_shopee[0] else None
    if client is None:
        raise ErroShopee("Loja não conectada à Shopee.")
    return client


def _resposta(r):
    if r.get("error"):
        raise ErroShopee(r.get("message") or r.get("error"))
    return r.get("response") or {}


def _get(client, caminho, **params):
    time.sleep(PAUSA)
    try:
        return _resposta(client._shop_request("GET", caminho, params=params))
    except requests.HTTPError as e:
        raise ErroShopee(_texto_http(e))


def _post(client, caminho, corpo):
    try:
        return _resposta(client._shop_request("POST", caminho, json_body=corpo))
    except requests.HTTPError as e:
        raise ErroShopee(_texto_http(e))


def _texto_http(e):
    try:
        j = e.response.json()
        return j.get("message") or j.get("error") or e.response.text[:200]
    except Exception:
        return f"HTTP {getattr(e.response, 'status_code', '?')}"


def _enviar_imagem(client, nome, dados, tipo):
    """Sobe uma foto para a Shopee (convert_image) e devolve a URL que a contestação usa."""
    caminho = "/api/v2/returns/convert_image"
    ts = int(time.time())
    q = {
        "partner_id": client.partner_id,
        "timestamp": ts,
        "sign": client._sign(caminho, ts, f"{client.access_token}{client.shop_id}"),
        "shop_id": client.shop_id,
        "access_token": client.access_token,
    }
    r = client.session.post(
        client.host + caminho, params=q,
        files={"upload_image": (nome or "foto.jpg", dados, tipo or "image/jpeg")}, timeout=60,
    )
    try:
        j = r.json()
    except ValueError:
        raise ErroShopee(f"Envio da foto falhou (HTTP {r.status_code}).")
    resp = _resposta(j)
    url = resp.get("url")
    if not url:
        raise ErroShopee("A Shopee não devolveu o endereço da foto.")
    return url


# ------------------------------------------------------------------ importação

def _img(v):
    if not v:
        return ""
    v = str(v)
    return v if v.startswith("http") else IMG_BASE + v


def _itens(lista):
    out = []
    for it in lista or []:
        imgs = it.get("images") or it.get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        out.append({
            "nome": it.get("name") or "",
            "sku": it.get("variation_sku") or it.get("item_sku") or "",
            "qtd": it.get("amount") or 1,
            "preco": it.get("item_price") or 0,
            "img": _img(imgs[0]) if imgs else "",
            "item_id": it.get("item_id"),
        })
    return out


def _campos(d):
    """Campos da tabela a partir de um registro da lista OU do detalhe da Shopee."""
    neg = d.get("negotiation") or {}
    neg_status = d.get("negotiation_status") or neg.get("negotiation_status") or ""
    user = d.get("user") or {}
    proof = d.get("seller_proof") or {}
    comp = d.get("seller_compensation") or {}
    videos = []
    for v in d.get("buyer_videos") or []:
        if isinstance(v, dict):
            videos.append({"thumb": v.get("thumbnail_url") or "", "url": v.get("video_url") or ""})
    c = {
        "order_sn": d.get("order_sn"),
        "status": d.get("status") or "",
        "neg_status": neg_status,
        "motivo": d.get("reason") or "",
        "texto": d.get("text_reason") or "",
        "valor": float(d.get("refund_amount") or 0),
        "valor_orig": float(d.get("amount_before_discount") or 0),
        "criado": int(d.get("create_time") or 0),
        "atualizado_shopee": int(d.get("update_time") or 0),
        "prazo": int(d.get("due_date") or 0) or None,
        "prazo_vendedor": int(d.get("return_seller_due_date") or 0) or None,
        "prazo_envio": int(d.get("return_ship_due_date") or 0) or None,
        "prova_status": d.get("seller_proof_status") or proof.get("seller_proof_status") or "",
        "prova_prazo": int(d.get("seller_evidence_deadline") or proof.get("seller_evidence_deadline") or 0) or None,
        "comp_status": d.get("seller_compensation_status") or comp.get("seller_compensation_status") or "",
        "precisa_envio": 1 if d.get("needs_logistics") else 0,
        "tipo": d.get("return_refund_type") or "",
        "comprador": user.get("username") or "",
        "itens": json.dumps(_itens(d.get("item")), ensure_ascii=False),
        "fotos": json.dumps([_img(x) for x in (d.get("image") or []) if x]),
        "videos": json.dumps(videos),
    }
    if neg.get("latest_offer_amount") is not None or neg.get("latest_solution"):
        c["oferta"] = json.dumps({
            "solucao": neg.get("latest_solution") or "",
            "valor": neg.get("latest_offer_amount"),
            "criador": neg.get("latest_offer_creator") or "",
            "limite": neg.get("counter_limit"),
            "prazo": neg.get("offer_due_date"),
        }, ensure_ascii=False)
    if (d.get("tracking_number") or "").strip():
        c["rastreio"] = d["tracking_number"].strip().upper()
    if (d.get("reverse_logistics_status") or d.get("logistics_status")):
        c["log_status"] = d.get("reverse_logistics_status") or d.get("logistics_status")
    return c


def _gravar(conn, return_sn, c, atual):
    agora = _agora_iso()
    if atual is None:
        cols = ["return_sn"] + list(c) + ["visto_em", "mudou_em"]
        vals = [return_sn] + list(c.values()) + [agora, agora]
        conn.execute(
            f"INSERT INTO dvs_devolucoes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
        )
        return
    mudou = any((atual[k] or "") != (c.get(k) or "") for k in ("status", "neg_status", "prova_status", "comp_status") if k in c)
    sets = ", ".join(f"{k} = ?" for k in c)
    vals = list(c.values())
    if mudou:
        sets += ", mudou_em = ?"
        vals.append(agora)
    conn.execute(f"UPDATE dvs_devolucoes SET {sets} WHERE return_sn = ?", vals + [return_sn])


def _salvar(registros):
    with models.get_conn() as conn:
        for d in registros:
            sn = str(d.get("return_sn") or "")
            if not sn:
                continue
            atual = conn.execute("SELECT * FROM dvs_devolucoes WHERE return_sn = ?", (sn,)).fetchone()
            _gravar(conn, sn, _campos(d), atual)


def _baixar_lista(client):
    agora = int(time.time())
    out = []
    for w in range(DIAS // 15):
        fim = agora - w * 15 * 86400
        ini = fim - 15 * 86400
        for pg in range(60):
            r = _get(client, "/api/v2/returns/get_return_list", page_no=pg, page_size=100,
                     create_time_from=ini, create_time_to=fim)
            out += r.get("return") or []
            if not r.get("more"):
                break
    return out


def _consultar_rastreio(client, sn):
    r = _get(client, "/api/v2/returns/get_reverse_tracking_info", return_sn=sn)
    st = r.get("reverse_logistics_status") or ""
    hist = [
        {"t": int(x.get("update_time") or 0), "d": x.get("tracking_description") or ""}
        for x in (r.get("tracking_info") or [])
    ][:8]
    c = {
        "log_status": st,
        "log_atualizado": int(r.get("reverse_logistics_update_time") or 0) or None,
        "log_hist": json.dumps(hist, ensure_ascii=False),
        "log_consultado": int(time.time()),
    }
    if r.get("tracking_number"):
        c["rastreio"] = r["tracking_number"].strip().upper()
    if st == "LOGISTICS_DELIVERY_DONE":
        c["entregue_em"] = c["log_atualizado"] or (hist[0]["t"] if hist else int(time.time()))
    return c


def _atualizar_campos(sn, c):
    with models.get_conn() as conn:
        sets = ", ".join(f"{k} = ?" for k in c)
        conn.execute(f"UPDATE dvs_devolucoes SET {sets} WHERE return_sn = ?", list(c.values()) + [sn])


def _precisa_rastreio(r, agora):
    """Quais devoluções consultar no rastreio reverso nesta rodada (e em que ordem)."""
    if not r["precisa_envio"] or r["status"] == "CANCELLED" or r["entregue_em"]:
        return None
    if r["log_status"] in LOG_FINAIS:
        return None
    ult = r["log_consultado"] or 0
    aberto = r["status"] in ABERTOS
    a_caminho = r["log_status"] in LOG_A_CAMINHO
    velho = r["criado"] and agora - r["criado"] > 45 * 86400
    if velho and not a_caminho and ult:
        return None
    espera = 3600 if (aberto or a_caminho) else 6 * 3600
    if agora - ult < espera:
        return None
    return (0 if a_caminho else 1 if aberto else 2, -(r["criado"] or 0))


def _precisa_detalhe(r):
    if r["status"] not in ABERTOS:
        return False
    if r["det_versao"] != r["atualizado_shopee"]:
        return True
    return r["neg_status"] == "PENDING_RESPOND" and time.time() - (r["det_consultado"] or 0) > 2 * 3600


def sincronizar():
    """Importa a lista da Shopee e atualiza rastreios e detalhes. Devolve um resumo."""
    if not _lock.acquire(False):
        return None
    try:
        client = _cliente()
        lista = _baixar_lista(client)
        _salvar(lista)
        agora = int(time.time())
        with models.get_conn() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM dvs_devolucoes").fetchall()]
        detalhes = [r["return_sn"] for r in rows if _precisa_detalhe(r)][:MAX_DETALHES]
        for sn in detalhes:
            try:
                d = _get(client, "/api/v2/returns/get_return_detail", return_sn=sn)
                c = _campos(d)
                c["det_consultado"] = int(time.time())
                c["det_versao"] = c["atualizado_shopee"]
                with models.get_conn() as conn:
                    atual = conn.execute("SELECT * FROM dvs_devolucoes WHERE return_sn = ?", (sn,)).fetchone()
                    _gravar(conn, sn, c, atual)
            except ErroShopee:
                continue
        fila = sorted(
            ((p, r["return_sn"]) for r in rows for p in [_precisa_rastreio(r, agora)] if p is not None)
        )[:MAX_RASTREIOS]
        n_rast = 0
        for _, sn in fila:
            try:
                _atualizar_campos(sn, _consultar_rastreio(client, sn))
                n_rast += 1
            except ErroShopee:
                _atualizar_campos(sn, {"log_consultado": int(time.time())})
        _meta("atualizado", _agora_iso())
        _meta("erro", "")
        return f"{len(lista)} devoluções lidas, {len(detalhes)} detalhes e {n_rast} rastreios atualizados."
    except Exception as e:  # noqa: BLE001 - mostra na tela, não derruba o loop
        _meta("erro", str(e)[:300])
        raise
    finally:
        _lock.release()


def atualizar_uma(sn, client=None):
    """Reconsulta uma devolução (depois de uma ação, por exemplo)."""
    client = client or _cliente()
    d = _get(client, "/api/v2/returns/get_return_detail", return_sn=sn)
    c = _campos(d)
    c["det_consultado"] = int(time.time())
    c["det_versao"] = c["atualizado_shopee"]
    with models.get_conn() as conn:
        atual = conn.execute("SELECT * FROM dvs_devolucoes WHERE return_sn = ?", (sn,)).fetchone()
        _gravar(conn, sn, c, atual)
    if c.get("precisa_envio"):
        try:
            _atualizar_campos(sn, _consultar_rastreio(client, sn))
        except ErroShopee:
            pass


def _loop():
    time.sleep(240)   # deixa o servidor subir antes
    while True:
        try:
            sincronizar()
        except Exception:
            pass
        time.sleep(INTERVALO)


# ------------------------------------------------------------------ montagem da tela

def _bipagens():
    """Pacotes bipados na tela Devoluções: por pedido e por rastreio."""
    por_pedido, por_rastreio = {}, {}
    inicio = None
    try:
        with models.get_conn() as conn:
            for r in conn.execute(
                "SELECT order_sn, rastreio, bipado_por, created_at FROM devolucoes ORDER BY id"
            ).fetchall():
                info = {"por": r["bipado_por"] or "", "em": r["created_at"] or ""}
                if r["created_at"] and (inicio is None or r["created_at"] < inicio):
                    inicio = r["created_at"]
                if r["order_sn"]:
                    por_pedido.setdefault(r["order_sn"], info)
                if r["rastreio"]:
                    por_rastreio.setdefault(r["rastreio"].strip().upper(), info)
    except Exception:
        pass
    # Pacotes entregues antes de a bipagem existir não viram alerta.
    ini_ts = 0
    if inicio:
        try:
            dt = datetime.fromisoformat(inicio)
            ini_ts = int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
        except ValueError:
            ini_ts = 0
    return por_pedido, por_rastreio, ini_ts


def _data(ts, hora=True):
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), BRT).strftime("%d/%m %H:%M" if hora else "%d/%m")


def _iso_data(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BRT).strftime("%d/%m %H:%M")
    except ValueError:
        return iso


def _falta(ts, agora):
    """Texto curto do tempo até o prazo."""
    s = int(ts) - agora
    if s <= 0:
        return "vencido"
    h = s // 3600
    if h < 1:
        return f"{max(1, s // 60)} min"
    if h < 48:
        return f"{h} h"
    return f"{h // 24} dias"


def _brl(v):
    s = f"{float(v or 0):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def situacao(r, bip, agora, ini_bip=0):
    st = r["status"]
    if st in ("REQUESTED", "PROCESSING") and (
        st == "REQUESTED" or r["neg_status"] == "PENDING_RESPOND"
        or r["prova_status"] in PROVA_PEDIDA or r["comp_status"] == "PENDING_REQUEST"
    ):
        return "responder"
    if r["prova_status"] in PROVA_PEDIDA or r["comp_status"] == "PENDING_REQUEST":
        return "responder"
    if r["entregue_em"] and not bip and st != "CANCELLED" and int(r["entregue_em"]) >= ini_bip \
            and agora - int(r["entregue_em"]) < 45 * 86400:
        return "sem_bipar" if agora - int(r["entregue_em"]) > ALERTA_DIAS * 86400 else "bipar"
    if st in ("JUDGING", "SELLER_DISPUTE"):
        return "analise"
    if st == "PROCESSING" and (r["entregue_em"] or bip):
        # pacote voltou: a Shopee espera a validação (aceitar ou contestar) até o prazo
        return "responder"
    if bip:
        return "conferida"
    if r["entregue_em"]:
        return "bipar" if st in ABERTOS else "encerrada"
    if r["precisa_envio"] and st != "CANCELLED":
        if r["log_status"] in LOG_A_CAMINHO:
            return "caminho"
        if st in ABERTOS and r["log_status"] not in LOG_FINAIS:
            return "aguardando"
    if st in ABERTOS:
        return "aguardando"
    return "encerrada"


def _montar(r, bips, agora):
    por_pedido, por_rastreio, ini_bip = bips
    bip = por_pedido.get(r["order_sn"]) or (por_rastreio.get(r["rastreio"]) if r["rastreio"] else None)
    r["bip"] = bip
    if bip:
        r["bip_txt"] = f"Bipada em {_iso_data(bip['em'])}" + (f" por {bip['por']}" if bip["por"] else "")
    r["sit"] = situacao(r, bip, agora, ini_bip)
    r["sit_nome"] = SIT_NOME[r["sit"]]
    r["tom"] = SIT_TOM[r["sit"]]
    r["status_pt"] = STATUS_PT.get(r["status"], r["status"] or "—")
    r["motivo_pt"] = MOTIVO_PT.get(r["motivo"], (r["motivo"] or "").replace("_", " ").capitalize() or "—")
    r["log_pt"] = LOG_PT.get(r["log_status"] or "", (r["log_status"] or "").replace("LOGISTICS_", "").replace("_", " ").lower())
    r["tipo_pt"] = TIPO_PT.get(r["tipo"] or "", "") if r["precisa_envio"] else "Só reembolso (sem devolver)"
    for k in ("itens", "fotos", "videos", "log_hist"):
        try:
            r[k] = json.loads(r[k] or "[]")
        except ValueError:
            r[k] = []
    try:
        r["oferta"] = json.loads(r["oferta"]) if r["oferta"] else None
    except ValueError:
        r["oferta"] = None
    of = r["oferta"]
    # PENDING_RESPOND = a última proposta é do comprador e espera a resposta da loja.
    r["proposta_comprador"] = bool(r["neg_status"] == "PENDING_RESPOND" and of and of.get("valor") is not None)
    for h in r["log_hist"]:
        h["txt"] = _data(h.get("t")) if h.get("t") else ""
    if of:
        of["solucao_pt"] = SOLUCAO_PT.get(of.get("solucao") or "", (of.get("solucao") or "").lower())
    # prazo mais urgente para o vendedor
    prazos = []
    if r["sit"] in ("responder", "sem_bipar", "bipar"):
        for ts in (r["prova_prazo"] if r["prova_status"] in PROVA_PEDIDA else None, r["prazo_vendedor"], r["prazo"]):
            if ts:
                prazos.append(int(ts))
        if of and of.get("prazo"):
            prazos.append(int(of["prazo"]))
    prazo = min([p for p in prazos if p > 0], default=None)
    r["prazo_ts"] = prazo
    r["prazo_txt"] = (_data(prazo) + " (falta " + _falta(prazo, agora) + ")") if prazo else ""
    r["prazo_perto"] = bool(prazo and prazo - agora < 24 * 3600)
    r["criado_txt"] = _data(r["criado"])
    r["entregue_txt"] = _data(r["entregue_em"]) if r["entregue_em"] else ""
    if r["entregue_em"]:
        seg = agora - int(r["entregue_em"])
        r["ha_entregue"] = f"{seg // 86400} dia(s)" if seg >= 86400 else f"{max(1, seg // 3600)} h"
    r["aberta"] = r["status"] in ABERTOS
    r["pode_aceitar"] = r["status"] in ("REQUESTED", "PROCESSING")
    r["pode_contestar"] = r["status"] in ("REQUESTED", "PROCESSING")
    r["pode_ofertar"] = r["status"] in ("REQUESTED", "PROCESSING") and r["neg_status"] in ("PENDING_RESPOND", "")
    r["link"] = SELLER_CENTER
    r["valor_txt"] = _brl(r["valor"])
    return r


def dados(filtro=None, busca=""):
    agora = int(time.time())
    limite = agora - DIAS * 86400
    with models.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM dvs_devolucoes WHERE criado >= ? OR status IN ('REQUESTED','PROCESSING','JUDGING','SELLER_DISPUTE') "
            "ORDER BY criado DESC", (limite,),
        ).fetchall()]
        acoes = {}
        for a in conn.execute("SELECT * FROM dvs_acoes ORDER BY id DESC LIMIT 400").fetchall():
            acoes.setdefault(a["return_sn"], []).append(dict(a))
    bips = _bipagens()
    rows = [_montar(r, bips, agora) for r in rows]
    for r in rows:
        r["acoes"] = [
            {**a, "em_txt": _iso_data(a["em"])} for a in acoes.get(r["return_sn"], [])[:5]
        ]
    contagem = Counter(r["sit"] for r in rows)
    validas = [r for r in rows if r["status"] != "CANCELLED"]
    abertas = [r for r in rows if r["aberta"]]
    resumo = {
        "responder": contagem["responder"],
        "sem_bipar": contagem["sem_bipar"],
        "bipar": contagem["bipar"],
        "caminho": contagem["caminho"],
        "analise": contagem["analise"],
        "abertas": len(abertas),
        "valor_aberto": sum(r["valor"] for r in abertas),
        "total": len(validas),
        "valor_total": sum(r["valor"] for r in validas if r["status"] == "ACCEPTED"),
        "canceladas": len(rows) - len(validas),
    }
    motivos = Counter(r["motivo_pt"] for r in validas).most_common(8)
    skus = Counter()
    sku_info = {}
    for r in validas:
        for it in r["itens"]:
            chave = it["sku"] or it["nome"][:40]
            skus[chave] += int(it.get("qtd") or 1)
            sku_info.setdefault(chave, it)
    top_skus = [(k, n, sku_info[k]) for k, n in skus.most_common(8)]

    if busca:
        b = busca.strip().upper()
        lista = [
            r for r in rows
            if b in (r["order_sn"] or "").upper() or b in r["return_sn"].upper()
            or b in (r["rastreio"] or "") or b in (r["comprador"] or "").upper()
            or any(b in (it["sku"] or "").upper() or b in (it["nome"] or "").upper() for it in r["itens"])
        ]
    elif filtro == "todas":
        lista = rows
    elif filtro == "atencao" or not filtro:
        lista = [r for r in rows if r["sit"] in ("responder", "sem_bipar", "bipar")]
    elif filtro in SIT_NOME:
        lista = [r for r in rows if r["sit"] == filtro]
    else:
        lista = rows
    ordem = {k: i for i, (k, _, _) in enumerate(SITUACOES)}
    lista.sort(key=lambda r: (ordem[r["sit"]], r["prazo_ts"] or 9e12, -(r["entregue_em"] or 0), -(r["criado"] or 0)))
    return {
        "lista": lista[:300], "n_lista": len(lista), "contagem": contagem, "resumo": resumo,
        "motivos": motivos, "top_skus": top_skus, "n_validas": len(validas),
    }


# ------------------------------------------------------------------ ações na Shopee

def _registrar(sn, acao, dados_acao, resultado, ok):
    with models.get_conn() as conn:
        conn.execute(
            "INSERT INTO dvs_acoes (return_sn, acao, dados, resultado, ok, quem, em) VALUES (?,?,?,?,?,?,?)",
            (sn, acao, json.dumps(dados_acao, ensure_ascii=False), str(resultado)[:500], 1 if ok else 0,
             session.get("employee_name") or "", _agora_iso()),
        )


def _existe(sn):
    with models.get_conn() as conn:
        return conn.execute("SELECT * FROM dvs_devolucoes WHERE return_sn = ?", (sn,)).fetchone()


ACOES_PT = {
    "confirm": "Aceitar devolução / reembolso",
    "accept_offer": "Aceitar a proposta do comprador",
    "offer": "Enviar contraproposta",
    "dispute": "Contestar",
}


def executar(sn, acao, form, arquivos):
    """Executa a ação na Shopee. Devolve (ok, mensagem)."""
    client = _cliente()
    if acao == "confirm":
        corpo = {"return_sn": sn}
    elif acao == "accept_offer":
        corpo = {"return_sn": sn}
    elif acao == "offer":
        try:
            solucao = int(form.get("solucao"))
            txt = (form.get("valor") or "").replace("R$", "").strip()
            if "," in txt:
                txt = txt.replace(".", "").replace(",", ".")
            valor = round(float(txt), 2)
        except (TypeError, ValueError):
            return False, "Escolha a solução e digite o valor (ex.: 25,90)."
        if valor < 0:
            return False, "Valor inválido."
        corpo = {"return_sn": sn, "proposal_solution": solucao, "proposal_adjusted_refund_amount": valor}
    elif acao == "dispute":
        email = (form.get("email") or "").strip()
        texto = (form.get("texto") or "").strip()
        try:
            motivo = int(form.get("motivo"))
        except (TypeError, ValueError):
            return False, "Escolha o motivo da contestação."
        if "@" not in email:
            return False, "Informe o e-mail para a Shopee responder sobre a contestação."
        if len(texto) < 10:
            return False, "Explique a contestação (pelo menos uma frase)."
        fotos = [f for f in arquivos if f and f.filename]
        if not fotos:
            return False, "Anexe pelo menos uma foto como prova (a Shopee exige)."
        if len(fotos) > 10:
            return False, "No máximo 10 fotos."
        urls = []
        for f in fotos:
            dados_f = f.read()
            if len(dados_f) > 10 * 1024 * 1024:
                return False, f"A foto {f.filename} passa de 10 MB."
            urls.append(_enviar_imagem(client, f.filename, dados_f, f.mimetype))
        _meta("email", email)
        corpo = {"return_sn": sn, "email": email, "dispute_reason": motivo,
                 "dispute_text_reason": texto, "images": urls}
    else:
        return False, "Ação desconhecida."
    try:
        resp = _post(client, "/api/v2/returns/" + acao, corpo)
        ok, msg = True, "A Shopee confirmou: " + ACOES_PT[acao].lower() + "."
    except ErroShopee as e:
        resp, ok, msg = str(e), False, "A Shopee recusou: " + str(e)
    registro = {k: v for k, v in corpo.items() if k != "return_sn"}
    _registrar(sn, acao, registro, json.dumps(resp, ensure_ascii=False) if ok else resp, ok)
    try:
        atualizar_uma(sn, client)
    except Exception:
        pass
    return ok, msg


# ------------------------------------------------------------------ rotas

def _liberado():
    if not session.get("employee_name"):
        return redirect(url_for("login"))
    if session.get("employee_name") not in LIBERADOS:
        flash("A tela Devoluções Shopee é liberada só para JULIANA e LUCAS.")
        return redirect(url_for("dashboard"))
    return None


@bp.route("/devolucoes/shopee")
def painel():
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    filtro = request.args.get("f") or ""
    busca = (request.args.get("q") or "").strip()
    d = dados(filtro, busca)
    if not filtro and not busca and not d["lista"]:
        filtro = "todas"
        d = dados(filtro)
    return render_page(
        "Devoluções Shopee", "devshopee", TEMPLATE, d=d, ic=ICONES, filtro=filtro or "atencao", busca=busca,
        situacoes=SITUACOES, brl=_brl, atualizado=_iso_data(_meta("atualizado")), erro=_meta("erro") or "",
        rodando=_lock.locked(), email=_meta("email") or "", alerta_dias=ALERTA_DIAS,
        solucao_pt=SOLUCAO_NUM_PT, acoes_pt=ACOES_PT,
    )


@bp.route("/devolucoes/shopee/atualizar", methods=["POST"])
def atualizar():
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    if _lock.locked():
        flash("Já está importando. Recarregue a página em 1 ou 2 minutos.")
    else:
        def rodar():
            try:
                sincronizar()
            except Exception:
                pass
        threading.Thread(target=rodar, daemon=True).start()
        flash("Importando da Shopee em segundo plano. Recarregue a página em 1 ou 2 minutos.")
    return redirect(request.referrer or url_for("devshopee.painel"))


@bp.route("/devolucoes/shopee/<sn>/opcoes")
def opcoes(sn):
    """Soluções para contraproposta e motivos de contestação (consulta na hora)."""
    bloqueio = _liberado()
    if bloqueio:
        return jsonify({"erro": "sem acesso"}), 403
    if not _existe(sn):
        return jsonify({"erro": "Devolução não encontrada."}), 404
    tipo = request.args.get("tipo")
    try:
        client = _cliente()
        if tipo == "oferta":
            r = _get(client, "/api/v2/returns/get_available_solutions", return_sn=sn)
            sol = []
            for s in r.get("solution") or r.get("solutions") or []:
                n = s.get("solution")
                sol.append({"id": n, "nome": SOLUCAO_NUM_PT.get(n, f"Solução {n}"),
                            "max": s.get("max_refund_amount")})
            return jsonify({"solucoes": sol})
        r = _get(client, "/api/v2/returns/get_return_dispute_reason", return_sn=sn)
        motivos = []
        for m in r.get("dispute_reason_list") or []:
            reqs = [e.get("requirement") for e in (m.get("evidence_module_list") or []) if e.get("requirement")]
            if (m.get("dispute_requirement") or "").strip().lower() == "live test":
                continue
            motivos.append({
                "id": m.get("dispute_reason"),
                "nome": MOTIVO_CONTESTACAO.get(m.get("dispute_reason")) or (reqs[0][:120] if reqs else f"Motivo {m.get('dispute_reason')}"),
                "detalhe": m.get("dispute_requirement") or "",
                "provas": reqs,
            })
        return jsonify({"motivos": motivos})
    except ErroShopee as e:
        return jsonify({"erro": str(e)})


@bp.route("/devolucoes/shopee/<sn>/acao", methods=["POST"])
def acao(sn):
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    volta = request.form.get("volta") or url_for("devshopee.painel")
    if not volta.startswith("/"):
        volta = url_for("devshopee.painel")
    r = _existe(sn)
    if not r:
        flash("Devolução não encontrada.")
        return redirect(volta)
    if request.form.get("confirmado") != "1":
        flash("Ação não confirmada.")
        return redirect(volta)
    a = request.form.get("acao") or ""
    try:
        ok, msg = executar(sn, a, request.form, request.files.getlist("fotos"))
    except ErroShopee as e:
        ok, msg = False, "Erro: " + str(e)
        _registrar(sn, a, {}, str(e), False)
    flash(("✔ " if ok else "✖ ") + f"Pedido {r['order_sn']}: {msg}")
    return redirect(volta + "#d" + sn)


@bp.route("/devolucoes/shopee/<sn>/reconsultar", methods=["POST"])
def reconsultar(sn):
    bloqueio = _liberado()
    if bloqueio:
        return bloqueio
    try:
        atualizar_uma(sn)
        flash("Devolução atualizada com a Shopee.")
    except ErroShopee as e:
        flash("Erro ao consultar a Shopee: " + str(e))
    return redirect(request.form.get("volta") or url_for("devshopee.painel"))


def resumo_alertas():
    """Contagem curta para avisos em outras telas (Devoluções)."""
    try:
        d = dados("atencao")
        return {"responder": d["resumo"]["responder"], "sem_bipar": d["resumo"]["sem_bipar"]}
    except Exception:
        return None


# ------------------------------------------------------------------ template

TEMPLATE = """
<style>
.ds-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 14px}
.ds-tabs a{padding:7px 13px;border-radius:999px;background:#fff;border:1px solid #d5d9e1;color:#1f2633;text-decoration:none;font-size:13px}
.ds-tabs a.on{background:#1f2633;color:#fff;border-color:#1f2633}
.ds-tabs a.al{border-color:#d93025;color:#9b1c1c;font-weight:700}
.ds-tabs a.al.on{background:#9b1c1c;color:#fff}
.ds-tabs b{margin-left:6px}
.ds-cards{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:16px}
.ds-card{background:#fff;border-radius:14px;padding:12px 14px;border:2px solid transparent;text-decoration:none;color:inherit;display:block}
.ds-card .l{font-size:12px;color:#4a5262;font-weight:600}
.ds-card .n{font-size:26px;font-weight:800;line-height:1.15}
.ds-card .s{font-size:11px;color:#5b6474}
.ds-card.r{border-color:#d93025}.ds-card.r .n{color:#b3261e}
.ds-card.y{border-color:#e0b800}
.ds-row{border-top:1px solid #e3e6ec;padding:14px 0;display:grid;grid-template-columns:72px 1fr 250px;gap:14px;align-items:start}
.ds-row:first-child{border-top:0}
.ds-row.alerta{background:#fff5f5;margin:0 -18px;padding:14px 18px;border-left:4px solid #d93025}
.ds-foto{width:72px;height:72px;border-radius:10px;object-fit:cover;background:#eceff3;display:block}
.ds-tag{display:inline-block;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
.ds-tag.ok{background:#d9f3e6;color:#0d6b3f}.ds-tag.warn{background:#fff1cc;color:#7a5300}
.ds-tag.bad{background:#fde0e0;color:#9b1c1c}.ds-tag.neutro{background:#eceff3;color:#4a5262}
.ds-mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.ds-sub{font-size:12px;color:#5b6474;margin-top:3px}
.ds-q{font-size:13px;margin-top:6px;padding:6px 10px;background:#f6f7f9;border-radius:8px}
.ds-of{font-size:13px;margin-top:6px;padding:8px 10px;background:#fff8dc;border:1px solid #f1d36b;border-radius:8px}
.ds-prazo{color:#b3261e;font-weight:700}
.ds-mini{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.ds-mini img{width:44px;height:44px;border-radius:6px;object-fit:cover;border:1px solid #d5d9e1}
.ds-acts{display:flex;flex-direction:column;gap:6px}
.ds-acts .v2-btn{justify-content:center;text-decoration:none}
.v2-h .v2-btn{text-decoration:none}
.ds-btn-r{background:#fde0e0!important;color:#9b1c1c!important;border:1px solid #f3b4b4!important}
.ds-log{font-size:12px;color:#4a5262;margin-top:4px}
.ds-log li{margin:2px 0}
.ds-hist{font-size:11px;color:#5b6474;margin:4px 0 0;padding-left:16px}
.ds-grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
.ds-tb{width:100%;border-collapse:collapse;font-size:13px}
.ds-tb td{padding:5px 4px;border-top:1px solid #eef0f3;vertical-align:middle}
.ds-tb img{width:30px;height:30px;border-radius:5px;object-fit:cover;vertical-align:middle;margin-right:6px}
.ds-busca{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.ds-busca input{flex:1;min-width:220px;font:inherit;font-size:14px;padding:9px 12px;border:1px solid #c7ccd6;border-radius:10px;background:#fff}
dialog.ds-dlg{border:0;border-radius:16px;padding:20px 22px;max-width:560px;width:calc(100% - 32px);box-shadow:0 20px 60px rgba(0,0,0,.3)}
dialog.ds-dlg::backdrop{background:rgba(20,24,33,.55)}
.ds-dlg h3{margin:0 0 6px;font-size:18px}
.ds-dlg label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}
.ds-dlg select,.ds-dlg input[type=text],.ds-dlg input[type=email],.ds-dlg textarea{width:100%;font:inherit;font-size:14px;padding:8px 10px;border:1px solid #c7ccd6;border-radius:8px}
.ds-dlg textarea{min-height:90px}
.ds-dlg .bt{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.ds-dlg .av{background:#fff5f5;border:1px solid #f3b4b4;color:#7a1a1a;border-radius:10px;padding:10px 12px;font-size:13px;margin-top:10px}
.ds-dlg .info{font-size:12px;color:#4a5262;margin-top:4px;white-space:pre-line}
details.ds-det summary{cursor:pointer;font-weight:700;font-size:14px}
@media(max-width:1100px){.ds-cards{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:800px){.ds-row{grid-template-columns:56px 1fr}.ds-row>div:nth-child(3){grid-column:1/-1}.ds-foto{width:56px;height:56px}.ds-grid2{grid-template-columns:1fr}.ds-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.ds-row.alerta{margin:0 -14px;padding:14px}}
</style>

<div style="max-width:1200px">
  <div class="v2-h">
    <div>
      <h1>Devoluções Shopee</h1>
      <div class="v2-sub">Todas as devoluções dos últimos 60 dias, importadas da Shopee a cada 20 min (sem bipar).
        {% if atualizado %}Última importação {{ atualizado }}.{% else %}Primeira importação em andamento.{% endif %}
        {% if rodando %}<b>Importando agora…</b>{% endif %}</div>
      {% if erro %}<div class="ds-sub" style="color:#9b1c1c">Última importação falhou: {{ erro }}</div>{% endif %}
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <a class="v2-btn sec" href="{{ url_for('devolucoes.painel') }}">{{ ic.escanear|safe }} Bipar devoluções</a>
      <form method="post" action="{{ url_for('devshopee.atualizar') }}"><button class="v2-btn sec" type="submit">{{ ic.recarregar|safe }} Atualizar agora</button></form>
    </div>
  </div>

  <div class="ds-cards">
    <a class="ds-card {% if d.resumo.responder %}r{% endif %}" href="?f=responder"><div class="l">Responder agora</div><div class="n">{{ d.resumo.responder }}</div><div class="s">propostas e pedidos novos</div></a>
    <a class="ds-card {% if d.resumo.sem_bipar %}r{% endif %}" href="?f=sem_bipar"><div class="l">Entregues e não bipadas</div><div class="n">{{ d.resumo.sem_bipar }}</div><div class="s">há mais de {{ alerta_dias }} dias{% if d.resumo.bipar %} · +{{ d.resumo.bipar }} recentes{% endif %}</div></a>
    <a class="ds-card {% if d.resumo.caminho %}y{% endif %}" href="?f=caminho"><div class="l">A caminho de volta</div><div class="n">{{ d.resumo.caminho }}</div><div class="s">postadas pelo cliente</div></a>
    <a class="ds-card" href="?f=analise"><div class="l">Em análise Shopee</div><div class="n">{{ d.resumo.analise }}</div><div class="s">contestadas / mediação</div></a>
    <div class="ds-card"><div class="l">Em aberto</div><div class="n" style="font-size:20px">{{ brl(d.resumo.valor_aberto) }}</div><div class="s">{{ d.resumo.abertas }} devoluções abertas</div></div>
    <a class="ds-card" href="?f=todas"><div class="l">Últimos 60 dias</div><div class="n">{{ d.resumo.total }}</div><div class="s">{{ brl(d.resumo.valor_total) }} reembolsados · {{ d.resumo.canceladas }} canceladas</div></a>
  </div>

  {% if d.n_validas %}
  <details class="ds-det v2-panel" style="margin-bottom:16px">
    <summary>Motivos e produtos mais devolvidos (60 dias)</summary>
    <div class="ds-grid2" style="margin:12px 0 0">
      <table class="ds-tb">
        {% for m, n in d.motivos %}<tr><td>{{ m }}</td><td style="text-align:right"><b>{{ n }}</b></td><td style="text-align:right;color:#5b6474;width:50px">{{ (n * 100 / d.n_validas)|round|int }}%</td></tr>{% endfor %}
      </table>
      <table class="ds-tb">
        {% for k, n, it in d.top_skus %}<tr><td>{% if it.img %}<img src="{{ it.img }}" alt="" loading="lazy">{% endif %}<span class="ds-mono">{{ k }}</span> <span class="ds-sub">{{ it.nome[:40] }}</span></td><td style="text-align:right"><b>{{ n }}</b> un</td></tr>{% endfor %}
      </table>
    </div>
  </details>
  {% endif %}

  <form class="ds-busca" method="get">
    <input name="q" value="{{ busca }}" placeholder="Buscar por pedido, nº da devolução, rastreio, comprador ou SKU">
    <button class="v2-btn" type="submit">Buscar</button>
    {% if busca %}<a class="v2-btn sec" href="{{ url_for('devshopee.painel') }}">Limpar</a>{% endif %}
  </form>

  <div class="ds-tabs">
    <a href="?f=atencao" class="al {% if filtro == 'atencao' and not busca %}on{% endif %}">Precisa de atenção<b>{{ d.contagem.responder + d.contagem.sem_bipar + d.contagem.bipar }}</b></a>
    {% for k, nome, tom in situacoes %}
    <a href="?f={{ k }}" class="{% if tom == 'bad' and d.contagem[k] %}al {% endif %}{% if filtro == k and not busca %}on{% endif %}">{{ nome }}<b>{{ d.contagem[k] }}</b></a>
    {% endfor %}
    <a href="?f=todas" class="{% if filtro == 'todas' and not busca %}on{% endif %}">Todas<b>{{ d.contagem.values()|sum }}</b></a>
  </div>

  <div class="v2-panel">
    {% if busca %}<div class="ds-sub" style="margin-bottom:8px">{{ d.n_lista }} resultado(s) para “{{ busca }}”.</div>{% endif %}
    {% if not d.lista %}<p class="v2-sub">Nada aqui{% if filtro %} neste filtro{% endif %}.</p>{% endif %}
    {% for r in d.lista %}
    <div class="ds-row {% if r.sit == 'sem_bipar' %}alerta{% endif %}" id="d{{ r.return_sn }}">
      <div>
        {% set foto = (r.itens[0].img if r.itens else '') %}
        {% if foto %}<img class="ds-foto" src="{{ foto }}" alt="" loading="lazy">{% else %}<div class="ds-foto"></div>{% endif %}
      </div>
      <div>
        <div><span class="ds-tag {{ r.tom }}">{{ r.sit_nome }}</span> <b>{{ r.status_pt }}</b> · {{ r.valor_txt }}{% if r.tipo_pt %} · <span class="ds-sub">{{ r.tipo_pt }}</span>{% endif %}</div>
        {% for it in r.itens %}<div style="font-size:13px;margin-top:4px"><b>{{ it.qtd }}×</b> {{ it.nome }} {% if it.sku %}<span class="ds-mono">· {{ it.sku }}</span>{% endif %}</div>{% endfor %}
        <div class="ds-sub">Pedido <span class="ds-mono">{{ r.order_sn }}</span> · Devolução <span class="ds-mono">{{ r.return_sn }}</span>{% if r.comprador %} · {{ r.comprador }}{% endif %} · aberta {{ r.criado_txt }}</div>
        <div class="ds-q"><b>{{ r.motivo_pt }}:</b> {{ r.texto or '—' }}</div>
        {% if r.fotos or r.videos %}<div class="ds-mini">
          {% for f in r.fotos %}<a href="{{ f }}" target="_blank" rel="noopener"><img src="{{ f }}" alt="foto do cliente" loading="lazy"></a>{% endfor %}
          {% for v in r.videos %}<a class="v2-btn sec" href="{{ v.url }}" target="_blank" rel="noopener">▶ vídeo {{ loop.index }}</a>{% endfor %}
        </div>{% endif %}
        {% if r.oferta and r.neg_status == 'PENDING_RESPOND' %}
        <div class="ds-of"><b>Proposta {% if r.oferta.criador and r.oferta.criador != r.comprador %}de {{ r.oferta.criador }}{% else %}do comprador{% endif %}:</b>
          {{ r.oferta.solucao_pt or 'reembolso' }}{% if r.oferta.valor is not none %} — <b>{{ brl(r.oferta.valor) }}</b>{% endif %}
          {% if r.oferta.limite is not none %}<span class="ds-sub"> · contrapropostas restantes: {{ r.oferta.limite }}</span>{% endif %}</div>
        {% endif %}
        {% if r.prazo_txt %}<div class="ds-sub {% if r.prazo_perto %}ds-prazo{% endif %}">Prazo para responder: {{ r.prazo_txt }}</div>{% endif %}
        {% if r.precisa_envio %}
        <div class="ds-sub">Envio de volta: <b>{{ r.log_pt }}</b>{% if r.rastreio %} · rastreio <span class="ds-mono">{{ r.rastreio }}</span>{% endif %}{% if r.entregue_txt %} · entregue {{ r.entregue_txt }}{% endif %}</div>
        {% if r.log_hist %}<details><summary class="ds-sub" style="cursor:pointer">histórico do rastreio</summary><ul class="ds-hist">{% for h in r.log_hist %}<li>{{ h.txt }} — {{ h.d }}</li>{% endfor %}</ul></details>{% endif %}
        {% endif %}
        {% if r.bip %}<div class="ds-sub" style="color:#0d6b3f;font-weight:600">✔ {{ r.bip_txt }}</div>
        {% elif r.sit == 'sem_bipar' %}<div class="ds-sub ds-prazo">⚠ A transportadora marcou entregue há {{ r.ha_entregue }} e ninguém bipou o pacote. Confira se chegou.</div>
        {% elif r.entregue_em and r.sit == 'bipar' %}<div class="ds-sub" style="color:#7a5300;font-weight:600">Entregue há {{ r.ha_entregue }} — bipe o pacote na tela Devoluções quando chegar.</div>{% endif %}
        {% if r.acoes %}<ul class="ds-log">{% for a in r.acoes %}<li>{{ a.em_txt }} · {{ a.quem }} · {{ acoes_pt.get(a.acao, a.acao) }}: <b style="color:{{ '#0d6b3f' if a.ok else '#9b1c1c' }}">{{ 'ok' if a.ok else 'recusado' }}</b>{% if not a.ok %} — {{ a.resultado }}{% endif %}</li>{% endfor %}</ul>{% endif %}
      </div>
      <div class="ds-acts">
        {% if r.aberta %}
          {% if r.proposta_comprador %}<button class="v2-btn" type="button" data-acao="accept_offer" data-sn="{{ r.return_sn }}" data-pedido="{{ r.order_sn }}" data-txt="Aceitar a proposta do comprador: {{ r.oferta.solucao_pt }}{% if r.oferta.valor is not none %} — {{ brl(r.oferta.valor) }}{% endif %}">Aceitar proposta</button>{% endif %}
          {% if r.pode_ofertar %}<button class="v2-btn sec" type="button" data-acao="offer" data-sn="{{ r.return_sn }}" data-pedido="{{ r.order_sn }}" data-valor="{{ r.valor }}">Fazer contraproposta</button>{% endif %}
          {% if r.pode_contestar %}<button class="v2-btn sec ds-btn-r" type="button" data-acao="dispute" data-sn="{{ r.return_sn }}" data-pedido="{{ r.order_sn }}">Contestar</button>{% endif %}
          {% if r.pode_aceitar %}<button class="v2-btn sec" type="button" data-acao="confirm" data-sn="{{ r.return_sn }}" data-pedido="{{ r.order_sn }}" data-txt="Aceitar a devolução e reembolsar {{ r.valor_txt }} ao cliente">Aceitar devolução</button>{% endif %}
        {% endif %}
        <a class="v2-btn sec ds-abrir" href="{{ r.link }}" target="_blank" rel="noopener" data-copiar="{{ r.order_sn }}" title="Abre a Central do Vendedor e copia o nº do pedido para colar na busca">Abrir na Shopee ↗</a>
        <form method="post" action="{{ url_for('devshopee.reconsultar', sn=r.return_sn) }}"><input type="hidden" name="volta" value="{{ request.full_path }}"><button class="v2-btn sec" type="submit" style="width:100%;justify-content:center">{{ ic.recarregar|safe }} Reconsultar</button></form>
      </div>
    </div>
    {% endfor %}
    {% if d.n_lista > d.lista|length %}<div class="ds-sub" style="margin-top:10px">Mostrando {{ d.lista|length }} de {{ d.n_lista }}. Use a busca para achar uma devolução específica.</div>{% endif %}
  </div>
</div>

<dialog class="ds-dlg" id="dlg">
  <form method="post" enctype="multipart/form-data" id="dlg-form">
    <h3 id="dlg-t"></h3>
    <div class="ds-sub" id="dlg-s"></div>
    <input type="hidden" name="acao" id="dlg-acao">
    <input type="hidden" name="confirmado" value="1">
    <input type="hidden" name="volta" value="{{ request.full_path }}">
    <div id="dlg-corpo"></div>
    <div class="av" id="dlg-av">Isto é enviado <b>direto para a Shopee</b> e não dá para desfazer.</div>
    <div class="bt"><button type="button" class="v2-btn sec" id="dlg-x">Cancelar</button><button type="submit" class="v2-btn" id="dlg-ok">Confirmar</button></div>
  </form>
</dialog>

<script>
(function(){
  var dlg=document.getElementById('dlg'), f=document.getElementById('dlg-form'), corpo=document.getElementById('dlg-corpo');
  var T=document.getElementById('dlg-t'), S=document.getElementById('dlg-s'), ok=document.getElementById('dlg-ok');
  var base="{{ url_for('devshopee.painel') }}";
  var email={{ email|tojson }};
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function abrir(b){
    var a=b.dataset.acao, sn=b.dataset.sn;
    f.action=base+'/'+encodeURIComponent(sn)+'/acao';
    document.getElementById('dlg-acao').value=a;
    S.textContent='Pedido '+b.dataset.pedido+' · devolução '+sn;
    corpo.innerHTML=''; ok.disabled=false;
    if(a==='confirm'||a==='accept_offer'){
      T.textContent=a==='confirm'?'Aceitar a devolução?':'Aceitar a proposta do comprador?';
      corpo.innerHTML='<p style="font-size:14px">'+esc(b.dataset.txt)+'.</p>';
      ok.textContent='Sim, aceitar';
    } else if(a==='offer'){
      T.textContent='Contraproposta ao comprador'; ok.textContent='Enviar contraproposta'; ok.disabled=true;
      corpo.innerHTML='<p class="ds-sub">Consultando as opções na Shopee…</p>';
      fetch(base+'/'+encodeURIComponent(sn)+'/opcoes?tipo=oferta').then(function(r){return r.json();}).then(function(j){
        if(j.erro||!j.solucoes||!j.solucoes.length){corpo.innerHTML='<p class="ds-prazo">A Shopee não permite contraproposta nesta devolução'+(j.erro?': '+esc(j.erro):'.')+'</p>';return;}
        var h='<label>Solução</label><select name="solucao" id="dlg-sol">';
        j.solucoes.forEach(function(s){h+='<option value="'+s.id+'" data-max="'+(s.max==null?'':s.max)+'">'+esc(s.nome)+(s.max!=null?' (até R$ '+String(s.max).replace('.',',')+')':'')+'</option>';});
        h+='</select><label>Valor do reembolso (R$)</label><input type="text" name="valor" inputmode="decimal" required placeholder="ex.: 15,00"><div class="info" id="dlg-max"></div>';
        corpo.innerHTML=h; ok.disabled=false;
      }).catch(function(){corpo.innerHTML='<p class="ds-prazo">Não consegui consultar a Shopee agora.</p>';});
    } else if(a==='dispute'){
      T.textContent='Contestar a devolução'; ok.textContent='Enviar contestação'; ok.disabled=true;
      corpo.innerHTML='<p class="ds-sub">Consultando os motivos na Shopee…</p>';
      fetch(base+'/'+encodeURIComponent(sn)+'/opcoes?tipo=contestar').then(function(r){return r.json();}).then(function(j){
        if(j.erro||!j.motivos||!j.motivos.length){corpo.innerHTML='<p class="ds-prazo">A Shopee não permite contestar esta devolução agora'+(j.erro?': '+esc(j.erro):'.')+'</p>';return;}
        var h='<label>Motivo</label><select name="motivo" id="dlg-mot" required><option value="">Escolha…</option>';
        j.motivos.forEach(function(m){h+='<option value="'+m.id+'" data-det="'+esc(m.detalhe)+'" data-prov="'+esc((m.provas||[]).join(' · '))+'">'+esc(m.nome)+'</option>';});
        h+='</select><div class="info" id="dlg-det"></div>';
        h+='<label>Explicação para a Shopee</label><textarea name="texto" required placeholder="Ex.: o produto foi enviado lacrado e conferido; a foto mostra o item correto…"></textarea>';
        h+='<label>Fotos de prova (até 10)</label><input type="file" name="fotos" accept="image/*" multiple required>';
        h+='<label>E-mail para a Shopee responder</label><input type="email" name="email" required value="'+esc(email)+'">';
        corpo.innerHTML=h; ok.disabled=false;
        var sel=document.getElementById('dlg-mot');
        sel.addEventListener('change',function(){var o=sel.options[sel.selectedIndex];document.getElementById('dlg-det').textContent=(o.dataset.det||'')+(o.dataset.prov?'\\nProva pedida: '+o.dataset.prov:'');});
      }).catch(function(){corpo.innerHTML='<p class="ds-prazo">Não consegui consultar a Shopee agora.</p>';});
    }
    dlg.showModal();
  }
  document.querySelectorAll('[data-acao]').forEach(function(b){b.addEventListener('click',function(){abrir(b);});});
  document.querySelectorAll('.ds-abrir').forEach(function(a){a.addEventListener('click',function(){
    try{navigator.clipboard.writeText(a.dataset.copiar);a.textContent='Pedido copiado ✓ (cole na busca)';}catch(e){}
  });});
  document.getElementById('dlg-x').addEventListener('click',function(){dlg.close();});
  f.addEventListener('submit',function(){ok.disabled=true;ok.textContent='Enviando…';});
})();
</script>
"""


def init_app(app, get_shopee_client):
    _get_shopee[0] = get_shopee_client
    init_db()
    app.register_blueprint(bp)
    threading.Thread(target=_loop, daemon=True).start()
