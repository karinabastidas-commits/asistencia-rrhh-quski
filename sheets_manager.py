"""
sheets_manager.py
Módulo de acceso a Google Sheets para Sistema de Asistencia RRHH - Quski
"""

import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from datetime import datetime, date, timedelta
import streamlit as st

# ── Constantes ────────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1gaPrP95SF0xat7xRs94CMyH22LeIoolEom1OjCDAA2I"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = {
    "Empleados":    ["ID_Empleado", "Nombre", "Email", "Area", "Email_Jefe", "Horario_Inicio", "Horario_Fin"],
    "Asistencia":   ["Fecha", "ID_Empleado", "Nombre", "Hora_Entrada", "Hora_Salida", "Estado", "Minutos_Atraso", "Observaciones"],
    "Permisos":     ["ID_Permiso", "Fecha", "ID_Empleado", "Horas_Solicitadas", "Motivo", "Estado", "Horas_Usadas_Mes", "Aprobado_Por"],
    "Vacaciones":   ["ID_Vacacion", "ID_Empleado", "Fecha_Inicio", "Fecha_Fin", "Dias_Habiles", "Estado", "Aprobado_Por"],
    "Horas_Extras": ["ID", "Fecha", "ID_Empleado", "Horas_Extra", "Motivo", "Aprobado_Por", "Estado"],
    "Configuracion":["Key", "Valor"],
    "Usuarios":          ["ID_Empleado", "Password_Hash", "Rol"],
    "Llamados_Atencion": ["ID_Llamado", "Fecha", "ID_Empleado", "Nombre", "Tipo", "Motivo", "Atrasos_Acumulados", "Registrado_Por", "Estado"],
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

    # ── Helpers de bajo nivel ────────────────────────────────────────────────

    def _sheet(self, name: str):
        return self.spreadsheet.worksheet(name)

    def get_df(self, sheet_name: str) -> pd.DataFrame:
        """Lee la hoja y devuelve DataFrame. Normaliza nombres de columnas."""
        try:
            records = self._sheet(sheet_name).get_all_records()
        except Exception:
            records = []
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
            return df
        return pd.DataFrame(columns=HEADERS[sheet_name])

    def append(self, sheet_name: str, row: list):
        """Agrega una fila al final de la hoja."""
        self._sheet(sheet_name).append_row(row, value_input_option="USER_ENTERED")

    def update_cell(self, sheet_name: str, row: int, col: int, value):
        """Actualiza una celda (row/col base 1, fila 1 = encabezado)."""
        self._sheet(sheet_name).update_cell(row, col, value)

    def update_row(self, sheet_name: str, row_idx: int, data: list):
        """Actualiza la fila completa (row_idx base 1, fila 1 = encabezado)."""
        ncols = len(data)
        col_letter = chr(ord("A") + ncols - 1)
        self._sheet(sheet_name).update(f"A{row_idx}:{col_letter}{row_idx}", [data],
                                        value_input_option="USER_ENTERED")

    # ── Configuración ────────────────────────────────────────────────────────

    def get_config(self) -> dict:
        df = self.get_df("Configuracion")
        if df.empty or "Key" not in df.columns:
            return CONFIG_DEFAULTS.copy()
        cfg = {}
        for _, row in df.iterrows():
            k = str(row["Key"]).strip()
            v = str(row["Valor"]).strip() if row["Valor"] is not None else ""
            # Skip blank, "nan", or "None" values so CONFIG_DEFAULTS take precedence
            if k and k not in ("nan", "None") and v and v not in ("nan", "None"):
                cfg[k] = v
        return {**CONFIG_DEFAULTS, **cfg}

    def set_config(self, key: str, valor: str):
        df = self.get_df("Configuracion")
        if not df.empty and "Key" in df.columns:
            keys = df["Key"].astype(str).tolist()
        else:
            keys = []
        if key in keys:
            row_idx = keys.index(key) + 2  # +1 header, +1 base-1
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
        nums = ids.str.extract(r"(\d+)$")[0].dropna().astype(int)
        next_n = int(nums.max()) + 1 if not nums.empty else 1
        # Si todos los IDs son puramente numéricos, continuar con números
        if ids.str.match(r"^\d+$").all():
            return str(next_n)
        # Si tienen prefijo (ej: EMP001), detectarlo y continuar
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
        df = self.get_empleados()
        ids = df["ID_Empleado"].astype(str).tolist()
        if emp_id not in ids:
            raise ValueError(f"Empleado {emp_id} no encontrado")
        row_idx = ids.index(emp_id) + 2
        self.update_row("Empleados", row_idx, [emp_id, nombre, email, area, email_jefe, hora_inicio, hora_fin])

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
        fecha = date.today().strftime("%Y-%m-%d")
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
        # Actualizar Hora_Salida (col 5) y Observaciones (col 8)
        self.update_cell("Asistencia", row_idx, 5, hora_salida)
        if observaciones:
            self.update_cell("Asistencia", row_idx, 8, observaciones)

    def marcar_ausencia(self, id_empleado: str, nombre: str, fecha: str, motivo: str = ""):
        self.append("Asistencia", [fecha, id_empleado, nombre, "", "", "Ausente", 0, motivo])

    # ── Permisos ─────────────────────────────────────────────────────────────

    def get_permisos(self) -> pd.DataFrame:
        return self.get_df("Permisos")

    def horas_permiso_usadas_mes(self, id_empleado: str, año_mes: str) -> float:
        """año_mes formato YYYY-MM"""
        df = self.get_permisos()
        if df.empty or "ID_Empleado" not in df.columns:
            return 0.0
        mask = (df["ID_Empleado"].astype(str) == str(id_empleado)) & \
               (df["Fecha"].astype(str).str.startswith(año_mes)) & \
               (df["Estado"].astype(str).str.lower().isin(["aprobado", "pendiente"]))
        total = pd.to_numeric(df[mask]["Horas_Solicitadas"], errors="coerce").fillna(0).sum()
        return float(total)

    def solicitar_permiso(self, id_empleado: str, fecha: str, horas: float, motivo: str, config: dict) -> str:
        año_mes = fecha[:7]
        usadas = self.horas_permiso_usadas_mes(id_empleado, año_mes)
        limite = float(config.get("Horas_Permiso_Mensual", 3))
        estado = "Aprobado" if (usadas + horas) <= limite else "Pendiente_Aprobacion"

        df = self.get_permisos()
        nums = pd.to_numeric(df["ID_Permiso"].astype(str).str.extract(r"(\d+)$")[0], errors="coerce").dropna()
        next_n = int(nums.max()) + 1 if not nums.empty else 1
        perm_id = f"PERM{next_n:04d}"
        horas_usadas_mes = usadas + horas if estado == "Aprobado" else usadas

        self.append("Permisos", [perm_id, fecha, id_empleado, horas, motivo, estado, horas_usadas_mes, ""])
        return estado

    def aprobar_permiso(self, perm_id: str, aprobado_por: str):
        df = self.get_permisos()
        ids = df["ID_Permiso"].astype(str).tolist()
        if perm_id not in ids:
            raise ValueError(f"Permiso {perm_id} no encontrado")
        row_idx = ids.index(perm_id) + 2
        self.update_cell("Permisos", row_idx, 6, "Aprobado")
        self.update_cell("Permisos", row_idx, 8, aprobado_por)

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

    def solicitar_vacaciones(self, id_empleado: str, fecha_ini: str, fecha_fin: str) -> str:
        dias = self.dias_habiles(fecha_ini, fecha_fin)
        df = self.get_vacaciones()
        nums = pd.to_numeric(df["ID_Vacacion"].astype(str).str.extract(r"(\d+)$")[0], errors="coerce").dropna()
        next_n = int(nums.max()) + 1 if not nums.empty else 1
        vac_id = f"VAC{next_n:04d}"
        self.append("Vacaciones", [vac_id, id_empleado, fecha_ini, fecha_fin, dias, "Pendiente", ""])
        return vac_id

    def aprobar_vacaciones(self, vac_id: str, aprobado_por: str):
        df = self.get_vacaciones()
        ids = df["ID_Vacacion"].astype(str).tolist()
        if vac_id not in ids:
            raise ValueError(f"Vacación {vac_id} no encontrada")
        row_idx = ids.index(vac_id) + 2
        self.update_cell("Vacaciones", row_idx, 6, "Aprobado")
        self.update_cell("Vacaciones", row_idx, 7, aprobado_por)

    # ── Horas Extras ─────────────────────────────────────────────────────────

    def get_horas_extras(self) -> pd.DataFrame:
        return self.get_df("Horas_Extras")

    def registrar_horas_extra(self, id_empleado: str, fecha: str, horas: float, motivo: str):
        df = self.get_horas_extras()
        nums = pd.to_numeric(df["ID"].astype(str).str.extract(r"(\d+)$")[0], errors="coerce").dropna()
        next_n = int(nums.max()) + 1 if not nums.empty else 1
        hex_id = f"HE{next_n:04d}"
        self.append("Horas_Extras", [hex_id, fecha, id_empleado, horas, motivo, "", "Pendiente"])

    # ── Usuarios / Autenticación ──────────────────────────────────────────────

    def ensure_usuarios_sheet(self):
        """Crea la hoja Usuarios si no existe y agrega usuario admin por defecto."""
        import hashlib
        try:
            self._sheet("Usuarios")
        except Exception:
            ws = self.spreadsheet.add_worksheet(title="Usuarios", rows=200, cols=3)
            ws.update("A1:C1", [["ID_Empleado", "Password_Hash", "Rol"]])
            # Crear admin por defecto: ID=admin, password=Quski2026
            pwd_hash = hashlib.sha256("Quski2026".encode()).hexdigest()
            ws.append_row(["admin", pwd_hash, "admin"], value_input_option="USER_ENTERED")

    def get_usuarios(self) -> pd.DataFrame:
        return self.get_df("Usuarios")

    def verificar_credenciales(self, id_empleado: str, password: str):
        """Verifica ID + contraseña. Devuelve dict {id_empleado, rol} o None."""
        import hashlib
        df = self.get_usuarios()
        if df.empty or "ID_Empleado" not in df.columns:
            return None
        mask = df["ID_Empleado"].astype(str).str.strip() == str(id_empleado).strip()
        rows = df[mask]
        if rows.empty:
            return None
        row = rows.iloc[0]
        pwd_hash = hashlib.sha256(password.encode()).hexdigest()
        if str(row["Password_Hash"]).strip() == pwd_hash:
            return {"id_empleado": str(id_empleado).strip(), "rol": str(row.get("Rol", "empleado")).strip()}
        return None

    def crear_usuario(self, id_empleado: str, password: str, rol: str = "empleado"):
        import hashlib
        pwd_hash = hashlib.sha256(password.encode()).hexdigest()
        df = self.get_usuarios()
        ids = df["ID_Empleado"].astype(str).str.strip().tolist() if not df.empty else []
        if str(id_empleado) in ids:
            row_idx = ids.index(str(id_empleado)) + 2
            self.update_row("Usuarios", row_idx, [id_empleado, pwd_hash, rol])
        else:
            self.append("Usuarios", [id_empleado, pwd_hash, rol])

    def cambiar_password(self, id_empleado: str, nueva_password: str):
        import hashlib
        pwd_hash = hashlib.sha256(nueva_password.encode()).hexdigest()
        df = self.get_usuarios()
        ids = df["ID_Empleado"].astype(str).str.strip().tolist()
        if str(id_empleado) not in ids:
            raise ValueError("Usuario no encontrado")
        row_idx = ids.index(str(id_empleado)) + 2
        self.update_cell("Usuarios", row_idx, 2, pwd_hash)

    # ── Llamados de Atención ─────────────────────────────────────────────────

    def ensure_llamados_sheet(self):
        """Crea la hoja Llamados_Atencion si no existe."""
        try:
            self._sheet("Llamados_Atencion")
        except Exception:
            ws = self.spreadsheet.add_worksheet(title="Llamados_Atencion", rows=500, cols=9)
            ws.update("A1:I1", [["ID_Llamado", "Fecha", "ID_Empleado", "Nombre", "Tipo",
                                  "Motivo", "Atrasos_Acumulados", "Registrado_Por", "Estado"]])

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
        año = date.today().year
        df = self.get_llamados_atencion()
        # Numeración por año: LA-2026-0001
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
        fecha = date.today().strftime("%Y-%m-%d")
        self.append("Llamados_Atencion",
                    [llamado_id, fecha, id_empleado, nombre, tipo, motivo, atrasos, registrado_por, "Activo"])
        return llamado_id

    # ── Utilidades de email ───────────────────────────────────────────────────

    def get_email_empleado(self, id_empleado: str) -> str:
        """Devuelve el email del empleado o cadena vacía si no se encuentra."""
        df = self.get_empleados()
        if df.empty or "ID_Empleado" not in df.columns:
            return ""
        mask = df["ID_Empleado"].astype(str) == str(id_empleado)
        rows = df[mask]
        if rows.empty:
            return ""
        return str(rows.iloc[0].get("Email", ""))

    def aprobar_hora_extra(self, hex_id: str, aprobado_por: str):
        df = self.get_horas_extras()
        ids = df["ID"].astype(str).tolist()
        if hex_id not in ids:
            raise ValueError(f"Hora extra {hex_id} no encontrada")
        row_idx = ids.index(hex_id) + 2
        self.update_cell("Horas_Extras", row_idx, 6, aprobado_por)
        self.update_cell("Horas_Extras", row_idx, 7, "Aprobado")
