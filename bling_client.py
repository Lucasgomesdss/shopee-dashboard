"""Conexão com a API v3 do Bling (conta Ana), usada só pela tela Devoluções.

Usa o app "DescFlow Devoluções", separado do emissor de notas, para que um problema
aqui nunca derrube o faturamento. Credenciais em variáveis de ambiente no Render:
BLING_CLIENT_ID e BLING_CLIENT_SECRET. O token fica salvo no banco (tabela
bling_tokens, uma linha só) e se renova sozinho.

Fase 2: somente LEITURA (pedidos de venda e notas fiscais). Este módulo não tem
nenhuma função que crie, altere, emita ou exclua nada no Bling.
"""

import base64
import os
import time
from datetime import datetime

import requests

import models

CLIENT_ID = os.environ.get("BLING_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("BLING_CLIENT_SECRET", "")

OAUTH_BASE = "https://www.bling.com.br/Api/v3"
API_BASE = "https://api.bling.com.br/Api/v3"

# Situações da NF-e no Bling consideradas "nota de saída válida" para devolução.
NFE_AUTORIZADAS = {5, 6, 7}  # Autorizada, Emitida DANFE, Registrada
NFE_SITUACOES = {
    1: "Pendente", 2: "Cancelada", 3: "Aguardando recibo", 4: "Rejeitada",
    5: "Autorizada", 6: "Emitida DANFE", 7: "Registrada", 8: "Aguardando protocolo",
    9: "Denegada", 10: "Consulta situação", 11: "Bloqueada",
}

_session = requests.Session()
_ultima_chamada = [0.0]


class BlingErro(Exception):
    pass


def configurado():
    return bool(CLIENT_ID and CLIENT_SECRET)


def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bling_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _salvar_token(dados):
    expira = time.time() + int(dados.get("expires_in") or 3600)
    with models.get_conn() as conn:
        conn.execute(
            """INSERT INTO bling_tokens (id, access_token, refresh_token, expires_at, updated_at)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token,
                 refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                 updated_at=excluded.updated_at""",
            (dados["access_token"], dados["refresh_token"], expira, datetime.utcnow().isoformat()),
        )


def _ler_token():
    with models.get_conn() as conn:
        row = conn.execute("SELECT * FROM bling_tokens WHERE id = 1").fetchone()
        return dict(row) if row else None


def conectado():
    return _ler_token() is not None


def url_autorizacao(state):
    return (
        f"{OAUTH_BASE}/oauth/authorize?response_type=code"
        f"&client_id={CLIENT_ID}&state={state}"
    )


def _pedir_token(campos):
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = _session.post(
        f"{OAUTH_BASE}/oauth/token",
        data=campos,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=20,
    )
    try:
        dados = resp.json()
    except ValueError:
        raise BlingErro(f"Resposta inválida do Bling ao pedir token (HTTP {resp.status_code}).")
    if resp.status_code != 200 or "access_token" not in dados:
        erro = (dados.get("error") or {}) if isinstance(dados.get("error"), dict) else {}
        raise BlingErro(erro.get("description") or erro.get("message") or str(dados)[:200])
    _salvar_token(dados)
    return dados


def trocar_code(code):
    """Chamado pelo /bling/callback depois que o Lucas autoriza o app."""
    return _pedir_token({"grant_type": "authorization_code", "code": code})


def _access_token():
    tok = _ler_token()
    if not tok:
        raise BlingErro("Bling ainda não autorizado. Use o botão 'Conectar Bling'.")
    if time.time() > tok["expires_at"] - 300:
        dados = _pedir_token({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        return dados["access_token"]
    return tok["access_token"]


def _get(caminho, params=None):
    """GET na API do Bling respeitando o limite de ~3 chamadas por segundo."""
    espera = 0.36 - (time.time() - _ultima_chamada[0])
    if espera > 0:
        time.sleep(espera)
    for tentativa in range(3):
        _ultima_chamada[0] = time.time()
        resp = _session.get(
            f"{API_BASE}/{caminho}",
            params=params,
            headers={"Authorization": f"Bearer {_access_token()}", "Accept": "application/json"},
            timeout=20,
        )
        if resp.status_code == 429 and tentativa < 2:
            time.sleep(1.5)
            continue
        break
    try:
        dados = resp.json()
    except ValueError:
        raise BlingErro(f"Resposta inválida do Bling (HTTP {resp.status_code}).")
    if resp.status_code >= 400:
        erro = dados.get("error") or {}
        msg = erro.get("description") or erro.get("message") if isinstance(erro, dict) else str(erro)
        raise BlingErro(msg or f"HTTP {resp.status_code}")
    return dados


def buscar_pedido_por_numero_loja(order_sn):
    """Pedido de venda do Bling cujo 'número na loja' é o número do pedido Shopee."""
    dados = _get("pedidos/vendas", params={"numerosLojas[]": order_sn})
    lista = dados.get("data") or []
    return lista


def detalhe_pedido(pedido_id):
    return _get(f"pedidos/vendas/{pedido_id}").get("data") or {}


def detalhe_nfe(nfe_id):
    return _get(f"nfe/{nfe_id}").get("data") or {}
