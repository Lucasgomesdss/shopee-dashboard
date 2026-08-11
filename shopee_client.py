"""
Cliente de integração com a Shopee Open Platform (API v2).

Este módulo cuida de:
- Montar a URL de autorização da loja (OAuth)
- Trocar o "code" de autorização por um access_token
- Assinar as requisições (obrigatório em toda chamada da API v2)
- Buscar pedidos (get_order_list) e detalhes dos pedidos (get_order_detail)

Documentação oficial: https://open.shopee.com/documents

IMPORTANTE: para isso funcionar de verdade você precisa:
1. Criar uma conta em https://open.shopee.com (Shopee Open Platform)
2. Criar um "App" e obter PARTNER_ID e PARTNER_KEY
3. Autorizar sua loja (o dono da loja precisa aprovar o acesso uma vez)
4. Preencher essas informações no arquivo .env (veja .env.example)

Enquanto você não tiver essas credenciais, o sistema roda em MODO DEMO
com pedidos de exemplo (veja seed_mock.py / config USE_MOCK_DATA).
"""

import hashlib
import hmac
import time
import requests

SHOPEE_HOST = "https://partner.shopeemobile.com"  # produção
# Para testes: "https://partner.test-stable.shopeemobile.com"


class ShopeeClient:
    def __init__(self, partner_id: str, partner_key: str, shop_id: str = None, access_token: str = None, host: str = SHOPEE_HOST):
        self.partner_id = int(partner_id) if partner_id else None
        self.partner_key = partner_key
        self.shop_id = int(shop_id) if shop_id else None
        self.access_token = access_token
        self.host = host
        self.session = requests.Session()  # reaproveita a conexão SSL entre chamadas (evita handshake novo a cada request)

    def _sign(self, path: str, timestamp: int, extra: str = ""):
        base_string = f"{self.partner_id}{path}{timestamp}{extra}"
        return hmac.new(
            self.partner_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def get_authorization_url(self, redirect_url: str) -> str:
        """Gera o link que o dono da loja precisa abrir para autorizar o app a acessar os pedidos."""
        path = "/api/v2/shop/auth_partner"
        timestamp = int(time.time())
        sign = self._sign(path, timestamp)
        return (
            f"{self.host}{path}?partner_id={self.partner_id}&timestamp={timestamp}"
            f"&sign={sign}&redirect={redirect_url}"
        )

    def get_access_token(self, code: str, shop_id: str):
        """Troca o 'code' recebido no /shopee/callback por um access_token + refresh_token."""
        path = "/api/v2/auth/token/get"
        timestamp = int(time.time())
        sign = self._sign(path, timestamp)
        url = f"{self.host}{path}?partner_id={self.partner_id}&timestamp={timestamp}&sign={sign}"
        body = {"code": code, "shop_id": int(shop_id), "partner_id": self.partner_id}
        resp = self.session.post(url, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def refresh_access_token(self, refresh_token: str, shop_id: str):
        """O access_token expira rápido (poucas horas). Use o refresh_token para renovar sem
        precisar autorizar de novo manualmente. O refresh_token dura mais (até 30 dias)."""
        path = "/api/v2/auth/access_token/get"
        timestamp = int(time.time())
        sign = self._sign(path, timestamp)
        url = f"{self.host}{path}?partner_id={self.partner_id}&timestamp={timestamp}&sign={sign}"
        body = {
            "refresh_token": refresh_token,
            "shop_id": int(shop_id),
            "partner_id": self.partner_id,
        }
        resp = self.session.post(url, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _shop_request(self, method: str, path: str, params: dict = None, json_body: dict = None):
        timestamp = int(time.time())
        extra = f"{self.access_token}{self.shop_id}"
        sign = self._sign(path, timestamp, extra)
        query = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "sign": sign,
            "shop_id": self.shop_id,
            "access_token": self.access_token,
        }
        if params:
            query.update(params)
        url = f"{self.host}{path}"
        if method == "GET":
            resp = self.session.get(url, params=query, timeout=20)
        else:
            resp = self.session.post(url, params=query, json=json_body or {}, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def get_order_list(self, time_from: int, time_to: int, cursor: str = "", order_status: str = "READY_TO_SHIP", page_size: int = 50, time_range_field: str = "create_time"):
        """Lista pedidos por período e status. Status úteis: READY_TO_SHIP, PROCESSED, SHIPPED, COMPLETED, CANCELLED.

        time_range_field: 'create_time' (quando o pedido foi feito) ou 'update_time' (quando mudou
        de status pela última vez). A Shopee só aceita até 15 dias de intervalo por chamada — quem
        chama essa função é responsável por dividir períodos maiores em janelas de 15 dias."""
        path = "/api/v2/order/get_order_list"
        params = {
            "time_range_field": time_range_field,
            "time_from": time_from,
            "time_to": time_to,
            "page_size": page_size,
            "cursor": cursor,
            "order_status": order_status,
        }
        return self._shop_request("GET", path, params=params)

    def get_order_detail(self, order_sn_list: list):
        """Detalhes de até 50 pedidos por chamada: itens, variação, quantidade, tracking_number etc."""
        path = "/api/v2/order/get_order_detail"
        params = {
            "order_sn_list": ",".join(order_sn_list),
            "response_optional_fields": "item_list,recipient_address,shipping_carrier,package_list",
        }
        return self._shop_request("GET", path, params=params)

    def get_tracking_number(self, order_sn: str, package_number: str = None):
        """Busca o código de rastreio de um pedido específico. Às vezes o get_order_detail
        não traz o tracking_number junto (só depois que a etiqueta é gerada), então usamos
        essa chamada dedicada como reforço."""
        path = "/api/v2/logistics/get_tracking_number"
        params = {"order_sn": order_sn}
        if package_number:
            params["package_number"] = package_number
        return self._shop_request("GET", path, params=params)
