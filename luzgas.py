#!/usr/bin/env python3
"""Monitor de luz con Datadis + PVPC oficial de Red Eléctrica.

Descarga tu consumo horario, tu potencia máxima y el detalle de tu contrato desde la
API privada de Datadis, lo cruza con los precios horarios del PVPC publicados por
Red Eléctrica (apidatos.ree.es), calcula cuánto habrías pagado con cada tarifa que
tengas en config.json, recomienda la potencia, detecta alertas y manda un correo.

Solo usa la biblioteca estándar de Python (3.9+).

Uso:
  python3 luzgas.py --modo auto        # lo que ejecuta la tarea programada
  python3 luzgas.py --modo mensual     # informe completo ahora
  python3 luzgas.py --modo semanal     # chequeo rápido ahora
  python3 luzgas.py --demo --sin-correo  # prueba con datos inventados
"""

import argparse
import csv
import datetime as dt
import html
import json
import os
import random
import smtplib
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
REPORTES = BASE / "reportes"
ESTADO = BASE / "estado.json"

DATADIS = "https://datadis.es"
REE_PVPC = "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
LUZFIJA = "https://luzfija.es/"
UA = "luzgas-monitor/1.0 (uso personal)"

# Festivos nacionales de fecha fija que la 2.0TD trata como valle (Circular CNMC 3/2020:
# se excluyen los sustituibles y los que no tienen fecha fija, como Viernes Santo).
FESTIVOS_FIJOS = {(1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25)}


class ErrorDatadis(Exception):
    pass


class ConsultaRepetida(ErrorDatadis):
    pass


# --------------------------------------------------------------------------- utilidades

def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def eur(x, dec=2):
    if x is None:
        return "-"
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def num(x, dec=2):
    if x is None:
        return "-"
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def kw_luzfija(x):
    return num(x, 2).rstrip("0").rstrip(",") if x is not None else ""


def llavero(servicio, cuenta):
    """Lee una contraseña del Llavero de macOS. Devuelve None si no existe."""
    try:
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", servicio, "-a", cuenta, "-w"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def http(url, data=None, headers=None, timeout=120, intentos=3):
    h = {"User-Agent": UA}
    h.update(headers or {})
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", errors="replace")[:300]
            ultimo = f"HTTP {e.code}: {cuerpo}"
            if e.code == 429 and "24 horas" in cuerpo:
                raise ConsultaRepetida(cuerpo)  # Datadis no repite la misma consulta en 24 h
            if e.code in (400, 401, 403, 404):
                break
            if e.code == 429:
                time.sleep(60)
        except Exception as e:  # timeouts, red
            ultimo = str(e)
        time.sleep(5 * (i + 1))
    raise ErrorDatadis(f"{url.split('?')[0]} -> {ultimo}")


def mes_suma(y, m, delta):
    m0 = (y * 12 + (m - 1)) + delta
    return m0 // 12, m0 % 12 + 1


def periodo_2_0td(fecha, hora_inicio):
    """Periodo de energía (punta/llano/valle) para la hora que empieza en hora_inicio (0-23)."""
    if fecha.weekday() >= 5 or (fecha.month, fecha.day) in FESTIVOS_FIJOS:
        return "valle"
    if hora_inicio < 8:
        return "valle"
    if 10 <= hora_inicio < 14 or 18 <= hora_inicio < 22:
        return "punta"
    return "llano"


def escalon(kw, escalones):
    for e in sorted(escalones):
        if e >= kw - 1e-9:
            return e
    return max(escalones)


# --------------------------------------------------------------------------- Datadis

