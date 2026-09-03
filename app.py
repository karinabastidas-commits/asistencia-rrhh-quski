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
    SheetsManager, CONFIG_DEFAULTS, ahora_local, hoy_local,
    es_error_transitorio, leer_formulario_kye,
    EST_PEND_JEFE, EST_PEND_RRHH, EST_APROBADO, EST_RECHAZADO, TIPO_TARDANZA,
    CAUSALES_LLAMADO, GRAVEDAD_CAUSAL, TIPOS_RECONOCIMIENTO,
    CATEGORIAS_DOCUMENTO, TIPOS_DOC_KYE, ESTADOS_CURSO,
    FINANCIAMIENTO_CURSO, NIVELES_TITULO,
    password_debil,
)

# ── Seguridad: control de intentos de inicio de sesión ────────────────────────
MAX_INTENTOS_LOGIN = 5          # intentos fallidos antes de bloquear
BLOQUEO_SEGUNDOS   = 300        # 5 minutos de espera tras agotarlos


def leer_secrets(seccion: str):
    """Devuelve una sección de los Secrets de Streamlit, o None si no existe.

    Hay que envolverlo: cuando no hay ningún archivo secrets.toml —el caso
    normal al ejecutar la app en un equipo local— `st.secrets` no se comporta
    como un diccionario vacío, sino que lanza StreamlitSecretNotFoundError.
    Sin esto, la app se cae en lugar de avisar que falta la configuración.
    """
    try:
        if seccion in st.secrets:
            return st.secrets[seccion]
    except Exception:
        return None
    return None


def notificar_salida_anticipada(sm, config, emp_id, nombre, ev, obs=""):
    """Avisa al jefe inmediato y a RRHH cuando alguien sale antes de su horario.

    Salir DESPUÉS del horario no dispara nada: no es atraso ni hora extra, solo
    queda la hora registrada.
    """
    filas = [("Empleado", f"{emp_id} – {nombre}"),
             ("Fecha", hoy().strftime("%d/%m/%Y")),
             ("Hora de salida", ev["hora_salida"]),
             ("Horario de salida establecido", ev["horario_fin"]),
             ("Minutos antes de la hora", ev["minutos_antes"])]
    if obs:
        filas.append(("Observaciones del empleado", obs))
    cuerpo = (f"<p><strong>{esc(nombre)}</strong> registró su salida "
              f"<strong>{ev['minutos_antes']} minuto(s) antes</strong> del horario "
              f"establecido.</p>" + tabla_html(filas) +
              "<p style='margin-top:16px;color:#6B7280;font-size:13px'>Aviso automático. "
              "Salir después del horario no genera ningún aviso.</p>")
    enviados, fallidos = notificar(
        [sm.get_email_jefe(emp_id), config.get("Email_RRHH", "")],
        f"Salida anticipada – {nombre} ({hoy().strftime('%d/%m/%Y')})", cuerpo)
    if enviados:
        flash("info", "📧 Salida anticipada notificada a: " + ", ".join(enviados))
    if fallidos:
        flash("warning", "⚠️ No se pudo avisar de la salida anticipada a: " + ", ".join(fallidos))


def revisar_salidas_pendientes(sm, config, dias: int = 7, mostrar: bool = False) -> int:
    """Busca jornadas ya cerradas donde nadie marcó la salida y avisa al jefe.

    Solo revisa días anteriores a hoy —la jornada en curso todavía puede
    cerrarse— y marca cada registro como avisado para no repetir el correo.
    Agrupa por empleado: si alguien tiene tres días sin marcar, recibe un solo
    correo con los tres.
    """
    try:
        pendientes = sm.salidas_pendientes(dias, config)
    except Exception as e:
        if mostrar:
            st.error(f"No se pudo revisar las salidas pendientes: {e}")
        return 0
    if not pendientes:
        if mostrar:
            st.success("✅ No hay jornadas sin salida marcada en los últimos "
                       f"{dias} días.")
        return 0

    por_empleado = {}
    for p in pendientes:
        por_empleado.setdefault(p["id_empleado"], []).append(p)

    avisados = 0
    for emp_id, jornadas in por_empleado.items():
        nombre = jornadas[0]["nombre"] or emp_id
        filas = [("Empleado", f"{emp_id} – {nombre}"),
                 ("Jornadas sin salida", len(jornadas))]
        for j in sorted(jornadas, key=lambda x: x["fecha"]):
            filas.append((j["fecha"].strftime("%d/%m/%Y"),
                          f"entrada {j['hora_entrada']} · sin salida"))
        cuerpo = (f"<p><strong>{esc(nombre)}</strong> tiene "
                  f"<strong>{len(jornadas)} jornada(s)</strong> sin registrar la hora "
                  f"de salida.</p>" + tabla_html(filas) +
                  "<p style='margin-top:16px'>Conviene confirmar con la persona a qué "
                  "hora salió y que RRHH complete el registro en el sistema.</p>")
        enviados, fallidos = notificar(
            [sm.get_email_jefe(emp_id), config.get("Email_RRHH", "")],
            f"Salida sin marcar – {nombre}", cuerpo)
        if enviados:
            avisados += len(jornadas)
            sello = f"Avisado {ahora().strftime('%Y-%m-%d %H:%M')}"
            for j in jornadas:
                try:
                    sm.marcar_aviso_salida(j["fila"], sello)
                except Exception:
                    pass
        if mostrar:
            if enviados:
                st.info(f"📧 {nombre}: {len(jornadas)} jornada(s) — avisado a "
                        + ", ".join(enviados))
            if fallidos:
                st.warning(f"⚠️ {nombre}: no se pudo avisar a " + ", ".join(fallidos))
    return avisados


def notificar_tardanza(sm, config, emp_id, nombre, momento, atraso):
    """Deja constancia del atraso y avisa al jefe inmediato, a RRHH y al empleado.

    Se dispara en CADA tardanza, llegue o no al umbral de un llamado formal, de
    modo que el atraso quede documentado el mismo día en que ocurre.
    """
    mes = momento.strftime("%Y-%m")
    try:
        atrasos_mes = sm.get_tardanzas_mes(emp_id, mes)
    except Exception:
        atrasos_mes = 0
    # Google Sheets tarda un instante en reflejar la fila recién escrita, así
    # que el conteo puede volver sin incluir este mismo atraso: nunca puede ser
    # menos de 1, porque lo acabamos de registrar.
    atrasos_mes = max(1, atrasos_mes)

    llamado_id = ""
    try:
        llamado_id = sm.registrar_tardanza(emp_id, nombre, momento.strftime("%H:%M"),
                                           int(atraso), atrasos_mes)
    except Exception as e:
        flash("warning", f"El atraso quedó en Asistencia pero no se pudo dejar "
                         f"constancia en Llamados de Atención ({e}).")

    # ¿Este atraso alcanza algún umbral de la política disciplinaria?
    alerta = ""
    for umbral, etiqueta in ((_cfg_int(config, "Tardanzas_Suspension", 8), "SUSPENSIÓN"),
                             (_cfg_int(config, "Tardanzas_Llamado_Escrito", 5), "llamado ESCRITO"),
                             (_cfg_int(config, "Tardanzas_Llamado_Verbal", 3), "llamado VERBAL")):
        if atrasos_mes >= umbral:
            alerta = (f"Acumula {atrasos_mes} atraso(s) este mes y alcanza el "
                      f"umbral de {etiqueta}.")
            break

    filas = [("Empleado", f"{emp_id} – {nombre}"),
             ("Fecha", momento.strftime("%d/%m/%Y")),
             ("Hora de entrada", momento.strftime("%H:%M")),
             ("Horario establecido", config.get("Horario_Inicio", "09:00")),
             ("Minutos de atraso", int(atraso)),
             ("Atrasos acumulados en el mes", atrasos_mes)]
    if llamado_id:
        filas.append(("Nº de registro", llamado_id))

    cuerpo = (f"<p>Se registró un <strong>atraso</strong> de "
              f"<strong>{esc(nombre)}</strong>.</p>" + tabla_html(filas))
    if alerta:
        cuerpo += (f"<p style='margin-top:16px;padding:10px;background:#FEF3C7;"
                   f"border-left:4px solid #D97706'><strong>⚠️ {esc(alerta)}</strong></p>")
    cuerpo += ("<p style='margin-top:16px;color:#6B7280;font-size:13px'>Registro "
               "informativo automático. Los llamados de atención formales los emite "
               "RRHH desde el sistema.</p>")

    enviados, fallidos = notificar(
        [sm.get_email_jefe(emp_id), config.get("Email_RRHH", ""),
         sm.get_email_empleado(emp_id)],
        f"Atraso registrado – {nombre} ({momento.strftime('%d/%m/%Y')})", cuerpo)
    if enviados:
        flash("info", "📧 Atraso notificado a: " + ", ".join(enviados))
    if fallidos:
        flash("warning", "⚠️ No se pudo avisar del atraso a: " + ", ".join(fallidos))
    if alerta:
        flash("warning", "⚠️ " + alerta)


def ventana_registro_entrada(config):
    """Rango horario en el que tiene sentido registrar una entrada.

    Va desde unas horas antes del horario de inicio hasta el horario de salida.
    Fuera de ese rango NO se marca entrada automática: quien abre el sistema a
    las nueve de la noche para revisar sus vacaciones o marcar su salida no
    está llegando al trabajo, y registrarle una entrada con 700 minutos de
    atraso inventa un dato falso en el expediente.
    """
    margen = _cfg_int(config, "Margen_Registro_Entrada_Horas", 3)
    try:
        ini = datetime.strptime(str(config.get("Horario_Inicio", "09:00"))[:5], "%H:%M")
        fin = datetime.strptime(str(config.get("Horario_Fin", "17:30"))[:5], "%H:%M")
    except ValueError:
        return None, None
    return (ini - timedelta(hours=margen)).time(), fin.time()


def dentro_de_ventana_entrada(momento, config) -> bool:
    desde, hasta = ventana_registro_entrada(config)
    if desde is None:
        return True
    return desde <= momento.time() <= hasta


def registrar_entrada_automatica():
    """Marca la entrada del empleado al iniciar sesión, una sola vez al día.

    Así nadie tiene que acordarse de pulsar un botón: con abrir el aplicativo
    su asistencia queda registrada con la hora real de Ecuador. Si ya marcó
    hoy no hace nada, así que volver a entrar más tarde no duplica el registro
    ni cambia la hora original. No aplica a la cuenta de administrador.
    """
    if st.session_state.get("entrada_auto_hecha"):
        return
    st.session_state.entrada_auto_hecha = True   # se marca aunque falle, para no reintentar en bucle

    usuario = get_usuario()
    if not usuario or es_admin():
        return
    sm = get_sm()
    if sm is None:
        return

    emp_id = str(usuario.get("id_empleado", "")).strip()
    emp = sm.get_empleado(emp_id)
    if not emp:
        return   # sin ficha de empleado no hay a quién registrarle la asistencia

    fecha_hoy = hoy().strftime("%Y-%m-%d")
    momento = ahora()
    config = st.session_state.config

    # Fuera del horario laboral no se inventa una entrada.
    if not dentro_de_ventana_entrada(momento, config):
        try:
            ya = sm.ya_registro_entrada(emp_id, fecha_hoy)
        except Exception:
            ya = True
        if not ya:
            desde, hasta = ventana_registro_entrada(config)
            flash("info", f"ℹ️ No se registró entrada automática porque son las "
                          f"{momento.strftime('%H:%M')}, fuera del horario laboral "
                          f"({desde.strftime('%H:%M')}–{hasta.strftime('%H:%M')}). "
                          "Si hoy trabajaste y falta tu marca, pídele a RRHH que la registre.")
        return

    try:
        if sm.ya_registro_entrada(emp_id, fecha_hoy):
            return
        estado, atraso = sm.registrar_entrada(
            emp_id, str(emp.get("Nombre", emp_id)),
            momento.strftime("%H:%M"), config)
        if estado == "Tardanza":
            flash("warning", f"⏰ Tu entrada quedó registrada a las "
                             f"{momento.strftime('%H:%M')} — {atraso} minuto(s) de atraso.")
            notificar_tardanza(sm, st.session_state.config, emp_id,
                               str(emp.get("Nombre", emp_id)), momento, atraso)
        else:
            flash("success", f"✅ Tu entrada quedó registrada a las "
                             f"{momento.strftime('%H:%M')}. ¡Buen día!")
    except Exception as e:
        flash("warning", f"No se pudo registrar tu entrada automáticamente ({e}). "
                         "Márcala desde el módulo de Asistencia.")


def secciones(etiquetas, key: str) -> str:
    """Barra de secciones que conserva la elegida al recargar la página.

    st.tabs siempre vuelve a la primera pestaña después de un st.rerun(), que
    es lo que hacía saltar de Vacaciones a Permisos al aprobar una solicitud.
    """
    return st.radio("Sección", etiquetas, key=key, horizontal=True,
                    label_visibility="collapsed")


def flash(tipo: str, mensaje: str):
    """Guarda un mensaje para mostrarlo DESPUÉS del st.rerun().

    st.success() seguido de st.rerun() no se alcanza a leer: la recarga borra
    la pantalla de inmediato y da la sensación de que el sistema "se salió".
    """
    st.session_state.setdefault("flash", []).append((tipo, mensaje))


def mostrar_flash():
    """Pinta arriba de la página los mensajes dejados antes de la recarga."""
    for tipo, mensaje in st.session_state.pop("flash", []):
        getattr(st, tipo, st.info)(mensaje)


