"""Popula o banco local com pedidos de exemplo, para você testar o fluxo
completo antes de ligar a integração real com a Shopee."""

import models

MOCK_ORDERS = [
    ("BR250800001", "BR123456789ABC", [
        {"name": "Camiseta Básica Algodão", "variation": "Preto - M", "quantity": 2},
        {"name": "Boné Trucker", "variation": "Único", "quantity": 1},
    ]),
    ("BR250800002", "BR123456790ABC", [
        {"name": "Caneca Personalizada 300ml", "variation": "Branca", "quantity": 3},
    ]),
    ("BR250800003", "BR123456791ABC", [
        {"name": "Camiseta Básica Algodão", "variation": "Branco - G", "quantity": 1},
        {"name": "Camiseta Básica Algodão", "variation": "Preto - P", "quantity": 1},
    ]),
    ("BR250800004", "BR123456792ABC", [
        {"name": "Mochila Impermeável", "variation": "Cinza", "quantity": 1},
    ]),
    ("BR250800005", "BR123456793ABC", [
        {"name": "Boné Trucker", "variation": "Único", "quantity": 2},
        {"name": "Caneca Personalizada 300ml", "variation": "Preta", "quantity": 1},
    ]),
    ("BR250800006", "BR123456794ABC", [
        {"name": "Camiseta Básica Algodão", "variation": "Preto - M", "quantity": 1},
    ]),
]


def run():
    models.init_db()
    for order_sn, tracking, items in MOCK_ORDERS:
        if not models.get_order(order_sn):
            models.upsert_order(order_sn, tracking, items)
    print(f"{len(MOCK_ORDERS)} pedidos de exemplo carregados.")
    print("Códigos de rastreio para testar a tela de escaneamento:")
    for order_sn, tracking, _ in MOCK_ORDERS:
        print(f"  {tracking}  ->  {order_sn}")


if __name__ == "__main__":
    run()
