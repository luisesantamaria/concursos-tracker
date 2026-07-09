# Roadmap - Authority First RS

## Fase A - Esqueleto y contrato de scope ✅

- Crear estructura V2.
- Declarar matriz fuente x evento.
- Crear filtro duro RS.
- Copiar V1 Ache-first a laboratorio.

## Fase A2 - Tabla base MVP ✅

Antes del timeline completo de eventos, crear `concursos_base_rs`.

Columnas esenciales: semaforo, tipo, orgao, municipio, numero, banca, pagina_oficial, edital_abertura_url.

## Fase B - Crawlers de bancas RS ✅

Legalle, La Salle, Fundatec, Quadrix, Objetiva, Cebraspe, Selecao/Fenix.

## Fase C - Descubrimiento de recursos municipales 🔄

Objetivo: descubrir la **pagina indice/listado estable** de concursos y processos seletivos de cada municipio RS.

**IMPORTANTE:** Esta fase NO extrae editais individuales. La salida es la URL de categoria/indice donde la prefeitura lista todos los concursos o PSS.

### Arquitectura: Cascata de 5 Tiers

```
Tier 0: Site oficial (dominio base)
Tier 1: Links gratuitos (menus HTML, anchors, transparencia)
Tier 2: Grounded search (Gemini + Google, solo si falta)
Tier 3: Gemini verificador/selector (ai_pick_best entre candidatas)
Tier 4: Agente de navegacion Playwright (ultimo recurso, dirigido)
```

### Reglas clave

- Verificacion por CONTENIDO, no por slug de URL.
- Sin scorer numerico: decisiones discretas + ai_pick_best.
- Precision sobre cobertura: zero falsos positivos.
- ~20% requiere revision humana — aceptable.
- No hardcodear patrones de un proveedor de portal.

### Medicion

- Golden set: 24 municipios verificados a mano.
- Script `medir_golden_set.py` mide precision y cobertura por tipo.
- Ejecutar despues de CUALQUIER cambio al verificador o selector.

### Estado actual - 2026-07-08

La ejecucion de nuevos chunks de run497 esta pausada por calidad antes de seguir cobertura. La triage manual sobre los 618 buckets confirmo FPs en Itaara, Canudos y Estrela, con una cola de ~30 municipios dudosos para auditar uno por uno.

Familias de FP detectadas: noticias individuales clasificadas como indice, paginas genericas de menu sin listado real, y sobre-conteo por resultados duplicados/inflados. Primero mapear la cola, corregir cada familia en el pipeline y validar contra golden; solo despues retomar fase 2 chunks 5-6.

### Pendencias

- [ ] Implementar ai_pick_best (reemplazar scorer).
- [ ] Headers de navegador real (fix 406).
- [ ] Cache de grounding por municipio.
- [ ] Deteccion de JS + fallback Playwright dirigido (Tier 4).
- [ ] Escalar golden set con municipios de otras letras.
- [ ] Auditar la cola dudosa de run497 y clasificar familias de FP antes de continuar chunks 5-6.

## Fase D - Scanner de indices (siguiente)

Objetivo: entrar a cada pagina indice descubierta en Fase C y extrair editais/eventos individuales con scraping.

Salida por evento: titulo, tipo_evento, url_documento, url_pdf, edital_num, data, hash, first_seen.

Esta fase construye el dataset de concursos. Fase C dio la puerta de entrada; Fase D entra y cataloga el contenido.

## Fase E - Diario municipal/FAMURS

Objetivo: cobrir homologacoes, convocacoes, nomeacoes y eventos administrativos.

- Adapter de Diario Municipal FAMURS para municipios sin ruta clara.
- Adapters dedicados para ~15 municipios grandes con DOM propio.

## Fase F - Normalizacion

Resolver identidad de concurso, tipo, edital_num, orgao, municipio, banca, estado del ciclo.

## Fase G - Auditor Ache

Ache deja de ser fuente principal y se convierte en comparador: concursos que faltan en el master, falsos positivos, recall por banca/municipio.
