# HANDOFF — Sesión grounded verify + mano-negra Chrome (2026-06-30)

> Documento para **continuar exactamente donde quedamos** en una sesión nueva.
> Todo lo de código/datos está **commiteado y pusheado** en la rama
> `claude/skill-files-accuracy-vd6uyt`. Nada se perdió por el reinicio.

---

## 1. ESTADO ACTUAL (lo seguro)

**Dataset:** `data/fase2/municipios_rs_local.csv` (497 municipios RS)

| Métrica | Valor |
|---|---|
| 🟢 **Pleno** (ambos buckets confirmados) | **413 (83.1%)** |
| 🟡 Parcial | ~70 |
| 🔴 Sin sitio | ~14 |

**Git:** rama `claude/skill-files-accuracy-vd6uyt`, working tree limpio, último commit
`bd53562` ("Mano-negra Chrome: +5 plenos verificados (pleno 408->413, 83.1%)").

**Cómo correr (recordatorio):**
```bash
cd "<repo>"  # .venv ya existe (virtualenv con python embebido)
# pipeline:
.venv/Scripts/python.exe scripts/fase2_municipios/cascade_municipios.py --help
# golden gate (tras CUALQUIER cambio de lógica):
.venv/Scripts/python.exe scripts/fase2_municipios/cascade_municipios.py --golden --output C:/tmp/golden_check.csv
.venv/Scripts/python.exe scripts/eval/medir_golden_set.py --golden authority_first/data/golden_set_v1.csv --pipeline C:/tmp/golden_check.csv --detalle
```
La **API key** de Gemini está en el entorno (`GEMINI_API_KEY`). Modelo `gemini-2.5-flash`.

---

## 2. QUÉ CONSTRUIMOS ESTA SESIÓN (commiteado)

Trayectoria del pleno: **396 → 405 → 407**. Commits clave (de viejo a nuevo):

1. **Mejora A — página combinada** (`0cdb44c`): `_try_combined_fill` ahora renderiza SPA,
   exige señal del otro tipo + ≥2 items de listado, y `_combined → probable` (no `revisar`)
   para que el batch verify la procese. Golden F-POS=0.
2. **Caso 3** (`e7c3c7f`): corrida de A sobre buckets vacíos → 8 ascensos (fusión monótona).
3. **Grounded verify** (`0d5c7f3`): `--grounded-verify` + función `grounded_verify_one`.
   Para los `probable` con **preview vacío** (Cloudflare/SPA), pregunta a Gemini con
   `google_search` → lee el **índice de Google** (el crawler de Google pasa el antibot que
   el Playwright headless no). **Guardarraíl:** confirma solo si `≥1 grounding chunk` +
   `evidencia ≥15 chars`. 0 chunks = inferencia → revisar. Golden F-POS=0.
4. **Parseo robusto** (`d95f162`): `maxOutputTokens 1024→2048` + parseo tolerante (fences,
   JSON truncado, fallback regex). Recuperación del grounded **38% → 58%**.
5. **Renombrado** (`b0e4426`): `cascade_municipios_rs.py` → **`cascade_municipios.py`**
   (state-agnostic, listo para RJ). Docstring documenta el **orden real de 8 pasos**.
   Import actualizado en `audit_fase2_rs.py` + comandos en CLAUDE.md/HANDOFF/RUNBOOK.
6. **Cierre grounded** (`f8a131f`): cosecha sobre los 42 buckets `probable` con URL →
   23 confirmó grounded, pero **5 eran FP de tipo** (licitação/nomeação) revertidos a mano
   → **18 ascensos netos** (pleno 396→405).
7. **+2 Chrome** (`ac4757a`): Itati y Caibaté verificados a mano → pleno 405→407.

**Reproducibilidad de la mano-negra:** de los 41 plenos que Luis confirmó a mano,
**44% (solo pipeline) → 77% (con grounded)**. El resto (~20) es techo: JSF/SPA que Google
ni indexa = revisión humana.

---

## 3. APRENDIZAJES CLAVE (no repetir errores)

- **Gemini rinde con LIBERTAD, no con corsé.** Afinar el prompt grounded (site:host,
  exigir dominio, desambiguar homónimos) BAJÓ la recuperación de **58% → 20%**. Revertido.
  El prompt simple/abierto es el techo (58%).
- **El guardarraíl NO valida TIPO.** El grounded confirma "lista múltiplos editais" aunque
  sean de licitação/nomeação. Probamos 2 filtros de tipo:
  - Filtro de palabras burdo → tumbó combinadas válidas (mencionan varios tipos).
  - Filtro refinado (`tipo_malo AND NOT tipo_correcto`) → **tampoco**, porque el **grounded
    es NO-DETERMINISTA**: los mismos 23 casos dan veredictos distintos cada corrida.
  - **Conclusión: descartado.** Un filtro determinista sobre base no-determinista no se
    estabiliza. Los 5 FP del cierre se manejaron a mano. NO reintentar este filtro.
