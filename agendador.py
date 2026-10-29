"""Tarefas automáticas em horário fixo (horário de Brasília):

- Dia finalizado: de segunda a sábado às 18:00 -- arquiva os pedidos que a transportadora já
  coletou, igual ao botão 'Dia finalizado'.
- Etiquetas: de segunda a sábado às 06:50 -- atualiza a lista de lotes da tela
  Etiquetas, igual ao botão 'Atualizar agora'.

Tudo roda numa linha de execução separada (em segundo plano), com folga entre as
consultas à Shopee, então a separação continua funcionando normalmente enquanto isso.

Cada tarefa roda uma vez por dia: o 'carimbo' do dia fica no banco (tabela
agendamentos), então mesmo com dois processos do servidor vivos ao mesmo tempo
(reciclagem do gunicorn) só um deles executa. Se o servidor estiver reiniciando
bem no horário, a tarefa roda assim que ele voltar, dentro da janela ('ate').
Se der erro (Shopee fora do ar, por exemplo), tenta de novo a cada 15 minutos,
no máximo 3 vezes."""
import threading
import time
from datetime import datetime, timedelta, timezone

import models

BRT = timezone(timedelta(hours=-3))
CHECAR_A_CADA = 30          # segundos
MAX_TENTATIVAS = 3
ESPERA_NOVA_TENTATIVA = 15  # minutos

# dias: 0 = segunda ... 6 = domingo
TAREFAS = {
    "dia_finalizado": {"nome": "Dia finalizado", "hora": (18, 0), "ate": (23, 59), "dias": {0, 1, 2, 3, 4, 5}},
    "etiquetas": {"nome": "Atualizar etiquetas", "hora": (6, 50), "ate": (12, 0), "dias": {0, 1, 2, 3, 4, 5}},
}

_executores = {}
_iniciado = [False]


def _agora():
    return datetime.now(BRT)


def init_db():
    with models.get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agendamentos (
                chave TEXT NOT NULL,
                dia TEXT NOT NULL,
                status TEXT NOT NULL,
                tentativas INTEGER NOT NULL DEFAULT 1,
                inicio TEXT,
                fim TEXT,
                resultado TEXT,
                PRIMARY KEY (chave, dia)
            )"""
        )


def _na_janela(t, agora):
    if agora.weekday() not in t["dias"]:
        return False
    hm = (agora.hour, agora.minute)
    return t["hora"] <= hm <= t["ate"]


def _reservar(chave, dia, agora):
    """Marca a tarefa do dia como 'rodando'. Devolve True só para o processo que
    conseguiu a marcação (os outros veem que já está feita/rodando e não fazem nada)."""
    with models.get_conn() as conn:
        n = conn.execute(
            "INSERT OR IGNORE INTO agendamentos (chave, dia, status, tentativas, inicio) VALUES (?, ?, 'rodando', 1, ?)",
            (chave, dia, agora.isoformat()),
        ).rowcount
        if n:
            return True
        # Nova tentativa depois de erro, respeitando o intervalo e o limite.
        # Também retoma uma execução que ficou 'rodando' por mais de 30 min (o
        # servidor reiniciou no meio, por exemplo).
        limite = (agora - timedelta(minutes=ESPERA_NOVA_TENTATIVA)).isoformat()
        parada = (agora - timedelta(minutes=30)).isoformat()
        n = conn.execute(
            "UPDATE agendamentos SET status = 'rodando', tentativas = tentativas + 1, inicio = ? "
            "WHERE chave = ? AND dia = ? AND tentativas < ? AND "
            "((status = 'erro' AND fim < ?) OR (status = 'rodando' AND inicio < ?))",
            (agora.isoformat(), chave, dia, MAX_TENTATIVAS, limite, parada),
        ).rowcount
        return bool(n)


def _concluir(chave, dia, status, resultado):
    with models.get_conn() as conn:
        conn.execute(
            "UPDATE agendamentos SET status = ?, fim = ?, resultado = ? WHERE chave = ? AND dia = ?",
            (status, _agora().isoformat(), resultado, chave, dia),
        )


def _executar(chave, dia):
    try:
        resultado = _executores[chave]()
        _concluir(chave, dia, "ok", resultado or "")
    except Exception as e:
        _concluir(chave, dia, "erro", str(e)[:300])


def _verificar():
    agora = _agora()
    dia = agora.strftime("%Y-%m-%d")
    for chave, t in TAREFAS.items():
        if chave in _executores and _na_janela(t, agora) and _reservar(chave, dia, agora):
            threading.Thread(target=_executar, args=(chave, dia), daemon=True).start()


def _loop():
    time.sleep(60)  # deixa o servidor subir antes
    while True:
        try:
            _verificar()
        except Exception:
            pass
        time.sleep(CHECAR_A_CADA)


def ultimo(chave):
    """Última execução automática da tarefa, para mostrar na tela."""
    try:
        with models.get_conn() as conn:
            r = conn.execute(
                "SELECT dia, status, inicio, fim, resultado FROM agendamentos WHERE chave = ? ORDER BY dia DESC LIMIT 1",
                (chave,),
            ).fetchone()
    except Exception:
        return None
    if not r:
        return None
    quando = (r["fim"] or r["inicio"] or "")
    try:
        quando = datetime.fromisoformat(quando).strftime("%d/%m às %H:%M")
    except Exception:
        pass
    return {"status": r["status"], "quando": quando, "resultado": r["resultado"] or ""}


def init_app(app, get_client, finalizar_dia, resumo_fechamento):
    init_db()

    def fechar():
        client = get_client()
        if client is None:
            raise RuntimeError("Loja ainda não autorizada na Shopee.")
        r = None
        # Se alguém clicou em 'Dia finalizado' agora mesmo, espera esse terminar.
        for _ in range(30):
            r = finalizar_dia(client, pausa=0.5)
            if r is not None:
                break
            time.sleep(20)
        if r is None:
            raise RuntimeError("Outro fechamento ficou rodando por mais de 10 minutos.")
        return resumo_fechamento(r)

    def etiquetas():
        import lotes
        lotes._rodando[0] = True  # mostra o aviso 'Atualizando...' na tela
        try:
            for _ in range(30):
                if lotes.atualizar():
                    break
                time.sleep(20)  # alguém clicou em Atualizar agora: espera e refaz
        finally:
            lotes._rodando[0] = False
        erro = lotes._ler().get("erro")
        if erro:
            raise RuntimeError(erro)
        return "Lista de etiquetas atualizada."

    _executores["dia_finalizado"] = fechar
    _executores["etiquetas"] = etiquetas
    if not _iniciado[0]:
        _iniciado[0] = True
        threading.Thread(target=_loop, daemon=True).start()
