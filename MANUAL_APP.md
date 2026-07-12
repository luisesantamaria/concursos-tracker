# Manual de construcción de la aplicación — Concursos Tracker

**Fecha**: 12-jul-2026 · **Documento hermano**: `MANUAL_IMPLEMENTACION.md` (el
motor de datos / 4 planos). Este manual cubre el **producto**: el portal/app
que consume el motor. Paso a paso, con los desafíos reales de cada etapa, cómo
resolverlos y qué tecnología usar (con criterio: aburrida, barata y que un
equipo de 1 persona + agentes IA pueda mantener).

**Principio rector**: el motor de datos es el foso; la app es la vitrina. Toda
decisión de la app debe minimizar el tiempo robado al motor. Por eso: stack
convencional, cero microservicios, un solo Postgres, y nada nativo hasta que
haya usuarios que lo pidan.

---

## Etapa 0 — Decisiones previas (1 día, papel y lápiz)

Antes de escribir una línea del producto, fijar por escrito:

| Decisión | Recomendación | Por qué |
|---|---|---|
| Alcance del MVP | Solo RS, solo consulta + alertas por email | Es donde el motor ya tiene datos con provenance |
| Modelo de negocio inicial | Gratis en beta; premium después (alertas instantáneas/filtros avanzados) | Primero validar retención, no cobrar |
| Web vs app nativa | **Web responsive + PWA** (instalable, push) | Una base de código; las stores llegan después con un wrapper |
| Idioma | pt-BR desde el día 1 | El usuario es brasileño; no traducir después |
| Nombre/dominio | registrar .com.br temprano | LGPD y confianza exigen identidad seria |

**Desafío oculto**: la tentación de "hacerlo para todo Brasil de una". El MVP
con RS completo y fresco vale más que un cascarón nacional vacío — la densidad
de datos por región es el producto.

---

## Etapa 1 — La base de datos compartida (semana 1)

El motor (Python) y la app comparten UNA base de datos: **PostgreSQL 16 +
PostGIS**. El motor escribe (certames, documentos, eventos, fuentes), la app
lee y escribe solo lo suyo (usuarios, suscripciones, notificaciones).

**Qué construir**:
1. Migrar el modelo canónico (MANUAL_IMPLEMENTACION §3) a esquema SQL:
   `municipio` (con `geom` PostGIS + código IBGE), `orgao`, `banca`, `fuente`,
   `certame`, `documento`, `evento`, `cargo`, `mencion`.
2. Tablas de producto: `usuario`, `perfil` (escolaridade, profissão, cidade
   base, radio_km, salario_min), `suscripcion` (certame seguido / búsqueda
   guardada), `notificacion` (cola + historial), `feedback` (reporte de dato
   erróneo — ver Etapa 8).
3. Cargar la tabla IBGE de municipios (código, nombre, UF, lat/lon del seat):
   dataset público del IBGE; son ~5.570 filas, se carga una vez.
4. Alembic para migraciones desde el día 1 (el esquema VA a cambiar).

**Herramientas**: Postgres 16 + PostGIS, SQLAlchemy 2 + Alembic (el motor ya
es Python), `ibge`-datasets públicos.

**Desafíos y soluciones**:
- *El motor hoy escribe CSVs, no DB* → escribir un `loader` idempotente
  CSV→Postgres (upsert por claves naturales) como puente; migrar el motor a
  escribir directo a DB solo cuando el esquema se estabilice. No bloquear la
  app esperando refactor del motor.
- *Ambigüedad municipio-nombre* (hay ~5 "Bom Jesus" en Brasil) → TODO se
  referencia por **código IBGE**, nunca por nombre. El motor RS debe mapear
  sus 497 nombres a códigos IBGE una sola vez (tabla de correspondencia).
- *Datos con distinta confianza* → columna `provenance`/`estado_verificacion`
  en todo lo visible: la app SOLO muestra filas `confirmado`; lo `revisar`
  jamás llega al usuario.

---

## Etapa 2 — API del catálogo (semana 2)

**Qué construir**: API REST de solo lectura sobre el catálogo.

- `GET /certames?uf=RS&municipio=&radio_km=&escolaridade=&salario_min=&tipo=&estado=abierto` — el endpoint central. Paginado, ordenado por fecha de publicación/cierre.
- `GET /certames/{id}` — detalle completo: cargos, documentos, timeline de eventos, fuentes con enlaces.
- `GET /municipios?q=` — autocompletado con código IBGE.

**Herramientas**: **FastAPI** + Pydantic (mismo lenguaje del motor, validación
gratis, OpenAPI automático para el frontend), uvicorn.

**Desafíos y soluciones**:
- *Filtro geográfico por radio* → PostGIS: `ST_DWithin(municipio.geom,
  usuario.geom, radio_km)`. Indexar con GiST. Nota: usar la sede del municipio
  es suficiente para el MVP; no meterse con polígonos.
