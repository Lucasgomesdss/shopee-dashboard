"""Geração do PDF de pré-separação: soma a quantidade de cada produto/variação
entre todos os pedidos ainda em aberto (a separar + pendentes), com a foto de cada
produto (a mesma cadastrada na Shopee) para facilitar a conferência visual no estoque.
Quantidades maiores que 1 aparecem destacadas em vermelho."""

from io import BytesIO
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet

IMG_SIZE = 1.8 * cm

# Cache em memória das fotos já baixadas (dura enquanto o processo do servidor estiver
# de pé). Os mesmos produtos aparecem de novo a cada PDF gerado, então isso evita
# baixar a mesma imagem repetidas vezes e deixa as próximas gerações bem mais rápidas.
_image_bytes_cache = {}
_image_session = requests.Session()


def _download_image_bytes(url):
    """Baixa os bytes de uma foto (com cache). Se não tiver imagem ou o download
    falhar (link quebrado, timeout), volta None e a linha fica só com o texto."""
    if not url:
        return None
    if url in _image_bytes_cache:
        return _image_bytes_cache[url]
    try:
        resp = _image_session.get(url, timeout=5)
        resp.raise_for_status()
        _image_bytes_cache[url] = resp.content
        return resp.content
    except Exception:
        _image_bytes_cache[url] = None
        return None


def _bytes_to_image(content, size=IMG_SIZE):
    if not content:
        return None
    try:
        img = Image(BytesIO(content), width=size, height=size)
        img.hAlign = "CENTER"
        return img
    except Exception:
        return None


def build_picking_list_pdf(rows, title="Lista de Pré-Separação", subtitle="pedidos em aberto"):
    """rows: lista de dicts com name, variation, quantity, image_url — já ordenada
    pela quantidade total (maior primeiro). title/subtitle permitem reaproveitar essa
    mesma função pro PDF de Produto Pendente (itens que faltaram na separação).

    As fotos são baixadas em paralelo (até 10 por vez) antes de montar a tabela —
    baixar uma por uma deixava o PDF muito lento quando havia muitos produtos
    diferentes na lista (cada download é uma chamada de rede separada pra Shopee)."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    elements = []

    title_p = Paragraph(title, styles["Title"])
    subtitle_p = Paragraph(
        f"Gerado em {datetime.now().strftime('%d/%m/%Y às %H:%M')} — {subtitle}",
        styles["Normal"],
    )
    elements += [title_p, subtitle_p, Spacer(1, 0.6 * cm)]

    if not rows:
        elements.append(Paragraph("Nenhum item encontrado no momento.", styles["Normal"]))
    else:
        cell_style = styles["Normal"]

        # Baixa todas as fotos em paralelo antes de montar a tabela.
        urls = [row.get("image_url") for row in rows]
        with ThreadPoolExecutor(max_workers=10) as executor:
            images_bytes = list(executor.map(_download_image_bytes, urls))

        data = [["Foto", "Produto", "Variação", "Qtd"]]
        qty_alert_rows = []  # linhas (índice na tabela) com quantidade > 1, para destacar em vermelho
        total_geral = 0
        for i, (row, img_bytes) in enumerate(zip(rows, images_bytes), start=1):
            img = _bytes_to_image(img_bytes)
            data.append([
                img or "sem foto",
                Paragraph(row["name"] or "-", cell_style),
                Paragraph(row["variation"] or "-", cell_style),
                str(row["quantity"]),
            ])
            total_geral += row["quantity"]
            if row["quantity"] > 1:
                qty_alert_rows.append(i)
        data.append(["", "", "TOTAL", str(total_geral)])

        table = Table(data, colWidths=[2.4 * cm, 7.1 * cm, 4.5 * cm, 2.5 * cm], repeatRows=1)
        style_commands = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EE4D2D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#FFF3EF")]),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FFE4DB")),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (3, 0), (3, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
        # Destaca em vermelho a quantidade de itens vendidos em mais de 1 unidade no
        # mesmo pedido, para o colaborador não esquecer de pegar mais de uma peça.
        for row_idx in qty_alert_rows:
            style_commands.append(("TEXTCOLOR", (3, row_idx), (3, row_idx), colors.red))
            style_commands.append(("FONTSIZE", (3, row_idx), (3, row_idx), 13))
            style_commands.append(("BOX", (3, row_idx), (3, row_idx), 1.2, colors.red))
        table.setStyle(TableStyle(style_commands))
        elements.append(table)

    doc.build(elements)
    buffer.seek(0)
    return buffer
