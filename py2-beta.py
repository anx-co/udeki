"""
Notas Udeki -> sitio web estático.

Recorre TODOS los IDs de info.txt (formato: ID,correo,contraseña), uno detrás
de otro, captura las calificaciones de los 4 periodos y la foto de perfil, y
genera, TODO EN LA MISMA CARPETA DONDE ESTÁ ESTE ARCHIVO:

    index.html            Tabla general con todos los estudiantes
    estudiantes/<ID>.html Detalle de cada estudiante (enlazado desde el índice)
    datos/<ID>.json       Respuestas crudas de Udeki (sirven para reconstruir)
    icons/<ID>.<ext>      Foto de perfil descargada (solo si el perfil tiene una)
    pdf/<ID>.pdf          Solo si GENERAR_PDF = True

Uso:
    python notas_web.py                 # todos los IDs de info.txt
    python notas_web.py 111 222         # solo esos IDs (reintentos)
    python notas_web.py --reconstruir   # rehace las páginas desde datos/*.json, sin entrar a Udeki

Al iniciar verifica playwright y Chromium: si ya funcionan se salta la instalación;
si falta algo, lo instala solo.

Abre index.html con doble clic: no necesita servidor.
"""

import argparse
import asyncio
import html
import importlib
import importlib.util
import json
import mimetypes
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

# ----------------------------------------------------------------------------
# CONFIGURACIÓN RÁPIDA
# ----------------------------------------------------------------------------
# Carpeta raíz = carpeta donde vive este .py. Aquí se lee info.txt y aquí se
# escriben index.html, estudiantes/, datos/, icons/ y pdf/.
BASE = Path(__file__).resolve().parent

ARCHIVO_INFO = "info.txt"
CARPETA_SALIDA = BASE
GENERAR_PDF = False          # True = además genera un PDF por estudiante
HEADLESS = True              # False = ver el navegador mientras trabaja
TIMEOUT_PERIODOS = 90        # segundos máximos esperando los 4 periodos por estudiante
PAUSA_ENTRE_ESTUDIANTES = 2  # segundos entre un estudiante y el siguiente

URL_LOGIN = "https://app.udeki.com/login"
URL_DASHBOARD = "https://app.udeki.com/dashboard"
URL_DESEMPENIO = "https://app.udeki.com/desempenio/estudiante"

PERIODOS = ["PRIMER PERIODO", "SEGUNDO PERIODO", "TERCER PERIODO", "CUARTO PERIODO"]
NIVELES = ["SUPERIOR", "ALTO", "BÁSICO", "BAJO"]

# Selector de la foto de perfil en la barra superior y su imagen por defecto.
SELECTOR_AVATAR = "#img_avatar_auth, .avatar img"
AVATAR_POR_DEFECTO = "img/user.png"
TAMANO_MIN_ICONO = 300       # bytes mínimos para dar la descarga por válida

ESCALA_VALORACION = {
    "BAJO": {"color": "#C00000"},
    "BÁSICO": {"color": "#E1A707"},
    "ALTO": {"color": "#4CB050"},
    "SUPERIOR": {"color": "#9350AD"},
}


# ----------------------------------------------------------------------------
# DEPENDENCIAS (se verifican al iniciar; solo instala lo que falte)
# ----------------------------------------------------------------------------
def _pip_instalar(paquete):
    """Instala un paquete con pip; si el sistema lo bloquea, reintenta con --break-system-packages."""
    base = [sys.executable, "-m", "pip", "install", paquete]
    try:
        subprocess.check_call(base)
    except subprocess.CalledProcessError:
        subprocess.check_call(base + ["--break-system-packages"])
    importlib.invalidate_caches()


def _playwright_importable():
    try:
        return importlib.util.find_spec("playwright") is not None and importlib.import_module("playwright.sync_api") is not None
    except Exception:
        return False


