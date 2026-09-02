# Sistema de Asistencia RRHH – Quski

Aplicativo web en Python/Streamlit conectado a Google Sheets.

---

## 📁 Archivos

| Archivo | Descripción |
|---|---|
| `app.py` | Aplicativo principal (Streamlit) |
| `sheets_manager.py` | Módulo de acceso a Google Sheets |
| `requirements.txt` | Dependencias Python |

---

## ⚙️ Instalación local (opcional)

```bash
pip install -r requirements.txt
streamlit run app.py
```

En producción el aplicativo corre en **Streamlit Community Cloud** y toma las
credenciales de los *Secrets*, no de un archivo subido a mano.

---

## 🔑 Secrets de Streamlit Cloud

En *Manage app → Settings → Secrets*:

```toml
[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "...@....gserviceaccount.com"
client_id = "..."
token_uri = "https://oauth2.googleapis.com/token"

[email]
smtp_server   = "smtp.gmail.com"
smtp_port     = "587"
smtp_user     = "karina.bastidas@quski.ec"
smtp_password = "xxxx xxxx xxxx xxxx"   # Contraseña de Aplicación de Google
```

El spreadsheet debe estar compartido como **Editor** con el `client_email`
del service account.

---

## 🗂️ Módulos

| Módulo | Quién lo ve | Funciones |
|---|---|---|
| **Dashboard** | RRHH | KPIs del día, asistencia, solicitudes pendientes |
| **Empleados** | RRHH | Crear, listar y editar empleados |
| **Asistencia** | Todos | Registrar entrada/salida, ausencias, historial |
| **Permisos** | Todos | Solicitar; RRHH aprueba en segunda instancia |
| **Vacaciones** | Todos | Solicitar; calcula días hábiles |
| **Aprobaciones** | Jefes | Bandeja con las solicitudes del equipo a cargo |
| **Horas Extras** | Todos | Registrar y aprobar horas extra |
| **Llamados de Atención** | RRHH | Emitir llamados; notifica al empleado y a su jefe |
| **Expediente** | RRHH | Historial consolidado por empleado |
| **Configuración** | RRHH | Horarios, tolerancia, email RRHH, política de tardanzas |
| **Gestión Usuarios** | RRHH | Crear cuentas y asignar roles |

---

## ✅ Flujo de aprobación

Permisos y vacaciones pasan por **dos firmas**:

```
Empleado solicita
      ↓         → correo al jefe inmediato y a RRHH
Pendiente_Jefe
      ↓         → el jefe aprueba en su módulo "Aprobaciones",
                  o RRHH registra la autorización que dio por otro medio
Pendiente_RRHH
      ↓         → correo a RRHH
Aprobado        → correo al empleado y al jefe
```

Un rechazo en cualquier etapa exige motivo y se notifica a todos.

**El jefe inmediato se determina por el campo `Email_Jefe`** de la hoja
Empleados. Ese correo debe coincidir exactamente con el `Email` del jefe en su
propia fila de la hoja Empleados; si no coincide, el jefe no verá las
solicitudes de su equipo. Si un empleado no tiene `Email_Jefe`, su solicitud va
directo a RRHH.

---

## 📊 Spreadsheet

ID: `1gaPrP95SF0xat7xRs94CMyH22LeIoolEom1OjCDAA2I`

Encabezados esperados en la fila 1 de cada hoja:

- **Empleados**: ID_Empleado · Nombre · Email · Area · Email_Jefe · Horario_Inicio · Horario_Fin
- **Asistencia**: Fecha · ID_Empleado · Nombre · Hora_Entrada · Hora_Salida · Estado · Minutos_Atraso · Observaciones
- **Permisos**: ID_Permiso · Fecha · ID_Empleado · Horas_Solicitadas · Motivo · Estado · Horas_Usadas_Mes · Aprobado_Por · Aprobado_Jefe · Fecha_Aprob_Jefe · Aprobado_RRHH · Fecha_Aprob_RRHH · Motivo_Rechazo · Rechazado_Por
- **Vacaciones**: ID_Vacacion · ID_Empleado · Fecha_Inicio · Fecha_Fin · Dias_Habiles · Estado · Aprobado_Por · Aprobado_Jefe · Fecha_Aprob_Jefe · Aprobado_RRHH · Fecha_Aprob_RRHH · Motivo_Rechazo · Rechazado_Por
- **Horas_Extras**: ID · Fecha · ID_Empleado · Horas_Extra · Motivo · Aprobado_Por · Estado
- **Configuracion**: Key · Valor
- **Usuarios**: ID_Empleado · Password_Hash · Rol
- **Llamados_Atencion**: ID_Llamado · Fecha · ID_Empleado · Nombre · Tipo · Motivo · Atrasos_Acumulados · Registrado_Por · Estado

Las columnas de aprobación (`Aprobado_Jefe` en adelante) las **agrega el propio
aplicativo** la primera vez que alguien inicia sesión después de actualizar.
No hay que crearlas a mano.

Estados posibles en Permisos y Vacaciones: `Pendiente_Jefe`, `Pendiente_RRHH`,
`Aprobado`, `Rechazado`.

---

## 🔒 Seguridad

- Las contraseñas se guardan con **PBKDF2-HMAC-SHA256 y salt aleatorio**. Los
  hashes antiguos (SHA-256 simple) se aceptan y se migran solos en el primer
  inicio de sesión de cada persona.
- Política mínima: 8 caracteres, con letras y al menos un número o símbolo.
- Cinco intentos fallidos bloquean el acceso durante 5 minutos.
- El texto que escriben los empleados se escapa antes de incluirlo en los
  correos, para que nadie pueda insertar enlaces o código en una notificación.
- **Nunca** poner contraseñas ni credenciales en el código: van en los Secrets.

---

## 🚀 Actualizar el aplicativo

1. Respaldar el spreadsheet (*Archivo → Hacer una copia*).
2. Subir los archivos modificados al repositorio de GitHub.
3. En Streamlit Cloud: *Manage app → Reboot app*.
4. Entrar como administrador para que se apliquen las migraciones de columnas.
