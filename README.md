# Coleta e conferência territorial — Mogi das Cruzes e Itanhaém

Pipeline em Python para coletar dados territoriais publicados por fontes municipais, preservar a origem de cada informação e permitir conferência local em uma interface com mapa. Mogi das Cruzes e Itanhaém permanecem separados porque as fontes e o significado dos dados são diferentes.

> **Estado atual:** o recorte público de Mogi referente a 2024 foi processado e documentado; Itanhaém possui somente a camada pública de arruamento identificada; a continuidade fiscal aceita importações locais autorizadas, mas não consulta automaticamente portais fiscais.

O projeto ainda não possui nome comercial e não deve ser apresentado como cadastro oficial, certidão ou produto jurídico.

## Situação por município

| Frente | Situação | Limite principal |
| --- | --- | --- |
| Mogi — cadastro imobiliário | Recorte de 2024 processado | A origem continua sendo o pacote público municipal |
| Mogi — valor venal | Histórico de 2016 a 2024 vinculado por `cadastro_id` | Valor publicado não é certidão nem situação fiscal |
| Mogi — georreferência | Referência por centro de quadra ou logradouro | Não representa a geometria exata do lote |
| Itanhaém | Arruamento público em tiles vetoriais | `gid` não é inscrição, lote ou proprietário |
| Situação fiscal | Importação local separada | O sistema não consulta sozinho o portal municipal |

## Escopo

- download retomável de recursos públicos;
- checkpoint SQLite e repetição controlada de requisições;
- preservação dos identificadores e campos originais;
- vínculo de cadastro, valor venal e referência territorial;
- cruzamento do ponto aproximado com bairro, distrito, macrozona e zoneamento;
- exportações separadas por município;
- interface local para pesquisa, filtros, fichas e mapa;
- importação controlada de certidões ou situações fiscais já obtidas legitimamente.

O fluxo ativo não baixa boletos e não armazena CPF, CNPJ, nome de proprietário, linha digitável, pagamento ou guia.

## Arquitetura

```text
fontes municipais públicas
          │
          ▼
extrator_imobiliario_mestre.py
          │
          ├── sessão HTTP e retomada
          ├── normalização e rastreabilidade
          ├── checkpoint SQLite
          ├── vínculo venal e territorial
          └── exportações por cidade
          │
          ▼
data/atual/checkpoint.sqlite
          │
          ├── resultados/mogi/
          ├── resultados/itanhaem/
          └── interface_mogi.sqlite
                         │
                         ▼
             servidor_conferencia_mogi.py
                         │
                         ▼
                  interface_mogi/
```

Responsabilidades detalhadas e regras de manutenção estão em [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md).

## Stack

- Python 3.10 ou superior;
- `requests` para HTTP;
- `mapbox-vector-tile` para tiles vetoriais;
- SQLite da biblioteca padrão para checkpoint e índice local;
- HTML, CSS, JavaScript e Leaflet na interface de conferência;
- `unittest` para regras de coleta, vínculo e servidor local.

## Preparação

No Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Em Linux ou macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## Validação barata

Antes de iniciar uma coleta:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile extrator_imobiliario_mestre.py servidor_conferencia_mogi.py
```

Esses comandos validam regras locais e sintaxe. Eles não comprovam que um portal externo continua disponível ou com o mesmo contrato.

## Coleta e atualização

Consulte a ajuda antes de executar:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py --help
```

Comece com limites conservadores:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py mogi --limit-mogi-resources 1
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py itanhaem --limit-tiles 1
```

Em Itanhaém, `--limit-tiles 1` limita diretamente os tiles consultados. Em Mogi, `--limit-mogi-resources 1` limita os recursos cadastrais divididos, mas camadas geoespaciais auxiliares ainda podem ser baixadas integralmente; confira `--help`, o código e as regras da fonte antes de executar.

Retomar Mogi usando o checkpoint existente:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py mogi
```

Reprocessar Mogi e completar o histórico público desde 2016:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py mogi --mogi-historico-desde 2016 --force
```

Atualizar somente as camadas territoriais de contexto:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py contexto-mogi
```

