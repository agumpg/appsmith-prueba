"""Regenera data/convenios.json y data/clasificacion.json desde los CSV de malaga.eu.

Las fuentes están listadas en references/enlaces-transparencia.md. El CSV de origen
trae fechas en formato M/D/YYYY, importes como texto ("20,000.00 €") y varias partes
dentro de una misma celda separadas por saltos de línea; aquí se normaliza todo eso.

Además calcula la clasificación por entidad firmante (nº de convenios e importe).
El CSV no publica el área de gobierno ni el distrito que tramita cada convenio, así
que la unidad de agregación disponible es la entidad con la que firma el Ayuntamiento.

Uso:
    python scripts/actualizar_convenios.py
"""

import csv
import io
import json
import re
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

FUENTES = {
    "2025": "https://www.malaga.eu/gobierno-abierto/.galleries/Convenios-Protocolos/2025/publicidad-activa/conv_01-01-2025_31-12-2025.csv",
    "2026": "https://www.malaga.eu/gobierno-abierto/.galleries/Convenios-Protocolos/2026/publicidad-activa/conv_01-01-2026_30-09-2026.csv",
}

DATOS = Path(__file__).resolve().parent.parent / "data"

# El propio Ayuntamiento de Málaga firma todos los convenios: no es "la otra parte".
ES_AYTO_MALAGA = re.compile(r"^ayuntamiento\s+(de\s+)?m[aá]l(a)?ga", re.I)

# Palabras vacías y sufijos societarios que no distinguen a una entidad.
VACIAS = {"de", "del", "la", "el", "los", "las", "y", "e", "para", "en", "a"}
SUFIJOS = {"sl", "sa", "slu", "sau", "scoop", "sll", "sociedad", "anonima", "municipal"}

# El origen escribe varias veces la misma entidad de formas distintas. Estas reglas
# unifican los casos que la normalización genérica no alcanza (siglas, erratas,
# cambios de denominación). Se evalúan sobre el nombre sin acentos y en minúsculas.
CANONICAS = [
    (r"emtsam|empresa\s+(malaguena|municipal)\s+de\s+transportes", "Empresa Malagueña de Transportes (EMTSAM)"),
    (r"prosalud", "Asociación para la Prevención y Promoción de la Salud Comunitaria (PROSALUD)"),
    (r"fede?a?racion\s+andaluza\s+de\s+natacion", "Federación Andaluza de Natación"),
    (r"diputacion\s+(provincial\s+)?(de\s+)?malaga", "Diputación Provincial de Málaga"),
    (r"fundacion\s+(bancaria\s+)?unicaja", "Fundación Unicaja"),
    (r"asociacion\s+civica\s+para\s+la\s+prevencion", "Asociación Cívica para la Prevención (ACP)"),
    (r"comision\s+espanola\s+de\s+ayuda\s+al\s+refugiado", "Comisión Española de Ayuda al Refugiado (CEAR)"),
    (r"telefono\s+(de\s+)?l?a?\s*esperanza", "Asociación Internacional del Teléfono de la Esperanza (ASITES)"),
    (r"centro\s+superior\s+de\s+formacion\s+europa\s+sur", "Centro Superior de Formación Europa Sur (CESUR)"),
    (r"consejo\s+regulador", "Consejo Regulador de las D.O. Málaga, Sierras de Málaga y Pasas de Málaga"),
    (r"musical\s+san\s+isidro\s+labrador", "Asociación Musical San Isidro Labrador de Churriana"),
    (r"diversidad\s+(fisi?.?quica|funcional).*serran", "Asociación de Padres de Personas con Diversidad Funcional de la Serranía (Churriana)"),
    (r"producciones\s+mic", "Producciones MIC, S.L."),
    (r"universidad\s+de\s+malaga", "Universidad de Málaga"),
]