- **El residuo de parciales NO tiene patrones codificables.** Barrido manual de 8 casos:
  fallos heterogéneos (atende muerto, SSL roto, renderer frozen, tipo equivocado, atos de
  nomeação) → revisión humana caso por caso, no código.
- **Costo:** `--skip-existing` es la palanca real de ahorro (salta confirmados). La cascada
  es barata: la mayoría resuelve en Tier 1+3 (1 llamada Gemini). No fusionar capas (arriesga
  precisión por centavos).
- **Aplicar grounded sobre URLs existentes (sin re-descubrir) = vía barata** para auditar/
  cosechar. El pipeline re-descubre desde cero y pierde las URLs Cloudflare (dan `nothing`).

---

## 4. MANO-NEGRA CHROME — DÓNDE QUEDAMOS (lo que sigue)

Luis quiere **MANO NEGRA TOTAL**: no solo verificar la URL guardada, sino **encontrar la
correcta** — dar click a menús + **buscar en Google** + ir más profundo hasta dar con el
índice real, y confirmarlo. (Como cuando se halló `derrubadas-rs.com.br/site/...` en Google.)

**Lote de trabajo: 19 candidatos** (los parciales a 1 paso de pleno + probable/probable).

### Ya verificados este sesión (8):
| Municipio | Bucket | Veredicto | Nota |
|---|---|---|---|
| **Itati** | C | ✅ CONFIRMADO (aplicado) | índice transparência con filtros, vacío |
| **Caibaté** | C | ✅ CONFIRMADO (aplicado) | "Concursos e Seleções" lista múltiples |
| Gramado | C | ❌ FP | atende URL muerta → busca genérica "Páginas" |
| São Pedro do Sul | C/P | 🟡 borderline | atende busca con 1 PSS + ruido |
| Candiota | P | ⚙️ frozen / dudoso | URL de *concursos* en bucket *processos* (tipo) |
| Maratá | C | ❌ tipo equivocado | `/site/editais` lista PSS, no concursos |
| São José do Sul | C | 🔒 SSL roto | "Error de privacidad" |
| Torres | C | ❌ FP | `/categorias/concurso` = atos de nomeação |

### RESUELTO (sesión 30-jun, lote de 11) — pleno 407→413:

**Vacaria (C/P)** ✅ CONFIRMADO — `/concurso` índice oficial combinado (vacío, criterio Itati).

**+5 confirmados a mano (probable→confirmado, monótono):**
| Municipio | Bucket | URL final | Evidencia |
|---|---|---|---|
| São José do Norte | P | `/portal-da-transparencia/processos-de-selecao` | "Processos de Seleção", 819 editais PSS |
| São Nicolau | C | `/site/concursos` (URL corregida) | "Concursos e Seleções Públicas", múltiplos |
| Caraá | C | `…:8079/transparencia/HomeConcursos.aspx` (corregida) | índice oficial vacío (criterio Itati) |
| General Câmara | P | `/concurso` (corregida) | índice combinado "Editais de Concursos" |
| Gentil | C | `/concurso.php` | índice combinado, múltiplos PSS |

**5 RECHAZADOS (quedan probable → revisión humana, sin índice oficial real):**
| Municipio | Bucket | Razón |
|---|---|---|
| Jaquirana | P | SCPI roto (error BD); sitio solo tiene "Concursos Públicos"; PSS solo en bancas externas (Objetivas/Legalle) |
| Canoas | P | solo tag de **noticias** (artículos, no editais); categorías oficiales sin PSS |
| Dom Pedrito | P | "Publicações e Editais" genérico, dominado por dispensa de licitação |
| Parobé | C | `/editais` lista editais por secretaría; concursos solo páginas por año, sin índice |
| Cruz Alta | C/P | concurso único externo (Fundatec) + archivo "Editais/Decretos/Avisos" mixto; sin índice PSS |

**Aprendizaje portales (reutilizable):**
- **SCPI/msgestaopublica** (`:8079/transparencia/`): el índice de concursos vive en el iframe
  `/transparencia/HomeConcursos.aspx` (menú Pessoal → "Concursos/Teste Seletivo"). Combinado
  (concursos+teste seletivo). Navegar directo al .aspx. Algunos dan error de BD = inútil.
- **CMS "portal-da-transparencia"** (S.J.Norte, Jaquirana, Dom Pedrito): secciones como rutas
  `/portal-da-transparencia/<slug>`. Buscar `/processos-de-selecao` o `/concursos-publicos`.
  Si solo hay "Publicações e Editais" genérico = NO es índice PSS (mezcla licitação).
