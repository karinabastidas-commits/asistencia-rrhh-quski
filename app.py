"""
app.py
Sistema de Asistencia RRHH – Quski
Aplicativo web con Streamlit + Google Sheets API
"""

import streamlit as st
import pandas as pd
import json
from datetime import date, datetime, timedelta
import html as _html
from sheets_manager import (
    SheetsManager, CONFIG_DEFAULTS,
    EST_PEND_JEFE, EST_PEND_RRHH, EST_APROBADO, EST_RECHAZADO,
    password_debil,
)

# ── Seguridad: control de intentos de inicio de sesión ────────────────────────
MAX_INTENTOS_LOGIN = 5          # intentos fallidos antes de bloquear
BLOQUEO_SEGUNDOS   = 300        # 5 minutos de espera tras agotarlos

# ── Configuración de página ───────────────────────────────────────────────────
st.set_page_config(
    page_title="Asistencia RRHH – Quski",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS personalizado ─────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Paleta Quski */
:root {
    --quski-blue:   #1A3A5C;
    --quski-teal:   #00A99D;
    --quski-light:  #E8F4F8;
    --quski-gray:   #6B7280;
}
[data-testid="stSidebar"] { background: var(--quski-blue); }
[data-testid="stSidebar"] * { color: #fff !important; }
[data-testid="stSidebar"] .stSelectbox label { color: #fff !important; }

.metric-card {
    background: var(--quski-light);
    border-left: 4px solid var(--quski-teal);
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 8px;
}
.metric-card h2 { color: var(--quski-blue); margin: 0; font-size: 2rem; }
.metric-card p  { color: var(--quski-gray); margin: 0; font-size: 0.85rem; }

.status-badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.78rem;
    font-weight: 600;
}
.badge-ok       { background:#D1FAE5; color:#065F46; }
.badge-warning  { background:#FEF3C7; color:#92400E; }
.badge-danger   { background:#FEE2E2; color:#991B1B; }
.badge-info     { background:#DBEAFE; color:#1E40AF; }

h1, h2, h3 { color: var(--quski-blue); }
</style>
""", unsafe_allow_html=True)


# ── Email helper ──────────────────────────────────────────────────────────────
def enviar_notificacion_email(destinatario: str, asunto: str, cuerpo_html: str,
                              _return_error: bool = False):
    """Envía notificación por email via SMTP.
    Retorna True si éxito, False si falla.
    Si _return_error=True, retorna (bool, str) con el mensaje de error."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    import traceback

    def _fail(msg):
        return (False, msg) if _return_error else False

    def _ok():
        return (True, "") if _return_error else True

    if "email" not in st.secrets:
        return _fail("❌ No existe la sección [email] en los Secrets de Streamlit.")

    cfg = st.secrets["email"]
    smtp_server   = cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port_raw = cfg.get("smtp_port", "587")
    smtp_user     = cfg.get("smtp_user", "")
    smtp_password = cfg.get("smtp_password", "")

    try:
        smtp_port = int(smtp_port_raw)
    except (ValueError, TypeError):
        return _fail(f"❌ smtp_port inválido: '{smtp_port_raw}'. Debe ser un número (ej. 587).")

    if not smtp_user:
        return _fail("❌ smtp_user está vacío en los Secrets.")
    if not smtp_password:
        return _fail("❌ smtp_password está vacío en los Secrets.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Quski RRHH – {asunto}"
    msg["From"]    = smtp_user
    msg["To"]      = destinatario
    html = f"""<html><body style="font-family:Arial,sans-serif;padding:24px;background:#f4f6f8">
    <div style="max-width:520px;margin:auto;background:white;border-radius:8px;
                border-left:4px solid #00A99D;padding:28px">
      <h2 style="color:#1A3A5C;margin-top:0">{asunto}</h2>
      {cuerpo_html}
      <hr style="margin:24px 0;border:none;border-top:1px solid #eee">
      <p style="color:#999;font-size:12px">Sistema de Asistencia RRHH – Quski<br>
      Este es un mensaje automático, no responder a este correo.</p>
    </div></body></html>"""
    msg.attach(MIMEText(html, "html"))

    # Intento 1: STARTTLS en puerto 587 (o el configurado)
    err_587 = None
    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, destinatario, msg.as_string())
        return _ok()
    except smtplib.SMTPAuthenticationError as e:
        # Error de credenciales — no tiene sentido reintentar con otro puerto
        detail = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        return _fail(
            f"❌ Error de autenticación SMTP (código {e.smtp_code}): {detail}\n\n"
            "Solución: genera una Contraseña de Aplicación en https://myaccount.google.com/apppasswords "
            "(requiere verificación en 2 pasos activa) y pon esos 16 caracteres en smtp_password."
        )
    except Exception as e_first:
        err_587 = f"{type(e_first).__name__}: {e_first}"

    # Intento 2: SSL directo en puerto 465 (algunos proveedores lo requieren)
    import ssl as _ssl
    try:
        ctx = _ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_server, 465, context=ctx, timeout=15) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, destinatario, msg.as_string())
        return _ok()
    except smtplib.SMTPAuthenticationError as e:
        detail = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        return _fail(
            f"❌ Error de autenticación (intentos 587 y 465):\n"
            f"  Puerto 587: {err_587}\n"
            f"  Puerto 465 (SSL): {detail}\n\n"
            "Solución: genera una Contraseña de Aplicación en https://myaccount.google.com/apppasswords "
            "(requiere verificación en 2 pasos activa) y pon esos 16 caracteres en smtp_password."
        )
    except Exception as e_second:
        return _fail(
            f"❌ No se pudo conectar por SMTP (probé ambos puertos):\n"
            f"  Puerto {smtp_port} (STARTTLS): {err_587}\n"
            f"  Puerto 465 (SSL): {type(e_second).__name__}: {e_second}\n\n"
            "Posibles causas:\n"
            "1. Streamlit Cloud bloquea SMTP saliente (raro, pero ocurre).\n"
            "2. El smtp_server no es correcto para este dominio.\n"
            "3. Problemas de red transitoria — intenta de nuevo en unos minutos."
        )


# ── Gestión de sesión y credenciales ─────────────────────────────────────────
def init_session():
    if "sm" not in st.session_state:
        st.session_state.sm = None
    if "config" not in st.session_state:
        st.session_state.config = {}
    if "usuario" not in st.session_state:
        st.session_state.usuario = None
    # Auto-conectar desde Streamlit Secrets (cuando está en la nube)
    if st.session_state.sm is None:
        try:
            if "gcp_service_account" in st.secrets:
                creds_dict = dict(st.secrets["gcp_service_account"])
                sm = SheetsManager(creds_dict)
                sm.ensure_usuarios_sheet()
                sm.ensure_llamados_sheet()
                # Agrega a Permisos y Vacaciones las columnas del flujo de doble
                # aprobación si la hoja viene de una versión anterior. Es
                # idempotente y debe correr ANTES de escribir cualquier solicitud.
                sm.migrar_esquema()
                st.session_state.sm = sm
                st.session_state.config = sm.get_config()
        except Exception as e:
            # No se silencia del todo: se guarda para poder mostrarlo en la
            # pantalla de conexión en lugar de fallar sin explicación.
            st.session_state.error_conexion = f"{type(e).__name__}: {e}"


def get_sm() -> SheetsManager | None:
    return st.session_state.get("sm")


def get_usuario() -> dict | None:
    return st.session_state.get("usuario")


def es_admin() -> bool:
    u = get_usuario()
    return u is not None and u.get("rol") == "admin"


def email_usuario_actual() -> str:
    """Correo del usuario conectado, tomado de su ficha de empleado."""
    u = get_usuario()
    if not u:
        return ""
    return str(u.get("email", "")).strip()


def es_jefe() -> bool:
    """True si al menos un empleado tiene a este usuario como jefe inmediato."""
    u = get_usuario()
    if not u:
        return False
    if "es_jefe" not in u:
        sm = get_sm()
        correo = email_usuario_actual()
        u["es_jefe"] = bool(sm and correo and sm.es_jefe(correo))
    return bool(u["es_jefe"])


def esc(texto) -> str:
    """Escapa texto antes de incrustarlo en el HTML de un correo.

    Sin esto, un empleado podría escribir etiquetas HTML o enlaces en el campo
    'Motivo' y esos irían tal cual en el correo que recibe RRHH o el jefe.
    """
    return _html.escape(str(texto if texto is not None else ""), quote=True)


# ── Notificaciones ────────────────────────────────────────────────────────────
def tabla_html(filas: list) -> str:
    """Tabla HTML para el cuerpo de los correos. filas: [(etiqueta, valor), ...].
    Todos los valores pasan por esc(), así que es seguro incluir texto escrito
    por el empleado (motivos, observaciones)."""
    partes = ['<table style="border-collapse:collapse;width:100%;margin-top:12px">']
    for i, (etiqueta, valor) in enumerate(filas):
        borde = "" if i == len(filas) - 1 else "border-bottom:1px solid #eee"
        partes.append(
            f'<tr><td style="padding:8px 12px;{borde};width:38%"><strong>{esc(etiqueta)}:</strong></td>'
            f'<td style="padding:8px 12px;{borde}">{esc(valor)}</td></tr>'
        )
    partes.append("</table>")
    return "".join(partes)


def notificar(destinatarios, asunto: str, cuerpo_html: str) -> tuple:
    """Envía el mismo correo a varios destinatarios, sin repetir direcciones.
    Devuelve (enviados, fallidos)."""
    enviados, fallidos, vistos = [], [], set()
    for d in destinatarios:
        d = str(d or "").strip()
        if not d or d.lower() in vistos:
            continue
        vistos.add(d.lower())
        try:
            if enviar_notificacion_email(d, asunto, cuerpo_html):
                enviados.append(d)
            else:
                fallidos.append(d)
        except Exception:
            fallidos.append(d)
    return enviados, fallidos


def mostrar_envio(enviados, fallidos):
    """Muestra en pantalla a quién se notificó y a quién no."""
    if enviados:
        st.info("📧 Notificado a: " + ", ".join(enviados))
    if fallidos:
        st.warning("⚠️ No se pudo enviar el correo a: " + ", ".join(fallidos)
                   + " — avísales por otro medio.")


def etiqueta_estado(estado) -> str:
    """Estado legible para el empleado, indicando la etapa exacta del trámite."""
    e = str(estado).strip()
    mapa = {
        EST_PEND_JEFE:          ("⏳", "Esperando aprobación de tu jefe inmediato"),
        EST_PEND_RRHH:          ("👤", "Aprobado por tu jefe – en revisión de RRHH"),
        EST_APROBADO:           ("✅", "Aprobado"),
        EST_RECHAZADO:          ("❌", "Rechazado"),
        "Pendiente":            ("⏳", "En trámite"),
        "Pendiente_Aprobacion": ("⏳", "En trámite"),
    }
    ico, txt = mapa.get(e, ("•", e or "—"))
    return f"{ico} {txt}"


def etiqueta_estado_corta(estado) -> str:
    """Versión breve para las tablas de RRHH."""
    e = str(estado).strip()
    return {
        EST_PEND_JEFE: "⏳ Con el jefe",
        EST_PEND_RRHH: "👤 Con RRHH",
        EST_APROBADO:  "✅ Aprobado",
        EST_RECHAZADO: "❌ Rechazado",
    }.get(e, e or "—")


def conectar_sheets(creds_dict: dict):
    try:
        sm = SheetsManager(creds_dict)
        sm.ensure_usuarios_sheet()
        sm.ensure_llamados_sheet()
        sm.migrar_esquema()
        st.session_state.sm = sm
        st.session_state.config = sm.get_config()
        return True
    except Exception as e:
        # Se muestra el tipo de error, no la traza completa: esta pantalla es
        # pública y la traza revela rutas internas y correos de servicio.
        st.error(f"❌ Error al conectar: {type(e).__name__}: {e}")
        return False


# ── Pantalla de login / credenciales ─────────────────────────────────────────
def pantalla_login():
    col_center = st.columns([1, 2, 1])[1]
    with col_center:
        st.image("https://www.quski.ec/wp-content/uploads/2022/08/logo-quski.png",
                 width=180, use_container_width=False)
    st.markdown("<h2 style='text-align:center'>Sistema de Asistencia RRHH</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;color:#6B7280'>Conecta tu cuenta de Google Service Account para continuar</p>",
                unsafe_allow_html=True)
    st.divider()

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        uploaded = st.file_uploader(
            "📂 Sube el archivo credentials.json (Service Account)",
            type=["json"],
            help="Descarga el JSON de tu Service Account en Google Cloud Console y compártelo con el spreadsheet."
        )
        if uploaded:
            try:
                creds_dict = json.load(uploaded)
                if st.button("🔗 Conectar a Google Sheets", use_container_width=True, type="primary"):
                    with st.spinner("Conectando…"):
                        if conectar_sheets(creds_dict):
                            st.success("✅ Conexión exitosa")
                            st.rerun()
            except Exception:
                st.error("Archivo JSON inválido. Verifica que sea el credentials.json correcto.")

        st.markdown("---")
        with st.expander("ℹ️ ¿Cómo obtener las credenciales?"):
            st.markdown("""
1. Ve a [Google Cloud Console](https://console.cloud.google.com)
2. Crea o selecciona un proyecto
3. Activa **Google Sheets API** y **Google Drive API**
4. En *IAM & Admin → Service Accounts*, crea una cuenta de servicio
5. Genera una clave JSON y descárgala
6. Comparte el spreadsheet con el email del Service Account (permisos de Editor)
7. Sube el JSON aquí
            """)


# ── Primer arranque: creación del administrador ──────────────────────────────
def pantalla_primer_admin():
    """Se muestra una sola vez, cuando la hoja Usuarios aún no tiene un admin.
    La persona define su propia contraseña en lugar de heredar una fija."""
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.warning("⚠️ **Primer arranque** – todavía no existe un administrador. "
                   "Crea la cuenta ahora para asegurar el sistema.")
        with st.form("form_primer_admin"):
            nuevo_id = st.text_input("ID del administrador", value="admin",
                                     help="Puedes dejar 'admin' o usar tu ID de empleado.")
            p1 = st.text_input("Contraseña nueva", type="password")
            p2 = st.text_input("Repetir contraseña", type="password")
            st.caption("Mínimo 8 caracteres, con letras y al menos un número o símbolo.")
            crear = st.form_submit_button("🔐 Crear administrador", type="primary",
                                          use_container_width=True)
        if crear:
            if not nuevo_id.strip():
                st.error("Ingresa un ID.")
            elif p1 != p2:
                st.error("Las contraseñas no coinciden.")
            else:
                problema = password_debil(p1)
                if problema:
                    st.error(f"❌ {problema}")
                else:
                    try:
                        get_sm().crear_usuario(nuevo_id.strip(), p1, "admin")
                        st.success("✅ Administrador creado. Ya puedes iniciar sesión.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ No se pudo crear el usuario: {e}")


# ── Pantalla de login de empleado ─────────────────────────────────────────────
def pantalla_login_empleado():
    col_center = st.columns([1, 2, 1])[1]
    with col_center:
        st.image("https://www.quski.ec/wp-content/uploads/2022/08/logo-quski.png",
                 width=180, use_container_width=False)
    st.markdown("<h2 style='text-align:center'>Sistema de Asistencia RRHH</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;color:#6B7280'>Ingresa con tu usuario y contraseña</p>",
                unsafe_allow_html=True)
    st.divider()

    # Primer arranque: si todavía no existe ningún administrador, se pide crear
    # uno aquí. Antes el sistema creaba solo un admin con contraseña fija escrita
    # en el código fuente, lo que la dejaba a la vista de cualquiera con acceso
    # al repositorio.
    sm_ini = get_sm()
    try:
        sin_admin = sm_ini is not None and not sm_ini.hay_admin()
    except Exception:
        sin_admin = False
    if sin_admin:
        pantalla_primer_admin()
        return

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("form_login_emp"):
            id_input = st.text_input("👤 ID de empleado (o 'admin')", placeholder="Ej: 301")
            pwd_input = st.text_input("🔑 Contraseña", type="password")
            submitted = st.form_submit_button("Ingresar", type="primary", use_container_width=True)

        if submitted:
            bloqueado_hasta = st.session_state.get("login_bloqueado_hasta")
            ahora = datetime.now()

            if bloqueado_hasta and ahora < bloqueado_hasta:
                restante = int((bloqueado_hasta - ahora).total_seconds())
                st.error(f"🔒 Demasiados intentos fallidos. Espera {restante} segundos "
                         "antes de volver a intentar.")
            elif not id_input or not pwd_input:
                st.error("Ingresa tu ID y contraseña.")
            else:
                sm = get_sm()
                resultado = sm.verificar_credenciales(id_input, pwd_input)
                if resultado is None:
                    fallidos = st.session_state.get("login_fallidos", 0) + 1
                    st.session_state.login_fallidos = fallidos
                    restantes = MAX_INTENTOS_LOGIN - fallidos
                    if restantes <= 0:
                        st.session_state.login_bloqueado_hasta = \
                            ahora + timedelta(seconds=BLOQUEO_SEGUNDOS)
                        st.session_state.login_fallidos = 0
                        st.error(f"🔒 Demasiados intentos fallidos. "
                                 f"Espera {BLOQUEO_SEGUNDOS // 60} minutos.")
                    else:
                        # Mensaje genérico a propósito: no revela si el ID existe.
                        st.error(f"❌ ID o contraseña incorrectos. "
                                 f"Te quedan {restantes} intento(s).")
                else:
                    st.session_state.login_fallidos = 0
                    st.session_state.login_bloqueado_hasta = None
                    # Datos de la ficha del empleado (nombre y correo)
                    nombre, correo = "Administrador", ""
                    if resultado["id_empleado"] != "admin":
                        emp = sm.get_empleado(resultado["id_empleado"])
                        if emp:
                            nombre = str(emp.get("Nombre", resultado["id_empleado"]))
                            correo = str(emp.get("Email", "")).strip()
                    else:
                        correo = st.session_state.config.get("Email_RRHH", "")
                    resultado["nombre"] = nombre
                    resultado["email"]  = correo
                    st.session_state.usuario = resultado
                    st.rerun()

        st.caption("¿Olvidaste tu contraseña? Contacta a RRHH.")


# ── Sidebar ───────────────────────────────────────────────────────────────────
def sidebar():
    usuario = get_usuario()
    with st.sidebar:
        st.markdown("## 🏢 Quski RRHH")
        st.markdown("---")
        if usuario:
            st.markdown(f"👤 **{usuario['nombre']}**")
            if usuario.get("rol") == "admin":
                rol_label = "🔑 Administrador"
            elif es_jefe():
                rol_label = "✍️ Jefe inmediato"
            else:
                rol_label = "👔 Empleado"
            st.caption(rol_label)
            st.markdown("---")

        if es_admin():
            options = [
                "🏠  Dashboard",
                "👥  Empleados",
                "✅  Asistencia",
                "📋  Permisos",
                "🏖️  Vacaciones",
                "⏰  Horas Extras",
                "⚙️  Configuración",
                "🔐  Gestión Usuarios",
                "⚠️  Llamados de Atención",
                "📁  Expediente",
            ]
        else:
            options = ["✅  Mi Asistencia"]
            # Quien tiene personal a cargo ve primero su bandeja de aprobaciones
            if es_jefe():
                options.append("✍️  Aprobaciones")
            options += [
                "📋  Mis Permisos",
                "🏖️  Mis Vacaciones",
                "⏰  Mis Horas Extra",
                "🔑  Cambiar Contraseña",
            ]

        page = st.selectbox("Módulo", options=options, label_visibility="collapsed")
        st.markdown("---")
        st.caption(f"📅 {date.today().strftime('%d/%m/%Y')}")
        if st.button("🔒 Cerrar sesión", use_container_width=True):
            st.session_state.usuario = None
            st.rerun()
    return page.split("  ")[-1].strip()


# ── Helpers UI ────────────────────────────────────────────────────────────────
def metric_card(label: str, value, icon: str = ""):
    st.markdown(f"""
    <div class="metric-card">
        <p>{icon} {label}</p>
        <h2>{value}</h2>
    </div>""", unsafe_allow_html=True)


def badge(text: str, kind: str = "info") -> str:
    return f'<span class="status-badge badge-{kind}">{text}</span>'


def status_badge(estado: str) -> str:
    estado_l = str(estado).lower()
    if "aprobado" in estado_l or "a_tiempo" in estado_l or "activo" in estado_l:
        return badge(estado, "ok")
    if "pendiente" in estado_l or "tardanza" in estado_l:
        return badge(estado, "warning")
    if "rechazo" in estado_l or "ausente" in estado_l:
        return badge(estado, "danger")
    return badge(estado, "info")


def df_con_badges(df: pd.DataFrame, col_estado: str = "Estado") -> pd.DataFrame:
    if col_estado in df.columns:
        df = df.copy()
        df[col_estado] = df[col_estado].apply(lambda v: status_badge(v))
    return df


# ── Módulo: Dashboard ─────────────────────────────────────────────────────────
def page_dashboard():
    sm = get_sm()
    st.title("🏠 Dashboard")
    hoy = date.today().strftime("%Y-%m-%d")
    mes = date.today().strftime("%Y-%m")

    try:
        emp_df  = sm.get_empleados()
        asis_df = sm.get_asistencia()
        perm_df = sm.get_permisos()
        vac_df  = sm.get_vacaciones()
        hex_df  = sm.get_horas_extras()
    except Exception as e:
        st.error(f"Error cargando datos: {e}")
        return

    total_emp = len(emp_df)
    asis_hoy  = len(asis_df[asis_df["Fecha"].astype(str) == hoy]) if not asis_df.empty else 0
    tardanzas_mes = len(asis_df[
        (asis_df["Fecha"].astype(str).str.startswith(mes)) &
        (asis_df["Estado"].astype(str) == "Tardanza")
    ]) if not asis_df.empty else 0
    pend_permisos = len(perm_df[perm_df["Estado"].astype(str).str.lower() == "pendiente_aprobacion"]) if not perm_df.empty else 0
    pend_vac = len(vac_df[vac_df["Estado"].astype(str).str.lower() == "pendiente"]) if not vac_df.empty else 0

    col1, col2, col3, col4 = st.columns(4)
    with col1: metric_card("Total empleados", total_emp, "👥")
    with col2: metric_card("Asistencias hoy", asis_hoy, "✅")
    with col3: metric_card("Tardanzas en el mes", tardanzas_mes, "⚠️")
    with col4: metric_card("Solicitudes pendientes", pend_permisos + pend_vac, "📋")

    st.divider()
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("📅 Asistencia de hoy")
        asis_hoy_df = asis_df[asis_df["Fecha"].astype(str) == hoy] if not asis_df.empty else pd.DataFrame()
        if not asis_hoy_df.empty:
            st.dataframe(
                asis_hoy_df[["ID_Empleado","Nombre","Hora_Entrada","Hora_Salida","Estado","Minutos_Atraso"]],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("Sin registros de asistencia hoy.")

    with col_b:
        st.subheader("📋 Solicitudes pendientes")
        pendientes = []
        if not perm_df.empty:
            p = perm_df[perm_df["Estado"].astype(str).str.lower() == "pendiente_aprobacion"].copy()
            if not p.empty:
                p["Tipo"] = "Permiso"
                pendientes.append(p[["ID_Permiso","ID_Empleado","Fecha","Tipo"]].rename(columns={"ID_Permiso":"ID"}))
        if not vac_df.empty:
            v = vac_df[vac_df["Estado"].astype(str).str.lower() == "pendiente"].copy()
            if not v.empty:
                v["Tipo"] = "Vacación"
                pendientes.append(v[["ID_Vacacion","ID_Empleado","Fecha_Inicio","Tipo"]].rename(
                    columns={"ID_Vacacion":"ID","Fecha_Inicio":"Fecha"}))
        if pendientes:
            df_p = pd.concat(pendientes, ignore_index=True)
            st.dataframe(df_p, use_container_width=True, hide_index=True)
        else:
            st.success("✅ Sin solicitudes pendientes.")

    # Gráfico de asistencia mensual
    if not asis_df.empty and "Fecha" in asis_df.columns:
        st.subheader("📈 Asistencia del mes")
        asis_mes = asis_df[asis_df["Fecha"].astype(str).str.startswith(mes)].copy()
        if not asis_mes.empty:
            asis_mes["Fecha"] = pd.to_datetime(asis_mes["Fecha"])
            chart_data = asis_mes.groupby(["Fecha","Estado"]).size().unstack(fill_value=0)
            st.bar_chart(chart_data)


# ── Módulo: Empleados ─────────────────────────────────────────────────────────
def page_empleados():
    sm = get_sm()
    st.title("👥 Empleados")
    tab1, tab2, tab3 = st.tabs(["📋 Lista", "➕ Nuevo empleado", "✏️ Editar"])

    df = sm.get_empleados()

    with tab1:
        if df.empty:
            st.info("No hay empleados registrados.")
        else:
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                buscar = st.text_input("🔍 Buscar por nombre o ID", "")
            with col_f2:
                areas = ["Todos"] + sorted(df["Area"].dropna().unique().tolist()) if "Area" in df.columns else ["Todos"]
                area_filtro = st.selectbox("Filtrar por área", areas)

            df_f = df.copy()
            if buscar:
                mask = df_f["Nombre"].astype(str).str.contains(buscar, case=False, na=False) | \
                       df_f["ID_Empleado"].astype(str).str.contains(buscar, case=False, na=False)
                df_f = df_f[mask]
            if area_filtro != "Todos":
                df_f = df_f[df_f["Area"].astype(str) == area_filtro]

            st.dataframe(df_f, use_container_width=True, hide_index=True)
            st.caption(f"{len(df_f)} empleado(s) encontrado(s)")

    with tab2:
        with st.form("form_nuevo_emp", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                nombre     = st.text_input("Nombre completo *")
                email      = st.text_input("Email corporativo *")
                area       = st.text_input("Área / Departamento *")
            with c2:
                email_jefe = st.text_input("Email del jefe *")
                hora_ini   = st.time_input("Horario de entrada", value=datetime.strptime("09:00", "%H:%M").time())
                hora_fin   = st.time_input("Horario de salida",  value=datetime.strptime("17:30", "%H:%M").time())

            submitted = st.form_submit_button("💾 Guardar empleado", type="primary")
            if submitted:
                if not all([nombre, email, area, email_jefe]):
                    st.error("Por favor completa todos los campos obligatorios (*)")
                else:
                    with st.spinner("Guardando…"):
                        emp_id = sm.agregar_empleado(
                            nombre, email, area, email_jefe,
                            hora_ini.strftime("%H:%M"), hora_fin.strftime("%H:%M")
                        )
                    st.success(f"✅ Empleado creado: **{emp_id} – {nombre}**")

    with tab3:
        if df.empty:
            st.info("No hay empleados para editar.")
        else:
            opciones = {f"{row['ID_Empleado']} – {row['Nombre']}": row["ID_Empleado"]
                        for _, row in df.iterrows()}
            sel = st.selectbox("Selecciona empleado", list(opciones.keys()))
            emp_id = opciones[sel]
            emp = df[df["ID_Empleado"].astype(str) == str(emp_id)].iloc[0]

            with st.form("form_editar_emp"):
                c1, c2 = st.columns(2)
                with c1:
                    nombre     = st.text_input("Nombre", emp.get("Nombre", ""))
                    email      = st.text_input("Email", emp.get("Email", ""))
                    area       = st.text_input("Área", emp.get("Area", ""))
                with c2:
                    email_jefe = st.text_input("Email jefe", emp.get("Email_Jefe", ""))
                    ini_t = datetime.strptime(str(emp.get("Horario_Inicio","09:00"))[:5], "%H:%M").time()
                    fin_t = datetime.strptime(str(emp.get("Horario_Fin","17:30"))[:5], "%H:%M").time()
                    hora_ini = st.time_input("Entrada", ini_t)
                    hora_fin = st.time_input("Salida",  fin_t)

                if st.form_submit_button("💾 Actualizar", type="primary"):
                    with st.spinner("Actualizando…"):
                        sm.actualizar_empleado(
                            str(emp_id), nombre, email, area, email_jefe,
                            hora_ini.strftime("%H:%M"), hora_fin.strftime("%H:%M")
                        )
                    st.success("✅ Empleado actualizado")


# ── Módulo: Asistencia ────────────────────────────────────────────────────────
def page_asistencia():
    sm = get_sm()
    config = st.session_state.config
    usuario = get_usuario()
    admin = es_admin()
    st.title("✅ Asistencia" if admin else "✅ Mi Asistencia")

    tabs = ["⬆️ Registrar entrada", "⬇️ Registrar salida", "📊 Historial"]
    if admin:
        tabs.append("❌ Marcar ausencia")
    tab_list = st.tabs(tabs)
    tab1, tab2, tab3 = tab_list[0], tab_list[1], tab_list[2]
    tab4 = tab_list[3] if admin else None

    df_emp = sm.get_empleados()
    hoy = date.today().strftime("%Y-%m-%d")

    # Filtrar empleados según rol
    if not admin and usuario:
        df_emp_fil = df_emp[df_emp["ID_Empleado"].astype(str) == usuario["id_empleado"]] if not df_emp.empty else df_emp
    else:
        df_emp_fil = df_emp

    def emp_opciones(df):
        if df.empty: return {}
        return {f"{r['ID_Empleado']} – {r['Nombre']}": (str(r["ID_Empleado"]), str(r["Nombre"]))
                for _, r in df.iterrows()}

    with tab1:
        st.subheader(f"Registrar entrada – {date.today().strftime('%d/%m/%Y')}")
        opciones = emp_opciones(df_emp_fil)
        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            if admin:
                sel = st.selectbox("Empleado", list(opciones.keys()), key="entrada_emp")
                emp_id, nombre = opciones[sel]
            else:
                emp_id, nombre = list(opciones.values())[0]
                st.info(f"👤 Registrando asistencia para: **{nombre}**")
            hora = st.time_input("Hora de entrada", value=datetime.now().time(), key="hora_entrada")

            if st.button("📌 Registrar entrada", type="primary"):
                if sm.ya_registro_entrada(emp_id, hoy):
                    st.warning(f"⚠️ {nombre} ya tiene entrada registrada hoy.")
                else:
                    with st.spinner("Registrando…"):
                        estado, atraso = sm.registrar_entrada(emp_id, nombre, hora.strftime("%H:%M"), config)
                    if estado == "Tardanza":
                        st.warning(f"⚠️ Tardanza registrada: **{atraso} minuto(s)** de retraso.")
                    else:
                        st.success(f"✅ Entrada registrada a tiempo para **{nombre}**")

    with tab2:
        st.subheader("Registrar salida")
        opciones = emp_opciones(df_emp_fil)
        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            if admin:
                sel = st.selectbox("Empleado", list(opciones.keys()), key="salida_emp")
                emp_id, nombre = opciones[sel]
            else:
                emp_id, nombre = list(opciones.values())[0]
                st.info(f"👤 Registrando salida para: **{nombre}**")
            fecha_sal = st.date_input("Fecha", date.today(), key="fecha_salida")
            hora_sal  = st.time_input("Hora de salida", value=datetime.now().time(), key="hora_salida")
            obs       = st.text_input("Observaciones (opcional)", key="obs_salida")

            if st.button("📌 Registrar salida", type="primary"):
                try:
                    with st.spinner("Registrando…"):
                        sm.registrar_salida(emp_id, fecha_sal.strftime("%Y-%m-%d"),
                                            hora_sal.strftime("%H:%M"), obs)
                    st.success(f"✅ Salida registrada para **{nombre}**")
                except ValueError as e:
                    st.error(str(e))

    with tab3:
        st.subheader("Historial de asistencia")
        c1, c2 = st.columns(2)
        with c1:
            fecha_desde = st.date_input("Desde", date.today().replace(day=1))
        with c2:
            fecha_hasta = st.date_input("Hasta", date.today())

        df_asis = sm.get_df("Asistencia")
        if not df_asis.empty:
            df_asis["Fecha"] = pd.to_datetime(df_asis["Fecha"], errors="coerce")
            mask = (df_asis["Fecha"].dt.date >= fecha_desde) & (df_asis["Fecha"].dt.date <= fecha_hasta)
            df_filtro = df_asis[mask].copy()
            df_filtro["Fecha"] = df_filtro["Fecha"].dt.strftime("%Y-%m-%d")

            # Empleados solo ven su propio historial
            if not admin and usuario:
                df_filtro = df_filtro[df_filtro["ID_Empleado"].astype(str) == usuario["id_empleado"]]
            elif admin and not df_emp.empty and "ID_Empleado" in df_filtro.columns:
                emp_fil = st.selectbox("Filtrar empleado", ["Todos"] +
                          [f"{r['ID_Empleado']} – {r['Nombre']}" for _, r in df_emp.iterrows()], key="hist_emp")
                if emp_fil != "Todos":
                    eid = emp_fil.split(" – ")[0]
                    df_filtro = df_filtro[df_filtro["ID_Empleado"].astype(str) == eid]

            if df_filtro.empty:
                st.info("Sin registros en el período seleccionado.")
            else:
                st.dataframe(df_filtro, use_container_width=True, hide_index=True)
                st.caption(f"{len(df_filtro)} registro(s)")
                st.markdown("**Resumen del período**")
                rc1, rc2, rc3 = st.columns(3)
                with rc1: st.metric("Total registros", len(df_filtro))
                with rc2: st.metric("Tardanzas", len(df_filtro[df_filtro["Estado"] == "Tardanza"]))
                with rc3: st.metric("Ausencias",  len(df_filtro[df_filtro["Estado"] == "Ausente"]))
        else:
            st.info("No hay registros de asistencia.")

    if tab4:
        with tab4:
            st.subheader("Marcar ausencia")
            opciones = emp_opciones(df_emp)
            if not opciones:
                st.warning("No hay empleados registrados.")
            else:
                sel = st.selectbox("Empleado", list(opciones.keys()), key="aus_emp")
                emp_id, nombre = opciones[sel]
                fecha_aus = st.date_input("Fecha de ausencia", date.today())
                motivo_aus = st.text_area("Motivo de ausencia")
                if st.button("📌 Registrar ausencia", type="primary"):
                    with st.spinner("Registrando…"):
                        sm.marcar_ausencia(emp_id, nombre, fecha_aus.strftime("%Y-%m-%d"), motivo_aus)
                    st.success(f"✅ Ausencia registrada para **{nombre}**")


# ── Módulo: Configuración ─────────────────────────────────────────────────────
def _cfg_int(config: dict, key: str, default: int) -> int:
    """Lee un entero del dict de config de forma segura."""
    try:
        return int(float(config.get(key, default)))
    except (ValueError, TypeError):
        return default


def _cfg_float(config: dict, key: str, default: float) -> float:
    """Lee un float del dict de config de forma segura."""
    try:
        return float(config.get(key, default))
    except (ValueError, TypeError):
        return default


def _cfg_time(config: dict, key: str, default: str):
    """Lee una hora (HH:MM) del dict de config de forma segura."""
    try:
        return datetime.strptime(str(config.get(key, default))[:5], "%H:%M").time()
    except (ValueError, TypeError):
        return datetime.strptime(default[:5], "%H:%M").time()


def page_configuracion():
    sm = get_sm()
    config = st.session_state.config
    st.title("⚙️ Configuración del sistema")

    _ZONAS = ["America/Guayaquil", "America/Bogota", "America/Lima", "America/New_York", "America/Mexico_City"]

    with st.form("form_config"):
        st.subheader("Horarios")
        c1, c2 = st.columns(2)
        with c1:
            h_ini = st.time_input("Horario de entrada", _cfg_time(config, "Horario_Inicio", "09:00"))
            tolerancia = st.number_input("Tolerancia (minutos)",
                min_value=0, max_value=30, value=_cfg_int(config, "Tolerancia_Minutos", 0))
        with c2:
            h_fin = st.time_input("Horario de salida", _cfg_time(config, "Horario_Fin", "17:30"))
            horas_perm = st.number_input("Horas de permiso mensual",
                min_value=1.0, max_value=20.0, step=0.5, value=_cfg_float(config, "Horas_Permiso_Mensual", 3.0))

        st.subheader("Contacto y zona horaria")
        email_rrhh = st.text_input("Email RRHH", config.get("Email_RRHH", "rrhh@quski.ec"))
        zona_saved = config.get("Zona_Horaria", "America/Guayaquil")
        zona_idx   = _ZONAS.index(zona_saved) if zona_saved in _ZONAS else 0
        zona = st.selectbox("Zona horaria", _ZONAS, index=zona_idx)

        st.subheader("⚠️ Política disciplinaria – Tardanzas")
        st.caption("Número de tardanzas acumuladas en el mes que activan cada tipo de llamado de atención")
        d1, d2, d3 = st.columns(3)
        with d1:
            tard_verbal   = st.number_input("Llamado Verbal", min_value=1, max_value=20,
                value=_cfg_int(config, "Tardanzas_Llamado_Verbal", 3))
        with d2:
            tard_escrito  = st.number_input("Llamado Escrito", min_value=1, max_value=20,
                value=_cfg_int(config, "Tardanzas_Llamado_Escrito", 5))
        with d3:
            tard_suspension = st.number_input("Suspensión", min_value=1, max_value=30,
                value=_cfg_int(config, "Tardanzas_Suspension", 8))

        if st.form_submit_button("💾 Guardar configuración", type="primary"):
            updates = {
                "Horario_Inicio":              h_ini.strftime("%H:%M"),
                "Horario_Fin":                 h_fin.strftime("%H:%M"),
                "Tolerancia_Minutos":          str(tolerancia),
                "Horas_Permiso_Mensual":       str(horas_perm),
                "Email_RRHH":                  email_rrhh,
                "Zona_Horaria":                zona,
                "Tardanzas_Llamado_Verbal":    str(tard_verbal),
                "Tardanzas_Llamado_Escrito":   str(tard_escrito),
                "Tardanzas_Suspension":        str(tard_suspension),
            }
            with st.spinner("Guardando…"):
                try:
                    sm.save_all_config(updates)
                    # Actualizar session_state directamente con lo que acabamos
                    # de guardar — NO re-leer de Sheets para evitar el delay de
                    # propagación de la API que devuelve datos viejos.
                    st.session_state.config = {**CONFIG_DEFAULTS, **updates}
                    st.success("✅ Configuración guardada")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Error al guardar: {e}")

    # ── Diagnóstico de email ──────────────────────────────────────────────────
    st.divider()
    st.subheader("🧪 Diagnóstico de Email")
    st.caption("Envía un correo de prueba para verificar que la configuración SMTP funciona correctamente.")

    # Mostrar estado actual de los Secrets
    with st.expander("🔍 Ver estado actual de la configuración SMTP (Secrets)", expanded=False):
        if "email" not in st.secrets:
            st.error("❌ **No existe** la sección `[email]` en los Secrets de Streamlit.")
            st.code("""# Agrega esto en Streamlit Cloud → App settings → Secrets:
[email]
smtp_server   = "smtp.gmail.com"
smtp_port     = "587"
smtp_user     = "tu_correo@gmail.com"
smtp_password = "xxxx xxxx xxxx xxxx"   # Contraseña de Aplicación (16 chars)""", language="toml")
        else:
            cfg_e = st.secrets["email"]
            st.success("✅ Sección `[email]` encontrada en Secrets.")
            c1, c2 = st.columns(2)
            with c1:
                st.write(f"**smtp_server:** `{cfg_e.get('smtp_server', '(no definido)')}`")
                st.write(f"**smtp_port:** `{cfg_e.get('smtp_port', '(no definido)')}`")
            with c2:
                u = cfg_e.get("smtp_user", "")
                st.write(f"**smtp_user:** `{u if u else '(vacío)'}`")
                p = cfg_e.get("smtp_password", "")
                p_disp = ("*" * min(len(p), 8) + f" ({len(p)} caracteres)") if p else "(vacío)"
                st.write(f"**smtp_password:** `{p_disp}`")
                if p and len(p) < 14:
                    st.warning("⚠️ La contraseña parece muy corta. Una Contraseña de Aplicación de Google tiene 16 caracteres (puede escribirse con o sin espacios).")
                elif p and len(p.replace(" ", "")) != 16:
                    st.warning(f"⚠️ Sin espacios son {len(p.replace(' ', ''))} caracteres. Las Contraseñas de Aplicación de Google tienen exactamente 16 caracteres sin espacios.")

    dest_prueba = st.text_input(
        "Destino del correo de prueba",
        value=config.get("Email_RRHH", ""),
        help="Email donde quieres recibir el correo de prueba"
    )
    if st.button("📨 Enviar correo de prueba", type="secondary", use_container_width=True):
        if not dest_prueba:
            st.warning("Escribe un email de destino primero.")
        else:
            with st.spinner("Enviando…"):
                ok, err = enviar_notificacion_email(
                    dest_prueba,
                    "Correo de prueba",
                    "<p>Este es un correo de prueba enviado desde el Sistema de Asistencia RRHH – Quski.</p>"
                    "<p>Si recibes este mensaje, la configuración SMTP funciona correctamente. ✅</p>",
                    _return_error=True
                )
            if ok:
                st.success(f"✅ Correo enviado exitosamente a **{dest_prueba}**. ¡Revisa tu bandeja de entrada!")
            else:
                st.error("**Error al enviar el correo:**")
                st.code(err, language=None)
                st.info(
                    "**Solución más común:** La Contraseña de Aplicación de Google se genera en "
                    "[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). "
                    "Asegúrate de que la verificación en dos pasos esté activada en tu cuenta Gmail."
                )


# ── Módulo: Gestión de Usuarios (solo admin) ──────────────────────────────────
def page_gestion_usuarios():
    sm = get_sm()
    st.title("🔐 Gestión de Usuarios")
    st.info("Aquí puedes crear o resetear contraseñas para cada empleado.")

    df_emp = sm.get_empleados()
    df_usr = sm.get_usuarios()

    tab1, tab2 = st.tabs(["➕ Crear / Resetear contraseña", "📋 Lista de usuarios"])

    with tab1:
        opciones_emp = {}
        if not df_emp.empty:
            opciones_emp = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                            for _, r in df_emp.iterrows()}
        opciones_emp["admin – Administrador RRHH"] = "admin"

        with st.form("form_crear_usuario", clear_on_submit=True):
            sel = st.selectbox("Empleado", list(opciones_emp.keys()))
            emp_id = opciones_emp[sel]
            nueva_pwd = st.text_input("Nueva contraseña", type="password",
                                      help="Mínimo 8 caracteres, con letras y al menos "
                                           "un número o símbolo.")
            confirmar = st.text_input("Confirmar contraseña", type="password")
            rol = st.selectbox("Rol", ["empleado", "admin"])

            if st.form_submit_button("💾 Guardar", type="primary"):
                problema = password_debil(nueva_pwd)
                if problema:
                    st.error(f"❌ {problema}")
                elif nueva_pwd != confirmar:
                    st.error("Las contraseñas no coinciden.")
                else:
                    with st.spinner("Guardando…"):
                        sm.crear_usuario(emp_id, nueva_pwd, rol)
                    st.success(f"✅ Usuario **{emp_id}** configurado correctamente.")

    with tab2:
        if df_usr.empty:
            st.info("No hay usuarios creados aún.")
        else:
            # Mostrar sin el hash de contraseña
            df_show = df_usr[["ID_Empleado", "Rol"]].copy() if "Password_Hash" in df_usr.columns else df_usr
            st.dataframe(df_show, use_container_width=True, hide_index=True)
            st.caption(f"{len(df_usr)} usuario(s) registrado(s)")


# ── Módulo: Cambiar Contraseña (empleado) ─────────────────────────────────────
def page_cambiar_password():
    sm = get_sm()
    usuario = get_usuario()
    st.title("🔑 Cambiar Contraseña")

    with st.form("form_cambiar_pwd", clear_on_submit=True):
        st.info(f"Cambiando contraseña para: **{usuario['nombre']}** (ID: {usuario['id_empleado']})")
        pwd_actual  = st.text_input("Contraseña actual", type="password")
        pwd_nueva   = st.text_input("Nueva contraseña", type="password")
        pwd_confirm = st.text_input("Confirmar nueva contraseña", type="password")

        if st.form_submit_button("🔒 Cambiar contraseña", type="primary"):
            problema = password_debil(pwd_nueva)
            if not sm.verificar_credenciales(usuario["id_empleado"], pwd_actual):
                st.error("❌ La contraseña actual es incorrecta.")
            elif problema:
                st.error(f"❌ {problema}")
            elif pwd_nueva == pwd_actual:
                st.error("La nueva contraseña debe ser distinta de la actual.")
            elif pwd_nueva != pwd_confirm:
                st.error("Las contraseñas nuevas no coinciden.")
            else:
                with st.spinner("Cambiando…"):
                    sm.cambiar_password(usuario["id_empleado"], pwd_nueva)
                st.success("✅ Contraseña cambiada exitosamente.")


# ── Tarjetas de aprobación (compartidas por permisos y vacaciones) ────────────
def _datos_solicitud(sm, fila, tipo: str) -> dict:
    """Normaliza una fila de Permisos o Vacaciones a una estructura común."""
    emp_id = str(fila["ID_Empleado"]).strip()
    nombre = sm.get_nombre_empleado(emp_id)
    if tipo == "permiso":
        return {
            "id":      str(fila["ID_Permiso"]).strip(),
            "emp_id":  emp_id,
            "nombre":  nombre,
            "titulo":  f"Permiso · {fila['Horas_Solicitadas']} h",
            "detalle": [("Fecha", fila.get("Fecha", "—")),
                        ("Horas solicitadas", f"{fila.get('Horas_Solicitadas', '—')} h"),
                        ("Motivo", fila.get("Motivo", "—") or "—")],
            "asunto":  "permiso",
        }
    return {
        "id":      str(fila["ID_Vacacion"]).strip(),
        "emp_id":  emp_id,
        "nombre":  nombre,
        "titulo":  f"Vacaciones · {fila.get('Dias_Habiles', '—')} días hábiles",
        "detalle": [("Desde", fila.get("Fecha_Inicio", "—")),
                    ("Hasta", fila.get("Fecha_Fin", "—")),
                    ("Días hábiles", fila.get("Dias_Habiles", "—"))],
        "asunto":  "vacaciones",
    }


def _correos_de(sm, emp_id: str, config: dict) -> tuple:
    """Devuelve (email_empleado, email_jefe, email_rrhh)."""
    return (sm.get_email_empleado(emp_id),
            sm.get_email_jefe(emp_id),
            config.get("Email_RRHH", ""))


def tarjeta_aprobacion(sm, config, fila, tipo: str, etapa: str,
                       aprobador_default: str, en_nombre_del_jefe: bool = False):
    """Dibuja una solicitud como tarjeta con botones de aprobar y rechazar.

    etapa: 'jefe'  → firma del jefe inmediato (pasa la solicitud a RRHH)
           'rrhh'  → firma final de RRHH (deja la solicitud aprobada)
    en_nombre_del_jefe: RRHH registra la autorización que el jefe dio por fuera.
    """
    d = _datos_solicitud(sm, fila, tipo)
    email_emp, email_jefe, email_rrhh = _correos_de(sm, d["emp_id"], config)
    k = f"{tipo}_{etapa}_{d['id']}"

    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"### {d['nombre']}")
            st.caption(f"ID empleado: {d['emp_id']}  ·  Solicitud: {d['id']}")
        with c2:
            st.markdown(f"**{d['titulo']}**")

        for etiqueta, valor in d["detalle"]:
            st.markdown(f"**{etiqueta}:** {valor}")

        # Si el jefe ya firmó, mostrarlo para que RRHH tenga el contexto
        firma_jefe = str(fila.get("Aprobado_Jefe", "") or "").strip()
        if firma_jefe:
            st.success(f"👤 Jefe inmediato: **{firma_jefe}** "
                       f"({fila.get('Fecha_Aprob_Jefe', '')})")
        elif etapa == "rrhh":
            st.caption("Sin registro de aprobación del jefe inmediato.")

        if en_nombre_del_jefe:
            st.warning("Esta solicitud aún espera al jefe inmediato "
                       f"({email_jefe or 'sin correo asignado'}). "
                       "Puedes registrar aquí su autorización si ya la dio por otro medio.")

        aprobador = st.text_input(
            "Aprobado por (nombre y cargo) *", value=aprobador_default,
            key=f"apr_{k}",
            help="Queda registrado en la hoja y en el correo que recibe el empleado.")

        b1, b2 = st.columns(2)
        with b1:
            etiqueta_btn = ("✅ Registrar aprobación del jefe" if en_nombre_del_jefe
                            else "✅ Aprobar")
            if st.button(etiqueta_btn, key=f"ok_{k}", type="primary",
                         use_container_width=True):
                if not aprobador.strip():
                    st.error("Indica quién aprueba.")
                else:
                    _procesar_aprobacion(sm, config, d, tipo, etapa, aprobador.strip(),
                                         email_emp, email_jefe, email_rrhh,
                                         en_nombre_del_jefe)
        with b2:
            with st.popover("❌ Rechazar", use_container_width=True):
                motivo_rech = st.text_area("Motivo del rechazo *", key=f"mr_{k}",
                                           placeholder="Explica brevemente por qué se rechaza.")
                if st.button("Confirmar rechazo", key=f"no_{k}", use_container_width=True):
                    if not motivo_rech.strip():
                        st.error("El motivo del rechazo es obligatorio.")
                    elif not aprobador.strip():
                        st.error("Indica quién rechaza.")
                    else:
                        _procesar_rechazo(sm, config, d, tipo, aprobador.strip(),
                                          motivo_rech.strip(), email_emp, email_jefe,
                                          email_rrhh)


def _procesar_aprobacion(sm, config, d, tipo, etapa, aprobador,
                         email_emp, email_jefe, email_rrhh, en_nombre_del_jefe):
    """Guarda la aprobación y envía los correos correspondientes."""
    es_permiso = (tipo == "permiso")
    try:
        with st.spinner("Guardando aprobación…"):
            if etapa == "jefe" or en_nombre_del_jefe:
                if es_permiso:
                    sm.aprobar_permiso_jefe(d["id"], aprobador)
                else:
                    sm.aprobar_vacaciones_jefe(d["id"], aprobador)
            else:
                if es_permiso:
                    sm.aprobar_permiso_rrhh(d["id"], aprobador)
                else:
                    sm.aprobar_vacaciones_rrhh(d["id"], aprobador)
    except Exception as e:
        st.error(f"❌ No se pudo guardar la aprobación: {e}")
        return

    final = (etapa == "rrhh" and not en_nombre_del_jefe)
    filas = d["detalle"] + [("Aprobado por", aprobador)]

    if final:
        st.success(f"✅ {d['asunto'].capitalize()} **{d['id']}** aprobado en firme.")
        cuerpo = (f"<p>La solicitud de <strong>{esc(d['asunto'])}</strong> de "
                  f"<strong>{esc(d['nombre'])}</strong> quedó "
                  f"<strong style='color:#065F46'>APROBADA</strong> por RRHH.</p>"
                  + tabla_html(filas))
        destinos = [email_emp, email_jefe, email_rrhh]
        asunto = f"{d['asunto'].capitalize()} aprobado – {d['nombre']}"
    else:
        st.success(f"✅ Aprobación del jefe registrada. "
                   f"La solicitud **{d['id']}** pasa a revisión de RRHH.")
        cuerpo = (f"<p>El jefe inmediato aprobó la solicitud de "
                  f"<strong>{esc(d['asunto'])}</strong> de "
                  f"<strong>{esc(d['nombre'])}</strong>. "
                  f"Queda pendiente la revisión final de RRHH.</p>"
                  + tabla_html(filas))
        destinos = [email_rrhh, email_emp]
        asunto = f"Pendiente de RRHH: {d['asunto']} – {d['nombre']}"

    enviados, fallidos = notificar(destinos, asunto, cuerpo)
    mostrar_envio(enviados, fallidos)
    st.rerun()


def _procesar_rechazo(sm, config, d, tipo, quien, motivo,
                      email_emp, email_jefe, email_rrhh):
    try:
        with st.spinner("Registrando rechazo…"):
            if tipo == "permiso":
                sm.rechazar_permiso(d["id"], quien, motivo)
            else:
                sm.rechazar_vacaciones(d["id"], quien, motivo)
    except Exception as e:
        st.error(f"❌ No se pudo registrar el rechazo: {e}")
        return

    st.warning(f"Solicitud **{d['id']}** rechazada.")
    cuerpo = (f"<p>La solicitud de <strong>{esc(d['asunto'])}</strong> de "
              f"<strong>{esc(d['nombre'])}</strong> fue "
              f"<strong style='color:#991B1B'>RECHAZADA</strong>.</p>"
              + tabla_html(d["detalle"] + [("Rechazada por", quien),
                                           ("Motivo del rechazo", motivo)]))
    enviados, fallidos = notificar([email_emp, email_jefe, email_rrhh],
                                   f"{d['asunto'].capitalize()} rechazado – {d['nombre']}",
                                   cuerpo)
    mostrar_envio(enviados, fallidos)
    st.rerun()


def _tabla_estado(df, tipo: str):
    """Tabla legible para el empleado, con la etapa exacta del trámite."""
    if df.empty:
        return df
    out = df.copy()
    out["Situación"] = out["Estado"].apply(etiqueta_estado)
    cols = (["ID_Permiso", "Fecha", "Horas_Solicitadas", "Motivo", "Situación"]
            if tipo == "permiso" else
            ["ID_Vacacion", "Fecha_Inicio", "Fecha_Fin", "Dias_Habiles", "Situación"])
    extra = [c for c in ("Aprobado_Jefe", "Aprobado_RRHH", "Motivo_Rechazo")
             if c in out.columns and out[c].astype(str).str.strip().ne("").any()]
    return out[[c for c in cols + extra if c in out.columns]]


# ── Módulo: Aprobaciones del jefe inmediato ──────────────────────────────────
def page_aprobaciones_jefe():
    sm = get_sm()
    config = st.session_state.config
    usuario = get_usuario()
    correo = email_usuario_actual()

    st.title("✍️ Aprobaciones de mi equipo")

    equipo = sm.get_subordinados(correo)
    if equipo.empty:
        st.info("No hay empleados asignados a tu cargo.")
        return
    st.caption(f"Tienes **{len(equipo)}** persona(s) a cargo.")

    perm_pend = sm.permisos_pendientes_jefe(correo)
    vac_pend  = sm.vacaciones_pendientes_jefe(correo)
    total = len(perm_pend) + len(vac_pend)

    if total == 0:
        st.success("✅ No tienes solicitudes pendientes por aprobar.")
    else:
        st.warning(f"⏳ Tienes **{total}** solicitud(es) esperando tu aprobación.")

    firma = f"{usuario.get('nombre', '')}".strip()

    t1, t2, t3 = st.tabs([f"📋 Permisos ({len(perm_pend)})",
                          f"🏖️ Vacaciones ({len(vac_pend)})",
                          "👥 Mi equipo"])
    with t1:
        if perm_pend.empty:
            st.info("Sin permisos pendientes.")
        for _, fila in perm_pend.iterrows():
            tarjeta_aprobacion(sm, config, fila, "permiso", "jefe", firma)

    with t2:
        if vac_pend.empty:
            st.info("Sin solicitudes de vacaciones pendientes.")
        for _, fila in vac_pend.iterrows():
            tarjeta_aprobacion(sm, config, fila, "vacacion", "jefe", firma)

    with t3:
        st.dataframe(equipo[[c for c in ("ID_Empleado", "Nombre", "Email", "Area")
                             if c in equipo.columns]],
                     use_container_width=True, hide_index=True)


# ── Módulo: Permisos (con restricción por rol) ────────────────────────────────
def _page_permisos_con_rol():
    sm = get_sm()
    config = st.session_state.config
    usuario = get_usuario()
    admin = es_admin()
    st.title("📋 Permisos" if admin else "📋 Mis Permisos")

    df_emp = sm.get_empleados()
    limite = _cfg_float(config, "Horas_Permiso_Mensual", 3.0)
    email_rrhh = config.get("Email_RRHH", "")

    # ── Vista RRHH / ADMIN ───────────────────────────────────────────────────
    if admin:
        pend_rrhh = sm.permisos_pendientes_rrhh()
        df_p = sm.get_permisos()
        pend_jefe = df_p[df_p["Estado"].astype(str) == EST_PEND_JEFE] if not df_p.empty else pd.DataFrame()

        tabs = st.tabs([f"⏳ Para aprobar ({len(pend_rrhh)})",
                        f"👤 Esperando al jefe ({len(pend_jefe)})",
                        "📋 Historial",
                        "➕ Nueva solicitud"])

        with tabs[0]:
            if pend_rrhh.empty:
                st.success("✅ No hay permisos esperando la firma de RRHH.")
            else:
                st.info(f"**{len(pend_rrhh)}** permiso(s) con el visto bueno del jefe, "
                        "listos para tu aprobación final.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_rrhh.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "permiso", "rrhh", firma)

        with tabs[1]:
            if pend_jefe.empty:
                st.success("✅ Ninguna solicitud está detenida en el jefe inmediato.")
            else:
                st.caption("Estas solicitudes esperan al jefe inmediato. Si el jefe ya "
                           "autorizó por correo o de forma verbal, puedes registrarlo aquí.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_jefe.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "permiso", "jefe", firma,
                                       en_nombre_del_jefe=True)

        with tabs[2]:
            if df_p.empty:
                st.info("No hay solicitudes de permisos.")
            else:
                opciones_est = ["Todos", EST_PEND_JEFE, EST_PEND_RRHH, EST_APROBADO, EST_RECHAZADO]
                filtro_est = st.selectbox("Filtrar por estado", opciones_est,
                                          format_func=lambda v: "Todos" if v == "Todos"
                                          else etiqueta_estado_corta(v))
                df_pf = df_p if filtro_est == "Todos" else df_p[df_p["Estado"].astype(str) == filtro_est]
                vista = df_pf.copy()
                if not vista.empty:
                    vista["Estado"] = vista["Estado"].apply(etiqueta_estado_corta)
                st.dataframe(vista, use_container_width=True, hide_index=True)
                st.caption(f"{len(df_pf)} solicitud(es)")

        with tabs[3]:
            st.info(f"ℹ️ Límite mensual: **{limite} horas por empleado**")
            opciones_a = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                          for _, r in df_emp.iterrows()} if not df_emp.empty else {}
            if not opciones_a:
                st.warning("No hay empleados registrados.")
            else:
                with st.form("form_permiso_admin", clear_on_submit=True):
                    sel = st.selectbox("Empleado", list(opciones_a.keys()))
                    fecha_p  = st.date_input("Fecha del permiso", date.today())
                    horas_p  = st.number_input("Horas solicitadas", min_value=0.5,
                                               max_value=8.0, step=0.5, value=1.0)
                    motivo_p = st.text_area("Motivo *")
                    if st.form_submit_button("📤 Registrar permiso", type="primary"):
                        if not motivo_p.strip():
                            st.error("El motivo es obligatorio.")
                        else:
                            _crear_permiso(sm, config, opciones_a[sel],
                                           sel.split(" – ")[-1],
                                           fecha_p, horas_p, motivo_p.strip(), email_rrhh)

    # ── Vista EMPLEADO ───────────────────────────────────────────────────────
    else:
        tab1, tab2 = st.tabs(["➕ Solicitar permiso", "📋 Mis permisos"])

        emp_id = usuario["id_empleado"] if usuario else ""
        emp = sm.get_empleado(emp_id)

        with tab1:
            if not emp:
                st.warning("No se encontró tu perfil de empleado. Avisa a RRHH.")
            else:
                nombre_emp = str(emp.get("Nombre", emp_id))
                jefe = sm.get_email_jefe(emp_id)
                st.info(f"👤 Solicitud para: **{emp_id} – {nombre_emp}**")
                if jefe:
                    st.caption(f"Tu solicitud irá primero a tu jefe inmediato ({jefe}) "
                               "y después a RRHH.")
                else:
                    st.caption("No tienes jefe inmediato asignado: tu solicitud irá "
                               "directamente a RRHH.")

                with st.form("form_permiso", clear_on_submit=True):
                    fecha_p  = st.date_input("Fecha del permiso", date.today())
                    horas_p  = st.number_input("Horas solicitadas", min_value=0.5,
                                               max_value=8.0, step=0.5, value=1.0)
                    motivo_p = st.text_area("Motivo *")
                    año_mes  = fecha_p.strftime("%Y-%m")
                    usadas   = sm.horas_permiso_usadas_mes(emp_id, año_mes)
                    st.caption(f"Horas usadas este mes: **{usadas:.1f}h** de **{limite:.1f}h** "
                               f"· Disponibles: **{max(0, limite - usadas):.1f}h**")
                    if st.form_submit_button("📤 Enviar solicitud", type="primary"):
                        if not motivo_p.strip():
                            st.error("El motivo es obligatorio.")
                        else:
                            _crear_permiso(sm, config, emp_id, nombre_emp,
                                           fecha_p, horas_p, motivo_p.strip(), email_rrhh)

        with tab2:
            df_p = sm.get_permisos()
            if not df_p.empty and usuario:
                df_p = df_p[df_p["ID_Empleado"].astype(str).str.strip() == str(emp_id).strip()]
            if df_p.empty:
                st.info("No tienes solicitudes de permisos.")
            else:
                st.dataframe(_tabla_estado(df_p, "permiso"),
                             use_container_width=True, hide_index=True)


def _crear_permiso(sm, config, emp_id, nombre_emp, fecha_p, horas_p, motivo_p, email_rrhh):
    """Registra el permiso y notifica al jefe inmediato y a RRHH."""
    try:
        with st.spinner("Enviando…"):
            res = sm.solicitar_permiso(emp_id, fecha_p.strftime("%Y-%m-%d"),
                                       horas_p, motivo_p, config)
    except Exception as e:
        st.error(f"❌ No se pudo registrar la solicitud: {e}")
        return

    st.success(f"✅ Permiso **{res['id']}** enviado — {etiqueta_estado(res['estado'])}")
    if res["excede_cupo"]:
        st.warning(f"⚠️ Con esta solicitud acumulas **{res['horas_usadas']:.1f}h** "
                   f"y el límite mensual es **{res['limite']:.1f}h**. "
                   "Requiere autorización expresa.")

    filas = [("Empleado", f"{emp_id} – {nombre_emp}"),
             ("Fecha", fecha_p.strftime("%d/%m/%Y")),
             ("Horas solicitadas", f"{horas_p} h"),
             ("Motivo", motivo_p),
             ("Horas acumuladas en el mes", f"{res['horas_usadas']:.1f} h de {res['limite']:.1f} h")]
    cuerpo = (f"<p><strong>{esc(nombre_emp)}</strong> ha solicitado un permiso.</p>"
              + tabla_html(filas)
              + "<p style='margin-top:16px'>Ingresa al sistema para aprobar o rechazar "
                "la solicitud.</p>")
    enviados, fallidos = notificar([res["email_jefe"], email_rrhh],
                                   f"Nueva solicitud de permiso – {nombre_emp}", cuerpo)
    mostrar_envio(enviados, fallidos)
    if not res["email_jefe"]:
        st.caption("ℹ️ Este empleado no tiene jefe inmediato registrado en su ficha, "
                   "así que solo se notificó a RRHH.")


# ── Módulo: Vacaciones (con restricción por rol) ──────────────────────────────
def _page_vacaciones_con_rol():
    sm = get_sm()
    config = st.session_state.config
    usuario = get_usuario()
    admin = es_admin()
    st.title("🏖️ Vacaciones" if admin else "🏖️ Mis Vacaciones")

    df_emp = sm.get_empleados()
    email_rrhh = config.get("Email_RRHH", "")

    # ── Vista RRHH / ADMIN ───────────────────────────────────────────────────
    if admin:
        pend_rrhh = sm.vacaciones_pendientes_rrhh()
        df_v = sm.get_vacaciones()
        pend_jefe = df_v[df_v["Estado"].astype(str) == EST_PEND_JEFE] if not df_v.empty else pd.DataFrame()

        tabs = st.tabs([f"⏳ Para aprobar ({len(pend_rrhh)})",
                        f"👤 Esperando al jefe ({len(pend_jefe)})",
                        "📋 Historial",
                        "➕ Nueva solicitud"])

        with tabs[0]:
            if pend_rrhh.empty:
                st.success("✅ No hay vacaciones esperando la firma de RRHH.")
            else:
                st.info(f"**{len(pend_rrhh)}** solicitud(es) con el visto bueno del jefe.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_rrhh.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "vacacion", "rrhh", firma)

        with tabs[1]:
            if pend_jefe.empty:
                st.success("✅ Ninguna solicitud está detenida en el jefe inmediato.")
            else:
                st.caption("Estas solicitudes esperan al jefe inmediato. Si el jefe ya "
                           "autorizó por otro medio, puedes registrarlo aquí.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_jefe.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "vacacion", "jefe", firma,
                                       en_nombre_del_jefe=True)

        with tabs[2]:
            if df_v.empty:
                st.info("No hay solicitudes de vacaciones.")
            else:
                opciones_est = ["Todos", EST_PEND_JEFE, EST_PEND_RRHH, EST_APROBADO, EST_RECHAZADO]
                filtro_v = st.selectbox("Filtrar por estado", opciones_est,
                                        format_func=lambda v: "Todos" if v == "Todos"
                                        else etiqueta_estado_corta(v))
                df_vf = df_v if filtro_v == "Todos" else df_v[df_v["Estado"].astype(str) == filtro_v]
                vista = df_vf.copy()
                if not vista.empty:
                    vista["Estado"] = vista["Estado"].apply(etiqueta_estado_corta)
                st.dataframe(vista, use_container_width=True, hide_index=True)
                st.caption(f"{len(df_vf)} solicitud(es)")

        with tabs[3]:
            opciones_a = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                          for _, r in df_emp.iterrows()} if not df_emp.empty else {}
            if not opciones_a:
                st.warning("No hay empleados registrados.")
            else:
                with st.form("form_vac_admin", clear_on_submit=True):
                    sel = st.selectbox("Empleado", list(opciones_a.keys()))
                    c1, c2 = st.columns(2)
                    with c1: fecha_ini = st.date_input("Fecha de inicio")
                    with c2: fecha_fin = st.date_input("Fecha de fin")
                    if st.form_submit_button("📤 Registrar solicitud", type="primary"):
                        if fecha_fin < fecha_ini:
                            st.error("La fecha de fin debe ser posterior a la de inicio.")
                        else:
                            _crear_vacaciones(sm, config, opciones_a[sel],
                                              sel.split(" – ")[-1],
                                              fecha_ini, fecha_fin, email_rrhh)

    # ── Vista EMPLEADO ───────────────────────────────────────────────────────
    else:
        tab1, tab2 = st.tabs(["➕ Solicitar vacaciones", "📋 Mis vacaciones"])

        emp_id = usuario["id_empleado"] if usuario else ""
        emp = sm.get_empleado(emp_id)

        with tab1:
            if not emp:
                st.warning("No se encontró tu perfil de empleado. Avisa a RRHH.")
            else:
                nombre_emp = str(emp.get("Nombre", emp_id))
                jefe = sm.get_email_jefe(emp_id)
                st.info(f"👤 Solicitud para: **{emp_id} – {nombre_emp}**")
                if jefe:
                    st.caption(f"Tu solicitud irá primero a tu jefe inmediato ({jefe}) "
                               "y después a RRHH.")
                else:
                    st.caption("No tienes jefe inmediato asignado: tu solicitud irá "
                               "directamente a RRHH.")

                with st.form("form_vac", clear_on_submit=True):
                    c1, c2 = st.columns(2)
                    with c1: fecha_ini = st.date_input("Fecha de inicio")
                    with c2: fecha_fin = st.date_input("Fecha de fin")
                    if st.form_submit_button("📤 Enviar solicitud", type="primary"):
                        if fecha_fin < fecha_ini:
                            st.error("La fecha de fin debe ser posterior a la de inicio.")
                        else:
                            _crear_vacaciones(sm, config, emp_id, nombre_emp,
                                              fecha_ini, fecha_fin, email_rrhh)

        with tab2:
            df_v = sm.get_vacaciones()
            if not df_v.empty and usuario:
                df_v = df_v[df_v["ID_Empleado"].astype(str).str.strip() == str(emp_id).strip()]
            if df_v.empty:
                st.info("No tienes solicitudes de vacaciones.")
            else:
                st.dataframe(_tabla_estado(df_v, "vacacion"),
                             use_container_width=True, hide_index=True)


def _crear_vacaciones(sm, config, emp_id, nombre_emp, fecha_ini, fecha_fin, email_rrhh):
    """Registra las vacaciones y notifica al jefe inmediato y a RRHH."""
    try:
        with st.spinner("Enviando…"):
            res = sm.solicitar_vacaciones(emp_id, fecha_ini.strftime("%Y-%m-%d"),
                                          fecha_fin.strftime("%Y-%m-%d"))
    except Exception as e:
        st.error(f"❌ No se pudo registrar la solicitud: {e}")
        return

    st.success(f"✅ Vacaciones **{res['id']}** enviadas — {etiqueta_estado(res['estado'])}")

    filas = [("Empleado", f"{emp_id} – {nombre_emp}"),
             ("Desde", fecha_ini.strftime("%d/%m/%Y")),
             ("Hasta", fecha_fin.strftime("%d/%m/%Y")),
             ("Días hábiles", res["dias"])]
    cuerpo = (f"<p><strong>{esc(nombre_emp)}</strong> ha solicitado vacaciones.</p>"
              + tabla_html(filas)
              + "<p style='margin-top:16px'>Ingresa al sistema para aprobar o rechazar "
                f"la solicitud (<strong>{esc(res['id'])}</strong>).</p>")
    enviados, fallidos = notificar([res["email_jefe"], email_rrhh],
                                   f"Nueva solicitud de vacaciones – {nombre_emp}", cuerpo)
    mostrar_envio(enviados, fallidos)
    if not res["email_jefe"]:
        st.caption("ℹ️ Este empleado no tiene jefe inmediato registrado en su ficha, "
                   "así que solo se notificó a RRHH.")


# ── Módulo: Horas Extras (con restricción por rol) ────────────────────────────
def _page_horas_extras_con_rol():
    sm = get_sm()
    usuario = get_usuario()
    admin = es_admin()
    st.title("⏰ Horas Extras" if admin else "⏰ Mis Horas Extra")
    tab1, tab2 = st.tabs(["➕ Registrar", "📋 Ver"])

    df_emp = sm.get_empleados()
    if admin:
        opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                    for _, r in df_emp.iterrows()} if not df_emp.empty else {}
    else:
        opciones = {}
        if not df_emp.empty and usuario:
            mask = df_emp["ID_Empleado"].astype(str) == usuario["id_empleado"]
            for _, r in df_emp[mask].iterrows():
                opciones[f"{r['ID_Empleado']} – {r['Nombre']}"] = str(r["ID_Empleado"])

    with tab1:
        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            with st.form("form_hex", clear_on_submit=True):
                if admin:
                    sel = st.selectbox("Empleado", list(opciones.keys()))
                    emp_id = opciones[sel]
                else:
                    emp_id = list(opciones.values())[0]
                    st.info(f"👤 Registrando para: **{list(opciones.keys())[0]}**")
                fecha_hex = st.date_input("Fecha", date.today())
                horas_hex = st.number_input("Horas extra", min_value=0.5, max_value=12.0, step=0.5, value=1.0)
                motivo_hex = st.text_area("Motivo / justificación *")
                if st.form_submit_button("💾 Registrar", type="primary"):
                    if not motivo_hex.strip():
                        st.error("El motivo es obligatorio.")
                    else:
                        with st.spinner("Registrando…"):
                            sm.registrar_horas_extra(emp_id, fecha_hex.strftime("%Y-%m-%d"), horas_hex, motivo_hex)
                        st.success("✅ Horas extra registradas para aprobación")

    with tab2:
        df_he = sm.get_horas_extras()
        if not admin and usuario and not df_he.empty:
            df_he = df_he[df_he["ID_Empleado"].astype(str) == usuario["id_empleado"]]
        if df_he.empty:
            st.info("No hay registros de horas extra.")
        else:
            filtro_he = st.selectbox("Filtrar", ["Todos", "Pendiente", "Aprobado"])
            df_hef = df_he if filtro_he == "Todos" else df_he[df_he["Estado"].astype(str) == filtro_he]
            st.dataframe(df_hef, use_container_width=True, hide_index=True)
            if admin:
                st.subheader("✅ Aprobar horas extra")
                pend_he = df_he[df_he["Estado"].astype(str) == "Pendiente"]
                if not pend_he.empty:
                    hex_sel = st.selectbox("Registro a aprobar",
                        pend_he.apply(lambda r: f"{r['ID']} – {r['ID_Empleado']} – {r['Fecha']} ({r['Horas_Extra']}h)", axis=1).tolist())
                    hex_id = hex_sel.split(" – ")[0]
                    aprobador_he = st.text_input("Aprobado por")
                    if st.button("✅ Aprobar", type="primary"):
                        if aprobador_he.strip():
                            sm.aprobar_hora_extra(hex_id, aprobador_he)
                            st.success(f"✅ Horas extra **{hex_id}** aprobadas")
                            st.rerun()
                        else:
                            st.error("Ingresa quién aprueba.")


# ── Módulo: Expediente del Empleado (solo admin) ─────────────────────────────
def page_expediente_empleado():
    sm = get_sm()
    st.title("📁 Expediente del Empleado")

    df_emp = sm.get_empleados()
    if df_emp.empty:
        st.warning("No hay empleados registrados.")
        return

    opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                for _, r in df_emp.iterrows()}
    sel = st.selectbox("Seleccionar empleado", list(opciones.keys()))
    emp_id = opciones[sel]
    emp = df_emp[df_emp["ID_Empleado"].astype(str) == emp_id].iloc[0]

    # ── Cabecera del expediente ──
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#1A3A5C 0%,#00A99D 100%);
                color:white;padding:28px 32px;border-radius:12px;margin-bottom:24px">
      <div style="font-size:0.8rem;opacity:0.7;text-transform:uppercase;letter-spacing:1px;
                  margin-bottom:4px">Expediente Oficial – RRHH Quski</div>
      <h2 style="margin:0;color:white;font-size:1.8rem">{emp['Nombre']}</h2>
      <div style="margin-top:12px;display:flex;gap:32px;flex-wrap:wrap;opacity:0.9;font-size:0.9rem">
        <span>🆔 ID: <strong>{emp_id}</strong></span>
        <span>🏢 Área: <strong>{emp.get('Area','–')}</strong></span>
        <span>📧 <strong>{emp.get('Email','–')}</strong></span>
        <span>👔 Jefe: <strong>{emp.get('Email_Jefe','–')}</strong></span>
        <span>⏰ Horario: <strong>{emp.get('Horario_Inicio','–')} – {emp.get('Horario_Fin','–')}</strong></span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Cargar datos del empleado ──
    df_asis = sm.get_df("Asistencia")
    df_perm = sm.get_permisos()
    df_vac  = sm.get_vacaciones()
    df_hex  = sm.get_horas_extras()
    df_lam  = sm.get_llamados_atencion()

    asis_emp = df_asis[df_asis["ID_Empleado"].astype(str) == emp_id].copy() if not df_asis.empty else pd.DataFrame()
    perm_emp = df_perm[df_perm["ID_Empleado"].astype(str) == emp_id].copy() if not df_perm.empty else pd.DataFrame()
    vac_emp  = df_vac[df_vac["ID_Empleado"].astype(str) == emp_id].copy()   if not df_vac.empty  else pd.DataFrame()
    hex_emp  = df_hex[df_hex["ID_Empleado"].astype(str) == emp_id].copy()   if not df_hex.empty  else pd.DataFrame()
    lam_emp  = df_lam[df_lam["ID_Empleado"].astype(str) == emp_id].copy()   if not df_lam.empty  else pd.DataFrame()

    total_asis  = len(asis_emp)
    total_tard  = len(asis_emp[asis_emp["Estado"].astype(str) == "Tardanza"]) if not asis_emp.empty else 0
    total_aus   = len(asis_emp[asis_emp["Estado"].astype(str) == "Ausente"])  if not asis_emp.empty else 0
    total_perm  = len(perm_emp)
    total_vac_d = int(pd.to_numeric(vac_emp["Dias_Habiles"], errors="coerce").fillna(0).sum()) if not vac_emp.empty else 0
    total_hex_h = pd.to_numeric(hex_emp["Horas_Extra"], errors="coerce").fillna(0).sum() if not hex_emp.empty else 0
    total_lam   = len(lam_emp)

    # KPIs
    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    with c1: metric_card("Asistencias", total_asis, "📅")
    with c2: metric_card("Tardanzas", total_tard, "⚠️")
    with c3: metric_card("Ausencias", total_aus, "❌")
    with c4: metric_card("Permisos", total_perm, "📋")
    with c5: metric_card("Días vac.", total_vac_d, "🏖️")
    with c6: metric_card("Hrs extra", f"{total_hex_h:.0f}", "⏰")
    with c7: metric_card("Llamados", total_lam, "📁")

    st.divider()

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["📅 Asistencia", "📋 Permisos", "🏖️ Vacaciones", "⏰ Horas Extra", "⚠️ Llamados de Atención"]
    )

    with tab1:
        if asis_emp.empty:
            st.info("Sin registros de asistencia.")
        else:
            st.dataframe(asis_emp, use_container_width=True, hide_index=True)
            r1, r2, r3 = st.columns(3)
            pct_tard = round(total_tard / total_asis * 100, 1) if total_asis else 0
            with r1: st.metric("% Puntualidad", f"{100 - pct_tard:.1f}%")
            with r2: st.metric("% Tardanza",    f"{pct_tard:.1f}%")
            with r3: st.metric("Total minutos atraso",
                                int(pd.to_numeric(asis_emp["Minutos_Atraso"], errors="coerce").fillna(0).sum()))

    with tab2:
        if perm_emp.empty:
            st.info("Sin permisos solicitados.")
        else:
            st.dataframe(perm_emp, use_container_width=True, hide_index=True)
            h_total = pd.to_numeric(perm_emp["Horas_Solicitadas"], errors="coerce").fillna(0).sum()
            st.caption(f"Total horas de permiso solicitadas: **{h_total:.1f}h**")

    with tab3:
        if vac_emp.empty:
            st.info("Sin vacaciones solicitadas.")
        else:
            st.dataframe(vac_emp, use_container_width=True, hide_index=True)
            st.caption(f"Total días hábiles de vacaciones: **{total_vac_d}**")

    with tab4:
        if hex_emp.empty:
            st.info("Sin horas extra registradas.")
        else:
            st.dataframe(hex_emp, use_container_width=True, hide_index=True)
            st.caption(f"Total horas extra trabajadas: **{total_hex_h:.1f}h**")

    with tab5:
        if lam_emp.empty:
            st.success("✅ Sin llamados de atención en el expediente.")
        else:
            verbales    = len(lam_emp[lam_emp["Tipo"].astype(str) == "Verbal"])
            escritos    = len(lam_emp[lam_emp["Tipo"].astype(str) == "Escrito"])
            suspensions = len(lam_emp[lam_emp["Tipo"].astype(str) == "Suspensión"])
            l1, l2, l3 = st.columns(3)
            with l1: metric_card("Verbales", verbales, "🗣️")
            with l2: metric_card("Escritos", escritos, "📝")
            with l3: metric_card("Suspensiones", suspensions, "🚫")
            st.dataframe(
                lam_emp[["ID_Llamado", "Fecha", "Tipo", "Motivo", "Atrasos_Acumulados", "Registrado_Por", "Estado"]],
                use_container_width=True, hide_index=True
            )

    # ── Exportar expediente como HTML ──
    st.divider()
    if st.button("📄 Exportar expediente (HTML para imprimir)", use_container_width=True):
        filas_lam = ""
        if not lam_emp.empty:
            for _, r in lam_emp.iterrows():
                filas_lam += f"<tr><td>{r['ID_Llamado']}</td><td>{r['Fecha']}</td><td>{r['Tipo']}</td><td>{r['Motivo']}</td><td>{r['Atrasos_Acumulados']}</td><td>{r['Registrado_Por']}</td><td>{r['Estado']}</td></tr>"
        else:
            filas_lam = "<tr><td colspan='7' style='text-align:center;color:#888'>Sin llamados de atención</td></tr>"

        filas_asis = ""
        if not asis_emp.empty:
            for _, r in asis_emp.iterrows():
                filas_asis += f"<tr><td>{r['Fecha']}</td><td>{r['Hora_Entrada']}</td><td>{r['Hora_Salida']}</td><td>{r['Estado']}</td><td>{r.get('Minutos_Atraso','')}</td><td>{r.get('Observaciones','')}</td></tr>"

        html_exp = f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
        <title>Expediente {emp['Nombre']}</title>
        <style>
          body{{font-family:Arial,sans-serif;margin:40px;color:#1a1a2e}}
          .header{{background:#1A3A5C;color:white;padding:24px;border-radius:8px;margin-bottom:24px}}
          .header h1{{margin:0;font-size:1.6rem}}
          .header p{{margin:8px 0 0;opacity:0.85;font-size:0.9rem}}
          .kpis{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
          .kpi{{background:#E8F4F8;border-left:4px solid #00A99D;padding:12px 20px;border-radius:6px;min-width:100px}}
          .kpi .val{{font-size:1.8rem;font-weight:700;color:#1A3A5C;margin:0}}
          .kpi .lbl{{font-size:0.75rem;color:#6B7280;margin:0}}
          h2{{color:#1A3A5C;border-bottom:2px solid #00A99D;padding-bottom:6px;margin-top:28px}}
          table{{border-collapse:collapse;width:100%;margin-top:8px;font-size:0.85rem}}
          th{{background:#1A3A5C;color:white;padding:8px 12px;text-align:left}}
          td{{padding:7px 12px;border-bottom:1px solid #eee}}
          tr:nth-child(even){{background:#f9f9f9}}
          .footer{{margin-top:32px;font-size:0.75rem;color:#999;text-align:center;border-top:1px solid #eee;padding-top:12px}}
          @media print{{body{{margin:20px}}}}
        </style></head><body>
        <div class="header">
          <h1>📁 Expediente Oficial – {emp['Nombre']}</h1>
          <p>ID: {emp_id} &nbsp;|&nbsp; Área: {emp.get('Area','–')} &nbsp;|&nbsp;
             Email: {emp.get('Email','–')} &nbsp;|&nbsp;
             Horario: {emp.get('Horario_Inicio','–')} – {emp.get('Horario_Fin','–')} &nbsp;|&nbsp;
             Generado: {date.today().strftime('%d/%m/%Y')}</p>
        </div>
        <div class="kpis">
          <div class="kpi"><p class="val">{total_asis}</p><p class="lbl">Asistencias</p></div>
          <div class="kpi"><p class="val">{total_tard}</p><p class="lbl">Tardanzas</p></div>
          <div class="kpi"><p class="val">{total_aus}</p><p class="lbl">Ausencias</p></div>
          <div class="kpi"><p class="val">{total_perm}</p><p class="lbl">Permisos</p></div>
          <div class="kpi"><p class="val">{total_vac_d}</p><p class="lbl">Días vacac.</p></div>
          <div class="kpi"><p class="val">{total_hex_h:.0f}h</p><p class="lbl">Horas extra</p></div>
          <div class="kpi"><p class="val">{total_lam}</p><p class="lbl">Llamados</p></div>
        </div>
        <h2>⚠️ Llamados de Atención</h2>
        <table><tr><th>ID</th><th>Fecha</th><th>Tipo</th><th>Motivo</th><th>Tardanzas</th><th>Emitido por</th><th>Estado</th></tr>
        {filas_lam}</table>
        <h2>📅 Historial de Asistencia</h2>
        <table><tr><th>Fecha</th><th>Entrada</th><th>Salida</th><th>Estado</th><th>Min. atraso</th><th>Observaciones</th></tr>
        {filas_asis}</table>
        <div class="footer">Quski – Sistema de Asistencia RRHH &nbsp;|&nbsp; Documento generado el {date.today().strftime('%d/%m/%Y')}</div>
        </body></html>"""

        st.download_button(
            label="⬇️ Descargar expediente",
            data=html_exp.encode("utf-8"),
            file_name=f"expediente_{emp_id}_{emp['Nombre'].replace(' ','_')}_{date.today().strftime('%Y%m%d')}.html",
            mime="text/html",
            use_container_width=True,
        )


# ── Módulo: Llamados de Atención (solo admin) ─────────────────────────────────
def page_llamados_atencion():
    sm = get_sm()
    st.title("⚠️ Llamados de Atención")

    tab1, tab2, tab3 = st.tabs(["📊 Monitor de Atrasos", "➕ Emitir Llamado", "📋 Historial"])

    mes_actual = date.today().strftime("%Y-%m")
    df_emp  = sm.get_empleados()
    df_asis = sm.get_df("Asistencia")
    df_llamados = sm.get_llamados_atencion()

    with tab1:
        st.subheader(f"Tardanzas del mes – {date.today().strftime('%B %Y')}")
        umbral = st.number_input("Umbral de alerta (número de tardanzas)", min_value=1, max_value=20, value=3,
                                  help="Empleados con igual o más tardanzas que este número serán marcados en rojo")

        if df_asis.empty or df_emp.empty:
            st.info("Sin datos de asistencia registrados.")
        else:
            asis_mes = df_asis[df_asis["Fecha"].astype(str).str.startswith(mes_actual)]
            tardanzas = asis_mes[asis_mes["Estado"].astype(str) == "Tardanza"].copy()

            if tardanzas.empty:
                st.success("✅ Sin tardanzas registradas este mes.")
            else:
                resumen = tardanzas.groupby("ID_Empleado").agg(
                    Tardanzas=("Estado", "count"),
                    Minutos_Total=("Minutos_Atraso", lambda x: pd.to_numeric(x, errors="coerce").fillna(0).sum())
                ).reset_index()
                resumen["Minutos_Total"] = resumen["Minutos_Total"].astype(int)
                emp_merge_cols = [c for c in ["ID_Empleado", "Nombre", "Email"] if c in df_emp.columns]
                resumen = resumen.merge(df_emp[emp_merge_cols], on="ID_Empleado", how="left")
                resumen["Estado"] = resumen["Tardanzas"].apply(
                    lambda x: "🚨 Supera umbral" if x >= umbral else "✅ Normal")
                resumen = resumen.sort_values("Tardanzas", ascending=False)

                st.dataframe(resumen[["ID_Empleado", "Nombre", "Tardanzas", "Minutos_Total", "Estado"]],
                             use_container_width=True, hide_index=True)

                alertas = resumen[resumen["Tardanzas"] >= umbral]
                if not alertas.empty:
                    st.warning(f"⚠️ **{len(alertas)}** empleado(s) superan el umbral de {umbral} tardanzas.")
                    st.caption("Puedes emitir un llamado de atención desde la pestaña ➕ Emitir Llamado.")

    with tab2:
        st.subheader("Emitir Llamado de Atención")
        config_la = st.session_state.config
        t_verbal     = int(float(config_la.get("Tardanzas_Llamado_Verbal", 3)))
        t_escrito    = int(float(config_la.get("Tardanzas_Llamado_Escrito", 5)))
        t_suspension = int(float(config_la.get("Tardanzas_Suspension", 8)))
        st.info(f"Política: **Verbal** ≥{t_verbal} tard. · **Escrito** ≥{t_escrito} tard. · **Suspensión** ≥{t_suspension} tard.")

        if df_emp.empty:
            st.warning("No hay empleados registrados.")
        else:
            opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                        for _, r in df_emp.iterrows()}
            sel_la = st.selectbox("Empleado", list(opciones.keys()), key="sel_llamado_emp")
            emp_id_la = opciones[sel_la]
            atrasos_mes = sm.get_tardanzas_mes(emp_id_la, mes_actual)

            if atrasos_mes >= t_suspension:
                tipo_sug = "Suspensión"; ico = "🚫"
            elif atrasos_mes >= t_escrito:
                tipo_sug = "Escrito"; ico = "📝"
            elif atrasos_mes >= t_verbal:
                tipo_sug = "Verbal"; ico = "🗣️"
            else:
                tipo_sug = "Verbal"; ico = "ℹ️"

            if atrasos_mes >= t_verbal:
                st.warning(f"{ico} **{atrasos_mes} tardanzas** este mes → se sugiere llamado **{tipo_sug}**")
            else:
                st.caption(f"Tardanzas este mes: **{atrasos_mes}** (por debajo del umbral de {t_verbal})")

            with st.form("form_llamado", clear_on_submit=True):
                emp_id = emp_id_la
                idx_tipo = ["Verbal", "Escrito", "Suspensión"].index(tipo_sug)
                tipo = st.selectbox("Tipo de llamado", ["Verbal", "Escrito", "Suspensión"],
                                    index=idx_tipo,
                                    help="Verbal: primer aviso oral | Escrito: queda en el expediente | Suspensión: sin goce de sueldo")
                motivo = st.text_area("Motivo del llamado de atención *",
                                      placeholder="Ej: Acumulación de tardanzas reiteradas durante el mes")
                registrado_por = st.text_input("Emitido por (nombre o cargo) *",
                                               placeholder="Ej: Karina Bastidas – Jefe RRHH")

                if st.form_submit_button("📋 Emitir llamado de atención", type="primary"):
                    if not motivo.strip():
                        st.error("El motivo es obligatorio.")
                    elif not registrado_por.strip():
                        st.error("Ingresa quién emite el llamado.")
                    else:
                        with st.spinner("Registrando…"):
                            emp_row = df_emp[df_emp["ID_Empleado"].astype(str) == emp_id]
                            nombre_emp = emp_row.iloc[0]["Nombre"] if not emp_row.empty else emp_id
                            email_emp  = emp_row.iloc[0].get("Email", "") if not emp_row.empty else ""
                            llamado_id = sm.registrar_llamado_atencion(
                                emp_id, nombre_emp, tipo, motivo, atrasos_mes, registrado_por
                            )
                        st.success(f"✅ Llamado **{llamado_id}** ({tipo}) emitido para **{nombre_emp}**")

                        # Notificación al empleado, a su jefe inmediato y a RRHH
                        email_jefe = sm.get_email_jefe(emp_id)
                        email_rrhh = st.session_state.config.get("Email_RRHH", "")
                        detalle_la = [("Empleado", f"{emp_id} – {nombre_emp}"),
                                      ("Tipo de llamado", tipo),
                                      ("Motivo", motivo),
                                      ("Tardanzas acumuladas en el mes", atrasos_mes),
                                      ("Emitido por", registrado_por),
                                      ("Fecha", date.today().strftime("%d/%m/%Y"))]

                        cuerpo_emp = (
                            f"<p>Estimado/a <strong>{esc(nombre_emp)}</strong>,</p>"
                            f"<p>Se ha emitido un <strong style='color:#991B1B'>"
                            f"Llamado de Atención {esc(tipo)}</strong> "
                            f"(referencia <strong>{esc(llamado_id)}</strong>).</p>"
                            + tabla_html(detalle_la)
                            + "<p style='margin-top:16px'>Para más información, "
                              "comuníquese con el área de RRHH.</p>")
                        env_e, fall_e = notificar([email_emp],
                                                  f"Llamado de Atención – {tipo}", cuerpo_emp)

                        cuerpo_jefe = (
                            f"<p>Se registró un <strong style='color:#991B1B'>"
                            f"Llamado de Atención {esc(tipo)}</strong> para "
                            f"<strong>{esc(nombre_emp)}</strong>, integrante de tu equipo.</p>"
                            + tabla_html(detalle_la)
                            + "<p style='margin-top:16px'>Esta notificación es informativa; "
                              "el expediente queda registrado en el sistema.</p>")
                        env_j, fall_j = notificar([email_jefe, email_rrhh],
                                                  f"Llamado de Atención registrado – {nombre_emp}",
                                                  cuerpo_jefe)

                        mostrar_envio(env_e + env_j, fall_e + fall_j)
                        if not email_emp:
                            st.caption("ℹ️ El empleado no tiene correo en su ficha; "
                                       "notifícale por otro medio.")
                        if not email_jefe:
                            st.caption("ℹ️ Este empleado no tiene jefe inmediato registrado "
                                       "en su ficha, así que no se notificó a ningún jefe.")

    with tab3:
        st.subheader("Historial de Llamados de Atención")
        if df_llamados.empty:
            st.info("No hay llamados de atención registrados.")
        else:
            c1, c2 = st.columns(2)
            with c1:
                opciones_f = ["Todos"] + [f"{r['ID_Empleado']} – {r['Nombre']}"
                                           for _, r in df_emp.iterrows()] if not df_emp.empty else ["Todos"]
                filtro_emp = st.selectbox("Filtrar por empleado", opciones_f)
            with c2:
                filtro_tipo = st.selectbox("Filtrar por tipo", ["Todos", "Verbal", "Escrito", "Suspensión"])

            df_lf = df_llamados.copy()
            if filtro_emp != "Todos":
                eid = filtro_emp.split(" – ")[0]
                df_lf = df_lf[df_lf["ID_Empleado"].astype(str) == eid]
            if filtro_tipo != "Todos":
                df_lf = df_lf[df_lf["Tipo"].astype(str) == filtro_tipo]

            st.dataframe(df_lf, use_container_width=True, hide_index=True)
            st.caption(f"{len(df_lf)} llamado(s) encontrado(s)")

            # Resumen por empleado
            if len(df_llamados) > 0:
                st.divider()
                st.subheader("Resumen por empleado")
                resumen_l = df_llamados.groupby(["ID_Empleado", "Nombre"])["Tipo"].value_counts().unstack(fill_value=0)
                st.dataframe(resumen_l, use_container_width=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_session()

    if get_sm() is None:
        pantalla_login()
        return

    if get_usuario() is None:
        pantalla_login_empleado()
        return

    page = sidebar()

    if es_admin():
        pages = {
            "Dashboard":         page_dashboard,
            "Empleados":         page_empleados,
            "Asistencia":        page_asistencia,
            "Permisos":          _page_permisos_con_rol,
            "Vacaciones":        _page_vacaciones_con_rol,
            "Horas Extras":      _page_horas_extras_con_rol,
            "Configuración":     page_configuracion,
            "Gestión Usuarios":     page_gestion_usuarios,
            "Llamados de Atención": page_llamados_atencion,
            "Expediente":           page_expediente_empleado,
        }
    else:
        pages = {
            "Mi Asistencia":     page_asistencia,
            "Aprobaciones":      page_aprobaciones_jefe,
            "Mis Permisos":      _page_permisos_con_rol,
            "Mis Vacaciones":    _page_vacaciones_con_rol,
            "Mis Horas Extra":   _page_horas_extras_con_rol,
            "Cambiar Contraseña": page_cambiar_password,
        }

    fn = pages.get(page)
    if fn:
        fn()
    else:
        st.error(f"Módulo '{page}' no encontrado.")


if __name__ == "__main__":
    main()
