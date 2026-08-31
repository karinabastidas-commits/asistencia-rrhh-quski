"""
app.py
Sistema de Asistencia RRHH – Quski
Aplicativo web con Streamlit + Google Sheets API
"""

import streamlit as st
import pandas as pd
import json
from datetime import date, datetime
from sheets_manager import SheetsManager

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


# ── Gestión de sesión y credenciales ─────────────────────────────────────────
def init_session():
    if "sm" not in st.session_state:
        st.session_state.sm = None
    if "config" not in st.session_state:
        st.session_state.config = {}


def get_sm() -> SheetsManager | None:
    return st.session_state.get("sm")


def conectar_sheets(creds_dict: dict):
    try:
        sm = SheetsManager(creds_dict)
        st.session_state.sm = sm
        st.session_state.config = sm.get_config()
        return True
    except Exception as e:
        import traceback
        detalle = traceback.format_exc()
        st.error(f"❌ Error al conectar: {type(e).__name__}: {e}")
        with st.expander("🔍 Detalle técnico del error"):
            st.code(detalle)
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


# ── Sidebar ───────────────────────────────────────────────────────────────────
def sidebar():
    with st.sidebar:
        st.markdown("## 🏢 Quski RRHH")
        st.markdown("---")
        page = st.selectbox(
            "Módulo",
            options=[
                "🏠  Dashboard",
                "👥  Empleados",
                "✅  Asistencia",
                "📋  Permisos",
                "🏖️  Vacaciones",
                "⏰  Horas Extras",
                "⚙️  Configuración",
            ],
            label_visibility="collapsed",
        )
        st.markdown("---")
        st.caption(f"📅 {date.today().strftime('%d/%m/%Y')}")
        if st.button("🔒 Cerrar sesión", use_container_width=True):
            st.session_state.sm = None
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
                    nombre     = st.text_input("Nombre", emp["Nombre"])
                    email      = st.text_input("Email", emp["Email"])
                    area       = st.text_input("Área", emp["Area"])
                with c2:
                    email_jefe = st.text_input("Email jefe", emp["Email_Jefe"])
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
    st.title("✅ Asistencia")
    tab1, tab2, tab3, tab4 = st.tabs(["⬆️ Registrar entrada", "⬇️ Registrar salida", "📊 Historial", "❌ Marcar ausencia"])

    df_emp = sm.get_empleados()
    hoy = date.today().strftime("%Y-%m-%d")

    def emp_opciones(df):
        if df.empty: return {}
        return {f"{r['ID_Empleado']} – {r['Nombre']}": (str(r["ID_Empleado"]), str(r["Nombre"]))
                for _, r in df.iterrows()}

    with tab1:
        st.subheader(f"Registrar entrada – {date.today().strftime('%d/%m/%Y')}")
        opciones = emp_opciones(df_emp)
        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            sel = st.selectbox("Empleado", list(opciones.keys()), key="entrada_emp")
            emp_id, nombre = opciones[sel]
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
        opciones = emp_opciones(df_emp)
        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            sel = st.selectbox("Empleado", list(opciones.keys()), key="salida_emp")
            emp_id, nombre = opciones[sel]
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

            if not df_emp.empty and "ID_Empleado" in df_filtro.columns:
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

                # Resumen
                st.markdown("**Resumen del período**")
                rc1, rc2, rc3 = st.columns(3)
                with rc1: st.metric("Total registros", len(df_filtro))
                with rc2: st.metric("Tardanzas", len(df_filtro[df_filtro["Estado"] == "Tardanza"]))
                with rc3: st.metric("Ausencias",  len(df_filtro[df_filtro["Estado"] == "Ausente"]))
        else:
            st.info("No hay registros de asistencia.")

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