def aviso_sin_perfil(sm, emp_id):
    """Explica por qué no se encontró el perfil, sin confundir un fallo de
    lectura de Google Sheets con una ficha inexistente."""
    detalle = getattr(sm, "ultimo_error", None)
    if detalle:
        st.error("❌ No se pudo leer la hoja de Empleados en este momento. "
                 "Tu perfil sí puede existir.")
        if "429" in detalle or "quota" in detalle.lower() or "Quota" in detalle:
            st.info("Se superó el límite de consultas por minuto de Google Sheets. "
                    "Espera un minuto y vuelve a cargar la página.")
        with st.expander("🔍 Detalle técnico"):
            st.code(detalle, language=None)
    else:
        st.warning(f"No se encontró tu ficha de empleado con el ID **{emp_id}**.")
        st.caption("Avisa a RRHH: tu ID de usuario debe coincidir exactamente con "
                   "el ID_Empleado de tu fila en la hoja Empleados.")


def ahora():
    """Hora actual en Ecuador (o la zona configurada).

    El servidor de Streamlit Cloud corre en UTC: usar ahora() marcaba
    las entradas cinco horas adelantadas."""
    return ahora_local(st.session_state.get("config") or {})


def hoy():
    """Fecha de hoy en Ecuador (o la zona configurada)."""
    return hoy_local(st.session_state.get("config") or {})

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

    cfg = leer_secrets("email")
    if cfg is None:
        return _fail("❌ No hay configuración de correo. Falta la sección [email] "
                     "en los Secrets de Streamlit (o el archivo .streamlit/secrets.toml "
                     "si estás ejecutando la app en tu equipo).")

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
            cuenta = leer_secrets("gcp_service_account")
            if cuenta is not None:
                creds_dict = dict(cuenta)
                sm = SheetsManager(creds_dict)
                # La conexión se da por buena en cuanto se puede leer la
                # configuración. Todo lo demás (crear hojas, agregar columnas)
                # es preparación del esquema y se hace aparte: si algo de eso
                # falla —por ejemplo por cuota de la API de Google— NO debe
                # dejar a todo el personal sin poder entrar al sistema.
                st.session_state.config = sm.get_config()
                st.session_state.sm = sm
                st.session_state.error_conexion = None
                preparar_esquema(sm)
        except Exception as e:
            import traceback
            st.session_state.error_conexion = (
                f"{type(e).__name__}: {e}\n\n" + traceback.format_exc())


def preparar_esquema(sm):
    """Crea hojas y columnas que falten, sin poner en riesgo la conexión.

    Cada paso va por separado: si uno falla (cuota de la API agotada, permisos,
    una hoja bloqueada) los demás siguen y el sistema queda utilizable. Los
    fallos se guardan para mostrárselos al administrador, no al empleado.
    """
    if st.session_state.get("esquema_preparado"):
        return
    fallos = []
    pasos = [("hoja Usuarios",          sm.ensure_usuarios_sheet),
             ("hoja Llamados",          sm.ensure_llamados_sheet),
             ("columnas nuevas",        sm.migrar_esquema),
             ("hojas de riesgo y KYE",  sm.ensure_hojas_riesgo)]
    for nombre, fn in pasos:
        try:
            fn()
        except Exception as e:
            fallos.append(f"{nombre}: {type(e).__name__}: {e}")
    st.session_state.esquema_preparado = True
    st.session_state.fallos_esquema = fallos


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
    sm = get_sm()
    correo = email_usuario_actual()
    if not (sm and correo):
        return False
    # Sin memoria entre recargas: si RRHH acaba de asignarle gente a cargo,
    # el módulo de aprobaciones debe aparecer sin cerrar sesión. La caché de
    # lecturas de SheetsManager evita que esto cueste una consulta cada vez.
    return sm.es_jefe(correo)


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
        st.session_state.config = sm.get_config()
        st.session_state.sm = sm
        preparar_esquema(sm)
        return True
    except Exception as e:
        # Se muestra el tipo de error, no la traza completa: esta pantalla es
        # pública y la traza revela rutas internas y correos de servicio.
        st.error(f"❌ Error al conectar: {type(e).__name__}: {e}")
        return False


