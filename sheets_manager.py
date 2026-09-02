"""
sheets_manager.py
Módulo de acceso a Google Sheets para Sistema de Asistencia RRHH - Quski
"""

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
import pandas as pd
from datetime import datetime, date, timedelta
import hashlib
import hmac
import os
import base64
import time
from zoneinfo import ZoneInfo

# Segundos que se reutiliza una lectura de una hoja antes de volver a pedirla.
# Google Sheets limita a ~60 lecturas por minuto y por usuario; sin esta caché
# una sola pantalla puede gastar 5 o 6 lecturas y agotar la cuota.
_CACHE_TTL = 15

ZONA_POR_DEFECTO = "America/Guayaquil"


def zona_horaria(config: dict | None = None) -> ZoneInfo:
    """Zona horaria configurada (America/Guayaquil por defecto)."""
    nombre = (config or {}).get("Zona_Horaria", ZONA_POR_DEFECTO) or ZONA_POR_DEFECTO
    try:
        return ZoneInfo(nombre)
    except Exception:
        return ZoneInfo(ZONA_POR_DEFECTO)


def ahora_local(config: dict | None = None) -> datetime:
    """Fecha y hora actuales en la zona horaria de la empresa.

    Importante: el servidor de Streamlit Cloud corre en UTC, así que
    datetime.now() devuelve una hora cinco horas adelantada respecto a Ecuador.
    Todo registro de asistencia debe pasar por aquí."""
    return datetime.now(zona_horaria(config))


def hoy_local(config: dict | None = None) -> date:
    """Fecha de hoy en la zona horaria de la empresa."""
    return ahora_local(config).date()

# ── Constantes ────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1gaPrP95SF0xat7xRs94CMyH22LeIoolEom1OjCDAA2I"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = {
    "Empleados":    ["ID_Empleado", "Nombre", "Email", "Area", "Email_Jefe", "Horario_Inicio", "Horario_Fin"],
    "Asistencia":   ["Fecha", "ID_Empleado", "Nombre", "Hora_Entrada", "Hora_Salida", "Estado", "Minutos_Atraso", "Observaciones"],
    "Permisos":     ["ID_Permiso", "Fecha", "ID_Empleado", "Horas_Solicitadas", "Motivo", "Estado",
                     "Horas_Usadas_Mes", "Aprobado_Por",
                     "Aprobado_Jefe", "Fecha_Aprob_Jefe", "Aprobado_RRHH", "Fecha_Aprob_RRHH",
                     "Motivo_Rechazo", "Rechazado_Por"],
    "Vacaciones":   ["ID_Vacacion", "ID_Empleado", "Fecha_Inicio", "Fecha_Fin", "Dias_Habiles", "Estado",
                     "Aprobado_Por",
                     "Aprobado_Jefe", "Fecha_Aprob_Jefe", "Aprobado_RRHH", "Fecha_Aprob_RRHH",
                     "Motivo_Rechazo", "Rechazado_Por"],
    "Horas_Extras": ["ID", "Fecha", "ID_Empleado", "Horas_Extra", "Motivo", "Aprobado_Por", "Estado"],
    "Configuracion":["Key", "Valor"],
    "Usuarios":          ["ID_Empleado", "Password_Hash", "Rol"],
    "Llamados_Atencion": ["ID_Llamado", "Fecha", "ID_Empleado", "Nombre", "Tipo", "Motivo",
                          "Atrasos_Acumulados", "Registrado_Por", "Estado"],
}

CONFIG_DEFAULTS = {
    "Horario_Inicio":              "09:00",
    "Horario_Fin":                 "17:30",
    "Tolerancia_Minutos":          "0",
    "Horas_Permiso_Mensual":       "3",
    "Email_RRHH":                  "rrhh@quski.ec",
    "Zona_Horaria":                "America/Guayaquil",
    "Tardanzas_Llamado_Verbal":    "3",
    "Tardanzas_Llamado_Escrito":   "5",
    "Tardanzas_Suspension":        "8",
}

# ── Estados del flujo de aprobación ───────────────────────────────────────────
# Pendiente_Jefe  → esperando al jefe inmediato
# Pendiente_RRHH  → jefe aprobó, esperando a RRHH
# Aprobado        → aprobado por ambos (o solo RRHH si no hay jefe asignado)
# Rechazado       → rechazado por el jefe o por RRHH
EST_PEND_JEFE = "Pendiente_Jefe"
EST_PEND_RRHH = "Pendiente_RRHH"
EST_APROBADO  = "Aprobado"
EST_RECHAZADO = "Rechazado"