- *Filtro por escolaridade* → taxonomía cerrada ANTES de exponer el filtro:
  `fundamental / medio / tecnico / superior / pos` + área. El extractor (Plano
  C) debe normalizar a esa taxonomía; si el edital es ambiguo, el cargo lleva
  `escolaridade=null` y APARECE en búsquedas amplias (no ocultar por dato
  faltante — sesgo silencioso).
- *"Cerca de mi localidad"* también significa **estatales y federales** con
  vagas en la región → el certame necesita `ambito` (municipal/estatal/
  federal) y los cargos `cidade_lotacao`; el matching une ambos caminos.

---

## Etapa 3 — Frontend web (semanas 3-4)

**Qué construir**: 4 pantallas, nada más.
1. **Home/búsqueda**: buscador por ciudad + filtros (escolaridade, salario,
   tipo, estado del certame). Lista de resultados con lo esencial: órgão,
   cargos-resumen, salario máximo, deadline de inscripción, badge de fuente
   verificada.
2. **Detalle del certame**: timeline de eventos, tabla de cargos, TODOS los
   documentos enlazados a su fuente oficial, botón "seguir este certame".
3. **Mi perfil**: escolaridade, profissão, ciudad + radio, salario mínimo,
   preferencias de notificación.
4. **Mis alertas**: certames seguidos + matches del perfil, con lo nuevo
   resaltado.

**Herramientas**: **Next.js 15 + Tailwind**, desplegado como PWA (manifest +
service worker → instalable en Android/iOS, web push en Android). shadcn/ui
para componentes. Nada de app nativa todavía.

