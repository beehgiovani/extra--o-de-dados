# Fontes, limites e decisões técnicas

Este documento registra as fontes usadas no projeto e as decisões que tomei para não atribuir uma precisão que os dados públicos não oferecem.

## Itanhaém

Uso o TileJSON público de arruamento disponibilizado pela Prefeitura:

- [TileJSON de arruamento](https://www2.itanhaem.sp.gov.br/ortofoto2012/mapa/tileserver.php?/arruamento.json)
- [Índice público do TileServer](https://www2.itanhaem.sp.gov.br/ortofoto2012/mapa/tileserver.php?/index.json)

Essa fonte contém vias, bairros, loteamentos e restrições urbanísticas. O identificador `gid` representa uma feição de arruamento; ele não é uma inscrição imobiliária e não identifica um lote. Por isso, não tento transformar ruas em imóveis nem atribuir geometrias de lote que não foram publicadas.

## Mogi das Cruzes

Uso três fontes públicas complementares:

- [Cadastro Imobiliário no portal de dados abertos](https://dados.mogidascruzes.sp.gov.br/api/3/action/package_show?id=cadastro-imobiliario)
- [Camada pública de quadras do GeoMogi](https://geomogi.mogidascruzes.sp.gov.br/mapa/quadra)
- [Camada pública de logradouros do GeoMogi](https://geomogi.mogidascruzes.sp.gov.br/mapa/logradouro)

O cadastro aberto fornece atributos como inscrição, uso, endereço, áreas e valor venal. Como a geometria individual de lote não está aberta, localizo o cadastro pelo centro da quadra correspondente. Quando não encontro a quadra, uso o centro do eixo do logradouro como referência alternativa. Cada registro recebe `precisao_geometria` e `metodo_georreferenciamento` para deixar esse nível de precisão explícito.

## Certidões de valor venal

O modo `importar-venal` não consulta portais públicos, não emite documentos e não faz varredura de inscrições. Ele recebe arquivos locais de certidões já obtidas legitimamente e guarda apenas inscrição, exercício, valor venal, moeda e metadados mínimos de emissão.

Antes de importar, o projeto bloqueia campos de CPF, CNPJ, proprietário, titular, nome, telefone, e-mail, boleto, pagamento, vencimento, débito e código de barras. O valor da certidão não substitui a fonte pública: ele fica registrado separadamente para manter a origem de cada informação.

## Retomada e rastreabilidade

O SQLite local guarda recursos concluídos, erros, registros normalizados e marcadores de execução. Isso permite retomar uma coleta interrompida sem repetir downloads já finalizados. Os arquivos de saída são reconstruídos a partir do checkpoint, evitando divergência entre a base local e as exportações.

## Limites de uso

O escopo deste repositório é territorial e cadastral público. Não incluo dados de proprietário, documentos pessoais, boletos, pagamentos, débitos ou qualquer informação sujeita a sigilo fiscal. Uma integração institucional futura deve usar fonte oficial autorizada, credenciais próprias e um armazenamento privado separado da camada pública do mapa.