# ── Pantalla de login / credenciales ─────────────────────────────────────────
def pantalla_login():
    """Pantalla previa a la conexión con Google Sheets.

    En producción NO debería verse nunca: el aplicativo se conecta solo con la
    cuenta de servicio guardada en los Secrets de Streamlit. Si un empleado
    llega aquí es que falta esa configuración, y pedirle un archivo de
    credenciales que no tiene (ni debe tener) no es la solución.
    """
    col_center = st.columns([1, 2, 1])[1]
    with col_center:
        st.image("https://www.quski.ec/wp-content/uploads/2022/08/logo-quski.png",
                 width=180, use_container_width=False)
    st.markdown("<h2 style='text-align:center'>Sistema de Asistencia RRHH</h2>",
                unsafe_allow_html=True)
    st.divider()

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        detalle = st.session_state.get("error_conexion") or ""
        # Un 503 de Google es una caída pasajera de su servicio, no un problema
        # de configuración: merece un mensaje distinto y un botón de reintento.
        transitorio = bool(detalle) and es_error_transitorio(detalle)

        if transitorio:
            st.warning("⏳ **Google no está respondiendo en este momento.**")
            st.markdown(
                "Es una interrupción temporal del servicio de Google Sheets, no "
                "un problema de tu usuario ni de la configuración del sistema. "
                "Suele durar poco.")
            if st.button("🔄 Reintentar conexión", type="primary",
                         use_container_width=True):
                st.session_state.sm = None
                st.session_state.error_conexion = None
                st.session_state.pop("esquema_preparado", None)
                st.rerun()
            st.caption("Si después de varios minutos sigue igual, avisa a RRHH.")
        else:
            st.error("⚠️ **El sistema no está conectado a la base de datos.**")
            st.markdown(
                "Esto **no es un problema de tu usuario** y no necesitas ningún "
                "archivo. Avisa al área de RRHH para que termine la configuración; "
                "mientras tanto no podrás ingresar.")

        if detalle:
            with st.expander("🔍 Detalle técnico (para RRHH o sistemas)"):
                st.code(detalle, language=None)

        st.markdown("---")
        with st.expander("🔐 Soy administrador del sistema"):
            st.markdown(
                "**Solución permanente** — para que nadie más vuelva a ver esta "
                "pantalla, carga la cuenta de servicio en los Secrets:\n\n"
                "1. En Streamlit Cloud abre *Manage app → Settings → Secrets*.\n"
                "2. Pega el contenido del `credentials.json` con este formato "
                "(respetando los `\\n` de la clave privada).\n"
                "3. Guarda y haz *Reboot app*."
            )
            st.code('''[gcp_service_account]
type                        = "service_account"
project_id                  = "tu-proyecto"
private_key_id              = "..."
private_key                 = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
client_email                = "...@....iam.gserviceaccount.com"
client_id                   = "..."
auth_uri                    = "https://accounts.google.com/o/oauth2/auth"
token_uri                   = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url        = "..."''', language="toml")
            st.caption("El spreadsheet debe estar compartido como Editor con ese "
                       "`client_email`.")

            st.markdown("---")
            st.markdown("**Conexión temporal** (solo para esta sesión):")
            uploaded = st.file_uploader(
                "Subir credentials.json", type=["json"],
                help="Solo destraba tu propia sesión. No arregla el problema "
                     "para el resto del equipo: para eso hay que usar los Secrets.")
            if uploaded:
                try:
                    creds_dict = json.load(uploaded)
                    if st.button("🔗 Conectar", use_container_width=True, type="primary"):
                        with st.spinner("Conectando…"):
                            if conectar_sheets(creds_dict):
                                st.success("✅ Conexión exitosa")
                                st.rerun()
                except Exception:
                    st.error("Archivo JSON inválido.")


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
            momento = ahora()

            if bloqueado_hasta and momento < bloqueado_hasta:
                restante = int((bloqueado_hasta - momento).total_seconds())
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
                            momento + timedelta(seconds=BLOQUEO_SEGUNDOS)
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
                "🏖️  Saldo Vacaciones",
                "🛡️  Riesgo Operativo",
                "🎓  Formación",
                "⚠️  Llamados de Atención",
                "📁  Expediente",
            ]
        else:
            options = ["✅  Mi Asistencia"]
            # Quien tiene personal a cargo ve primero su bandeja de aprobaciones
            if es_jefe():
                options.append("✍️  Aprobaciones")
                options.append("⚠️  Llamados de Atención")
            options += [
                "🎓  Mi Formación",
                "📋  Mis Permisos",
                "🏖️  Mis Vacaciones",
                "⏰  Mis Horas Extra",
                "🔑  Cambiar Contraseña",
            ]

        page = st.selectbox("Módulo", options=options, label_visibility="collapsed",
                            key="nav_modulo")
        st.markdown("---")
        st.caption(f"📅 {hoy().strftime('%d/%m/%Y')}")
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
    hoy_str = hoy().strftime("%Y-%m-%d")
    mes = hoy().strftime("%Y-%m")

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
    asis_hoy  = len(asis_df[asis_df["Fecha"].astype(str) == hoy_str]) if not asis_df.empty else 0
    tardanzas_mes = len(asis_df[
        (asis_df["Fecha"].astype(str).str.startswith(mes)) &
        (asis_df["Estado"].astype(str) == "Tardanza")
    ]) if not asis_df.empty else 0
    pend_permisos = len(perm_df[perm_df["Estado"].astype(str).str.lower() == "pendiente_aprobacion"]) if not perm_df.empty else 0
    pend_vac = len(vac_df[vac_df["Estado"].astype(str).str.lower() == "pendiente"]) if not vac_df.empty else 0

    col1, col2, col3, col4 = st.columns(4)
    with col1: metric_card("Total empleados", total_emp, "👥")
    with col2: metric_card("Asistencias hoy_str", asis_hoy, "✅")
    with col3: metric_card("Tardanzas en el mes", tardanzas_mes, "⚠️")
    with col4: metric_card("Solicitudes pendientes", pend_permisos + pend_vac, "📋")

    st.divider()
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("📅 Asistencia de hoy_str")
        asis_hoy_df = asis_df[asis_df["Fecha"].astype(str) == hoy_str] if not asis_df.empty else pd.DataFrame()
        if not asis_hoy_df.empty:
            st.dataframe(
                asis_hoy_df[["ID_Empleado","Nombre","Hora_Entrada","Hora_Salida","Estado","Minutos_Atraso"]],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("Sin registros de asistencia hoy_str.")

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

            st.divider()
            st.caption("**Vacaciones** — necesarios para calcular su saldo")
            v1, v2 = st.columns(2)
            with v1:
                f_ingreso = st.date_input("Fecha de ingreso a la empresa *",
                                          value=hoy(), min_value=date(1970, 1, 1),
                                          max_value=hoy(),
                                          help="De aquí sale toda la antigüedad.")
            with v2:
                d_tomados = st.number_input("Días de vacaciones ya tomados",
                                            min_value=0.0, max_value=999.0, step=0.5,
                                            value=0.0,
                                            help="Solo los tomados antes de usar el sistema.")

            submitted = st.form_submit_button("💾 Guardar empleado", type="primary")
            if submitted:
                if not all([nombre, email, area, email_jefe]):
                    st.error("Por favor completa todos los campos obligatorios (*)")
                else:
                    with st.spinner("Guardando…"):
                        emp_id = sm.agregar_empleado(
                            nombre, email, area, email_jefe,
                            hora_ini.strftime("%H:%M"), hora_fin.strftime("%H:%M"),
                            f_ingreso.strftime("%Y-%m-%d"), d_tomados
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

                st.divider()
                st.caption("**Vacaciones**")
                v1, v2 = st.columns(2)
                with v1:
                    ing_prev = sm._a_fecha(emp.get("Fecha_Ingreso"))
                    f_ingreso = st.date_input("Fecha de ingreso a la empresa",
                                              value=ing_prev or hoy(),
                                              min_value=date(1970, 1, 1), max_value=hoy())
                with v2:
                    d_tomados = st.number_input(
                        "Días de vacaciones ya tomados (carga inicial)",
                        min_value=0.0, max_value=999.0, step=0.5,
                        value=float(sm._a_numero(emp.get("Dias_Tomados_Inicial"), 0.0)))
                if ing_prev is None:
                    st.caption("⚠️ Sin fecha de ingreso no se puede calcular su saldo "
                               "de vacaciones.")

                if st.form_submit_button("💾 Actualizar", type="primary"):
                    with st.spinner("Actualizando…"):
                        sm.actualizar_empleado(
                            str(emp_id), nombre, email, area, email_jefe,
                            hora_ini.strftime("%H:%M"), hora_fin.strftime("%H:%M"),
                            f_ingreso.strftime("%Y-%m-%d"), d_tomados
                        )
                    flash("success", "✅ Empleado actualizado")
                    st.rerun()


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
    hoy_str = hoy().strftime("%Y-%m-%d")

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
        st.subheader(f"Registrar entrada – {hoy().strftime('%d/%m/%Y')}")
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
            if sm.ya_registro_entrada(emp_id, hoy_str):
                st.success(f"✅ {nombre} ya tiene su entrada registrada hoy.")
            else:
                # La hora ya no se escribe a mano: se toma del reloj en el
                # instante del clic, para que el registro no sea manipulable.
                st.info(f"🕐 Hora actual en Ecuador: **{ahora().strftime('%H:%M')}**")
                st.caption("La hora se toma automáticamente al registrar y no se "
                           "puede modificar.")
                if not dentro_de_ventana_entrada(ahora(), config):
                    _d, _h = ventana_registro_entrada(config)
                    st.warning(f"⚠️ Son las {ahora().strftime('%H:%M')}, fuera del "
                               f"horario laboral ({_d.strftime('%H:%M')}–{_h.strftime('%H:%M')}). "
                               "Registrar la entrada ahora dejaría un atraso enorme en "
                               "el expediente. Verifica que sea lo que quieres.")
                if st.button("📌 Registrar mi entrada ahora", type="primary"):
                    momento = ahora()
                    with st.spinner("Registrando…"):
                        estado, atraso = sm.registrar_entrada(
                            emp_id, nombre, momento.strftime("%H:%M"), config)
                    if estado == "Tardanza":
                        flash("warning", f"⏰ Entrada de **{nombre}** registrada a las "
                                         f"{momento.strftime('%H:%M')} — "
                                         f"**{atraso} minuto(s)** de atraso.")
                        notificar_tardanza(sm, config, emp_id, nombre, momento, atraso)
                    else:
                        flash("success", f"✅ Entrada de **{nombre}** registrada a tiempo, "
                                         f"a las {momento.strftime('%H:%M')}.")
                    st.rerun()

    with tab2:
        st.subheader(f"Registrar salida – {hoy().strftime('%d/%m/%Y')}")
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
            st.info(f"🕐 Hora actual en Ecuador: **{ahora().strftime('%H:%M')}**")
            st.caption("La hora se toma automáticamente al registrar y no se puede "
                       "modificar. La salida se registra sobre la entrada de hoy.")
            _hfin = config.get("Horario_Fin", "17:30")
            st.caption(f"Tu horario de salida es **{_hfin}**. Salir después de esa hora "
                       "**no genera atraso ni hora extra**: solo queda la hora registrada. "
                       "Si sales antes, se avisa a tu jefe inmediato.")
            obs = st.text_input("Observaciones (opcional)", key="obs_salida")

            if st.button("📌 Registrar mi salida ahora", type="primary"):
                momento = ahora()
                try:
                    with st.spinner("Registrando…"):
                        ev = sm.registrar_salida(emp_id, hoy_str,
                                                 momento.strftime("%H:%M"), obs, config)
                    flash("success", f"✅ Salida de **{nombre}** registrada a las "
                                     f"{momento.strftime('%H:%M')}.")
                    if ev["anticipada"]:
                        flash("warning", f"⏱️ Salida **{ev['minutos_antes']} minuto(s) "
                                         f"antes** del horario ({ev['horario_fin']}). "
                                         "Se avisó a tu jefe inmediato.")
                        notificar_salida_anticipada(sm, config, emp_id, nombre, ev, obs)
                    elif ev.get("permiso") and ev["minutos_antes"] > 0:
                        # Salió antes pero tenía permiso: no es salida anticipada.
                        pm = ev["permiso"]
                        flash("info", f"ℹ️ Saliste {ev['minutos_antes']} minuto(s) antes, "
                                      f"amparado por el permiso **{pm['id']}** "
                                      f"({pm['horas']}h). No se registró como salida "
                                      "anticipada ni se avisó a tu jefe.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
                    st.caption("Si no marcaste la entrada hoy, RRHH debe registrarla "
                               "antes de poder marcar la salida.")

    if admin:
        with st.expander("🔎 Revisar jornadas sin salida marcada"):
            st.caption("Busca días ya cerrados donde alguien marcó entrada pero no "
                       "salida, y avisa al jefe inmediato y a RRHH. Cada jornada se "
                       "avisa una sola vez.")
            dias_rev = st.number_input("Días hacia atrás a revisar", min_value=1,
                                       max_value=60, value=7, key="dias_rev_salidas")
            if st.button("📨 Revisar y avisar", key="btn_rev_salidas"):
                with st.spinner("Revisando…"):
                    revisar_salidas_pendientes(sm, config, int(dias_rev), mostrar=True)

    with tab3:
        st.subheader("Historial de asistencia")
        c1, c2 = st.columns(2)
        with c1:
            fecha_desde = st.date_input("Desde", hoy().replace(day=1))
        with c2:
            fecha_hasta = st.date_input("Hasta", hoy())

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
                fecha_aus = st.date_input("Fecha de ausencia", hoy())
                motivo_aus = st.text_area("Motivo de ausencia")
                if st.button("📌 Registrar ausencia", type="primary"):
                    with st.spinner("Registrando…"):
                        sm.marcar_ausencia(emp_id, nombre, fecha_aus.strftime("%Y-%m-%d"), motivo_aus)
                    st.success(f"✅ Ausencia registrada para **{nombre}**")


# ── Módulo: Configuración ─────────────────────────────────────────────────────
def _cfg_int(config: dict, key: str, default: int,
             minimo: int = None, maximo: int = None) -> int:
    """Lee un entero de la configuración, acotado al rango permitido."""
    try:
        v = int(float(config.get(key, default)))
    except (ValueError, TypeError):
        v = default
    if minimo is not None:
        v = max(minimo, v)
    if maximo is not None:
        v = min(maximo, v)
    return v


def _cfg_float(config: dict, key: str, default: float,
               minimo: float = None, maximo: float = None) -> float:
    """Lee un decimal de la configuración, acotado al rango permitido."""
    try:
        v = float(config.get(key, default))
    except (ValueError, TypeError):
        v = default
    if minimo is not None:
        v = max(minimo, v)
    if maximo is not None:
        v = min(maximo, v)
    return v


def num_config(etiqueta: str, config: dict, key: str, default, minimo, maximo,
               paso=None, ayuda: str = None):
    """Campo numérico ligado a un valor de configuración.

    Toma el valor guardado y lo acota al rango del campo. Antes, un valor
    fuera de rango en la hoja —por ejemplo 60 tardanzas con un tope de 30—
    reventaba la pantalla entera de Configuración con
    StreamlitValueAboveMaxError. Al usar la misma función para el rango y para
    el valor, ambos no pueden quedar descoordinados.
    """
    es_entero = isinstance(default, int) and isinstance(minimo, int)
    if es_entero:
        v = _cfg_int(config, key, default, minimo, maximo)
    else:
        v = _cfg_float(config, key, float(default), float(minimo), float(maximo))
        minimo, maximo = float(minimo), float(maximo)
    kw = {"min_value": minimo, "max_value": maximo, "value": v}
    if paso is not None:
        kw["step"] = paso
    if ayuda:
        kw["help"] = ayuda
    guardado = config.get(key, None)
    campo = st.number_input(etiqueta, **kw)
    # Si lo guardado no cabía en el rango, se avisa en vez de corregir en silencio
    try:
        if guardado not in (None, "") and abs(float(guardado) - float(v)) > 1e-9:
            st.caption(f"⚠️ El valor guardado era **{guardado}**, fuera del rango "
                       f"permitido ({minimo}–{maximo}). Se ajustó a **{v}**.")
    except (ValueError, TypeError):
        pass
    return campo


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
            tolerancia = num_config("Tolerancia (minutos)", config,
                "Tolerancia_Minutos", 0, 0, 120)
        with c2:
            h_fin = st.time_input("Horario de salida", _cfg_time(config, "Horario_Fin", "17:30"))
            tol_salida = num_config("Tolerancia de salida (minutos)", config,
                "Tolerancia_Salida_Minutos", 0, 0, 120,
                ayuda="Minutos antes del horario de fin que se toleran sin avisar "
                      "al jefe. En 0, cualquier salida anticipada dispara el aviso.")
            horas_perm = num_config("Horas de permiso mensual", config,
                "Horas_Permiso_Mensual", 3.0, 0.5, 80.0, paso=0.5)

        st.subheader("Contacto y zona horaria")
        email_rrhh = st.text_input("Email RRHH", config.get("Email_RRHH", "rrhh@quski.ec"))
        zona_saved = config.get("Zona_Horaria", "America/Guayaquil")
        zona_idx   = _ZONAS.index(zona_saved) if zona_saved in _ZONAS else 0
        zona = st.selectbox("Zona horaria", _ZONAS, index=zona_idx)

        st.subheader("⚠️ Política disciplinaria – Tardanzas")
        st.caption("Número de tardanzas acumuladas en el mes que activan cada tipo de llamado de atención")
        d1, d2, d3 = st.columns(3)
        with d1:
            tard_verbal   = num_config("Llamado Verbal", config,
                "Tardanzas_Llamado_Verbal", 3, 1, 99)
        with d2:
            tard_escrito  = num_config("Llamado Escrito", config,
                "Tardanzas_Llamado_Escrito", 5, 1, 99)
        with d3:
            tard_suspension = num_config("Suspensión", config,
                "Tardanzas_Suspension", 8, 1, 99)

        st.subheader("📂 Repositorio de documentos")
        st.caption("Los archivos del personal se guardan en una carpeta de Google "
                   "Drive; en la hoja solo queda el enlace.")
        drive_id = st.text_input(
            "ID de la carpeta de Drive",
            value=config.get("Drive_Carpeta_ID", ""),
            help="Está en la URL de la carpeta: drive.google.com/drive/folders/ESTE_ID")
        st.caption("⚠️ La carpeta debe ser de una persona (no de la cuenta de "
                   "servicio) y estar compartida como **Editor** con el correo "
                   "de la cuenta de servicio. Las cuentas de servicio no tienen "
                   "espacio propio en Drive.")

        st.subheader("🛡️ Riesgo operativo – pesos del modelo")
        st.caption("Cuánto pesa cada factor en el puntaje de riesgo. Se normalizan a "
                   "100, así que poner uno en cero lo elimina y redistribuye el resto.")
        w1, w2, w3 = st.columns(3)
        with w1:
            w_buro = num_config("Score de buró", config, "Riesgo_Peso_Buro", 30, 0, 100)
            w_pep = num_config("Condición PEP", config, "Riesgo_Peso_PEP", 10, 0, 100)
        with w2:
            w_doc = num_config("Documentación KYE", config, "Riesgo_Peso_Documentos", 15, 0, 100)
            w_fam = num_config("Situación familiar", config, "Riesgo_Peso_Familiar", 10, 0, 100,
                ayuda="Estado civil, hijos y situación del cónyuge. Ponerlo en cero "
                      "elimina ese factor del cálculo.")
        with w3:
            w_dis = num_config("Historial disciplinario", config, "Riesgo_Peso_Disciplina", 25, 0, 100)
            w_ant = num_config("Antigüedad", config, "Riesgo_Peso_Antiguedad", 10, 0, 100)
            w_for = num_config("Formación académica", config, "Riesgo_Peso_Formacion", 10, 0, 100,
                ayuda="Títulos validados y cursos recientes reducen el riesgo.")
        b1, b2, b3 = st.columns(3)
        with b1:
            buro_bueno = num_config("Score de buró considerado bueno", config,
                "Buro_Score_Bueno", 800, 0, 1000)
        with b2:
            buro_malo = num_config("Score de buró considerado deficiente", config,
                "Buro_Score_Malo", 400, 0, 1000)
        with b3:
            st.caption("Umbrales de nivel sobre el puntaje 0-100")
            u_med = num_config("Medio desde", config, "Riesgo_Umbral_Medio", 30, 0, 100)
            u_alt = num_config("Alto desde", config, "Riesgo_Umbral_Alto", 55, 0, 100)
            u_cri = num_config("Crítico desde", config, "Riesgo_Umbral_Critico", 75, 0, 100)

        st.subheader("🏖️ Vacaciones")
        st.caption("Días **calendario** (incluyen fines de semana), no días hábiles.")
        v1, v2, v3 = st.columns(3)
        with v1:
            vac_base = num_config("Días por año", config,
                "Dias_Vacaciones_Base", 15, 1, 90)
        with v2:
            vac_desde = num_config("Año en que empieza el día adicional", config,
                "Anio_Inicio_Dia_Adicional", 5, 1, 50,
                ayuda="Desde este año de servicio se suma un día por cada año más.")
        with v3:
            vac_techo = num_config("Máximo de días", config,
                "Max_Dias_Vacaciones", 30, 1, 120)

        if st.form_submit_button("💾 Guardar configuración", type="primary"):
            updates = {
                "Horario_Inicio":              h_ini.strftime("%H:%M"),
                "Horario_Fin":                 h_fin.strftime("%H:%M"),
                "Tolerancia_Minutos":          str(tolerancia),
                "Tolerancia_Salida_Minutos":   str(tol_salida),
                "Horas_Permiso_Mensual":       str(horas_perm),
                "Email_RRHH":                  email_rrhh,
                "Zona_Horaria":                zona,
                "Tardanzas_Llamado_Verbal":    str(tard_verbal),
                "Tardanzas_Llamado_Escrito":   str(tard_escrito),
                "Tardanzas_Suspension":        str(tard_suspension),
                "Dias_Vacaciones_Base":        str(vac_base),
                "Anio_Inicio_Dia_Adicional":   str(vac_desde),
                "Max_Dias_Vacaciones":         str(vac_techo),
                "Riesgo_Peso_Buro":            str(w_buro),
                "Riesgo_Peso_PEP":             str(w_pep),
                "Riesgo_Peso_Documentos":      str(w_doc),
                "Riesgo_Peso_Familiar":        str(w_fam),
                "Riesgo_Peso_Disciplina":      str(w_dis),
                "Riesgo_Peso_Antiguedad":      str(w_ant),
                "Riesgo_Peso_Formacion":       str(w_for),
                "Drive_Carpeta_ID":            drive_id.strip(),
                "Buro_Score_Bueno":            str(buro_bueno),
                "Buro_Score_Malo":             str(buro_malo),
                "Riesgo_Umbral_Medio":         str(u_med),
                "Riesgo_Umbral_Alto":          str(u_alt),
                "Riesgo_Umbral_Critico":       str(u_cri),
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
        cfg_e = leer_secrets("email")
        if cfg_e is None:
            st.error("❌ **No hay configuración de correo.**")
            st.markdown(
                "- **En Streamlit Cloud:** *Manage app → Settings → Secrets*.\n"
                "- **En tu equipo:** crea el archivo `.streamlit/secrets.toml` "
                "dentro de la carpeta del proyecto.\n\n"
                "En ambos casos el contenido es el mismo:")
            st.code("""[email]
smtp_server   = "smtp.gmail.com"
smtp_port     = "587"
smtp_user     = "tu_correo@quski.ec"
smtp_password = "xxxx xxxx xxxx xxxx"   # Contraseña de Aplicación (16 caracteres)""",
                    language="toml")
            st.caption("⚠️ Nunca subas secrets.toml a GitHub: agrégalo al archivo .gitignore.")
        else:
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
                    ("Días calendario", fila.get("Dias_Calendario", "—") or "—"),
                    ("Días hábiles", fila.get("Dias_Habiles", "—")),
                    ("Motivo", fila.get("Motivo", "") or "—"),
                    ("Reemplazo", fila.get("Reemplazo", "") or "—")],
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

        if not email_emp:
            st.warning("⚠️ Este empleado **no tiene correo** en su ficha, así que no "
                       "recibirá el aviso de la aprobación. Complétalo en el módulo "
                       "de Empleados.")

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
        msg_ok = (f"✅ {d['asunto'].capitalize()} **{d['id']}** de "
                  f"**{d['nombre']}** aprobado en firme.")
        cuerpo = (f"<p>La solicitud de <strong>{esc(d['asunto'])}</strong> de "
                  f"<strong>{esc(d['nombre'])}</strong> quedó "
                  f"<strong style='color:#065F46'>APROBADA</strong> por RRHH.</p>"
                  + tabla_html(filas))
        destinos = [email_emp, email_jefe, email_rrhh]
        asunto = f"{d['asunto'].capitalize()} aprobado – {d['nombre']}"
    else:
        msg_ok = (f"✅ Aprobación registrada para **{d['id']}** "
                  f"({d['nombre']}). Pasa a revisión de RRHH.")
        cuerpo = (f"<p>El jefe inmediato aprobó la solicitud de "
                  f"<strong>{esc(d['asunto'])}</strong> de "
                  f"<strong>{esc(d['nombre'])}</strong>. "
                  f"Queda pendiente la revisión final de RRHH.</p>"
                  + tabla_html(filas))
        destinos = [email_rrhh, email_emp]
        asunto = f"Pendiente de RRHH: {d['asunto']} – {d['nombre']}"

    with st.spinner("Enviando notificaciones…"):
        enviados, fallidos = notificar(destinos, asunto, cuerpo)
    flash("success", msg_ok)
    if enviados:
        flash("info", "📧 Notificado a: " + ", ".join(enviados))
    if fallidos:
        flash("warning", "⚠️ No se pudo notificar a: " + ", ".join(fallidos))
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

    cuerpo = (f"<p>La solicitud de <strong>{esc(d['asunto'])}</strong> de "
              f"<strong>{esc(d['nombre'])}</strong> fue "
              f"<strong style='color:#991B1B'>RECHAZADA</strong>.</p>"
              + tabla_html(d["detalle"] + [("Rechazada por", quien),
                                           ("Motivo del rechazo", motivo)]))
    with st.spinner("Enviando notificaciones…"):
        enviados, fallidos = notificar([email_emp, email_jefe, email_rrhh],
                                       f"{d['asunto'].capitalize()} rechazado – {d['nombre']}",
                                       cuerpo)
    flash("warning", f"Solicitud **{d['id']}** de {d['nombre']} rechazada.")
    if enviados:
        flash("info", "📧 Notificado a: " + ", ".join(enviados))
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


# ── Saldo de vacaciones ───────────────────────────────────────────────────────
def _regla_vacaciones(config) -> str:
    base  = _cfg_int(config, "Dias_Vacaciones_Base", 15)
    desde = _cfg_int(config, "Anio_Inicio_Dia_Adicional", 5)
    techo = _cfg_int(config, "Max_Dias_Vacaciones", 30)
    return (f"**{base} días calendario** por cada año de servicio; a partir del año "
            f"**{desde}** se suma un día por cada año adicional, hasta un máximo de "
            f"**{techo}** días.")


def panel_saldo(sm, config, emp_id, compacto: bool = False):
    """Muestra el saldo de vacaciones de un empleado. Devuelve el dict del saldo."""
    saldo = sm.saldo_vacaciones(emp_id, config)

    if saldo["sin_fecha_ingreso"]:
        st.warning("⚠️ No tienes fecha de ingreso registrada, así que todavía no se "
                   "puede calcular tu saldo de vacaciones. Pídele a RRHH que la cargue.")
        return saldo

    c1, c2, c3 = st.columns(3)
    c1.metric("Días disponibles", f"{saldo['disponibles']:.1f}")
    c2.metric("Ganados a la fecha", f"{saldo['acumulados']:.1f}")
    c3.metric("Ya usados", f"{saldo['usados_total']:.1f}")

    if not compacto:
        with st.expander("🔍 Cómo se calcula este saldo"):
            st.markdown(
                f"- **Ingreso a la empresa:** {saldo['ingreso'].strftime('%d/%m/%Y')}\n"
                f"- **Antigüedad:** {saldo['anios']:.2f} años\n"
                f"- **Le corresponden este año:** {saldo['derecho_anio_actual']} días\n"
                f"- **Ganados desde el ingreso:** {saldo['acumulados']:.1f} días "
                f"(incluye la parte proporcional del año en curso)\n"
                f"- **Días tomados antes del sistema:** {saldo['inicial']:.1f}\n"
                f"- **Vacaciones aprobadas en el sistema:** {saldo['aprobados']:.1f}\n"
                f"- **En trámite (aún sin aprobar):** {saldo['en_tramite']:.1f}\n\n"
                f"**Disponibles = {saldo['acumulados']:.1f} − {saldo['usados_total']:.1f} "
                f"= {saldo['disponibles']:.1f} días**")
            st.caption("Regla: " + _regla_vacaciones(config))

    if saldo["disponibles"] < 0:
        st.error(f"⚠️ Saldo en negativo: se han tomado {abs(saldo['disponibles']):.1f} "
                 "días más de los ganados.")
    elif saldo["en_tramite"] > 0:
        st.caption(f"ℹ️ Los {saldo['en_tramite']:.1f} día(s) en trámite ya están "
                   "descontados del saldo disponible.")
    return saldo


def page_saldo_vacaciones():
    sm = get_sm()
    config = st.session_state.config
    st.title("🏖️ Saldo de Vacaciones")
    st.caption("Regla aplicada: " + _regla_vacaciones(config) +
               " Se ajusta desde Configuración.")

    et_tabla = "📊 Saldos del personal"
    et_carga = "📥 Carga inicial por empleado"
    seccion = secciones([et_tabla, et_carga], "sec_saldo_vac")
    st.divider()

    df_emp = sm.get_empleados()
    if df_emp.empty:
        st.warning("No hay empleados registrados.")
        return

    if seccion == et_tabla:
        with st.spinner("Calculando saldos…"):
            tabla = sm.tabla_saldos(config)
        if tabla.empty:
            st.info("No hay empleados para calcular.")
            return

        sin_fecha = tabla[tabla["Fecha_Ingreso"] == "—"]
        if not sin_fecha.empty:
            st.warning(f"⚠️ **{len(sin_fecha)}** empleado(s) sin fecha de ingreso. "
                       "Hasta cargarla no se puede calcular su saldo.")
            st.caption("Sin fecha: " + ", ".join(sin_fecha["Nombre"].astype(str).tolist()))

        con_datos = tabla[tabla["Fecha_Ingreso"] != "—"]
        if not con_datos.empty:
            disp = pd.to_numeric(con_datos["Disponibles"], errors="coerce")
            c1, c2, c3 = st.columns(3)
            c1.metric("Empleados con saldo calculado", len(con_datos))
            c2.metric("Días disponibles en total", f"{disp.sum():.0f}")
            c3.metric("Con saldo negativo", int((disp < 0).sum()))

        st.dataframe(tabla, use_container_width=True, hide_index=True)
        st.caption("**Acumulados** = días ganados desde el ingreso · "
                   "**Carga inicial** = días tomados antes de usar el sistema · "
                   "**Disponibles** = acumulados − (carga inicial + aprobados + en trámite)")

        csv = tabla.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Descargar saldos en CSV", csv,
                           file_name=f"saldos_vacaciones_{hoy().strftime('%Y%m%d')}.csv",
                           mime="text/csv")

    else:
        st.markdown("Carga la **fecha de ingreso** y los **días de vacaciones ya "
                    "tomados** antes de que existiera el sistema. A partir de ahí, "
                    "las solicitudes que se aprueben se descuentan solas.")

        opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                    for _, r in df_emp.iterrows() if str(r["ID_Empleado"]).strip()}
        if not opciones:
            st.warning("No hay empleados registrados.")
            return
        sel = st.selectbox("Empleado", list(opciones.keys()), key="saldo_emp_sel")
        emp_id = opciones[sel]
        emp = sm.get_empleado(emp_id) or {}

        st.divider()
        panel_saldo(sm, config, emp_id)
        st.divider()

        ingreso_actual = sm._a_fecha(emp.get("Fecha_Ingreso"))
        tomados_actual = sm._a_numero(emp.get("Dias_Tomados_Inicial"), 0.0)

        with st.form("form_carga_vac"):
            c1, c2 = st.columns(2)
            with c1:
                f_ingreso = st.date_input(
                    "Fecha de ingreso a la empresa",
                    value=ingreso_actual or hoy(),
                    min_value=date(1970, 1, 1), max_value=hoy(),
                    help="Fecha real de entrada. De aquí sale toda la antigüedad.")
            with c2:
                d_tomados = st.number_input(
                    "Días ya tomados antes del sistema", min_value=0.0,
                    max_value=999.0, step=0.5, value=float(tomados_actual),
                    help="Solo los que NO están registrados como solicitudes aquí. "
                         "Las vacaciones aprobadas en el sistema se descuentan aparte.")
            if st.form_submit_button("💾 Guardar carga inicial", type="primary"):
                try:
                    with st.spinner("Guardando…"):
                        sm.set_datos_vacaciones(emp_id, f_ingreso.strftime("%Y-%m-%d"),
                                                d_tomados)
                    flash("success", f"✅ Datos de vacaciones guardados para **{sel}**.")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ No se pudo guardar: {e}")

        if ingreso_actual is None:
            st.info("Este empleado aún no tiene fecha de ingreso cargada.")


# ── Módulo: Aprobaciones del jefe inmediato ──────────────────────────────────
def page_aprobaciones_jefe():
    sm = get_sm()
    config = st.session_state.config
    usuario = get_usuario()
    correo = email_usuario_actual()

    st.title("✍️ Aprobaciones de mi equipo")

    equipo = sm.get_subordinados(correo)
    if equipo.empty:
        # No basta con decir "no hay nadie a tu cargo": casi siempre es que el
        # correo de la ficha no coincide con el que se escribió en Email_Jefe.
        # Se muestra contra qué se comparó para que RRHH lo corrija de una vez.
        if getattr(sm, "ultimo_error", None):
            st.error("❌ No se pudo leer la hoja de Empleados en este momento.")
            with st.expander("🔍 Detalle técnico"):
                st.code(sm.ultimo_error, language=None)
            return

        st.info("No hay empleados asignados a tu cargo.")
        with st.expander("🔍 ¿Por qué? Revisar la asignación de jefes", expanded=True):
            if not correo:
                st.warning("Tu ficha de empleado no tiene correo en la columna "
                           "**Email**, así que nadie puede tenerte como jefe.")
                st.caption("Pídele a RRHH que complete tu correo en la hoja Empleados.")
                return
            st.markdown(f"Se buscaron empleados cuya columna **Email_Jefe** sea "
                        f"exactamente `{correo}`.")
            df_todos = sm.get_empleados()
            if "Email_Jefe" in df_todos.columns and not df_todos.empty:
                jefes = (df_todos["Email_Jefe"].astype(str).str.strip()
                         .replace({"": None, "nan": None, "None": None}).dropna().unique())
                if len(jefes):
                    st.markdown("**Correos de jefe registrados hoy en la hoja:**")
                    for j in sorted(jefes):
                        marca = "✅" if j.strip().lower() == correo.strip().lower() else "▫️"
                        st.markdown(f"{marca} `{j}`")
                    st.caption("Si tu correo aparece escrito distinto (mayúsculas, "
                               "espacios, otro dominio), corrígelo en la hoja Empleados "
                               "para que coincida con el de tu ficha.")
                else:
                    st.warning("Ningún empleado tiene la columna **Email_Jefe** "
                               "completa. Mientras esté vacía, todas las solicitudes "
                               "van directo a RRHH sin pasar por el jefe.")
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

    et_perm = f"📋 Permisos ({len(perm_pend)})"
    et_vac  = f"🏖️ Vacaciones ({len(vac_pend)})"
    et_eq   = "👥 Mi equipo"
    seccion = secciones([et_perm, et_vac, et_eq], "sec_aprob_jefe")
    st.divider()

    if seccion == et_perm:
        if perm_pend.empty:
            st.info("Sin permisos pendientes.")
        for _, fila in perm_pend.iterrows():
            tarjeta_aprobacion(sm, config, fila, "permiso", "jefe", firma)

    elif seccion == et_vac:
        if vac_pend.empty:
            st.info("Sin solicitudes de vacaciones pendientes.")
        for _, fila in vac_pend.iterrows():
            tarjeta_aprobacion(sm, config, fila, "vacacion", "jefe", firma)

    else:
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

        et_apr  = f"⏳ Para aprobar ({len(pend_rrhh)})"
        et_jefe = f"👤 Esperando al jefe ({len(pend_jefe)})"
        et_hist = "📋 Historial"
        et_new  = "➕ Nueva solicitud"
        seccion = secciones([et_apr, et_jefe, et_hist, et_new], "sec_rrhh_permisos")
        st.divider()

        if seccion == et_apr:
            if pend_rrhh.empty:
                st.success("✅ No hay permisos esperando la firma de RRHH.")
            else:
                st.info(f"**{len(pend_rrhh)}** permiso(s) con el visto bueno del jefe, "
                        "listos para tu aprobación final.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_rrhh.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "permiso", "rrhh", firma)

        elif seccion == et_jefe:
            if pend_jefe.empty:
                st.success("✅ Ninguna solicitud está detenida en el jefe inmediato.")
            else:
                st.caption("Estas solicitudes esperan al jefe inmediato. Si el jefe ya "
                           "autorizó por correo o de forma verbal, puedes registrarlo aquí.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_jefe.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "permiso", "jefe", firma,
                                       en_nombre_del_jefe=True)

        elif seccion == et_hist:
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

        else:
            st.info(f"ℹ️ Límite mensual: **{limite} horas por empleado**")
            opciones_a = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                          for _, r in df_emp.iterrows()} if not df_emp.empty else {}
            if not opciones_a:
                st.warning("No hay empleados registrados.")
            else:
                with st.form("form_permiso_admin", clear_on_submit=True):
                    sel = st.selectbox("Empleado", list(opciones_a.keys()))
                    fecha_p  = st.date_input("Fecha del permiso", hoy())
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
                aviso_sin_perfil(sm, emp_id)
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
                    fecha_p  = st.date_input("Fecha del permiso", hoy())
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

        et_apr  = f"⏳ Para aprobar ({len(pend_rrhh)})"
        et_jefe = f"👤 Esperando al jefe ({len(pend_jefe)})"
        et_hist = "📋 Historial"
        et_new  = "➕ Nueva solicitud"
        seccion = secciones([et_apr, et_jefe, et_hist, et_new], "sec_rrhh_vac")
        st.divider()

        if seccion == et_apr:
            if pend_rrhh.empty:
                st.success("✅ No hay vacaciones esperando la firma de RRHH.")
            else:
                st.info(f"**{len(pend_rrhh)}** solicitud(es) con el visto bueno del jefe.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_rrhh.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "vacacion", "rrhh", firma)

        elif seccion == et_jefe:
            if pend_jefe.empty:
                st.success("✅ Ninguna solicitud está detenida en el jefe inmediato.")
            else:
                st.caption("Estas solicitudes esperan al jefe inmediato. Si el jefe ya "
                           "autorizó por otro medio, puedes registrarlo aquí.")
                firma = usuario.get("nombre", "RRHH")
                for _, fila in pend_jefe.iterrows():
                    tarjeta_aprobacion(sm, config, fila, "vacacion", "jefe", firma,
                                       en_nombre_del_jefe=True)

        elif seccion == et_hist:
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

        else:
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
                    motivo_v    = st.text_area("Motivo del pedido")
                    reemplazo_v = st.text_input("¿Quién lo reemplaza?")
                    if st.form_submit_button("📤 Registrar solicitud", type="primary"):
                        if fecha_fin < fecha_ini:
                            st.error("La fecha de fin debe ser posterior a la de inicio.")
                        else:
                            _crear_vacaciones(sm, config, opciones_a[sel],
                                              sel.split(" – ")[-1],
                                              fecha_ini, fecha_fin, email_rrhh,
                                              motivo_v.strip(), reemplazo_v.strip())

    # ── Vista EMPLEADO ───────────────────────────────────────────────────────
    else:
        tab1, tab2 = st.tabs(["➕ Solicitar vacaciones", "📋 Mis vacaciones"])

        emp_id = usuario["id_empleado"] if usuario else ""
        emp = sm.get_empleado(emp_id)

        with tab1:
            if not emp:
                aviso_sin_perfil(sm, emp_id)
            else:
                nombre_emp = str(emp.get("Nombre", emp_id))
                jefe = sm.get_email_jefe(emp_id)
                st.info(f"👤 Solicitud para: **{emp_id} – {nombre_emp}**")

                saldo = panel_saldo(sm, config, emp_id)
                st.divider()

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
                    motivo_v = st.text_area(
                        "Motivo del pedido",
                        placeholder="Ej: Vacaciones familiares programadas",
                        help="Queda registrado junto con la solicitud.")
                    reemplazo_v = st.text_input(
                        "¿Quién te reemplaza? *",
                        placeholder="Ej: 402 – Jessica Benavides",
                        help="Persona que cubre tus funciones mientras no estés.")
                    if st.form_submit_button("📤 Enviar solicitud", type="primary"):
                        if fecha_fin < fecha_ini:
                            st.error("La fecha de fin debe ser posterior a la de inicio.")
                        else:
                            pedidos = sm.dias_calendario(fecha_ini.strftime("%Y-%m-%d"),
                                                         fecha_fin.strftime("%Y-%m-%d"))
                            if not reemplazo_v.strip():
                                st.error("Indica quién te va a reemplazar.")
                            elif (not saldo["sin_fecha_ingreso"]
                                    and pedidos > saldo["disponibles"]):
                                st.error(
                                    f"❌ Estás pidiendo **{pedidos} días calendario** y "
                                    f"solo tienes **{saldo['disponibles']:.1f} disponibles**. "
                                    "Ajusta las fechas o consulta con RRHH.")
                            else:
                                _crear_vacaciones(sm, config, emp_id, nombre_emp,
                                                  fecha_ini, fecha_fin, email_rrhh,
                                                  motivo_v.strip(), reemplazo_v.strip())

        with tab2:
            df_v = sm.get_vacaciones()
            if not df_v.empty and usuario:
                df_v = df_v[df_v["ID_Empleado"].astype(str).str.strip() == str(emp_id).strip()]
            if df_v.empty:
                st.info("No tienes solicitudes de vacaciones.")
            else:
                st.dataframe(_tabla_estado(df_v, "vacacion"),
                             use_container_width=True, hide_index=True)


def _crear_vacaciones(sm, config, emp_id, nombre_emp, fecha_ini, fecha_fin, email_rrhh,
                      motivo="", reemplazo=""):
    """Registra las vacaciones y notifica al jefe inmediato y a RRHH."""
    try:
        with st.spinner("Enviando…"):
            res = sm.solicitar_vacaciones(emp_id, fecha_ini.strftime("%Y-%m-%d"),
                                          fecha_fin.strftime("%Y-%m-%d"),
                                          motivo, reemplazo)
    except Exception as e:
        st.error(f"❌ No se pudo registrar la solicitud: {e}")
        return

    st.success(f"✅ Vacaciones **{res['id']}** enviadas — {etiqueta_estado(res['estado'])}")

    filas = [("Empleado", f"{emp_id} – {nombre_emp}"),
             ("Desde", fecha_ini.strftime("%d/%m/%Y")),
             ("Hasta", fecha_fin.strftime("%d/%m/%Y")),
             ("Días calendario", res.get("dias_calendario", res["dias"])),
             ("Días hábiles", res["dias"]),
             ("Motivo", motivo or "—"),
             ("Reemplazo durante la ausencia", reemplazo or "—")]
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
                fecha_hex = st.date_input("Fecha", hoy())
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


# ── Módulo: Riesgo Operativo y KYE ───────────────────────────────────────────
_SI_NO = ["No", "Si"]


def _chk(valor) -> int:
    return 1 if str(valor).strip().lower() in ("si", "sí", "true", "1", "x") else 0


def panel_riesgo(sm, config, emp_id, compacto: bool = False):
    """Muestra la evaluación de riesgo de un asesor con su desglose."""
    ev = sm.evaluar_riesgo(emp_id, config)

    c1, c2 = st.columns([1, 2])
    with c1:
        st.metric("Riesgo operativo", f"{ev['puntaje']:.0f}/100",
                  help="0 = sin riesgo · 100 = riesgo máximo")
        st.markdown(f"### {ev['icono']} {ev['nivel']}")
    with c2:
        if not ev["tiene_kye"]:
            st.warning("⚠️ Sin ficha **Conozca a su Empleado**. Los factores de "
                       "documentación y situación familiar se calculan en el peor "
                       "escenario hasta que se llene.")
        top = sorted(ev["desglose"], key=lambda d: -d["Aporta al puntaje"])[:2]
        if top and top[0]["Aporta al puntaje"] > 0:
            st.markdown("**Lo que más pesa:**")
            for d in top:
                if d["Aporta al puntaje"] > 0:
                    st.markdown(f"- {d['Factor']} (+{d['Aporta al puntaje']}) — {d['Por qué']}")

    if not compacto:
        with st.expander("🔍 Desglose completo del cálculo", expanded=False):
            st.dataframe(pd.DataFrame(ev["desglose"]), use_container_width=True,
                         hide_index=True)
            st.caption(f"Los pesos suman {ev['peso_total']:.0f} y el puntaje se "
                       "normaliza a 100. Se ajustan en Configuración.")
    return ev


def page_riesgo_operativo():
    sm = get_sm()
    config = st.session_state.config
    usuario = get_usuario()
    st.title("🛡️ Riesgo Operativo")

    et_mat = "📊 Matriz por asesor"
    et_kye = "📋 Ficha Conozca a su Empleado"
    et_bur = "📈 Score de buró"
    et_rec = "🎖️ Reconocimientos"
    seccion = secciones([et_mat, et_kye, et_bur, et_rec], "sec_riesgo")
    st.divider()

    df_emp = sm.get_empleados()
    if df_emp.empty:
        st.warning("No hay empleados registrados.")
        return
    opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                for _, r in df_emp.iterrows() if str(r["ID_Empleado"]).strip()}

    # ── Matriz ───────────────────────────────────────────────────────────────
    if seccion == et_mat:
        with st.spinner("Evaluando al personal…"):
            matriz = sm.matriz_riesgo(config)
        if matriz.empty:
            st.info("Sin empleados para evaluar.")
            return

        c1, c2, c3, c4 = st.columns(4)
        for col, nivel, etiqueta in ((c1, "Crítico", "🔴 Crítico"), (c2, "Alto", "🟠 Alto"),
                                     (c3, "Medio", "🟡 Medio"), (c4, "Bajo", "🟢 Bajo")):
            col.metric(etiqueta, int(matriz["Nivel"].str.contains(nivel).sum()))

        criticos = matriz[matriz["Nivel"].str.contains("Crítico|Alto")]
        if not criticos.empty:
            st.error(f"⚠️ **{len(criticos)}** asesor(es) en riesgo alto o crítico. "
                     "Requieren revisión de controles.")

        sin_kye = matriz[matriz["Ficha KYE"] == "Falta"]
        if not sin_kye.empty:
            st.warning(f"📋 **{len(sin_kye)}** sin ficha KYE: "
                       + ", ".join(sin_kye["Nombre"].astype(str).tolist()))

        st.dataframe(matriz, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Descargar matriz en CSV",
                           matriz.to_csv(index=False).encode("utf-8"),
                           file_name=f"matriz_riesgo_{hoy().strftime('%Y%m%d')}.csv",
                           mime="text/csv")

        st.divider()
        st.subheader("Detalle por asesor")
        sel = st.selectbox("Asesor", list(opciones.keys()), key="riesgo_detalle")
        panel_riesgo(sm, config, opciones[sel])

    # ── Ficha KYE ────────────────────────────────────────────────────────────
    elif seccion == et_kye:
        st.caption("Datos del formulario de vinculación. Alimentan los factores de "
                   "documentación, condición PEP y situación familiar de la matriz.")
        sel = st.selectbox("Empleado", list(opciones.keys()), key="kye_emp")
        emp_id = opciones[sel]
        k = sm.get_kye(emp_id)
        if k:
            st.success(f"✅ Ficha registrada · última actualización: "
                       f"{k.get('Actualizado', '—')}")
        else:
            st.info("Este empleado todavía no tiene ficha. Puedes subir su "
                    "formulario en Excel o llenarla a mano abajo.")

        # ── Carga del formulario en Excel ────────────────────────────────────
        with st.expander("📤 Subir el formulario en Excel y llenar automáticamente",
                         expanded=not bool(k)):
            st.caption("Sube el archivo **Conozca a su Empleado** ya lleno. El "
                       "sistema extrae los datos y precarga el formulario de abajo "
                       "para que los revises antes de guardar. **Nada se guarda "
                       "hasta que presiones Guardar ficha KYE.**")
            archivo = st.file_uploader("Formulario del empleado", type=["xlsx", "xlsm"],
                                       key=f"up_kye_{emp_id}")
            if archivo is not None and st.button("📥 Leer el formulario",
                                                 key=f"btn_kye_{emp_id}"):
                try:
                    with st.spinner("Leyendo el formulario…"):
                        r = leer_formulario_kye(archivo)
                    st.session_state[f"kye_pre_{emp_id}"] = r["datos"]
                    leidos = sum(1 for v in r["datos"].values() if str(v).strip())
                    detectado = r["nombre_detectado"]
                    if detectado and detectado.lower().split()[0] not in sel.lower():
                        st.warning(f"⚠️ El formulario dice **{detectado}**, pero "
                                   f"seleccionaste **{sel}**. Verifica que sea la "
                                   "persona correcta antes de guardar.")
                    flash("success", f"📥 Formulario de **{detectado or sel}** leído: "
                                     f"{leidos} campos precargados. Revísalos y guarda.")
                    if r["avisos"]:
                        flash("warning", "Estos campos no se pudieron leer y hay que "
                                         "completarlos a mano: " + " · ".join(r["avisos"]))
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ No se pudo leer el archivo: {e}")
                    st.caption("Verifica que sea el formulario estándar de vinculación "
                               "en formato .xlsx, sin filas ni columnas agregadas.")

        # Los datos leídos del Excel precargan el formulario, sin pisar lo que
        # ya estuviera guardado con un valor vacío.
        precarga = st.session_state.get(f"kye_pre_{emp_id}")
        if precarga:
            k = {**k, **{kk: vv for kk, vv in precarga.items() if str(vv).strip()}}
            st.info("📥 Formulario precargado desde el Excel. **Revisa los datos** "
                    "—sobre todo estado civil, sexo y las declaraciones PEP— y "
                    "presiona Guardar al final.")

        with st.form("form_kye"):
            st.markdown("##### I. Información personal")
            c1, c2, c3 = st.columns(3)
            with c1:
                cedula = st.text_input("Cédula / Pasaporte", k.get("Cedula", ""))
                nacion = st.text_input("Nacionalidad", k.get("Nacionalidad", "Ecuatoriana"))
            with c2:
                f_nac = st.text_input("Fecha de nacimiento (AAAA-MM-DD)",
                                      k.get("Fecha_Nacimiento", ""))
                ciudad = st.text_input("Ciudad de nacimiento", k.get("Ciudad_Nacimiento", ""))
            with c3:
                civiles = ["Soltero(a)", "Casado(a)", "Divorciado(a)", "Viudo(a)",
                           "Separado(a)", "Unión libre"]
                ec = k.get("Estado_Civil", "")
                civil = st.selectbox("Estado civil", civiles,
                                     index=civiles.index(ec) if ec in civiles else 0)
                sexos = ["Femenino", "Masculino"]
                sx = k.get("Sexo", "")
                sexo = st.selectbox("Sexo", sexos, index=sexos.index(sx) if sx in sexos else 0)
            hijos = st.number_input("Número de hijos", min_value=0, max_value=20,
                                    value=int(sm._a_numero(k.get("Num_Hijos"), 0)))

            st.markdown("##### II. Residencia y contacto")
            r1, r2, r3 = st.columns(3)
            with r1:
                provincia = st.text_input("Provincia", k.get("Provincia", ""))
                canton    = st.text_input("Cantón", k.get("Canton", ""))
            with r2:
                parroquia = st.text_input("Parroquia", k.get("Parroquia", ""))
                direccion = st.text_input("Dirección", k.get("Direccion", ""))
            with r3:
                tel_dom = st.text_input("Teléfono domicilio", k.get("Telefono_Domicilio", ""))
                celular = st.text_input("Celular", k.get("Celular", ""))
            email_p = st.text_input("Correo personal", k.get("Email_Personal", ""))

            st.markdown("##### III–IV. Cónyuge y su actividad económica")
            y1, y2, y3 = st.columns(3)
            with y1:
                cy_nom = st.text_input("Nombre del cónyuge", k.get("Conyuge_Nombre", ""),
                                       help="Déjalo vacío si no aplica.")
                cy_ced = st.text_input("Cédula del cónyuge", k.get("Conyuge_Cedula", ""))
            with y2:
                cy_trab = st.selectbox("¿El cónyuge tiene ingresos propios?", _SI_NO,
                                       index=_chk(k.get("Conyuge_Trabaja")))
                cy_rel  = st.selectbox("Relación laboral",
                                       ["", "Empleado público", "Empleado privado", "Independiente"],
                                       index=["", "Empleado público", "Empleado privado",
                                              "Independiente"].index(k.get("Conyuge_Relacion", ""))
                                       if k.get("Conyuge_Relacion", "") in
                                       ["", "Empleado público", "Empleado privado", "Independiente"] else 0)
            with y3:
                cy_emp  = st.text_input("Empresa / negocio", k.get("Conyuge_Empresa", ""))
                cy_carg = st.text_input("Cargo", k.get("Conyuge_Cargo", ""))
            cy_act = st.text_input("Actividad económica", k.get("Conyuge_Actividad", ""))

            st.markdown("##### V. Declaración de Persona Expuesta Políticamente")
            p1, p2 = st.columns(2)
            with p1:
                es_pep = st.selectbox("¿Es o fue PEP en los últimos 3 años?", _SI_NO,
                                      index=_chk(k.get("Es_PEP")))
                pep_cargo = st.text_input("Cargo desempeñado", k.get("PEP_Cargo", ""))
            with p2:
                fam_pep = st.selectbox("¿Tiene familiar o vínculo cercano PEP?", _SI_NO,
                                       index=_chk(k.get("Familiar_PEP")))
                fam_det = st.text_input("Nombre, cargo y parentesco",
                                        k.get("Familiar_PEP_Detalle", ""))

            st.markdown("##### Otros ingresos")
            o1, o2 = st.columns(2)
            with o1:
                otros = st.selectbox("¿Declara otros ingresos?", _SI_NO,
                                     index=_chk(k.get("Otros_Ingresos")))
            with o2:
                otros_det = st.text_input("Detalle", k.get("Otros_Ingresos_Detalle", ""))

            st.markdown("##### Documentos adjuntos y validaciones")
            etiquetas = {
                "Doc_Hoja_Vida": "a) Hoja de vida",
                "Doc_Cedula": "b) Copia de cédula",
                "Doc_Cedula_Conyuge": "c) Cédula del cónyuge",
                "Doc_Papeleta": "d) Papeleta de votación",
                "Doc_Papeleta_Conyuge": "e) Papeleta del cónyuge",
                "Doc_Ref_Laborales": "f) 3 referencias laborales",
                "Doc_Ref_Personales": "g) 3 referencias personales",
                "Doc_Servicio_Basico": "h) Planilla de servicio básico",
                "Doc_Declaracion_Patrimonial": "i) Declaración patrimonial",
            }
            docs, cols_d = {}, st.columns(3)
            for i, (campo, etiqueta) in enumerate(etiquetas.items()):
                with cols_d[i % 3]:
                    docs[campo] = "Si" if st.checkbox(
                        etiqueta, value=bool(_chk(k.get(campo))), key=f"kye_{campo}") else "No"

            f_form = st.text_input("Fecha del formulario (AAAA-MM-DD)",
                                   k.get("Fecha_Formulario", hoy().strftime("%Y-%m-%d")))
            obs = st.text_area("Observaciones", k.get("Observaciones", ""))

            if st.form_submit_button("💾 Guardar ficha KYE", type="primary"):
                datos = {
                    "Cedula": cedula, "Fecha_Nacimiento": f_nac, "Nacionalidad": nacion,
                    "Ciudad_Nacimiento": ciudad, "Estado_Civil": civil, "Sexo": sexo,
                    "Num_Hijos": hijos, "Provincia": provincia, "Canton": canton,
                    "Parroquia": parroquia, "Direccion": direccion,
                    "Telefono_Domicilio": tel_dom, "Celular": celular,
                    "Email_Personal": email_p,
                    "Conyuge_Nombre": cy_nom, "Conyuge_Cedula": cy_ced,
                    "Conyuge_Trabaja": cy_trab, "Conyuge_Relacion": cy_rel,
                    "Conyuge_Empresa": cy_emp, "Conyuge_Actividad": cy_act,
                    "Conyuge_Cargo": cy_carg,
                    "Es_PEP": es_pep, "PEP_Cargo": pep_cargo,
                    "Familiar_PEP": fam_pep, "Familiar_PEP_Detalle": fam_det,
                    "Otros_Ingresos": otros, "Otros_Ingresos_Detalle": otros_det,
                    "Fecha_Formulario": f_form, "Observaciones": obs,
                    "Registrado_Por": str(usuario.get("nombre", "")) if usuario else "",
                    **docs,
                }
                try:
                    with st.spinner("Guardando…"):
                        sm.guardar_kye(emp_id, datos)
                    st.session_state.pop(f"kye_pre_{emp_id}", None)
                    flash("success", f"✅ Ficha KYE de **{sel}** guardada.")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ No se pudo guardar: {e}")

    # ── Score de buró ────────────────────────────────────────────────────────
    elif seccion == et_bur:
        st.caption("Score al ingreso y en cada revisión. Un deterioro frente al "
                   "score de ingreso aumenta el riesgo del asesor.")
        sel = st.selectbox("Empleado", list(opciones.keys()), key="buro_emp")
        emp_id = opciones[sel]

        actual = sm.score_buro_actual(emp_id)
        if actual:
            b1, b2, b3 = st.columns(3)
            b1.metric("Score actual", f"{actual['actual']:.0f}")
            b2.metric("Al ingreso", f"{actual['ingreso']:.0f}" if actual["ingreso"] else "—",
                      delta=(f"{actual['actual'] - actual['ingreso']:+.0f}"
                             if actual["ingreso"] else None))
            b3.metric("Revisiones", actual["revisiones"])
        else:
            st.info("Sin score registrado para este empleado.")

        with st.form("form_buro", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            with c1:
                score = st.number_input("Score", min_value=0, max_value=1000, value=700,
                                        help="Mayor score = mejor comportamiento crediticio.")
            with c2:
                tipo_b = st.selectbox("Tipo", ["Ingreso", "Revisión anual",
                                               "Revisión semestral", "Revisión extraordinaria"])
            with c3:
                fuente = st.text_input("Fuente", "Equifax")
            fecha_b = st.date_input("Fecha de consulta", hoy(), max_value=hoy())
            obs_b = st.text_area("Observaciones")
            if st.form_submit_button("💾 Registrar score", type="primary"):
                try:
                    with st.spinner("Guardando…"):
                        bid = sm.registrar_score_buro(
                            emp_id, score, tipo_b, fuente,
                            str(usuario.get("nombre", "")) if usuario else "",
                            obs_b, fecha_b.strftime("%Y-%m-%d"))
                    flash("success", f"✅ Score **{bid}** registrado para {sel}.")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ No se pudo registrar: {e}")

        h = sm.historial_buro(emp_id)
        if not h.empty:
            st.divider()
            st.subheader("Historial")
            st.dataframe(h[[c for c in ("Fecha", "Score", "Tipo", "Fuente",
                                        "Observaciones", "Registrado_Por")
                            if c in h.columns]],
                         use_container_width=True, hide_index=True)
            serie = h.copy()
            serie["Score"] = pd.to_numeric(serie["Score"], errors="coerce")
            serie = serie.dropna(subset=["Score"])
            if len(serie) > 1:
                st.line_chart(serie.set_index("Fecha")["Score"])

    # ── Reconocimientos ──────────────────────────────────────────────────────
    else:
        st.caption("Cartas de felicitación y reconocimientos. Compensan parcialmente "
                   "el historial disciplinario en la matriz de riesgo.")
        sel = st.selectbox("Empleado", list(opciones.keys()), key="rec_emp")
        emp_id = opciones[sel]
        nombre_emp = sel.split(" – ")[-1]

        with st.form("form_reconocimiento", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                tipo_r = st.selectbox("Tipo de reconocimiento", TIPOS_RECONOCIMIENTO)
            with c2:
                fecha_r = st.date_input("Fecha", hoy(), max_value=hoy())
            motivo_r = st.text_area("Motivo *",
                                    placeholder="Ej: Cero diferencias en cierre de caja "
                                                "durante doce meses consecutivos.")
            otorga = st.text_input("Otorgado por *",
                                   value=str(usuario.get("nombre", "")) if usuario else "")
            if st.form_submit_button("🎖️ Registrar reconocimiento", type="primary"):
                if not motivo_r.strip():
                    st.error("El motivo es obligatorio.")
                elif not otorga.strip():
                    st.error("Indica quién lo otorga.")
                else:
                    try:
                        with st.spinner("Guardando…"):
                            rid = sm.registrar_reconocimiento(
                                emp_id, nombre_emp, tipo_r, motivo_r.strip(),
                                otorga.strip(), fecha_r.strftime("%Y-%m-%d"))
                        flash("success", f"🎖️ Reconocimiento **{rid}** registrado "
                                         f"para {nombre_emp}.")
                        email_emp = sm.get_email_empleado(emp_id)
                        cuerpo = (f"<p>Estimado/a <strong>{esc(nombre_emp)}</strong>,</p>"
                                  f"<p>Se ha registrado un "
                                  f"<strong style='color:#065F46'>{esc(tipo_r)}</strong> "
                                  f"en tu expediente.</p>"
                                  + tabla_html([("Tipo", tipo_r),
                                                ("Motivo", motivo_r.strip()),
                                                ("Otorgado por", otorga.strip()),
                                                ("Fecha", fecha_r.strftime("%d/%m/%Y"))])
                                  + "<p style='margin-top:16px'>¡Felicitaciones!</p>")
                        env, fal = notificar([email_emp, sm.get_email_jefe(emp_id),
                                              config.get("Email_RRHH", "")],
                                             f"Reconocimiento – {nombre_emp}", cuerpo)
                        if env:
                            flash("info", "📧 Notificado a: " + ", ".join(env))
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ No se pudo registrar: {e}")

        rec = sm.get_reconocimientos(emp_id)
        if not rec.empty:
            st.divider()
            st.subheader(f"Reconocimientos de {nombre_emp}")
            st.dataframe(rec[[c for c in ("ID_Reconocimiento", "Fecha", "Tipo",
                                          "Motivo", "Otorgado_Por")
                              if c in rec.columns]],
                         use_container_width=True, hide_index=True)
        else:
            st.info("Sin reconocimientos registrados para esta persona.")


# ── Módulo: Formación, títulos y documentos ──────────────────────────────────
def _carpeta_drive(config) -> str:
    return str(config.get("Drive_Carpeta_ID", "") or "").strip()


def _subir_y_registrar(sm, config, emp_id, nombre_emp, archivo, categoria,
                       tipo_doc, quien, estado, referencia="") -> str | None:
    """Sube el archivo a Drive y deja el registro en la hoja. Devuelve el ID."""
    carpeta = _carpeta_drive(config)
    if not carpeta:
        st.error("❌ No hay carpeta de Drive configurada. RRHH debe hacerlo en "
                 "Configuración antes de poder subir documentos.")
        return None
    try:
        with st.spinner("Subiendo el archivo a Drive…"):
            # Nombre único y descriptivo para poder ubicarlo en la carpeta
            limpio = "".join(c for c in archivo.name if c.isalnum() or c in "._- ")
            final = f"{emp_id}_{ahora().strftime('%Y%m%d-%H%M')}_{limpio}"
            subido = sm.subir_documento_drive(archivo, final, carpeta)
            did = sm.registrar_documento(
                emp_id, nombre_emp, categoria, tipo_doc, final,
                subido["id"], subido["link"], quien, estado, referencia)
        return did
    except Exception as e:
        st.error(f"❌ No se pudo subir el archivo: {e}")
        st.caption("Si dice *storageQuotaExceeded* o *File not found*, revisa que "
                   "la carpeta de Drive esté compartida como **Editor** con la "
                   "cuenta de servicio.")
        return None


def _tabla_documentos(df, mostrar_empleado=False):
    if df.empty:
        st.info("Sin documentos registrados.")
        return
    cols = ["ID_Documento", "Fecha"] + (["Nombre"] if mostrar_empleado else []) + \
           ["Categoria", "Tipo_Documento", "Estado", "Revisado_Por", "Observaciones"]
    vista = df[[c for c in cols if c in df.columns]].copy()
    if "Estado" in vista.columns:
        iconos = {"Pendiente": "⏳ Pendiente", "Aprobado": "✅ Aprobado",
                  "Rechazado": "❌ Rechazado"}
        vista["Estado"] = vista["Estado"].map(lambda v: iconos.get(str(v).strip(), v))
    st.dataframe(vista, use_container_width=True, hide_index=True)
    # Los enlaces se listan aparte: dentro de una tabla no son clicables
    with st.expander("🔗 Abrir los archivos en Drive"):
        for _, r in df.iterrows():
            link = str(r.get("Drive_Link", "")).strip()
            etiqueta = f"{r.get('ID_Documento','')} · {r.get('Tipo_Documento','')}"
            if link:
                st.markdown(f"- [{etiqueta}]({link})")
            else:
                st.markdown(f"- {etiqueta} — sin enlace")


def page_formacion():
    sm = get_sm()
    config = st.session_state.config
    usuario = get_usuario()
    admin = es_admin()
    quien = str(usuario.get("nombre", "")) if usuario else ""

    st.title("🎓 Formación y Documentos" if admin else "🎓 Mi Formación")

    if admin:
        et_rev = "📥 Por revisar"
        et_doc = "📂 Documentos"
        et_cur = "📚 Capacitación"
        et_tit = "🎓 Títulos"
        seccion = secciones([et_rev, et_doc, et_cur, et_tit], "sec_formacion_admin")
    else:
        et_doc = "📤 Subir documentos"
        et_cur = "📚 Mis cursos"
        et_tit = "🎓 Mis títulos"
        et_rev = None
        seccion = secciones([et_doc, et_cur, et_tit], "sec_formacion_emp")
    st.divider()

    if not _carpeta_drive(config):
        st.warning("⚠️ Todavía no hay carpeta de Google Drive configurada, así que "
                   "no se pueden subir archivos." +
                   (" Configúrala en **Configuración → Repositorio de documentos**."
                    if admin else " Avisa a RRHH."))

    df_emp = sm.get_empleados()
    if admin:
        if df_emp.empty:
            st.warning("No hay empleados registrados.")
            return
        opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                    for _, r in df_emp.iterrows() if str(r["ID_Empleado"]).strip()}
    else:
        emp_id = str(usuario.get("id_empleado", "")).strip()
        emp = sm.get_empleado(emp_id)
        if not emp:
            aviso_sin_perfil(sm, emp_id)
            return
        nombre_emp = str(emp.get("Nombre", emp_id))

    # ══ RRHH: cola de revisión ═══════════════════════════════════════════════
    if admin and seccion == et_rev:
        pend_doc = sm.get_documentos(estado="Pendiente")
        titulos  = sm.get_titulos()
        pend_tit = (titulos[titulos["Estado_Validacion"].astype(str) == "Pendiente"]
                    if not titulos.empty else pd.DataFrame())

        c1, c2 = st.columns(2)
        c1.metric("Documentos por revisar", len(pend_doc))
        c2.metric("Títulos por validar", len(pend_tit))

        st.subheader("Documentos enviados por el personal")
        if pend_doc.empty:
            st.success("✅ No hay documentos pendientes de revisión.")
        else:
            for _, d in pend_doc.iterrows():
                did = str(d["ID_Documento"])
                with st.container(border=True):
                    st.markdown(f"### {d.get('Nombre','')}")
                    st.caption(f"{did} · {d.get('Categoria','')} · "
                               f"{d.get('Tipo_Documento','')} · subido "
                               f"{d.get('Fecha','')} por {d.get('Subido_Por','')}")
                    link = str(d.get("Drive_Link", "")).strip()
                    if link:
                        st.markdown(f"📎 [Abrir el archivo en Drive]({link})")
                    else:
                        st.caption("Sin enlace al archivo.")
                    obs = st.text_input("Observación (opcional)", key=f"obs_doc_{did}")
                    b1, b2 = st.columns(2)
                    with b1:
                        if st.button("✅ Aprobar", key=f"ok_doc_{did}",
                                     type="primary", use_container_width=True):
                            try:
                                sm.revisar_documento(did, True, quien, obs)
                                flash("success", f"✅ Documento {did} aprobado.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ {e}")
                    with b2:
                        if st.button("❌ Rechazar", key=f"no_doc_{did}",
                                     use_container_width=True):
                            if not obs.strip():
                                st.error("Indica el motivo del rechazo en la observación.")
                            else:
                                try:
                                    sm.revisar_documento(did, False, quien, obs)
                                    flash("warning", f"Documento {did} rechazado.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"❌ {e}")

        st.divider()
        st.subheader("Títulos por validar")
        if pend_tit.empty:
            st.success("✅ No hay títulos pendientes de validación.")
        else:
            for _, t in pend_tit.iterrows():
                tid = str(t["ID_Titulo"])
                with st.container(border=True):
                    st.markdown(f"### {t.get('Nombre','')}")
                    st.markdown(f"**{t.get('Nivel','')}** — {t.get('Titulo','')}")
                    st.caption(f"{t.get('Institucion','')} · {t.get('Anio_Obtencion','')} · "
                               f"SENESCYT: {t.get('Registro_SENESCYT','') or '—'}")
                    doc_id = str(t.get("Documento_ID", "")).strip()
                    if doc_id:
                        docs = sm.get_documentos()
                        fila = docs[docs["ID_Documento"].astype(str) == doc_id] \
                               if not docs.empty else pd.DataFrame()
                        if not fila.empty:
                            enlace = str(fila.iloc[0].get("Drive_Link", "")).strip()
                            if enlace:
                                st.markdown(f"📎 [Ver el título escaneado]({enlace})")
                    else:
                        st.warning("Sin documento de respaldo adjunto.")
                    obs_t = st.text_input("Observación", key=f"obs_tit_{tid}")
                    v1, v2 = st.columns(2)
                    with v1:
                        if st.button("✅ Validar", key=f"ok_tit_{tid}",
                                     type="primary", use_container_width=True):
                            try:
                                sm.validar_titulo(tid, True, quien, obs_t)
                                flash("success", f"✅ Título {tid} validado.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ {e}")
                    with v2:
                        if st.button("⚠️ Observar", key=f"no_tit_{tid}",
                                     use_container_width=True):
                            if not obs_t.strip():
                                st.error("Explica qué se observa del título.")
                            else:
                                try:
                                    sm.validar_titulo(tid, False, quien, obs_t)
                                    flash("warning", f"Título {tid} observado.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"❌ {e}")

    # ══ Documentos ═══════════════════════════════════════════════════════════
    elif seccion == et_doc:
        if admin:
            sel = st.selectbox("Empleado", list(opciones.keys()), key="doc_emp")
            emp_id, nombre_emp = opciones[sel], sel.split(" – ")[-1]
        else:
            st.info(f"👤 {emp_id} – {nombre_emp}")
            st.caption("Lo que subas queda **pendiente** hasta que RRHH lo revise.")

        with st.form("form_doc", clear_on_submit=True):
            categoria = st.selectbox("Categoría", CATEGORIAS_DOCUMENTO)
            tipo_doc = st.text_input(
                "Descripción del documento *",
                placeholder="Ej: Copia de cédula, Certificado de Excel avanzado")
            archivo = st.file_uploader("Archivo *",
                                       type=["pdf", "jpg", "jpeg", "png", "docx", "xlsx"],
                                       help="Máximo 200 MB. Se guarda en la carpeta "
                                            "de Drive de la empresa.")
            if st.form_submit_button("📤 Subir documento", type="primary"):
                if not tipo_doc.strip():
                    st.error("Describe qué documento es.")
                elif archivo is None:
                    st.error("Selecciona un archivo.")
                else:
                    estado = "Aprobado" if admin else "Pendiente"
                    did = _subir_y_registrar(sm, config, emp_id, nombre_emp, archivo,
                                             categoria, tipo_doc.strip(), quien, estado)
                    if did:
                        if admin:
                            flash("success", f"✅ Documento **{did}** guardado.")
                        else:
                            flash("success", f"📤 Documento **{did}** enviado. "
                                             "Queda pendiente de revisión por RRHH.")
                            env, _ = notificar(
                                [config.get("Email_RRHH", "")],
                                f"Documento por revisar – {nombre_emp}",
                                f"<p><strong>{esc(nombre_emp)}</strong> subió un "
                                f"documento para revisión.</p>" +
                                tabla_html([("Empleado", f"{emp_id} – {nombre_emp}"),
                                            ("Categoría", categoria),
                                            ("Documento", tipo_doc.strip()),
                                            ("Referencia", did)]))
                        st.rerun()

        st.divider()
        _tabla_documentos(sm.get_documentos(emp_id))

    # ══ Capacitación ═════════════════════════════════════════════════════════
    elif seccion == et_cur:
        if admin:
            sel = st.selectbox("Empleado", list(opciones.keys()), key="cur_emp")
            emp_id, nombre_emp = opciones[sel], sel.split(" – ")[-1]

        p = sm.perfil_formacion(emp_id)
        c1, c2, c3 = st.columns(3)
        c1.metric("Cursos finalizados", p["cursos_finalizados"])
        c2.metric("En curso", p["cursos_en_curso"])
        c3.metric("Horas acumuladas", f"{p['horas_capacitacion']:.0f}")
        if p["formacion_reciente"]:
            st.success(f"✅ Formación vigente: {p['cursos_recientes']} curso(s) en "
                       "los últimos dos años.")
        else:
            st.warning("⚠️ Sin formación registrada en los últimos dos años.")

        with st.form("form_curso", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                curso = st.text_input("Nombre del curso *")
                institucion = st.text_input("Institución *")
                fin_tipo = st.selectbox("Financiamiento", FINANCIAMIENTO_CURSO)
            with c2:
                estado_c = st.selectbox("Estado", ESTADOS_CURSO)
                horas = st.number_input("Horas", min_value=0, max_value=5000, value=0)
                costo = st.number_input("Costo (USD)", min_value=0.0, step=10.0, value=0.0,
                                        help="Útil para saber cuánto invirtió la empresa.")
            f1, f2 = st.columns(2)
            with f1: f_ini = st.date_input("Fecha de inicio", hoy())
            with f2: f_fin = st.date_input("Fecha de fin (o prevista)", hoy())
            cert = st.file_uploader("Certificado (opcional)",
                                    type=["pdf", "jpg", "jpeg", "png"])
            obs_c = st.text_area("Observaciones")
            if st.form_submit_button("💾 Registrar curso", type="primary"):
                if not curso.strip() or not institucion.strip():
                    st.error("El nombre del curso y la institución son obligatorios.")
                else:
                    doc_id = ""
                    if cert is not None:
                        doc_id = _subir_y_registrar(
                            sm, config, emp_id, nombre_emp, cert,
                            "Certificado de curso", f"Certificado: {curso.strip()}",
                            quien, "Aprobado" if admin else "Pendiente") or ""
                    try:
                        with st.spinner("Guardando…"):
                            cid = sm.registrar_capacitacion(
                                emp_id, nombre_emp, curso.strip(), institucion.strip(),
                                fin_tipo, f_ini.strftime("%Y-%m-%d"),
                                f_fin.strftime("%Y-%m-%d"), estado_c, horas, costo,
                                quien, doc_id, obs_c)
                        flash("success", f"✅ Curso **{cid}** registrado.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ No se pudo registrar: {e}")

        cursos = sm.get_capacitaciones(emp_id)
        if not cursos.empty:
            st.divider()
            st.subheader("Historial de capacitación")
            st.dataframe(cursos[[c for c in ("ID_Curso", "Curso", "Institucion",
                                             "Financiamiento", "Fecha_Inicio",
                                             "Fecha_Fin", "Estado", "Horas", "Costo")
                                 if c in cursos.columns]],
                         use_container_width=True, hide_index=True)

            if admin:
                st.caption("Actualizar el estado de un curso (por ejemplo, marcarlo "
                           "como finalizado):")
                a1, a2, a3 = st.columns([2, 2, 1])
                with a1:
                    cid_sel = st.selectbox("Curso", cursos["ID_Curso"].astype(str).tolist(),
                                           key="cur_upd")
                with a2:
                    nuevo = st.selectbox("Nuevo estado", ESTADOS_CURSO, key="cur_estado")
                with a3:
                    st.write("")
                    if st.button("Actualizar", key="btn_cur_upd"):
                        try:
                            campos = {"Estado": nuevo}
                            if nuevo == "Finalizado":
                                campos["Fecha_Fin"] = hoy().strftime("%Y-%m-%d")
                            sm.actualizar_capacitacion(cid_sel, campos)
                            flash("success", f"Curso {cid_sel} → {nuevo}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ {e}")

            invertido = pd.to_numeric(cursos[cursos["Financiamiento"].astype(str)
                                      .str.contains("empresa", case=False, na=False)]["Costo"],
                                      errors="coerce").fillna(0).sum()
            if invertido:
                st.info(f"💰 La empresa ha invertido **USD {invertido:,.2f}** en la "
                        "capacitación de esta persona.")

    # ══ Títulos ══════════════════════════════════════════════════════════════
    else:
        if admin:
            sel = st.selectbox("Empleado", list(opciones.keys()), key="tit_emp")
            emp_id, nombre_emp = opciones[sel], sel.split(" – ")[-1]

        p = sm.perfil_formacion(emp_id)
        c1, c2 = st.columns(2)
        c1.metric("Nivel máximo alcanzado", p["nivel_maximo"])
        c2.metric("Títulos validados", f"{p['titulos_validados']} de {p['titulos']}")

        with st.form("form_titulo", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                nivel = st.selectbox("Nivel *", list(NIVELES_TITULO.keys()))
                titulo_n = st.text_input("Título obtenido *",
                                         placeholder="Ej: Ingeniera en Finanzas")
            with c2:
                inst = st.text_input("Institución *")
                anio = st.number_input("Año de obtención", min_value=1950,
                                       max_value=hoy().year, value=hoy().year)
            senescyt = st.text_input("Número de registro SENESCYT",
                                     help="Permite verificar el título en el registro oficial.")
            doc_t = st.file_uploader("Copia del título *",
                                     type=["pdf", "jpg", "jpeg", "png"])
            obs_t = st.text_area("Observaciones")
            if st.form_submit_button("🎓 Registrar título", type="primary"):
                if not titulo_n.strip() or not inst.strip():
                    st.error("El título y la institución son obligatorios.")
                elif doc_t is None:
                    st.error("Adjunta la copia del título para poder validarlo.")
                else:
                    doc_id = _subir_y_registrar(
                        sm, config, emp_id, nombre_emp, doc_t, "Título académico",
                        f"{nivel}: {titulo_n.strip()}", quien,
                        "Aprobado" if admin else "Pendiente") or ""
                    if doc_id:
                        try:
                            with st.spinner("Guardando…"):
                                tid = sm.registrar_titulo(
                                    emp_id, nombre_emp, nivel, titulo_n.strip(),
                                    inst.strip(), anio, senescyt.strip(), quien,
                                    doc_id, obs_t)
                            flash("success", f"🎓 Título **{tid}** registrado. "
                                             "Queda pendiente de validación por RRHH.")
                            if not admin:
                                notificar([config.get("Email_RRHH", "")],
                                          f"Título por validar – {nombre_emp}",
                                          f"<p><strong>{esc(nombre_emp)}</strong> "
                                          f"registró un título.</p>" +
                                          tabla_html([("Nivel", nivel),
                                                      ("Título", titulo_n.strip()),
                                                      ("Institución", inst.strip()),
                                                      ("Año", anio),
                                                      ("SENESCYT", senescyt.strip() or "—")]))
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ No se pudo registrar: {e}")

        titulos = sm.get_titulos(emp_id)
        if not titulos.empty:
            st.divider()
            vista = titulos[[c for c in ("ID_Titulo", "Nivel", "Titulo", "Institucion",
                                         "Anio_Obtencion", "Registro_SENESCYT",
                                         "Estado_Validacion", "Validado_Por",
                                         "Observaciones")
                             if c in titulos.columns]].copy()
            iconos = {"Pendiente": "⏳ Pendiente", "Validado": "✅ Validado",
                      "Observado": "⚠️ Observado"}
            if "Estado_Validacion" in vista.columns:
                vista["Estado_Validacion"] = vista["Estado_Validacion"].map(
                    lambda v: iconos.get(str(v).strip(), v))
            st.dataframe(vista, use_container_width=True, hide_index=True)


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
             Generado: {hoy().strftime('%d/%m/%Y')}</p>
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
        <div class="footer">Quski – Sistema de Asistencia RRHH &nbsp;|&nbsp; Documento generado el {hoy().strftime('%d/%m/%Y')}</div>
        </body></html>"""

        st.download_button(
            label="⬇️ Descargar expediente",
            data=html_exp.encode("utf-8"),
            file_name=f"expediente_{emp_id}_{emp['Nombre'].replace(' ','_')}_{hoy().strftime('%Y%m%d')}.html",
            mime="text/html",
            use_container_width=True,
        )


# ── Módulo: Llamados de Atención (solo admin) ─────────────────────────────────
def page_llamados_atencion():
    sm = get_sm()
    admin = es_admin()
    usuario = get_usuario()
    st.title("⚠️ Llamados de Atención")

    # RRHH y auditoría ven a toda la empresa; un jefe de área solo a su equipo.
    if not admin:
        correo = email_usuario_actual()
        st.caption("Como jefe de área puedes emitir llamados sobre las personas a "
                   "tu cargo. El registro queda a tu nombre.")
    tab1, tab2, tab3 = st.tabs(["📊 Monitor de Atrasos", "➕ Emitir Llamado", "📋 Historial"])

    mes_actual = hoy().strftime("%Y-%m")
    df_emp  = sm.get_empleados() if admin else sm.get_subordinados(email_usuario_actual())
    if df_emp.empty and not admin:
        st.info("No hay empleados asignados a tu cargo.")
        return
    df_asis = sm.get_df("Asistencia")
    df_llamados = sm.get_llamados_atencion()

    with tab1:
        st.subheader(f"Tardanzas del mes – {hoy().strftime('%B %Y')}")
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

            # La causal se elige fuera del formulario para poder sugerir la
            # gravedad en cuanto cambia, sin esperar al envío.
            causal = st.selectbox("Causal del llamado *", CAUSALES_LLAMADO,
                                  key="causal_llamado",
                                  help="Determina la gravedad sugerida y permite "
                                       "analizar después qué falla más en la operación.")
            tipo_por_causal = GRAVEDAD_CAUSAL.get(causal, "Verbal")
            escala = ["Verbal", "Escrito", "Suspensión"]
            # Manda la más grave entre la sugerida por tardanzas y la de la causal
            sugerido = escala[max(escala.index(tipo_sug), escala.index(tipo_por_causal))]
            if causal != "Acumulación de tardanzas":
                st.caption(f"Para **{causal}** la gravedad sugerida es "
                           f"**{tipo_por_causal}**. Puedes cambiarla abajo.")

            with st.form("form_llamado", clear_on_submit=True):
                emp_id = emp_id_la
                tipo = st.selectbox("Tipo de llamado", escala,
                                    index=escala.index(sugerido),
                                    help="Verbal: primer aviso oral | Escrito: queda en el expediente | Suspensión: sin goce de sueldo")
                motivo = st.text_area("Detalle del hecho *",
                                      placeholder="Ej: Tasación de la prenda 4471 con avalúo 35% "
                                                  "sobre el valor de mercado, sin sustento en el sistema.",
                                      help="Describe el hecho concreto: fecha, operación, monto, "
                                           "cliente. Es lo que sostiene el llamado si se impugna.")
                registrado_por = st.text_input("Emitido por (nombre y cargo) *",
                                               value=str(usuario.get("nombre", "")) if usuario else "",
                                               placeholder="Ej: Karina Bastidas – Auditoría y Control Interno")

                if st.form_submit_button("📋 Emitir llamado de atención", type="primary"):
                    if not motivo.strip():
                        st.error("El detalle del hecho es obligatorio.")
                    elif not registrado_por.strip():
                        st.error("Ingresa quién emite el llamado.")
                    else:
                        with st.spinner("Registrando…"):
                            emp_row = df_emp[df_emp["ID_Empleado"].astype(str) == emp_id]
                            nombre_emp = emp_row.iloc[0]["Nombre"] if not emp_row.empty else emp_id
                            email_emp  = emp_row.iloc[0].get("Email", "") if not emp_row.empty else ""
                            llamado_id = sm.registrar_llamado_atencion(
                                emp_id, nombre_emp, tipo,
                                f"[{causal}] {motivo}", atrasos_mes, registrado_por
                            )
                        st.success(f"✅ Llamado **{llamado_id}** ({tipo}) emitido para **{nombre_emp}**")

                        # Notificación al empleado, a su jefe inmediato y a RRHH
                        email_jefe = sm.get_email_jefe(emp_id)
                        email_rrhh = st.session_state.config.get("Email_RRHH", "")
                        detalle_la = [("Empleado", f"{emp_id} – {nombre_emp}"),
                                      ("Causal", causal),
                                      ("Tipo de llamado", tipo),
                                      ("Motivo", motivo),
                                      ("Tardanzas acumuladas en el mes", atrasos_mes),
                                      ("Emitido por", registrado_por),
                                      ("Fecha", hoy().strftime("%d/%m/%Y"))]

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
        # Se separan los llamados formales que emite RRHH de los registros
        # automáticos de atraso: viven en la misma hoja pero no son lo mismo.
        df_disc = df_llamados[df_llamados["Tipo"].astype(str) != TIPO_TARDANZA] \
            if not df_llamados.empty and "Tipo" in df_llamados.columns else df_llamados
        df_tard = df_llamados[df_llamados["Tipo"].astype(str) == TIPO_TARDANZA] \
            if not df_llamados.empty and "Tipo" in df_llamados.columns else pd.DataFrame()

        et_disc = f"⚠️ Llamados formales ({len(df_disc)})"
        et_tard = f"⏰ Registros de atraso ({len(df_tard)})"
        vista = secciones([et_disc, et_tard], "sec_hist_llamados")
        st.divider()

        base = df_disc if vista == et_disc else df_tard
        if vista == et_tard:
            st.caption("Cada atraso queda registrado automáticamente aquí y se avisa "
                       "por correo al jefe inmediato y a RRHH. **No son sanciones**: "
                       "sirven de respaldo cuando haya que emitir un llamado formal.")

        if base.empty:
            st.info("No hay registros en esta sección.")
        else:
            c1, c2 = st.columns(2)
            with c1:
                opciones_f = ["Todos"] + [f"{r['ID_Empleado']} – {r['Nombre']}"
                                           for _, r in df_emp.iterrows()] if not df_emp.empty else ["Todos"]
                filtro_emp = st.selectbox("Filtrar por empleado", opciones_f,
                                          key="filtro_emp_hist")
            with c2:
                tipos = ["Todos"] + sorted(base["Tipo"].astype(str).unique().tolist())
                filtro_tipo = st.selectbox("Filtrar por tipo", tipos, key="filtro_tipo_hist")

            df_lf = base.copy()
            if filtro_emp != "Todos":
                eid = filtro_emp.split(" – ")[0]
                df_lf = df_lf[df_lf["ID_Empleado"].astype(str) == eid]
            if filtro_tipo != "Todos":
                df_lf = df_lf[df_lf["Tipo"].astype(str) == filtro_tipo]

            st.dataframe(df_lf, use_container_width=True, hide_index=True)
            st.caption(f"{len(df_lf)} registro(s)")

            if vista == et_disc and not df_disc.empty:
                st.divider()
                st.subheader("Resumen de llamados formales por empleado")
                resumen_l = (df_disc.groupby(["ID_Empleado", "Nombre"])["Tipo"]
                             .value_counts().unstack(fill_value=0))
                st.dataframe(resumen_l, use_container_width=True)
            elif vista == et_tard and not df_tard.empty:
                st.divider()
                st.subheader("Atrasos por empleado")
                resumen_t = (df_tard.groupby(["ID_Empleado", "Nombre"])
                             .size().reset_index(name="Atrasos registrados")
                             .sort_values("Atrasos registrados", ascending=False))
                st.dataframe(resumen_t, use_container_width=True, hide_index=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_session()

    if get_sm() is None:
        pantalla_login()
        return

    if get_usuario() is None:
        pantalla_login_empleado()
        return

    fallos = st.session_state.get("fallos_esquema") or []
    if fallos and es_admin() and not st.session_state.get("fallos_avisados"):
        st.session_state.fallos_avisados = True
        st.warning("⚠️ El sistema conectó bien, pero algunos pasos de preparación "
                   "de la hoja de cálculo fallaron. Suele ser cuota de la API de "
                   "Google: vuelve a cargar en un minuto y se completan solos.")
        with st.expander("🔍 Qué falló"):
            for f in fallos:
                st.code(f, language=None)

    registrar_entrada_automatica()

    # RRHH revisa una vez por sesión si quedaron jornadas sin salida marcada.
    if es_admin() and not st.session_state.get("salidas_revisadas"):
        st.session_state.salidas_revisadas = True
        n = revisar_salidas_pendientes(get_sm(), st.session_state.config)
        if n:
            flash("warning", f"⚠️ Se avisó a los jefes por **{n} jornada(s)** de días "
                             "anteriores que quedaron sin registrar la salida.")

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
            "Saldo Vacaciones":     page_saldo_vacaciones,
            "Riesgo Operativo":     page_riesgo_operativo,
            "Formación":            page_formacion,
            "Llamados de Atención": page_llamados_atencion,
            "Expediente":           page_expediente_empleado,
        }
    else:
        pages = {
            "Mi Asistencia":     page_asistencia,
            "Aprobaciones":      page_aprobaciones_jefe,
            "Llamados de Atención": page_llamados_atencion,
            "Mi Formación":      page_formacion,
            "Mis Permisos":      _page_permisos_con_rol,
            "Mis Vacaciones":    _page_vacaciones_con_rol,
            "Mis Horas Extra":   _page_horas_extras_con_rol,
            "Cambiar Contraseña": page_cambiar_password,
        }

    mostrar_flash()

    fn = pages.get(page)
    if fn:
        fn()
    else:
        st.error(f"Módulo '{page}' no encontrado.")


if __name__ == "__main__":
    main()
