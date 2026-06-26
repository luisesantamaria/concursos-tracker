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
| resultado_classificados | banca | diario |
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

## Capa Ache

Ache entra despues:

1. Cosecha candidatos.
2. Compara contra `concursos_master`.
3. Si falta algo, crea una tarea de investigacion.
4. Solo se promueve si se encuentra evidencia oficial.