# --- Clasificación temática -------------------------------------------------
# NO es un dato oficial: el CSV no publica el área de gobierno ni el distrito que
# tramita cada convenio. Esta clasificación la deduce el script por palabras clave
# del objeto y, si el objeto no dice nada (nombres de proyecto como "La Brújula de
# la Esperanza"), por el tipo de entidad firmante. Lo que no encaja se queda en
# "Sin clasificar" a propósito: es más honesto que forzarlo a un cajón.
# Las reglas se evalúan en orden: la primera que coincide gana.
AREAS_OBJETO = [
    ("Seguridad y emergencias", r"policia|bomber|emergencia|proteccion civil|salvamento|socorrismo|seguridad vial|cuerpo nacional"),
    ("Fiestas y tradiciones", r"cofrad|semana santa|carnaval|verdiales|feria de malaga|feria malaga|caseta|belen|cabalgata|reyes magos|romeria|folclor|fiesta|rengue|cruces de mayo|procesion|trono|ornamentacion|santisima|virgen|cristo|hermandad"),
    ("Deporte", r"deport|natacion|futbol|baloncesto|gimnasia|atletismo|maraton|ciclis|padel|tenis|balonmano|voley|ajedrez|piragu|escalada|surf|boxeo|futsal|hockey|triatlon|petanca|karate|judo|esgrima|remo|halterof|entrenamientos y partidos"),
    ("Movilidad y transporte", r"transporte|movilidad|bicicleta|emtsam|autobus|aparcamiento|trafico|titulos de viaje|precio de los abonos|bonificaciones en el precio"),
    ("Salud, consumo y adicciones", r"salud|sanitar|adicion|adiccion|drogodepend|alcohol|tabaquismo|donacion de sangre|hospital|paliativos|cancer|alzheimer|consumo|arbitraje|prevencion universal|ocio nocturno|mediadores preventivos"),
    ("Mayores", r"mayores|tercera edad|envejecimiento|centro de dia|siempre acompanados"),
    ("Igualdad y mujer", r"igualdad|mujer|violencia de genero|lgtbi|feminis|conciliacion|punto violeta"),
    ("Derechos sociales e inclusión", r"social|inclusion|vulnerab|pobreza|exclusion|sin hogar|inmigra|refugiado|acoge|acogida|discapacidad|diversidad funcional|dependencia|voluntariado|familias|infancia|menores|adopcion|banco de alimentos|comedor|solidaridad|gitano|reparto de alimentos|personas en situacion|sindrome de down|extranjeras"),
    ("Educación y formación", r"educa|escolar|escuela infantil|universidad|catedra|formacion profesional|formativas|practicas remuneradas|ensenanzas|instituto|beca|aula|alumn|docente|ciclo formativo|\bformacion|cursos de verano|albaneria|albanileria|malaga 42"),
    ("Juventud", r"juvenil|juventud|joven"),
    ("Cultura y patrimonio", r"cultur|museo|teatro|cine|festival|libro|biblioteca|music|orquesta|flamenco|arte|artist|patrimonio|arqueolog|excavacion|exposicion|literat|poes|danza|banda|coro|periodismo|premio|certamen|audiovisual|fotograf|comic|zarzuela|cante|concier|recital|noche en blanco|pasarela|fashion"),
    ("Turismo y promoción de la ciudad", r"turis|congres|promocion de la ciudad|gastronom|vino|cerveza|marca malaga|rodaje|seatrade|vitur|hosteleria|protour|agencias de viajes|malaga is here"),
    ("Empleo, empresa e innovación", r"empleo|emprend|empresa|comercio|industria|innovacion|tecnolog|startup|economia|mercado|microelectronica|inteligencia artificial|incubadora|polo digital"),
    ("Medio ambiente y sostenibilidad", r"medio ambiente|sostenib|energia|climatic|residuo|reciclaj|arbol|parque|jardin|animal|zoosanitario|playa|litoral|huerto|biodiversidad|contaminacion|corredores verdes|mas bella|aguas regeneradas|ciclo del agua|abastecimiento|saneamiento"),
    ("Urbanismo y vivienda", r"urbanis|vivienda|rehabilitacion|suelo|edificacion|obras|infraestructura|accesibilidad|alquiler de espacios|cesion de uso|solar"),
    ("Participación ciudadana y distritos", r"participacion|distrito|vecin|asociacionismo|barriada|convivencia"),
    ("Cooperación internacional y proyectos europeos", r"cooperacion|hermanamiento|internacional|ods|agenda 2030|agenda urbana|accion exterior|metropolis global|europa eres tu|urbact|interreg|aduanera|tolerancia|femp"),
    ("Administración y transformación digital", r"administracion electronica|transparencia|tributar|recaudacion|padron|estadistica|datos abiertos|interoperabilidad|sede electronica|ciberseguridad|ciudad inteligente|territorios inteligentes|agenda digital|smart city|software|nextauditool|programa matrix|encomienda"),
]

# Segunda pasada: cuando el objeto es sólo el nombre del proyecto, el tipo de
# entidad firmante sí dice de qué va.
AREAS_ENTIDAD = [
    ("Fiestas y tradiciones", r"cofradia|hermandad|santisima|pena el|pena la|penas|verdiales|agrupacion de cofradias"),
    ("Deporte", r"\bclub\b|deportiv|balonmano|baloncesto|natacion|futbol"),
    ("Cultura y patrimonio", r"conservatorio|musical|orquesta|\bcoro\b|teatro|museo|cinemateca|banda"),
    ("Derechos sociales e inclusión", r"caritas|cruz roja|acoge|solidaridad|sindrome de down|ronald mcdonald|banco solidario|nena paine|marroqui|esperanza|cesma|dosta|arrabal"),
    ("Juventud", r"juvenil"),
    ("Igualdad y mujer", r"gender|50&50"),
    ("Educación y formación", r"universidad|conservatorio superior|centro superior de formacion"),
    ("Turismo y promoción de la ciudad", r"hosteleria|agencias de viajes|vitur|malagaport|cervezas|mahou"),
    ("Empleo, empresa e innovación", r"smart city|telefonica|cluster|asociacion de comercios|comerciantes|empresarios"),
    ("Participación ciudadana y distritos", r"vecinal|vecinos|asociacion de mujeres"),
]