**Desafíos y soluciones**:
- *Confianza visual* → cada dato muestra su fuente ("Publicado no site da
  Prefeitura de X el 12/07 — ver original"). El estándar de evidencia del
  motor se vuelve UX: es el diferenciador frente a los agregadores que no
  enlazan la fuente.
- *SEO* (la adquisición orgánica de este nicho es Google: "concurso prefeitura
  X 2026") → páginas de certame server-rendered (Next lo da gratis), sitemap
  por certame, datos estructurados schema.org/JobPosting. Esto es marketing
  gratuito permanente — no saltárselo.
- *iOS y push* → Safari soporta web push solo con PWA instalada (iOS 16.4+);
  aceptar email como canal primario en iOS hasta tener wrapper nativo.

---

## Etapa 4 — Cuentas y perfiles (semana 4)

**Herramientas**: better-auth o Supabase Auth (email + Google). NO construir
auth a mano.

**Desafíos y soluciones**:
- *Fricción de registro* → el buscador y el detalle son PÚBLICOS; la cuenta
  solo se pide para seguir/alertas. Regla: valor antes de registro.
- *LGPD desde el diseño* (perfil = dato personal; escolaridade/ciudad son
  sensibles en agregado): consentimiento explícito de finalidad, export y
  borrado de cuenta self-service, política de privacidad simple. Base legal:
  ejecución de servicio solicitado. No vender datos. Esto es 1 día de trabajo
  ahora o un problema legal después.

---

## Etapa 5 — Matching y alertas (semanas 5-6) — el corazón del producto

**Qué construir**:
1. **Matcher**: job que cruza (perfil × certames nuevos/actualizados) →
   `match(usuario, certame, score_reglas)`. Sin ML: reglas explícitas
   (escolaridade compatible + geografía dentro del radio o dispuesto-a-mudarse
   + salario ≥ mínimo). Determinista y explicable ("te avisamos porque:
   superior + a 40km + R$4.500").
2. **Eventos → notificaciones**: cuando el Plano B/C emite un evento
   (nuevo edital, retificação, convocação, cierre de inscripción en 5 días),
   generar notificaciones para: seguidores del certame + perfiles que matchean.
3. **Canales**: email (digest diario + instantáneo para deadline/convocação) y
   web push. WhatsApp NO en el MVP (API de Meta = costo + fricción de
   aprobación; evaluarlo cuando haya retención probada).

**Herramientas**: jobs con **APScheduler o Celery + Redis** (empezar con
APScheduler: menos piezas); email con **Amazon SES o Resend** (dominio propio
verificado, SPF/DKIM desde el día 1); web push con VAPID estándar.

**Desafíos y soluciones**:
- *El desafío nº1 del producto entero: frescura vs ruido*. Un aviso tardío no
  sirve; diez avisos irrelevantes = unsubscribe. Solución: (a) SLA del motor
  de detección 24-48h (Plano B); (b) digest por defecto, instantáneo solo
  para lo crítico (apertura que matchea, deadline, convocação con tu nombre…
  futuro); (c) preferencias granulares desde el día 1.
- *Deliverability de email* → dominio dedicado, warm-up gradual, lista solo
  opt-in, link de unsubscribe de un clic. Medir bounce/open desde el primer
  envío.
- *Idempotencia* → tabla `notificacion` con clave única (usuario, certame,
  evento): un evento re-procesado NUNCA re-notifica.

---

## Etapa 6 — Búsqueda (cuando el catálogo pase ~2.000 certames)

Postgres full-text (tsvector pt-BR) alcanza de sobra para el MVP. Si la UX de
búsqueda se vuelve central (typo-tolerance, facetas rápidas), migrar a
**Meilisearch** (un binario, se sincroniza con un job). No usar Elasticsearch
(sobredimensionado para 1 persona).

---

## Etapa 7 — Infraestructura y deploy

**Topología recomendada (2 máquinas)**:
1. **VPS Brasil** (São Paulo — Hetzner no tiene BR; usar Contabo BR, Hostinger
   BR o AWS Lightsail sa-east-1): corre el MOTOR (scraping/monitoreo — los
   sitios municipales geo-bloquean tráfico no-BR, ya documentado en RUNBOOK) +
   Postgres. 4-8GB RAM bastan para arrancar.
2. **App**: Vercel (Next.js) + el FastAPI en el mismo VPS detrás de Caddy, o
   todo en el VPS si se prefiere una sola factura. CDN de Vercel/Cloudflare
   para lo estático.

**Herramientas**: Docker Compose (motor, api, db, redis), Caddy (TLS
automático), backups diarios de Postgres a S3/B2 (**probar el restore** una
vez al mes — un backup no probado no existe).

**Costo estimado MVP**: VPS ~US$15-30/mes + Vercel free/20 + SES centavos +
dominio. Total < US$60/mes. El costo de IA del motor va aparte (ver análisis
de escala) pero con flash-lite pagado es del orden de decenas de dólares por
corrida estatal completa, no cientos.

---

## Etapa 8 — Calidad de datos EN producción (continuo, empieza en beta)

El estándar 0-FP del motor necesita su contraparte de producto:

1. **Botón "reportar error"** en cada certame/dato → tabla `feedback` → cola
   de revisión humana. Los usuarios son el auditor distribuido gratis; cada
   reporte confirmado se convierte en corrección + (si aplica) patrón
   promovido al motor (v2/memory).
2. **Dashboard de frescura**: por fuente, fecha del último check y del último
   cambio detectado; alarma si una fuente activa lleva >72h sin check.
3. **Página de estado público** ("cubrimos X municipios de RS, actualizado
   hace Y horas") — transparencia = confianza = el diferenciador.
4. **Métricas de producto que importan** (no vanidad): % de alertas abiertas,
   unsubscribes por tipo de alerta, reportes de error por 1.000 vistas,
   retención semana 4.

---

## Etapa 9 — De RS a Brasil (después de la beta, no antes)

Gatillo para expandir: retención semana-4 > 20% en RS y NPS de confianza
positivo. La expansión es del MOTOR (demand-driven, MANUAL_IMPLEMENTACION
F6); la app solo necesita: selector de UF, y que el matcher ya es nacional
por diseño (código IBGE + PostGIS). No hay re-arquitectura de producto.

---

## Resumen del stack (una tabla para no perderse)

| Capa | Elección | Alternativa si duele |
|---|---|---|
| DB | Postgres 16 + PostGIS | — (no negociable) |
| ORM/migraciones | SQLAlchemy 2 + Alembic | — |
| API | FastAPI | — |
| Jobs | APScheduler | Celery+Redis al crecer |
| Frontend | Next.js 15 + Tailwind + shadcn, PWA | wrapper Capacitor para stores |
| Auth | better-auth / Supabase Auth | — |
| Email | SES o Resend | — |
| Push | Web Push (VAPID) | FCM vía wrapper nativo |
| Búsqueda | Postgres FTS | Meilisearch |
| Infra | VPS BR + Docker Compose + Caddy; Vercel para el front | todo-en-VPS |
| Observabilidad | Grafana/Uptime-Kuma + logs estructurados | — |

## Orden de construcción y criterios de salida

1. **Sprint 1-2 (Etapas 1-2)**: DB + loader CSV→Postgres + API catálogo. *Salida: `GET /certames?uf=RS` devuelve los confirmados reales del motor.*
2. **Sprint 3-4 (Etapa 3)**: web pública consultable. *Salida: buscar "Canoas, superior, R$3.000+" y ver certames reales con fuentes enlazadas.*
3. **Sprint 5 (Etapa 4)**: cuentas + perfil. *Salida: registro <2 min, LGPD ok.*
4. **Sprint 6-7 (Etapa 5)**: matcher + email + push. *Salida: usuario de prueba recibe alerta real <48h de la publicación real.*
5. **Beta cerrada RS** (~50 usuarios reales, grupos de concurseiros de RS): medir Etapa 8. *Salida: retención y tasa de error que justifiquen expandir.*

**La dependencia crítica**: los sprints 1-2 pueden empezar HOY con el CSV
canónico de RS existente. Los sprints 6-7 dependen de que el Plano B
(monitoreo/eventos) exista — por eso en el motor, tras cerrar F2, el Plano B
es la prioridad, no la expansión geográfica.
