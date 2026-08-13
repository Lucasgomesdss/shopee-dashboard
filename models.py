"""Camada de banco de dados (SQLite) para o dashboard de separação de pedidos.

Guardamos localmente APENAS o que o time de expedição precisa:
- número do pedido (order_sn)
- código de rastreio (tracking_number)
- lista de itens: nome do produto, variação, quantidade
- status do fluxo de separação (não é o status financeiro/pagamento da Shopee)

Nada de valores de venda, forma de pagamento ou dados do comprador é salvo aqui.
"""

import sqlite3
import json
import os
import time
from datetime import datetime
from contextlib import contextmanager

DB_PATH = "/var/data/dashboard.db" if os.path.isdir("/var/data") else os.path.join(os.path.dirname(__file__), "dashboard.db")

STATUS_TO_SEPARATE = "to_separate"   # ainda não foi escaneado/separado
STATUS_PENDING = "pending"           # separação com problema (item faltando, etc.)
STATUS_COMPLETED = "completed"       # separado e conferido com OK do colaborador
STATUS_ARCHIVED = "archived"         # concluído E já coletado pela transportadora — some da lista/contagem mas fica salvo


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_sn TEXT PRIMARY KEY,
                tracking_number TEXT,
                items_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'to_separate',
                pending_reason TEXT,
                missing_items_json TEXT,
                employee_name TEXT,
                confirmed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracking ON orders(tracking_number)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shopee_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                shop_id TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Migração: bancos criados antes do campo missing_items_json existir. Ignora
        # o erro se a coluna já existir (SQLite não tem "ADD COLUMN IF NOT EXISTS").
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN missing_items_json TEXT")
        except sqlite3.OperationalError:
            pass


def save_shopee_token(shop_id: str, access_token: str, refresh_token: str, expire_in_seconds: int):
    """Guarda o token da loja autorizada. Só existe 1 linha (id=1) — sempre substitui a anterior."""
    now = datetime.utcnow()
    expires_at = time.time() + expire_in_seconds
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO shopee_tokens (id, shop_id, access_token, refresh_token, expires_at, updated_at)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 shop_id=excluded.shop_id, access_token=excluded.access_token,
                 refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                 updated_at=excluded.updated_at""",
            (shop_id, access_token, refresh_token, expires_at, now.isoformat()),
        )


def get_shopee_token():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM shopee_tokens WHERE id = 1").fetchone()
        return dict(row) if row else None


def upsert_order(order_sn: str, tracking_number: str, items: list):
    """Insere um pedido novo vindo da Shopee. Se já existir, só atualiza os itens/rastreio
    (não mexe no status para não perder o trabalho já feito pelo time) -- EXCETO se ele
    estava arquivado: quem chama upsert_order (o /sync) só busca pedidos que estão
    PROCESSED agora, então se um pedido arquivado aparece de novo aqui é porque ele voltou
    a ter etiqueta válida (ex: etiqueta antiga foi invalidada pela transportadora e a
    Shopee reemitiu uma nova) -- nesse caso ele "revive" e volta pra fila de A separar,
    pra não ficar escondido do time."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT order_sn, status FROM orders WHERE order_sn = ?", (order_sn,)
        ).fetchone()
        if existing:
            if existing["status"] == STATUS_ARCHIVED:
                conn.execute(
                    """UPDATE orders SET tracking_number = ?, items_json = ?, status = ?,
                       updated_at = ? WHERE order_sn = ?""",
                    (tracking_number, json.dumps(items), STATUS_TO_SEPARATE, now, order_sn),
                )
            else:
                conn.execute(
                    "UPDATE orders SET tracking_number = ?, items_json = ?, updated_at = ? WHERE order_sn = ?",
                    (tracking_number, json.dumps(items), now, order_sn),
                )
        else:
            conn.execute(
                """INSERT INTO orders
                   (order_sn, tracking_number, items_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_sn, tracking_number, json.dumps(items), STATUS_TO_SEPARATE, now, now),
            )


def find_by_tracking(tracking_number: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE tracking_number = ?", (tracking_number.strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_order(order_sn: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_sn = ?", (order_sn,)).fetchone()
        return dict(row) if row else None


def list_by_status(status: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY updated_at DESC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def counts():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM orders GROUP BY status"
        ).fetchall()
        result = {STATUS_TO_SEPARATE: 0, STATUS_PENDING: 0, STATUS_COMPLETED: 0}
        for r in rows:
            result[r["status"]] = r["n"]
        return result


def mark_completed(order_sn: str, employee_name: str):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """UPDATE orders SET status = ?, employee_name = ?, confirmed_at = ?,
               pending_reason = NULL, missing_items_json = NULL, updated_at = ? WHERE order_sn = ?""",
            (STATUS_COMPLETED, employee_name, now, now, order_sn),
        )


def mark_pending(order_sn: str, employee_name: str, reason: str):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """UPDATE orders SET status = ?, employee_name = ?, pending_reason = ?,
               updated_at = ? WHERE order_sn = ?""",
            (STATUS_PENDING, employee_name, reason, now, order_sn),
        )


def mark_missing_product(order_sn: str, employee_name: str, missing_items: list):
    """Marca o pedido como pendente por falta de produto, guardando quais itens
    especificamente faltaram — usado depois pra agregar tudo na aba Produto Pendente
    e gerar o PDF de busca em massa no estoque."""
    now = datetime.utcnow().isoformat()
    names = ", ".join(
        f"{it['name']} ({it['variation']})" if it.get("variation") and it["variation"] != "-" else it.get("name", "")
        for it in missing_items
    )
    reason = f"Falta produto: {names}" if names else "Falta produto"
    with get_conn() as conn:
        conn.execute(
            """UPDATE orders SET status = ?, employee_name = ?, pending_reason = ?,
               missing_items_json = ?, updated_at = ? WHERE order_sn = ?""",
            (STATUS_PENDING, employee_name, reason, json.dumps(missing_items), now, order_sn),
        )


def list_missing_items():
    """Todos os itens marcados como 'faltou' nos pedidos ainda pendentes — usado para
    montar a lista agregada e o PDF da aba Produto Pendente."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT missing_items_json FROM orders WHERE status = ? AND missing_items_json IS NOT NULL",
            (STATUS_PENDING,),
        ).fetchall()
        all_items = []
        for r in rows:
            all_items.extend(json.loads(r["missing_items_json"]))
        return all_items