def _chromium_funciona():
    """Prueba real: si Chromium abre, sirve (sin importar la versión)."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


def verificar_dependencias():
    """Si todo ya funciona se salta la instalación y arranca el proceso."""
    print("🔎 Verificando dependencias...")

    if _playwright_importable():
        print("   ✔ playwright: ya instalado")
    else:
        print("   ⬇️ playwright: no está, instalando...")
        try:
            _pip_instalar("playwright")
        except Exception as ex:
            print(f"   ❌ No se pudo instalar playwright ({ex}).")
            print("      Instálalo a mano con:  pip install playwright")
            sys.exit(1)
        print("   ✔ playwright: instalado")

    if _chromium_funciona():
        print("   ✔ Chromium: ya disponible")
    else:
        print("   ⬇️ Chromium: no está, descargando (puede tardar unos minutos)...")
        try:
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        except Exception as ex:
            print(f"   ❌ No se pudo descargar Chromium ({ex}).")
            print("      Hazlo a mano con:  python -m playwright install chromium")
            sys.exit(1)
        if not _chromium_funciona():
            print("   ❌ Chromium se descargó pero no abre. Revisa los mensajes de arriba.")
            sys.exit(1)
        print("   ✔ Chromium: listo")

    print("✅ Dependencias listas.\n")


# ----------------------------------------------------------------------------
# REGLAS DE NEGOCIO
# ----------------------------------------------------------------------------
def convertir_nota_a_nivel(nota_val):
    """Convierte la nota numérica a su categoría en texto."""
    try:
        val = float(nota_val)
        if val < 3.0:
            return "BAJO"
        elif val < 4.0:
            return "BÁSICO"
        elif val < 4.6:
            return "ALTO"
        else:
            return "SUPERIOR"
    except (ValueError, TypeError):
        texto = str(nota_val).upper()
        if "BASICO" in texto or "BÁSICO" in texto:
            return "BÁSICO"
        return texto if texto in ESCALA_VALORACION else "BÁSICO"


def meta_80(total):
    """80% del total. Cualquier decimal (0,1 a 0,9) sube al siguiente entero;
    si el resultado es exacto (0,0) se queda igual.
    Se calcula con enteros (total*8 / 10) para evitar errores de coma flotante."""
    base, resto = divmod(total * 8, 10)
    return base + 1 if resto >= 1 else base


def claves_ordenadas(registros):
    return sorted(registros.keys(), key=lambda x: int(x.split("_")[1]))


def calcular_matriz(registros):
    """{área: {periodo: {nivel: conteo}}} contando indicadores."""
    matriz = {}
    for p_key in claves_ordenadas(registros):
        num_p = int(p_key.split("_")[1])
        if not 1 <= num_p <= 4:
            continue
        nombre_periodo = PERIODOS[num_p - 1]
        for mat in registros[p_key].get("modelscal", []) or []:
            nombre_mat = (mat.get("nombre") or "OTRA ÁREA").strip()
            if nombre_mat not in matriz:
                matriz[nombre_mat] = {p: {n: 0 for n in NIVELES} for p in PERIODOS}
            for ind in mat.get("indicadores", []) or []:
                nivel = convertir_nota_a_nivel(ind.get("nota", "3.0"))
                if nivel in NIVELES:
                    matriz[nombre_mat][nombre_periodo][nivel] += 1
    return matriz


def calcular_resumen(matriz):
    """Por área: (tengo, total) de cada periodo + acumulado anual contra el 80%."""
    resumen = {}
    for area in sorted(matriz):
        por_periodo = {}
        tengo_anual = total_anual = 0
        for p in PERIODOS:
            c = matriz[area][p]
            total = sum(c.values())
            tengo = total - c["BAJO"]  # solo BÁSICO, ALTO y SUPERIOR
            por_periodo[p] = (tengo, total)
            tengo_anual += tengo
            total_anual += total
        meta = meta_80(total_anual)
        resumen[area] = {
            "periodos": por_periodo,
            "tengo": tengo_anual,
            "total": total_anual,
            "meta": meta,
            "ok": tengo_anual >= meta,  # igual a la meta cuenta como verde
        }
    return resumen


def datos_estudiante(id_est, registros):
    claves = claves_ordenadas(registros)
    base = registros[claves[0]] if claves else {}
    est = base.get("std", {}) or {}
    nombres = est.get("nombres", "")
    apellidos = est.get("apellidos", "")
    nombre = (
        est.get("nombrecompleto")
        or f"{nombres} {apellidos}".strip()
        or "No disponible"
    )
    grado_raw = base.get("grado", "Media Vocacional")
    grado = (
        grado_raw.get("nombre", "Media Vocacional")
        if isinstance(grado_raw, dict)
        else str(grado_raw)
    )
    return {
        "nombre": nombre,
        "identificacion": est.get("identificacion") or est.get("documento") or "No disponible",
        "nacimiento": est.get("fechadenacimiento") or est.get("fecha_nacimiento") or "No disponible",
        "genero": est.get("genero", "No disponible"),
        "nui": est.get("nui") or base.get("nui", "No disponible"),
        "id_est": est.get("id") or est.get("idEstudiante") or base.get("idEstudiante", id_est),
        "grado": grado,
    }


def archivo_seguro(id_est):
    return re.sub(r"[^\w.-]", "_", str(id_est))


def clave_natural(x):
    return (0, int(x), x) if str(x).isdigit() else (1, 0, str(x))


def construir_entrada(id_est, registros, fecha, icono=None):
    matriz = calcular_matriz(registros)
    resumen = calcular_resumen(matriz)
    return {
        "id": str(id_est),
        "archivo": archivo_seguro(id_est),
        "fecha": fecha,
        "registros": registros,
        "info": datos_estudiante(id_est, registros),
        "matriz": matriz,
        "resumen": resumen,
        "rojos": sum(1 for r in resumen.values() if not r["ok"]),
        "n_periodos": len(registros),
        "icono": icono if icono and (CARPETA_SALIDA / "icons" / icono).exists() else None,
    }


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
def esc(v):
    return html.escape(str(v))


CSS = """
:root{
--bg:#eaf1fb;--card:#ffffff;--ink:#12203d;--muted:#566784;--line:#d3ddee;--head:#e2eaf7;--accent:#2563eb;
--sup:#9350AD;--alto:#4CB050;--bas:#E1A707;--bajo:#C00000;
--okc:#1fa25a;--ok-bg:#d5f4e2;--ok-fg:#08683a;
--badc:#e5384c;--bad-bg:#fddfe3;--bad-fg:#a1111f;
--warn-bg:#ffeab0;--warn-fg:#6b4700;
--p1:#2563eb;--p2:#0891b2;--p3:#0d9488;--p4:#4f46e5;
--shadow:0 1px 2px rgba(18,32,61,.08),0 6px 18px rgba(18,32,61,.07)}
@media (prefers-color-scheme:dark){:root{
--bg:#0a1020;--card:#121a2e;--ink:#e8eefc;--muted:#94a3c4;--line:#25345a;--head:#1b2746;--accent:#6ea8ff;
--ok-bg:#0e3b29;--ok-fg:#7ef0b0;--bad-bg:#4a1620;--bad-fg:#ff9eab;--warn-bg:#44330a;--warn-fg:#ffd36b;
--shadow:0 1px 2px rgba(0,0,0,.45),0 8px 22px rgba(0,0,0,.35)}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0;padding:14px;
background:var(--bg);color:var(--ink);line-height:1.4;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.wrap{max-width:1120px;margin:0 auto}
a{color:var(--accent)}
h2{font-size:17px;margin:28px 0 12px;display:flex;align-items:center;gap:9px}
h2::before{content:"";width:6px;height:20px;border-radius:3px;background:linear-gradient(var(--p1),var(--p3))}
.sub{color:var(--muted);font-size:13px;margin:0}

/* Encabezado */
.hero{background:linear-gradient(115deg,#0a2559 0%,#1558c0 55%,#0f9ab0 100%);color:#fff;border-radius:18px;
padding:18px;margin-bottom:16px;box-shadow:var(--shadow)}
.hero h1{margin:0;font-size:25px;letter-spacing:-.01em;line-height:1.15}
.hero .sub{color:rgba(255,255,255,.82);margin-top:4px}
.hero-top{display:flex;gap:14px;align-items:center}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:16px}
.stats.c3{grid-template-columns:repeat(3,1fr)}
@media (min-width:640px){.stats.c4{grid-template-columns:repeat(4,1fr)}}
.stat{appearance:none;border:0;border-radius:12px;padding:10px 13px;text-align:left;color:#fff;font:inherit;
background:rgba(255,255,255,.17);outline:2px solid transparent;outline-offset:2px}
button.stat{cursor:pointer}
.stat b{display:block;font-size:27px;line-height:1.1}
.stat span{font-size:12px;line-height:1.25;display:block}
.stat.s-ok{background:#1fa25a}.stat.s-bad{background:#e5384c}.stat.s-inc{background:#f2a900;color:#2b1d00}
.stat.act{outline-color:#fff}

/* Avatar: iniciales o foto de perfil */
.av{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:50%;
color:#fff;font-weight:700;font-size:13px;flex:none;background:#7c8aa5;overflow:hidden}
.av.grande{width:56px;height:56px;font-size:21px;box-shadow:0 0 0 3px rgba(255,255,255,.45)}
img.av{object-fit:cover;object-position:center;display:block;background:#7c8aa5}
img.av.grande{box-shadow:0 0 0 3px rgba(255,255,255,.45)}

/* Navegación y datos */
.nav{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;font-size:13px}
.nav a{padding:7px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);
text-decoration:none;box-shadow:var(--shadow)}
.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.info-item{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px 12px;box-shadow:var(--shadow)}
.info-item small{display:block;color:var(--muted);font-size:11px}
.info-item b{font-size:14px;word-break:break-word}

/* Tablas */
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:14px;box-shadow:var(--shadow);background:var(--card)}
table{border-collapse:collapse;background:var(--card);width:100%}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:center;font-size:13px}
th{background:var(--head);font-weight:700}
td.area,td.izq,th.izq{text-align:left}
th.p1,th.p2,th.p3,th.p4{color:#fff}
th.p1{background:var(--p1)}th.p2{background:var(--p2)}th.p3{background:var(--p3)}th.p4{background:var(--p4)}
th.tot{background:#122b5c;color:#fff}
.ok{background:var(--ok-bg);color:var(--ok-fg);font-weight:700}
.bad{background:var(--bad-bg);color:var(--bad-fg);font-weight:700}
td.area.s-ok{box-shadow:inset 5px 0 0 var(--okc)}
td.area.s-bad{box-shadow:inset 5px 0 0 var(--badc)}
.meta{display:block;font-weight:400;font-size:11px;opacity:.85}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700;margin:3px 4px 0 0}
.pill.warn{background:var(--warn-bg);color:var(--warn-fg)}

/* Barras: avance hacia el 80% y distribución por nivel */
.bar{position:relative;height:6px;border-radius:3px;background:rgba(120,135,165,.3);margin-top:5px;min-width:56px}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
.bar.si i{background:var(--okc)}.bar.no i{background:var(--badc)}
.bar b{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink);opacity:.75;border-radius:1px}
.stack{display:flex;height:5px;border-radius:3px;overflow:hidden;margin-top:5px;min-width:46px;background:rgba(120,135,165,.22)}
.stack i{display:block;height:100%}
.sup{background:var(--sup)}.alto{background:var(--alto)}.bas{background:var(--bas)}.bajo{background:var(--bajo)}
.lg{display:inline-block;width:10px;height:10px;border-radius:3px;margin:0 5px 0 12px;vertical-align:-1px}

/* Consolidado por niveles */
.tabla-matriz th,.tabla-matriz td{font-size:11px;padding:5px 3px}
.tabla-matriz th.lv{color:#fff;font-size:10px}
.tabla-matriz th.lv-sup{background:var(--sup)}.tabla-matriz th.lv-alto{background:var(--alto)}
.tabla-matriz th.lv-bas{background:var(--bas);color:#2b1d00}.tabla-matriz th.lv-bajo{background:var(--bajo)}
.tabla-matriz td.c-sup{background:rgba(147,80,173,.25);font-weight:700}
.tabla-matriz td.c-alto{background:rgba(76,176,80,.25);font-weight:700}
.tabla-matriz td.c-bas{background:rgba(225,167,7,.28);font-weight:700}
.tabla-matriz td.c-bajo{background:rgba(192,0,0,.25);font-weight:700}

/* Detalle por periodo */
details{margin-top:14px}
summary{cursor:pointer;font-weight:700;font-size:15px;padding:9px 14px;background:var(--card);
border-left:7px solid var(--c,var(--accent));border-radius:10px;box-shadow:var(--shadow)}
.materia-card{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--lv,var(--line));
border-radius:12px;padding:12px;margin-top:12px;box-shadow:var(--shadow)}
.materia-header{display:flex;justify-content:space-between;align-items:center;gap:10px;
border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:8px}
.materia-titulo{font-size:15px;font-weight:700}
.docente{font-size:12px;color:var(--muted)}
.badge{padding:3px 9px;border-radius:999px;color:#fff;font-weight:700;font-size:11px;white-space:nowrap}
.desempenio-box{border:1px solid var(--line);border-left:5px solid var(--lv,var(--line));border-radius:8px;
padding:8px 10px;margin-top:6px;display:flex;justify-content:space-between;align-items:center;gap:10px}
.desempenio-texto{font-size:13px;line-height:1.35}

/* Índice */
.filtros{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:0 0 12px}
.filtros input[type=search]{flex:1;min-width:200px;padding:10px 14px;border:1px solid var(--line);
border-radius:999px;background:var(--card);color:var(--ink);font-size:14px;box-shadow:var(--shadow)}
.gral tbody.est{border-top:5px solid var(--head)}
.gral td.est-c{text-align:left;min-width:190px}
.est-flex{display:flex;gap:10px;align-items:center}
.gral td.n{white-space:nowrap;min-width:78px}
.gral td.n.ok,.gral td.n.bad{min-width:96px}
.leyenda{color:var(--muted);font-size:12px;margin:12px 2px}

@media print{
body{background:#fff;color:#000;padding:0}
.nav,.filtros{display:none}
.salto{break-before:page}
.materia-card,.desempenio-box{break-inside:avoid}
.tabla-matriz th,.tabla-matriz td{font-size:8px}
}
"""

JS_INDICE = """
const q=document.getElementById('q'),c=document.getElementById('cuenta'),bt=document.querySelectorAll('button.stat');
let modo='todos';
function pasa(b){
  const r=Number(b.dataset.rojos);
  if(modo==='ok')return r===0;
  if(modo==='bad')return r>0;
  if(modo==='inc')return b.dataset.inc==='1';
  return true;
}
function filtrar(){
  const t=q.value.toLowerCase().trim();let n=0;
  document.querySelectorAll('tbody.est').forEach(function(b){
    const ok=(!t||b.dataset.buscar.indexOf(t)>-1)&&pasa(b);
    b.hidden=!ok;if(ok)n++;
  });
  c.textContent='Mostrando '+n;
}
bt.forEach(function(x){x.addEventListener('click',function(){
  modo=x.dataset.modo;
  bt.forEach(function(y){y.classList.toggle('act',y===x);});
  filtrar();
});});
q.addEventListener('input',filtrar);filtrar();
"""

NIVELES_CSS = (("SUPERIOR", "sup"), ("ALTO", "alto"), ("BÁSICO", "bas"), ("BAJO", "bajo"))


def envolver(titulo, cuerpo, js=""):
    script = f"<script>{js}</script>" if js else ""
    return (
        '<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(titulo)}</title><style>{CSS}</style></head>"
        f'<body><div class="wrap">{cuerpo}</div>{script}</body></html>'
    )


def fmt_par(par):
    tengo, total = par
    return "—" if total == 0 else f"{tengo} / {total}"


def avatar(e, prefijo="", grande=False):
    """Foto de perfil si existe; si no, círculo con las iniciales."""
    info = e["info"]
    extra = " grande" if grande else ""
    if e.get("icono"):
        return (
            f'<img class="av{extra}" src="{esc(prefijo)}icons/{esc(e["icono"])}" '
            f'alt="Foto de {esc(info["nombre"])}" loading="lazy">'
        )
    palabras = [p for p in str(info["nombre"]).split() if p[:1].isalpha()]
    iniciales = "".join(p[0] for p in palabras[:2]).upper() or "?"
    tono = sum(ord(ch) for ch in str(e["id"])) * 47 % 360
    return f'<span class="av{extra}" style="background:hsl({tono},62%,42%)">{esc(iniciales)}</span>'


def barra_niveles(conteos):
    segs = "".join(
        f'<i class="{cl}" style="flex:{conteos[n]}"></i>' for n, cl in NIVELES_CSS if conteos[n]
    )
    return f'<div class="stack">{segs}</div>' if segs else ""


def barra_meta(r):
    """Barra de avance (tengo/total) con una marca en el 80%."""
    if r["total"] == 0:
        return ""
    pct = r["tengo"] * 100 / r["total"]
    marca = r["meta"] * 100 / r["total"]
    cls = "si" if r["ok"] else "no"
    return f'<div class="bar {cls}"><i style="width:{pct:.1f}%"></i><b style="left:{marca:.1f}%"></b></div>'


def html_indice(entradas):
    if not entradas:
        return envolver(
            "Notas Udeki",
            '<div class="hero"><h1>Notas Udeki</h1><p class="sub">Aún no hay estudiantes. '
            "Ejecuta <code>python notas_web.py</code> para recolectar las notas.</p></div>",
        )

    ultima = max((e["fecha"] for e in entradas if e["fecha"]), default="")
    total = len(entradas)
    en_rojo = sum(1 for e in entradas if e["rojos"])
    incompletos = sum(1 for e in entradas if e["n_periodos"] < 4)

    bloques = []
    for e in entradas:
        res = e["resumen"]
        nombre = esc(e["info"]["nombre"])
        insignias = (
            f'<span class="pill bad">{e["rojos"]} en rojo</span>'
            if e["rojos"]
            else '<span class="pill ok">al día</span>'
        )
        if e["n_periodos"] < 4:
            insignias += f'<span class="pill warn">{e["n_periodos"]}/4 periodos</span>'

        areas = list(res.items())
        celda_est = (
            f'<td class="est-c" rowspan="{max(len(areas), 1)}"><div class="est-flex">'
            f'{avatar(e)}<div>'
            f'<a href="estudiantes/{esc(e["archivo"])}.html"><strong>{nombre}</strong></a>'
            f'<br><small>ID {esc(e["id"])}</small><br>{insignias}</div></div></td>'
        )
        filas = []
        if not areas:
            filas.append(f'<tr>{celda_est}<td colspan="6" class="izq">Sin áreas registradas</td></tr>')
        for k, (area, r) in enumerate(areas):
            estado = "s-ok" if r["ok"] else "s-bad"
            tds = [celda_est] if k == 0 else []
            tds.append(f'<td class="area {estado}">{esc(area)}</td>')
            for p in PERIODOS:
                tds.append(
                    f'<td class="n">{fmt_par(r["periodos"][p])}{barra_niveles(e["matriz"][area][p])}</td>'
                )
            cls = "ok" if r["ok"] else "bad"
            tds.append(
                f'<td class="n {cls}" title="Meta 80%: {r["meta"]}">'
                f'{r["tengo"]} / {r["total"]}<span class="meta">meta {r["meta"]}</span>{barra_meta(r)}</td>'
            )
            filas.append("<tr>" + "".join(tds) + "</tr>")

        buscar = esc(f'{e["id"]} {e["info"]["nombre"]}'.lower())
        inc = 1 if e["n_periodos"] < 4 else 0
        bloques.append(
            f'<tbody class="est" data-buscar="{buscar}" data-rojos="{e["rojos"]}" data-inc="{inc}">'
            + "".join(filas)
            + "</tbody>"
        )

    cuerpo = f"""
<div class="hero">
  <h1>Notas Udeki</h1>
  <p class="sub">Consolidado general · actualizado {esc(ultima)}</p>
  <div class="stats c4">
    <button class="stat act" data-modo="todos"><b>{total}</b><span>Estudiantes</span></button>
    <button class="stat s-ok" data-modo="ok"><b>{total - en_rojo}</b><span>Al día</span></button>
    <button class="stat s-bad" data-modo="bad"><b>{en_rojo}</b><span>Con áreas en rojo</span></button>
    <button class="stat s-inc" data-modo="inc"><b>{incompletos}</b><span>Incompletos</span></button>
  </div>
</div>
<div class="filtros">
  <input type="search" id="q" placeholder="Buscar por nombre o ID">
  <span id="cuenta" class="sub"></span>
</div>
<div class="scroll"><table class="gral">
<thead><tr>
  <th class="izq">Estudiante</th><th class="izq">Área</th>
  <th class="p1">Periodo 1</th><th class="p2">Periodo 2</th>
  <th class="p3">Periodo 3</th><th class="p4">Periodo 4</th>
  <th class="tot">Total anual</th>
</tr></thead>
{"".join(bloques)}
</table></div>
<p class="leyenda">
Cada periodo muestra <b>tengo / total</b> y una barra con la mezcla de niveles:
<span class="lg sup"></span>Superior<span class="lg alto"></span>Alto<span class="lg bas"></span>Básico<span class="lg bajo"></span>Bajo.<br>
Total anual: suma de los 4 periodos. La barra avanza hacia el total y la marca vertical es el 80%:
verde si la alcanza, rojo si no.
</p>
"""
    return envolver("Notas Udeki — General", cuerpo, JS_INDICE)


def html_resumen(e):
    filas = []
    for area, r in e["resumen"].items():
        cls = "ok" if r["ok"] else "bad"
        estado = "s-ok" if r["ok"] else "s-bad"
        filas.append(
            f'<tr><td class="area {estado}"><strong>{esc(area)}</strong></td>'
            f'<td class="{cls}" style="min-width:110px">{r["tengo"]}{barra_meta(r)}</td>'
            f'<td>{r["meta"]}</td><td>{r["total"]}</td></tr>'
        )
    return f"""
<h2>Resumen de evaluación individual por área</h2>
<div class="scroll"><table>
<thead><tr><th class="izq">Áreas</th><th class="ok">Tengo</th><th class="p1">80%</th><th>Total</th></tr></thead>
<tbody>{"".join(filas)}</tbody></table></div>
<p class="leyenda">La marca vertical de cada barra es el 80% del total; el relleno es lo que tiene el estudiante.</p>
"""


def html_consolidado(e):
    cabecera = "".join(
        f'<th colspan="4" class="p{i}">{esc(p)}</th>' for i, p in enumerate(PERIODOS, 1)
    )
    sub = "".join(
        '<th class="lv lv-sup">Superior</th><th class="lv lv-alto">Alto</th>'
        '<th class="lv lv-bas">Básico</th><th class="lv lv-bajo">Bajo</th>'
        for _ in PERIODOS
    )
    filas = []
    for area in sorted(e["matriz"]):
        celdas = []
        for p in PERIODOS:
            for n, cl in NIVELES_CSS:
                c = e["matriz"][area][p][n]
                celdas.append(f'<td class="c-{cl}">{c}</td>' if c else "<td></td>")
        filas.append(f'<tr><td class="area"><strong>{esc(area)}</strong></td>{"".join(celdas)}</tr>')
    return f"""
<h2>Consolidado anual de desempeños por área</h2>
<div class="scroll"><table class="tabla-matriz">
<thead><tr><th rowspan="2" class="izq">Áreas</th>{cabecera}</tr><tr>{sub}</tr></thead>
<tbody>{"".join(filas)}</tbody></table></div>
"""


def html_detalle(registros):
    partes = ['<div class="salto"></div>', "<h2>Detalle por periodo</h2>"]
    for p_key in claves_ordenadas(registros):
        num = int(p_key.split("_")[1])
        partes.append(f'<details open><summary style="--c:var(--p{min(max(num, 1), 4)})">Periodo {num}</summary>')
        for mat in registros[p_key].get("modelscal", []) or []:
            nombre = esc(mat.get("nombre") or "Asignatura sin nombre")
            docente = esc(mat.get("docente") or "Docente no asignado")
            faltas = esc(mat.get("faltas", "0"))
            nivel = convertir_nota_a_nivel(mat.get("sisnal", mat.get("nota", "BÁSICO")))
            color = ESCALA_VALORACION.get(nivel, {}).get("color", "#ffc107")
            fg = "#2b1d00" if nivel == "BÁSICO" else "#fff"
            partes.append(
                f'<div class="materia-card" style="--lv:{color}"><div class="materia-header"><div>'
                f'<div class="materia-titulo">{nombre}</div>'
                f'<div class="docente">Docente: {docente} | Faltas: {faltas}</div></div>'
                f'<span class="badge" style="background-color:{color};color:{fg}">{esc(nivel)}</span></div>'
            )
            for des in mat.get("indicadores", []) or []:
                n_des = convertir_nota_a_nivel(des.get("nota", "3.0"))
                c_des = ESCALA_VALORACION.get(n_des, {}).get("color", "#ffc107")
                fg_des = "#2b1d00" if n_des == "BÁSICO" else "#fff"
                partes.append(
                    f'<div class="desempenio-box" style="--lv:{c_des}"><div class="desempenio-texto">'
                    f'{esc(des.get("nombreindicador", "Sin descripción"))}</div>'
                    f'<span class="badge" style="background-color:{c_des};color:{fg_des}">{esc(n_des)}</span></div>'
                )
            partes.append("</div>")
        partes.append("</details>")
    return "".join(partes)


def html_estudiante(e, anterior=None, siguiente=None):
    info = e["info"]
    nav = ['<a href="../index.html">← Todos los estudiantes</a>']
    if anterior:
        nav.append(f'<a href="{esc(anterior["archivo"])}.html" title="{esc(anterior["info"]["nombre"])}">◀ Anterior</a>')
    if siguiente:
        nav.append(f'<a href="{esc(siguiente["archivo"])}.html" title="{esc(siguiente["info"]["nombre"])}">Siguiente ▶</a>')
    if (CARPETA_SALIDA / "pdf" / f'{e["archivo"]}.pdf').exists():
        nav.append(f'<a href="../pdf/{esc(e["archivo"])}.pdf">Descargar PDF</a>')

    n_areas = len(e["resumen"])
    clase_per = "stat s-inc" if e["n_periodos"] < 4 else "stat"

    cuerpo = f"""
<div class="nav">{"".join(nav)}</div>
<div class="hero">
  <div class="hero-top">{avatar(e, "../", True)}
    <div><h1>{esc(info["nombre"])}</h1>
    <p class="sub">Grado {esc(info["grado"])} · ID {esc(e["id"])} · actualizado {esc(e["fecha"])}</p></div>
  </div>
  <div class="stats c3">
    <div class="stat s-ok"><b>{n_areas - e["rojos"]}</b><span>Áreas al día</span></div>
    <div class="stat s-bad"><b>{e["rojos"]}</b><span>Áreas en rojo</span></div>
    <div class="{clase_per}"><b>{e["n_periodos"]}/4</b><span>Periodos capturados</span></div>
  </div>
</div>
<div class="info-grid">
  <div class="info-item"><small>Identificación</small><b>{esc(info["identificacion"])}</b></div>
  <div class="info-item"><small>Fecha de nacimiento</small><b>{esc(info["nacimiento"])}</b></div>
  <div class="info-item"><small>Género</small><b>{esc(info["genero"])}</b></div>
  <div class="info-item"><small>NUI</small><b>{esc(info["nui"])}</b></div>
  <div class="info-item"><small>ID estudiante</small><b>{esc(info["id_est"])}</b></div>
  <div class="info-item"><small>Grado</small><b>{esc(info["grado"])}</b></div>
</div>
{html_resumen(e)}
{html_consolidado(e)}
{html_detalle(e["registros"])}
"""
    return envolver(f'{info["nombre"]} — Notas Udeki', cuerpo)


# ----------------------------------------------------------------------------
# ARCHIVOS
# ----------------------------------------------------------------------------
def leer_credenciales(archivo=ARCHIVO_INFO):
    """Devuelve [(id, correo, password), ...] en el orden de info.txt."""
    ruta_info = Path(archivo)
    if not ruta_info.is_absolute():
        ruta_info = BASE / ruta_info
    lista, vistos = [], set()
    try:
        with open(ruta_info, "r", encoding="utf-8-sig") as f:
            for num_linea, linea in enumerate(f, start=1):
                linea = linea.strip()
                if not linea or linea.startswith("#"):
                    continue
                partes = [p.strip() for p in linea.split(",", 2)]
                if len(partes) < 3:
                    print(f"⚠️ Línea {num_linea} de {ARCHIVO_INFO} mal formada (se esperaba ID,correo,contraseña), se ignora.")
                    continue
                id_actual, correo, password = partes
                if id_actual.lower() in ("id", "") or id_actual in vistos:
                    continue
                vistos.add(id_actual)
                lista.append((id_actual, correo, password))
    except FileNotFoundError:
        print(f"❌ No se encontró el archivo '{ARCHIVO_INFO}' junto a este script ({ruta_info}).")
    return lista


def guardar_icono(id_est, datos, ext):
    """Guarda la foto de perfil en icons/<ID>.<ext> y devuelve el nombre del archivo."""
    carpeta = CARPETA_SALIDA / "icons"
    carpeta.mkdir(parents=True, exist_ok=True)
    seguro = archivo_seguro(id_est)
    for viejo in carpeta.glob(f"{seguro}.*"):  # borra versiones con otra extensión
        try:
            viejo.unlink()
        except OSError:
            pass
    nombre = f"{seguro}{ext}"
    (carpeta / nombre).write_bytes(datos)
    return nombre


def icono_previo(id_est):
    """Reutiliza la foto ya descargada en ejecuciones anteriores, si sigue ahí."""
    ruta = CARPETA_SALIDA / "datos" / f"{archivo_seguro(id_est)}.json"
    if not ruta.exists():
        return None
    try:
        nombre = json.loads(ruta.read_text(encoding="utf-8")).get("icono")
    except Exception:
        return None
    if nombre and (CARPETA_SALIDA / "icons" / nombre).exists():
        return nombre
    return None


def guardar_datos(id_est, registros, icono=None):
    carpeta = CARPETA_SALIDA / "datos"
    carpeta.mkdir(parents=True, exist_ok=True)
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    contenido = {"id": str(id_est), "fecha": fecha, "icono": icono, "periodos": registros}
    ruta = carpeta / f"{archivo_seguro(id_est)}.json"
    ruta.write_text(json.dumps(contenido, indent=2, ensure_ascii=False), encoding="utf-8")
    return fecha


def cargar_entradas(orden_ids):
    carpeta = CARPETA_SALIDA / "datos"
    entradas = {}
    for ruta in carpeta.glob("*.json"):
        try:
            crudo = json.loads(ruta.read_text(encoding="utf-8"))
            registros = crudo.get("periodos") or {}
            if not registros:
                continue
            id_est = str(crudo.get("id", ruta.stem))
            entradas[id_est] = construir_entrada(
                id_est, registros, crudo.get("fecha", ""), crudo.get("icono")
            )
        except Exception as ex:
            print(f"⚠️ No se pudo leer {ruta.name}: {ex}")
    orden = [i for i in orden_ids if i in entradas]
    orden += sorted((i for i in entradas if i not in orden), key=clave_natural)
    return [entradas[i] for i in orden]


def reconstruir_sitio(orden_ids):
    """Genera index.html y una página por estudiante a partir de datos/*.json."""
    entradas = cargar_entradas(orden_ids)
    carpeta_est = CARPETA_SALIDA / "estudiantes"
    carpeta_est.mkdir(parents=True, exist_ok=True)
    for i, e in enumerate(entradas):
        anterior = entradas[i - 1] if i > 0 else None
        siguiente = entradas[i + 1] if i < len(entradas) - 1 else None
        (carpeta_est / f'{e["archivo"]}.html').write_text(
            html_estudiante(e, anterior, siguiente), encoding="utf-8"
        )
    (CARPETA_SALIDA / "index.html").write_text(html_indice(entradas), encoding="utf-8")
    print(f"\n🌐 Sitio generado con {len(entradas)} estudiantes: {(CARPETA_SALIDA / 'index.html').resolve()}")


# ----------------------------------------------------------------------------
# NAVEGADOR (Playwright)
# ----------------------------------------------------------------------------
async def cambiar_periodo(page, texto_periodo):
    """Abre el desplegable de 'Período' y selecciona la opción indicada."""
    await page.click(".vs__dropdown-toggle")
    await page.wait_for_selector('li[role="option"]', timeout=10000)
    await page.get_by_role("option", name=texto_periodo, exact=True).click(timeout=10000)


async def descargar_icono(page, id_est):
    """Baja la foto de perfil del usuario logueado. Devuelve (bytes, extensión) o None."""
    try:
        await page.wait_for_selector(SELECTOR_AVATAR, timeout=15000)
    except Exception:
        print("   ⚠️ No apareció la foto de perfil en la barra superior.")
        return None

    try:
        src = await page.evaluate(
            """(sel) => {
                const img = document.querySelector(sel);
                if (!img) return null;
                if (img.tagName === 'IMG') return img.getAttribute('src') || img.currentSrc || null;
                const dentro = img.querySelector('img');
                return dentro ? (dentro.getAttribute('src') || dentro.currentSrc || null) : null;
            }""",
            SELECTOR_AVATAR,
        )
    except Exception as ex:
        print(f"   ⚠️ No se pudo leer la foto de perfil ({ex}).")
        return None

    if not src:
        return None
    if AVATAR_POR_DEFECTO in src:
        print("   · El perfil no tiene foto propia (usa la de por defecto).")
        return None

    url = urljoin(page.url, src)
    try:
        # context.request reutiliza las cookies de la sesión, así que sirve aunque
        # la imagen esté en otro subdominio protegido.
        respuesta = await page.context.request.get(url)
        if not respuesta.ok:
            print(f"   ⚠️ La foto de perfil respondió {respuesta.status}.")
            return None
        tipo = (respuesta.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not tipo.startswith("image/"):
            print(f"   ⚠️ La foto de perfil no es una imagen ({tipo or 'sin tipo'}).")
            return None
        datos = await respuesta.body()
    except Exception as ex:
        print(f"   ⚠️ No se pudo descargar la foto de perfil ({ex}).")
        return None

    if len(datos) < TAMANO_MIN_ICONO:
        print("   ⚠️ La foto de perfil llegó vacía o incompleta.")
        return None

    ext = (mimetypes.guess_extension(tipo) or ".png").lower()
    if ext == ".jpe":
        ext = ".jpg"
    print(f"   🖼️ Foto de perfil descargada ({len(datos) / 1024:.0f} KB).")
    return datos, ext


async def capturar_estudiante(browser, correo, password):
    """Login + captura de los 4 periodos y la foto de perfil, en un contexto limpio."""
    context = await browser.new_context()
    registros = {}
    icono = None
    completos = asyncio.Event()

    async def handle_response(response):
        if "getcalificacionesestudiante" not in response.url or response.status != 200:
            return
        try:
            data = await response.json()
        except Exception as ex:
            print(f"   Error procesando el paquete: {ex}")
            return
        if len(registros) >= 4 or data in registros.values():
            return
        key = f"periodo_{len(registros) + 1}"
        registros[key] = data
        print(f"   📥 {key} capturado ({len(registros)}/4)")
        if len(registros) == 4:
            completos.set()

    try:
        page = await context.new_page()
        page.on("response", handle_response)
        await page.goto(URL_LOGIN)

        try:
            selector_correo = 'input[type="email"], input[name="email"], input[name="correo"], input#email'
            selector_password = 'input[type="password"], input[name="password"], input[name="contrasena"], input#password'
            selector_boton = 'button[type="submit"], button:has-text("Ingresar"), button:has-text("Iniciar sesión"), button:has-text("Login")'
            await page.wait_for_selector(selector_correo, timeout=20000)
            await page.fill(selector_correo, correo)
            await page.fill(selector_password, password)
            await page.click(selector_boton)
            await page.wait_for_url(URL_DASHBOARD, timeout=30000)
            await asyncio.sleep(2)
            await page.goto(URL_DESEMPENIO)
        except Exception as ex:
            print(f"   ⚠️ Nota durante el login: {ex}")

        for _ in range(30):
            if registros:
                break
            await asyncio.sleep(1)

        if registros:
            icono = await descargar_icono(page, None)
            for texto in PERIODOS[1:]:
                try:
                    await cambiar_periodo(page, texto)
                    await asyncio.sleep(1.5)
                except Exception as ex:
                    print(f"   ⚠️ No se pudo cambiar a '{texto}' ({ex}).")
            try:
                await asyncio.wait_for(completos.wait(), timeout=TIMEOUT_PERIODOS)
            except asyncio.TimeoutError:
                print(f"   ⚠️ Tiempo agotado. Se usarán los {len(registros)} periodos capturados.")
    finally:
        await context.close()
    return registros, icono


async def generar_pdf(browser, entrada):
    carpeta = CARPETA_SALIDA / "pdf"
    carpeta.mkdir(parents=True, exist_ok=True)
    page = await browser.new_page()
    try:
        html_txt = html_estudiante(entrada)
        # set_content no tiene URL base: se la damos para que icons/ resuelva en el PDF.
        base_uri = (CARPETA_SALIDA / "estudiantes").as_uri() + "/"
        html_txt = html_txt.replace("<head>", f'<head><base href="{base_uri}">', 1)
        await page.set_content(html_txt, wait_until="networkidle")
        await page.pdf(path=str(carpeta / f'{entrada["archivo"]}.pdf'), format="A4", print_background=True)
    finally:
        await page.close()


# ----------------------------------------------------------------------------
# PRINCIPAL
# ----------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="Notas Udeki -> sitio web")
    parser.add_argument("ids", nargs="*", help="IDs a procesar (por defecto, todos los de info.txt)")
    parser.add_argument("--reconstruir", action="store_true", help="rehacer las páginas desde datos/*.json sin entrar a Udeki")
    args = parser.parse_args()

    credenciales = leer_credenciales()
    orden_ids = [c[0] for c in credenciales]

    if args.reconstruir:
        reconstruir_sitio(orden_ids)
        return

    if not credenciales:
        print("❌ No hay credenciales en info.txt.")
        return

    if args.ids:
        pedidos = [i.strip() for i in args.ids if i.strip()]
        por_id = {c[0]: c for c in credenciales}
        for i in pedidos:
            if i not in por_id:
                print(f"⚠️ El ID '{i}' no está en {ARCHIVO_INFO}, se omite.")
        lista = [por_id[i] for i in pedidos if i in por_id]
    else:
        lista = credenciales

    if not lista:
        print("❌ No hay IDs para procesar.")
        return

    from playwright.async_api import async_playwright

    resultados = {"completos": [], "incompletos": [], "fallidos": []}
    print(f"Procesando {len(lista)} estudiantes...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        try:
            for n, (id_est, correo, password) in enumerate(lista, start=1):
                print(f"\n[{n}/{len(lista)}] ID {id_est}")
                try:
                    registros, icono = await capturar_estudiante(browser, correo, password)
                except Exception as ex:
                    print(f"   ❌ Error: {ex}")
                    resultados["fallidos"].append(id_est)
                    continue

                if not registros:
                    print("   ❌ No se capturó ningún periodo.")
                    resultados["fallidos"].append(id_est)
                    continue

                nombre_icono = guardar_icono(id_est, *icono) if icono else None
                if not nombre_icono:
                    nombre_icono = icono_previo(id_est)  # conserva la foto anterior, si la había

                fecha = guardar_datos(id_est, registros, nombre_icono)
                if GENERAR_PDF:
                    try:
                        entrada = construir_entrada(id_est, registros, fecha, nombre_icono)
                        await generar_pdf(browser, entrada)
                    except Exception as ex:
                        print(f"   ⚠️ No se pudo generar el PDF: {ex}")

                clave = "completos" if len(registros) == 4 else "incompletos"
                resultados[clave].append(id_est)
                print(f"   ✔ Guardado ({len(registros)}/4 periodos)")
                await asyncio.sleep(PAUSA_ENTRE_ESTUDIANTES)
        finally:
            await browser.close()
            reconstruir_sitio(orden_ids)

    print("\n--- Resumen ---")
    print(f"Completos:   {len(resultados['completos'])}")
    print(f"Incompletos: {len(resultados['incompletos'])} {resultados['incompletos'] or ''}")
    print(f"Fallidos:    {len(resultados['fallidos'])} {resultados['fallidos'] or ''}")
    reintentar = resultados["fallidos"] + resultados["incompletos"]
    if reintentar:
        print("\nPara reintentar solo esos:")
        print("python notas_web.py " + " ".join(reintentar))


if __name__ == "__main__":
    if "--reconstruir" not in sys.argv:
        verificar_dependencias()
    asyncio.run(main())