def area_tematica(objeto, entidades):
    """Área temática deducida del objeto y, en su defecto, de la entidad firmante."""
    plano_objeto = sin_acentos(objeto)
    for nombre, patron in AREAS_OBJETO:
        if re.search(patron, plano_objeto):
            return nombre
    plano_entidades = sin_acentos(" / ".join(entidades))
    for nombre, patron in AREAS_ENTIDAD:
        if re.search(patron, plano_entidades):
            return nombre
    return "Sin clasificar"


def sin_acentos(valor):
    descompuesto = unicodedata.normalize("NFKD", valor.lower())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def clave(nombre):
    """Clave laxa que agrupa las variantes tipográficas de una misma entidad."""
    palabras = [p for p in re.split(r"[^a-z0-9]+", sin_acentos(nombre)) if p]
    palabras = [p for p in palabras if p not in VACIAS and p not in SUFIJOS]
    return " ".join(palabras)


def canonica(nombre):
    plano = sin_acentos(nombre)
    for patron, destino in CANONICAS:
        if re.search(patron, plano):
            return destino
    return None


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


def leer_filas():
    filas = []
    for anio, url in FUENTES.items():
        for r in csv.DictReader(io.StringIO(descargar(url))):
            partes = [p.strip() for p in r["Partes"].split("\n") if p.strip()]
            otras = [p for p in partes if not ES_AYTO_MALAGA.match(p)]
            filas.append(
                {
                    "anio": anio,
                    "num": r["Núm. de inscripción definitiva"].strip(),
                    "tipo": r["Tipo"].strip(),
                    "objeto": r["Objeto"].strip(),
                    "area": area_tematica(r["Objeto"], otras),
                    "partes": " / ".join(otras) or "—",
                    "formaliza": a_iso(r["Fecha de formalización"]),
                    "inicio": a_iso(r["Duración: fecha inicio vigencia"]),
                    "fin": a_iso(r["Duración: fecha fin vigencia"]),
                    "importe": a_numero(r["Importe de los compromisos del Ayuntamiento"]),
                    "modif": r["Modificaciones"].strip(),
                    "pdf": r["Enlace a texto"].strip(),
                    "_entidades": otras,
                }
            )
    return filas


def clasificar(filas):
    """Agrega por entidad firmante.

    Un convenio firmado con varias entidades cuenta (y suma su importe) en cada una
    de ellas, así que los totales de la clasificación no cuadran con el total de
    convenios. Son 11 registros de 324.
    """
    grafias = defaultdict(Counter)   # clave -> grafías vistas, para elegir la más frecuente
    conv = Counter()
    imp = Counter()
    for fila in filas:
        for bruto in fila["_entidades"]:
            fijo = canonica(bruto)
            k = clave(fijo or bruto)
            grafias[k][fijo or re.sub(r"\s+", " ", bruto)] += 1
            conv[k] += 1
            imp[k] += fila["importe"]

    entidades = []
    for k, veces in conv.items():
        # Nombre a mostrar: la grafía más repetida (a igualdad, la más larga).
        nombre = sorted(grafias[k].items(), key=lambda x: (-x[1], -len(x[0])))[0][0]
        entidades.append({"entidad": nombre, "convenios": veces, "importe": round(imp[k], 2)})

    # Clasificación temática: aquí cada convenio cuenta una sola vez.
    areas_conv = Counter()
    areas_imp = Counter()
    for fila in filas:
        areas_conv[fila["area"]] += 1
        areas_imp[fila["area"]] += fila["importe"]
    areas = [
        {"area": a, "convenios": n, "importe": round(areas_imp[a], 2)}
        for a, n in areas_conv.items()
    ]

    return {
        "porNumero": sorted(entidades, key=lambda e: (-e["convenios"], -e["importe"], e["entidad"])),
        "porImporte": sorted(entidades, key=lambda e: (-e["importe"], -e["convenios"], e["entidad"])),
        "porArea": sorted(areas, key=lambda a: (-a["convenios"], -a["importe"], a["area"])),
        "porAreaImporte": sorted(areas, key=lambda a: (-a["importe"], -a["convenios"], a["area"])),
    }


def escribir(nombre, contenido):
    destino = DATOS / nombre
    destino.write_text(
        json.dumps(contenido, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return destino


def main():
    filas = leer_filas()
    clasificacion = clasificar(filas)
    for fila in filas:
        del fila["_entidades"]

    escribir("convenios.json", filas)
    escribir("clasificacion.json", clasificacion)

    total = sum(f["importe"] for f in filas)
    print(f"{len(filas)} registros, importe comprometido {total:,.2f} €")
    print(f"{len(clasificacion['porNumero'])} entidades firmantes")
    for e in clasificacion["porNumero"][:5]:
        print(f"  {e['convenios']:3} convenios   {e['entidad']}")
    for e in clasificacion["porImporte"][:5]:
        print(f"  {e['importe']:>16,.2f} EUR   {e['entidad']}")
    print(f"{len(clasificacion['porArea'])} areas tematicas")
    for a in clasificacion["porArea"]:
        print(f"  {a['convenios']:3} convenios  {a['importe']:>16,.2f} EUR   {a['area']}")


if __name__ == "__main__":
    main()