# ── Módulo: Permisos ──────────────────────────────────────────────────────────
def page_permisos():
    sm = get_sm()
    config = st.session_state.config
    st.title("📋 Permisos")
    tab1, tab2 = st.tabs(["➕ Solicitar permiso", "📋 Gestionar permisos"])

    df_emp = sm.get_empleados()

    with tab1:
        limite = float(config.get("Horas_Permiso_Mensual", 3))
        st.info(f"ℹ️ Límite mensual de permisos: **{limite} horas por empleado**")

        opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                    for _, r in df_emp.iterrows()} if not df_emp.empty else {}

        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            with st.form("form_permiso", clear_on_submit=True):
                sel = st.selectbox("Empleado", list(opciones.keys()))
                emp_id = opciones[sel]
                fecha_p = st.date_input("Fecha del permiso", date.today())
                horas_p = st.number_input("Horas solicitadas", min_value=0.5, max_value=8.0, step=0.5, value=1.0)
                motivo_p = st.text_area("Motivo *")

                año_mes = fecha_p.strftime("%Y-%m")
                usadas = sm.horas_permiso_usadas_mes(emp_id, año_mes)
                disponibles = max(0, limite - usadas)
                st.caption(f"Horas usadas este mes: **{usadas:.1f}h** | Disponibles: **{disponibles:.1f}h**")

                if st.form_submit_button("📤 Enviar solicitud", type="primary"):
                    if not motivo_p.strip():
                        st.error("El motivo es obligatorio.")
                    elif horas_p > disponibles and disponibles < horas_p:
                        st.warning(f"⚠️ Supera el límite mensual ({disponibles:.1f}h disponibles). Quedará en aprobación RRHH.")
                        estado = sm.solicitar_permiso(emp_id, fecha_p.strftime("%Y-%m-%d"), horas_p, motivo_p, config)
                        st.info(f"Solicitud enviada con estado: **{estado}**")
                    else:
                        with st.spinner("Enviando…"):
                            estado = sm.solicitar_permiso(emp_id, fecha_p.strftime("%Y-%m-%d"), horas_p, motivo_p, config)
                        st.success(f"✅ Permiso solicitado – Estado: **{estado}**")

    with tab2:
        df_p = sm.get_permisos()
        if df_p.empty:
            st.info("No hay solicitudes de permisos.")
        else:
            filtro_est = st.selectbox("Filtrar por estado", ["Todos", "Pendiente_Aprobacion", "Aprobado"])
            df_pf = df_p if filtro_est == "Todos" else df_p[df_p["Estado"].astype(str) == filtro_est]
            st.dataframe(df_pf, use_container_width=True, hide_index=True)

            st.subheader("✅ Aprobar permiso")
            pend = df_p[df_p["Estado"].astype(str) == "Pendiente_Aprobacion"]
            if pend.empty:
                st.info("No hay permisos pendientes de aprobación.")
            else:
                perm_sel = st.selectbox("Permiso a aprobar",
                    pend.apply(lambda r: f"{r['ID_Permiso']} – {r['ID_Empleado']} – {r['Fecha']}", axis=1).tolist())
                perm_id = perm_sel.split(" – ")[0]
                aprobador = st.text_input("Aprobado por (nombre o email)")
                if st.button("✅ Aprobar", type="primary"):
                    if not aprobador.strip():
                        st.error("Ingresa quién aprueba el permiso.")
                    else:
                        with st.spinner("Aprobando…"):
                            sm.aprobar_permiso(perm_id, aprobador)
                        st.success(f"✅ Permiso **{perm_id}** aprobado")
                        st.rerun()