def reopen(order_sn: str):
    """Volta um pedido pendente para a fila de separação."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, pending_reason = NULL, missing_items_json = NULL, updated_at = ? WHERE order_sn = ?",
            (STATUS_TO_SEPARATE, now, order_sn),
        )


def open_orders_items():
    """Todos os itens de pedidos ainda não concluídos (to_separate + pending), para o PDF de pré-separação."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT items_json FROM orders WHERE status IN (?, ?)",
            (STATUS_TO_SEPARATE, STATUS_PENDING),
        ).fetchall()
        all_items = []
        for r in rows:
            all_items.extend(json.loads(r["items_json"]))
        return all_items


def list_open_order_sns(limit: int = 500):
    """order_sn de pedidos ainda na fila local (a separar + pendente), mais antigos
    primeiro — usado no 'Dia finalizado' para verificar com a Shopee quais já saíram
    do status PROCESSED (foram enviados por outro canal, cancelados, etc) sem passar
    pelo escaneamento do time, e arquivá-los, corrigindo a contagem de 'A separar'.
    Limitado por chamada pra não estourar o tempo do botão quando o backlog crescer
    muito; o restante é checado nas próximas conferências."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT order_sn FROM orders WHERE status IN (?, ?) ORDER BY updated_at ASC LIMIT ?",
            (STATUS_TO_SEPARATE, STATUS_PENDING, limit),
        ).fetchall()
        return [r["order_sn"] for r in rows]


def list_completed_order_sns(limit: int = 200):
    """order_sn dos pedidos em Concluídos, mais antigos primeiro (são os mais prováveis
    de já terem sido coletados) — usado no /sync para verificar com a Shopee e arquivar
    os que já foram coletados. Limitado por chamada pra não estourar o tempo do /sync
    quando o backlog de concluídos crescer muito; o restante é checado nas próximas
    sincronizações."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT order_sn FROM orders WHERE status = ? ORDER BY confirmed_at ASC LIMIT ?",
            (STATUS_COMPLETED, limit),
        ).fetchall()
        return [r["order_sn"] for r in rows]


def archive_order(order_sn: str):
    """Arquiva um pedido: sai da lista e da contagem (seja de Concluídos, seja de
    A separar/Pendente), mas não é apagado — fica salvo no banco pra consulta futura."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_sn = ?",
            (STATUS_ARCHIVED, now, order_sn),
        )


def list_archived_order_sns(limit: int = 500):
    """order_sn de pedidos arquivados, mais recentes primeiro -- usado no 'Dia
    finalizado' para verificar se algum deles voltou a ter etiqueta válida (PROCESSED)
    na Shopee -- por exemplo quando a transportadora invalida uma etiqueta e a Shopee
    reemite uma nova. Sem essa checagem, o pedido ficava escondido do time pra sempre,
    mesmo precisando ser separado de novo."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT order_sn FROM orders WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (STATUS_ARCHIVED, limit),
        ).fetchall()
        return [r["order_sn"] for r in rows]


def revive_order(order_sn: str):
    """Traz um pedido arquivado de volta pra fila de A separar -- usado quando o 'Dia
    finalizado' descobre que ele voltou a ter etiqueta válida (PROCESSED) na Shopee."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_sn = ?",
            (STATUS_TO_SEPARATE, now, order_sn),
        )