O checkpoint permite retomar a execução. Ainda assim, `--force` deve ser usado somente depois de preservar a base atual e entender o custo do reprocessamento.

## Interface local

No Windows, execute `abrir_interface_mogi.bat` ou:

```powershell
.\.venv\Scripts\python.exe servidor_conferencia_mogi.py
```

O servidor escuta apenas em `127.0.0.1`. A busca e as fichas usam a base local; o mapa-base do OpenStreetMap depende de internet. A interface permite pesquisar por cadastro, referência territorial, rua, bairro ou zona, além de conferir áreas, uso, valores e contexto espacial.

## Resultados

```text
data/atual/
├── checkpoint.sqlite
└── resultados/
    ├── mogi/
    │   ├── cadastros_individuais.csv
    │   ├── valores_venais_por_cadastro.csv
    │   ├── historico_valores_venais_por_cadastro.csv
    │   ├── locais_agrupados.csv
    │   ├── dados_territoriais.csv
    │   ├── mapa_territorial.geojson
    │   └── manifesto.json
    ├── itanhaem/
    └── consolidado/
```

`cadastros_individuais.csv` é a visão cadastral de Mogi. `locais_agrupados` é uma visão territorial e não deve substituir os cadastros individuais. O histórico venal fica separado para impedir que um valor antigo seja mostrado como atual.

## Identificadores de Mogi

- `cadastro_id` identifica cada cadastro individual e é a chave usada no vínculo com o histórico venal;
- `id_local` é uma referência territorial que pode ser compartilhada por vários cadastros.

O programa não deduz significado jurídico dos trechos de `id_local` e nunca consolida unidades diferentes usando somente esse campo.

## Evidência do recorte de Mogi

Na execução validada em **20 de agosto de 2026**:

- 233.377 cadastros imobiliários individuais de 2024;
- 13.483 referências `id_local` distintas;
- 177.689 cadastros com algum valor venal público;
- 160.366 cadastros com valor do exercício de 2024;
- 17.323 cadastros preenchidos pelo exercício histórico mais recente;
- 55.688 cadastros sem valor nas bases consultadas;
- 1.382.284 registros no histórico de 2016 a 2024;
- 230.802 cadastros com referência espacial e 2.575 sem ponto;
- 230.404 cadastros associados a algum contexto de bairro, distrito, macrozona e zoneamento.

Essas contagens são uma fotografia daquela execução, não uma promessa de atualização em tempo real.

## Importações privadas e configuração segura

Materiais autorizados podem ser importados sem misturá-los à coleta pública:

```powershell
.\.venv\Scripts\python.exe extrator_imobiliario_mestre.py importar-fiscal `
  --fiscal-file "entrada_privada\situacao_fiscal.csv" `
  --fiscal-cidade mogi
```

`data/`, `entrada_privada/`, `documentos_privados/`, `capturas_locais/`, perfis de navegador, cookies e arquivos `.env` são ignorados pelo Git. Não mova resultados reais para pastas versionadas e não inclua credenciais em parâmetros, logs ou documentação.

## Limites e uso responsável

- Confirme termos, disponibilidade e finalidade antes de consultar uma fonte.
- Não contorne autenticação, bloqueios ou limites de requisição.
- Georreferência por ponto de referência não é levantamento topográfico.
- Bairro e zoneamento descrevem o contexto do ponto e exigem confirmação municipal para uso jurídico.
- Valor venal publicado não comprova quitação nem pendência fiscal.
- A camada de lote que exigiu usuário autorizado não é acessada nem contornada pelo projeto.

As fontes e as limitações de cada campo estão registradas em [`docs/FONTES_E_LIMITES.md`](docs/FONTES_E_LIMITES.md).

## Roadmap e continuidade

- [Status e próximos passos](STATUS_E_PROXIMOS_PASSOS.md)
- [Arquitetura](docs/ARQUITETURA.md)
- [Fontes e limites](docs/FONTES_E_LIMITES.md)
- [Continuidade fiscal de Mogi](docs/CONTINUIDADE_MOGI.md)

O próximo marco é consolidar o trabalho em evolução, revalidar uma amostra autorizada e manter manifestos, schemas e comparações entre exercícios reproduzíveis.
