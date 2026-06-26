# Architecture - Authority First

## Problema

Ache Concursos es util como radar, pero no es autoridad final y esta sesgado hacia concursos recientes. Eso deja fuera:

- processos seletivos pequenos publicados solo en prefeitura o diario;
- concursos antiguos que siguen vigentes para convocacao/nomeacao;
- homologacoes y posses que la banca ya no publica;
- eventos administrativos que nunca aparecen en Ache.

## Modelo

El sistema separa dos entidades:

1. `Concurso`: registro madre usado para matching.
2. `Evento`: documento o hecho del ciclo de vida usado para alertas.

Un concurso puede tener muchos eventos.

## Matriz fuente x evento

| Evento | Autoridad principal | Respaldo |
|---|---|---|
| edital_abertura | banca | prefeitura / diario |
| retificacao | banca | diario |
| cronograma | banca | null |
| inscricao | banca | null |
| gabarito | banca | null |
| resultado_clasificados | banca | diario |
| homologacao | diario | banca |
| convocacao | diario + prefeitura | null |
| nomeacao | diario + prefeitura | null |
| posse | diario + prefeitura | null |

## Regla RS

Ningun crawler puede escribir a `data/raw` sin pasar por el filtro RS.

Una fila se acepta solamente si tiene al menos una de estas evidencias:

- `uf == RS`;
- municipio identificado en la whitelist de 497 municipios RS;
- URL oficial de prefeitura RS validada desde `sites_municipios_rs.csv`;
- banca con titulo/contexto que contiene municipio RS o sigla `RS`;
- diario/FAMURS con municipio RS.

Si solo hay una palabra vaga como "gaucho", "sul", o un dominio de banca sin municipio, la fila se marca como `out_of_scope_pending` y no entra al master.

## Pipeline de Descubrimiento Municipal: Cascata de 5 Tiers

El descubrimiento de paginas indice de concursos/PSS por municipio usa una cascata que gasta herramientas caras solo cuando las baratas fallan:

```
Tier 0 — Site oficial
  Encontrar o confirmar el dominio base de la prefeitura.
  Probar .rs.gov.br y .atende.net. Seguir avisos de mudanza.

Tier 1 — Links gratuitos (requests)
  Leer menus HTML, anchors, sitemap, portal da transparencia.
  Seguir links intermedios (Editais, Documentos, Publicacoes).
  Verificar cada candidata por CONTENIDO, no por slug.
  Costo: cero. Tiempo: ~1-2s por municipio.

Tier 2 — Grounded search (Gemini + Google)
  Solo si Tier 1 no completo ambos buckets.
  Una llamada con google_search por municipio.
  Costo: ~$35/1000 prompts + tokens.
  Gemini solo DESCUBRE URLs. El codigo verifica.

Tier 3 — Gemini verificador/selector
  Recibe candidatas verificadas y toma decisiones discretas.
  Cuando hay multiples candidatas validas: ai_pick_best
  elige la mejor pagina indice por comprension de contenido.
  NO usa scorer numerico ni constantes magicas.
  Costo: una llamada barata sin grounding, solo para empates.

Tier 4 — Agente de navegacion (Playwright)
  Ultimo recurso. Abre el site en Chromium headless.
  Navegacion DIRIGIDA: sigue menus por texto (Publicacoes,
  Concursos, Transparencia), no crawl ciego del site entero.
  Resuelve: botones que saltan a IP crudo (Ararica),
  paginas renderizadas por JS, portales embebidos.
  Costo: ~3-5s/pagina, ~300-500MB RAM.
  Reusa UNA instancia de browser para toda la corrida.
```

### Principios de la cascata

- El DESCUBRIMIENTO lo hace requests gratis → Gemini/Google → Playwright.
- La VERIFICACION la hace codigo deterministico: contenido manda sobre slug.
- La SELECCION entre candidatas la hace Gemini (ai_pick_best): comprension, no puntos.
- Nunca se emite una URL sin verificar contra contenido real.
- Precision sobre cobertura: si no esta seguro, queda vacio o revisar.
- No hardcodear patrones de proveedor (multi24, subareas, IPs crus).

### Que es una URL valida en esta fase

Aceptamos: pagina indice, pagina de categoria, listado, portal con todos los concursos/PSS, pagina con filtros/lista/cards de varios eventos, pagina padre.

Rechazamos: PDF directo, noticia individual, pagina de un edital especifico, /detalhe/452/, anexo, cronograma, retificacao, licitacao, concurso cultural (soberanas/rainhas).

### Decisiones discretas (reemplazan scores)

```
indice_oficial             — pagina indice estable encontrada
indice_oficial_combinado   — uma pagina serve para ambos buckets
portal_externo_oficial     — portal externo (IP, atende.net) desde menu oficial
detalle_individual_rechazado — solo encontro detalle, falta indice
licitacao_rechazada        — pagina es de licitacoes
concurso_cultural_rechazado — soberanas/rainhas, no selecao publica
nao_encontrado             — genuinamente no encontrado
revisar                    — ambiguo, necesita ojos humanos
```

## Golden Set

24 municipios verificados a mano como ground truth independiente.

Distribucion de tipos: facil (3), combinada (3), bucket en contenedor (2), portal delegado IP, hermanas ambiguas, site mudado, portal externo hash, combobox, portal embebido, categorias internas, y otros.

~20% requiere revision humana — ese es el techo esperado de automatizacion.

Metrica a optimizar: precision (zero falsos positivos). Cobertura es secundaria.

## Capa Ache

Ache entra despues:

1. Cosecha candidatos.
2. Compara contra `concursos_master`.
3. Si falta algo, crea una tarea de investigacion.
4. Solo se promueve si se encuentra evidencia oficial.
