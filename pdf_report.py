"""Geração do PDF de pré-separação: soma a quantidade de cada produto/variação
entre todos os pedidos ainda em aberto (a separar + pendentes)."""

from io import BytesIO
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet


def build_picking_list_pdf(rows):
    """rows: lista de ((nome_produto, variacao), quantidade_total), já ordenada."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    elements = []

    title = Paragraph("Lista de Pré-Separação", styles["Title"])
    subtitle = Paragraph(
        f"Gerado em {datetime.now().strftime('%d/%m/%Y às %H:%M')} — pedidos em aberto",
        styles["Normal"],
    )
    elements += [title, subtitle, Spacer(1, 0.6 * cm)]

    if not rows:
        elements.append(Paragraph("Nenhum pedido em aberto no momento.", styles["Normal"]))
    else:
        data = [["Produto", "Variação", "Quantidade total"]]
        total_geral = 0
        for (name, variation), qty in rows:
            data.append([name or "-", variation or "-", str(qty)])
            total_geral += qty
        data.append(["", "TOTAL", str(total_geral)])

        table = Table(data, colWidths=[9 * cm, 5 * cm, 3.5 * cm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EE4D2D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#FFF3EF")]),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FFE4DB")),
            ("ALIGN", (2, 0), (2, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(table)

    doc.build(elements)
    buffer.seek(0)
    return buffer
