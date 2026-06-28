# Concursos Tracker — Handoff completo de sesión (web → local Windows)

> **Qué es este archivo.** Es la transferencia literal de una sesión larga de
> Claude Code (web) a una sesión local nueva en Windows. Contiene TODO: contexto,
> meta, arquitectura, dónde vamos, el porqué de cada decisión, cómo correr, y las
> reglas no negociables. Si eres la sesión local que acaba de abrir esto: **léelo
> entero antes de tocar nada**, y también lee `CLAUDE.md` y `AGENTS.md` en la raíz.

---

## 0. Cómo trabajar a partir de ahora (lee esto primero)

- **Eres ahora la única fuente de verdad.** Antes el trabajo estaba partido: una
  sesión web editaba código y commiteaba, y un entorno local corría el scraping y
  pegaba los resultados de vuelta. Eso se acabó. **Tú haces las dos cosas**: editas
  código, lo commiteas, lo pusheas a GitHub, corres el pipeline/auditor localmente
  (estás en Brasil, sin geo-block), y actúas sobre los outputs. Sin relevos.
- **Repo:** `https://github.com/luisesantamaria/concursos-tracker`
- **Rama de trabajo:** `claude/skill-files-accuracy-vd6uyt` (TODO está aquí; nunca
  trabajes en `main`).
- **Flujo git no negociable:** `git pull` antes de cada corrida (correr siempre
  sobre código corregido), y `git push` después de cada cambio de código o
  corrección de datos. Mensajes de commit claros y descriptivos.
- **Idioma:** el usuario escribe en español; respóndele en español. El código y los
  commits en inglés está bien.

---

## 1. Identidad del proyecto y meta

**Concursos Tracker** es un crawler y pipeline de validación *authority-first* para
concursos públicos y processos seletivos simplificados (PSS) de **Rio Grande do Sul
(RS), Brasil**.

Principio de autoridad de fuentes (orden):
1. **Banca organizadora** (autoritativa para el ciclo de vida del concurso activo).
2. **Prefeitura / sitio oficial del órgano** (municipales y eventos post-resultado).
3. **Diário / FAMURS / portales de publicación.**
4. **Portales radar** (Ache, PCI, QConcursos…) solo para descubrir/auditar, **nunca
   como prueba final**.

**Meta global:** construir una base confiable de concursos/PSS de RS. Se hace por
fases. **No inventes datos**: si un campo no está en la fuente, queda vacío.

---

## 2. Fase actual y estado EXACTO (dónde vamos)

### Fase actual: Descubrimiento de índices municipales (FASE 2)

Encontrar la **página índice/listado estable** de concursos y de PSS en cada
municipio de RS. **Esta fase NO extrae editais/PDFs individuales** — eso es fase 3.

- **Qué es válido en esta fase:** página índice / categoría / portal que lista
  *varios* eventos; página padre desde la que se navega a editais.
- **Qué se rechaza:** PDF directo; página de edital individual; noticia de un solo
  concurso; anexo/cronograma/retificação; licitação/pregão/chamamento; concurso
  cultural (soberanas/rainhas).

### Estado numérico actual (497 municipios de RS, corrida completa hecha)

| Estado | # | % |
|---|---|---|
| ✅ Confirmado pleno (ambos buckets) | 347 | 69.8% |
| 🟡 Parcial / revisar | 100 | 20.1% |
| ⚪ Sin resultado | 50 | 10.1% |