# Estados que aún cuentan contra el cupo mensual de permisos
ESTADOS_VIGENTES = {EST_PEND_JEFE.lower(), EST_PEND_RRHH.lower(), EST_APROBADO.lower(),
                    "pendiente", "pendiente_aprobacion"}


# ── Seguridad: hashing de contraseñas ─────────────────────────────────────────
# PBKDF2-HMAC-SHA256 con salt aleatorio por usuario. Reemplaza al SHA-256 plano,
# que era vulnerable a tablas rainbow porque el mismo password siempre producía
# el mismo hash. Los hashes antiguos se siguen aceptando y se migran solos en el
# primer inicio de sesión correcto.
_PBKDF2_ITERACIONES = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Genera un hash PBKDF2 con salt. Formato: pbkdf2$<iter>$<salt_b64>$<hash_b64>"""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERACIONES)
    return "pbkdf2${}${}${}".format(
        _PBKDF2_ITERACIONES,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, almacenado: str) -> bool:
    """Verifica la contraseña contra el hash almacenado (nuevo o heredado)."""
    almacenado = str(almacenado or "").strip()
    if not almacenado:
        return False
    if almacenado.startswith("pbkdf2$"):
        try:
            _, iteraciones, salt_b64, hash_b64 = almacenado.split("$")
            salt = base64.b64decode(salt_b64)
            esperado = base64.b64decode(hash_b64)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iteraciones))
            return hmac.compare_digest(dk, esperado)
        except Exception:
            return False
    # Hash heredado: SHA-256 sin salt
    heredado = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(heredado, almacenado)


def es_hash_heredado(almacenado: str) -> bool:
    """True si el hash usa el formato antiguo y conviene migrarlo."""
    return bool(almacenado) and not str(almacenado).strip().startswith("pbkdf2$")


def password_debil(password: str) -> str | None:
    """Devuelve un mensaje si la contraseña no cumple la política mínima, o None."""
    if len(password) < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if password.isdigit():
        return "La contraseña no puede ser solo números."
    if password.isalpha():
        return "La contraseña debe incluir al menos un número o símbolo."
    comunes = {"12345678", "password", "quski2026", "contrasena", "administrador", "abc12345"}
    if password.lower() in comunes:
        return "Esa contraseña es demasiado común. Elige otra."
    return None


# ── SheetsManager ─────────────────────────────────────────────────────────────
class SheetsManager:
    """Wrapper sobre gspread para el spreadsheet de Asistencia RRHH."""

    def __init__(self, credentials_source):
        """
        credentials_source: ruta a credentials.json (str)
                           ó dict con el contenido del JSON de service account.
        """
        if isinstance(credentials_source, dict):
            creds = Credentials.from_service_account_info(credentials_source, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(credentials_source, scopes=SCOPES)

        self.client = gspread.authorize(creds)
        self.spreadsheet = self.client.open_by_key(SPREADSHEET_ID)
        # Caché de lecturas y último error de lectura, para no confundir un
        # fallo de la API con "no hay datos".
        self._cache = {}
        self.ultimo_error = None

    # ── Helpers de bajo nivel ────────────────────────────────────────────────

    def _sheet(self, name: str):
        return self.spreadsheet.worksheet(name)

    def _invalidar_cache(self, sheet_name: str = None):
        """Descarta la lectura guardada tras escribir en una hoja."""
        if sheet_name:
            self._cache.pop(sheet_name, None)
        else:
            self._cache.clear()

    def get_df(self, sheet_name: str, usar_cache: bool = True) -> pd.DataFrame:
        """Lee la hoja y devuelve DataFrame. Normaliza nombres de columnas.

        Reutiliza la última lectura durante _CACHE_TTL segundos: una sola
        pantalla puede consultar la misma hoja varias veces y Google Sheets
        corta el acceso al pasarse de cuota.
        """
        if usar_cache:
            guardado = self._cache.get(sheet_name)
            if guardado and (time.time() - guardado[0]) < _CACHE_TTL:
                return guardado[1].copy()

        try:
            records = self._sheet(sheet_name).get_all_records()
            self.ultimo_error = None
        except Exception as e:
            # Se guarda el error en vez de silenciarlo: antes, una cuota
            # agotada de la API se veía en pantalla como "no hay datos".
            self.ultimo_error = f"{type(e).__name__}: {e}"
            return pd.DataFrame(columns=HEADERS[sheet_name])

        if records:
            df = pd.DataFrame(records)
            # Normalizar columnas al formato esperado (case-insensitive)
            expected = HEADERS.get(sheet_name, [])
            rename_map = {}
            for col in df.columns:
                for exp in expected:
                    if col.lower() == exp.lower() and col != exp:
                        rename_map[col] = exp
            if rename_map:
                df = df.rename(columns=rename_map)
            # Garantizar que existan todas las columnas esperadas (hojas antiguas)
            for exp in expected:
                if exp not in df.columns:
                    df[exp] = ""
        else:
            df = pd.DataFrame(columns=HEADERS[sheet_name])

        self._cache[sheet_name] = (time.time(), df)
        return df.copy()

    def append(self, sheet_name: str, row: list):
        """Agrega una fila al final de la hoja."""
        self._sheet(sheet_name).append_row(row, value_input_option="USER_ENTERED")
        self._invalidar_cache(sheet_name)

    def update_cell(self, sheet_name: str, row: int, col: int, value):
        """Actualiza una celda (row/col base 1, fila 1 = encabezado)."""
        self._sheet(sheet_name).update_cell(row, col, value)
        self._invalidar_cache(sheet_name)

    def update_row(self, sheet_name: str, row_idx: int, data: list):
        """Actualiza la fila completa (row_idx base 1, fila 1 = encabezado)."""
        ncols = len(data)
        rango = f"A{row_idx}:{rowcol_to_a1(row_idx, ncols)}"
        self._sheet(sheet_name).update(range_name=rango, values=[data],
                                       value_input_option="USER_ENTERED")
        self._invalidar_cache(sheet_name)

    def update_campos(self, sheet_name: str, row_idx: int, campos: dict):
        """Actualiza varias columnas por nombre en una sola llamada a la API.
        campos: {nombre_columna: valor}"""
        headers = HEADERS.get(sheet_name, [])
        peticiones = []
        for nombre, valor in campos.items():
            if nombre not in headers:
                continue
            col_i = headers.index(nombre) + 1
            peticiones.append({
                "range": rowcol_to_a1(row_idx, col_i),
                "values": [[str(valor)]],
            })
        if peticiones:
            self._sheet(sheet_name).batch_update(peticiones, value_input_option="USER_ENTERED")
            self._invalidar_cache(sheet_name)

    def _fila_de(self, sheet_name: str, id_col: str, id_val: str) -> int:
        """Devuelve el índice base-1 de la fila cuyo id_col == id_val."""
        df = self.get_df(sheet_name)
        if df.empty or id_col not in df.columns:
            raise ValueError(f"Registro {id_val} no encontrado en {sheet_name}")
        ids = df[id_col].astype(str).str.strip().tolist()
        objetivo = str(id_val).strip()
        if objetivo not in ids:
            raise ValueError(f"Registro {id_val} no encontrado en {sheet_name}")
        return ids.index(objetivo) + 2  # +1 encabezado, +1 base-1

    def ensure_columns(self, sheet_name: str):
        """Agrega al final las columnas de HEADERS que falten en la hoja.
        Permite migrar hojas creadas con versiones anteriores sin perder datos."""
        esperadas = HEADERS.get(sheet_name, [])
        if not esperadas:
            return
        try:
            sheet = self._sheet(sheet_name)
        except Exception:
            return
        try:
            actuales = [str(c).strip() for c in sheet.row_values(1)]
        except Exception:
            actuales = []
        if not actuales:
            sheet.update(range_name=f"A1:{rowcol_to_a1(1, len(esperadas))}",
                         values=[esperadas], value_input_option="USER_ENTERED")
            return
        faltantes = [c for c in esperadas if c not in actuales]
        if not faltantes:
            return
        nuevos = actuales + faltantes
        if sheet.col_count < len(nuevos):
            sheet.add_cols(len(nuevos) - sheet.col_count)
        sheet.update(range_name=f"A1:{rowcol_to_a1(1, len(nuevos))}",
                     values=[nuevos], value_input_option="USER_ENTERED")

    def migrar_esquema(self):
        """Asegura que Permisos y Vacaciones tengan las columnas del flujo de
        doble aprobación. Es idempotente: si ya están, no hace nada."""
        for hoja in ("Permisos", "Vacaciones"):
            try:
                self.ensure_columns(hoja)
            except Exception:
                pass

    # ── Configuración ────────────────────────────────────────────────────────

    def get_config(self) -> dict:
        df = self.get_df("Configuracion")
        if df.empty or "Key" not in df.columns:
            return CONFIG_DEFAULTS.copy()
        cfg = {}
        for _, row in df.iterrows():
            k = str(row["Key"]).strip()
            v = str(row["Valor"]).strip() if row["Valor"] is not None else ""
            # Ignorar vacíos, "nan" o "None" para que manden los valores por defecto
            if k and k not in ("nan", "None") and v and v not in ("nan", "None"):
                cfg[k] = v
        return {**CONFIG_DEFAULTS, **cfg}

    def save_all_config(self, config: dict):
        """Guarda toda la configuración de una vez: limpia la hoja y escribe
        la fila de encabezado + todas las claves/valores.  Más robusto que
        llamar a set_config N veces porque garantiza que A1 siempre sea el
        encabezado correcto y no quedan filas huérfanas."""
        sheet = self._sheet("Configuracion")
        rows = [["Key", "Valor"]] + [[k, str(v)] for k, v in config.items()]
        sheet.clear()
        self._invalidar_cache("Configuracion")
        result = sheet.update(range_name=f"A1:B{len(rows)}", values=rows,
                              value_input_option="USER_ENTERED")
        updated = (result or {}).get("updatedRows", 0)
        if updated and updated < len(rows):
            raise RuntimeError(
                f"Escritura incompleta: se esperaban {len(rows)} filas, "
                f"Google Sheets reportó {updated}."
            )

    def set_config(self, key: str, valor: str):
        """Guarda o actualiza una sola clave de configuración.
        Garantiza que la hoja tenga encabezado Key/Valor en la fila 1."""
        sheet = self._sheet("Configuracion")
        all_values = sheet.get_all_values()
        if not all_values:
            sheet.update(range_name="A1:B1", values=[["Key", "Valor"]],
                         value_input_option="USER_ENTERED")
        elif [str(v).strip() for v in all_values[0]] != ["Key", "Valor"]:
            sheet.insert_row(["Key", "Valor"], 1, value_input_option="USER_ENTERED")

        df = self.get_df("Configuracion")
        if not df.empty and "Key" in df.columns:
            keys = df["Key"].astype(str).tolist()
        else:
            keys = []
        if key in keys:
            row_idx = keys.index(key) + 2
            self.update_row("Configuracion", row_idx, [key, valor])
        else:
            self.append("Configuracion", [key, valor])

    # ── Empleados ────────────────────────────────────────────────────────────

    def get_empleados(self) -> pd.DataFrame:
        return self.get_df("Empleados")

    def next_id_empleado(self) -> str:
        df = self.get_empleados()
        if df.empty or "ID_Empleado" not in df.columns:
            return "EMP001"
        ids = df["ID_Empleado"].astype(str).str.strip()
        ids = ids[ids != ""]
        if ids.empty:
            return "EMP001"
        nums = ids.str.extract(r"(\d+)$")[0].dropna().astype(int)
        next_n = int(nums.max()) + 1 if not nums.empty else 1
        if ids.str.match(r"^\d+$").all():
            return str(next_n)
        prefix_s = ids.str.extract(r"^([A-Za-z_]+)(\d+)$")
        if prefix_s is not None and not prefix_s.dropna().empty:
            prefix = prefix_s[0].dropna().iloc[0]
            digits = len(prefix_s[1].dropna().iloc[0])
            return f"{prefix}{next_n:0{digits}d}"
        return f"EMP{next_n:03d}"

    def agregar_empleado(self, nombre, email, area, email_jefe, hora_inicio, hora_fin) -> str:
        emp_id = self.next_id_empleado()
        self.append("Empleados", [emp_id, nombre, email, area, email_jefe, hora_inicio, hora_fin])
        return emp_id

    def actualizar_empleado(self, emp_id: str, nombre, email, area, email_jefe, hora_inicio, hora_fin):
        row_idx = self._fila_de("Empleados", "ID_Empleado", emp_id)
        self.update_row("Empleados", row_idx, [emp_id, nombre, email, area, email_jefe, hora_inicio, hora_fin])

    # ── Jerarquía: jefe inmediato y subordinados ─────────────────────────────

    def get_empleado(self, id_empleado: str) -> dict | None:
        """Devuelve la fila del empleado como dict, o None."""
        df = self.get_empleados()
        if df.empty or "ID_Empleado" not in df.columns:
            return None
        mask = df["ID_Empleado"].astype(str).str.strip() == str(id_empleado).strip()
        rows = df[mask]
        return rows.iloc[0].to_dict() if not rows.empty else None

    def get_empleado_por_email(self, email: str) -> dict | None:
        """Busca un empleado por su correo (case-insensitive)."""
        df = self.get_empleados()
        if df.empty or "Email" not in df.columns or not str(email).strip():
            return None
        mask = df["Email"].astype(str).str.strip().str.lower() == str(email).strip().lower()
        rows = df[mask]
        return rows.iloc[0].to_dict() if not rows.empty else None

    def get_email_empleado(self, id_empleado: str) -> str:
        """Devuelve el email del empleado o cadena vacía si no se encuentra."""
        emp = self.get_empleado(id_empleado)
        return str(emp.get("Email", "")).strip() if emp else ""

    def get_email_jefe(self, id_empleado: str) -> str:
        """Devuelve el correo del jefe inmediato del empleado, o cadena vacía."""
        emp = self.get_empleado(id_empleado)
        if not emp:
            return ""
        jefe = str(emp.get("Email_Jefe", "")).strip()
        return "" if jefe.lower() in ("", "nan", "none") else jefe

    def get_nombre_empleado(self, id_empleado: str) -> str:
        emp = self.get_empleado(id_empleado)
        return str(emp.get("Nombre", id_empleado)) if emp else str(id_empleado)

    def get_subordinados(self, email_jefe: str) -> pd.DataFrame:
        """Empleados cuyo Email_Jefe coincide con el correo dado."""
        df = self.get_empleados()
        if df.empty or "Email_Jefe" not in df.columns or not str(email_jefe).strip():
            return pd.DataFrame(columns=HEADERS["Empleados"])
        mask = df["Email_Jefe"].astype(str).str.strip().str.lower() == str(email_jefe).strip().lower()
        return df[mask]

    def es_jefe(self, email: str) -> bool:
        """True si alguien reporta a este correo."""
        return not self.get_subordinados(email).empty

    def ids_subordinados(self, email_jefe: str) -> list:
        df = self.get_subordinados(email_jefe)
        if df.empty:
            return []
        return df["ID_Empleado"].astype(str).str.strip().tolist()

    # ── Asistencia ───────────────────────────────────────────────────────────

    def get_asistencia(self, fecha: str = None) -> pd.DataFrame:
        df = self.get_df("Asistencia")
        if fecha and not df.empty and "Fecha" in df.columns:
            df = df[df["Fecha"].astype(str) == fecha]
        return df

    def ya_registro_entrada(self, id_empleado: str, fecha: str) -> bool:
        df = self.get_asistencia(fecha)
        if df.empty or "ID_Empleado" not in df.columns:
            return False
        return str(id_empleado) in df["ID_Empleado"].astype(str).values

    def registrar_entrada(self, id_empleado: str, nombre: str, hora_entrada: str, config: dict) -> tuple:
        """Registra la hora de entrada. Devuelve (estado, minutos_atraso)."""
        fecha = hoy_local(config).strftime("%Y-%m-%d")
        horario_inicio = config.get("Horario_Inicio", "09:00")
        tolerancia = int(float(config.get("Tolerancia_Minutos", 0)))

        fmt = "%H:%M"
        entrada_dt = datetime.strptime(hora_entrada[:5], fmt)
        inicio_dt  = datetime.strptime(horario_inicio[:5], fmt)
        diff = (entrada_dt - inicio_dt).total_seconds() / 60
        minutos_atraso = max(0, diff - tolerancia)
        estado = "Tardanza" if minutos_atraso > 0 else "A_Tiempo"

        self.append("Asistencia", [fecha, id_empleado, nombre, hora_entrada, "", estado, int(minutos_atraso), ""])
        return estado, int(minutos_atraso)

    def registrar_salida(self, id_empleado: str, fecha: str, hora_salida: str, observaciones: str = ""):
        df = self.get_df("Asistencia")
        if df.empty:
            raise ValueError("No hay registros de asistencia")
        mask = (df["Fecha"].astype(str) == fecha) & (df["ID_Empleado"].astype(str) == str(id_empleado))
        rows = df[mask]
        if rows.empty:
            raise ValueError(f"No se encontró entrada para {id_empleado} el {fecha}")
        row_idx = rows.index[0] + 2
        self.update_cell("Asistencia", row_idx, 5, hora_salida)
        if observaciones:
            self.update_cell("Asistencia", row_idx, 8, observaciones)

    def marcar_ausencia(self, id_empleado: str, nombre: str, fecha: str, motivo: str = ""):
        self.append("Asistencia", [fecha, id_empleado, nombre, "", "", "Ausente", 0, motivo])

    # ── Permisos ─────────────────────────────────────────────────────────────

    def get_permisos(self) -> pd.DataFrame:
        return self.get_df("Permisos")

    def horas_permiso_usadas_mes(self, id_empleado: str, año_mes: str) -> float:
        """Horas de permiso ya comprometidas en el mes (aprobadas o en trámite)."""
        df = self.get_permisos()
        if df.empty or "ID_Empleado" not in df.columns:
            return 0.0
        mask = (df["ID_Empleado"].astype(str) == str(id_empleado)) & \
               (df["Fecha"].astype(str).str.startswith(año_mes)) & \
               (df["Estado"].astype(str).str.lower().isin(ESTADOS_VIGENTES))
        total = pd.to_numeric(df[mask]["Horas_Solicitadas"], errors="coerce").fillna(0).sum()
        return float(total)

    def _next_id(self, sheet_name: str, id_col: str, prefijo: str, ancho: int = 4) -> str:
        df = self.get_df(sheet_name)
        if df.empty or id_col not in df.columns:
            return f"{prefijo}{1:0{ancho}d}"
        nums = pd.to_numeric(df[id_col].astype(str).str.extract(r"(\d+)$")[0], errors="coerce").dropna()
        next_n = int(nums.max()) + 1 if not nums.empty else 1
        return f"{prefijo}{next_n:0{ancho}d}"

    def solicitar_permiso(self, id_empleado: str, fecha: str, horas: float,
                          motivo: str, config: dict) -> dict:
        """Crea la solicitud de permiso. Siempre requiere aprobación del jefe
        inmediato (si está asignado) y luego de RRHH.

        Devuelve {"id", "estado", "excede_cupo", "email_jefe", "horas_usadas"}.
        """
        año_mes = fecha[:7]
        usadas = self.horas_permiso_usadas_mes(id_empleado, año_mes)
        limite = float(config.get("Horas_Permiso_Mensual", 3))
        excede = (usadas + horas) > limite

        email_jefe = self.get_email_jefe(id_empleado)
        estado = EST_PEND_JEFE if email_jefe else EST_PEND_RRHH

        perm_id = self._next_id("Permisos", "ID_Permiso", "PERM")
        fila = [perm_id, fecha, id_empleado, horas, motivo, estado,
                usadas + horas, "", "", "", "", "", "", ""]
        self.append("Permisos", fila)
        return {
            "id": perm_id,
            "estado": estado,
            "excede_cupo": excede,
            "email_jefe": email_jefe,
            "horas_usadas": usadas + horas,
            "limite": limite,
        }

    def aprobar_permiso_jefe(self, perm_id: str, aprobador: str):
        """Aprobación del jefe inmediato. Pasa la solicitud a RRHH."""
        row = self._fila_de("Permisos", "ID_Permiso", perm_id)
        self.update_campos("Permisos", row, {
            "Estado":           EST_PEND_RRHH,
            "Aprobado_Jefe":    aprobador,
            "Fecha_Aprob_Jefe": ahora_local().strftime("%Y-%m-%d %H:%M"),
        })

    def aprobar_permiso_rrhh(self, perm_id: str, aprobador: str):
        """Aprobación final de RRHH."""
        row = self._fila_de("Permisos", "ID_Permiso", perm_id)
        self.update_campos("Permisos", row, {
            "Estado":           EST_APROBADO,
            "Aprobado_RRHH":    aprobador,
            "Fecha_Aprob_RRHH": ahora_local().strftime("%Y-%m-%d %H:%M"),
            "Aprobado_Por":     aprobador,
        })

    def rechazar_permiso(self, perm_id: str, rechazado_por: str, motivo: str):
        row = self._fila_de("Permisos", "ID_Permiso", perm_id)
        self.update_campos("Permisos", row, {
            "Estado":         EST_RECHAZADO,
            "Motivo_Rechazo": motivo,
            "Rechazado_Por":  rechazado_por,
        })

    # Compatibilidad con la versión anterior
    def aprobar_permiso(self, perm_id: str, aprobado_por: str):
        self.aprobar_permiso_rrhh(perm_id, aprobado_por)

    def permisos_pendientes_jefe(self, email_jefe: str) -> pd.DataFrame:
        """Permisos esperando la firma de este jefe."""
        ids = self.ids_subordinados(email_jefe)
        df = self.get_permisos()
        if df.empty or not ids:
            return pd.DataFrame(columns=HEADERS["Permisos"])
        mask = df["ID_Empleado"].astype(str).str.strip().isin(ids) & \
               (df["Estado"].astype(str) == EST_PEND_JEFE)
        return df[mask]

    def permisos_pendientes_rrhh(self) -> pd.DataFrame:
        df = self.get_permisos()
        if df.empty:
            return pd.DataFrame(columns=HEADERS["Permisos"])
        # Incluye el estado antiguo para no perder solicitudes previas
        mask = df["Estado"].astype(str).isin([EST_PEND_RRHH, "Pendiente_Aprobacion"])
        return df[mask]

    # ── Vacaciones ───────────────────────────────────────────────────────────

    def get_vacaciones(self) -> pd.DataFrame:
        return self.get_df("Vacaciones")

    def dias_habiles(self, fecha_ini: str, fecha_fin: str) -> int:
        d0 = datetime.strptime(fecha_ini, "%Y-%m-%d")
        d1 = datetime.strptime(fecha_fin, "%Y-%m-%d")
        dias = 0
        current = d0
        while current <= d1:
            if current.weekday() < 5:  # Lunes-Viernes
                dias += 1
            current += timedelta(days=1)
        return dias

    def solicitar_vacaciones(self, id_empleado: str, fecha_ini: str, fecha_fin: str) -> dict:
        """Crea la solicitud de vacaciones. Requiere aprobación del jefe
        inmediato (si está asignado) y luego de RRHH.

        Devuelve {"id", "estado", "dias", "email_jefe"}.
        """
        dias = self.dias_habiles(fecha_ini, fecha_fin)
        email_jefe = self.get_email_jefe(id_empleado)
        estado = EST_PEND_JEFE if email_jefe else EST_PEND_RRHH

        vac_id = self._next_id("Vacaciones", "ID_Vacacion", "VAC")
        fila = [vac_id, id_empleado, fecha_ini, fecha_fin, dias, estado,
                "", "", "", "", "", "", ""]
        self.append("Vacaciones", fila)
        return {"id": vac_id, "estado": estado, "dias": dias, "email_jefe": email_jefe}

    def aprobar_vacaciones_jefe(self, vac_id: str, aprobador: str):
        row = self._fila_de("Vacaciones", "ID_Vacacion", vac_id)
        self.update_campos("Vacaciones", row, {
            "Estado":           EST_PEND_RRHH,
            "Aprobado_Jefe":    aprobador,
            "Fecha_Aprob_Jefe": ahora_local().strftime("%Y-%m-%d %H:%M"),
        })

    def aprobar_vacaciones_rrhh(self, vac_id: str, aprobador: str):
        row = self._fila_de("Vacaciones", "ID_Vacacion", vac_id)
        self.update_campos("Vacaciones", row, {
            "Estado":           EST_APROBADO,
            "Aprobado_RRHH":    aprobador,
            "Fecha_Aprob_RRHH": ahora_local().strftime("%Y-%m-%d %H:%M"),
            "Aprobado_Por":     aprobador,
        })

    def rechazar_vacaciones(self, vac_id: str, rechazado_por: str, motivo: str):
        row = self._fila_de("Vacaciones", "ID_Vacacion", vac_id)
        self.update_campos("Vacaciones", row, {
            "Estado":         EST_RECHAZADO,
            "Motivo_Rechazo": motivo,
            "Rechazado_Por":  rechazado_por,
        })

    # Compatibilidad con la versión anterior
    def aprobar_vacaciones(self, vac_id: str, aprobado_por: str):
        self.aprobar_vacaciones_rrhh(vac_id, aprobado_por)

    def vacaciones_pendientes_jefe(self, email_jefe: str) -> pd.DataFrame:
        ids = self.ids_subordinados(email_jefe)
        df = self.get_vacaciones()
        if df.empty or not ids:
            return pd.DataFrame(columns=HEADERS["Vacaciones"])
        mask = df["ID_Empleado"].astype(str).str.strip().isin(ids) & \
               (df["Estado"].astype(str) == EST_PEND_JEFE)
        return df[mask]

    def vacaciones_pendientes_rrhh(self) -> pd.DataFrame:
        df = self.get_vacaciones()
        if df.empty:
            return pd.DataFrame(columns=HEADERS["Vacaciones"])
        mask = df["Estado"].astype(str).isin([EST_PEND_RRHH, "Pendiente"])
        return df[mask]

    # ── Horas Extras ─────────────────────────────────────────────────────────

    def get_horas_extras(self) -> pd.DataFrame:
        return self.get_df("Horas_Extras")

    def registrar_horas_extra(self, id_empleado: str, fecha: str, horas: float, motivo: str) -> str:
        hex_id = self._next_id("Horas_Extras", "ID", "HE")
        self.append("Horas_Extras", [hex_id, fecha, id_empleado, horas, motivo, "", "Pendiente"])
        return hex_id

    def aprobar_hora_extra(self, hex_id: str, aprobado_por: str):
        row_idx = self._fila_de("Horas_Extras", "ID", hex_id)
        self.update_cell("Horas_Extras", row_idx, 6, aprobado_por)
        self.update_cell("Horas_Extras", row_idx, 7, "Aprobado")

    # ── Usuarios / Autenticación ──────────────────────────────────────────────

    def ensure_usuarios_sheet(self):
        """Crea la hoja Usuarios si no existe.

        Nota de seguridad: ya NO se crea un usuario admin con contraseña fija.
        El admin inicial se crea desde la pantalla de primer arranque, donde la
        persona define su propia contraseña.
        """
        try:
            self._sheet("Usuarios")
        except Exception:
            ws = self.spreadsheet.add_worksheet(title="Usuarios", rows=200, cols=3)
            ws.update(range_name="A1:C1", values=[["ID_Empleado", "Password_Hash", "Rol"]],
                      value_input_option="USER_ENTERED")

    def hay_admin(self) -> bool:
        """True si ya existe al menos un usuario con rol admin y contraseña."""
        df = self.get_usuarios()
        if df.empty or "Rol" not in df.columns:
            return False
        mask = (df["Rol"].astype(str).str.strip().str.lower() == "admin") & \
               (df["Password_Hash"].astype(str).str.strip() != "")
        return bool(mask.any())

    def get_usuarios(self) -> pd.DataFrame:
        return self.get_df("Usuarios")

    def verificar_credenciales(self, id_empleado: str, password: str):
        """Verifica ID + contraseña. Devuelve dict {id_empleado, rol} o None.
        Migra automáticamente los hashes antiguos a PBKDF2 tras un login válido."""
        df = self.get_usuarios()
        if df.empty or "ID_Empleado" not in df.columns:
            return None
        mask = df["ID_Empleado"].astype(str).str.strip() == str(id_empleado).strip()
        rows = df[mask]
        if rows.empty:
            return None
        row = rows.iloc[0]
        almacenado = str(row["Password_Hash"]).strip()
        if not verify_password(password, almacenado):
            return None
        # Migración transparente del hash heredado
        if es_hash_heredado(almacenado):
            try:
                row_idx = self._fila_de("Usuarios", "ID_Empleado", id_empleado)
                self.update_cell("Usuarios", row_idx, 2, hash_password(password))
            except Exception:
                pass
        return {
            "id_empleado": str(id_empleado).strip(),
            "rol": str(row.get("Rol", "empleado")).strip() or "empleado",
        }

    def crear_usuario(self, id_empleado: str, password: str, rol: str = "empleado"):
        pwd_hash = hash_password(password)
        df = self.get_usuarios()
        ids = df["ID_Empleado"].astype(str).str.strip().tolist() if not df.empty else []
        if str(id_empleado).strip() in ids:
            row_idx = ids.index(str(id_empleado).strip()) + 2
            self.update_row("Usuarios", row_idx, [id_empleado, pwd_hash, rol])
        else:
            self.append("Usuarios", [id_empleado, pwd_hash, rol])

    def cambiar_password(self, id_empleado: str, nueva_password: str):
        row_idx = self._fila_de("Usuarios", "ID_Empleado", id_empleado)
        self.update_cell("Usuarios", row_idx, 2, hash_password(nueva_password))

    # ── Llamados de Atención ─────────────────────────────────────────────────

    def ensure_llamados_sheet(self):
        """Crea la hoja Llamados_Atencion si no existe."""
        try:
            self._sheet("Llamados_Atencion")
        except Exception:
            ws = self.spreadsheet.add_worksheet(title="Llamados_Atencion", rows=500, cols=9)
            ws.update(range_name="A1:I1", values=[HEADERS["Llamados_Atencion"]],
                      value_input_option="USER_ENTERED")

    def get_llamados_atencion(self) -> pd.DataFrame:
        return self.get_df("Llamados_Atencion")

    def get_tardanzas_mes(self, id_empleado: str, año_mes: str) -> int:
        df = self.get_df("Asistencia")
        if df.empty or "ID_Empleado" not in df.columns:
            return 0
        mask = (df["ID_Empleado"].astype(str) == str(id_empleado)) & \
               (df["Fecha"].astype(str).str.startswith(año_mes)) & \
               (df["Estado"].astype(str) == "Tardanza")
        return int(len(df[mask]))

    def registrar_llamado_atencion(self, id_empleado: str, nombre: str, tipo: str,
                                    motivo: str, atrasos: int, registrado_por: str) -> str:
        año = hoy_local().year
        df = self.get_llamados_atencion()
        if not df.empty and "ID_Llamado" in df.columns:
            este_año = df[df["ID_Llamado"].astype(str).str.startswith(f"LA-{año}-")]
            nums = pd.to_numeric(
                este_año["ID_Llamado"].astype(str).str.extract(r"-(\d+)$")[0],
                errors="coerce"
            ).dropna()
            next_n = int(nums.max()) + 1 if not nums.empty else 1
        else:
            next_n = 1
        llamado_id = f"LA-{año}-{next_n:04d}"
        fecha = hoy_local().strftime("%Y-%m-%d")
        self.append("Llamados_Atencion",
                    [llamado_id, fecha, id_empleado, nombre, tipo, motivo, atrasos, registrado_por, "Activo"])
        return llamado_id