# ── Módulo: Vacaciones ────────────────────────────────────────────────────────
def page_vacaciones():
    sm = get_sm()
    st.title("🏖️ Vacaciones")
    tab1, tab2 = st.tabs(["➕ Solicitar vacaciones", "📋 Gestionar vacaciones"])

    df_emp = sm.get_empleados()
    opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                for _, r in df_emp.iterrows()} if not df_emp.empty else {}

    with tab1:
        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            with st.form("form_vac", clear_on_submit=True):
                sel = st.selectbox("Empleado", list(opciones.keys()))
                emp_id = opciones[sel]
                c1, c2 = st.columns(2)
                with c1: fecha_ini = st.date_input("Fecha de inicio")
                with c2: fecha_fin = st.date_input("Fecha de fin")

                if fecha_fin >= fecha_ini:
                    dias = sm.dias_habiles(fecha_ini.strftime("%Y-%m-%d"), fecha_fin.strftime("%Y-%m-%d"))
                    st.caption(f"📆 Días hábiles: **{dias}**")

                if st.form_submit_button("📤 Solicitar vacaciones", type="primary"):
                    if fecha_fin < fecha_ini:
                        st.error("La fecha de fin debe ser posterior a la de inicio.")
                    else:
                        with st.spinner("Enviando…"):
                            vac_id = sm.solicitar_vacaciones(
                                emp_id, fecha_ini.strftime("%Y-%m-%d"), fecha_fin.strftime("%Y-%m-%d")
                            )
                        st.success(f"✅ Vacaciones solicitadas: **{vac_id}** – Estado: Pendiente")

    with tab2:
        df_v = sm.get_vacaciones()
        if df_v.empty:
            st.info("No hay solicitudes de vacaciones.")
        else:
            filtro_v = st.selectbox("Filtrar por estado", ["Todos", "Pendiente", "Aprobado"])
            df_vf = df_v if filtro_v == "Todos" else df_v[df_v["Estado"].astype(str) == filtro_v]
            st.dataframe(df_vf, use_container_width=True, hide_index=True)

            st.subheader("✅ Aprobar vacaciones")
            pend_v = df_v[df_v["Estado"].astype(str) == "Pendiente"]
            if pend_v.empty:
                st.info("No hay vacaciones pendientes.")
            else:
                vac_sel = st.selectbox("Solicitud a aprobar",
                    pend_v.apply(lambda r: f"{r['ID_Vacacion']} – {r['ID_Empleado']} ({r['Fecha_Inicio']} → {r['Fecha_Fin']})", axis=1).tolist())
                vac_id_ap = vac_sel.split(" – ")[0]
                aprobador_v = st.text_input("Aprobado por")
                if st.button("✅ Aprobar vacaciones", type="primary"):
                    if not aprobador_v.strip():
                        st.error("Ingresa quién aprueba.")
                    else:
                        with st.spinner("Aprobando…"):
                            sm.aprobar_vacaciones(vac_id_ap, aprobador_v)
                        st.success(f"✅ Vacaciones **{vac_id_ap}** aprobadas")
                        st.rerun()


# ── Módulo: Horas Extras ──────────────────────────────────────────────────────
def page_horas_extras():
    sm = get_sm()
    st.title("⏰ Horas Extras")
    tab1, tab2 = st.tabs(["➕ Registrar horas extra", "📋 Gestionar horas extra"])

    df_emp = sm.get_empleados()
    opciones = {f"{r['ID_Empleado']} – {r['Nombre']}": str(r["ID_Empleado"])
                for _, r in df_emp.iterrows()} if not df_emp.empty else {}

    with tab1:
        if not opciones:
            st.warning("No hay empleados registrados.")
        else:
            with st.form("form_hex", clear_on_submit=True):
                sel = st.selectbox("Empleado", list(opciones.keys()))
                emp_id = opciones[sel]
                fecha_hex = st.date_input("Fecha", date.today())
                horas_hex = st.number_input("Horas extra trabajadas", min_value=0.5, max_value=12.0, step=0.5, value=1.0)
                motivo_hex = st.text_area("Motivo / justificación *")

                if st.form_submit_button("💾 Registrar horas extra", type="primary"):
                    if not motivo_hex.strip():
                        st.error("El motivo es obligatorio.")
                    else:
                        with st.spinner("Registrando…"):
                            sm.registrar_horas_extra(emp_id, fecha_hex.strftime("%Y-%m-%d"), horas_hex, motivo_hex)
                        st.success(f"✅ Horas extra registradas para aprobación")

    with tab2:
        df_he = sm.get_horas_extras()
        if df_he.empty:
            st.info("No hay registros de horas extra.")
        else:
            filtro_he = st.selectbox("Filtrar por estado", ["Todos", "Pendiente", "Aprobado"])
            df_hef = df_he if filtro_he == "Todos" else df_he[df_he["Estado"].astype(str) == filtro_he]
            st.dataframe(df_hef, use_container_width=True, hide_index=True)

            st.subheader("✅ Aprobar horas extra")
            pend_he = df_he[df_he["Estado"].astype(str) == "Pendiente"]
            if pend_he.empty:
                st.info("No hay horas extra pendientes de aprobación.")
            else:
                hex_sel = st.selectbox("Registro a aprobar",
                    pend_he.apply(lambda r: f"{r['ID']} – {r['ID_Empleado']} – {r['Fecha']} ({r['Horas_Extra']}h)", axis=1).tolist())
                hex_id = hex_sel.split(" – ")[0]
                aprobador_he = st.text_input("Aprobado por")
                if st.button("✅ Aprobar", type="primary"):
                    if not aprobador_he.strip():
                        st.error("Ingresa quién aprueba.")
                    else:
                        with st.spinner("Aprobando…"):
                            sm.aprobar_hora_extra(hex_id, aprobador_he)
                        st.success(f"✅ Horas extra **{hex_id}** aprobadas")
                        st.rerun()


