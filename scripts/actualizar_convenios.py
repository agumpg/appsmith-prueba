"""Regenera data/convenios.json desde los CSV de publicidad activa de malaga.eu.

Las fuentes están listadas en references/enlaces-transparencia.md. El CSV de origen
trae fechas en formato M/D/YYYY, importes como texto ("20,000.00 €") y varias partes
dentro de una misma celda separadas por saltos de línea; aquí se normaliza todo eso.

Uso:
    python scripts/actualizar_convenios.py
"""

import csv
import io
import json
import re
import urllib.request
from pathlib import Path

FUENTES = {
    "2025": "https://www.malaga.eu/gobierno-abierto/.galleries/Convenios-Protocolos/2025/publicidad-activa/conv_01-01-2025_31-12-2025.csv",
    "2026": "https://www.malaga.eu/gobierno-abierto/.galleries/Convenios-Protocolos/2026/publicidad-activa/conv_01-01-2026_30-09-2026.csv",
}

DESTINO = Path(__file__).resolve().parent.parent / "data" / "convenios.json"


def a_iso(valor):
    """M/D/YYYY -> YYYY-MM-DD (ordenable alfabéticamente)."""
    valor = (valor or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", valor)
    if not m:
        return valor
    mes, dia, anio = m.groups()
    return f"{anio}-{int(mes):02d}-{int(dia):02d}"


def a_numero(valor):
    """'20,000.00 €' -> 20000.0"""
    limpio = (valor or "").replace("€", "").replace(",", "").strip()
    try:
        return round(float(limpio), 2)
    except ValueError:
        return 0.0


def descargar(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8-sig")


def main():
    filas = []
    for anio, url in FUENTES.items():
        for r in csv.DictReader(io.StringIO(descargar(url))):
            partes = [p.strip() for p in r["Partes"].split("\n") if p.strip()]
            otras = [p for p in partes if "Ayuntamiento" not in p]
            filas.append(
                {
                    "anio": anio,
                    "num": r["Núm. de inscripción definitiva"].strip(),
                    "tipo": r["Tipo"].strip(),
                    "objeto": r["Objeto"].strip(),
                    "partes": " / ".join(otras) or "—",
                    "formaliza": a_iso(r["Fecha de formalización"]),
                    "inicio": a_iso(r["Duración: fecha inicio vigencia"]),
                    "fin": a_iso(r["Duración: fecha fin vigencia"]),
                    "importe": a_numero(r["Importe de los compromisos del Ayuntamiento"]),
                    "modif": r["Modificaciones"].strip(),
                    "pdf": r["Enlace a texto"].strip(),
                }
            )

    DESTINO.parent.mkdir(exist_ok=True)
    DESTINO.write_text(
        json.dumps(filas, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    total = sum(f["importe"] for f in filas)
    print(f"{len(filas)} registros -> {DESTINO}")
    print(f"importe comprometido: {total:,.2f} €")


if __name__ == "__main__":
    main()