class Datadis:
    def __init__(self, nif, password):
        self.nif = nif
        self.password = password
        self.token = None

    def login(self):
        body = urllib.parse.urlencode({"username": self.nif, "password": self.password}).encode()
        t = http(f"{DATADIS}/nikola-auth/tokens/login", data=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"}, intentos=2)
        t = t.strip().strip('"')
        if not t or "no validated" in t.lower() or "no confirmated" in t.lower() or len(t) < 40:
            raise ErrorDatadis(f"Login de Datadis rechazado: {t[:80]}")
        self.token = t

    def get(self, endpoint, **params):
        # Datadis rechaza repetir la misma consulta en 24 h: se guarda cada respuesta y,
        # si ya se hizo hoy o Datadis responde 429, se usa la copia guardada.
        q = urllib.parse.urlencode(sorted((k, v) for k, v in params.items() if v not in (None, "")))
        CACHE.mkdir(exist_ok=True)
        clave = CACHE / ("api_" + "".join(ch if ch.isalnum() else "_" for ch in f"{endpoint}_{q}")[:150] + ".json")
        hoy = dt.date.today()
        if clave.exists() and dt.date.fromtimestamp(clave.stat().st_mtime) == hoy:
            txt = clave.read_text()
        else:
            if not self.token:
                self.login()
            try:
                txt = http(f"{DATADIS}/api-private/api/{endpoint}?{q}",
                           headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
                clave.write_text(txt)
            except ConsultaRepetida:
                if not clave.exists():
                    raise
                log(f"{endpoint}: Datadis no repite consultas en 24 h, uso la copia guardada")
                txt = clave.read_text()
        try:
            d = json.loads(txt)
        except json.JSONDecodeError:
            raise ErrorDatadis(f"{endpoint}: respuesta no JSON: {txt[:200]}")
        errores = []
        if isinstance(d, dict):
            errores = d.get("distributorError") or []
            for clave in ("timeCurve", "maxPower", "contract", "supplies"):
                if isinstance(d.get(clave), list):
                    return d[clave], errores
            for v in d.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "errorCode" not in v[0]:
                    return v, errores
            return [], errores
        return (d if isinstance(d, list) else []), errores

    def suministros(self):
        filas, err = self.get("get-supplies-v2")
        if not filas:
            raise ErrorDatadis(f"Datadis no devolvió suministros. Errores: {err}")
        return filas

    def contrato(self, cups, distributor):
        filas, _ = self.get("get-contract-detail-v2", cups=cups, distributorCode=distributor)
        return filas[0] if filas else {}

    def consumo_mes(self, cups, distributor, point_type, y, m):
        ym = f"{y:04d}/{m:02d}"
        filas, _ = self.get("get-consumption-data-v2", cups=cups, distributorCode=distributor,
                            startDate=ym, endDate=ym, measurementType="0", pointType=point_type)
        return filas

    def potencia_max(self, cups, distributor, y0, m0, y1, m1):
        filas, _ = self.get("get-max-power-v2", cups=cups, distributorCode=distributor,
                            startDate=f"{y0:04d}/{m0:02d}", endDate=f"{y1:04d}/{m1:02d}")
        return filas


# --------------------------------------------------------------------------- PVPC (REE)

def pvpc_mes(y, m, hoy):
    """Precios PVPC horarios (€/kWh sin impuestos) de un mes. Cachea los meses cerrados."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"pvpc_{y:04d}-{m:02d}.json"
    cerrado = (y, m) < (hoy.year, hoy.month)
    if f.exists() and (cerrado or time.time() - f.stat().st_mtime < 6 * 3600):
        return {tuple(k.split("|")): v for k, v in json.loads(f.read_text()).items()}
    ny, nm = mes_suma(y, m, 1)
    fin = dt.date(ny, nm, 1) - dt.timedelta(days=1)
    q = urllib.parse.urlencode({
        "start_date": f"{y:04d}-{m:02d}-01T00:00", "end_date": f"{fin.isoformat()}T23:59",
        "time_trunc": "hour", "geo_limit": "peninsular",
    })
    d = json.loads(http(f"{REE_PVPC}?{q}", timeout=60))
    acum = defaultdict(list)
    for inc in d.get("included", []):
        if inc.get("type") != "PVPC":
            continue
        for v in inc["attributes"]["values"]:
            s = v["datetime"]  # 2026-09-01T00:00:00.000+02:00 (inicio de hora, hora local)
            acum[(s[:10], str(int(s[11:13])))].append(v["value"] / 1000.0)
    precios = {k: sum(v) / len(v) for k, v in acum.items()}
    f.write_text(json.dumps({"|".join(k): v for k, v in precios.items()}))
    return precios


# --------------------------------------------------------------------------- datos de demo

def datos_demo(hoy):
    random.seed(7)
    y1, m1 = mes_suma(hoy.year, hoy.month, -1)
    y0, m0 = mes_suma(y1, m1, -11)
    ini = dt.date(y0, m0, 1)
    fin = dt.date(hoy.year, hoy.month, 1) - dt.timedelta(days=1)
    horas, maxp = [], defaultdict(lambda: {"P1": 0, "P2": 0})
    d = ini
    while d <= fin:
        invierno = d.month in (12, 1, 2)
        verano = d.month in (7, 8)
        for h in range(24):
            base = 0.11 + random.random() * 0.04
            if h in (6, 7):
                base += 1.3  # termo programado
            if h in (14, 21):
                base += 0.6 + random.random() * 0.5  # cocina
            if 19 <= h <= 23:
                base += 0.25
            if invierno and (19 <= h <= 23 or 7 <= h <= 9):
                base += 0.8
            if verano and 15 <= h <= 23:
                base += 0.7
            if d.weekday() == 5 and 10 <= h <= 12:
                base += 0.9  # lavadora
            if d >= fin - dt.timedelta(days=20) and 1 <= h <= 5:
                base += 0.09  # algo nuevo encendido de madrugada (para probar la alerta)
            horas.append({"fecha": d.isoformat(), "h": h, "kwh": round(base, 3), "metodo": "Real"})
            pico = base * (2.6 if h in (14, 21) else 1.4)
            per = "P2" if periodo_2_0td(d, h) == "valle" else "P1"
            k = f"{d.year:04d}-{d.month:02d}"
            maxp[k][per] = max(maxp[k][per], min(pico, 4.1))
        d += dt.timedelta(days=1)
    for x in random.sample(range(len(horas)), 60):
        horas[x]["metodo"] = "Estimada"
    contrato = {"marketer": "COMERCIALIZADORA DEMO S.A.", "contractedPowerkW": [4.6, 4.6],
                "accesFare": "2.0TD", "distributor": "I-DE (demo)", "lastMarketerDate": "2026/09/28",
                "timeDiscrimination": "3 periodos", "cups": "ES0000000000000000XX"}
    return horas, dict(maxp), contrato


# --------------------------------------------------------------------------- lectura y validación

ENTRADA = BASE / "entrada"


def filas_datadis(filas):
    """Convierte filas de la API a horas. Devuelve (horas, valida).

    En octubre de 2026 la API devolvía, para algunos suministros, el primer día del periodo
    repetido todos los días con la misma fecha. Si las fechas vienen atascadas pero los días
    son distintos, se reconstruyen por orden; si los días son copias, el mes se descarta.
    """
    if not filas:
        return [], True
    fechas = {r.get("date") for r in filas}
    atascadas = len(fechas) == 1 and len(filas) > 26
    if atascadas:
        bloques = [tuple(r.get("consumptionKWh") for r in filas[i:i + 24]) for i in range(0, len(filas) - 23, 24)]
        todo_cero = all(not any(b) for b in bloques)  # casa vacía: días iguales a cero son reales
        if len(bloques) > 3 and len(set(bloques)) == 1 and not todo_cero:
            return [], False
    out, dia, previa = [], None, 99
    for r in filas:
        try:
            hh = int(str(r["time"])[:2])  # Datadis: 01:00 = hora de 00 a 01
            if atascadas:
                if dia is None:
                    dia = dt.datetime.strptime(r["date"], "%Y/%m/%d").date()
                elif hh <= previa:
                    dia += dt.timedelta(days=1)
                fecha, previa = dia, hh
            else:
                fecha = dt.datetime.strptime(r["date"], "%Y/%m/%d").date()
            kwh = r.get("consumptionKWh")
            if kwh is None:
                continue
            out.append({"fecha": fecha.isoformat(), "h": min(hh - 1, 23), "kwh": float(kwh),
                        "metodo": str(r.get("obtainMethod", ""))})
        except (KeyError, ValueError):
            continue
    return out, True


def _fecha_flexible(s):
    s = s.strip().split(" ")[0]
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def leer_csv_entrada(cups=""):
    """Lee los CSV de consumo horario que dejes en entrada/ (Datadis web, UFD, i-DE...).

    Si el nombre del archivo contiene parte del CUPS (los 20 primeros caracteres), solo se usa
    para ese suministro; si no lleva CUPS en el nombre, se usa para todos.
    """
    if not ENTRADA.exists():
        return []
    horas = {}
    for f in sorted(ENTRADA.glob("*.csv")):
        if "ES00" in f.name.upper() and cups[:20].upper() not in f.name.upper():
            continue
        texto = f.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
        lineas = [l for l in texto.splitlines() if l.strip()]
        if not lineas:
            continue
        sep = ";" if lineas[0].count(";") >= lineas[0].count(",") else ","
        filas = list(csv.reader(lineas, delimiter=sep))
        cab_i = next((i for i, r in enumerate(filas[:30]) if any("fecha" in c.lower() or c.lower() == "date" for c in r)), None)
        if cab_i is None:
            log(f"{f.name}: no encuentro la cabecera (Fecha/Hora/Consumo)")
            continue
        cab = [c.strip().lower() for c in filas[cab_i]]
        ci_f = next(i for i, c in enumerate(cab) if "fecha" in c or c == "date")
        ci_h = next((i for i, c in enumerate(cab) if c.startswith("hora") or c == "time"), None)
        ci_c = next((i for i, c in enumerate(cab) if ("consumo" in c or c.startswith("ae") or "kwh" in c)
                     and "excedent" not in c and "vertid" not in c), None)
        if ci_h is None or ci_c is None:
            log(f"{f.name}: faltan columnas de hora o consumo")
            continue
        en_wh = "wh" in cab[ci_c] and "kwh" not in cab[ci_c]
        ci_m = next((i for i, c in enumerate(cab) if "real" in c or "metodo" in c or "método" in c), None)
        for r in filas[cab_i + 1:]:
            try:
                fecha = _fecha_flexible(r[ci_f])
                hh = int(r[ci_h].strip()[:2].rstrip(":"))
                v = float(r[ci_c].strip().replace(".", "").replace(",", ".")) if "," in r[ci_c] else float(r[ci_c])
            except (ValueError, IndexError):
                continue
            if not fecha:
                continue
            v = v / 1000.0 if en_wh else v
            h = min(max(hh - 1, 0), 23)
            clave = (fecha.isoformat(), h)
            horas[clave] = {"fecha": fecha.isoformat(), "h": h, "kwh": v,
                            "metodo": r[ci_m] if ci_m is not None and ci_m < len(r) else "Real"}
        log(f"CSV leído: {f.name}")
    return list(horas.values())


# --------------------------------------------------------------------------- descarga real

def abrir_datadis(cfg):
    pwd = llavero(cfg.get("keychain_datadis", "luzgas-datadis"), cfg["nif"])
    if not pwd:
        raise ErrorDatadis("No encuentro la contraseña de Datadis en el Llavero. Mira el paso 2 del README.")
    dd = Datadis(cfg["nif"], pwd)
    return dd, dd.suministros()


def horas_desde_lecturas(lecturas, desde_fecha=None):
    """Reparte el consumo real entre lecturas de contador en horas sintéticas.

    lecturas: [{"fecha": "AAAA-MM-DD", "p1": kWh acumulados punta, "p2": llano, "p3": valle}, ...]
    Cada tramo entre dos lecturas se reparte a partes iguales entre las horas de su periodo.
    Sirve para comparar tarifas cuando Datadis no da la curva horaria; no vale para alertas horarias.
    """
    lec = sorted(lecturas or [], key=lambda x: x["fecha"])
    horas = []
    for a, b in zip(lec, lec[1:]):
        d0, d1 = dt.date.fromisoformat(a["fecha"]), dt.date.fromisoformat(b["fecha"])
        if d1 <= d0:
            continue
        slots = defaultdict(list)
        d = d0
        while d < d1:
            for h in range(24):
                slots[periodo_2_0td(d, h)].append((d.isoformat(), h))
            d += dt.timedelta(days=1)
        for per, clave in (("punta", "p1"), ("llano", "p2"), ("valle", "p3")):
            delta = float(b[clave]) - float(a[clave])
            if delta < 0 or not slots[per]:
                continue
            v = delta / len(slots[per])
            for f, h in slots[per]:
                horas.append({"fecha": f, "h": h, "kwh": v, "metodo": "lectura"})
    return horas


def datos_reales(cfg, dd, sums, hoy, meses):
    pref = (cfg.get("cups") or "")[:20]
    s = next((x for x in sums if x.get("cups", "")[:20] == pref), None) if pref else sums[0]
    if s is None:
        raise ErrorDatadis(f"Datadis no tiene el CUPS {cfg.get('cups')} a tu nombre.")
    cups, dist, pt = s["cups"], str(s.get("distributorCode")), str(s.get("pointType", "5"))
    try:
        contrato = dd.contrato(cups, dist)
    except ErrorDatadis as e:
        log(f"Contrato: {e}")
        contrato = {"_sin_contrato": True}
    contrato.setdefault("cups", cups)

    CACHE.mkdir(exist_ok=True)
    horas, defectuosos = [], []
    y1, m1 = hoy.year, hoy.month
    for i in range(meses - 1, -1, -1):
        y, m = mes_suma(y1, m1, -i)
        f = CACHE / f"consumo_{cups[-6:]}_{y:04d}-{m:02d}.json"
        viejo = (y, m) < mes_suma(y1, m1, -2)
        de_hoy = f.exists() and dt.date.fromtimestamp(f.stat().st_mtime) == hoy
        if f.exists() and (viejo or de_hoy):
            filas = json.loads(f.read_text())
        else:
            try:
                filas = dd.consumo_mes(cups, dist, pt, y, m)
            except ErrorDatadis as e:
                log(f"Consumo {y}-{m:02d}: {e}")
                filas = json.loads(f.read_text()) if f.exists() else []
            if filas:
                f.write_text(json.dumps(filas))
            time.sleep(2)
        filas_mes, ok = filas_datadis(filas)
        if not ok:
            defectuosos.append(f"{y:04d}-{m:02d}")
            continue
        horas += filas_mes

    # CSV descargados a mano (web de Datadis o de la distribuidora) en entrada/: mandan sobre la API.
    manual = leer_csv_entrada(cups)
    if manual:
        fechas_manual = {h["fecha"] for h in manual}
        horas = [h for h in horas if h["fecha"] not in fechas_manual] + manual
        meses_manual = {h["fecha"][:7] for h in manual}
        defectuosos = [d for d in defectuosos if d not in meses_manual]

    # Lecturas reales del contador (facturas): rellenan los días que no tienen dato horario fiable.
    fechas_ok = {h["fecha"] for h in horas}
    sint = [h for h in horas_desde_lecturas(cfg.get("lecturas_reales")) if h["fecha"] not in fechas_ok]
    horas += sint
    contrato["_meses_defectuosos"] = defectuosos
    contrato["_meses_csv"] = sorted({h["fecha"][:7] for h in manual}) if manual else []
    contrato["_dias_lecturas"] = len({h["fecha"] for h in sint})

    y0, m0 = mes_suma(y1, m1, -(meses - 1))
    maxp = defaultdict(lambda: {"P1": 0.0, "P2": 0.0})
    try:
        for r in dd.potencia_max(cups, dist, y0, m0, y1, m1):
            v = float(r.get("maxPower") or 0)
            v = v / 1000.0 if v > 50 else v  # la doc dice W en un sitio y kW en otro
            fecha = dt.datetime.strptime(r["date"], "%Y/%m/%d").date()
            per = str(r.get("period", "")).upper()
            # Datadis: periodo 1 = punta, 2 = llano, 3 = valle. Punta y llano van a la potencia P1.
            p = "P2" if ("VALLE" in per or per in ("3", "P3")) else "P1"
            k = f"{fecha.year:04d}-{fecha.month:02d}"
            maxp[k][p] = max(maxp[k][p], v)
    except ErrorDatadis as e:
        log(f"Potencia máxima: {e}")
    return horas, dict(maxp), contrato


# --------------------------------------------------------------------------- cálculo

def coste(tarifa, horas, p1kw, p2kw, reg, precios_pvpc):
    """Coste total con impuestos de un conjunto de horas. Devuelve dict o None si faltan datos."""
    dias = len({h["fecha"] for h in horas})
    if not dias:
        return None
    kwh = sum(h["kwh"] for h in horas)
    tipo = tarifa.get("tipo", "pvpc")
    sin_precio = 0
    if tipo == "cuota":
        # Tarifa plana: cuota fija al mes con impuestos incluidos (p. ej. Holaluz Tarifa Justa).
        cuota = tarifa.get("cuota_mensual_total_eur")
        if cuota is None:
            return None
        total = float(cuota) * 12 / 365.0 * dias
        return {"dias": dias, "kwh": kwh, "potencia": None, "energia": None, "total": total,
                "anual": total * 365.0 / dias, "eur_kwh": total / kwh if kwh else None}
    if tipo == "pvpc":
        energia = 0.0
        for h in horas:
            p = precios_pvpc.get((h["fecha"], str(h["h"])))
            if p is None:
                sin_precio += 1
                continue
            energia += h["kwh"] * p
        if horas and sin_precio / len(horas) > 0.02:
            return None
        potencia = (p1kw * (reg["pvpc_potencia_p1_eur_kw_anio"] + reg["pvpc_margen_comercializacion_eur_kw_anio"])
                    + p2kw * reg["pvpc_potencia_p2_eur_kw_anio"]) * dias / 365.0
    else:
        pp1, pp2 = tarifa.get("potencia_p1_eur_kw_dia"), tarifa.get("potencia_p2_eur_kw_dia")
        precio = tarifa.get("energia_eur_kwh")
        if pp1 is None or pp2 is None or precio is None:
            return None
        potencia = (p1kw * pp1 + p2kw * pp2) * dias
        if isinstance(precio, dict):
            energia = 0.0
            for h in horas:
                per = periodo_2_0td(dt.date.fromisoformat(h["fecha"]), h["h"])
                energia += h["kwh"] * float(precio[per])
        else:
            energia = kwh * float(precio)
    bono = reg["financiacion_bono_social_eur_anio"] * dias / 365.0
    base_iee = potencia + energia + bono
    iee = max(base_iee * reg["impuesto_electrico_pct"] / 100.0, kwh * reg["impuesto_electrico_minimo_eur_kwh"])
    contador = reg["alquiler_contador_eur_mes"] * 12 / 365.0 * dias
    extras = float(tarifa.get("cuota_mensual_extra_eur") or 0) * 12 / 365.0 * dias
    base_iva = base_iee + iee + contador + extras
    total = base_iva * (1 + reg["iva_pct"] / 100.0)
    return {"dias": dias, "kwh": kwh, "potencia": potencia, "energia": energia, "total": total,
            "anual": total * 365.0 / dias, "eur_kwh": total / kwh if kwh else None}


def analizar(cfg, horas, maxp, contrato, hoy, estado, modo):
    reg = cfg["regulado"]
    aj = cfg["ajustes"]
    alertas, info = [], []

    # Ventana: últimos 12 meses cerrados.
    y1, m1 = mes_suma(hoy.year, hoy.month, -1)
    y0, m0 = mes_suma(y1, m1, -11)
    ini, fin = dt.date(y0, m0, 1), dt.date(hoy.year, hoy.month, 1) - dt.timedelta(days=1)
    anio = [h for h in horas if ini.isoformat() <= h["fecha"] <= fin.isoformat()]

    pot = contrato.get("contractedPowerkW") or estado.get("ultima_potencia") or []
    if not contrato.get("marketer") and estado.get("ultima_comercializadora"):
        contrato["marketer"] = estado["ultima_comercializadora"]
    p1kw = float(pot[0]) if pot else None
    p2kw = float(pot[1]) if len(pot) > 1 else p1kw

    # --- contrato y comercializadora
    marketer = (contrato.get("marketer") or "").strip()
    esperada = (cfg["contrato_actual"].get("comercializadora_esperada") or "").strip()
    previa = estado.get("ultima_comercializadora")
    if marketer and esperada and marketer.lower() != esperada.lower():
        alertas.append(("alta", "Tu comercializadora no es la esperada",
                        f"Datadis dice <b>{html.escape(marketer)}</b> y tú esperabas <b>{html.escape(esperada)}</b>. "
                        f"Fecha del último cambio: {html.escape(str(contrato.get('lastMarketerDate', '?')))}. "
                        "Si no lo pediste, llama hoy a la nueva compañía, pide el desistimiento por escrito (tienes 14 días naturales "
                        "desde el alta) y bloquea en tu banco los recibos de esa empresa."))
    elif marketer and previa and marketer != previa:
        alertas.append(("alta", "Ha cambiado tu comercializadora",
                        f"Antes: {html.escape(previa)}. Ahora: <b>{html.escape(marketer)}</b>. Si no fuiste tú, actúa hoy (14 días para desistir)."))
    if "HOLALUZ" in marketer.upper():
        alertas.append(("media", "Estás con Holaluz",
                        "Holaluz tuvo problemas serios en 2026. Si deja de operar, pasarías automáticamente al PVPC con una "
                        "comercializadora de referencia y no te quedarías sin luz, pero conviene comparar ya y no esperar a que pase. "
                        "Revisa también si tienes su tarifa plana (cuota fija con regularización anual)."))
    prev_pot = estado.get("ultima_potencia")
    if pot and prev_pot and [round(float(x), 2) for x in pot] != [round(float(x), 2) for x in prev_pot]:
        alertas.append(("media", "Ha cambiado tu potencia contratada",
                        f"Antes {prev_pot}, ahora {pot} kW. Comprueba que lo pediste tú."))

    # --- renovación
    fr = cfg["contrato_actual"].get("fecha_renovacion")
    if fr:
        try:
            dias_ren = (dt.date.fromisoformat(fr) - hoy).days
            if 0 <= dias_ren <= 45:
                alertas.append(("media", f"Tu contrato se renueva en {dias_ren} días",
                                "Es el momento de comparar: las compañías suelen subir el precio al renovar. "
                                "Sigue los pasos de LuzFija de este correo antes de esa fecha."))
        except ValueError:
            pass

    # --- lecturas estimadas (últimos 60 días)
    lim60 = (hoy - dt.timedelta(days=60)).isoformat()
    horas_reales = [h for h in horas if h["metodo"] != "lectura"]
    rec = [h for h in horas_reales if h["fecha"] >= lim60]
    est = [h for h in rec if h["metodo"] and not h["metodo"].lower().startswith("real")]
    if rec and len(est) / len(rec) > 0.05:
        alertas.append(("baja", "Hay lecturas estimadas",
                        f"{len(est)} de {len(rec)} horas de los últimos 60 días no son lecturas reales. "
                        "Si la factura sale alta, reclama que se facture con lectura real."))

    # --- consumo de madrugada (aparato encendido, fuga o enganche)
    por_dia = defaultdict(list)
    for h in horas_reales:
        if 1 <= h["h"] <= 5:
            por_dia[h["fecha"]].append(h["kwh"])
    minimos = sorted((f, min(v)) for f, v in por_dia.items() if len(v) >= 4)
    noct = None
    if len(minimos) >= 60:
        ult = [v for f, v in minimos[-30:]]
        ant = [v for f, v in minimos[-120:-30]]
        a, b = statistics.median(ult), statistics.median(ant)
        noct = {"ahora_w": a * 1000, "antes_w": b * 1000}
        if a > b * 1.3 and (a - b) > 0.05:
            alertas.append(("media", "Tu consumo de madrugada ha subido",
                            f"El consumo mínimo entre la 1 y las 5 pasó de unos {num(b * 1000, 0)} W a {num(a * 1000, 0)} W "
                            f"(unos {eur((a - b) * 24 * 365 * 0.2, 0)} al año). Busca un aparato que se quede encendido "
                            "(termo sin programar, calefactor, servidor). Si no encuentras nada, apaga el general y mira si el contador sigue contando."))

    # --- potencia
    pot_meses = sorted(maxp.items())[-12:]
    max_p1 = max((v["P1"] for _, v in pot_meses), default=0)
    max_p2 = max((v["P2"] for _, v in pot_meses), default=0)
    recomendacion = None
    if pot_meses and max_p1 > 0 and p1kw and cfg.get("recomendar_potencia", True):
        rp1 = escalon(max_p1 * (1 + aj["margen_potencia"]), aj["potencias_normalizadas_kw"])
        rp2 = max(rp1, p2kw or rp1, escalon(max_p2 * (1 + aj["margen_potencia"]), aj["potencias_normalizadas_kw"]))
        precio_p1 = (reg["pvpc_potencia_p1_eur_kw_anio"] + reg["pvpc_margen_comercializacion_eur_kw_anio"])
        if cfg["contrato_actual"].get("tipo") != "pvpc" and cfg["contrato_actual"].get("potencia_p1_eur_kw_dia"):
            precio_p1 = cfg["contrato_actual"]["potencia_p1_eur_kw_dia"] * 365
        ahorro = (p1kw - rp1) * precio_p1 * (1 + reg["impuesto_electrico_pct"] / 100) * (1 + reg["iva_pct"] / 100)
        recomendacion = {"p1": rp1, "p2": rp2, "ahorro": ahorro}
    excesos = [k for k, v in pot_meses if p1kw and (v["P1"] > p1kw or v["P2"] > (p2kw or p1kw))]
    if excesos:
        alertas.append(("baja", "Te has pasado de la potencia contratada",
                        f"En {', '.join(excesos)} la potencia máxima superó la contratada. No hay multa: si se mantiene, "
                        "salta el limitador. Si te pasa a menudo, programa el termo para que no coincida con la cocina."))

    # --- calidad de los datos de consumo
    dias_ventana = (fin - ini).days + 1
    dias_con_datos = len({h["fecha"] for h in anio})
    cobertura = dias_con_datos / dias_ventana if dias_ventana else 0
    defectuosos = [m for m in (contrato.get("_meses_defectuosos") or []) if ini.isoformat()[:7] <= m <= fin.isoformat()[:7]]
    dias_lect = len({h["fecha"] for h in anio if h["metodo"] == "lectura"})
    datos_ok = cobertura >= 0.8
    a_lecturas = bool(dias_lect)
    if not cfg.get("comparar", True):
        pass
    elif not datos_ok:
        alertas.append(("media", "Faltan datos de consumo",
                        f"Tengo datos fiables de {dias_con_datos} de {dias_ventana} días y no comparo tarifas para no darte cifras falsas. "
                        + (f"Datadis devolvió datos repetidos (el mismo día copiado todo el mes) en: {', '.join(defectuosos)}. " if defectuosos else "")
                        + "Añade la lectura real del contador de tu última factura en <code>lecturas_reales</code> de config.json "
                        "(o pídeselo a Claude) y el próximo informe compara."))
    elif defectuosos:
        info.append(f"Datadis devolvió datos repetidos en {len(defectuosos)} meses. Para esos días uso las lecturas reales "
                    f"del contador de tus facturas ({dias_lect} días): el total es exacto, pero el reparto hora a hora es aproximado, "
                    "así que el PVPC sale con su precio medio de cada periodo.")

    # --- costes
    tarifas = [{"nombre": "PVPC (tarifa regulada)", "tipo": "pvpc", "_pvpc": True}]
    actual = dict(cfg["contrato_actual"])
    actual["_actual"] = True
    if actual.get("tipo") == "pvpc":
        tarifas[0]["_actual"] = True
        tarifas[0]["nombre"] = "PVPC (tu tarifa actual)"
    elif actual.get("tipo") in ("fija", "periodos", "cuota"):
        tarifas.insert(0, actual)
    tarifas += [dict(o) for o in cfg.get("ofertas", []) if o.get("activa")]
    if not datos_ok or not cfg.get("comparar", True):
        tarifas = []

    precios = {}
    if modo != "mensual":
        tarifas = []
    if anio and tarifas:
        meses_v = sorted({h["fecha"][:7] for h in anio})
        for ym in meses_v:
            try:
                precios.update(pvpc_mes(int(ym[:4]), int(ym[5:7]), hoy))
            except Exception as e:
                log(f"PVPC {ym}: {e}")
    filas = []
    if p1kw:
        for t in tarifas:
            c = coste(t, anio, p1kw, p2kw, reg, precios)
            filas.append({"t": t, "c": c})
            if recomendacion and c:
                c2 = coste(t, anio, recomendacion["p1"], recomendacion["p2"], reg, precios)
                filas[-1]["c_rec"] = c2
    filas_ok = sorted([f for f in filas if f["c"]], key=lambda f: f["c"]["anual"])
    actual_fila = next((f for f in filas if f["t"].get("_actual")), None)

    # --- reparto de consumo
    reparto = defaultdict(float)
    mensual = defaultdict(float)
    for h in anio:
        reparto[periodo_2_0td(dt.date.fromisoformat(h["fecha"]), h["h"])] += h["kwh"]
        mensual[h["fecha"][:7]] += h["kwh"]
    total_kwh = sum(reparto.values())

    return {
        "alertas": alertas, "contrato": contrato, "p1kw": p1kw, "p2kw": p2kw, "ini": ini, "fin": fin,
        "filas": filas_ok, "filas_sin_datos": [f for f in filas if not f["c"]], "actual": actual_fila,
        "recomendacion": recomendacion, "pot_meses": pot_meses, "max_p1": max_p1, "max_p2": max_p2,
        "reparto": dict(reparto), "total_kwh": total_kwh, "mensual": dict(sorted(mensual.items())),
        "noct": noct, "anio": anio, "todas": horas, "marketer": marketer, "info": info,
        "datos_ok": datos_ok and cfg.get("comparar", True), "cobertura": cobertura, "excesos": excesos,
        "nombre": cfg.get("nombre", ""), "cups": contrato.get("cups", ""), "con_lecturas": a_lecturas,
    }


# --------------------------------------------------------------------------- LuzFija

def csv_luzfija(horas, destino):
    """CSV horario en formato tipo Datadis que el importador de LuzFija reconoce."""
    with open(destino, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Fecha", "Hora", "AE_kWh", "REAL/ESTIMADO"])
        for h in sorted(horas, key=lambda x: (x["fecha"], x["h"])):
            d = dt.date.fromisoformat(h["fecha"])
            w.writerow([d.strftime("%Y/%m/%d"), f"{h['h'] + 1:02d}:00",
                        f"{h['kwh']:.3f}".replace(".", ","), h["metodo"] or "R"])


def url_luzfija(a):
    r = a["reparto"]
    dias = len({h["fecha"] for h in a["anio"]}) or 365
    p = {"p1": kw_luzfija(a["p1kw"]), "p2": kw_luzfija(a["p2kw"]), "zonaFiscal": "Península"}
    if a.get("datos_ok", True):
        p.update({"dias": str(dias), "cPunta": str(round(r.get("punta", 0))),
                  "cLlano": str(round(r.get("llano", 0))), "cValle": str(round(r.get("valle", 0)))})
    return LUZFIJA + "?" + urllib.parse.urlencode(p)


# --------------------------------------------------------------------------- correo HTML

COL = {"tinta": "#1f2933", "suave": "#5f6b7a", "linea": "#e4e7eb", "fondo": "#f5f7fa",
       "azul": "#2563eb", "verde": "#15803d", "ambar": "#b45309", "rojo": "#b91c1c"}


def barra(valor, maximo, color, ancho=220, alto=10):
    w = 0 if not maximo else max(2, int(ancho * valor / maximo))
    return (f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse"><tr>'
            f'<td style="background:{color};width:{w}px;height:{alto}px;font-size:0;line-height:0">&nbsp;</td>'
            f'<td style="width:{ancho - w}px;height:{alto}px;font-size:0;line-height:0">&nbsp;</td></tr></table>')


def seccion(titulo, cuerpo):
    return (f'<tr><td style="padding:20px 24px 4px 24px"><h2 style="margin:0 0 8px 0;font-size:17px;color:{COL["tinta"]}">'
            f'{titulo}</h2>{cuerpo}</td></tr>')


def instrucciones(a, cfg):
    pasos = []
    best = a["filas"][0] if a["filas"] else None
    act = a["actual"]
    umbral = cfg["ajustes"]["ahorro_minimo_para_cambiar_eur_anio"]
    for nivel, titulo, _ in a["alertas"]:
        if nivel == "alta":
            pasos.append(f"<b>Urgente:</b> revisa la alerta \"{html.escape(titulo)}\" antes que nada.")
    if cfg.get("nota"):
        pasos.append(cfg["nota"])
    if best and act and act.get("c") and best is not act:
        ahorro = act["c"]["anual"] - best["c"]["anual"]
        if ahorro >= umbral:
            n = html.escape(best["t"]["nombre"])
            segundo = a["filas"][1] if len(a["filas"]) > 1 else None
            if best["t"].get("_pvpc") and segundo and not segundo["t"].get("_actual") \
                    and segundo["c"]["anual"] - best["c"]["anual"] < 40:
                dif2 = segundo["c"]["anual"] - best["c"]["anual"]
                pasos.append(f"El <b>PVPC</b> sale el más barato (unos {eur(ahorro, 0)} menos al año que tu tarifa), pero "
                             f"<b>{html.escape(segundo['t']['nombre'])}</b> queda a solo {eur(dif2, 0)} al año y su precio no cambia "
                             "cada hora. Cualquiera de las dos es buena; el PVPC sube en épocas de crisis como la de este otoño.")
            elif best["t"].get("_pvpc"):
                pasos.append(f"Con tu consumo real, el <b>PVPC</b> te habría ahorrado unos <b>{eur(ahorro, 0)} al año</b>. "
                             "Para pasarte: web de una comercializadora de referencia (Curenergía, Energía XXI, Comercializadora "
                             "Regulada o Baser), alta en PVPC con tu CUPS y tu IBAN. Sin corte ni coste. Revisa antes si tienes permanencia.")
            else:
                pasos.append(f"<b>{n}</b> te habría ahorrado unos <b>{eur(ahorro, 0)} al año</b>. Comprueba en su web que el precio "
                             "sigue igual, sin permanencia ni servicios añadidos, y contrata online. Luego actualiza "
                             "<code>contrato_actual</code> de este suministro en config.json.")
        else:
            pasos.append(f"Ninguna opción de tu lista ahorra más de {eur(umbral, 0)} al año. Quédate como estás.")
    rec = a["recomendacion"]
    if rec and a["p1kw"] and rec["p1"] < a["p1kw"] - 1e-6:
        pasos.append(f"Baja la potencia P1 de <b>{num(a['p1kw'])} a {num(rec['p1'])} kW</b> y deja P2 en {num(rec['p2'])} kW "
                     f"(ahorro aproximado {eur(rec['ahorro'], 0)} al año). Se pide a tu comercializadora; bajar cuesta unos 9 € "
                     "una vez. Vive un mes así y mira si salta la luz.")
    if cfg.get("comparar", True):
        if a.get("datos_ok"):
            pasos.append(f'Contrasta con <b>LuzFija</b>: abre <a href="{html.escape(url_luzfija(a))}">este enlace</a> (lleva tus '
                         'potencias y tus kWh por periodo) y pulsa <i>Calcular</i>. Si alguna oferta de su ranking sale mejor que las '
                         'de tu lista, añádela a <code>ofertas</code> de este suministro con <code>"activa": true</code>.')
        else:
            pasos.append("Añade la lectura real del contador de tu última factura a <code>lecturas_reales</code> (o pídeselo a Claude) "
                         f'para que el próximo informe compare. Mientras tanto, <a href="{html.escape(url_luzfija(a))}">LuzFija</a> '
                         "lleva ya tus potencias.")
    if not pasos:
        return '<p style="margin:0">Nada que hacer este mes.</p>'
    return "<ol style=\"margin:0;padding-left:20px\">" + "".join(
        f'<li style="margin:0 0 10px 0">{p}</li>' for p in pasos) + "</ol>"


def resumen_linea(a):
    act, best = a["actual"], (a["filas"][0] if a["filas"] else None)
    partes = []
    if act and act.get("c"):
        partes.append(f"pagas unos <b>{eur(act['c']['anual'], 0)}/año</b>")
        if best and best is not act and act["c"]["anual"] - best["c"]["anual"] > 1:
            partes.append(f"<b style=\"color:{COL['verde']}\">{html.escape(best['t']['nombre'])}</b> te ahorraría "
                          f"{eur(act['c']['anual'] - best['c']['anual'], 0)}")
        elif best is act:
            partes.append("tu tarifa es la más barata de tu lista")
    altas = sum(1 for n, _, _ in a["alertas"] if n == "alta")
    partes.append(f"{len(a['alertas'])} alerta(s)" + (f", {altas} urgente(s)" if altas else "") if a["alertas"] else "sin alertas")
    return f"<b>{html.escape(a['nombre'] or a['cups'])}</b>: " + "; ".join(partes) + "."


def bloque_suministro(a, cfg, hoy, modo):
    c = COL
    color_alerta = {"alta": c["rojo"], "media": c["ambar"], "baja": c["suave"]}
    filas = [
        f'<tr><td style="padding:22px 24px 6px 24px;border-top:6px solid {c["fondo"]}">'
        f'<div style="font-size:19px;font-weight:bold">{html.escape(a["nombre"] or "Suministro")}</div>'
        f'<div style="font-size:12px;color:{c["suave"]};margin-top:3px">{html.escape(a["marketer"] or "-")} · '
        f'P1 {num(a["p1kw"])} kW / P2 {num(a["p2kw"])} kW · CUPS {html.escape(a["cups"][:20])}</div></td></tr>']

    if a["alertas"]:
        cuerpo = ""
        for nivel, t, d in a["alertas"]:
            cuerpo += (f'<div style="border-left:4px solid {color_alerta[nivel]};background:{c["fondo"]};padding:10px 12px;margin:0 0 8px 0">'
                       f'<div style="font-weight:bold;color:{color_alerta[nivel]}">{html.escape(t)}</div>'
                       f'<div style="font-size:14px;margin-top:4px;line-height:1.45">{d}</div></div>')
        filas.append(seccion("Alertas", cuerpo))
    if modo != "mensual":
        return filas

    filas.append(seccion("Qué hacer", f'<div style="font-size:14px;line-height:1.5">{instrucciones(a, cfg)}</div>'))

    act = a["actual"]
    if a["filas"]:
        mx = max(f["c"]["anual"] for f in a["filas"])
        t = (f'<table width="100%" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:14px">'
             f'<tr style="color:{c["suave"]};font-size:12px;text-align:left"><th>Tarifa</th><th style="text-align:right">Al año</th>'
             f'<th style="text-align:right">Al mes</th><th style="text-align:right">€/kWh real</th></tr>')
        base = act["c"]["anual"] if act and act.get("c") else None
        for i, f in enumerate(a["filas"]):
            cc = f["c"]
            tag = " <span style=\"font-size:11px;color:#fff;background:%s;padding:1px 6px;border-radius:8px\">%s</span>"
            etiqueta = tag % (c["azul"], "tu tarifa") if f["t"].get("_actual") else (tag % (c["verde"], "la más barata") if i == 0 else "")
            dif = ""
            if base and not f["t"].get("_actual"):
                d = cc["anual"] - base
                dif = f'<div style="font-size:12px;color:{c["verde"] if d < 0 else c["rojo"]}">{"-" if d < 0 else "+"}{eur(abs(d), 0)}/año</div>'
            t += (f'<tr style="border-top:1px solid {c["linea"]}"><td>{html.escape(f["t"]["nombre"])}{etiqueta}'
                  f'<div style="margin-top:4px">{barra(cc["anual"], mx, c["azul"] if f["t"].get("_actual") else "#94a3b8")}</div></td>'
                  f'<td style="text-align:right;white-space:nowrap"><b>{eur(cc["anual"], 0)}</b>{dif}</td>'
                  f'<td style="text-align:right">{eur(cc["anual"] / 12, 0)}</td>'
                  f'<td style="text-align:right">{num(cc["eur_kwh"], 3)}</td></tr>')
        t += "</table>"
        origen = ("las lecturas reales del contador de tus facturas" if a.get("con_lecturas") else "tu consumo real hora a hora")
        nota = (f'<p style="font-size:12px;color:{c["suave"]};margin:8px 0 0 0">Calculado con {origen} de '
                f'{a["ini"].strftime("%m/%Y")} a {a["fin"].strftime("%m/%Y")} ({num(a["total_kwh"], 0)} kWh, pasado a un año), '
                'tus potencias actuales, PVPC oficial de Red Eléctrica, impuesto eléctrico, bono social, contador e IVA. '
                'Las ofertas usan los precios de config.json: si una compañía los cambió, actualízalos.</p>')
        for i_ in a.get("info", []):
            nota += f'<p style="font-size:12px;color:{c["suave"]};margin:6px 0 0 0">{i_}</p>'
        if a["filas_sin_datos"]:
            nota += (f'<p style="font-size:12px;color:{c["ambar"]};margin:6px 0 0 0">Sin calcular por falta de precios: '
                     + ", ".join(html.escape(f["t"]["nombre"]) for f in a["filas_sin_datos"]) + ".</p>")
        filas.append(seccion("Cuánto habrías pagado en 12 meses", t + nota))

    if a["pot_meses"]:
        mx = max(a["p1kw"] or 0, a["max_p1"], a["max_p2"]) or 1
        t = f'<table cellpadding="3" cellspacing="0" style="border-collapse:collapse;font-size:13px">'
        for k, v in a["pot_meses"]:
            t += (f'<tr><td style="color:{c["suave"]};width:52px">{k[5:]}/{k[2:4]}</td>'
                  f'<td>{barra(v["P1"], mx, c["azul"], 170, 9)}</td><td style="text-align:right;width:58px">{num(v["P1"])}</td>'
                  f'<td>{barra(v["P2"], mx, "#7c3aed", 90, 9)}</td><td style="text-align:right;width:52px">{num(v["P2"])}</td></tr>')
        t += "</table>"
        txt = (f'<p style="font-size:14px;margin:0 0 8px 0">Máximo en 12 meses: <b>{num(a["max_p1"])} kW</b> en punta/llano '
               f'(azul) y <b>{num(a["max_p2"])} kW</b> en valle (morado). Contratado: <b>{num(a["p1kw"])} / {num(a["p2kw"])} kW</b>.</p>')
        rec = a["recomendacion"]
        if rec:
            if rec["p1"] < (a["p1kw"] or 0) - 1e-6:
                txt += (f'<p style="font-size:14px;margin:0 0 8px 0">Se podría bajar a <b>P1 = {num(rec["p1"])} kW</b> '
                        f'(P2 {num(rec["p2"])} kW): unos <b>{eur(rec["ahorro"], 0)} al año</b>.</p>')
            elif rec["p1"] > (a["p1kw"] or 0) + 1e-6:
                txt += (f'<p style="font-size:14px;margin:0 0 8px 0">Vas justo: tu máximo más un margen pide {num(rec["p1"])} kW. '
                        'No la bajes.</p>')
            else:
                txt += '<p style="font-size:14px;margin:0 0 8px 0">Tu potencia está bien ajustada.</p>'
        elif cfg.get("nota_potencia"):
            txt += f'<p style="font-size:14px;margin:0 0 8px 0">{cfg["nota_potencia"]}</p>'
        t += (f'<p style="font-size:12px;color:{c["suave"]};margin:6px 0 0 0">La barra entera equivale a {num(mx)} kW. '
              'Datadis registra la media de cada cuarto de hora, no los picos de un segundo.</p>')
        filas.append(seccion("Potencia máxima por mes", txt + t))

    if a["mensual"] and a.get("datos_ok"):
        mx = max(a["mensual"].values())
        t = f'<table cellpadding="3" cellspacing="0" style="border-collapse:collapse;font-size:13px">'
        for k, v in a["mensual"].items():
            t += (f'<tr><td style="color:{c["suave"]};width:52px">{k[5:]}/{k[2:4]}</td>'
                  f'<td>{barra(v, mx, "#0f766e", 200, 9)}</td><td style="text-align:right;width:70px">{num(v, 0)} kWh</td></tr>')
        t += "</table>"
        r, tot = a["reparto"], a["total_kwh"] or 1
        rep_ = " · ".join(f'{n.capitalize()} <b>{num(100 * r.get(n, 0) / tot, 0)} %</b>' for n in ("punta", "llano", "valle"))
        extra = ""
        if a["noct"]:
            extra = (f'<p style="font-size:13px;color:{c["suave"]};margin:8px 0 0 0">Consumo mínimo de madrugada: '
                     f'{num(a["noct"]["ahora_w"], 0)} W (antes {num(a["noct"]["antes_w"], 0)} W).</p>')
        filas.append(seccion("Tu consumo", f'<p style="font-size:14px;margin:0 0 8px 0">{num(a["total_kwh"], 0)} kWh en el periodo. '
                             f'Reparto: {rep_}.</p>' + t + extra))
    return filas


def html_correo(bloques, cfg, hoy, modo, demo):
    c = COL
    titulo = "Informe mensual de tu luz" if modo == "mensual" else "Chequeo semanal de tu luz"
    filas = [
        f'<tr><td style="padding:24px 24px 14px 24px;background:{c["tinta"]};color:#fff">'
        f'<div style="font-size:13px;opacity:.8">{hoy.strftime("%d/%m/%Y")}{" · DATOS DE PRUEBA" if demo else ""}</div>'
        f'<div style="font-size:22px;font-weight:bold;margin-top:4px">{titulo}</div></td></tr>']
    resumen = "".join(f'<p style="margin:0 0 6px 0">{resumen_linea(a)}</p>' for a, _ in bloques)
    filas.append(seccion("En resumen", f'<div style="font-size:15px;line-height:1.5">{resumen}</div>'))
    for a, scfg in bloques:
        filas += bloque_suministro(a, scfg, hoy, modo)

    if modo == "mensual":
        rec = []
        if hoy.month in (1, 4, 7, 10):
            rec.append("<b>Gas:</b> la TUR cambia este mes. Mira el precio nuevo en la web de tu comercializadora de referencia "
                       "o en la OCU y, si estás en el mercado libre, compáralo con la TUR.")
        rec.append("Manda la lectura del contador del gas (app Yoleogas o la de tu compañía) para que no te facturen estimado.")
        rec.append('LuzFija compara todas las ofertas del mercado con tus datos y no guarda nada. Cada suministro trae su enlace '
                   'en "Qué hacer".')
        filas.append(seccion("Recordatorios", "<ul style=\"margin:0;padding-left:20px;font-size:14px;line-height:1.5\">"
                             + "".join(f"<li style=\"margin:0 0 8px 0\">{r}</li>" for r in rec) + "</ul>"))

    filas.append(f'<tr><td style="padding:16px 24px 24px 24px;font-size:11px;color:{c["suave"]}">'
                 'Generado por luzgas.py con datos de Datadis, tus facturas y precios del PVPC de Red Eléctrica. '
                 f'Valores regulados de {cfg["regulado"]["anio"]}: revísalos cada enero en config.json.</td></tr>')
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>{titulo}</title></head><body style="margin:0;background:{c["fondo"]};font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:{c["tinta"]}">'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="background:{c["fondo"]}"><tr><td align="center" style="padding:16px 8px">'
            f'<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#fff;border:1px solid {c["linea"]}">'
            + "".join(filas) + "</table></td></tr></table></body></html>")


def texto_plano(bloques):
    l = ["Informe de luz"]
    for a, _ in bloques:
        l.append("")
        l.append(a["nombre"] or a["cups"])
        for _, t, d in a["alertas"]:
            l.append(f"  ALERTA: {t}")
        for f in a["filas"]:
            l.append(f"  {f['t']['nombre']}: {f['c']['anual']:.0f} €/año")
        l.append(f"  LuzFija: {url_luzfija(a)}")
    return "\n".join(l)


def enviar(cfg, asunto, cuerpo_html, cuerpo_txt, adjuntos=()):
    e = cfg["email"]
    pwd = llavero(e.get("keychain_smtp", "luzgas-smtp"), e["de"])
    if not pwd:
        raise RuntimeError("No hay contraseña de aplicación SMTP en el Llavero.")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = asunto, e["de"], e["para"]
    msg.set_content(cuerpo_txt)
    msg.add_alternative(cuerpo_html, subtype="html")
    for adj in adjuntos:
        if adj and Path(adj).exists():
            msg.add_attachment(Path(adj).read_bytes(), maintype="text", subtype="csv", filename=Path(adj).name)
    with smtplib.SMTP(e.get("smtp_host", "smtp.gmail.com"), int(e.get("smtp_puerto", 587)), timeout=60) as s:
        s.starttls()
        s.login(e["de"], pwd)
        s.send_message(msg)


def notificar(titulo, texto):
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'display notification {json.dumps(texto)} with title {json.dumps(titulo)}'], timeout=10)
    except Exception:
        pass


# --------------------------------------------------------------------------- principal

def decidir_modo(cfg, estado, hoy):
    r = cfg.get("recurrencia", {})
    if hoy.day >= int(r.get("dia_del_mes_informe_completo", 6)) and estado.get("ultimo_mensual") != hoy.strftime("%Y-%m"):
        return "mensual"
    if not r.get("chequeo_semanal", False):
        return None
    ult = estado.get("ultimo_semanal")
    if not ult or (hoy - dt.date.fromisoformat(ult)).days >= int(r.get("dias_entre_chequeos_rapidos", 7)):
        return "semanal"
    return None


def suministros_de(cfg):
    """Lista de configuraciones por suministro (cada una con los ajustes globales mezclados)."""
    base = {k: v for k, v in cfg.items() if k != "suministros"}
    lista = cfg.get("suministros") or [{"nombre": "", "cups": cfg.get("cups", ""),
                                        "contrato_actual": cfg.get("contrato_actual", {}), "ofertas": cfg.get("ofertas", [])}]
    out = []
    for s in lista:
        if s.get("activo", True) is False:
            continue
        m = dict(base)
        m.update(s)
        m.setdefault("contrato_actual", {})
        m.setdefault("ofertas", [])
        out.append(m)
    return out


def anadir_lectura(cfg_path, cups, fecha, p1, p2, p3):
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for s in cfg.get("suministros", []):
        if s.get("cups", "")[:20] == cups[:20]:
            lec = [x for x in s.setdefault("lecturas_reales", []) if x["fecha"] != fecha]
            lec.append({"fecha": fecha, "p1": float(p1), "p2": float(p2), "p3": float(p3)})
            s["lecturas_reales"] = sorted(lec, key=lambda x: x["fecha"])
            cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Lectura añadida a {s.get('nombre') or cups}: {fecha} P1 {p1} P2 {p2} P3 {p3}")
            return
    sys.exit(f"No encuentro el CUPS {cups} en config.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", choices=["auto", "mensual", "semanal"], default="mensual")
    ap.add_argument("--demo", action="store_true", help="datos inventados, no toca Datadis")
    ap.add_argument("--sin-correo", action="store_true")
    ap.add_argument("--abrir", action="store_true", help="abre el informe HTML al terminar")
    ap.add_argument("--anadir-lectura", nargs=5, metavar=("CUPS", "FECHA", "P1", "P2", "P3"),
                    help="añade una lectura real del contador (kWh acumulados por periodo) y sale")
    args = ap.parse_args()

    cfg_path = BASE / "config.json"
    if args.anadir_lectura:
        anadir_lectura(cfg_path, *args.anadir_lectura)
        return
    if not cfg_path.exists():
        cfg_path = BASE / "config.example.json"
        if not args.demo:
            sys.exit("Falta config.json. Copia config.example.json y rellénalo.")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    estado = json.loads(ESTADO.read_text()) if ESTADO.exists() and not args.demo else {}
    hoy = dt.date.today()

    modo = args.modo
    if modo == "auto":
        modo = decidir_modo(cfg, estado, hoy)
        if not modo:
            log("Nada que hacer hoy.")
            return
    log(f"Modo {modo}{' (demo)' if args.demo else ''}")

    sums_cfg = suministros_de(cfg)
    bloques, errores, csvs, resumen_json = [], [], [], []
    REPORTES.mkdir(exist_ok=True)
    sufijo = "-demo" if args.demo else ""
    meses = 13 if modo == "mensual" else 5

    dd = sums = None
    if not args.demo:
        try:
            dd, sums = abrir_datadis(cfg)
        except ErrorDatadis as e:
            errores.append(str(e))

    for scfg in (sums_cfg[:1] if args.demo else sums_cfg):
        if args.demo:
            scfg = dict(scfg)
            scfg.update({"nombre": "Vivienda de prueba", "comparar": True, "recomendar_potencia": True})
            scfg["contrato_actual"] = {"nombre": "Tarifa fija actual (demo)", "tipo": "fija", "potencia_p1_eur_kw_dia": 0.1095,
                                       "potencia_p2_eur_kw_dia": 0.0466, "energia_eur_kwh": 0.1399,
                                       "comercializadora_esperada": "OTRA COMERCIALIZADORA S.L."}
            scfg["ofertas"] = [dict(o, activa=True, nombre=o["nombre"].replace("EJEMPLO, cámbialo: ", "Demo: "))
                               for o in cfg.get("ofertas", []) or sums_cfg[0].get("ofertas", [])]
            horas, maxp, contrato = datos_demo(hoy)
            est_s = {"ultima_potencia": [4.6, 4.6]}
        else:
            if dd is None:
                break
            try:
                horas, maxp, contrato = datos_reales(scfg, dd, sums, hoy, meses)
            except ErrorDatadis as e:
                errores.append(f"{scfg.get('nombre')}: {e}")
                continue
            est_s = estado.setdefault("suministros", {}).setdefault(scfg.get("cups", "")[:20], {})
        a = analizar(scfg, horas, maxp, contrato, hoy, est_s, modo)
        a["nombre"] = scfg.get("nombre") or a["nombre"]
        bloques.append((a, scfg))
        if modo == "mensual" and a.get("datos_ok") and not a.get("con_lecturas"):
            out_csv = REPORTES / f"{hoy.isoformat()}-curva-horaria-{(scfg.get('cups') or 'x')[-6:]}{sufijo}.csv"
            csv_luzfija(a["anio"], out_csv)
            csvs.append(str(out_csv))
        ult_lec = max((x["fecha"] for x in scfg.get("lecturas_reales", [])), default=None)
        resumen_json.append({
            "nombre": a["nombre"], "cups": scfg.get("cups", ""), "comercializadora": a["marketer"],
            "datos_ok": a.get("datos_ok"), "usa_lecturas": a.get("con_lecturas"), "ultima_lectura": ult_lec,
            "necesita_lectura_nueva": bool(scfg.get("comparar", True) and a.get("con_lecturas") and ult_lec
                                           and (hoy - dt.date.fromisoformat(ult_lec)).days > 35),
            "alertas": [t for _, t, _ in a["alertas"]],
        })
        if not args.demo:
            est_s["ultima_comercializadora"] = a["marketer"] or est_s.get("ultima_comercializadora")
            if contrato.get("contractedPowerkW"):
                est_s["ultima_potencia"] = contrato["contractedPowerkW"]

    out_html = REPORTES / f"{hoy.isoformat()}-{modo}{sufijo}.html"
    if bloques:
        cuerpo = html_correo(bloques, cfg, hoy, modo, args.demo)
    else:
        cuerpo = ("<p>No pude leer Datadis.</p>" + "".join(f"<p><code>{html.escape(x)}</code></p>" for x in errores))
    out_html.write_text(cuerpo, encoding="utf-8")
    log(f"Informe: {out_html}")

    urgente = any(n == "alta" for a, _ in bloques for n, _, _ in a["alertas"])
    asunto = (f"{'[ALERTA] ' if urgente else ''}{'Informe mensual' if modo == 'mensual' else 'Chequeo semanal'} "
              f"de tu luz · {hoy.strftime('%d/%m/%Y')}")
    if errores and not bloques:
        asunto = "Monitor de luz: error al leer Datadis"
    (REPORTES / f"ultimo{sufijo}.json").write_text(json.dumps({
        "fecha": hoy.isoformat(), "modo": modo, "asunto": asunto, "html": str(out_html), "csv": csvs,
        "texto": texto_plano(bloques) if bloques else "\n".join(errores),
        "errores": errores, "suministros": resumen_json}, indent=2, ensure_ascii=False), encoding="utf-8")

    metodo = cfg["email"].get("metodo", "claude")
    hay_alertas = any(a["alertas"] for a, _ in bloques) or bool(errores)
    mandar = cfg["email"].get("activo") and not args.sin_correo and metodo == "smtp"
    if modo == "semanal" and cfg["email"].get("semanal_solo_si_hay_alertas", True) and not hay_alertas:
        mandar = False
    if mandar:
        try:
            enviar(cfg, asunto, cuerpo, texto_plano(bloques), csvs)
            log("Correo enviado.")
        except Exception as e:
            log(f"No pude mandar el correo: {e}")
            notificar("Monitor de luz", f"Informe listo pero sin correo: {e}"[:200])

    if not args.demo and bloques:
        estado["ultimo_semanal"] = hoy.isoformat()
        if modo == "mensual":
            estado["ultimo_mensual"] = hoy.strftime("%Y-%m")
        ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False))
    if args.abrir:
        subprocess.run(["/usr/bin/open", str(out_html)])
    if errores:
        for e in errores:
            log(f"ERROR: {e}")
        if not bloques:
            sys.exit(2)


if __name__ == "__main__":
    main()