- **CMS "/site/"** (Caibaté, S.Nicolau): ruta dedicada `/site/concursos` = índice combinado
  limpio; mejor que `/site/editais?tipo=NNNN` (filtro de un solo concurso).
- **atende.net** (`/cidadao/pagina/<slug>`): concursos suelen ser páginas POR AÑO
  (`concurso-publico-2024`), sin índice combinado → suelen quedar a revisión humana.

**Truco técnico útil (Chrome):** `get_page_text` solo agarra menús en estos sitios. Usar
`javascript_tool` para extraer el contenido principal (liviano y preciso):
```js
(()=>{const el=document.querySelector('main,#conteudo,.conteudo,#content,.content,article')||document.body;return el?el.innerText.replace(/\s+/g,' ').trim().slice(0,700):'(vacio)';})()
```
Para SPA que tarda: navegar PRIMERO, ejecutar el JS en llamada SEPARADA (el batch
navigate+JS falla por timing). Algunos sitios tienen navegación "pegada" → re-navegar.

**Criterio de confirmación (reglas del proyecto):** índice/listagem que muestra MÚLTIPLES
concursos/PSS del **tipo correcto**. Rechazar: PDF directo, edital individual, licitação/
pregão/dispensa, atos de nomeação, concurso cultural (soberanas/rainhas/fotografia).
Páginas índice oficiales VACÍAS (sin items ahora) pero con estructura correcta = válidas
(criterio Itati/Vacaria). Si la URL no sirve → buscar la correcta en Google y navegar.

**Al confirmar, aplicar al CSV (monótono, solo sube probable→confirmado) + commit:**
nota `rev_humana(Chrome): indice <tipo> verificado`.

---

## 5. PRÓXIMOS PASOS (en orden)

1. **Lote de 11 RESUELTO** (sección 4): Vacaria + 5 confirmados (413, 83.1%), 5 a revisión
   humana. El techo realista (~412-415) está prácticamente alcanzado.
2. **Cerrar fase 2.** 83.1% con cero FP es el resultado defendible (el proyecto acepta ~20%
   revisión humana). Los ~70 parciales restantes son residuo sin índice oficial real
   (bancas externas, news tags, repos genéricos de licitação, atende por-año). Si se quiere
   exprimir más: barrer parciales restantes uno a uno en Chrome, pero rinde poco (mismo perfil
   que los 5 rechazados). Los 5 rechazados de este lote quedan documentados para no re-verificar.
3. **Mejoras futuras anotadas** (NO urgentes, post-RS):
   - Parametrizar `--uf` (UF_SIGLA/UF_NOME están hardcodeados líneas 43-44) para reusar en RJ.
   - Propagar el grounded verify al **auditor** (`audit_fase2_rs.py`) para que no marque
     falsos HARD en Cloudflare.
   - (Descartado: filtro de tipo en grounded — no estabilizable por no-determinismo.)
4. **Fase siguiente** (cuando fase 2 cierre): extraer editais de los índices, o dejar el
   auditor corriendo para mantenimiento (detectar link rot).

---

## 6. CONTEXTO DEL PROYECTO (recordatorios)

- **Quién:** Luis corre todo local (en Brasil, sin geo-block), único editor, pushea a GitHub.
  Responder en **español**. Es el árbitro visual (juzga manos/caras en B3LL3; aquí juzga URLs).
- **Reglas de oro:** precisión > cobertura, **cero falsos positivos**, no inventar URLs, no
  scorers numéricos, no hardcodear patrones de portal (multi24, IPs, atende). Iterar barato.
  `git pull` antes de correr, `git push` después. Nunca commitear secrets/.env/keys.
- **Arquitectura (8 pasos reales, tiers numerados por COSTO no orden):** Tier 0 sitio →
  Tier 1 links gratis (+render SPA) → Tier 3 IA clasifica → Combined fill (A) → Tier 2
  grounded search → Directed grounding → Tier 4 Playwright → [final] Batch verify +
  Grounded verify. Confianza: confirmado/probable/revisar → pleno/parcial/sin.
- **Golden gate:** `medir_golden_set.py` (24 municipios). Criterio automatable: WRNG=0/F-POS=0.
- **Auditor** (`audit_fase2_rs.py`): componente SEPARADO de mantenimiento (re-visita
  confirmadas, detecta link rot, OK/SOFT/HARD). No se elimina.
- **Memoria** (`~/.claude/.../memory/`): ver `project_grounded_verify.md`,
  `project_verificacion_probable.md`, `project_auditor_sesgo_tipo_equivocado.md`.

---

*Para retomar: lee este archivo, haz `git pull`, reconecta Chrome, y continúa la
mano-negra total desde Vacaria + los 10 pendientes (sección 4).*
