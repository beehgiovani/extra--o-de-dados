# Arquitetura do projeto

Este documento mostra como organizei o projeto e onde cada responsabilidade deve permanecer. A intenção é permitir que outro desenvolvedor continue o trabalho sem misturar coleta, dados derivados e interface.

## Fluxo principal

```text
fontes municipais
       │
       ▼
extrator_imobiliario_mestre.py
       │
       ├── normalização e rastreabilidade
       ├── checkpoint e retomada
       ├── vínculo venal e territorial
       └── exportações separadas por cidade
       │
       ▼
data/atual/checkpoint.sqlite
       │
       ├── resultados/mogi
       ├── resultados/itanhaem
       └── interface_mogi.sqlite, índice regenerável
                                  │
                                  ▼
                       servidor_conferencia_mogi.py
                                  │
                                  ▼
                         interface_mogi/
```

## Código ativo

| Caminho | Responsabilidade |
|---|---|
| `extrator_imobiliario_mestre.py` | coleta, checkpoint, normalização, cruzamentos e exportações |
| `servidor_conferencia_mogi.py` | índice de pesquisa, API somente local e arquivos estáticos |
| `interface_mogi/app.js` | busca, mapa, paginação e ficha cadastral |
| `interface_mogi/styles.css` | apresentação da interface |
| `abrir_interface_mogi.bat` | abertura local no Windows |
| `tests/` | regras que não podem regredir durante a continuidade |

## Organização interna do extrator

O arquivo principal ainda é grande, mas está dividido em blocos estáveis:

1. `CheckpointStore`: banco, estados, recursos e gravações em lote;
2. `PersistentHttpClient`: sessão HTTP, repetição controlada e cookies locais;
3. funções geoespaciais: tiles, coordenadas e teste de ponto em polígono;
4. extrator de Itanhaém;
5. extratores de Mogi e camadas do GeoMogi;
6. vínculo de valor venal e georreferência;
7. importações locais de documentos já obtidos;
8. exportadores;
9. interface de linha de comando.

Uma futura separação em módulos deve seguir esses limites. Não vale mover funções apenas para diminuir o tamanho do arquivo se isso quebrar o checkpoint ou duplicar regras de normalização.

## Identificadores de Mogi

| Campo | Uso confirmado no projeto |
|---|---|
| `cadastro_id` | chave da linha cadastral individual e do histórico venal |
| `id_local` | referência territorial que pode ser compartilhada por cadastros diferentes |
| `numero_imovel_quadra` | número publicado no cadastro |
| `complemento` | descrição textual publicada para a linha cadastral |

O código preserva os valores originais e também mantém versões normalizadas apenas para busca e vínculo. Nenhuma etapa deve consolidar registros diferentes usando somente `id_local`.

## Bancos e arquivos gerados

`data/atual/checkpoint.sqlite` é a fonte local retomável. O banco `data/atual/interface_mogi.sqlite` é derivado e pode ser reconstruído quando o checkpoint ou os agrupamentos mudam.

As exportações ficam em:

```text
data/atual/resultados/
├── mogi/
├── itanhaem/
└── consolidado/
```

Dados gerados, documentos privados, capturas de portal e perfis de navegador ficam fora do Git por meio do `.gitignore`.

## Regras de manutenção

- Toda nova fonte deve ter nome interno estável e URL registrada.
- Toda gravação retomável deve passar pelo checkpoint.
- Um campo derivado precisa indicar método ou fonte quando puder ser confundido com dado oficial.
- Geometria aproximada nunca deve ser apresentada como polígono do lote.
- A lista pode ser paginada, mas o mapa agrega todas as coordenadas compatíveis com os filtros.
- Alterações no esquema devem ser compatíveis com bancos existentes ou possuir migração explícita.
