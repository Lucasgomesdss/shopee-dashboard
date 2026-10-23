"""Fase 3 das Devoluções: monta e cria a nota de devolução no Bling (conta Ana).

A nota é criada como RASCUNHO (situação Pendente). A emissão na Sefaz continua sendo
feita no próprio Bling ("Enviar notas pendentes"), depois da conferência.

O preenchimento copia o MODELO: a última nota de devolução feita à mão no Bling
(nota de entrada, finalidade Devolução). Dele saem natureza de operação, loja,
intermediador, frete, forma de pagamento e o final do CFOP. Da nota de saída
original saem cliente, itens, preços, desconto e a chave referenciada.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import bling_client
import models

BRT = timezone(timedelta(hours=-3))
FINALIDADE_DEVOLUCAO = 4

# Código IBGE da UF (2 primeiros dígitos da chave de acesso) -> sigla
UF_IBGE = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
    "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
    "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
    "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF",
}

ST_NA_FILA = "na_fila"
ST_GERANDO = "gerando"
ST_GERADO = "gerado"
ST_FALHOU = "falhou"

_job_lock = threading.Lock()


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


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------------ modelo

def _resumir_modelo(nfe):
    itens = nfe.get("itens") or []
    cfop = str((itens[0] if itens else {}).get("cfop") or "")
    parcelas = nfe.get("parcelas") or []
    forma = (parcelas[0].get("formaPagamento") or {}).get("id") if parcelas else None
    return {
        "id": nfe.get("id"),
        "numero": str(nfe.get("numero") or ""),
        "natureza_id": (nfe.get("naturezaOperacao") or {}).get("id"),
        "loja_id": (nfe.get("loja") or {}).get("id"),
        "intermediador": nfe.get("intermediador") or None,
        "frete_por_conta": (nfe.get("transporte") or {}).get("fretePorConta"),
        "forma_pagamento_id": forma,
        "cfop_final": cfop[1:] if len(cfop) == 4 else "",
        "cliente": (nfe.get("contato") or {}).get("nome") or "",
    }


def obter_modelo(forcar=False):
    """Acha (e guarda) a última nota de devolução feita no Bling para usar de modelo."""
    if not forcar:
        salvo = _meta_ler("modelo_devolucao")
        if salvo:
            try:
                return json.loads(salvo)
            except ValueError:
                pass
    vistos = 0
    for pagina in range(1, 4):
        lista = bling_client.listar_nfes({"tipo": "0", "pagina": pagina, "limite": 100})
        if not lista:
            break
        for item in lista:
            if item.get("situacao") not in bling_client.NFE_AUTORIZADAS:
                continue
            vistos += 1
            if vistos > 12:
                break
            nfe = bling_client.detalhe_nfe(item["id"])
            if int(nfe.get("finalidade") or 0) == FINALIDADE_DEVOLUCAO and (nfe.get("naturezaOperacao") or {}).get("id"):
                modelo = _resumir_modelo(nfe)
                _meta_gravar("modelo_devolucao", json.dumps(modelo, ensure_ascii=False))
                return modelo
        if vistos > 12:
            break
    raise bling_client.BlingErro(
        "Não achei no Bling uma nota de devolução feita à mão para usar de modelo."
    )


# ----------------------------------------------------------------- montar corpo

def _uf_empresa(chave):
    return UF_IBGE.get(str(chave or "")[:2], "")


def montar_corpo(dev, itens_dev, original, modelo):
    """Monta o JSON da nota de devolução a partir da nota original e do modelo."""
    contato = dict(original.get("contato") or {})
    endereco = contato.get("endereco") or {}
    uf_cliente = (endereco.get("uf") or "").upper()
    uf_empresa = _uf_empresa(original.get("chaveAcesso") or dev.get("nfe_chave"))

    cfop = ""
    if modelo.get("cfop_final"):
        prefixo = "1" if uf_cliente and uf_empresa and uf_cliente == uf_empresa else "2"
        cfop = prefixo + modelo["cfop_final"]

    qtd_por_codigo = {}
    for it in itens_dev:
        qtd_por_codigo.setdefault(it["codigo"], []).append(_num(it["qtd_devolver"]))

    itens = []
    soma_original = 0.0
    soma_devolvida = 0.0
    for it in original.get("itens") or []:
        valor = _num(it.get("valor"))
        qtd_orig = _num(it.get("quantidade"))
        soma_original += valor * qtd_orig
        fila = qtd_por_codigo.get(it.get("codigo")) or []
        qtd = fila.pop(0) if fila else 0.0
        if qtd <= 0:
            continue
        soma_devolvida += valor * qtd
        novo = {
            "codigo": it.get("codigo"),
            "descricao": it.get("descricao"),
            "unidade": it.get("unidade") or "UN",
            "quantidade": qtd,
            "valor": valor,
            "tipo": it.get("tipo") or "P",
            "origem": it.get("origem") if it.get("origem") is not None else 0,
        }
        for campo in ("classificacaoFiscal", "cest", "gtin", "pesoBruto", "pesoLiquido"):
            if it.get(campo) not in (None, ""):
                novo[campo] = it[campo]
        if cfop:
            novo["cfop"] = cfop
        itens.append(novo)

    if not itens:
        raise ValueError("Nenhum item com quantidade para devolver.")

    # Desconto da nota original, proporcional ao que voltou.
    frete_orig = _num(original.get("valorFrete"))
    desconto_orig = max(0.0, round(soma_original + frete_orig - _num(original.get("valorNota")), 2))
    proporcao = (soma_devolvida / soma_original) if soma_original else 0
    desconto = round(desconto_orig * proporcao, 2)
    total = round(soma_devolvida - desconto, 2)

    agora = datetime.now(BRT)
    corpo = {
        "tipo": 0,
        "finalidade": FINALIDADE_DEVOLUCAO,
        "dataOperacao": agora.strftime("%Y-%m-%d %H:%M:%S"),
        "naturezaOperacao": {"id": modelo["natureza_id"]},
        "contato": {
            "nome": contato.get("nome"),
            "tipoPessoa": "J" if len("".join(c for c in str(contato.get("numeroDocumento") or "") if c.isdigit())) == 14 else "F",
            "numeroDocumento": contato.get("numeroDocumento"),
            "contribuinte": 9,
        },
        "documentoReferenciado": {
            "modelo": "55",
            "chaveAcesso": original.get("chaveAcesso"),
            "numero": str(original.get("numero") or ""),
            "serie": str(original.get("serie") or ""),
        },
        "desconto": desconto,
        "itens": itens,
        "parcelas": [{
            "data": agora.strftime("%Y-%m-%d"),
            "valor": total,
            "observacoes": "Devolução de mercadorias",
        }],
        "observacoes": f"Devolução referente à NF-e {original.get('numero')} (pedido Shopee {dev.get('order_sn')}).",
    }
    if endereco:
        corpo["contato"]["endereco"] = {
            k: endereco.get(k) for k in ("endereco", "numero", "complemento", "bairro", "cep", "municipio", "uf", "pais")
            if endereco.get(k) not in (None, "")
        }
    for k in ("ie", "email", "telefone"):
        if contato.get(k):
            corpo["contato"][k] = contato[k]
    loja_id = (original.get("loja") or {}).get("id") or modelo.get("loja_id")
    if loja_id:
        corpo["loja"] = {"id": loja_id}
    if modelo.get("intermediador"):
        corpo["intermediador"] = modelo["intermediador"]
    if modelo.get("frete_por_conta") is not None:
        corpo["transporte"] = {"fretePorConta": modelo["frete_por_conta"]}
    if modelo.get("forma_pagamento_id"):
        corpo["parcelas"][0]["formaPagamento"] = {"id": modelo["forma_pagamento_id"]}
    return corpo


def corpo_para(dev_id):
    """Carrega a devolução, a nota original (fresca do Bling) e o modelo; devolve o corpo."""
    with models.get_conn() as conn:
        dev = dict(conn.execute("SELECT * FROM devolucoes WHERE id = ?", (dev_id,)).fetchone())
        itens = [dict(r) for r in conn.execute(
            "SELECT * FROM devolucao_itens WHERE devolucao_id = ? ORDER BY id", (dev_id,)
        ).fetchall()]
    original = bling_client.detalhe_nfe(dev["bling_nfe_id"])
    modelo = obter_modelo()
    return montar_corpo(dev, itens, original, modelo)


# --------------------------------------------------------------------- geração

def _ja_tem_devolucao(order_sn, dev_id):
    with models.get_conn() as conn:
        row = conn.execute(
            "SELECT nfe_devolucao_numero FROM devolucoes WHERE order_sn = ? AND id <> ? "
            "AND nfe_devolucao_id IS NOT NULL",
            (order_sn, dev_id),
        ).fetchone()
        return row["nfe_devolucao_numero"] if row else None


def _atualizar(dev_id, **campos):
    campos["updated_at"] = datetime.utcnow().isoformat()
    sets = ", ".join(f"{k} = ?" for k in campos)
    with models.get_conn() as conn:
        conn.execute(f"UPDATE devolucoes SET {sets} WHERE id = ?", (*campos.values(), dev_id))


def gerar_uma(dev_id):
    with models.get_conn() as conn:
        dev = conn.execute("SELECT * FROM devolucoes WHERE id = ?", (dev_id,)).fetchone()
    if not dev or dev["nfe_devolucao_id"] or dev["removido"]:
        return
    outra = _ja_tem_devolucao(dev["order_sn"], dev_id)
    if outra:
        _atualizar(dev_id, status=ST_FALHOU, motivo=f"Este pedido já tem devolução gerada (nota {outra}).")
        return
    _atualizar(dev_id, status=ST_GERANDO, motivo=None)
    try:
        corpo = corpo_para(dev_id)
        criada = bling_client.criar_nfe(corpo)
    except Exception as e:
        _atualizar(dev_id, status=ST_FALHOU, motivo=f"Bling recusou: {e}")
        return
    if not criada.get("id"):
        _atualizar(dev_id, status=ST_FALHOU, motivo="O Bling não devolveu o número da nota criada.")
        return
    _atualizar(
        dev_id, status=ST_GERADO, motivo=None,
        nfe_devolucao_id=criada.get("id"), nfe_devolucao_numero=str(criada.get("numero") or ""),
    )


def _rodar_fila():
    with _job_lock:
        while True:
            with models.get_conn() as conn:
                row = conn.execute(
                    "SELECT id FROM devolucoes WHERE status = ? AND removido = 0 ORDER BY id LIMIT 1",
                    (ST_NA_FILA,),
                ).fetchone()
            if not row:
                return
            gerar_uma(row["id"])


def iniciar_fila():
    if not _job_lock.locked():
        threading.Thread(target=_rodar_fila, daemon=True).start()


def retomar_se_preciso():
    """Se o servidor reiniciou no meio de um lote, continua de onde parou."""
    with models.get_conn() as conn:
        conn.execute(
            "UPDATE devolucoes SET status = ? WHERE status = ? AND nfe_devolucao_id IS NULL",
            (ST_NA_FILA, ST_GERANDO),
        )
        tem = conn.execute(
            "SELECT 1 FROM devolucoes WHERE status = ? AND removido = 0 LIMIT 1", (ST_NA_FILA,)
        ).fetchone()
    if tem:
        iniciar_fila()