# ── Módulo: Configuración ─────────────────────────────────────────────────────
def page_configuracion():
    sm = get_sm()
    config = st.session_state.config
    st.title("⚙️ Configuración del sistema")

    with st.form("form_config"):
        st.subheader("Horarios")
        c1, c2 = st.columns(2)
        with c1:
            h_ini = st.time_input("Horario de entrada",
                datetime.strptime(config.get("Horario_Inicio","09:00")[:5], "%H:%M").time())
            tolerancia = st.number_input("Tolerancia (minutos)",
                min_value=0, max_value=30, value=int(float(config.get("Tolerancia_Minutos",0))))
        with c2:
            h_fin = st.time_input("Horario de salida",
                datetime.strptime(config.get("Horario_Fin","17:30")[:5], "%H:%M").time())
            horas_perm = st.number_input("Horas de permiso mensual",
                min_value=1.0, max_value=20.0, step=0.5, value=float(config.get("Horas_Permiso_Mensual",3)))

        st.subheader("Contacto y zona horaria")
        email_rrhh = st.text_input("Email RRHH", config.get("Email_RRHH","rrhh@quski.ec"))
        zona = st.selectbox("Zona horaria",
            ["America/Guayaquil", "America/Bogota", "America/Lima", "America/New_York", "America/Mexico_City"],
            index=["America/Guayaquil","America/Bogota","America/Lima","America/New_York","America/Mexico_City"]
                    .index(config.get("Zona_Horaria","America/Guayaquil")))

        if st.form_submit_button("💾 Guardar configuración", type="primary"):
            updates = {
                "Horario_Inicio":        h_ini.strftime("%H:%M"),
                "Horario_Fin":           h_fin.strftime("%H:%M"),
                "Tolerancia_Minutos":    str(tolerancia),
                "Horas_Permiso_Mensual": str(horas_perm),
                "Email_RRHH":            email_rrhh,
                "Zona_Horaria":          zona,
            }
            with st.spinner("Guardando…"):
                for k, v in updates.items():
                    sm.set_config(k, v)
                st.session_state.config = sm.get_config()
            st.success("✅ Configuración guardada")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_session()

    if get_sm() is None:
        pantalla_login()
        return

    page = sidebar()

    pages = {
        "Dashboard":      page_dashboard,
        "Empleados":      page_empleados,
        "Asistencia":     page_asistencia,
        "Permisos":       page_permisos,
        "Vacaciones":     page_vacaciones,
        "Horas Extras":   page_horas_extras,
        "Configuración":  page_configuracion,
    }

    fn = pages.get(page)
    if fn:
        fn()
    else:
        st.error(f"Módulo '{page}' no encontrado.")


if __name__ == "__main__":
    main()