- **Precisión sobre confirmados: ~99.5% (medida-hasta-ahora).** Se auditaron 768
  URLs confirmadas con métodos determinísticos + scan cross-tipo + muestra manual,
  y se hallaron y corrigieron **4 falsos positivos** (Erechim, São José do
  Inhacorá, Três Coroas, Cerro Largo — todos del patrón "bucket apuntando al índice
  del tipo opuesto", bajados a `revisar`).
- **Golden set gate:** automatable **WRNG=0 / F-POS=0** (cero URLs inventadas).
- **Geo-block ausente** corriendo desde Brasil; solo ~1.6% de las 497 son bloqueos
  de red reales (Cloudflare/SSL/antibot).
- **Auditor validado contra verdad de campo (golden):** **0 falsos HARD sobre 46
  URLs golden verificadas a mano → auditor CONFIABLE.** Sus `OK` se pueden confiar
  sin abrir cada página; sus `HARD` sobre el dataset completo serían FP reales.

### TAREA PENDIENTE INMEDIATA (lo próximo que hay que hacer)

**Correr la auditoría completa `--render --ai-all` sobre las 497** (los ~760
confirmados). Es la pieza que convierte el 99.5% de "medido-parcial" a
"auditado-al-100%". Los `HARD` que salgan serán **FP semánticos reales** → bajarlos
a `revisar` en el CSV y commitear. Es larga (render + 1 llamada Gemini por URL,
~1.5–2.5 h) y gasta ~760 llamadas Gemini (centavos-pocos dólares).

```bash
python scripts/eval/audit_fase2_rs.py \
  --input data/fase2/municipios_rs_local.csv \
  --render --ai-all --detalle
```

Tras eso: corregir los FP reales → fase 2 **cerrada** → arrancar **fase 3**
(scanner de índices que entra a cada página confirmada y extrae editais/PDFs).

---

## 3. ⚠️ ESTADO DE LOS DATOS — leer con atención

El CSV acumulado de las 497 (`data/fase2/municipios_rs_local.csv`, ~497 filas, +
su `.xlsx` y el `*_auditoria.csv`) **NO está en GitHub.** Razones: `data/` está en
`.gitignore` (regla de seguridad: no commitear outputs generados) y los `git push`
desde el entorno local viejo fallaban por falta de credenciales.

**Acción requerida en el setup:** localiza ese CSV en tu clon/entorno anterior
(ruta `data/fase2/municipios_rs_local.csv`) y **cópialo** dentro de la carpeta nueva
en `data/fase2/`. Si no lo tienes a mano, hay que **regenerarlo** corriendo el
pipeline completo (ver §7, ~4 horas + Gemini). **No empieces la fase 3 ni la
auditoría completa sin este CSV.**

Cuando tengas acceso de push configurado, considera commitearlo como excepción
puntual para no volver a perderlo:
```bash
git add -f data/fase2/municipios_rs_local.csv
git commit -m "Snapshot fase 2: 497 municipios RS"
git push -u origin claude/skill-files-accuracy-vd6uyt
```

---

## 4. Arquitectura: cascada de 5 tiers

El pipeline gasta herramientas caras solo cuando las baratas fallan:

- **Tier 0 — Site oficial:** encuentra/confirma el dominio de la prefeitura
  (adivina slugs + fallback con Gemini grounding).
- **Tier 1 — Links gratis:** sigue menús HTML, anchors, sitemap, transparência.
  Puro requests, sin IA. **Renderiza con navegador si la home es un shell SPA.**
- **Tier 2 — Grounded search:** Gemini + Google Search, una llamada por municipio,
  solo si Tier 1 quedó incompleto.
- **Tier 3 — Verificador/selector Gemini:** decisiones discretas (no scores);
  elige el mejor índice entre candidatos válidos (`ai_pick_best`).
- **Tier 4 — Agente de navegación (Playwright):** último recurso, navega menús por
  texto (no crawling ciego), para portales JS/IP impredecibles.

### Reglas críticas de arquitectura
- **NO usar scorers numéricos** (score=85, etc.) para elegir entre candidatos. Usar
  **decisiones discretas** (`indice_oficial`, `indice_oficial_combinado`,
  `detalle_individual_rechazado`, `licitacao_rechazada`, `nao_encontrado`,
  `revisar`…) y `ai_pick_best` cuando varios son válidos.
- **NO hardcodear patrones de portal** (multi24, secao=dinamico, IPs específicas).
  Cada regla arregla un portal y rompe el siguiente. Si es específico de proveedor,
  que lo maneje la IA o acepta `revisar`.
- **Precisión sobre cobertura. Cero falsos positivos.** Un 3/5 todo correcto es
  mejor que 5/5 con uno mal. ~20% de revisión humana es aceptable.

---

## 5. Mapa de archivos / scripts

```
scripts/
  fase1_bancas/          # FASE 1 (ya lista): crawlers de bancas
    crawl_bancas_base_rs.py     # crawler de 15 bancas (Legalle, Fundatec, FGV…)
    ai_repair_bancas_rs.py      # post-proceso con IA (corrige campos)
    quick_audit_bancas_rs.py    # auditor determinístico de bancas
  fase2_municipios/      # FASE 2 (actual)
    cascade_municipios_rs.py    # EL pipeline de 5 tiers (el corazón)
  eval/
    medir_golden_set.py         # evaluador vs golden (HIT/HOST/WRNG/MISS/F-POS)
    audit_fase2_rs.py           # AUDITOR de confirmados (det + --render + --ai)
    validate_golden_audit.py    # valida el auditor contra el golden (verdad campo)
  shared/
    scope_rs.py                 # registro RS, normalización, guard de scope

authority_first/
  data/golden_set_v1.csv        # 24 municipios verificados a MANO (verdad de campo)
  docs/RUNBOOK_corridas_locales.md  # runbook de corridas + monitoreo (LÉELO)

data/fase2/                     # outputs (gitignored; ver §3)
CLAUDE.md  AGENTS.md  README.md # instrucciones del proyecto (LÉELAS)
HANDOFF_LOCAL.md                # este archivo
```

---

## 6. El AUDITOR (`audit_fase2_rs.py`) — el QA y monitor permanente

Responde la pregunta "¿cómo sé que no hay FP escondidos, hoy y a futuro?".
Re-baja cada URL **confirmada** y la clasifica en **OK / SOFT / HARD**, escribiendo
`<input>_auditoria.csv` con solo los sospechosos. **No toca la lógica del pipeline.**

Tres niveles (de barato a inteligente):

| Modo | Qué hace | Cuándo |
|---|---|---|
| (sin flags) | Estructural: PDF, muerta (4xx), ruta de detalle/noticia, sin keyword | Chequeo rápido / link rot |
| `--render` | Abre páginas JS en navegador real (atende.net, oxy.elotech) y juzga el contenido renderizado | Verificar portales JS |
| `--ai` | Gemini da un verdicto **discreto** (valido_indice / tipo_equivocado / nao_e_indice / licitacao_ou_cultural) solo a los dudosos | Limpiar SOFT/HARD barato |
| `--ai-all` | Gemini revisa **cada** confirmado → caza FP semánticos (tipo legal equivocado) escondidos entre los OK | Auditoría completa |

Salidas de severidad:
- **HARD** = problema casi seguro (solo se afirma cuando vimos el contenido real:
  render exitoso o estático ya rico). Sobre el dataset, un HARD = FP real → bajar a
  `revisar`.
- **SOFT** = no verificable sin más (antibot, muro de cookies, low-confidence) →
  ojo humano opcional.

### Salvaguardas que ya tiene (no las quites, costaron varias iteraciones):
1. **Combinadas válidas:** una página que lista concursos Y PSS es `valido_indice`
   para cualquier bucket (si no, ~140 combinadas daban HARD falso).
2. **HARD solo con contenido confiable** (rendered o static_rich); si no → SOFT.
3. **Detección de muro de cookies/login** → SOFT, no HARD.
4. **Ventana de 6000 chars a Gemini** (un edital más allá de 3000 daba "sem itens").
5. **Guard anti-poda:** si la página tiene ≥2 números de edital distintos
   (NN/AAAA), se anula un `nao_e_indice` del modelo (un listado real gana sobre el
   "parece detalle"). Este guard llevó la validación golden a **0 errores**.

### Validar el auditor contra verdad de campo (hazlo tras tocar el auditor):
```bash
python scripts/eval/validate_golden_audit.py convert
python scripts/eval/audit_fase2_rs.py --input /tmp/golden_as_pipeline.csv --render --ai-all --detalle
python scripts/eval/validate_golden_audit.py compare   # 0 ERRORES = auditor confiable
```

---

## 7. Cómo correr (comandos y flags)

### Pipeline fase 2 (cascade)
```bash
# Una corrida por letras, acumulando, sin re-gastar Gemini en confirmados:
python scripts/fase2_municipios/cascade_municipios_rs.py \
  --all --letras ab --append --skip-existing \
  --output data/fase2/municipios_rs_local.csv

# Un solo municipio (debug):
python scripts/fase2_municipios/cascade_municipios_rs.py --municipio "Caxias do Sul" \
  --output /tmp/uno.csv
```

Flags clave:
- `--all` carga los 497 (fuente TCE). `--golden` corre solo los 24 golden.
- `--letras ab` filtra por inicial (insensible a acentos).
- `--append` fusiona en el CSV existente (no borra lo previo).
- `--skip-existing` salta municipios ya **confirmados** (ahorra Gemini); reintenta
  `revisar`/`sin resultado`. Fuerza append.
- El CSV se escribe **tras cada municipio** (checkpoint): si se corta, re-corre el
  mismo comando y retoma donde quedó.

### Gate de regresión (tras CUALQUIER cambio de lógica de verify/select/tier)
```bash
python scripts/fase2_municipios/cascade_municipios_rs.py --golden --output /tmp/golden_check.csv
python scripts/eval/medir_golden_set.py \
  --golden authority_first/data/golden_set_v1.csv --pipeline /tmp/golden_check.csv --detalle
# Espera: automatable WRNG=0 / F-POS=0. Si aparece un nuevo WRNG/F-POS automatable, es regresión.
```

### Auditor / monitoreo (recurrente; también caza link rot)
```bash
python scripts/eval/audit_fase2_rs.py --input data/fase2/municipios_rs_local.csv --render --ai-all --detalle
```

> Guía completa y políticas: `authority_first/docs/RUNBOOK_corridas_locales.md`.

---

## 8. El recorrido / por qué el código es así (decisiones clave)

Para que no "redescubras" estos problemas ni deshagas los fixes:

1. **Geo-block:** muchos `*.rs.gov.br` bloquean tráfico fuera de Brasil (AWS ELB
   "Blocked request this country"). **Por eso se corre desde Brasil.** Resolvió ~26%
   de "sin resultado" falsos.
2. **WAF / fingerprint TLS:** algunos portales (Next.js tras WAF) rechazan el TLS de
   `requests`. `fetch_page` tiene **fallback a `curl_cffi`** (impersona Chrome) ante
   403/406/429/503/error. Recuperó Barros Cassal, Boa Vista do Sul, etc.
3. **SPA/Next.js:** sitios que renderizan el menú por JS → Tier 1 no veía links.
   `Page.is_spa` + render del menú en Tier 1 (gated, solo SPA).
4. **Antibot (DDoS-Guard/Cloudflare "Just a moment"):** se **etiqueta** como
   `bloqueo_antibot` (no se intenta derrotar — es bloqueo de red, va a revisión).
5. **Niveles de confianza:** cada URL lleva `confianza` = `confirmado` / `probable`
   / `revisar`. Verificación híbrida (determinística + batch Gemini) sube
   `probable`→`confirmado`.
6. **Excel coloreado** + URLs clicables para revisión humana cómoda.
7. **Output cumulativo + checkpoint** para corridas largas reanudables.
8. **El patrón cross-tipo** (un bucket apuntando al índice del tipo opuesto) fue la
   clase de FP más sutil. Se decidió **NO** crear un fix de motor frágil (rompería
   combinadas legítimas como Mato Leitão); en su lugar el **auditor con IA** lo caza
   y se corrige el dato a `revisar`.
9. **Disciplina anti-perfeccionismo:** solo se toca código por (a) algo roto, o (b)
   un patrón repetido y general. Un caso aislado → se queda en `revisar`.

Historia de commits relevante (rama actual, más reciente arriba):
`9d03f3c` guard anti-poda · `ff867c8` HARD confiable + muros · `11c41e0` combinadas ·
`3ed2d5a` --render/--ai · `2a5a1a5` auditor base · `bc499c4` antibot Cloudflare ·
`0f43f22` checkpoint CSV · `c6f3a52` render SPA · `1ab1da0` fallback curl_cffi.

---

## 9. Setup local (Windows)

### 9.1 Carpeta y clon
Crea la carpeta y clona dentro:
```powershell
mkdir "C:\Users\Luis Santamaria\Documents\PC\Claude\Concursos Tracker"
cd "C:\Users\Luis Santamaria\Documents\PC\Claude\Concursos Tracker"
git clone https://github.com/luisesantamaria/concursos-tracker.git
cd concursos-tracker
git checkout claude/skill-files-accuracy-vd6uyt
```

### 9.2 Entorno Python
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 9.3 Llave de Gemini
```powershell
$env:GEMINI_API_KEY="tu_llave"     # para que persista: setx GEMINI_API_KEY "tu_llave"
```

### 9.4 Acceso de push a GitHub (Personal Access Token)
El push por HTTPS pide credenciales. Crea un PAT en GitHub (Settings → Developer
settings → Tokens → Fine-grained, acceso a `luisesantamaria/concursos-tracker`).
Al primer `git push`, usa tu usuario y pega el PAT como contraseña; el Credential
Manager de Windows lo guarda. (Alternativa: `gh auth login`.)

### 9.5 El CSV de datos (§3)
Copia `data/fase2/municipios_rs_local.csv` (y `.xlsx`) de tu entorno anterior a
`data/fase2/` en este clon. Si no existe, regenéralo con el pipeline completo.

---

## 10. Roadmap

- **Fase 2 (actual):** cerrar con la auditoría completa `--render --ai-all` → bajar
  FP reales a `revisar` → set de índices confirmados limpio y verificado.
- **Fase 3 (siguiente):** *scanner de índices* — entrar a cada página confirmada y
  extraer los editais/PDFs individuales (número, órgano, banca, fechas, status). El
  render de navegador que ya tiene el auditor es el primer ladrillo de esto.
- **Más adelante:** dedup/identidad cross-fuente, fechas y status del ciclo de vida,
  re-verificación periódica (link rot) con el auditor.

---

## 11. Checklist de arranque para la sesión local

1. [ ] Leí este archivo, `CLAUDE.md` y `AGENTS.md` completos.
2. [ ] Cloné el repo en la carpeta correcta y estoy en la rama
       `claude/skill-files-accuracy-vd6uyt`.
3. [ ] venv + requirements + `playwright install chromium` listos.
4. [ ] `GEMINI_API_KEY` seteada; push a GitHub funcionando (PAT).
5. [ ] Copié `data/fase2/municipios_rs_local.csv` (o decidí regenerarlo).
6. [ ] `git pull` antes de correr; `git push` después de cada cambio.
7. [ ] Próximo paso: auditoría completa `--render --ai-all` sobre las 497.

Bienvenida, sesión local. Tienes todo el contexto. Continúa desde el paso 7.
